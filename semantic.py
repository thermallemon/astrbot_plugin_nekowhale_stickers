"""语义选图：向量召回 + 大模型精选。

对标 meme_manager 的 semantic 模式，但省掉了视觉标注这一步——
nekowhale 素材自带 emotion / tags / use / desc 人工标注，直接拿来建库即可。

流程：
    query = 用户这句 + bot 那句
        ↓ bge-m3 (SiliconFlow)
    316 张表情向量里取 top-k 相似
        ↓ deepseek-flash（结构化判断：这张图放到这句对话后面，像不像人顺手甩的）
    选中一张（pick>=1）；模型偶尔返回 0 时退回向量 top1，保证每条回复都有图

为什么两段式：单靠向量 top1 会挑出「情绪对但事情不对」的图（实测「你知道你会
发表情包吗」会被召回成「嘲笑」），加一层 LLM 复核能把错误率压下去，代价是每条
回复多 ~700 token 和 0.5~1s。

本模块只依赖 numpy 与 AstrBot 的 logger，方便脱离框架做离线测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np

from astrbot.api import logger


def _provider_type_of(inst: Any) -> str:
    """取 provider 实例的能力类型，统一成字符串。

    AstrBot 4.x 的 ``ProviderMeta.provider_type`` 是 ``enum.Enum``（不是 str 枚举），
    直接和字符串比较永远为 False，所以这里要取 ``.value``；
    meta() 抛错时退回 provider_config["type"]（如 ``openai_embedding``）做子串判断。
    """
    try:
        ptype = getattr(inst.meta(), "provider_type", "")
    except Exception:
        ptype = ""
    ptype = getattr(ptype, "value", ptype)
    if isinstance(ptype, str) and ptype:
        return ptype
    try:
        raw = getattr(inst, "provider_config", {}).get("type", "") or ""
    except Exception:
        raw = ""
    raw = str(raw)
    if "embedding" in raw:
        return "embedding"
    if "rerank" in raw:
        return "rerank"
    if "speech_to_text" in raw:
        return "speech_to_text"
    if "text_to_speech" in raw:
        return "text_to_speech"
    return raw

CACHE_VERSION = 1
EMB_BATCH_SIZE = 32
DEFAULT_RECALL_K = 10
DEFAULT_MIN_SIM = 0.40
# 历史阈值：早期「向量不够像就不发」用的门限，v2.1 起不再用于拒绝发送
# （低相似度也交给模型挑最不违和的一张），保留常量只为兼容旧配置引用。
FALLBACK_TOP1_MIN_SIM = 0.62

MAX_USER_CHARS = 240
MAX_REPLY_CHARS = 260

SYSTEM_PROMPT = """你是表情包挑选器。输入是一段聊天（用户说了什么、我回了什么）和若干候选表情包。
任务：挑一张紧跟着我这条回复发出去的图。它要像人聊天时顺手甩的一张图，不是硬凑。

判断标准：
1. 候选图上的动作或文字，要和这段对话说的**同一件事**有关，或者正好是这个情绪的反应。只是「都是开心」「都是疑惑」不算贴切。
2. 在满足第 1 条的前提下选最贴切的那张。名次靠前通常更贴切，但不要只因为名次靠前就选。
3. 最近已经发过的图已经排除了，不用考虑重复。

只输出 JSON，不要解释：{"pick": <候选项序号>, "why": "<15 字以内理由>", "feel": "<情绪词>"}"""


# --------------------------------------------------------------------- utils
def build_embed_text(item: dict[str, Any]) -> str:
    """用来建向量的一句话。实测只用 use（适用场景）检索最准：
    加上 tags 和 desc 会把信号稀释（「芝士雪豹」会被召回到「嗦冰棍」）。"""
    use = (item.get("use") or "").strip()
    if use:
        return use
    parts = [item.get("name") or "", item.get("emotion") or ""]
    parts.extend(t for t in (item.get("tags") or []) if t)
    return " ".join(p for p in parts if p).strip()


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def _fingerprint(ids: list[str], texts: list[str]) -> str:
    h = hashlib.sha256()
    for sid, text in zip(ids, texts):
        h.update(sid.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    text = text[3:]
    if text[:4].lower() == "json":
        text = text[4:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def parse_judge(raw: str, candidate_count: int) -> tuple[int, str, str]:
    """从模型输出里抠出 (pick, why, feel)。pick 越界或解析不了时返回 -1。"""
    text = _strip_fence(raw or "")
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text, re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    data = None
    pick = -1
    why = ""
    feel = ""
    if isinstance(data, dict):
        try:
            pick = int(data.get("pick", -1))
        except (TypeError, ValueError):
            pick = -1
        why = str(data.get("why") or "").strip()[:60]
        feel = str(data.get("feel") or "").strip()[:20]
    if pick < 0 and text:
        match = re.search(r'"?pick"?\s*[:=]\s*"?(\d+)', text)
        if match:
            pick = int(match.group(1))
    if pick > candidate_count:
        pick = -1
    return pick, why, feel


# -------------------------------------------------------------------- picker
class SemanticPicker:
    """向量召回 + LLM 精选。生命周期由插件管理，向量常驻内存（316×1024 float32 ≈ 1.3MB）。"""

    def __init__(
        self,
        context: Any,
        *,
        embedding_provider_id: str = "",
        rerank_provider_id: str = "",
        cache_dir: str | Path = "",
        recall_k: int = DEFAULT_RECALL_K,
        min_sim: float = DEFAULT_MIN_SIM,
        rerank_timeout: float = 3.0,
        llm_enabled: bool = True,
        weight_fn: Any = None,
    ) -> None:
        self.context = context
        self.embedding_provider_id = (embedding_provider_id or "").strip()
        self.rerank_provider_id = (rerank_provider_id or "").strip()
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.recall_k = max(1, int(recall_k or DEFAULT_RECALL_K))
        self.min_sim = float(min_sim if min_sim is not None else DEFAULT_MIN_SIM)
        self.rerank_timeout = float(rerank_timeout or 3.0)
        self.llm_enabled = bool(llm_enabled)
        # 可选的逐条权重（比如给双人表情降权）：签名 item -> 0..1 的系数
        self.weight_fn = weight_fn

        self._vec_by_id: dict[str, np.ndarray] = {}
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._fingerprint = ""
        self._matrix: np.ndarray | None = None
        self._rows: list[dict[str, Any]] = []
        self._row_weights: np.ndarray | None = None
        self._matrix_key = ""
        self._building = False
        self._lock = asyncio.Lock()
        self._cache_meta: dict[str, Any] = {}
        self.last_error = ""
        self.built_at = 0.0

    # ---------------------------------------------------------------- state
    @property
    def ready(self) -> bool:
        return bool(self._ids) and self._matrix is not None and len(self._rows) > 0

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "vectors": len(self._ids),
            "covered": len(self._rows),
            "dim": int(self._cache_meta.get("dim") or 0),
            "model": self._cache_meta.get("model") or "",
            "embedding_provider": self.embedding_provider_id,
            "rerank_provider": self.rerank_provider_id,
            "llm": self.llm_enabled,
            "cache": str(self._cache_path()) if self.cache_dir else "",
            "built_at": self.built_at,
            "error": self.last_error,
        }

    # ------------------------------------------------------------- provider
    def providers_ready(self) -> bool:
        """embedding provider 是否已经能拿到（启动早期 provider 还没注册完）。"""
        return self._embed_provider() is not None

    def _embed_provider(self) -> Any:
        if self._is_auto(self.embedding_provider_id):
            self._resolve_auto_embedding()
            # 没解析出真实 id 就直接返回，免得下面把 auto 当成 provider id 去查
            # 从而把「没找到向量模型」的提示覆盖成「找不到 embedding provider：auto」
            if self._is_auto(self.embedding_provider_id):
                return None
        if not self.embedding_provider_id:
            return None
        prov = self.context.get_provider_by_id(self.embedding_provider_id)
        if prov is None:
            self.last_error = f"找不到 embedding provider：{self.embedding_provider_id}"
            return None
        if not (hasattr(prov, "get_embedding") or hasattr(prov, "get_embeddings")):
            self.last_error = f"{self.embedding_provider_id} 不是 embedding provider"
            return None
        return prov

    @staticmethod
    def _is_auto(value: str) -> bool:
        return (value or "").strip().lower() in {"", "auto", "自动", "default"}

    def _auto_embedding_ids(self) -> list[str]:
        """按 provider_manager 的实例表挑出所有 embedding 类型的 provider id。"""
        pm = getattr(self.context, "provider_manager", None)
        inst_map = getattr(pm, "inst_map", None)
        if not isinstance(inst_map, dict):
            return []
        found: list[str] = []
        for pid, inst in inst_map.items():
            if _provider_type_of(inst) == "embedding":
                found.append(pid)
        return found

    def _resolve_auto_embedding(self) -> None:
        """把 auto 解析成真实 id；解析成功后写回，日志与 stats 都能看到到底用了哪个。"""
        if self.embedding_provider_id and not self._is_auto(self.embedding_provider_id):
            return
        ids = self._auto_embedding_ids()
        if not ids:
            self.last_error = (
                "向量模型设为「自动」但没找到可用的 embedding provider，"
                "请先在 AstrBot 里添加一个向量模型，或在本项手动选一个"
            )
            return
        pid = ids[0]
        if pid != self.embedding_provider_id:
            self.embedding_provider_id = pid
            logger.info(f"[nekowhale] 向量模型自动选中：{pid}")
        self.last_error = ""


    def _chat_provider(self) -> Any:
        if not self.llm_enabled or not self.rerank_provider_id:
            return None
        prov = self.context.get_provider_by_id(self.rerank_provider_id)
        if prov is None:
            self.last_error = f"找不到 chat provider：{self.rerank_provider_id}"
            return None
        if not hasattr(prov, "text_chat"):
            self.last_error = f"{self.rerank_provider_id} 不支持 text_chat"
            return None
        return prov

    # ---------------------------------------------------------------- cache
    def _cache_path(self) -> Path:
        return Path(self.cache_dir) / "emb_cache.npz" if self.cache_dir else Path("")

    def _meta_path(self) -> Path:
        return Path(self.cache_dir) / "emb_cache.json" if self.cache_dir else Path("")

    def load_cache(self, items: list[dict[str, Any]]) -> bool:
        """读磁盘向量。模型/维度/条目指纹对不上就返回 False，交给 build() 重建。"""
        if not self.cache_dir:
            return False
        npz = self._cache_path()
        meta_path = self._meta_path()
        if not npz.is_file() or not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.last_error = f"向量缓存元信息损坏：{e}"
            return False
        if int(meta.get("version") or 0) != CACHE_VERSION:
            return False
        texts = [build_embed_text(it) for it in items]
        ids = [str(it["id"]) for it in items]
        if meta.get("fingerprint") != _fingerprint(ids, texts):
            return False
        try:
            data = np.load(npz, allow_pickle=False)
            vecs = np.asarray(data["vecs"], dtype=np.float32)
            cache_ids = [str(x) for x in data["ids"].tolist()]
        except Exception as e:
            self.last_error = f"向量缓存读取失败：{e}"
            return False
        if vecs.ndim != 2 or len(cache_ids) != vecs.shape[0]:
            return False
        vecs = _l2_normalize(vecs)
        self._ids = cache_ids
        self._texts = texts
        self._vec_by_id = {sid: vecs[i] for i, sid in enumerate(cache_ids)}
        self._cache_meta = meta
        self._fingerprint = str(meta.get("fingerprint") or "")
        self.built_at = float(meta.get("built_at") or 0.0)
        self._rebuild_matrix(items)
        return True

    def _tmp_cache_path(self) -> Path:
        return Path(self.cache_dir) / "emb_cache.tmp.npz"

    def _save_cache(self, ids: list[str], vecs: np.ndarray, texts: list[str]) -> None:
        if not self.cache_dir:
            return
        try:
            Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
            # 必须是 .npz 结尾：numpy 会给自己加后缀，别用 .npz.tmp 之类的临时名
            tmp = self._tmp_cache_path()
            np.savez_compressed(tmp, ids=np.array(ids), vecs=vecs.astype(np.float32))
            tmp.replace(self._cache_path())
            meta = {
                "version": CACHE_VERSION,
                "model": str(getattr(self._embed_provider(), "model", "") or ""),
                "dim": int(vecs.shape[1]),
                "count": len(ids),
                "fingerprint": _fingerprint(ids, texts),
                "built_at": time.time(),
            }
            self._meta_path().write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._cache_meta = meta
        except Exception as e:
            self.last_error = f"向量缓存写入失败：{e}"
            logger.warning(f"[nekowhale] 向量缓存写入失败：{e}")

    # ---------------------------------------------------------------- build
    async def build(self, items: list[dict[str, Any]], force: bool = False) -> bool:
        """给所有表情建向量。已就绪且指纹没变时直接返回（除非 force）。"""
        prov = self._embed_provider()
        if prov is None:
            return False
        ids = [str(it["id"]) for it in items]
        texts = [build_embed_text(it) for it in items]
        fingerprint = _fingerprint(ids, texts)
        if not force and self._ids and fingerprint == self._fingerprint:
            self._rebuild_matrix(items)
            return True
        async with self._lock:
            if self._building:
                return self.ready
            self._building = True
            started = time.time()
            try:
                vectors: list[list[float]] = []
                for start in range(0, len(texts), EMB_BATCH_SIZE):
                    chunk = texts[start : start + EMB_BATCH_SIZE]
                    chunk = [t or "空" for t in chunk]
                    if hasattr(prov, "get_embeddings"):
                        out = await prov.get_embeddings(chunk)
                    else:
                        out = [await prov.get_embedding(t) for t in chunk]
                    vectors.extend(out)
                if len(vectors) != len(texts):
                    raise RuntimeError(
                        f"向量数量不符：{len(vectors)} != {len(texts)}"
                    )
                matrix = _l2_normalize(np.asarray(vectors, dtype=np.float32))
                self._ids = ids
                self._texts = texts
                self._vec_by_id = {sid: matrix[i] for i, sid in enumerate(ids)}
                self._fingerprint = fingerprint
                self.built_at = time.time()
                self.last_error = ""
                self._save_cache(ids, matrix, texts)
                self._rebuild_matrix(items)
                logger.info(
                    f"[nekowhale] 表情向量建库完成 {len(ids)} 张 / "
                    f"dim={matrix.shape[1]} / {time.time() - started:.1f}s"
                )
                return True
            except Exception as e:
                self.last_error = f"建向量失败：{e}"
                logger.error(f"[nekowhale] 建向量失败：{e}")
                return False
            finally:
                self._building = False

    def _rebuild_matrix(self, items: list[dict[str, Any]]) -> None:
        key = "|".join(str(it["id"]) for it in items)
        if key == self._matrix_key and self._matrix is not None:
            return
        rows: list[dict[str, Any]] = []
        vecs: list[np.ndarray] = []
        for it in items:
            vec = self._vec_by_id.get(str(it["id"]))
            if vec is None:
                continue
            rows.append(it)
            vecs.append(vec)
        self._rows = rows
        self._matrix = np.vstack(vecs) if vecs else None
        self._row_weights = None
        if rows and self.weight_fn is not None:
            try:
                weights = [
                    float(self.weight_fn(it))
                    for it in rows
                ]
                self._row_weights = np.asarray(weights, dtype=np.float32)
            except Exception:
                self._row_weights = None
                logger.warning("[nekowhale] 计算逐条权重失败，按原始相似度排序")
        self._matrix_key = key

    # --------------------------------------------------------------- recall
    async def _embed_query(self, text: str) -> np.ndarray:
        prov = self._embed_provider()
        if prov is None:
            raise RuntimeError(self.last_error or "embedding provider 不可用")
        vec = await prov.get_embedding(text)
        arr = np.asarray([vec], dtype=np.float32)
        return _l2_normalize(arr)[0]

    def _recall(
        self,
        query_vec: np.ndarray,
        k: int,
        exclude: set[str],
    ) -> list[tuple[float, dict[str, Any]]]:
        if self._matrix is None:
            return []
        sims = self._matrix @ query_vec
        if self._row_weights is not None:
            sims = sims * self._row_weights
        order = np.argsort(-sims)
        picked: list[tuple[float, dict[str, Any]]] = []
        for idx in order:
            item = self._rows[int(idx)]
            if str(item["id"]) in exclude:
                continue
            picked.append((float(sims[int(idx)]), item))
            if len(picked) >= k:
                break
        return picked

    # ---------------------------------------------------------------- judge
    @staticmethod
    def _candidate_block(cands: list[tuple[float, dict[str, Any]]]) -> str:
        lines = []
        for rank, (_sim, item) in enumerate(cands, 1):
            name = item.get("name") or item.get("id")
            emotion = item.get("emotion") or ""
            use = item.get("use") or ""
            tags = "、".join(t for t in (item.get("tags") or [])[:4] if t)
            head = f"{rank}. {name}"
            if emotion:
                head += f"〔{emotion}〕"
            detail = "｜".join(x for x in (use, tags) if x)
            lines.append(f"{head} —— {detail}" if detail else head)
        return "\n".join(lines)

    async def _judge(
        self,
        user_text: str,
        reply_text: str,
        cands: list[tuple[float, dict[str, Any]]],
    ) -> tuple[int, str, str, float]:
        prov = self._chat_provider()
        if prov is None:
            return -1, "", "", 0.0
        user_block = (user_text or "").strip()[:MAX_USER_CHARS] or "（只发了张图或表情，没说话）"
        reply_block = (reply_text or "").strip()[:MAX_REPLY_CHARS] or "（没说话）"
        prompt = (
            f"用户：{user_block}\n"
            f"我（bot）：{reply_block}\n\n"
            f"候选：\n{self._candidate_block(cands)}"
        )
        started = time.time()
        try:
            response = await asyncio.wait_for(
                prov.text_chat(
                    prompt=prompt,
                    contexts=[],
                    system_prompt=SYSTEM_PROMPT,
                ),
                timeout=self.rerank_timeout,
            )
        except asyncio.TimeoutError:
            self.last_error = "精选超时"
            return -1, "", "", time.time() - started
        except Exception as e:
            self.last_error = f"精选失败：{e}"
            logger.warning(f"[nekowhale] 精选失败：{e}")
            return -1, "", "", time.time() - started
        text = getattr(response, "completion_text", "") or ""
        pick, why, feel = parse_judge(text, len(cands))
        return pick, why, feel, time.time() - started

    # ----------------------------------------------------------------- pick
    async def pick(
        self,
        items: list[dict[str, Any]],
        user_text: str,
        reply_text: str,
        recent: list[str] | None = None,
        recall_k: int | None = None,
        min_sim: float | None = None,
        use_llm: bool | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        info: dict[str, Any] = {
            "stage": "",
            "sim": 0.0,
            "candidates": 0,
            "why": "",
            "feel": "",
            "llm_ms": 0,
            "total_ms": 0,
            "pick": "",
        }
        started = time.time()
        if not self.ready:
            self._rebuild_matrix(items)
        if self._matrix is None:
            info["stage"] = "no-vectors"
            return None, info

        query = f"{(user_text or '').strip()[:MAX_USER_CHARS]}\n{(reply_text or '').strip()[:MAX_REPLY_CHARS]}".strip()
        if not query:
            info["stage"] = "empty-query"
            return None, info

        try:
            query_vec = await self._embed_query(query)
        except Exception as e:
            self.last_error = f"查询向量失败：{e}"
            info["stage"] = "embed-error"
            logger.warning(f"[nekowhale] 查询向量失败：{e}")
            return None, info

        k = int(recall_k or self.recall_k)
        threshold = float(self.min_sim if min_sim is None else min_sim)
        cands = self._recall(query_vec, k, set(recent or []))
        info["candidates"] = len(cands)
        if not cands:
            info["stage"] = "no-candidates"
            return None, info
        info["sim"] = round(cands[0][0], 3)

        # 相似度低也照样往下走：宁可让模型在候选里挑一张最不违和的，
        # 也不要因为「不够像」就整条不配图。
        if cands[0][0] < threshold:
            info["low_sim"] = True

        want_llm = self.llm_enabled if use_llm is None else bool(use_llm)
        if not want_llm:
            info["stage"] = "recall-top1"
            info["total_ms"] = int((time.time() - started) * 1000)
            return cands[0][1], info

        pick, why, feel, llm_seconds = await self._judge(user_text, reply_text, cands)
        info["llm_ms"] = int(llm_seconds * 1000)
        info["why"] = why
        info["feel"] = feel
        info["total_ms"] = int((time.time() - started) * 1000)

        if 1 <= pick <= len(cands):
            chosen = cands[pick - 1][1]
            info["stage"] = "llm"
            info["pick"] = str(chosen.get("id"))
            return chosen, info

        if pick == 0:
            # 不再有「判定为沉重话题就不发」的分支：模型偶尔还是会返回 0，
            # 这种情况下退到向量 top1，保证每条回复都有图（正常配图）。
            chosen = cands[0][1]
            info["stage"] = "llm-zero-top1"
            info["pick"] = str(chosen.get("id"))
            return chosen, info

        # 解析失败 / 超时 / 模型没给序号：退到向量 top1，保证每条回复都有图
        chosen = cands[0][1]
        info["stage"] = "llm-fallback-top1"
        info["pick"] = str(chosen.get("id"))
        return chosen, info
