# -*- coding: utf-8 -*-
"""大肥鱼表情包（nekowhale）——自动配图 + 语义选图 + 贴字表情。

和 2.1 的区别：

1. **插件里不含素材**。322 张表情共 388MB，塞不进插件包（市场限 16MB）。
   清单（172KB）和标注（160KB）在首次启动时从官方站拉，单张表情用到哪张下哪张，
   下完留在本地当缓存。版权归作者，插件只做「按需取用」，不做镜像。
2. **把 MCP 的四个工具做进了插件**：搜图 / 发表情 / 查详情 / 贴字表情。
   以前这套能力要靠 `dsh-nekowhale-stickers` 的 MCP server（要求装 Node 20），
   现在纯 Python，装了插件就有。
3. 贴字表情（think / sign / scroll / couplet / hug）是上游 Node 实现在 Python 里的复刻，
   逐像素对过，肉眼无差。
"""

from __future__ import annotations

import asyncio
import json
import platform
import random
import sys
import tempfile
import time
from collections import defaultdict, deque
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Record, Reply, Video
from astrbot.core.message.message_event_result import MessageChain

from .credit import CREDIT_BRIEF, CREDIT_FULL, LICENSE_BRIEF, LOAD_BANNER
from .fetcher import FetchError, KitFetcher
from .render import TEMPLATE_LABELS, TEMPLATES, MemeRenderer, RenderError
from .semantic import DEFAULT_MIN_SIM, DEFAULT_RECALL_K, SemanticPicker
from .sticker_index import StickerIndex, norm_packs

PLUGIN_NAME = "astrbot_plugin_nekowhale_stickers"
PLUGIN_VERSION = "2.3.2"
SITE = "https://kiriyuki.com"

# 模型自己把表情发出去时会用到的工具名（含 MCP 那边的名字，避免重复发）
STICKER_TOOLS = {
    "sticker_get",
    "sticker_send",
    "meme_render",
    "search_memes",
    "nekowhale_sticker_send",
    "nekowhale_meme_render",
}

EXTRA_SENT = "_nekowhale_already_sent"
EXTRA_PICKED = "_nekowhale_picked"
EXTRA_CTX = "_nekowhale_ctx"

# 配置面板里的下拉项用中文（AstrBot 的 options 只能是纯字符串，没有 label/value 之分），
# 所以这里做一层「中文值 → 内部值」的映射；旧的英文值继续兼容。
SELECTOR_ALIASES = {
    "向量选图": "semantic",
    "语义选图": "semantic",
    "semantic": "semantic",
    "关键词选图": "keyword",
    "关键词": "keyword",
    "keyword": "keyword",
    "自动": "auto",
    "auto": "auto",
}
SELECTOR_LABELS = {"semantic": "向量选图", "keyword": "关键词选图", "auto": "自动"}
MODE_LABELS = {"after_send": "正文后补一张", "inline": "图文一起发"}
MODE_ALIASES = {
    "正文后补一张": "after_send",
    "先发正文再补图": "after_send",
    "after_send": "after_send",
    "图文一起发": "inline",
    "随正文一起": "inline",
    "inline": "inline",
}

TOOL_TEXTS_KEY = "_send_message_to_user_current_session_plain_texts"
EXTRA_TOOL_TEXTS = "_nekowhale_tool_texts"

_TRIM_CHARS = set(" \t\r\n，。！？～~、,.!?…—－-:：；;\"'“”‘’()（）[]【】<>《》*+`　")


def _norm_text(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch not in _TRIM_CHARS)


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


@register(
    PLUGIN_NAME,
    "in1t1a1",
    "大肥鱼表情包：每条回复自动配张图，素材按需从官方站下载；附语义搜图与贴字表情",
    PLUGIN_VERSION,
)
class NekoWhaleStickers(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        self._data_dir = self._resolve_data_dir()
        self._kit = KitFetcher(
            self._data_dir,
            site=str(self._cfg("site", SITE) or SITE),
            timeout=float(self._cfg("download_timeout", 40) or 40),
            concurrency=int(self._cfg("download_concurrency", 3) or 3),
        )
        self._renderer: MemeRenderer | None = None
        if bool(self._cfg("meme_enabled", True)):
            self._renderer = MemeRenderer(
                self._kit,
                cache_dir=self._data_dir,
            )

        self._index = self._make_index({}, {})
        self._loaded = False
        self._load_error = ""
        self._load_sync()

        self._recent: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=int(self._cfg("recent_window", 12) or 12))
        )
        # 最小间隔按「回复条数」倒数：发过一张后，同一会话接下来 N 条回复不配图。
        # 默认 0 = 每条都配。老版本默认 1，等于每两条回复必跳过一条。
        self._cooldown: dict[str, int] = defaultdict(int)
        self._cooldown_remain = 0
        self._skip_reason = "无"
        self._build_attempted = False

        self._picker = SemanticPicker(
            context,
            embedding_provider_id=self._cfg("embedding_provider_id", ""),
            rerank_provider_id=self._cfg("rerank_provider_id", ""),
            cache_dir=self._vector_cache_dir(),
            recall_k=int(self._cfg("recall_k", DEFAULT_RECALL_K) or DEFAULT_RECALL_K),
            min_sim=float(self._cfg("min_sim", DEFAULT_MIN_SIM)),
            rerank_timeout=float(self._cfg("rerank_timeout", 6.0) or 6.0),
            llm_enabled=bool(self._cfg("llm_rerank", True)),
            weight_fn=self._item_weight,
        )
        self._vector_task: asyncio.Task | None = None
        self._warm_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._ready_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- config
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _mode(self) -> str:
        raw = str(self._cfg("mode", "after_send") or "after_send").strip()
        return MODE_ALIASES.get(raw, MODE_ALIASES.get(raw.lower(), "after_send"))

    def _selector(self) -> str:
        raw = str(self._cfg("selector", "自动") or "自动").strip()
        return SELECTOR_ALIASES.get(raw, SELECTOR_ALIASES.get(raw.lower(), "auto"))

    def _resolve_data_dir(self) -> Path:
        """素材目录：显式配置 > 插件数据目录 > 老位置（已经在用的机器不重下 388MB）。"""
        custom = str(self._cfg("data_dir", "") or "").strip()
        if custom:
            return Path(custom).expanduser()
        plugin_dir: Path | None = None
        try:
            plugin_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "nekowhale-stickers"
        except Exception as e:
            logger.warning(f"[nekowhale] 拿不到插件数据目录：{e}")
        legacy = Path(tempfile.gettempdir()) / ".astrbot" / "nekowhale-stickers"
        for candidate in (plugin_dir, legacy):
            if candidate and (candidate / "manifest.json").is_file():
                return candidate
        return plugin_dir or legacy

    def _vector_cache_dir(self) -> str:
        custom = str(self._cfg("cache_dir", "") or "").strip()
        if custom:
            return custom
        try:
            return str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as e:
            logger.warning(f"[nekowhale] 向量缓存改用系统临时目录：{e}")
            return str(Path(tempfile.gettempdir()) / ".astrbot" / "nekowhale_cache")

    def _max_bytes(self) -> int:
        """配置里用 MB 表示（max_size_mb），内部一律换成字节。"""
        try:
            mb = float(self._cfg("max_size_mb", 3.0) or 0)
        except (TypeError, ValueError):
            mb = 3.0
        return max(0, int(round(mb * 1024 * 1024)))

    def _dual_weight(self) -> float:
        """双人表情（有 dir 字段的那些）的权重系数，1.0 = 不降权。"""
        try:
            weight = float(self._cfg("dual_weight", 0.85))
        except (TypeError, ValueError):
            weight = 0.85
        return min(1.0, max(0.05, weight))

    def _item_weight(self, item: dict[str, Any]) -> float:
        """语义检索时给单条表情乘的系数（双人表情按配置降权）。"""
        return self._dual_weight() if item.get("dir") else 1.0

    def _make_index(self, manifest: dict[str, Any], meta: dict[str, Any]) -> StickerIndex:
        return StickerIndex(
            data_dir=self._data_dir,
            manifest=manifest,
            meta=meta,
            max_bytes=self._max_bytes(),
            exclude_emotions=self._cfg("exclude_emotions", []),
            fallback_emotions=self._cfg("fallback_emotions", None),
            dual_weight=self._dual_weight(),
            enabled_packs=norm_packs(self._cfg("enabled_packs", ["大肥鱼"])),
        )

    # ------------------------------------------------------------------ load
    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_sync(self) -> None:
        """离线快路径：磁盘上已经有清单就直接建索引，不等网络。"""
        manifest = self._read_json(self._data_dir / "manifest.json")
        if not manifest or not manifest.get("items"):
            self._load_error = "还没有表情清单（首次启动会自动下载）"
            return
        meta = self._read_json(self._data_dir / "meta" / "nekowhale.json") or {}
        self._index = self._make_index(manifest, meta)
        try:
            count = self._index.load()
        except Exception as e:
            self._loaded = False
            self._load_error = str(e)
            logger.error(f"[nekowhale] 索引载入失败：{e}")
            return
        self._loaded = True
        self._load_error = ""
        stats = self._index.stats()
        logger.info(
            f"[nekowhale] 索引载入 {count} 张（本地已有 {stats['present']} 张 / "
            f"{stats['size_mb']}MB，标注{'已载入' if stats['meta_found'] else '缺失'}，"
            f"包={','.join(stats['packs']) or '全部'}，目录={stats['data_dir']}）"
        )
        logger.info(
            f"[nekowhale] 运行环境：{platform.system()} {platform.release()} / Python "
            f"{sys.version_info.major}.{sys.version_info.minor}，插件 {PLUGIN_VERSION}"
        )
        self._warm_vectors()

    async def _lock(self) -> asyncio.Lock:
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    async def refresh(self, force: bool = False, rebuild_vectors: bool = False) -> bool:
        """拉清单 + 标注 → 重建索引。失败时保留旧索引。"""
        lock = await self._lock()
        async with lock:
            try:
                manifest = await self._kit.manifest(force=force)
            except FetchError as e:
                self._load_error = str(e)
                logger.error(f"[nekowhale] {e}")
                return False
            try:
                meta = await self._kit.meta(force=force)
            except Exception as e:
                logger.warning(f"[nekowhale] 标注取不到，选图退化为按文件名匹配：{e}")
                meta = {}
            old_ids = [i["id"] for i in self._index.items]
            self._index = self._make_index(manifest, meta)
            try:
                count = self._index.load()
            except Exception as e:
                self._load_error = f"索引重建失败：{e}"
                logger.error(f"[nekowhale] {self._load_error}")
                return False
            self._loaded = True
            self._load_error = ""
            self._skip_reason = "无"
            new_ids = [i["id"] for i in self._index.items]
            if rebuild_vectors or old_ids != new_ids:
                self._picker.drop_cache()
                self._spawn_build()
            logger.info(
                f"[nekowhale] 索引就绪 {count} 张（本地 {self._index.stats()['present']} 张，"
                f"清单版本 {manifest.get('version')}）"
            )
            return True

    def _warm_vectors(self) -> None:
        if not self._loaded:
            return
        try:
            if self._picker.load_cache(self._index.items):
                logger.info(
                    f"[nekowhale] 向量缓存命中 {self._picker.stats()['vectors']} 张"
                )
        except Exception as e:
            logger.debug(f"[nekowhale] 向量缓存读取失败：{e}")

    def _spawn_build(self, force: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._vector_task and not self._vector_task.done():
            return
        self._vector_task = loop.create_task(self._build_vectors(force))

    async def initialize(self) -> None:
        """插件加载完成、事件循环已在跑：补清单 → 建向量 → 预热几张图。"""
        try:
            loop = asyncio.get_running_loop()
            self._ready_task = loop.create_task(self._startup())
        except RuntimeError:
            pass

    async def _startup(self) -> None:
        logger.info(LOAD_BANNER)
        if not self._loaded:
            await self.refresh(force=False)
        else:
            try:
                await self.refresh(force=False)
            except Exception as e:
                logger.warning(f"[nekowhale] 清单刷新失败（继续用本地）：{e}")
        if not self._loaded:
            return
        if self._selector() != "keyword" and not self._picker.ready:
            await self._warm_when_ready()

    async def _warm_when_ready(self) -> None:
        for _ in range(30):
            if self._picker.ready:
                return
            if self._picker.providers_ready():
                break
            await asyncio.sleep(2)
        await self._build_vectors()

    async def _build_vectors(self, force: bool = False) -> bool:
        try:
            ok = bool(await self._picker.build(self._index.items, force=force))
        except Exception:
            logger.exception("[nekowhale] 后台建向量异常")
            return False
        if not ok:
            logger.warning(f"[nekowhale] 建向量失败：{self._picker.last_error}")
        return ok

    async def prefetch(self, count: int = 6) -> int:
        """先把几张常用的下下来，免得第一条回复要等下载。"""
        if not self._loaded or count <= 0:
            return 0
        pool = [i for i in self._index.items if not i.get("present")]
        if not pool:
            return 0
        random.shuffle(pool)
        done = 0
        for item in pool[:count]:
            try:
                await self._kit.ensure_sticker(item)
                done += 1
            except Exception as e:
                logger.debug(f"[nekowhale] 预热 {item.get('id')} 失败：{e}")
                break
        if done:
            logger.info(f"[nekowhale] 已预热 {done} 张表情")
        return done

    # ------------------------------------------------------------- detection
    @filter.on_using_llm_tool()
    async def _watch_tools(
        self,
        event: AstrMessageEvent,
        tool: Any = None,
        tool_args: dict | None = None,
    ):
        """模型本轮已经自己发表了情时打标记，避免重复发。"""
        name = str(getattr(tool, "name", "") or "")
        if name in STICKER_TOOLS:
            event.set_extra(EXTRA_SENT, True)
            return
        if name == "send_message_to_user":
            texts: list[str] = []
            has_media = False
            for msg in (tool_args or {}).get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                kind = str(msg.get("type", "")).lower()
                if kind in {"image", "record", "video", "file"}:
                    event.set_extra(EXTRA_SENT, True)
                    has_media = True
                elif kind == "plain":
                    text = str(msg.get("text") or "").strip()
                    if text:
                        texts.append(text)
            # 只在「这次调用带媒体」时才记这句话：纯文本调用可能被别的插件拦下，
            # 若照样记账，_drop_dup_reply 会把正经正文当重复内容吞掉 → 机器人完全沉默。
            if texts and has_media:
                merged = event.get_extra(EXTRA_TOOL_TEXTS) or []
                if not isinstance(merged, list):
                    merged = []
                event.set_extra(EXTRA_TOOL_TEXTS, merged + texts)

    # --------------------------------------------------- pick & send helpers
    def _should_skip(self, event: AstrMessageEvent) -> bool:
        if not self._index.items:
            self._skip_reason = "表情库为空"
            return True
        if not self._cfg("enabled", True):
            self._skip_reason = "已关闭"
            return True
        if not self._loaded:
            self._skip_reason = f"索引未载入：{self._load_error}"
            return True
        if event.get_extra(EXTRA_SENT) or event.get_extra(EXTRA_PICKED):
            self._skip_reason = "本轮已发过图"
            return True
        if event.is_stopped():
            self._skip_reason = "事件已终止"
            return True
        umo = event.unified_msg_origin
        skip_umos = self._cfg("skip_umos", []) or []
        if umo in skip_umos:
            self._skip_reason = "会话在 skip_umos 里"
            return True
        return False

    def _roll(self) -> bool:
        try:
            probability = float(self._cfg("probability", 1.0))
        except (TypeError, ValueError):
            probability = 1.0
        if probability >= 1.0:
            return True
        if probability <= 0:
            return False
        return random.random() <= probability

    def _cooldown_count(self) -> int:
        """最小间隔（回复条数）。<=0 表示不限制。"""
        try:
            value = int(self._cfg("cooldown", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, value)

    def _cooldown_blocked(self, umo: str) -> bool:
        count = self._cooldown_count()
        self._cooldown_remain = 0
        if count <= 0:
            return False
        remain = self._cooldown.get(umo, 0)
        if remain <= 0:
            return False
        self._cooldown[umo] = remain - 1
        self._cooldown_remain = remain - 1
        return True

    async def _choose(
        self, umo: str, user_text: str, reply_text: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """语义优先，退化为关键词。返回 (表情, 决策信息)。"""
        selector = self._selector()
        fallback = bool(self._cfg("fallback_to_keyword", True))
        if selector == "auto":
            # 自动：向量库已就绪就走向量；没就绪就立刻用关键词，
            # 不为了等建库把首条回复卡住（建库在后台继续跑）。
            if self._picker.ready:
                selector = "semantic"
            elif fallback:
                selector = "keyword"
            else:
                selector = "semantic"
        if selector in {"semantic", "auto"} and not self._picker.ready:
            task = self._vector_task
            if task and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=30)
                except Exception as e:
                    logger.warning(f"[nekowhale] 等待建向量没等到：{e}")
            elif not self._build_attempted:
                self._build_attempted = await self._build_vectors()
        info: dict[str, Any] = {}
        if selector in {"semantic", "auto"} and self._picker.ready:
            item, info = await self._picker.pick(
                self._index.items,
                user_text=user_text,
                reply_text=reply_text,
                recent=list(self._recent[umo]),
            )
            if item is not None:
                return item, info
            self._skip_reason = f"语义未选中（{info.get('stage')}, sim={info.get('sim')}）"
            if not fallback:
                return None, info
            # 语义这一层没给出结果时不要直接放弃，关键词还能兜一层，
            # 否则 embedding provider 抖一下就会「这一条不配图」。
            info = dict(info, stage=f"{info.get('stage')}→关键词兜底")
        if selector == "keyword" or fallback:
            item = self._index.pick(
                reply_text=reply_text,
                user_text=user_text,
                recent=list(self._recent[umo]),
                top_n=int(self._cfg("top_n", 6) or 6),
            )
            if item is None:
                self._skip_reason = "关键词也没挑到"
                return None, {"stage": "keyword-empty", "sim": 0.0}
            if not info:
                info = {"stage": "keyword", "sim": 0.0}
            return item, info
        self._skip_reason = "语义向量未就绪且未开启关键词兜底"
        return None, {"stage": "semantic-not-ready"}

    async def _deliver(self, event: AstrMessageEvent, item: dict[str, Any]) -> bool:
        try:
            path = await self._kit.ensure_sticker(item)
        except Exception as e:
            logger.warning(f"[nekowhale] 表情下载失败 {item.get('id')}：{e}")
            self._skip_reason = f"下载失败：{e}"
            return False
        chain = MessageChain([Image.fromFileSystem(str(path))])
        umo = event.unified_msg_origin
        errors: list[str] = []
        try:
            await event.send(chain)
            return True
        except Exception as e:
            errors.append(f"event.send: {e}")
        try:
            ok = await self.context.send_message(umo, chain)
            if ok:
                return True
            errors.append("context.send_message: 未找到匹配的平台")
        except Exception as e:
            errors.append(f"context.send_message: {e}")
        logger.warning(f"[nekowhale] 表情发送失败 {'; '.join(errors)}")
        return False

    # ------------------------------------------------------------- decorate
    def _drop_dup_reply(self, event: AstrMessageEvent) -> bool:
        """模型已经用 send_message_to_user 说过同一句话时，丢掉重复的正文。"""
        if not self._cfg("drop_dup_reply", True):
            return False
        from_framework: list[str] = []
        val = event.get_extra(TOOL_TEXTS_KEY)
        if isinstance(val, list):
            from_framework = [str(v) for v in val if str(v).strip()]
        media_sent = bool(event.get_extra(EXTRA_SENT))
        if not from_framework and not media_sent:
            return False
        sent: list[str] = list(from_framework)
        val = event.get_extra(EXTRA_TOOL_TEXTS)
        if isinstance(val, list):
            sent.extend(str(v) for v in val if str(v).strip())
        if not sent:
            return False
        result = event.get_result()
        if result is None or not result.chain or not result.is_llm_result():
            return False
        if not all(isinstance(comp, (Plain, Reply, At)) for comp in result.chain):
            return False
        text = "".join(comp.text for comp in result.chain if isinstance(comp, Plain)).strip()
        norm = _norm_text(text)
        if not norm:
            return False
        hit = ""
        for raw in sent:
            other = _norm_text(str(raw))
            if not other:
                continue
            if norm == other or norm in other or other in norm:
                hit = raw
                break
            if SequenceMatcher(None, norm, other).ratio() >= 0.7:
                hit = raw
                break
        if not hit:
            return False
        logger.info(
            f"[nekowhale] {event.unified_msg_origin} 正文与工具已发内容重复，跳过正文："
            f"{text[:40]!r} ≈ {str(hit)[:40]!r}"
        )
        result.chain.clear()
        return True

    @filter.on_decorating_result()
    async def _decorate(self, event: AstrMessageEvent):
        try:
            if self._drop_dup_reply(event):
                return
            ctx = self._snapshot(event)
            if ctx is None:
                return
            if self._mode() == "inline":
                await self._inline(event, ctx)
        except Exception:
            logger.exception("[nekowhale] 装饰失败")

    def _snapshot(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        result = event.get_result()
        if result is None or not result.chain:
            return None
        if not result.is_llm_result():
            return None
        for comp in result.chain:
            if isinstance(comp, (Image, Record, Video)):
                return None
        text = "".join(comp.text for comp in result.chain if isinstance(comp, Plain)).strip()
        if not text:
            return None
        try:
            user_text = event.get_message_str() or ""
        except Exception:
            user_text = ""
        ctx = {"user_text": user_text, "reply_text": text}
        event.set_extra(EXTRA_CTX, ctx)
        return ctx

    async def _inline(self, event: AstrMessageEvent, ctx: dict[str, Any]) -> None:
        self._skip_reason = "无"
        if self._should_skip(event):
            logger.debug(f"[nekowhale] 跳过（{self._skip_reason}）")
            return
        if not self._roll():
            logger.debug("[nekowhale] 跳过（概率没中）")
            return
        umo = event.unified_msg_origin
        if self._cooldown_blocked(umo):
            logger.info(f"[nekowhale] {umo} 跳过（最小间隔，还剩 {self._cooldown_remain} 条回复不配图）")
            return
        result = event.get_result()
        if result is None:
            return
        item, info = await self._choose(umo, ctx["user_text"], ctx["reply_text"])
        if item is None:
            logger.info(f"[nekowhale] {umo} 跳过（{info.get('stage')}）")
            return
        try:
            path = await self._kit.ensure_sticker(item)
        except Exception as e:
            logger.warning(f"[nekowhale] 表情下载失败 {item.get('id')}：{e}")
            return
        result.chain.append(Image.fromFileSystem(str(path)))
        self._recent[umo].append(str(item["id"]))
        self._cooldown[umo] = self._cooldown_count()
        event.set_extra(EXTRA_PICKED, True)
        self._log_pick(umo, item, info)

    # ---------------------------------------------------------- after send
    @filter.after_message_sent()
    async def _after_sent(self, event: AstrMessageEvent):
        try:
            await self._after_sent_impl(event)
        except Exception:
            logger.exception("[nekowhale] 发送后补图失败")

    async def _after_sent_impl(self, event: AstrMessageEvent) -> None:
        ctx = event.get_extra(EXTRA_CTX)
        event.set_extra(EXTRA_CTX, None)
        if not ctx:
            # 命令回执、纯图片回复、正文被去重清空都会走到这里，
            # 默认级别不打日志，排查时把日志调到 DEBUG 就能看到。
            logger.debug("[nekowhale] 跳过（这一轮没有可配图的回复正文）")
            return
        if self._mode() != "after_send":
            return
        self._skip_reason = "无"
        if self._should_skip(event):
            logger.debug(f"[nekowhale] 跳过（{self._skip_reason}）")
            return
        if not self._roll():
            logger.debug("[nekowhale] 跳过（概率没中）")
            return
        umo = event.unified_msg_origin
        if self._cooldown_blocked(umo):
            logger.info(f"[nekowhale] {umo} 跳过（最小间隔，还剩 {self._cooldown_remain} 条回复不配图）")
            return
        started = time.time()
        item, info = await self._choose(umo, ctx["user_text"], ctx["reply_text"])
        if item is None:
            logger.info(
                f"[nekowhale] {umo} 跳过（{info.get('stage')}, "
                f"sim={info.get('sim')}, {int((time.time() - started) * 1000)}ms）"
            )
            return
        if not await self._deliver(event, item):
            return
        self._recent[umo].append(str(item["id"]))
        self._cooldown[umo] = self._cooldown_count()
        event.set_extra(EXTRA_PICKED, True)
        self._log_pick(umo, item, info)

    def _log_pick(self, umo: str, item: dict[str, Any], info: dict[str, Any]) -> None:
        why = info.get("why") or ""
        logger.info(
            f"[nekowhale] {umo} -> {item['id']} "
            f"({item.get('emotion') or '无情绪'} / {item['bytes'] // 1024}KB / "
            f"sim={info.get('sim')} / {info.get('stage')} / "
            f"{(info.get('total_ms') or 0)}ms)"
            + (f" 「{why}」" if why else "")
        )

    # --------------------------------------------------------------- commands
    @filter.command("表情自检")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _status(self, event: AstrMessageEvent):
        stats = self._index.stats()
        sem = self._picker.stats()
        kit = self._kit.stats()
        top = "、".join(f"{k}{v}" for k, v in list(stats["emotions"].items())[:8])
        lines = [
            f"表情库：{stats['total']} 张 / {stats['size_mb']}MB "
            f"（本地已有 {stats['present']}，缺 {stats['missing']}）",
            f"素材目录：{kit['dir']}",
            f"标注：{'已载入' if stats['meta_found'] else '缺失'}｜"
            f"启用的包：{'、'.join(stats['packs']) or '全部'}（清单共 {stats['known']} 张）",
            f"选图：{SELECTOR_LABELS.get(self._selector(), self._selector())} / 模式 {MODE_LABELS.get(self._mode(), self._mode())} / 概率 {self._cfg('probability', 1.0)}",
            f"体积上限：{self._max_bytes() // 1048576}MB｜双人表情权重：{self._dual_weight():.2f}",
            f"向量：{'就绪' if sem['ready'] else '未就绪'} "
            f"{sem['vectors']} 张 dim={sem['dim']}（{sem['model'] or '未建库'}）",
            f"精选模型：{sem['rerank_provider'] or '关闭'}（{'开' if sem['llm'] else '关'}）",
            f"贴字表情：{'开' if self._renderer else '关'}"
            f"（模板 {'/'.join(TEMPLATE_LABELS.values())}）",
            f"下载站：{kit['site']}｜本次运行已下 {kit['downloaded']} 张 {kit['downloaded_mb']}MB",
            f"最近跳过：{self._skip_reason}",
            f"情绪分布：{top}",
        ]
        if kit["last_error"]:
            lines.insert(3, f"最近错误：{kit['last_error']}")
        if sem["error"]:
            lines.insert(3, f"向量错误：{sem['error']}")
        lines.append(LICENSE_BRIEF)
        yield event.plain_result("\n".join(lines))

    @filter.command("表情测试")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _dry_run(self, event: AstrMessageEvent):
        raw = event.message_str or ""
        raw = raw.partition(" ")[2].strip() if " " in raw else ""
        if not raw:
            yield event.plain_result(
                "用法：/表情测试 用户说了什么 | bot回了什么\n只发一次判断，不会真的把图发出去。"
            )
            return
        user_text, _, reply_text = raw.partition("|")
        started = time.time()
        item, info = await self._choose(
            event.unified_msg_origin, user_text.strip(), reply_text.strip()
        )
        cost = int((time.time() - started) * 1000)
        if item is None:
            yield event.plain_result(
                f"不发（{info.get('stage')}，最高相似度 {info.get('sim')}，{cost}ms）"
            )
            return
        local = "已在本地" if item.get("present") else "需要现下"
        yield event.plain_result(
            f"选中：{item['name'] or item['id']}〔{item.get('emotion') or '无'}〕（{local}）\n"
            f"阶段：{info.get('stage')} / 相似度 {info.get('sim')} / {cost}ms\n"
            f"理由：{info.get('why') or '（关键词模式或模型没给）'}"
        )

    @filter.command("表情下载")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _prefetch_cmd(self, event: AstrMessageEvent):
        raw = event.message_str or ""
        arg = raw.partition(" ")[2].strip() if " " in raw else ""
        if arg in {"全部", "all"}:
            total = [i for i in self._index.items if not i.get("present")]
            size = sum(i["bytes"] for i in total)
            yield event.plain_result(
                f"开始下载全部 {len(total)} 张（约 {_human(size)}），可能要几分钟…\n"
                f"这会把大肥鱼全套存到本地，占磁盘 {_human(size)}。"
            )
            ok, fail = 0, 0
            for item in total:
                try:
                    await self._kit.ensure_sticker(item)
                    ok += 1
                except Exception:
                    fail += 1
                if (ok + fail) % 20 == 0:
                    await asyncio.sleep(0)
            yield event.plain_result(f"下载完成：成功 {ok} 张，失败 {fail} 张")
            return
        count = 6
        if arg.isdigit():
            count = max(1, min(200, int(arg)))
        done = await self.prefetch(count)
        yield event.plain_result(f"预热完成：新下载 {done} 张（失败会记日志）")

    @filter.command("表情重载")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _reload(self, event: AstrMessageEvent):
        yield event.plain_result("正在向官方站对清单…")
        ok = await self.refresh(force=True, rebuild_vectors=False)
        if ok:
            stats = self._index.stats()
            yield event.plain_result(
                f"表情库已重载：{stats['total']} 张（本地 {stats['present']}）"
                f"｜版本 {self._kit.stats()['manifest_version']}"
            )
        else:
            yield event.plain_result(f"重载失败：{self._load_error}")

    @filter.command("表情重建向量")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def _rebuild(self, event: AstrMessageEvent):
        yield event.plain_result("开始重建向量，可能要十几秒…")
        ok = await self._picker.build(self._index.items, force=True)
        sem = self._picker.stats()
        if ok:
            yield event.plain_result(f"向量重建完成：{sem['vectors']} 张 / dim={sem['dim']}")
        else:
            yield event.plain_result(f"重建失败：{sem['error']}")

    @filter.command("表情署名")
    async def _credit(self, event: AstrMessageEvent):
        yield event.plain_result(CREDIT_FULL)

    @filter.command("表情图")
    async def _meme_cmd(self, event: AstrMessageEvent):
        if not self._renderer:
            yield event.plain_result("贴字表情没开（配置里 meme_enabled 关着）")
            return
        raw = event.message_str or ""
        arg = raw.partition(" ")[2].strip() if " " in raw else ""
        if not arg:
            yield event.plain_result(
                "用法：/表情图 <模板> <文字>\n"
                "模板：" + "、".join(f"{k}={v}" for k, v in TEMPLATE_LABELS.items()) + "\n"
                "例：/表情图 想 今天不想上班 ｜ /表情图 抱（用你的头像）"
            )
            return
        parts = arg.split(" ", 1)
        name = parts[0].strip()
        text = parts[1].strip() if len(parts) > 1 else ""
        tpl = self._template_of(name)
        if tpl is None:
            yield event.plain_result(
                f"没有这个模板：{name}（可用：{'、'.join(TEMPLATE_LABELS)}）"
            )
            return
        try:
            image = None
            if tpl == "hug":
                image = self._avatar_url(event)
            path = await self._renderer.render(tpl, text, image)
        except RenderError as e:
            yield event.plain_result(f"画不出来：{e}")
            return
        except Exception as e:
            logger.exception("[nekowhale] 贴字表情失败")
            yield event.plain_result(f"画不出来：{e}")
            return
        yield event.image_result(str(path))

    # ------------------------------------------------------------ llm tools
    @filter.llm_tool(name="nekowhale_sticker_search")
    async def tool_search(
        self, event: AstrMessageEvent, query: str, limit: int = 5, emotion: str = ""
    ):
        """在大肥鱼表情库里搜索合适的表情。

        什么时候用：你想发张表情但不确定库里有哪张合适时，先用它找，再用
        nekowhale_sticker_send 发出去。也可以自问自答式用：只想看看某情绪下有什么图。

        Args:
            query(string): 想表达的意思或关键词，比如「摸鱼」「委屈」「点赞」
            limit(number): 返回几张，默认 5，最多 10
            emotion(string): 只在这个情绪里找，比如「开心」「难过」「疲惫」；留空表示不限
        """
        if not self._index.items:
            return "表情库还没就绪（首次启动要下载清单），这次先别发表情。"
        limit = max(1, min(10, int(limit or 5)))
        items = self._index.search(query, limit=limit, emotion=emotion)
        # 记下来，这样模型接着可以说「发第 2 张」
        self._last_search_items = list(items)
        if not items:
            return f"没搜到「{query}」相关的表情。可以换个说法，或直接正常聊天。"
        lines = []
        for idx, it in enumerate(items, 1):
            bits = [it["name"] or it["id"]]
            if it.get("emotion"):
                bits.append(f"〔{it['emotion']}〕")
            if it.get("tags"):
                bits.append("标签：" + "、".join(it["tags"][:5]))
            if it.get("use"):
                bits.append("用法：" + it["use"])
            lines.append(f"{idx}. " + " ".join(bits))
        lines.append("要发哪张，用 nekowhale_sticker_send 传名字（带编号也行）。")
        return "\n".join(lines)

    @filter.llm_tool(name="nekowhale_sticker_send")
    async def tool_send(self, event: AstrMessageEvent, name: str):
        """把一张大肥鱼表情发到当前会话。

        什么时候用：想让对方看到图（而不是只描述图）时。发完就不用再描述那张图了，
        随后正常说你的话即可。一轮里最多发一张。

        Args:
            name(string): 表情名，比如「摸鱼」「猫猫搓手」；也可以是 nekowhale_sticker_search 返回的编号或完整 id
        """
        if not self._index.items:
            return "表情库还没就绪，这次先别发。"
        item = self._resolve_item(name)
        if item is None:
            hint = self._index.search(name, limit=3)
            if hint:
                cand = "、".join(h["name"] or h["id"] for h in hint)
                return f"没有叫「{name}」的表情。相近的有：{cand}。换个名字再试。"
            return f"没有叫「{name}」的表情，换个名字或先用 nekowhale_sticker_search 搜一下。"
        try:
            path = await self._kit.ensure_sticker(item)
        except Exception as e:
            return f"这张表情下载失败了（{e}），这次别发图，正常回话。"
        try:
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
        except Exception as e:
            return f"发图失败（{e}），别重试，正常回话。"
        event.set_extra(EXTRA_SENT, True)
        return f"已发出表情「{item['name'] or item['id']}」〔{item.get('emotion') or '无情绪'}〕，不用再描述这张图。"

    @filter.llm_tool(name="nekowhale_sticker_info")
    async def tool_info(self, event: AstrMessageEvent, names: str):
        """查表情的详细信息：标签、适用场景、画面描述、台词。

        什么时候用：想确认一张图具体画的是什么、适合什么场合，或者对方问「这图什么意思」时。

        Args:
            names(string): 一个或多个表情名，用逗号或顿号隔开
        """
        if not self._index.items:
            return "表情库还没就绪。"
        keys = [k.strip() for k in names.replace("、", ",").replace("，", ",").split(",") if k.strip()]
        if not keys:
            return "要查哪个表情？给我名字，比如「摸鱼」。"
        out = []
        for key in keys[:5]:
            item = self._resolve_item(key)
            if item is None:
                out.append(f"「{key}」：没找到")
                continue
            lines = [f"「{item['name'] or item['id']}」"]
            if item.get("emotion"):
                lines.append(f"  情绪：{item['emotion']}")
            if item.get("tags"):
                lines.append("  标签：" + "、".join(item["tags"]))
            if item.get("use"):
                lines.append(f"  适用：{item['use']}")
            if item.get("desc"):
                lines.append(f"  画面：{item['desc']}")
            if item.get("text"):
                lines.append(f"  台词：{item['text']}")
            if item.get("dir"):
                lines.append(f"  方向：{item['dir']}")
            out.append("\n".join(lines))
        out.append(CREDIT_BRIEF)
        return "\n".join(out)

    @filter.llm_tool(name="nekowhale_meme_render")
    async def tool_render(
        self, event: AstrMessageEvent, template: str, text: str = "", image: str = ""
    ):
        """把一句话贴进大肥鱼的表情里，生成一张新 GIF 并直接发出去。

        模板（括号里是中文别名）：think（想）、sign（举牌）、scroll（葱卷）、
        couplet_top（上联）、couplet_bottom（下联）、hug（抱）。

        Args:
            template(string): 模板名，如 think / 想 / 举牌 / 抱
            text(string): 要贴上去的文字。想/举牌 建议 2~20 字；葱卷可到 60 字；上联下联建议 5~12 字且竖排
            image(string): 只有 hug 用：头像图片地址或本地路径；留空则用对方的 QQ 头像
        """
        if not self._renderer:
            return "贴字表情功能关着，没法画。"
        tpl = self._template_of(template)
        if tpl is None:
            return f"没有「{template}」这个模板（可用：{'、'.join(TEMPLATE_LABELS)}）。"
        if tpl == "hug" and not image:
            image = self._avatar_url(event) or ""
        try:
            path = await self._renderer.render(tpl, text, image or None)
        except RenderError as e:
            return f"画不出来：{e}"
        except Exception as e:
            logger.exception("[nekowhale] 贴字表情失败")
            return f"画不出来（{e}），别重试。"
        try:
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
        except Exception as e:
            return f"图做好了但发送失败（{e}），别再画了。"
        event.set_extra(EXTRA_SENT, True)
        return f"已发出「{TEMPLATE_LABELS.get(tpl, tpl)}」贴字表情，不用再描述这张图。"

    # ---------------------------------------------------------------- helper
    @staticmethod
    def _template_of(name: str) -> str | None:
        key = (name or "").strip().lower()
        aliases = {v: k for k, v in TEMPLATE_LABELS.items()}
        if key in TEMPLATES:
            return key
        if key in aliases:
            return aliases[key]
        # 英文别名
        extra = {"hug": "hug", "bear": "hug", "抱抱": "hug", "想": "think", "think": "think"}
        return extra.get(key)

    def _resolve_item(self, name: str) -> dict[str, Any] | None:
        raw = (name or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            recent = self._last_search
            if 0 <= idx < len(recent):
                return recent[idx]
        # nekowhale_sticker_search 的编号有时会带出来「3」或「3. 摸鱼」
        head = raw.split(".")[0].strip()
        if head.isdigit() and head != raw:
            return self._resolve_item(head)
        return self._index.by_id(raw)

    @property
    def _last_search(self) -> list[dict[str, Any]]:
        return getattr(self, "_last_search_items", [])

    def _avatar_url(self, event: AstrMessageEvent) -> str | None:
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:
            return None
        if sid.isdigit() and len(sid) <= 12:
            return f"https://q1.qlogo.cn/g?b=qq&nk={sid}&s=640"
        return None

    async def terminate(self):
        for task in (self._warm_task, self._vector_task, self._prefetch_task, self._ready_task):
            if task and not task.done():
                task.cancel()
        logger.info("[nekowhale] 已卸载")
