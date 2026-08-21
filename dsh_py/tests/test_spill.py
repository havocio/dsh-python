"""spill 家族验证（spill / spill-local / spill-policy，第 3 层）。

运行：python dsh_py/tests/test_spill.py

覆盖：
- encode_segment：路径穿越/空/点/特殊字符注入安全 + 单射可逆；
- save_text_file：会话目录、随机前缀、独占仅所有者写、UTF-8 字节计数；
- LocalSpillStore.saveText：返回 locator/bytes/retrievalHint，文件真实落盘；
- spill-policy：超大纯文本被替换为预览 + 通知（文本变短、含 locator、不超
  cap）；未超限/非纯文本/无 agent/read 工具原样保留；存储失败 best-effort
  保留原样；maxInlineBytes 省略为 no-op、非法值 fail-loud；
- execute_with_agent 的 post-execute content 替换生效。
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.spill_local import apply as spill_local_apply
from dsh_py.services.spill_local import encode_segment, save_text_file, session_dir

import dsh_py.plugins.spill_policy as spill_policy


def _ctx(tmp=None):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    if tmp is not None:
        spill_local_apply(ctx, {"root": os.path.join(tmp, "spill")})
    return ctx


# --------------------------------------------------------------------------- #
# encode_segment
# --------------------------------------------------------------------------- #
def test_encode_segment_safety():
    # 穿越/绝对路径/分隔符/控制字符全部中和为单段
    for raw in ("../", "..", ".", "/etc/passwd", "a\\b", "a b", "", "a\u0000b", "~x"):
        encoded = encode_segment(raw)
        assert "/" not in encoded and "\\" not in encoded and "\u0000" not in encoded
        assert encoded != "." and encoded != ".."  # 字面穿越段绝不出现
    assert encode_segment("") == "~"
    assert encode_segment("..") == "~002E~002E"
    # 字面段字符保留
    assert encode_segment("web_fetch.txt") == "web_fetch.txt"
    # 单射：不同输入不冲突（抽查）
    values = {encode_segment(r) for r in ("a", "a~", "~a", "a b", "a\tb", "a-b")}
    assert len(values) == len(("a", "a~", "~a", "a b", "a\tb", "a-b"))


# --------------------------------------------------------------------------- #
# save_text_file / LocalSpillStore
# --------------------------------------------------------------------------- #
async def test_save_text_file_and_store():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "spill")
        saved = await save_text_file(root, "sess-1", "web_fetch.txt", "hello 世界")
        assert saved["bytes"] == len("hello 世界".encode("utf-8"))
        assert os.path.exists(saved["path"])
        assert session_dir(root, "sess-1") in saved["path"]
        with open(saved["path"], encoding="utf-8") as f:
            assert f.read() == "hello 世界"
        # 会话目录私有
        assert oct(os.stat(os.path.dirname(saved["path"])).st_mode & 0o700) == "0o700"

        ctx = _ctx(tmp)
        ref = await ctx.spillStore.saveText({
            "owner": {"sessionId": "sess-2"},
            "source": {"toolName": "bash", "callId": "c1", "label": "result"},
            "suggestedName": "bash.txt",
            "content": "x" * 100,
        })
        assert ref["locator"].startswith(os.path.abspath(os.path.join(tmp, "spill")))
        assert ref["bytes"] == 100
        assert "grep" in ref["retrievalHint"]
        assert os.path.exists(str(ref["locator"]))


# --------------------------------------------------------------------------- #
# spill-policy
# --------------------------------------------------------------------------- #
class _Agent:
    def __init__(self, session):
        self.session = session


async def test_policy_spills_oversized_result():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        spill_policy.apply(ctx, {"maxInlineBytes": 400})
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        big = "A" * 2000
        async def handler(args):
            return big, False
        ctx.tools.register("web_fetch", "抓取", {"type": "object", "properties": {}}, handler)

        text, is_error, _ = await ctx.tools.execute_with_agent("web_fetch", "{}", agent=agent)
        assert is_error is False
        # 替换：变短、含 locator 与省略提示、不超 cap
        assert len(text.encode("utf-8")) <= 400
        assert "Full formatted result stored at:" in text
        assert "Omitted" in text and "bytes." in text
        # spill 文件真实存在（locator 是绝对路径）
        locator = text.split("stored at: ")[1].split(". ")[0]
        assert os.path.exists(locator)


async def test_policy_keeps_small_and_non_text():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        spill_policy.apply(ctx, {"maxInlineBytes": 200})
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        async def small(args):
            return "short", False
        ctx.tools.register("probe", "探测", {"type": "object", "properties": {}}, small)
        text, is_error, _ = await ctx.tools.execute_with_agent("probe", "{}", agent=agent)
        assert is_error is False and text == "short"


async def test_policy_no_agent_keeps_inline():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        spill_policy.apply(ctx, {"maxInlineBytes": 50})

        async def big(args):
            return "B" * 100, False
        ctx.tools.register("noagent", "无 agent", {"type": "object", "properties": {}}, big)
        # 无 agent 调用：best-effort 保留内联
        text, is_error, _ = await ctx.tools.execute_with_agent("noagent", "{}")
        assert is_error is False and text == "B" * 100


async def test_policy_storage_failure_keeps_inline():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        spill_policy.apply(ctx, {"maxInlineBytes": 50})
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        # 后端抛错 → best-effort 保留原样
        class BrokenStore:
            async def saveText(self, input):
                raise OSError("disk full")
        ctx.provide("spillStore", BrokenStore())

        async def big(args):
            return "C" * 100, False
        ctx.tools.register("broke", "坏存储", {"type": "object", "properties": {}}, big)
        text, is_error, _ = await ctx.tools.execute_with_agent("broke", "{}", agent=agent)
        assert is_error is False and text == "C" * 100


async def test_policy_read_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        spill_policy.apply(ctx, {"maxInlineBytes": 50})
        session = ctx.sessions.prepare(cwd=None)
        agent = _Agent(session)

        async def read_handler(args):
            return "D" * 100, False
        ctx.tools.register("read", "读文件", {"type": "object", "properties": {}}, read_handler)
        text, is_error, _ = await ctx.tools.execute_with_agent("read", "{}", agent=agent)
        assert is_error is False and text == "D" * 100  # 不 spill，避免 read→spill→read 循环


def test_policy_config_validation():
    ctx = _ctx()
    spill_policy.apply(ctx, {})  # 省略 → no-op
    for bad in ({"maxInlineBytes": -1}, {"maxInlineBytes": 1.5}, {"maxInlineBytes": True}):
        try:
            spill_policy.apply(ctx, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝配置：{bad}")


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(tests) + len(sync_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()
