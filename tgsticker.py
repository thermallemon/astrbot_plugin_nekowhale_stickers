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
  找不到就返回「不可用」，外层退回原来的 Image 路径，绝不安装、绝不报成功。
* **不猜结果**。`send()` 三态返回：
  `None` = 没发（不可用/转码失败，外层可以安全地走原路，不会双发）；
  `True` = Telegram 明确返回了消息对象；
  `False` = 已经尝试发送但失败（可能已经到了 TG 侧）——外层**不许**再走一次
  Image/context 发送，否则同一条表情会发两遍。
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
SEND_TIMEOUT = 20.0
ENCODE_CRF = 34
CACHE_DIRNAME = "tg_stickers"
# 换编码参数（或换 ffmpeg 大版本）时改这里，老缓存自然失效。
ENCODE_SIG = "vp9-yuva420p-rgba512-crf%d" % ENCODE_CRF

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

    这种错误发生在真正发 HTTP 之前，所以外层可以安全地退回普通图片；
    网络错误、超时一律不走这里——那种情况可能已经到了 TG，绝不能再发一遍。
    """


class TgStickerBridge:
    """把本地表情文件转成 TG 原生贴纸并发出去。

    只负责 TG 分支；不可用时一律返回 None，让调用方沿用原来的 Image 路径。
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
        """源文件 → 带 alpha 的 WEBM；失败返回 None（调用方退回原路）。"""
        src = Path(src)
        if not src.is_file():
            self.last_error = "源文件不存在：%s" % src
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
        except Exception as e:  # 转码失败不是发送失败，走原路不算双发
            self.last_error = "转码失败：%s" % e
            logger.warning("[nekowhale] TG 贴纸转码失败（%s）：%s" % (src.name, e))
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

    def _convert(self, ffmpeg: str, src: Path, out: Path) -> None:
        raw, fps = self._frames(src)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
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
            str(ENCODE_CRF),
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
                timeout=self.convert_timeout,
            )
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
                raise RuntimeError("ffmpeg 退出码 %s：%s" % (proc.returncode, err))
            if not tmp.is_file():
                raise RuntimeError("ffmpeg 没有产出文件")
            os.replace(tmp, out)
        except BaseException:
            # 失败不留半成品，缓存目录里只该有能用的成品
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _frames(self, src: Path) -> tuple[bytes, int]:
        """逐帧解出 RGBA、等比放进 512×512 透明画布；返回 (原始像素, fps)。"""
        from PIL import Image  # Pillow 是 AstrBot 自带依赖（render.py 已在用）

        chunks: list[bytes] = []
        durations: list[int] = []
        with Image.open(src) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            limit = max(1, min(total, self.max_frames))
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
        """发原生贴纸。None = 没发（可安全走原路），True/False = 已尝试发送。"""
        route = self.route(event)
        if route is None:
            return None
        client, chat_id = route
        path = await self.prepare(src)
        if path is None:
            return None
        try:
            await self._call_send(client, chat_id, path)
        except asyncio.CancelledError:
            raise
        except _StickerArgError as e:
            # 参数没被受理 = 一个字节都没发出去，退回原来的图片路径不构成双发
            self.last_error = "send_sticker 不接受文件对象：%s" % e
            logger.warning(
                "[nekowhale] TG 原生贴纸没被 SDK 受理（%s），这张退回普通图片" % e
            )
            return None
        except Exception as e:
            self.last_error = "send_sticker 失败：%s" % e
            logger.warning("[nekowhale] TG 原生贴纸发送失败（不再二次发送）：%s" % e)
            return False
        self.last_error = ""
        return True

    async def _call_send(self, client: Any, chat_id: Any, path: Path) -> None:
        # 直接给 PTB 一个带 .webm 文件名的文件对象：新版 Bot API 允许用
        # sendSticker 上传 .WEBM（不必先进贴纸集/BotFather），也不需要 import PTB。
        handle = open(path, "rb")
        try:
            try:
                # 只有「调用当场」的 TypeError 才算参数没被受理（那时还没发请求）。
                # 一旦拿到 awaitable，后面再出的错都是「可能已经发出去了」，归失败。
                result = client.send_sticker(chat_id=chat_id, sticker=handle)
            except TypeError as e:
                raise _StickerArgError(str(e)) from e
            if inspect.isawaitable(result):
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
