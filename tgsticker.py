# -*- coding: utf-8 -*-
"""Telegram 原生贴纸（send_sticker）最小实现：GIF/单帧图 → VP9 + alpha 的 WEBM。

为什么需要：AstrBot 4.29.4 的 TG 适配器把插件返回的 Image/GIF 一律走
`send_animation`（`tg_event.py:377-388`），在 TG 里显示成「循环视频/动图」，
不是原生贴纸。插件本来能拿到平台实例上的 `ExtBot`，直接 `send_sticker`
就能发原生贴纸，不用改 AstrBot 核心、也不用重启。

设计边界（都是硬的）：
* **只给 TG 用**。本模块顶层只 import 标准库和 AstrBot 的 logger，Pillow
  在函数里现取；**任何地方都不 import python-telegram-bot**。非 TG 平台走不到
  这里，QQ 的 Image 路径一个字节都不改。
* **不新增运行时依赖**。转码用本机已有的 ffmpeg（配置/环境变量/PATH 里找），
  找不到就返回「不可用」，由调用方按原生开关决定是否走旧 Image 路径，绝不安装、
  绝不报成功。
* **不猜结果**。`send()` 三态返回：
  `None` = 没发（不可用/转码失败/参数被拒/话题 id 不可靠）；
  `True` = Telegram 明确返回了消息对象；
  `False` = 已经尝试发送但失败（可能已经到了 TG 侧）——外层**不许**再走一次
  Image/context 发送，否则同一条表情会发两遍。
  `None` 是否回退旧 GIF 由调用方决定：原生开关开着时**不许**静默回退，
  只有非 TG 或开关关闭时才走旧 Image 路径。
* 转码产物有上限：512×512、≤3 秒、≤256KB、限帧、ffmpeg 超时；缓存按
  源文件（路径+mtime+大小+编码参数）哈希，命中不重转。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

# Telegram 视频贴纸规格：512×512、≤3 秒、≤256KB。
TG_SIDE = 512
TG_MAX_BYTES = 256 * 1024
TG_MAX_SECONDS = 3.0
TG_MAX_FRAMES = 240
TG_MIN_FPS = 10
TG_MAX_FPS = 30
TG_DEFAULT_FPS = 15
CONVERT_TIMEOUT = 25.0
# PTB 的 read/write/connect/pool 是分段 httpx 超时，文件上传默认 write 用
# media_write_timeout=20s。这里显式传更合理的预算，外层 wait_for 必须晚于
# 内层各段之和（10+20+5+2=37s），否则外层会抢先截断一个本来还有效的请求。
SEND_READ_TIMEOUT = 10.0
SEND_WRITE_TIMEOUT = 20.0
SEND_CONNECT_TIMEOUT = 5.0
SEND_POOL_TIMEOUT = 2.0
SEND_TIMEOUT = 42.0  # > 37s 内层总和，留 5s 给事件循环/文件关闭
ENCODE_CRF = 34
# 首轮 CRF34 成品若超过 256KB，只再做一次更狠的 CRF46；实测最大的 3 秒
# 512×512 GIF 可从约 325KB 压到约 161KB。仍不够就安全回退，不引入降帧/
# 降分辨率等额外路径。
ENCODE_RETRY_CRF = 46
# 重试也要留在 convert_timeout 总预算里；剩余不足 2 秒就不冒险再开一次 ffmpeg。
RETRY_MIN_SECONDS = 2.0
CACHE_DIRNAME = "tg_stickers"
# 换编码参数（或换 ffmpeg 大版本）时改这里，老缓存自然失效。
ENCODE_SIG = "vp9-yuva420p-rgba512-crf%d-retry%d" % (ENCODE_CRF, ENCODE_RETRY_CRF)

_FFMPEG_ENV = "NEKOWHALE_FFMPEG"
_FFMPEG_FALLBACKS = (
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "C:/Program Files/ffmpeg/bin/ffmpeg.exe",
)


def _attr(obj: Any, *names: str) -> Any:
    """按顺序取第一个存在的属性（支持 a.b.c 这种链）。"""
    for name in names:
        cur = obj
        ok = True
        for part in name.split("."):
            cur = getattr(cur, part, None)
            if cur is None:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


def platform_name(event: Any) -> str:
    """尽量拿到平台名；AstrBot 各版本字段名不完全一样。"""
    for getter in ("get_platform_name", "get_platform_id"):
        fn = getattr(event, getter, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                value = None
            if value:
                return str(value)
    for name in ("platform_name", "platform_meta.name", "platform_meta.id"):
        value = _attr(event, name)
        if value:
            return str(value)
    return ""


def is_telegram(event: Any) -> bool:
    """只认 telegram，别让子串碰瓷别的平台（qq_official / dingtalk 都不该命中）。"""
    name = platform_name(event).strip().lower()
    if not name:
        return False
    return name == "tg" or "telegram" in name


def find_client(event: Any) -> Any:
    """找平台实例上的 Bot（AstrBot 的 TG 适配器把它挂在 self.client 上）。"""
    for name in (
        "client",
        "bot",
        "platform.client",
        "platform.bot",
        "message_obj.client",
        "message_obj.raw_message._bot",
    ):
        obj = _attr(event, name)
        if obj is not None and hasattr(obj, "send_sticker"):
            return obj
    return None


def resolve_chat_id(event: Any) -> Any:
    """拿这次会话的 chat_id；群聊要的是群，不是发言人。"""
    for name in (
        "chat_id",
        "message_obj.chat_id",
        "message_obj.raw_message.chat_id",
        "message_obj.raw_message.chat.id",
        "message_obj.raw_message.message.chat.id",
        # Telegram 适配器把 chat.id 写成字符串塞进这两个字段
        "message_obj.session_id",
        "message_obj.group_id",
    ):
        value = _attr(event, name)
        if isinstance(value, (int, str)) and str(value).strip():
            return value
    # 最后才问事件本身；这里刻意不碰 get_sender_id——群里的表情不该变成
    # 发给某一个人的私聊
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if value not in (None, "", 0, "0"):
            return value
    return None


def _thread_from_text(value: Any) -> int | None:
    """从适配器写的 "chat_id#thread_id" 后缀里取话题 id；普通 chat_id 返回 None。"""
    if not isinstance(value, str) or "#" not in value:
        return None
    base, _, suffix = value.rpartition("#")
    if not base or not suffix.isdigit():
        return None
    thread = int(suffix)
    return thread if thread > 0 else None


def _as_thread_id(value: Any) -> int | None:
    """PTB 的 message_thread_id 可能是 int 或数字字符串；其它形状一律不认。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        thread = int(value.strip())
        return thread if thread > 0 else None
    return None


def split_chat_thread(chat_id: Any) -> tuple[Any, int | None]:
    """拆掉 resolve_chat_id 带出来的 "#话题" 后缀，返回 (真实 chat_id, 话题 id)。"""
    thread = _thread_from_text(chat_id)
    if thread is not None:
        base, _, _ = str(chat_id).rpartition("#")
        return base, thread
    return chat_id, None


def resolve_thread_id(event: Any) -> tuple[int | None, bool]:
    """拿这次消息的话题 id。

    返回 (thread_id, 是否看到话题证据)。第一个值 None 且第二个 True 表示
    「这是话题消息但拿不到可靠 id」——调用方不能猜主群或别的会话；原生开关
    开着时这张就不发，只有非 TG 或开关关闭时才走旧路径。
    """
    message = _attr(event, "message_obj")
    # AstrBot 4.30 的 TG 适配器只把话题写在 session_id/group_id 的 "#n" 后缀里，
    # AstrBotMessage 本身没有 message_thread_id 属性；先吃这部分最可靠的证据。
    for name in ("session_id", "group_id"):
        thread = _thread_from_text(_attr(message, name))
        if thread is not None:
            return thread, True
    raw = _attr(event, "message_obj.raw_message") or _attr(event, "raw_message")
    is_topic = bool(
        _attr(
            raw,
            "effective_message.is_topic_message",
            "message.is_topic_message",
            "callback_query.message.is_topic_message",
        )
    )
    raw_thread = _as_thread_id(
        _attr(
            raw,
            "effective_message.message_thread_id",
            "message.message_thread_id",
            "callback_query.message.message_thread_id",
        )
    )
    if not is_topic:
        return None, False
    if raw_thread is None:
        return None, True
    # Telegram 论坛的 general topic 是 thread_id=1，适配器把它当普通消息处理；
    # 这里同样不把 1 号话题当成要显式携带的 thread。
    is_forum = bool(
        _attr(
            raw,
            "effective_message.chat.is_forum",
            "message.chat.is_forum",
            "callback_query.message.chat.is_forum",
        )
    )
    if is_forum and raw_thread == 1:
        return None, False
    return raw_thread, True


def locate_ffmpeg() -> str | None:
    """本机已有的 ffmpeg：环境变量 > PATH > 常见绝对路径。找不到就 None。"""
    env = str(os.environ.get(_FFMPEG_ENV) or "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in _FFMPEG_FALLBACKS:
        if Path(cand).is_file():
            return cand
    return None


def _fps_of(durations: list[int]) -> int:
    usable = [d for d in durations if d and d > 0]
    if not usable:
        return TG_DEFAULT_FPS
    avg_ms = sum(usable) / len(usable)
    if avg_ms <= 0:
        return TG_DEFAULT_FPS
    fps = int(round(1000.0 / avg_ms))
    return max(TG_MIN_FPS, min(TG_MAX_FPS, fps))


class _StickerArgError(Exception):
    """SDK 直接拒绝了这个参数（不是发不出去，而是没被受理）。

    这种错误发生在真正发 HTTP 之前，所以没有双发风险；但原生开关开着时
    外层也不许静默退回旧 GIF（那会把失败伪装成成功），只如实报未发出。
    网络错误、超时一律不走这里——那种情况可能已经到了 TG，绝不能再发一遍。
    """


class _OversizeError(Exception):
    """转码后超限，且一次有界重试也没救回来；让 prepare 走安全回退。"""

    def __init__(
        self,
        size: int,
        limit: int,
        *,
        first_size: int | None = None,
        retried: bool = False,
        reason: str = "oversize",
    ) -> None:
        self.size = size
        self.limit = limit
        self.first_size = first_size
        self.retried = retried
        self.reason = reason
        super().__init__(self._message())

    def _message(self) -> str:
        if self.reason == "budget":
            return "首轮转码 %dB 超过 %dB 上限，剩余时间预算不足，未做压缩重试" % (
                self.first_size or self.size,
                self.limit,
            )
        if self.reason == "timeout":
            return "压缩重试超时（首轮 %dB 超过 %dB 上限）" % (
                self.first_size or self.size,
                self.limit,
            )
        if self.retried:
            return "压缩重试后仍 %dB 超过 %dB 上限（首轮 %dB）" % (
                self.size,
                self.limit,
                self.first_size or self.size,
            )
        return "转码后 %dB 超过 %dB 上限" % (self.size, self.limit)


def _send_failure_kind(exc: BaseException) -> str:
    """把 PTB 异常归类成可写日志的短标签，不把原始异常文本带进日志。

    不 import python-telegram-bot，按异常类名判断即可覆盖 PTB 22.8 的常见类：
    TimedOut / NetworkError / ConnectError / ReadError / WriteError 等都表示
    「结果未确认」，RetryAfter 是限流，BadRequest/Forbidden 是明确拒绝。
    """
    name = type(exc).__name__.lower()
    if "timedout" in name or "timeout" in name:
        return "超时（结果未确认）"
    if "retryafter" in name:
        return "限流"
    if "badrequest" in name or "forbidden" in name or "invalidtoken" in name:
        return "明确拒绝"
    if any(k in name for k in ("network", "connect", "read", "write", "protocol")):
        return "网络异常（结果未确认）"
    return "发送异常（结果未确认）"


class TgStickerBridge:
    """把本地表情文件转成 TG 原生贴纸并发出去。

    只负责 TG 分支；不可用时返回 None。调用方必须结合原生开关判断：
    开关开着时不要回退旧 GIF，非 TG/开关关闭时才沿用原来的 Image 路径。
    """

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        enabled: bool = True,
        max_bytes: int = TG_MAX_BYTES,
        max_seconds: float = TG_MAX_SECONDS,
        max_frames: int = TG_MAX_FRAMES,
        send_timeout: float = SEND_TIMEOUT,
        convert_timeout: float = CONVERT_TIMEOUT,
        ffmpeg: str | None = None,
    ) -> None:
        root = Path(cache_dir)
        self.cache_dir = root if root.name == CACHE_DIRNAME else root / CACHE_DIRNAME
        self.enabled = bool(enabled)
        self.max_bytes = int(max_bytes)
        self.max_seconds = float(max_seconds)
        self.max_frames = int(max_frames)
        self.send_timeout = float(send_timeout)
        self.convert_timeout = float(convert_timeout)
        self.ffmpeg = ffmpeg
        self.last_error = ""
        self.cache_hits = 0
        self.converted = 0

    # ------------------------------------------------------------- routing
    def route(self, event: Any) -> tuple[Any, Any] | None:
        """能走原生贴纸吗？返回 (client, chat_id)，不能就 None。"""
        if not self.enabled:
            return None
        if not is_telegram(event):
            return None
        client = find_client(event)
        if client is None:
            self.last_error = "TG 平台实例上没有 send_sticker（client 未暴露）"
            return None
        chat_id = resolve_chat_id(event)
        if chat_id is None:
            self.last_error = "拿不到当前会话的 chat_id"
            return None
        return client, chat_id

    # ------------------------------------------------------------ convert
    def cache_path(self, src: Path) -> Path:
        try:
            stat = src.stat()
        except OSError:
            stat = None
        stamp = "%s|%s|%s|%s" % (
            src.resolve(),
            stat.st_mtime_ns if stat else 0,
            stat.st_size if stat else 0,
            ENCODE_SIG,
        )
        key = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / ("%s.webm" % key)

    async def prepare(self, src: Path | str) -> Path | None:
        """源文件 → 带 alpha 的 WEBM；失败返回 None（由调用方决定是否回旧路）。"""
        src = Path(src)
        if not src.is_file():
            self.last_error = "源文件不存在：%s" % src.name
            return None
        out = self.cache_path(src)
        if out.is_file():
            size = out.stat().st_size
            if 0 < size <= self.max_bytes:
                self.cache_hits += 1
                return out
            try:
                out.unlink()
            except OSError:
                pass
        configured = self.ffmpeg if (self.ffmpeg and Path(self.ffmpeg).is_file()) else None
        ffmpeg = configured or locate_ffmpeg()
        if not ffmpeg:
            self.last_error = "本机没有 ffmpeg，TG 原生动态贴纸不可用（不安装依赖）"
            return None
        try:
            await asyncio.to_thread(self._convert, ffmpeg, src, out)
        except _OversizeError as e:
            # 超限不是发送失败：明确记录原因，外层按原生开关决定是否回旧路。
            self.last_error = str(e)
            logger.info("[nekowhale] %s，这张不发原生贴纸" % self.last_error)
            self._discard(out)
            return None
        except Exception as e:  # 转码失败不是发送失败；原生开着时不回旧 GIF
            # 原始异常可能带 ffmpeg stderr/路径；日志只留类名，避免把本机路径带出去。
            kind = type(e).__name__
            self.last_error = "转码失败：%s" % kind
            logger.warning("[nekowhale] TG 贴纸转码失败（%s / %s）" % (src.name, kind))
            self._discard(out)
            return None
        if not out.is_file() or out.stat().st_size <= 0:
            self.last_error = "转码没有产物"
            return None
        size = out.stat().st_size
        if size > self.max_bytes:
            self.last_error = "转码后 %dB 超过 %dB 上限" % (size, self.max_bytes)
            logger.info("[nekowhale] %s，这张不发原生贴纸" % self.last_error)
            try:
                out.unlink()
            except OSError:
                pass
            return None
        self.converted += 1
        self.last_error = ""
        return out

    def _discard(self, path: Path) -> None:
        """删掉不该留下的转码产物/半成品；删不掉也不影响回退。"""
        try:
            path.unlink()
        except OSError:
            pass

    def _encode(
        self,
        ffmpeg: str,
        raw: bytes,
        fps: int,
        out: Path,
        crf: int,
        timeout: float,
    ) -> None:
        """一次独立的 ffmpeg 编码；临时文件唯一，失败/超时都不留半成品。"""
        # 临时文件用 mkstemp 每次取独有名：同源并发转码不会互相覆盖，也不会把
        # 半成品写到别人正在读的成品名上。fd 立刻关掉，后面交给 ffmpeg 写。
        fd, tmp_name = tempfile.mkstemp(
            prefix=out.stem + ".", suffix=".webm.part", dir=str(self.cache_dir)
        )
        os.close(fd)
        tmp = Path(tmp_name)
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            "%dx%d" % (TG_SIDE, TG_SIDE),
            "-framerate",
            str(fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",
            "-b:v",
            "0",
            "-crf",
            str(crf),
            "-auto-alt-ref",
            "0",
            "-row-mt",
            "1",
            # 临时文件叫 *.part，指望 ffmpeg 猜扩展名会失败，显式指定封装格式
            "-f",
            "webm",
            str(tmp),
        ]
        try:
            proc = subprocess.run(
                args,  # 全常量参数，不拼 shell、不塞用户输入
                input=raw,
                capture_output=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
                raise RuntimeError("ffmpeg 退出码 %s：%s" % (proc.returncode, err))
            if not tmp.is_file():
                raise RuntimeError("ffmpeg 没有产出文件")
            os.replace(tmp, out)
        except BaseException:
            # 失败不留半成品，缓存目录里只该有能用的成品
            self._discard(tmp)
            raise

    def _convert(self, ffmpeg: str, src: Path, out: Path) -> None:
        raw, fps = self._frames(src)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        self._encode(ffmpeg, raw, fps, out, ENCODE_CRF, self.convert_timeout)
        first_size = out.stat().st_size
        if first_size <= self.max_bytes:
            return
        # 只解一次帧、只多编码一次：第二次仍用同一个 convert_timeout 总预算，
        # 不排队、不批量、不无界重试；预算不够就带着明确原因回退。
        remaining = self.convert_timeout - (time.monotonic() - started)
        if remaining < RETRY_MIN_SECONDS:
            self._discard(out)
            raise _OversizeError(
                first_size, self.max_bytes, first_size=first_size, reason="budget"
            )
        try:
            self._encode(ffmpeg, raw, fps, out, ENCODE_RETRY_CRF, remaining)
        except subprocess.TimeoutExpired:
            self._discard(out)
            raise _OversizeError(
                first_size, self.max_bytes, first_size=first_size, reason="timeout"
            )
        size = out.stat().st_size
        if size > self.max_bytes:
            self._discard(out)
            raise _OversizeError(size, self.max_bytes, first_size=first_size, retried=True)

    def _frames(self, src: Path) -> tuple[bytes, int]:
        """逐帧解出 RGBA、等比放进 512×512 透明画布；返回 (原始像素, fps)。"""
        from PIL import Image  # Pillow 是 AstrBot 自带依赖（render.py 已在用）

        chunks: list[bytes] = []
        durations: list[int] = []
        with Image.open(src) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            # 3 秒 × 30fps 已经把时长撑满，多解的帧最后也要丢掉：先按这个硬上限
            # 截住，省得把 240 帧 512×512 RGBA（每帧 1MB）全积在内存里。
            useful = int(self.max_seconds * TG_MAX_FPS) or 1
            limit = max(1, min(total, self.max_frames, useful))
            elapsed_ms = 0
            budget_ms = self.max_seconds * 1000.0
            for index in range(limit):
                im.seek(index)
                duration = int(im.info.get("duration") or 0)
                durations.append(duration)
                # 先转成字节再往下走，别把几十帧 PIL 对象全攥在手上
                chunks.append(self._fit(im.convert("RGBA")).tobytes())
                elapsed_ms += duration if duration > 0 else 0
                if duration > 0 and elapsed_ms >= budget_ms:
                    break  # 够 3 秒了，剩下的帧不要
        if not chunks:
            raise ValueError("没有解出任何帧")
        fps = _fps_of(durations)
        keep = max(1, min(len(chunks), int(self.max_seconds * fps) or 1))
        return b"".join(chunks[:keep]), fps

    @staticmethod
    def _fit(frame: Any) -> Any:
        from PIL import Image

        canvas = Image.new("RGBA", (TG_SIDE, TG_SIDE), (0, 0, 0, 0))
        width, height = frame.size
        if width <= 0 or height <= 0:
            return canvas
        scale = min(TG_SIDE / width, TG_SIDE / height)
        size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        if size != (width, height):
            frame = frame.resize(size, Image.LANCZOS)
        canvas.paste(frame, ((TG_SIDE - size[0]) // 2, (TG_SIDE - size[1]) // 2), frame)
        return canvas

    # --------------------------------------------------------------- send
    async def send(self, event: Any, src: Path | str) -> bool | None:
        """发原生贴纸。None = 没发；True/False = 已经向 Telegram 尝试过。

        None 时调用方必须结合原生开关判断是否回旧 Image：开关开着就不能
        静默退回黑底 GIF，只有非 TG 或开关关闭时才走旧路径。
        """
        route = self.route(event)
        if route is None:
            return None
        client, chat_id = route
        chat_id, chat_thread = split_chat_thread(chat_id)
        thread_id, topic = resolve_thread_id(event)
        if chat_thread is not None:
            thread_id = chat_thread if thread_id is None else thread_id
            topic = True
        if topic and thread_id is None:
            # 看到话题证据却拿不到可靠 id：宁可这张不发，也不把表情丢到主群。
            self.last_error = "话题消息拿不到可靠的 message_thread_id，这张不发（不猜主群）"
            logger.warning(
                "[nekowhale] TG 话题消息没有可靠的 message_thread_id，这张不发（不猜主群）"
            )
            return None
        path = await self.prepare(src)
        if path is None:
            return None
        try:
            await self._call_send(client, chat_id, path, thread_id)
        except asyncio.CancelledError:
            raise
        except _StickerArgError:
            # 参数没被受理 = 一个字节都没发出去，没有双发风险；但也不静默变 GIF。
            self.last_error = "send_sticker 参数未被 SDK 受理（确定未发）"
            logger.warning("[nekowhale] TG 原生贴纸参数未被 SDK 受理（确定没发），这张不发")
            return None
        except Exception as e:
            # 原始异常文本可能含对象细节；日志只留类别/异常类名/文件大小/话题。
            kind = type(e).__name__
            category = _send_failure_kind(e)
            self.last_error = "send_sticker 失败：%s" % category
            try:
                kb = max(1, path.stat().st_size // 1024)
            except OSError:
                kb = 0
            logger.warning(
                "[nekowhale] TG 原生贴纸发送失败（不再二次发送）：%s / %s / %sKB / 话题=%s"
                % (category, kind, kb, "是" if thread_id is not None else "否")
            )
            return False
        try:
            kb = max(1, path.stat().st_size // 1024)
        except OSError:
            kb = 0
        logger.info(
            "[nekowhale] TG 原生贴纸已受理（%sKB / 话题=%s）"
            % (kb, "是" if thread_id is not None else "否")
        )
        self.last_error = ""
        return True

    async def _call_send(
        self, client: Any, chat_id: Any, path: Path, thread_id: int | None = None
    ) -> None:
        # 直接给 PTB 一个带 .webm 文件名的文件对象：新版 Bot API 允许用
        # sendSticker 上传 .WEBM（不必先进贴纸集/BotFather），也不需要 import PTB。
        handle = open(path, "rb")
        try:
            try:
                # 只有「调用当场」的 TypeError 才算参数没被受理（那时还没发请求）。
                # 一旦拿到 awaitable，后面再出的错都是「可能已经发出去了」，归失败。
                kwargs = {"chat_id": chat_id, "sticker": handle}
                if thread_id is not None:
                    # 普通群/私聊不带这个参数，保持原调用完全不变。
                    kwargs["message_thread_id"] = thread_id
                # 显式给 PTB 分段预算；旧测试桩/精简 client 的签名可能不认这些
                # 关键字，只传它签名里确实接受的，避免人为造出 TypeError。
                try:
                    params = inspect.signature(client.send_sticker).parameters
                except (TypeError, ValueError):
                    params = {}
                accepts_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                budget = (
                    ("read_timeout", SEND_READ_TIMEOUT),
                    ("write_timeout", SEND_WRITE_TIMEOUT),
                    ("connect_timeout", SEND_CONNECT_TIMEOUT),
                    ("pool_timeout", SEND_POOL_TIMEOUT),
                )
                for key, value in budget:
                    if accepts_kwargs or key in params:
                        kwargs[key] = value
                result = client.send_sticker(**kwargs)
            except TypeError as e:
                raise _StickerArgError(str(e)) from e
            if inspect.isawaitable(result):
                # 外层必须晚于内层各段之和，否则会抢先截断还有效的上传。
                await asyncio.wait_for(result, timeout=self.send_timeout)
        finally:
            handle.close()

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "cache_dir": str(self.cache_dir),
            "cache_hits": self.cache_hits,
            "converted": self.converted,
            "max_bytes": self.max_bytes,
            "last_error": self.last_error,
        }
