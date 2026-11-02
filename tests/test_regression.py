"""标准库回归：从真实源码 AST 提取核心函数，不加载 AstrBot。

运行：python -m unittest discover -s _plugins/astrbot_plugin_nekowhale_stickers/tests -v
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import random
from collections import defaultdict
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


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


def _extract_main_functions(*wanted):
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
        "EXTRA_SENT": "_nekowhale_already_sent",
        "EXTRA_PICKED": "_nekowhale_picked",
        "EXTRA_CTX": "_nekowhale_ctx",
    }
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


class _Subject:
    """Small object exposing only collaborators used by extracted methods."""

    def __init__(self, cfg=None, *, items=None, picker=None, kit=None, context=None):
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


if __name__ == "__main__":
    unittest.main()
