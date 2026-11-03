"""贴字表情渲染：把一句话写进大肥鱼的动作里，输出新的 GIF。

这是上游 `dsh-nekowhale-stickers` 的 `lib/meme/`（Node + @napi-rs/canvas）的
**纯 Python 复刻**：不装 Node、不跑 MCP，只用 Pillow + numpy (+ fontTools)。

为什么能复刻：上游那套 JS 本来就是从服务端的 Python（pil_utils.draw_text）移植过去的，
模板素材是离线算好的「抠掉原字的底图 + 逐帧四角/位移/蒙版」，运行时只做
「把字贴回去 + 编码」，没有必须靠 canvas 才行的东西。

六个模板：
    hug            大头像贴进「抱」的怀里（需要一张头像图）
    think          举着「想」的气泡里写话
    sign           举牌，文字按牌子透视变形贴上去
    scroll         葱卷，长文本滚动
    couplet_top    上联竖排
    couplet_bottom 下联竖排

与官方输出的一致性（逐像素对比首帧，255 制）：think 均值 1.01、sign 1.66、
scroll 0.39、hug 1.39、couplet 4.10 —— 差异全部来自 GIF 调色板量化，肉眼看不出。

三个坑（照抄会踩）：
1. **字体是 woff2 分片**，Pillow/FreeType 读不了，要用 fontTools 转成 TTF 再加载；
   字体文件也跟着 unicode-range 按需拉，所以「测试文字」六个字只下两三个分片。
2. **整段 GIF 必须共用一份调色板**，逐帧各自量化会让静止部分逐帧换色，看起来在闪。
3. **楷体基线差 0.067em**：canvas 的 textBaseline="middle" 按 em 框取中，
   FreeType 的 "mm" 按 hhea asc/desc 取中，直接用会整体偏下。竖排模板里做了常量补偿。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

SITE_PACK = "大肥鱼"
MEME_PACKS = ("大肥鱼",)

TEMPLATES = ("hug", "think", "sign", "scroll", "couplet_top", "couplet_bottom")

TEMPLATE_LABELS = {
    "hug": "抱",
    "think": "想",
    "sign": "举牌",
    "scroll": "葱卷",
    "couplet_top": "上联",
    "couplet_bottom": "下联",
}

# 服务端 skia 量出来的行高口径（见上游 text.js 注释）
METRICS = {"NotoBlack": (1.16, 0.288, 0.0), "Kai": (0.8789, 0.1201, 0.0059)}

# 没有 fontTools / 拉不到字体时的兜底字体（按平台）
FALLBACK_FONTS = {
    "Kai": [
        "C:/Windows/Fonts/simkai.ttf",
        "C:/Windows/Fonts/STKAITI.TTF",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ],
    "NotoBlack": [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
}


class RenderError(RuntimeError):
    pass


# 贴字表情的图片来源：QQ 头像 CDN（q1/q2...qlogo.cn）+ 配置的素材站（含子域）。
# 只在一处校验，`/表情图 抱` 和 LLM 工具 nekowhale_meme_render 都会走到这里。
IMAGE_HOSTS = ("qlogo.cn",)


def image_url_allowed(url: str, site: str = "") -> tuple[bool, str]:
    """判断贴字表情的图片地址是否允许，返回（是否允许，拒绝原因）。"""
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False, "地址格式不对"
    if parsed.scheme not in ("http", "https"):
        return False, "只支持 http(s) 地址"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False, "地址里没有域名"
    allowed = set(IMAGE_HOSTS)
    try:
        site_host = (urllib.parse.urlsplit(site or "").hostname or "").lower().rstrip(".")
    except ValueError:
        site_host = ""
    if site_host:
        allowed.add(site_host)
    for domain in allowed:
        if host == domain or host.endswith("." + domain):
            return True, ""
    return False, f"域名 {host} 不在允许范围"


# --------------------------------------------------------------- 文本排版
def metrics(family: str, px: float) -> tuple[float, float, int]:
    asc, desc, lead = METRICS.get(family, (1.16, 0.288, 0.0))
    return px * asc, px * desc, round(px * (asc + desc + lead))


def balanced_lines(text: str, w: float, h: float) -> list[str]:
    """选一个行数，让字号最大，再把字平均分到各行（上游 balancedLines）。"""
    text = (text or "").strip()
    if not text:
        return []
    if "\n" in text:
        return [s.strip() for s in text.split("\n") if s.strip()]
    chars = list(text)
    n = len(chars)
    if n <= 1 or re.search(r"\s", text):
        return [text]
    best, best_size = 1, 0.0
    for m in range(1, n + 1):
        per = -(-n // m)
        size = min(w / per, h / m)
        if size > best_size:
            best_size, best = size, m
    base, rem = divmod(n, best)
    out, i = [], 0
    for k in range(best):
        take = base + (1 if k < rem else 0)
        out.append("".join(chars[i : i + take]))
        i += take
    return out


class MemeRenderer:
    """一个插件实例一份；内部缓存字体、模板元数据、解码过的底图。"""

    def __init__(self, fetcher: Any, cache_dir: str | Path) -> None:
        self.fetcher = fetcher
        self.cache_dir = Path(cache_dir)
        self._css: dict[str, list[tuple[list[tuple[int, int]], str]]] = {}
        self._ttf: dict[tuple[str, str], bytes] = {}
        self._fonts: dict[tuple[str, str, int], Any] = {}
        self._meme_index: dict[str, Any] | None = None
        self._meta: dict[str, dict[str, Any]] = {}
        self._fallback_cache: dict[tuple[str, int], Any] = {}

    # ------------------------------------------------------------- 取资源
    async def _index(self) -> dict[str, Any]:
        if self._meme_index is None:
            self._meme_index = await self.fetcher.json_of("/meme/index.json")
        return self._meme_index

    async def _asset(self, name: str) -> str:
        idx = await self._index()
        assets = (idx.get("assets") or {}).get(SITE_PACK) or {}
        rel = assets.get(name)
        if not rel:
            raise RenderError(f"官方表情素材里找不到 {name}")
        return "/meme/" + str(rel).lstrip("/")

    async def _meta_of(self, tpl: str) -> dict[str, Any]:
        if tpl not in self._meta:
            raw = await self.fetcher.blob(await self._asset(f"{tpl}.json"))
            self._meta[tpl] = json.loads(raw.decode("utf-8"))
        return self._meta[tpl]

    async def _gif(self, name: str) -> tuple[int, int, list[np.ndarray], list[int]]:
        data = await self.fetcher.blob(await self._asset(name))
        return decode_gif(data)

    async def _png(self, name: str) -> np.ndarray:
        data = await self.fetcher.blob(await self._asset(name))
        return np.array(Image.open(io.BytesIO(data)).convert("RGBA"))

    # --------------------------------------------------------------- 字体
    async def _parse_css(self, family: str) -> list[tuple[list[tuple[int, int]], str]]:
        if family in self._css:
            return self._css[family]
        sub = "kai" if family == "Kai" else "notoblack"
        raw = await self.fetcher.blob(f"/fonts/{sub}/fonts.css")
        css = raw.decode("utf-8", "replace")
        faces: list[tuple[list[tuple[int, int]], str]] = []
        for block in re.findall(r"@font-face\s*\{([^}]+)\}", css):
            m = re.search(r"url\(['\"]?([^)'\"\s]+)['\"]?\)", block)
            r = re.search(r"unicode-range\s*:\s*([^;]+)", block, re.I)
            if not m or not r:
                continue
            path = m.group(1)
            rel = path.lstrip("/") if path.startswith("/") else f"fonts/{sub}/{path}"
            ranges: list[tuple[int, int]] = []
            for part in r.group(1).split(","):
                mm = re.match(r"\s*U\+([0-9A-Fa-f?]+)(?:-([0-9A-Fa-f]+))?", part.strip())
                if not mm:
                    continue
                lo = int(mm.group(1).replace("?", "0"), 16)
                hi = int(mm.group(2) or mm.group(1).replace("?", "F"), 16)
                ranges.append((lo, hi))
            faces.append((ranges, rel))
        self._css[family] = faces
        return faces

    async def _ttf_bytes(self, family: str, rel: str) -> bytes:
        key = (family, rel)
        if key in self._ttf:
            return self._ttf[key]
        cache = self.cache_dir / "fonts" / rel.replace("/", "_").replace("\\", "_")
        ttf = cache.with_suffix(".ttf")
        if ttf.is_file():
            data = await asyncio.to_thread(ttf.read_bytes)
        else:
            raw = await self.fetcher.blob("/" + rel.lstrip("/"))
            data = await asyncio.to_thread(woff2_to_ttf, raw)
            try:
                ttf.parent.mkdir(parents=True, exist_ok=True)
                tmp = ttf.with_suffix(".ttf.part")
                await asyncio.to_thread(tmp.write_bytes, data)
                await asyncio.to_thread(os.replace, tmp, ttf)
            except OSError as e:
                logger.debug(f"[nekowhale] 字体缓存写入失败：{e}")
        self._ttf[key] = data
        return data

    async def _chart(self, family: str, ch: str) -> str | None:
        cp = ord(ch)
        for ranges, rel in await self._parse_css(family):
            for lo, hi in ranges:
                if lo <= cp <= hi:
                    return rel
        return None

    def _fallback_font(self, family: str, px: int):
        key = (family, px)
        if key in self._fallback_cache:
            return self._fallback_cache[key]
        font = None
        for path in FALLBACK_FONTS.get(family, []) + FALLBACK_FONTS["NotoBlack"]:
            try:
                if Path(path).is_file():
                    font = ImageFont.truetype(path, px)
                    break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        self._fallback_cache[key] = font
        return font

    async def font_for(self, family: str, ch: str, px: int):
        try:
            rel = await self._chart(family, ch)
            if rel is None:
                return None
            key = (family, rel, px)
            if key not in self._fonts:
                data = await self._ttf_bytes(family, rel)
                self._fonts[key] = ImageFont.truetype(io.BytesIO(data), px)
            return self._fonts[key]
        except Exception as e:  # fontTools 没装 / 字体拉不下来
            logger.debug(f"[nekowhale] 字体分片不可用，改用系统字体：{e}")
            return self._fallback_font(family, px)

    async def text_width(self, family: str, s: str, px: int) -> float:
        total = 0.0
        for ch in s:
            f = await self.font_for(family, ch, px)
            if f is None:
                continue
            total += f.getlength(ch)
        return total

    async def fit_font(
        self, lines: list[str], w: float, h: float, family: str, max_px: int, min_px: int
    ) -> int:
        # 留边：字形墨迹会超出 advance，贴边就会切字（2026-11-01 修）
        limit = w * 0.92
        for px in range(int(max_px), min_px - 1, -1):
            if len(lines) * metrics(family, px)[2] > h * 0.98:
                continue
            ok = True
            for line in lines:
                if await self.text_width(family, line, px) > limit:
                    ok = False
                    break
            if ok:
                return px
        return min_px

    async def text_block(
        self,
        w: float,
        h: float,
        text: str,
        family: str,
        color: tuple[int, int, int],
        max_px: int = 78,
        min_px: int = 8,
        left_align: bool = True,
        stroke_ratio: float = 0.0,
    ) -> np.ndarray:
        w, h = max(1, round(w)), max(1, round(h))
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        lines = balanced_lines(text, w, h)
        if not lines:
            return np.array(img)
        px = await self.fit_font(lines, w, h, family, max_px, min_px)
        asc, _desc, line_h = metrics(family, px)
        widths = [await self.text_width(family, line, px) for line in lines]
        block_w = max(widths)
        left = left_align and len(lines) > 1
        x0 = (w - block_w) / 2 if left else w / 2
        stroke = round(px * stroke_ratio) if stroke_ratio else 0
        top = (h - len(lines) * line_h) / 2
        for line, width in zip(lines, widths):
            y = top + asc
            x = x0 if left else (w - width) / 2
            for ch in line:
                f = await self.font_for(family, ch, px)
                if f is None:
                    continue
                draw.text(
                    (x, y),
                    ch,
                    font=f,
                    fill=color,
                    anchor="ls",
                    stroke_width=stroke,
                    stroke_fill=color,
                )
                x += f.getlength(ch)
            top += line_h
        return np.array(img)

    async def vertical_strip(
        self, w: float, length: float, text: str, family: str, color: tuple[int, int, int]
    ) -> np.ndarray:
        w, length = max(1, round(w)), max(1, round(length))
        img = Image.new("RGBA", (w, length), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        chars = [c for c in re.sub(r"\s+", "", text)]
        if not chars:
            return np.array(img)
        pitch = length / len(chars)
        px = max(8, int(min(w * 0.92, pitch * 0.86)))
        stroke = max(1, px // 40)
        dy = 0.067 * px if family == "Kai" else 0.0
        for i, ch in enumerate(chars):
            f = await self.font_for(family, ch, px)
            if f is None:
                continue
            y = pitch * i + pitch / 2 - dy
            draw.text(
                (w / 2, y),
                ch,
                font=f,
                fill=color,
                anchor="mm",
                stroke_width=stroke,
                stroke_fill=color,
            )
        return np.array(img)

    # ------------------------------------------------------------- 模板渲染
    async def _masks(self, name: str, w: int, h: int, n: int) -> list[np.ndarray]:
        sheet = await self._png(name)
        out = []
        for i in range(min(n, max(1, sheet.shape[0] // max(1, h)))):
            m = sheet[i * h : (i + 1) * h, :, :].copy()
            m[..., 3] = m[..., 0]
            out.append(m)
        return out

    async def _frame_of(self, meta: dict[str, Any], i: int) -> dict[str, Any]:
        frames = meta.get("frames") or [{}]
        return frames[min(i, len(frames) - 1)]

    async def render(
        self, tpl: str, text: str = "", image: bytes | str | None = None
    ) -> Path:
        tpl = (tpl or "").strip().lower()
        if tpl not in TEMPLATES:
            raise RenderError(f"没有这个模板：{tpl}（可用：{'、'.join(TEMPLATES)}）")
        index = await self._index()
        if not ((index.get("packs") or {}).get(SITE_PACK) or {}).get(tpl):
            raise RenderError(f"官方素材里暂时没有「{TEMPLATE_LABELS.get(tpl, tpl)}」这个模板")

        text = (text or "").strip()
        if tpl == "hug":
            if not image:
                raise RenderError("「抱」需要一张头像图（image 参数给图片地址或本地路径）")
            avatar = image if isinstance(image, bytes) else await self._load_image(image)
            w, h, frames, delays = await self._render_hug(avatar)
        elif tpl == "think":
            w, h, frames, delays = await self._render_think(text or "妈妈")
        elif tpl == "sign":
            w, h, frames, delays = await self._render_sign(text or "今天也要开心哦")
        elif tpl == "scroll":
            w, h, frames, delays = await self._render_scroll(text or "葱卷")
        else:
            default = "恭喜发财" if tpl == "couplet_top" else "万事如意"
            w, h, frames, delays = await self._render_couplet(tpl, text or default)

        out_dir = self.cache_dir / "renders"
        out_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(
            f"{tpl}\x00{text}\x00{len(frames)}".encode("utf-8")
        ).hexdigest()[:16]
        path = out_dir / f"{key}.gif"
        return await asyncio.to_thread(self._write_gif, frames, delays, path)

    @staticmethod
    def _write_gif(frames: list[np.ndarray], delays: list[int], path: Path) -> Path:
        """先写临时文件再原子改名；Windows 上目标被占用时退避重试，最后换名保命。"""
        tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}.part")
        try:
            encode_gif(frames, delays, str(tmp))
            for i in range(10):
                try:
                    os.replace(tmp, path)
                    return path
                except PermissionError:
                    time.sleep(min(0.02 * (2 ** i), 0.5))
            # 同一个 key 的旧图正被发送流程占着：换个名字写出去，别让这条回复失败
            alt = path.with_name(f"{path.stem}-{uuid.uuid4().hex[:6]}.gif")
            os.replace(tmp, alt)
            logger.debug(f"[nekowhale] 原图被占用，改用 {alt.name}")
            return alt
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    async def _load_image(self, src: str) -> bytes:
        src = (src or "").strip()
        if not src:
            raise RenderError("没有拿到头像图")
        if src.startswith("http://") or src.startswith("https://"):
            ok, reason = image_url_allowed(src, getattr(self.fetcher, "site", ""))
            if not ok:
                raise RenderError(
                    f"头像地址不允许：{reason}。只支持 QQ 头像 CDN（qlogo.cn）、"
                    "素材站域名或本地文件路径"
                )
            return await self.fetcher.blob(src)
        p = Path(src).expanduser()
        if not p.is_file():
            raise RenderError(f"头像文件不存在：{src}")
        return await asyncio.to_thread(p.read_bytes)

    async def _render_think(self, text: str) -> tuple[int, int, list[np.ndarray], list[int]]:
        meta = await self._meta_of("think")
        w, h, frames, delays = await self._gif("think.gif")
        masks = await self._masks(meta["mask"], w, h, len(meta["frames"])) if meta.get("mask") else None
        x0, y0, x1, y1 = meta["box"]
        flat = await self.text_block(
            x1 - x0 + 1, y1 - y0 + 1, text, "NotoBlack", (16, 16, 16), max_px=78, min_px=8
        )
        out = []
        for i, fr in enumerate(frames):
            fm = await self._frame_of(meta, i)
            dst = fr.copy()
            layer = np.zeros((h, w, 4), np.uint8)
            dy = int(fm.get("dy") or 0)
            hh, ww = flat.shape[:2]
            layer[y0 + dy : y0 + dy + hh, x0 : x0 + ww] = flat
            if masks:
                m = masks[min(i, len(masks) - 1)]
                layer[..., 3] = (layer[..., 3].astype(np.uint16) * m[..., 3] // 255).astype(np.uint8)
            out.append(composite_over(dst, layer))
        return w, h, out, delays

    async def _render_sign(self, text: str) -> tuple[int, int, list[np.ndarray], list[int]]:
        meta = await self._meta_of("sign")
        w, h, frames, delays = await self._gif("sign.gif")
        flat = await self.text_block(
            meta["W"],
            meta["H"],
            text,
            "Kai",
            (38, 34, 40),
            max_px=90,
            min_px=8,
            stroke_ratio=0.025,
        )
        out = []
        for i, fr in enumerate(frames):
            fm = await self._frame_of(meta, i)
            dst = fr.copy()
            draw_warped(dst, flat, fm["quad"])
            out.append(dst)
        return w, h, out, delays

    async def _render_scroll(self, text: str) -> tuple[int, int, list[np.ndarray], list[int]]:
        meta = await self._meta_of("scroll")
        w, h, frames, delays = await self._gif("scroll.gif")
        masks = await self._masks(meta["mask"], w, h, len(meta["frames"]))
        x0, y0, x1, y1 = meta["wideBox"]
        flat = await self.text_block(
            x1 - x0 + 1, y1 - y0 + 1, text, "NotoBlack", (16, 16, 16), max_px=110, min_px=8
        )
        out = []
        for i, fr in enumerate(frames):
            fm = await self._frame_of(meta, i)
            dst = fr.copy()
            if fm.get("mask"):
                m = masks[min(i, len(masks) - 1)]
                layer = np.zeros((h, w, 4), np.uint8)
                dy = int(fm.get("dy") or 0)
                hh, ww = flat.shape[:2]
                layer[y0 + dy : y0 + dy + hh, x0 : x0 + ww] = flat
                layer[..., 3] = (
                    layer[..., 3].astype(np.uint16) * m[..., 3] // 255
                ).astype(np.uint8)
                dst = composite_over(dst, layer)
            out.append(dst)
        return w, h, out, delays

    async def _render_couplet(
        self, tpl: str, text: str
    ) -> tuple[int, int, list[np.ndarray], list[int]]:
        meta = await self._meta_of(tpl)
        w, h, frames, delays = await self._gif(f"{tpl}.gif")
        strip = await self.vertical_strip(meta["W"], meta["L"], text, "Kai", (28, 22, 22))
        out = []
        for i, fr in enumerate(frames):
            fm = await self._frame_of(meta, i)
            q = fm["quad"]  # TL,BL,BR,TR
            dst = fr.copy()
            draw_warped(dst, strip, [q[0], q[3], q[2], q[1]])
            out.append(dst)
        return w, h, out, delays

    async def _render_hug(self, avatar: bytes) -> tuple[int, int, list[np.ndarray], list[int]]:
        meta = await self._meta_of("hug")
        dia = int(meta["dia"])
        w, h, frames, delays = await self._gif("hug.gif")
        _pw, _ph, paws, _pd = await self._gif(meta["paw"])
        av = await asyncio.to_thread(circular_avatar, avatar, dia)
        out = []
        for i, fr in enumerate(frames):
            fm = await self._frame_of(meta, i)
            dst = fr.copy()
            comp = np.zeros((h, w, 4), np.uint8)
            cx, cy = round(fm["cx"]), round(fm["cy"])
            comp[cy - dia // 2 : cy - dia // 2 + dia, cx - dia // 2 : cx - dia // 2 + dia] = av
            paw = paws[min(i, len(paws) - 1)]
            base = np.dstack([dst[..., :3] * (dst[..., 3:4] / 255.0), dst[..., 3:4]])
            for layer in (comp, paw):
                base = composite_over(base, layer)
            out.append(base)
        return w, h, out, delays


# ---------------------------------------------------------------- 合成算法
def composite_over(dst: np.ndarray, layer: np.ndarray) -> np.ndarray:
    """source-over 合成（dst 在下面）。"""
    a = layer[..., 3:4] / 255.0
    da = dst[..., 3:4] / 255.0
    oa = a + da * (1 - a)
    safe = np.where(oa > 0, oa, 1.0)
    rgb = (layer[..., :3] * a + dst[..., :3] * da * (1 - a)) / safe
    return np.dstack([rgb, oa * 255]).clip(0, 255).astype(np.uint8)


def homography(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> np.ndarray:
    a = np.zeros((8, 9), dtype=np.float64)
    for i in range(4):
        x, y = src[i]
        u, v = dst[i]
        a[2 * i] = [x, y, 1, 0, 0, 0, -u * x, -u * y, u]
        a[2 * i + 1] = [0, 0, 0, x, y, 1, -v * x, -v * y, v]
    sol, *_ = np.linalg.lstsq(a[:, :8], a[:, 8], rcond=None)
    return np.array([*sol, 1.0])


def draw_warped(dst: np.ndarray, src: np.ndarray, quad: list[list[float]]) -> None:
    """把 src 按 quad（TL,TR,BR,BL）透视贴到 dst 上，就地修改 dst。"""
    dh, dw = dst.shape[:2]
    sh, sw = src.shape[:2]
    h_mat = homography(quad, [(0, 0), (sw, 0), (sw, sh), (0, sh)])
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    x0 = max(0, int(np.floor(min(xs))))
    x1 = min(dw - 1, int(np.ceil(max(xs))))
    y0 = max(0, int(np.floor(min(ys))))
    y1 = min(dh - 1, int(np.ceil(max(ys))))
    if x1 < x0 or y1 < y0:
        return
    gy, gx = np.mgrid[y0 : y1 + 1, x0 : x1 + 1]
    px = gx + 0.5
    py = gy + 0.5
    h11, h12, h13, h21, h22, h23, h31, h32, h33 = h_mat
    w = h31 * px + h32 * py + h33
    valid = np.abs(w) > 1e-9
    w = np.where(valid, w, 1.0)
    u = (h11 * px + h12 * py + h13) / w - 0.5
    v = (h21 * px + h22 * py + h23) / w - 0.5
    valid &= (u >= -0.5) & (v >= -0.5) & (u <= sw - 0.5) & (v <= sh - 0.5)
    if not valid.any():
        return
    ux = np.clip(u, 0, sw - 1)
    vy = np.clip(v, 0, sh - 1)
    ix = np.floor(ux).astype(np.int32)
    iy = np.floor(vy).astype(np.int32)
    fx = ux - ix
    fy = vy - iy
    ix1 = np.minimum(sw - 1, ix + 1)
    iy1 = np.minimum(sh - 1, iy + 1)
    s = src.astype(np.float64)
    w00 = (1 - fx) * (1 - fy)
    w10 = fx * (1 - fy)
    w01 = (1 - fx) * fy
    w11 = fx * fy
    p00, p10 = s[iy, ix], s[iy, ix1]
    p01, p11 = s[iy1, ix], s[iy1, ix1]
    a = (p00[..., 3] * w00 + p10[..., 3] * w10 + p01[..., 3] * w01 + p11[..., 3] * w11) / 255.0
    valid &= a > 0.002
    if not valid.any():
        return
    pr = (
        p00[..., 0] * p00[..., 3] * w00
        + p10[..., 0] * p10[..., 3] * w10
        + p01[..., 0] * p01[..., 3] * w01
        + p11[..., 0] * p11[..., 3] * w11
    ) / 255.0
    pg = (
        p00[..., 1] * p00[..., 3] * w00
        + p10[..., 1] * p10[..., 3] * w10
        + p01[..., 1] * p01[..., 3] * w01
        + p11[..., 1] * p11[..., 3] * w11
    ) / 255.0
    pb = (
        p00[..., 2] * p00[..., 3] * w00
        + p10[..., 2] * p10[..., 3] * w10
        + p01[..., 2] * p01[..., 3] * w01
        + p11[..., 2] * p11[..., 3] * w11
    ) / 255.0
    d = dst[y0 : y1 + 1, x0 : x1 + 1].astype(np.float64)
    da = d[..., 3] / 255.0
    out_a = a + da * (1 - a)
    safe = np.where(out_a > 0, out_a, 1.0)
    for c, pc in enumerate((pr, pg, pb)):
        d[..., c] = (pc + d[..., c] * da * (1 - a)) / safe
    d[..., 3] = out_a * 255
    d[~valid] = dst[y0 : y1 + 1, x0 : x1 + 1][~valid]
    dst[y0 : y1 + 1, x0 : x1 + 1] = np.clip(d, 0, 255).astype(np.uint8)


def circular_avatar(data: bytes, dia: int) -> np.ndarray:
    src = Image.open(io.BytesIO(data)).convert("RGBA")
    sw, sh = src.size
    s = min(sw, sh)
    left, top = (sw - s) // 2, (sh - s) // 2
    box = src.crop((left, top, left + s, top + s)).resize((dia * 2, dia * 2), Image.LANCZOS)
    mask = Image.new("L", (dia * 2, dia * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, dia * 2 - 1, dia * 2 - 1), fill=255)
    big = Image.new("RGBA", (dia * 2, dia * 2), (0, 0, 0, 0))
    big.paste(box, (0, 0), mask)
    return np.array(big.resize((dia, dia), Image.LANCZOS))


# ------------------------------------------------------------------- 编解码
def decode_gif(data: bytes) -> tuple[int, int, list[np.ndarray], list[int]]:
    im = Image.open(io.BytesIO(data))
    frames, delays = [], []
    for i in range(getattr(im, "n_frames", 1)):
        im.seek(i)
        frames.append(np.array(im.convert("RGBA")))
        delays.append(im.info.get("duration", 60) or 60)
    return im.size[0], im.size[1], frames, delays


def encode_gif(frames: list[np.ndarray], delays: list[int], path: str) -> None:
    """全帧共用一份调色板（逐帧各算会让静止部分逐帧换色、看起来在闪）。"""
    images = [Image.fromarray(f, "RGBA") for f in frames]
    biggest = max(images, key=lambda im: im.size[0] * im.size[1])
    sample = Image.new("RGB", biggest.size, (255, 255, 255))
    sample.paste(biggest, (0, 0), biggest)
    palette = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    transparent = 255
    entries = palette.getpalette() or []
    entries = (entries + [0, 0, 0] * 256)[:768]
    palette.putpalette(entries[:765] + [0, 0, 0])

    out = []
    for i, rgba in enumerate(images):
        alpha = frames[i][..., 3]
        idx = np.array(
            rgba.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE)
        )
        idx = np.where(alpha >= 128, idx, transparent).astype(np.uint8)
        q = Image.fromarray(idx, "P")
        q.putpalette(palette.getpalette())
        out.append(q)
    out[0].save(
        path,
        format="GIF",  # 显式指定：临时文件名是 .part，PIL 靠后缀猜格式会猜错
        save_all=True,
        append_images=out[1:],
        duration=delays,
        loop=0,
        disposal=2,
        transparency=transparent,
        optimize=False,
    )


# ------------------------------------------------------------------- 字体
def woff2_to_ttf(data: bytes) -> bytes:
    """woff2 → ttf。需要 fontTools（带 brotli）。装不上就抛给调用方退回系统字体。"""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(data))
    font.flavor = None
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()

