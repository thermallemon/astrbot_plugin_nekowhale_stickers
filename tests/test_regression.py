"""标准库回归：从真实源码 AST 提取核心函数，不加载 AstrBot。

运行：python -m unittest discover -s _plugins/astrbot_plugin_nekowhale_stickers/tests -v
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def _ensure_astrbot_logger_stub() -> None:
    """fetcher / render / semantic 只从 astrbot.api 取 logger。

    本机（以及 CI）没装 AstrBot 时在这里补一个最小桩，独立脚本想脱离框架
    加载这些模块也必须先提供同样的 ``astrbot.api.logger``。
    """
    try:
        from astrbot.api import logger  # noqa: F401
        return
    except ImportError:
        pass
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("nekowhale-tests")
    pkg.api = api
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api


_ensure_astrbot_logger_stub()


class _Logger:
    def __init__(self):
        self.lines = []

    def debug(self, message):
        self.lines.append(("debug", message))

    def info(self, message):
        self.lines.append(("info", message))

    def warning(self, message):
        self.lines.append(("warning", message))

    def exception(self, message):
        self.lines.append(("exception", message))


LOGGER = _Logger()


class _Image:
    @staticmethod
    def fromFileSystem(path):
        return ("image", path)


class _MessageChain(list):
    pass


def _extract_main_functions(*wanted, overrides=None):
    """Compile only real methods, retaining source line numbers and logic."""
    tree = ast.parse(MAIN_SOURCE, filename=str(ROOT / "main.py"))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NekoWhaleStickers"
    )
    found = {}
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            node = ast.fix_missing_locations(node)
            node.decorator_list = []
            found[node.name] = node
    missing = set(wanted) - set(found)
    if missing:
        raise AssertionError(f"missing methods in main.py: {sorted(missing)}")
    namespace = {
        "Any": Any,
        "asyncio": asyncio,
        "random": random,
        "time": __import__("time"),
        "logger": LOGGER,
        "Image": _Image,
        "MessageChain": _MessageChain,
        "Path": Path,
        "json": json,
        "shutil": shutil,
        "EXTRA_SENT": "_nekowhale_already_sent",
        "EXTRA_PICKED": "_nekowhale_picked",
        "EXTRA_CTX": "_nekowhale_ctx",
    }
    if overrides:
        namespace.update(overrides)
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.Module(body=[future, *found.values()], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(ROOT / "main.py"), "exec"), namespace)
    return namespace


_NS = _extract_main_functions(
    "_should_skip",
    "_roll",
    "_cooldown_count",
    "_cooldown_blocked",
    "_choose",
    "_deliver",
    "_tg_send",
    "_after_sent_impl",
)


class _Event:
    def __init__(self, umo="group:test"):
        self.unified_msg_origin = umo
        self.extras = {}
        self.stopped = False
        self.send_calls = []
        self.send_error = None
        self.result = None

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def is_stopped(self):
        return self.stopped

    async def send(self, chain):
        self.send_calls.append(chain)
        if self.send_error:
            raise self.send_error


class _Result:
    def __init__(self):
        self.chain = []

    def is_llm_result(self):
        return True


class _Kit:
    def __init__(self, path="C:/tmp/sticker.gif", error=None):
        self.path = Path(path)
        self.error = error
        self.calls = 0

    async def ensure_sticker(self, item):
        self.calls += 1
        if self.error:
            raise self.error
        return self.path


class _Picker:
    def __init__(self, ready=True, result=(None, {"stage": "no-candidates", "sim": 0.0})):
        self.ready = ready
        self.result = result
        self.calls = 0

    async def pick(self, *args, **kwargs):
        self.calls += 1
        return self.result


class _Index:
    def __init__(self, item=None, items=None):
        self.item = item
        self.items = list(items if items is not None else ([item] if item else []))
        self.calls = 0

    def pick(self, **kwargs):
        self.calls += 1
        return self.item


class _Context:
    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if self.error:
            raise self.error
        return self.result


class _TgBridge:
    """TgStickerBridge 的最小替身：默认「不走 TG」，保持 QQ 原有断言不变。

    result 三态跟真实桥一致：None = 没走原生贴纸（回退原 Image 路径）；
    True = Telegram 明确发成功；False = 已经尝试发过但失败（不许再补发）。
    """

    def __init__(self, result=None, error=None, enabled=True):
        self.result = result
        self.error = error
        self.enabled = enabled
        self.last_error = ""
        self.calls = []

    async def send(self, event, path):
        self.calls.append((event, path))
        if self.error:
            raise self.error
        return self.result

    def stats(self):
        return {"enabled": self.enabled, "cache_dir": "", "cache_hits": 0, "converted": 0,
                "max_bytes": 0, "last_error": self.last_error}


class _Subject:
    """Small object exposing only collaborators used by extracted methods."""

    def __init__(self, cfg=None, *, items=None, picker=None, kit=None, context=None, tg=None):
        self.cfg = dict(cfg or {})
        self._cooldown = {}
        self._cooldown_remain = 0
        self._skip_reason = "无"
        self._loaded = True
        self._load_error = ""
        self._index = _Index(items=items or [])
        self._picker = picker or _Picker()
        self._kit = kit or _Kit()
        self.context = context or _Context()
        self._tg = tg or _TgBridge()
        self._recent = defaultdict(list)
        self._vector_task = None
        self._build_attempted = True

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _selector(self):
        return self.cfg.get("selector", "auto")

    def _cooldown_count(self):
        return _NS["_cooldown_count"](self)

    def _cooldown_blocked(self, umo):
        return _NS["_cooldown_blocked"](self, umo)

    def _should_skip(self, event):
        return _NS["_should_skip"](self, event)

    def _roll(self):
        return _NS["_roll"](self)

    async def _choose(self, *args):
        return await _NS["_choose"](self, *args)

    async def _deliver(self, event, item):
        return await _NS["_deliver"](self, event, item)

    async def _tg_send(self, event, path):
        return await _NS["_tg_send"](self, event, path)

    async def _after_sent_impl(self, event):
        return await _NS["_after_sent_impl"](self, event)

    def _mode(self):
        return self.cfg.get("mode", "after_send")

    def _log_pick(self, *args):
        self.logged_pick = args


class CoreDecisionTests(unittest.TestCase):
    def test_should_skip_empty_index_and_state_boundaries(self):
        subject = _Subject(items=[])
        event = _Event()
        self.assertTrue(subject._should_skip(event))
        self.assertEqual(subject._skip_reason, "表情库为空")

        subject = _Subject(cfg={"enabled": False}, items=[{"id": "1"}])
        self.assertTrue(subject._should_skip(event))
        self.assertEqual(subject._skip_reason, "已关闭")

        subject = _Subject(items=[{"id": "1"}])
        subject._loaded = False
        self.assertTrue(subject._should_skip(event))
        subject._loaded = True
        event.set_extra("_nekowhale_already_sent", True)
        self.assertTrue(subject._should_skip(event))
        event.extras.clear()
        event.stopped = True
        self.assertTrue(subject._should_skip(event))

    def test_should_skip_uses_exact_umo_skip_list(self):
        subject = _Subject(cfg={"skip_umos": ["group:blocked"]}, items=[{"id": "1"}])
        self.assertTrue(subject._should_skip(_Event("group:blocked")))
        self.assertFalse(subject._should_skip(_Event("group:other")))

    def test_roll_clamps_invalid_and_boundary_probability(self):
        for value in (None, "bad", True):
            with self.subTest(value=value):
                subject = _Subject(cfg={"probability": value})
                self.assertTrue(subject._roll())
        self.assertFalse(_Subject(cfg={"probability": 0})._roll())
        self.assertTrue(_Subject(cfg={"probability": 1})._roll())

    def test_cooldown_counts_down_and_isolated_by_umo(self):
        subject = _Subject(cfg={"cooldown": 2}, items=[{"id": "1"}])
        subject._cooldown["A"] = 2
        self.assertTrue(subject._cooldown_blocked("A"))
        self.assertEqual(subject._cooldown_remain, 1)
        self.assertTrue(subject._cooldown_blocked("A"))
        self.assertFalse(subject._cooldown_blocked("A"))
        self.assertFalse(subject._cooldown_blocked("B"))

    def test_choose_semantic_fallback_and_empty_index(self):
        item = {"id": "semantic"}
        keyword = {"id": "keyword"}
        subject = _Subject(
            cfg={"selector": "auto", "fallback_to_keyword": True},
            items=[keyword],
            picker=_Picker(result=(None, {"stage": "no-candidates", "sim": 0.0})),
        )
        subject._index.item = keyword
        got, info = asyncio.run(subject._choose("g", "u", "r"))
        self.assertIs(got, keyword)
        self.assertEqual(info["stage"], "no-candidates→关键词兜底")

        subject = _Subject(items=[])
        subject._index.item = None
        subject._picker = _Picker(result=(None, {"stage": "empty-query", "sim": 0.0}))
        got, _ = asyncio.run(subject._choose("g", "", ""))
        self.assertIsNone(got)

        subject = _Subject(
            cfg={"selector": "auto", "fallback_to_keyword": False},
            items=[item],
            picker=_Picker(ready=False),
        )
        got, info = asyncio.run(subject._choose("g", "u", "r"))
        self.assertIsNone(got)
        self.assertEqual(info["stage"], "semantic-not-ready")


class DeliveryAndStateTests(unittest.TestCase):
    def test_deliver_uses_event_then_context_fallback(self):
        item = {"id": "1"}
        event = _Event()
        subject = _Subject(kit=_Kit())
        self.assertTrue(asyncio.run(subject._deliver(event, item)))
        self.assertEqual(len(event.send_calls), 1)
        self.assertEqual(len(subject.context.calls), 0)

        event = _Event()
        event.send_error = RuntimeError("event send failed")
        context = _Context(result=True)
        subject = _Subject(kit=_Kit(), context=context)
        self.assertTrue(asyncio.run(subject._deliver(event, item)))
        self.assertEqual(len(context.calls), 1)

    def test_deliver_reports_download_and_both_send_failures(self):
        item = {"id": "1"}
        event = _Event()
        subject = _Subject(kit=_Kit(error=RuntimeError("download failed")))
        self.assertFalse(asyncio.run(subject._deliver(event, item)))
        self.assertIn("下载失败", subject._skip_reason)

        event = _Event()
        event.send_error = RuntimeError("event send failed")
        subject = _Subject(
            kit=_Kit(), context=_Context(result=False, error=RuntimeError("context failed"))
        )
        self.assertFalse(asyncio.run(subject._deliver(event, item)))

    def test_after_sent_marks_picked_only_after_success_and_clears_ctx(self):
        item = {"id": "1", "bytes": 100, "emotion": "开心"}
        event = _Event("group:state")
        event.set_extra("_nekowhale_ctx", {"user_text": "u", "reply_text": "r"})
        subject = _Subject(items=[item], kit=_Kit(), cfg={"cooldown": 1})
        subject._index.item = item
        subject._picker = _Picker(result=(item, {"stage": "keyword", "sim": 0.0}))

        asyncio.run(subject._after_sent_impl(event))
        self.assertIsNone(event.get_extra("_nekowhale_ctx"))
        self.assertTrue(event.get_extra("_nekowhale_picked"))
        self.assertEqual(subject._recent["group:state"], ["1"])
        self.assertEqual(subject._cooldown["group:state"], 1)

        event = _Event("group:failed")
        event.set_extra("_nekowhale_ctx", {"user_text": "u", "reply_text": "r"})
        subject = _Subject(items=[item], kit=_Kit(error=RuntimeError("no network")))
        subject._index.item = item
        subject._picker = _Picker(result=(item, {"stage": "keyword", "sim": 0.0}))
        asyncio.run(subject._after_sent_impl(event))
        self.assertFalse(event.get_extra("_nekowhale_picked"))
        self.assertEqual(subject._recent["group:failed"], [])


class FetcherCacheRegressionTests(unittest.TestCase):
    @staticmethod
    def _load_fetcher():
        path = ROOT / "fetcher.py"
        spec = importlib.util.spec_from_file_location("nw_fetcher_for_tests", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_blob_empty_cached_file_is_redownloaded(self):
        fetcher = self._load_fetcher()
        with tempfile.TemporaryDirectory() as temp:
            obj = fetcher.KitFetcher(temp, site="https://example.test")
            cached = obj._asset_cache(obj._url("/meme/a.bin"))
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"")
            calls = []

            async def fake_get(url):
                calls.append(url)
                return 200, b"fresh", ""

            obj._get = fake_get
            got = asyncio.run(obj.blob("/meme/a.bin"))
            self.assertEqual(got, b"fresh")
            self.assertEqual(len(calls), 1)

    def test_blob_does_not_serve_cached_file_over_max_bytes(self):
        fetcher = self._load_fetcher()
        with tempfile.TemporaryDirectory() as temp:
            obj = fetcher.KitFetcher(temp, site="https://example.test")
            cached = obj._asset_cache(obj._url("/meme/large.bin"))
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"123456")
            calls = []

            async def fake_get(url):
                calls.append(url)
                return 200, b"ok", ""

            obj._get = fake_get
            got = asyncio.run(obj.blob("/meme/large.bin", max_bytes=3))
            self.assertEqual(got, b"ok")
            self.assertEqual(len(calls), 1)

    def test_sticker_paths_preserve_unicode_and_platform_separators(self):
        fetcher = self._load_fetcher()
        with tempfile.TemporaryDirectory() as temp:
            obj = fetcher.KitFetcher(temp)
            path = obj.sticker_path({"pack": "大肥鱼", "file": "测试.gif"})
            self.assertEqual(path, Path(temp) / "packs" / "大肥鱼" / "测试.gif")
            # Path joins are platform-native; no POSIX separator is required on Windows.
            self.assertEqual(path.name, "测试.gif")
            self.assertEqual(path.parent.name, "大肥鱼")
            self.assertEqual(path.parent.parent.name, "packs")


class DataDirMigrationTests(unittest.TestCase):
    """审查建议 2：老版 /tmp 素材一次性迁进 plugin_data，失败不丢库。"""

    def _fixture(self, root: Path):
        plugin_root = root / "plugin_data" / "astrbot_plugin_nekowhale_stickers"
        plugin_root.mkdir(parents=True, exist_ok=True)
        temp_root = root / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)

        class _StarTools:
            @staticmethod
            def get_data_dir(name):
                return plugin_root

        class _Temp:
            @staticmethod
            def gettempdir():
                return str(temp_root)

        namespace = _extract_main_functions(
            "_resolve_data_dir",
            "_migrate_legacy_data_dir",
            overrides={
                "StarTools": _StarTools,
                "tempfile": _Temp,
                "PLUGIN_NAME": "astrbot_plugin_nekowhale_stickers",
            },
        )
        return (
            namespace,
            plugin_root / "nekowhale-stickers",
            temp_root / ".astrbot" / "nekowhale-stickers",
        )

    @staticmethod
    def _subject(namespace):
        class _Subject:
            def _cfg(self, key, default=None):
                return default

            def _migrate_legacy_data_dir(self, legacy, target):
                return namespace["_migrate_legacy_data_dir"](legacy, target)

        return _Subject()

    def test_fresh_install_uses_plugin_data_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            namespace, target, legacy = self._fixture(Path(temp))
            got = namespace["_resolve_data_dir"](self._subject(namespace))
            self.assertEqual(got, target)
            self.assertFalse(legacy.exists())

    def test_existing_plugin_data_wins_without_touching_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            namespace, target, legacy = self._fixture(Path(temp))
            target.mkdir(parents=True)
            (target / "manifest.json").write_text("{}", encoding="utf-8")
            legacy.mkdir(parents=True)
            (legacy / "manifest.json").write_text("{}", encoding="utf-8")
            got = namespace["_resolve_data_dir"](self._subject(namespace))
            self.assertEqual(got, target)
            self.assertTrue((legacy / "manifest.json").is_file())

    def test_legacy_tmp_dir_is_migrated_then_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            namespace, target, legacy = self._fixture(Path(temp))
            (legacy / "packs").mkdir(parents=True)
            (legacy / "manifest.json").write_text('{"version": 1}', encoding="utf-8")
            (legacy / "packs" / "测试.gif").write_bytes(b"GIF89a")
            got = namespace["_resolve_data_dir"](self._subject(namespace))
            self.assertEqual(got, target)
            self.assertEqual((target / "manifest.json").read_text(encoding="utf-8"), '{"version": 1}')
            self.assertEqual((target / "packs" / "测试.gif").read_bytes(), b"GIF89a")
            self.assertFalse(legacy.exists())
            marker = json.loads((target / "migrated-from-tmp.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["from"], str(legacy))
            self.assertFalse(target.with_name(target.name + ".migrating").exists())

    def test_migration_failure_keeps_legacy_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            namespace, target, legacy = self._fixture(Path(temp))
            legacy.mkdir(parents=True)
            (legacy / "manifest.json").write_text("{}", encoding="utf-8")
            staging = target.with_name(target.name + ".migrating")
            staging.write_text("占住 staging 路径，让 copytree 失败", encoding="utf-8")
            got = namespace["_resolve_data_dir"](self._subject(namespace))
            self.assertEqual(got, legacy)
            self.assertTrue((legacy / "manifest.json").is_file())
            self.assertFalse(target.exists())


class ImageUrlWhitelistTests(unittest.TestCase):
    """审查建议 3：贴字表情只从 QQ 头像 CDN / 素材站取图，其余明确拒绝。"""

    @staticmethod
    def _render_module():
        spec = importlib.util.spec_from_file_location(
            "nw_render_whitelist_test", ROOT / "render.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_allows_qq_avatar_and_site_domains(self):
        render = self._render_module()
        site = "https://kiriyuki.com"
        for url in (
            "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640",
            "http://qlogo.cn/avatar.png",
            "https://kiriyuki.com/meme/a.gif",
            "https://cdn.kiriyuki.com/meme/a.gif",
        ):
            ok, reason = render.image_url_allowed(url, site)
            self.assertTrue(ok, f"{url} 应该放行，却被拒绝：{reason}")

    def test_rejects_other_and_lookalike_domains(self):
        render = self._render_module()
        site = "https://kiriyuki.com"
        for url in (
            "https://example.com/a.gif",
            "https://evil-kiriyuki.com/a.gif",
            "https://kiriyuki.com.evil.com/a.gif",
            "https://qlogo.cn.evil.com/a.gif",
            "ftp://qlogo.cn/a.gif",
        ):
            ok, reason = render.image_url_allowed(url, site)
            self.assertFalse(ok, f"{url} 应该拒绝")
            self.assertTrue(reason)

    def test_load_image_checks_whitelist_before_download(self):
        render = self._render_module()

        class _Fetcher:
            site = "https://kiriyuki.com"
            calls = []

            async def blob(self, url):
                self.calls.append(url)
                return b"avatar"

        fetcher = _Fetcher()
        with tempfile.TemporaryDirectory() as temp:
            renderer = render.MemeRenderer(fetcher, temp)
            got = asyncio.run(
                renderer._load_image("https://q1.qlogo.cn/g?b=qq&nk=10001&s=640")
            )
            self.assertEqual(got, b"avatar")
            with self.assertRaises(render.RenderError) as caught:
                asyncio.run(renderer._load_image("https://example.com/a.gif"))
        self.assertIn("不允许", str(caught.exception))
        self.assertEqual(
            fetcher.calls, ["https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"]
        )


class LoggerImportRegressionTests(unittest.TestCase):
    """审查建议 1：三个模块只从 astrbot.api 取 logger，不再自带 logging 兜底。"""

    MODULES = ("fetcher.py", "semantic.py", "render.py")

    def test_modules_only_import_logger_from_astrbot(self):
        for name in self.MODULES:
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("from astrbot.api import logger", source, name)
            self.assertNotIn("import logging", source, name)
            self.assertNotIn("getLogger", source, name)

    def test_modules_load_against_explicit_stub(self):
        logger = sys.modules["astrbot.api"].logger
        for name in self.MODULES:
            spec = importlib.util.spec_from_file_location(
                f"nw_{Path(name).stem}_import_test", ROOT / name
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            self.assertIs(module.logger, logger, name)


# --------------------------------------------------------------------- TG
_NS_TOOLS = _extract_main_functions(
    "_tg_send",
    "_inline",
    "tool_send",
    "tool_render",
    overrides={
        # 只用到名字映射，不必真去 import render.py 的模板表
        "TEMPLATE_LABELS": {"think": "想", "sign": "举牌"},
        "RenderError": type("RenderError", (Exception,), {}),
    },
)


def _load_tgsticker():
    spec = importlib.util.spec_from_file_location("nw_tgsticker_for_tests", ROOT / "tgsticker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TG = _load_tgsticker()


class _TgClient:
    """假装成 AstrBot 平台实例上的 PTB ExtBot：只提供 send_sticker。"""

    def __init__(self, error=None, delay=0.0):
        self.error = error
        self.delay = delay
        self.calls = []

    async def send_sticker(self, chat_id=None, sticker=None):
        self.calls.append(
            {
                "chat_id": chat_id,
                "name": getattr(sticker, "name", ""),
                "readable": hasattr(sticker, "read"),
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {"message_id": 1}


class _TgEvent:
    """最小事件桩：能给出平台名、client 和 chat_id。"""

    def __init__(self, platform="telegram", chat_id=123456, client=..., umo="telegram:group:1"):
        self._platform = platform
        self.chat_id = chat_id
        self.unified_msg_origin = umo
        self.get_sender_id = lambda: "9"
        if client is not ...:
            self.client = client

    def get_platform_name(self):
        return self._platform

    def get_group_id(self):
        return str(self.chat_id)


class TgRouteTests(unittest.TestCase):
    """路由：只有 Telegram + 有 send_sticker + 拿得到 chat_id 才走原生贴纸。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def _bridge(self, **kwargs):
        return TG.TgStickerBridge(self.dir / "cache", **kwargs)

    def test_qq_event_never_routes_to_sticker(self):
        bridge = self._bridge()
        event = _TgEvent(platform="aiocqhttp", client=_TgClient())
        self.assertIsNone(bridge.route(event))
        self.assertEqual(bridge.last_error, "")

    def test_disabled_switch_routes_nowhere(self):
        bridge = self._bridge(enabled=False)
        self.assertIsNone(bridge.route(_TgEvent(client=_TgClient())))

    def test_missing_client_or_send_sticker_falls_back(self):
        bridge = self._bridge()
        self.assertIsNone(bridge.route(_TgEvent(client=...)))
        self.assertIn("send_sticker", bridge.last_error)
        self.assertIsNone(bridge.route(_TgEvent(client=SimpleNamespace(send_message=None))))
        self.assertIn("send_sticker", bridge.last_error)

    def test_missing_chat_id_falls_back(self):
        bridge = self._bridge()
        event = _TgEvent(client=_TgClient())
        del event.chat_id
        event.get_group_id = lambda: None
        event.get_sender_id = lambda: None
        self.assertIsNone(bridge.route(event))
        self.assertIn("chat_id", bridge.last_error)

    def test_route_returns_client_and_chat_id(self):
        client = _TgClient()
        bridge = self._bridge()
        self.assertEqual(bridge.route(_TgEvent(chat_id=777, client=client)), (client, 777))

    def test_chat_id_prefers_chat_over_sender(self):
        event = _TgEvent(chat_id="group-1")
        event.get_sender_id = lambda: "user-9"
        self.assertEqual(TG.resolve_chat_id(event), "group-1")
        event = _TgEvent()
        del event.chat_id
        event.get_group_id = lambda: "g-2"
        event.get_sender_id = lambda: "user-9"
        self.assertEqual(TG.resolve_chat_id(event), "g-2")

    def test_platform_name_tolerates_other_shapes(self):
        class _Bare:
            platform_name = "Telegram"

        self.assertTrue(TG.is_telegram(_Bare()))
        self.assertFalse(TG.is_telegram(SimpleNamespace()))
        self.assertFalse(TG.is_telegram(SimpleNamespace(platform_meta=SimpleNamespace(name="aiocqhttp"))))

    def test_other_platforms_do_not_match_by_substring(self):
        # 只有真 Telegram；qq_official / dingtalk / lark 一个都不许命中
        for name in ("aiocqhttp", "qq_official", "dingtalk", "lark", "satori", "webchat"):
            self.assertFalse(TG.is_telegram(SimpleNamespace(platform_name=name)), name)
        self.assertTrue(TG.is_telegram(SimpleNamespace(platform_name="telegram")))

    def test_sender_id_is_never_used_as_chat(self):
        # 拿不到会话 id 时宁可不发原生贴纸，也不能把群表情发成私聊
        event = _TgEvent()
        del event.chat_id
        event.get_group_id = lambda: None
        event.get_sender_id = lambda: "user-9"
        self.assertIsNone(TG.resolve_chat_id(event))


class TgSendTests(unittest.TestCase):
    """发送三态：None 可回退原路，True/False 都表示已经尝试过、不许补发。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.src = self.dir / "sticker.gif"
        self.src.write_bytes(b"GIF89a")  # 内容不重要，测试只走缓存命中

    def _bridge(self, **kwargs):
        bridge = TG.TgStickerBridge(self.dir / "cache", **kwargs)
        seeded = bridge.cache_path(self.src)
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_bytes(b"webm-bytes")
        return bridge, seeded

    def test_success_uploads_webm_handle_and_counts(self):
        bridge, seeded = self._bridge()
        client = _TgClient()
        self.assertTrue(asyncio.run(bridge.send(_TgEvent(chat_id=42, client=client), self.src)))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["chat_id"], 42)
        self.assertTrue(client.calls[0]["name"].endswith(".webm"))
        self.assertEqual(bridge.cache_hits, 1)
        self.assertEqual(bridge.last_error, "")

    def test_send_failure_returns_false_without_retry(self):
        bridge, _ = self._bridge()
        client = _TgClient(error=RuntimeError("Bad Request: STICKER_VIDEO_BIG"))
        sent = asyncio.run(bridge.send(_TgEvent(client=client), self.src))
        self.assertIs(sent, False)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("send_sticker 失败", bridge.last_error)

    def test_timeout_returns_false_not_none(self):
        bridge, _ = self._bridge(send_timeout=0.05)
        client = _TgClient(delay=0.3)
        sent = asyncio.run(bridge.send(_TgEvent(client=client), self.src))
        self.assertIs(sent, False)
        self.assertEqual(len(client.calls), 1)

    def test_no_client_returns_none_so_caller_can_fall_back(self):
        bridge, _ = self._bridge()
        self.assertIsNone(asyncio.run(bridge.send(_TgEvent(client=...), self.src)))

    def test_convert_failure_returns_none_and_writes_no_cache(self):
        bridge = TG.TgStickerBridge(self.dir / "fresh", ffmpeg=str(self.dir / "nope-ffmpeg.exe"))
        sent = asyncio.run(bridge.send(_TgEvent(client=_TgClient()), self.src))
        self.assertIsNone(sent)
        self.assertIn("转码失败", bridge.last_error)
        self.assertEqual(bridge.converted, 0)
        leftovers = list((self.dir / "fresh").rglob("*")) if (self.dir / "fresh").exists() else []
        self.assertEqual([p for p in leftovers if p.is_file()], [])

    def test_oversized_cache_entry_is_dropped_and_reencoded_never_sent(self):
        bridge = TG.TgStickerBridge(self.dir / "cache", max_bytes=4)
        seeded = bridge.cache_path(self.src)
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_bytes(b"x" * 50)
        client = _TgClient()
        sent = asyncio.run(bridge.send(_TgEvent(client=client), self.src))
        self.assertIsNone(sent)  # 超限：不发，退回原路，绝不当作已发
        self.assertEqual(client.calls, [])
        self.assertIn("转码失败", bridge.last_error)

    def test_sdk_rejecting_file_object_falls_back_without_counting(self):
        class _Picky:
            def send_sticker(self, chat_id, sticker):  # 老 SDK：只接受 file_id
                raise TypeError("sticker must be a file_id")

        if not TG.locate_ffmpeg():
            self.skipTest("本机没有 ffmpeg")
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("没有 Pillow")
        src = self.dir / "picky.png"
        Image.new("RGBA", (40, 40), (30, 60, 90, 0)).save(src)
        bridge = TG.TgStickerBridge(self.dir / "cache")
        # None = 一个字节都没发出去，外层可以安全走普通图片，也不算成功
        self.assertIsNone(asyncio.run(bridge.send(_TgEvent(client=_Picky()), src)))
        self.assertIn("不接受文件对象", bridge.last_error)

    def test_bogus_configured_ffmpeg_falls_back_to_locate(self):
        if not TG.locate_ffmpeg():
            self.skipTest("本机没有 ffmpeg")
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("没有 Pillow")
        src = self.dir / "fallback.png"
        Image.new("RGBA", (40, 40), (10, 20, 30, 0)).save(src)
        bridge = TG.TgStickerBridge(self.dir / "cache", ffmpeg=str(self.dir / "no-such-ffmpeg.exe"))
        self.assertIsNotNone(asyncio.run(bridge.prepare(src)), bridge.last_error)

    def test_ffmpeg_is_never_imported_from_telegram(self):
        source = (ROOT / "tgsticker.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            head = line.strip()
            self.assertFalse(head.startswith("import telegram"), head)
            self.assertFalse(head.startswith("from telegram"), head)

    def test_failed_convert_leaves_no_partial_cache_file(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover - Pillow 是 AstrBot 自带依赖
            self.skipTest("没有 Pillow")
        src = self.dir / "real.gif"
        frames = []
        for i in range(3):
            im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(im).rectangle([i * 10, 10, i * 10 + 30, 40], fill=(0, 200, 120, 255))
            frames.append(im)
        frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0, disposal=2)

        cache = self.dir / "broken"
        bridge = TG.TgStickerBridge(cache)
        original = TG.subprocess.run
        # 假 ffmpeg：非零退出、什么都不写；mkstemp 建的临时文件必须被清干净
        TG.subprocess.run = lambda args, **kwargs: SimpleNamespace(returncode=1, stderr=b"boom", stdout=b"")
        try:
            sent = asyncio.run(bridge.send(_TgEvent(client=_TgClient()), src))
        finally:
            TG.subprocess.run = original
        self.assertIsNone(sent)
        self.assertIn("转码失败", bridge.last_error)
        leftovers = [p.name for p in cache.rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [])
        self.assertEqual(bridge.converted, 0)

    def test_each_conversion_uses_its_own_temp_file(self):
        """同一成品名连转两次也不许共用 out.part：临时名各自独有，成品仍只有一个。"""
        cache = self.dir / "uniq"
        bridge = TG.TgStickerBridge(cache)
        bridge._frames = lambda src: (b"\x00" * 16, 10)  # 不真解图，这里只验临时文件名
        out = bridge.cache_path(self.src)
        seen = []

        def fake_run(args, **kwargs):
            tmp = Path(args[-1])
            seen.append(tmp)
            tmp.write_bytes(b"webm")  # 假装 ffmpeg 已经写好
            return SimpleNamespace(returncode=0, stderr=b"")

        original = TG.subprocess.run
        TG.subprocess.run = fake_run
        try:
            bridge._convert("ffmpeg", self.src, out)
            bridge._convert("ffmpeg", self.src, out)
        finally:
            TG.subprocess.run = original

        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0].name, seen[1].name)
        for tmp in seen:
            self.assertEqual(tmp.parent, bridge.cache_dir)
            self.assertTrue(tmp.name.endswith(".webm.part"), tmp.name)
        self.assertEqual([p.name for p in cache.rglob("*.part")], [])
        self.assertEqual(out.read_bytes(), b"webm")

    def test_frames_stop_at_useful_cap_not_max_frames(self):
        """3 秒 × 30fps 就够用：200 帧的 GIF 只解 90 帧，不为 240 帧再攒 RGBA。"""
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("没有 Pillow")
        src = self.dir / "long.gif"
        # 每帧颜色不同：GIF 会把完全相同的帧去重，那样就测不到 200 帧
        frames = [Image.new("RGBA", (8, 8), (i % 256, 40, 60, 128)) for i in range(200)]
        # 20ms/帧 = 50fps：时长上限本可解 150 帧，靠 30fps 的硬上限先截到 90
        frames[0].save(src, save_all=True, append_images=frames[1:], duration=20, loop=0, disposal=2)

        class _Tiny:
            def tobytes(self):
                return b"f"

        calls = []

        def _fake_fit(frame):
            calls.append(1)
            return _Tiny()

        original = TG.TgStickerBridge.__dict__["_fit"]  # 取 staticmethod 本体，恢复后不能变成实例方法
        TG.TgStickerBridge._fit = staticmethod(_fake_fit)
        try:
            raw, fps = TG.TgStickerBridge(self.dir / "frames")._frames(src)
        finally:
            TG.TgStickerBridge._fit = original

        self.assertEqual(fps, TG.TG_MAX_FPS)  # 20ms/帧 → 50fps，被夹到 30
        self.assertEqual(len(calls), 90)  # 3.0s × 30fps 的解帧硬上限，不是 200/240
        self.assertEqual(len(raw), 90)  # 正好 3 秒，不用再截


@unittest.skipUnless(TG.locate_ffmpeg(), "本机没有 ffmpeg，跳过真实转码样本")
class TgRealEncodeTests(unittest.TestCase):
    """本机有 ffmpeg 时跑一次真转码：必须 VP9/512×512，且解码后确实还有透明像素。"""

    def test_dynamic_transparent_gif_becomes_vp9_with_alpha(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover
            self.skipTest("没有 Pillow")
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            src = work / "sample.gif"
            frames = []
            for i in range(6):
                im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
                ImageDraw.Draw(im).ellipse([40 + i * 15, 80, 160 + i * 15, 200], fill=(255, 120, 140, 255))
                frames.append(im)
            frames[0].save(src, save_all=True, append_images=frames[1:], duration=80, loop=0, disposal=2)

            bridge = TG.TgStickerBridge(work / "cache")
            out = asyncio.run(bridge.prepare(src))
            self.assertIsNotNone(out, bridge.last_error)
            self.assertEqual(out.suffix, ".webm")
            self.assertLessEqual(out.stat().st_size, bridge.max_bytes)

            ffmpeg = TG.locate_ffmpeg()
            side = TG.TG_SIDE
            raw = subprocess.run(
                [ffmpeg, "-v", "error", "-c:v", "libvpx-vp9", "-i", str(out),
                 "-f", "rawvideo", "-pix_fmt", "rgba", "-frames:v", "1", "pipe:1"],
                capture_output=True,
            ).stdout
            self.assertEqual(len(raw), side * side * 4)
            alphas = raw[3::4]
            self.assertTrue(any(a < 16 for a in alphas), "解码后没有透明像素：alpha 通道丢了")
            self.assertTrue(any(a > 240 for a in alphas), "解码后没有不透明像素")
            self.assertEqual(bridge.converted, 1)

            again = asyncio.run(bridge.prepare(src))
            self.assertEqual(again, out)
            self.assertEqual(bridge.cache_hits, 1)
            self.assertEqual(bridge.converted, 1)


class TgCallPathTests(unittest.TestCase):
    """插件四条发图路径：TG 成功/失败都不许再补发，QQ 原路一个字不动。"""

    ITEM = {"id": "1", "name": "摸鱼", "emotion": "开心", "bytes": 2048}

    def _subject(self, tg=None, **kwargs):
        subject = _Subject(items=[self.ITEM], tg=tg, **kwargs)
        subject._resolve_item = lambda name: self.ITEM
        subject._index.search = lambda name, limit=3: []
        subject._renderer = SimpleNamespace(render=self._fake_render)
        subject._avatar_url = lambda event: None
        subject._template_of = lambda name: "think"
        return subject

    async def _fake_render(self, tpl, text, image=None):
        path = Path(self._render_path)
        path.write_bytes(b"GIF89a")
        return path

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._render_path = str(Path(self.temp.name) / "meme.gif")

    def test_deliver_keeps_qq_path_on_none(self):
        event = _Event("aiocqhttp:group:1")
        subject = self._subject(tg=_TgBridge(result=None))
        self.assertTrue(asyncio.run(subject._deliver(event, self.ITEM)))
        self.assertEqual(len(event.send_calls), 1)
        self.assertEqual(subject.context.calls, [])

    def test_deliver_does_not_resend_when_sticker_already_sent(self):
        for result in (True, False):
            with self.subTest(result=result):
                event = _Event("telegram:group:1")
                subject = self._subject(tg=_TgBridge(result=result))
                got = asyncio.run(subject._deliver(event, self.ITEM))
                self.assertIs(got, result)
                self.assertEqual(event.send_calls, [])
                self.assertEqual(subject.context.calls, [])
                if result is False:
                    self.assertIn("TG 贴纸发送失败", subject._skip_reason)

    def test_deliver_treats_bridge_crash_as_attempted(self):
        event = _Event("telegram:group:1")
        subject = self._subject(tg=_TgBridge(error=RuntimeError("boom")))
        self.assertFalse(asyncio.run(subject._deliver(event, self.ITEM)))
        self.assertEqual(event.send_calls, [])
        self.assertEqual(subject.context.calls, [])

    def test_deliver_with_real_bridge_on_qq_leaves_state_untouched(self):
        bridge = TG.TgStickerBridge(Path(self.temp.name) / "cache", ffmpeg=str(Path(self.temp.name) / "no-ffmpeg"))
        event = _Event("aiocqhttp:group:1")
        subject = self._subject(tg=bridge)
        self.assertTrue(asyncio.run(subject._deliver(event, self.ITEM)))
        self.assertEqual(len(event.send_calls), 1)
        self.assertEqual(bridge.last_error, "")
        self.assertEqual(bridge.converted, 0)

    def test_tool_send_reports_failure_without_marking_sent(self):
        event = _Event("telegram:group:1")
        subject = self._subject(tg=_TgBridge(result=False))
        reply = asyncio.run(_NS_TOOLS["tool_send"](subject, event, "摸鱼"))
        self.assertIn("发图失败", reply)
        self.assertFalse(event.get_extra("_nekowhale_already_sent"))
        self.assertEqual(event.send_calls, [])

    def test_tool_send_success_skips_image_and_marks_sent(self):
        event = _Event("telegram:group:1")
        subject = self._subject(tg=_TgBridge(result=True))
        reply = asyncio.run(_NS_TOOLS["tool_send"](subject, event, "摸鱼"))
        self.assertIn("已发出", reply)
        self.assertTrue(event.get_extra("_nekowhale_already_sent"))
        self.assertEqual(event.send_calls, [])

    def test_tool_render_reports_failure_without_marking_sent(self):
        event = _Event("telegram:group:1")
        subject = self._subject(tg=_TgBridge(result=False))
        reply = asyncio.run(_NS_TOOLS["tool_render"](subject, event, "想", "上班", ""))
        self.assertIn("发送失败", reply)
        self.assertFalse(event.get_extra("_nekowhale_already_sent"))

    def test_inline_counts_only_when_really_sent(self):
        event = _Event("telegram:group:1")
        event.get_result = lambda: _Result()
        event.get_message_str = lambda: "u"
        subject = self._subject(tg=_TgBridge(result=True))
        subject._index.item = self.ITEM
        subject._picker = _Picker(result=(self.ITEM, {"stage": "keyword", "sim": 0.5}))
        asyncio.run(_NS_TOOLS["_inline"](subject, event, {"user_text": "u", "reply_text": "r"}))
        self.assertTrue(event.get_extra("_nekowhale_picked"))
        self.assertEqual(subject._recent["telegram:group:1"], ["1"])
        self.assertEqual(event.get_result().chain, [])

        event = _Event("telegram:group:1")
        event.get_result = lambda: _Result()
        event.get_message_str = lambda: "u"
        subject = self._subject(tg=_TgBridge(result=False))
        subject._index.item = self.ITEM
        subject._picker = _Picker(result=(self.ITEM, {"stage": "keyword", "sim": 0.5}))
        asyncio.run(_NS_TOOLS["_inline"](subject, event, {"user_text": "u", "reply_text": "r"}))
        self.assertFalse(event.get_extra("_nekowhale_picked"))
        self.assertEqual(subject._recent["telegram:group:1"], [])
        self.assertEqual(subject._cooldown, {})
        self.assertEqual(event.get_result().chain, [])


if __name__ == "__main__":
    unittest.main()
