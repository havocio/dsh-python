"""glob / grep 文件系统发现工具冒烟（tool-fs-search，对标 dsh-tool-fs-search）。

纯 assert + __main__ 风格：python dsh_py/tests/test_tool_fs_search.py

用原生 Python 实现：不依赖 ripgrep 二进制，保留 dsh 的配置语义、保留、溢出与
spill 契约。为可重复运行，所有文件落在临时目录。
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services.fs import FileSystem
from dsh_py.services.tools import ToolService
from dsh_py.services.system_prompt import SystemPrompt
import dsh_py.plugins.tool_fs_search as search


class MockSpill:
    """记录 saveText 调用的轻量 spill 后端（不必是 Service）。"""

    def __init__(self):
        self.calls = []

    async def saveText(self, inp):
        self.calls.append(inp)
        return {
            "locator": f"file:///spill/{inp['suggestedName']}",
            "bytes": len(inp["content"]),
            "retrievalHint": "用 read 读取此路径查看完整结果。",
        }


class FakeAgent:
    def __init__(self, cwd, sid="sess-1"):
        self.session = type("S", (), {"header": type("H", (), {"cwd": cwd, "id": sid})()})()


def build_context(tmp, config=None, with_spill=True):
    ctx = AppContext()
    FileSystem(ctx)
    ToolService(ctx)
    SystemPrompt(ctx)
    if with_spill:
        ctx.provide("spillStore", MockSpill())
    search.apply(ctx, config or {})
    return ctx


def seed_tree(root):
    """创建：a.py, b.py, sub/c.py, deep/x.py, .git/keep（应被排除）。"""
    os.makedirs(os.path.join(root, "sub"))
    os.makedirs(os.path.join(root, "deep"))
    os.makedirs(os.path.join(root, ".git"))
    files = {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def beta():\n    return 2\n",
        "sub/c.py": "def gamma():\n    return 3\n",
        "deep/x.py": "import os\n# secret token here\nx = 42\n",
        ".git/keep": "should be excluded\n",
    }
    for rel, content in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(content)


async def call(ctx, agent, name, args):
    text, is_error, _ctxs = await ctx.tools.execute_with_agent(name, json.dumps(args), agent=agent)
    return text, is_error


# --------------------------------------------------------------------------- #
async def test_glob_basic():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        # *.py 匹配任意深度文件名，且排除 .git
        text, err = await call(ctx, agent, "glob", {"pattern": "*.py"})
        assert not err, text
        lines = [l for l in text.split("\n") if l]
        assert "a.py" in lines and "b.py" in lines and "sub/c.py" in lines and "deep/x.py" in lines
        assert not any(".git" in l for l in lines), "VCS 目录不应出现"
        print("  ✓ glob 基础发现（含子目录、排除 VCS）")


async def test_glob_subdir_path():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "glob", {"pattern": "*.py", "path": "sub"})
        assert not err, text
        assert "sub/c.py" in text
        assert "a.py" not in text
        print("  ✓ glob 限定 path=sub 仅返回子树")


async def test_glob_over_cap_samples_and_spills():
    with tempfile.TemporaryDirectory() as tmp:
        # 制造大量文件以触发截断
        for i in range(20):
            with open(os.path.join(tmp, f"f{i:02d}.txt"), "w", encoding="utf-8") as f:
                f.write(f"file {i}\n")
        ctx = build_context(tmp, config={"globMaxResults": 3})
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "glob", {"pattern": "*.txt"})
        assert not err, text
        assert "显示 3 / 20 个路径" in text, text
        spill = ctx.spillStore
        assert spill.calls, "超量时应触发 spill"
        assert spill.calls[0]["suggestedName"] == "glob-results.txt"
        assert len(spill.calls[0]["content"].split("\n")) == 20
        print("  ✓ glob 超量：截断 + footer + spill 完整结果")


async def test_grep_basic():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "grep", {"pattern": "def "})
        assert not err, text
        assert "找到" in text and "匹配" in text
        # 三个文件各有一个 def，应出现三处匹配
        assert text.count("Line") == 3, text
        assert "a.py" in text and "b.py" in text and "sub/c.py" in text
        print("  ✓ grep 基础正则匹配（按文件分组、带行号）")


async def test_grep_include_filter():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "grep", {"pattern": "x =", "include": "*.py"})
        assert not err, text
        # 仅在 .py 中搜索；deep/x.py 命中，其它无
        assert "deep/x.py" in text
        print("  ✓ grep include 过滤器生效")


async def test_grep_invalid_pattern():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "grep", {"pattern": "([unclosed"})
        assert err, "无效正则应判为错误"
        assert "SEARCH_INVALID_PATTERN" in text or "正则" in text, text
        print("  ✓ grep 无效正则 → SEARCH_INVALID_PATTERN")


async def test_grep_empty_result():
    with tempfile.TemporaryDirectory() as tmp:
        seed_tree(tmp)
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "grep", {"pattern": "zzz_no_such_pattern_zzz"})
        assert not err, text
        assert "未找到匹配" in text
        print("  ✓ grep 无匹配 → 友好空结果")


async def test_glob_empty_pattern_error():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, "glob", {"pattern": "   "})
        assert err, "空白 pattern 应报错"
        print("  ✓ glob 空白 pattern → 错误")


async def main():
    await test_glob_basic()
    await test_glob_subdir_path()
    await test_glob_over_cap_samples_and_spills()
    await test_grep_basic()
    await test_grep_include_filter()
    await test_grep_invalid_pattern()
    await test_grep_empty_result()
    await test_glob_empty_pattern_error()
    print("test_tool_fs_search: 全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
