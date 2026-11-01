"""表情索引：清单 + 标注 → 可挑选的表情池，以及关键词打分。

和 2.1 的区别：**不再要求图片已经在本地**。插件不带素材，用户装完只有清单
（`manifest.json`，172KB），图按需下载。所以索引里的 `present` 只决定
「这张点了要不要现下」，不决定「这张能不能选」。

另外多了 `enabled_packs`：默认只开「大肥鱼」，「大肥鱼和GPT娘」要手动打开
（双人表情 + GPT娘形象带 OpenAI 标识，群聊里容易突兀）。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

# 字段权重（与 nekowhale 官方 search.js 的 WEIGHTS 对齐，但改为
# "表情关键词 → 回复文本" 的反向匹配，更适合中文长句）
W_NAME = 5.0
W_TAG = 4.0
W_TAG_SHORT = 2.0
W_EMOTION = 3.5
W_ALIAS = 3.0

DEFAULT_FALLBACK_EMOTIONS = ("其他", "开心", "搞怪", "卖萌", "打招呼", "赞同", "惊讶")
DEFAULT_PACKS = ("大肥鱼",)

# 口语里表达某情绪时常用的说法。标签是提炼过的书面词（委屈/破防/肝不动），
# 聊天原文往往说"好气哦""被鸽了""绷不住"，所以要这层口语到情绪的映射。
# 注意：只放有情绪指向的词，"什么""怎么""?" 这类纯疑问词会误判，一律不放。
EMOTION_ALIASES: dict[str, tuple[str, ...]] = {
    "开心": ("开心", "高兴", "太好了", "好耶", "真棒", "舒服", "满足", "满意", "不错", "漂亮", "恭喜"),
    "大笑": ("哈哈", "笑死", "爆笑", "笑喷", "乐死", "绷不住", "蚌埠住", "好笑", "笑疯"),
    "笑": ("笑",),
    "搞怪": ("整活", "离谱", "抽象", "逆天", "沙雕", "搞笑", "节目效果", "搞事"),
    "兴奋": ("激动", "起飞", "期待", "来了来了", "兴奋", "燃", "热血", "冲鸭"),
    "卖萌": ("撒娇", "可爱", "蹭蹭", "贴贴", "卖萌", "软软", "呜"),
    "生气": ("生气", "好气", "窝火", "气死", "恼", "火大", "烦死", "好烦", "骂", "欠揍", "被鸽", "鸽了", "锤你", "警告", "别惹"),
    "喜欢": ("喜欢", "爱你", "心动", "想你", "抱抱", "亲亲", "贴贴", "好可爱"),
    "无语": ("无语", "服了", "汗", "逆天", "看不下去", "忍不了", "绷住"),
    "得意": ("得意", "小看我", "骄傲", "拿捏", "稳了", "轻松", "简单", "嘿嘿", "那当然"),
    "疲惫": ("累", "困", "睡", "晚安", "加班", "熬夜", "躺平", "摸鱼", "肝", "摆烂", "歇", "没劲", "撑不住", "头疼"),
    "难过": ("难过", "伤心", "委屈", "破防", "心碎", "没钱", "吃土", "失意", "唉", "emo", "崩溃", "难受", "烦"),
    "哭": ("哭", "呜呜", "眼泪", "泪目", "想哭"),
    "惊讶": ("震惊", "居然", "没想到", "真的假的", "惊了", "卧槽"),
    "疑惑": ("不懂", "疑惑", "没懂", "咋回事", "为什么"),
    "害怕": ("害怕", "恐怖", "吓", "不敢", "瑟瑟", "惊悚"),
    "拒绝": ("不要", "不行", "拒绝", "讨厌", "算了", "不干"),
    "嘲讽": ("呵呵", "就这", "阴阳", "嘲讽", "笑死你", "菜鸡"),
    "打招呼": ("你好", "哈喽", "嗨", "来了", "冒泡", "在吗", "好久不见", "早安", "早上好", "拜拜", "撤了", "下线", "走了"),
    "赞同": ("确实", "同意", "有道理", "+1", "赞同", "对的", "是吧", "就是说", "没错"),
    "害羞": ("害羞", "脸红", "不好意思", "捂脸", "别夸"),
    "思考": ("思考", "考虑", "琢磨", "emmm", "让我想想"),
    "感谢": ("谢谢", "感谢", "谢啦", "多谢", "thx", "3q", "麻烦你"),
    "安慰": ("抱抱", "没事", "别难过", "安慰", "摸摸", "我在"),
    "其他": (),
}

# 用户那句话的情绪权重远高于 bot 自己的回复（表情是用来回应对方的）。
USER_ALIAS_WEIGHT = 2.0
REPLY_ALIAS_WEIGHT = 0.5


def norm_packs(raw: Any) -> list[str] | None:
    """配置里的包名列表 → 规范化。空表示"全开"。"""
    if raw is None:
        return list(DEFAULT_PACKS)
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,，\s]+", raw) if p.strip()]
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_PACKS)
    out = [str(p).strip() for p in raw if str(p).strip()]
    return out or list(DEFAULT_PACKS)


class StickerIndex:
    def __init__(
        self,
        data_dir: str | Path,
        manifest: dict[str, Any],
        meta: dict[str, Any] | None = None,
        max_bytes: int = 3_000_000,
        exclude_emotions: list[str] | None = None,
        fallback_emotions: list[str] | None = None,
        penalize_direction: bool = True,
        enabled_packs: list[str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.manifest = manifest or {}
        self.meta = meta or {}
        self.max_bytes = int(max_bytes or 0)
        self.exclude_emotions = set(exclude_emotions or [])
        self.fallback_emotions = list(fallback_emotions or DEFAULT_FALLBACK_EMOTIONS)
        self.penalize_direction = bool(penalize_direction)
        self.enabled_packs = norm_packs(enabled_packs)

        self.items: list[dict[str, Any]] = []
        self.by_emotion: dict[str, list[dict[str, Any]]] = {}
        self.meta_found = bool((self.meta or {}).get("stickers"))
        self.loaded_at = 0.0
        self.total_known = 0
        self.skipped_pack = 0
        self.skipped_size = 0
        self.skipped_emotion = 0

    # ------------------------------------------------------------------ load
    def load(self) -> int:
        annotations: dict[str, dict[str, Any]] = {}
        for st in (self.meta or {}).get("stickers") or []:
            sid = st.get("id")
            if sid:
                annotations[str(sid)] = st

        items: list[dict[str, Any]] = []
        for it in (self.manifest or {}).get("items") or []:
            sid = it.get("id")
            pack = it.get("pack")
            fname = it.get("file")
            if not (sid and pack and fname):
                continue
            self.total_known += 1
            if self.enabled_packs and pack not in self.enabled_packs:
                self.skipped_pack += 1
                continue
            size = int(it.get("bytes") or 0)
            if self.max_bytes and size > self.max_bytes:
                self.skipped_size += 1
                continue

            ann = annotations.get(str(sid)) or {}
            emotion = (ann.get("emotion") or "").strip()
            if self.exclude_emotions and emotion in self.exclude_emotions:
                self.skipped_emotion += 1
                continue

            path = self.data_dir / "packs" / str(pack) / str(fname)
            try:
                present = path.is_file() and (
                    not size or path.stat().st_size == size
                )
            except OSError:
                present = False

            tags = [t for t in (ann.get("tags") or []) if isinstance(t, str) and t.strip()]
            items.append(
                {
                    "id": str(sid),
                    "name": (ann.get("name") or it.get("name") or "").strip(),
                    "pack": str(pack),
                    "file": str(fname),
                    "hash": str(it.get("hash") or ""),
                    "emotion": emotion,
                    "tags": tags,
                    "use": (ann.get("use") or "").strip(),
                    "desc": (ann.get("desc") or "").strip(),
                    "meme": (ann.get("meme") or "").strip(),
                    "dir": (ann.get("dir") or "").strip(),
                    "text": (ann.get("text") or "").strip(),
                    "bytes": size,
                    "w": int(it.get("w") or 0),
                    "h": int(it.get("h") or 0),
                    "frames": int(it.get("frames") or 0),
                    "path": str(path),
                    "present": present,
                }
            )

        self.items = items
        by_emotion: dict[str, list[dict[str, Any]]] = {}
        for it in items:
            by_emotion.setdefault(it["emotion"], []).append(it)
        self.by_emotion = by_emotion
        return len(items)

    # ----------------------------------------------------------------- score
    @staticmethod
    def _score_one(item: dict[str, Any], text: str) -> float:
        score = 0.0
        name = item.get("name") or ""
        if name and len(name) >= 2 and name in text:
            score += W_NAME
        for tag in item.get("tags") or []:
            if not tag:
                continue
            if len(tag) == 1:
                if tag in text:
                    score += W_TAG_SHORT
            elif tag in text:
                score += W_TAG
        # 描摹与用法是长句，只在用户/回复原样提到时才计分（少见，作加成）
        for field, weight in (("meme", 3.0), ("use", 2.0)):
            kw = item.get(field) or ""
            if kw and len(kw) >= 4 and kw in text:
                score += weight
        return score

    def emotion_scores(self, reply_text: str, user_text: str = "") -> dict[str, float]:
        """按口语别名给每个情绪打分，决定这张表情应该表达什么情绪。

        - 每个情绪只取**最长命中别名**，避免"开心/不开心"这类长短重叠把分数堆起来
        - 用户那句话权重高于 bot 自己的回复——表情是用来回应对方的
        """
        scores: dict[str, float] = {}
        for emotion, aliases in EMOTION_ALIASES.items():
            if not aliases:
                continue
            user_best = 0.0
            reply_best = 0.0
            for alias in aliases:
                if not alias:
                    continue
                weight = 1.0 if len(alias) >= 2 else 0.5
                if user_text and alias in user_text:
                    user_best = max(user_best, weight)
                if alias in reply_text:
                    reply_best = max(reply_best, weight)
            total = user_best * USER_ALIAS_WEIGHT + reply_best * REPLY_ALIAS_WEIGHT
            if total > 0:
                scores[emotion] = total
        return scores

    def rank(self, reply_text: str, user_text: str = "") -> list[tuple[float, dict[str, Any]]]:
        """在「情绪已经定好」的前提下，给该情绪内的表情按标签贴合度排序。"""
        emo_scores = self.emotion_scores(reply_text, user_text)
        if not emo_scores:
            return []
        best_emo = max(emo_scores.items(), key=lambda kv: kv[1])[0]
        pool = list(self.by_emotion.get(best_emo) or [])
        if not pool:
            return []
        text = f"{reply_text}\n{user_text}"
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in pool:
            score = self._score_one(item, text) or 0.1
            if self.penalize_direction and item.get("dir") and len(self.enabled_packs) < 2:
                score *= 0.85
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    # ------------------------------------------------------------------ pick
    def pick(
        self,
        reply_text: str,
        user_text: str = "",
        recent: list[str] | None = None,
        top_n: int = 6,
    ) -> dict[str, Any] | None:
        if not self.items:
            return None
        recent_set = set(recent or [])

        def _not_recent(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
            filtered = [i for i in pool if i["id"] not in recent_set]
            return filtered or pool

        ranked = self.rank(reply_text, user_text)
        if ranked:
            head = [it for _s, it in ranked[: max(1, int(top_n))]]
            cand = _not_recent(head)
            if cand:
                return random.choice(cand)

        pool: list[dict[str, Any]] = []
        for emo in self.fallback_emotions:
            pool.extend(self.by_emotion.get(emo) or [])
        if not pool:
            pool = [i for i in self.items if i.get("emotion")]
        if not pool:
            pool = list(self.items)
        pool = _not_recent(pool)
        return random.choice(pool) if pool else None

    # ------------------------------------------------------------- 检索接口
    def search(self, query: str, limit: int = 5, emotion: str = "") -> list[dict[str, Any]]:
        """给模型用的关键词检索：名字/标签/用法/画面描述/情绪都能命中。"""
        q = (query or "").strip()
        pool = self.items
        if emotion:
            pool = [i for i in pool if i.get("emotion") == emotion]
        if not q:
            return self._shuffle(pool)[:limit]
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in pool:
            score = self._score_one(item, q)
            if item.get("emotion") and item["emotion"] in q:
                score += W_EMOTION
            if q and q in (item.get("name") or ""):
                score += 2.0
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _s, it in scored[:limit]]

    @staticmethod
    def _shuffle(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = list(pool)
        random.shuffle(out)
        return out

    def by_id(self, key: str) -> dict[str, Any] | None:
        key = (key or "").strip()
        if not key:
            return None
        for item in self.items:
            if item["id"] == key or item["name"] == key:
                return item
        for item in self.items:
            if key in item["id"] or (item.get("name") and key in item["name"]):
                return item
        return None

    def stats(self) -> dict[str, Any]:
        missing = sum(1 for i in self.items if not i.get("present"))
        return {
            "total": len(self.items),
            "present": len(self.items) - missing,
            "missing": missing,
            "meta_found": self.meta_found,
            "data_dir": str(self.data_dir),
            "annotated": sum(1 for i in self.items if i["emotion"]),
            "emotions": {
                k: len(v) for k, v in sorted(self.by_emotion.items(), key=lambda x: -len(x[1]))
            },
            "size_mb": round(sum(i["bytes"] for i in self.items) / 1048576, 1),
            "packs": self.enabled_packs,
            "known": self.total_known,
            "skipped_pack": self.skipped_pack,
            "skipped_size": self.skipped_size,
        }
