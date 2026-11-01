"""素材按需下载。

插件本身**不含任何表情素材**（表情 GIF 版权归作者，且 322 张共 388MB，塞不进插件包）。
所有资源在第一次用到时从官方站 kiriyuki.com 现取，取到本地缓存后用缓存：

    manifest  /api/kit/nekowhale        172KB（gzip 34KB）  清单：有哪些图、多大、什么哈希
    meta      /meme/kit/nekowhale.json  160KB               标注：情绪 / 标签 / 用法 / 画面描述
    gif       /media/gif/<包>/<文件>?h=<hash>               单张表情，0.1~5.5MB
    meme      /meme/index.json + /meme/assets/<哈希>.gif    贴字渲染用的模板底图

设计约束（都是踩过的）：
* **不做镜像兜底**：素材只从官方站拉。自己镜像一份等于把版权责任扛到自己身上，
  也和「非商业、保留作者链接」的许可条款越走越远。
* **校验哈希**：清单里每张图带 `hash`（sha1 前 12 位），下载后必须对得上，
  否则删掉重下一次；对不上两次就报错，绝不把半截图塞进聊天。
* **先落 .part 再改名**：下载中途被杀不会留下半截文件被当成缓存。
* **只用标准库**：urllib + gzip，不引入 httpx/aiohttp —— 用户的 AstrBot 环境里
  不一定有哪个，装依赖失败会导致整个插件加载失败。
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # 独立跑（不带 AstrBot）时退化到标准 logging
    import logging

    logger = logging.getLogger("nekowhale")

PLUGIN_VERSION = "2.3.1"
USER_AGENT = f"astrbot-plugin-nekowhale-stickers/{PLUGIN_VERSION} (+https://kiriyuki.com)"

KIT_MANIFEST = "/api/kit/nekowhale"
KIT_META = "/meme/kit/nekowhale.json"
MEME_INDEX = "/meme/index.json"

# 清单/标注的保鲜期：清单每天对一次（带 ETag，没事就是 304），标注一周一次
MANIFEST_TTL = 86400
META_TTL = 7 * 86400
DEFAULT_TIMEOUT = 40.0


class FetchError(RuntimeError):
    """下载失败（网络不通、404、哈希对不上）。调用方应降级而不是崩。"""


def _sha1_prefix(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


def _replace(src: Path, dst: Path, tries: int = 10) -> None:
    """原子替换，且对 Windows 的文件占用做退避重试。

    Windows 上 os.replace 只要目标文件被任何进程（哪怕只是读）打开着，
    就会抛 PermissionError [WinError 5]；而机器人发图时正好会短暂打开同一个
    gif。Linux 上是原子的、不会出现这种情况，所以这里统一兜一层。
    """
    last: Exception | None = None
    for i in range(max(1, tries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # 目标被占用：等一会儿再来
            last = e
            time.sleep(min(0.02 * (2 ** i), 0.5))
    raise FetchError(f"写入 {dst.name} 失败（文件正被其它程序占用）：{last}")


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


class KitFetcher:
    """官方素材的本地缓存与按需下载器。"""

    def __init__(
        self,
        data_dir: str | Path,
        site: str = "https://kiriyuki.com",
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = 3,
    ) -> None:
        self.dir = Path(data_dir).expanduser()
        self.site = (site or "https://kiriyuki.com").rstrip("/")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self._sem = threading.Semaphore(max(1, int(concurrency or 3)))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()
        self._state_file = self.dir / "kit-state.json"
        self._state: dict[str, Any] = self._read_state()
        self._manifest: dict[str, Any] | None = None
        self._meta: dict[str, Any] | None = None
        self.last_error = ""
        # 统计（/表情自检 里展示）
        self.downloaded = 0
        self.downloaded_bytes = 0

    # ------------------------------------------------------------------ state
    def _read_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _write_state(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".json.part")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            _replace(tmp, self._state_file)
        except Exception as e:
            logger.debug(f"[nekowhale] 写状态文件失败：{e}")

    def _lock(self, key: str) -> asyncio.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    # ------------------------------------------------------------------ http
    def _get_sync(self, url: str, etag: str = "") -> tuple[int, bytes, str]:
        """返回 (status, body, etag)。304 时 body 为空。"""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, */*",
            "Accept-Encoding": "gzip",
        }
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(url, headers=headers)
        with self._sem:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    if "gzip" in enc:
                        raw = gzip.decompress(raw)
                    new_etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
                    return int(resp.status), raw, new_etag
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, b"", etag
                raise FetchError(f"HTTP {e.code} {url}") from e
            except urllib.error.URLError as e:
                raise FetchError(f"连不上 {url}：{getattr(e, 'reason', e)}") from e
            except TimeoutError as e:
                raise FetchError(f"超时 {url}") from e

    async def _get(self, url: str, etag: str = "") -> tuple[int, bytes, str]:
        return await asyncio.to_thread(self._get_sync, url, etag)

    def _url(self, rel: str) -> str:
        if rel.startswith("http://") or rel.startswith("https://"):
            return rel
        if not rel.startswith("/"):
            rel = "/" + rel
        return self.site + rel

    # -------------------------------------------------------------- manifest
    def _manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    async def manifest(self, force: bool = False) -> dict[str, Any]:
        """清单。首次会下载；之后按 TTL 用 ETag 对一次。失败时退回磁盘缓存。"""
        if self._manifest is not None and not force:
            return self._manifest

        path = self._manifest_path()
        cached: dict[str, Any] | None = None
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                cached = None

        age = time.time() - float(self._state.get("manifest_at") or 0)
        fresh = cached is not None and age < MANIFEST_TTL
        if fresh and not force:
            self._manifest = cached
            return cached

        etag = str(self._state.get("manifest_etag") or "")
        if force:
            etag = ""
        try:
            status, body, new_etag = await self._get(self._url(KIT_MANIFEST), etag)
            if status == 304 and cached is not None:
                self._state["manifest_at"] = time.time()
                self._write_state()
                self._manifest = cached
                return cached
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict) or not data.get("items"):
                raise FetchError("清单格式不对（没有 items）")
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            _replace(tmp, path)
            self._state["manifest_etag"] = new_etag
            self._state["manifest_at"] = time.time()
            self._state["manifest_version"] = data.get("version")
            self._write_state()
            logger.info(
                f"[nekowhale] 表情清单已更新：{len(data.get('items') or [])} 张 / "
                f"版本 {data.get('version')}"
            )
            self._manifest = data
            return data
        except Exception as e:
            self.last_error = str(e)
            if cached is not None:
                logger.warning(f"[nekowhale] 清单刷新失败（继续用本地缓存）：{e}")
                self._manifest = cached
                return cached
            raise FetchError(
                f"拿不到表情清单（{e}）。插件需要能访问 {self.site}；"
                "离线环境请手动把官方 manifest.json 放进数据目录。"
            ) from e

    # ------------------------------------------------------------------ meta
    def _meta_path(self) -> Path:
        return self.dir / "meta" / "nekowhale.json"

    async def meta(self, force: bool = False) -> dict[str, Any]:
        """标注（情绪/标签/用法）。没有标注也能跑，只是选图会退化成按名字猜。"""
        if self._meta is not None and not force:
            return self._meta
        path = self._meta_path()
        cached: dict[str, Any] | None = None
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                cached = None
        age = time.time() - float(self._state.get("meta_at") or 0)
        if cached is not None and age < META_TTL and not force:
            self._meta = cached
            return cached

        etag = "" if force else str(self._state.get("meta_etag") or "")
        try:
            status, body, new_etag = await self._get(self._url(KIT_META), etag)
            if status == 304 and cached is not None:
                self._state["meta_at"] = time.time()
                self._write_state()
                self._meta = cached
                return cached
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict) or not data.get("stickers"):
                raise FetchError("标注格式不对（没有 stickers）")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.part")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            _replace(tmp, path)
            self._state["meta_etag"] = new_etag
            self._state["meta_at"] = time.time()
            self._write_state()
            logger.info(f"[nekowhale] 表情标注已更新：{len(data.get('stickers') or [])} 条")
            self._meta = data
            return data
        except Exception as e:
            self.last_error = str(e)
            if cached is not None:
                logger.warning(f"[nekowhale] 标注刷新失败（继续用本地缓存）：{e}")
                self._meta = cached
                return cached
            logger.warning(f"[nekowhale] 拿不到表情标注，选图退化为按文件名匹配：{e}")
            self._meta = {"stickers": []}
            return self._meta

    # ------------------------------------------------------------------- gif
    def sticker_path(self, item: dict[str, Any]) -> Path:
        return self.dir / "packs" / str(item["pack"]) / str(item["file"])

    def sticker_url(self, item: dict[str, Any]) -> str:
        gif_base = (self._manifest or {}).get("gif_base") or "/media/gif"
        pack = urllib.parse.quote(str(item["pack"]), safe="")
        name = urllib.parse.quote(str(item["file"]), safe="")
        return f"{self.site}{gif_base}/{pack}/{name}?h={item.get('hash') or ''}"

    def sticker_ok(self, item: dict[str, Any]) -> bool:
        path = self.sticker_path(item)
        try:
            if not path.is_file():
                return False
            expect = int(item.get("bytes") or 0)
            return not expect or path.stat().st_size == expect
        except OSError:
            return False

    @staticmethod
    def _write_atomic(path: Path, body: bytes, tmp: Path | None = None) -> None:
        """写临时文件再改名（放到线程里跑，别卡事件循环）。"""
        tmp = tmp or path.with_suffix(path.suffix + ".part")
        try:
            tmp.write_bytes(body)
            _replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    async def ensure_sticker(self, item: dict[str, Any], verify: bool = True) -> Path:
        """确保这张表情在本地，返回本地路径。并发调用同一张只会下载一次。"""
        path = self.sticker_path(item)
        if self.sticker_ok(item):
            return path
        async with self._lock(f"sticker:{item.get('id')}"):
            if self.sticker_ok(item):
                return path
            if self._manifest is None:
                await self.manifest()
            url = self.sticker_url(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            last: Exception | None = None
            for attempt in range(2):
                try:
                    status, body, _ = await self._get(url)
                    expect = str(item.get("hash") or "")
                    if verify and expect and _sha1_prefix(body) != expect:
                        raise FetchError(f"哈希对不上（{item.get('id')}）")
                    tmp = path.with_suffix(path.suffix + ".part")
                    await asyncio.to_thread(self._write_atomic, path, body, tmp)
                    self.downloaded += 1
                    self.downloaded_bytes += len(body)
                    logger.info(
                        f"[nekowhale] 下载 {item.get('id')} {_human(len(body))}"
                        + (f"（第 {attempt + 1} 次）" if attempt else "")
                    )
                    return path
                except Exception as e:
                    last = e
                    await asyncio.sleep(0.6 * (attempt + 1))
            raise FetchError(f"表情下载失败 {item.get('id')}：{last}")

    # ------------------------------------------------------------ meme 资源
    def _asset_cache(self, url: str) -> Path:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.dir / "meme_cache" / key[:2] / key

    async def blob(self, url: str, max_bytes: int = 12_000_000) -> bytes:
        """取任意官方资源（meme 模板、字体分片），按 URL 缓存。"""
        path = self._asset_cache(self._url(url))
        if path.is_file():
            try:
                data = path.read_bytes()
                if data:
                    return data
            except OSError:
                pass
        async with self._lock(f"blob:{path.name}"):
            if path.is_file():
                try:
                    return path.read_bytes()
                except OSError:
                    pass
            status, body, _ = await self._get(self._url(url))
            if len(body) > max_bytes:
                raise FetchError(f"资源过大（{_human(len(body))}）：{url}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(body)
            _replace(tmp, path)
            return body

    async def json_of(self, rel: str) -> dict[str, Any]:
        return json.loads((await self.blob(rel)).decode("utf-8"))

    # ----------------------------------------------------------------- 统计
    def stats(self) -> dict[str, Any]:
        packs_dir = self.dir / "packs"
        files = 0
        total = 0
        if packs_dir.is_dir():
            for p in packs_dir.rglob("*.gif"):
                try:
                    files += 1
                    total += p.stat().st_size
                except OSError:
                    pass
        return {
            "dir": str(self.dir),
            "local_files": files,
            "local_mb": round(total / 1048576, 1),
            "downloaded": self.downloaded,
            "downloaded_mb": round(self.downloaded_bytes / 1048576, 1),
            "manifest_version": self._state.get("manifest_version"),
            "site": self.site,
            "last_error": self.last_error,
        }
