"""str_replace_editor 工具冒烟（tool-str-replace-editor，对标 dsh-tool-str-replace-editor）。

纯 assert + __main__ 风格：python dsh_py/tests/test_tool_str_replace_editor.py

覆盖 view（文件/目录）、create、str_replace（唯一/重复/缺失）、insert、参数校验。
基于本地临时目录与真实 ``ctx.fs`` 后端。
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
import dsh_py.plugins.tool_str_replace_editor as sre


class FakeAgent:
    def __init__(self, cwd, sid="sess-1"):
        self.session = type("S", (), {"header": type("H", (), {"cwd": cwd, "id": sid})()})()


def build_context(tmp, config=None):
    ctx = AppContext()
    FileSystem(ctx)
    ToolService(ctx)
    sre.apply(ctx, config or {})
    return ctx


async def call(ctx, agent, args):
    text, is_error, _ctxs = await ctx.tools.execute_with_agent(
        "str_replace_editor", json.dumps(args), agent=agent)
    return text, is_error


# --------------------------------------------------------------------------- #
async def test_view_file_shows_line_numbers():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "note.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("line one\nline two\nline three\n")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {"command": "view", "path": p})
        assert not err, text
        assert "line one" in text and "line two" in text
        # 行号应为 6 位右对齐 + 双空格
        assert "     1  line one" in text, text
        print("  ✓ view 文件：带行号")


async def test_view_range():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "r.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(f"row {i}" for i in range(1, 6)))
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {"command": "view", "path": p, "view_range": [2, 3]})
        assert not err, text
        assert "row 2" in text and "row 3" in text
        assert "row 1" not in text
        print("  ✓ view view_range=[2,3] 仅显示指定行")


async def test_view_directory_lists():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "pkg", "sub"))
        open(os.path.join(tmp, "pkg", "a.py"), "w").close()
        open(os.path.join(tmp, "pkg", "sub", "b.py"), "w").close()
        open(os.path.join(tmp, "pkg", ".hidden"), "w").close()  # 应被剔除
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {"command": "view", "path": os.path.join(tmp, "pkg")})
        assert not err, text
        assert "a.py" in text and "sub" in text
        assert ".hidden" not in text
        print("  ✓ view 目录：列出 2 层、剔除隐藏项")


async def test_create_and_overwrite_guard():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "new.py")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {"command": "create", "path": p, "file_text": "print(1)\n"})
        assert not err, text
        assert os.path.exists(p)
        # 再次 create 已存在文件应报错
        text2, err2 = await call(ctx, agent, {"command": "create", "path": p, "file_text": "x"})
        assert err2, "已存在文件不应被 create 覆盖"
        print("  ✓ create 成功 + 已存在时拒绝覆盖")


async def test_str_replace_unique():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "code.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {
            "command": "str_replace", "path": p,
            "old_str": "return 1", "new_str": "return 2",
        })
        assert not err, text
        with open(p, encoding="utf-8") as f:
            assert "return 2" in f.read()
        print("  ✓ str_replace 唯一匹配 → 替换成功")


async def test_str_replace_not_unique():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "dup.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x = 1\nx = 1\n")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {
            "command": "str_replace", "path": p, "old_str": "x = 1",
        })
        assert err, "重复匹配应报错"
        assert "多次" in text or "unique" in text.lower() or "出现" in text, text
        print("  ✓ str_replace 非唯一 → 拒绝（要求唯一）")


async def test_str_replace_not_found():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nf.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hello\n")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {
            "command": "str_replace", "path": p, "old_str": "no-such-text",
        })
        assert err, "未命中应报错"
        print("  ✓ str_replace 未命中 → 报错")


async def test_insert():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "ins.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write("a\nb\nc\n")
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        text, err = await call(ctx, agent, {
            "command": "insert", "path": p, "insert_line": 1, "new_str": "INSERTED",
        })
        assert not err, text
        with open(p, encoding="utf-8") as f:
            lines = f.read().split("\n")
        # insert_line=1 → 第 1 行之后插入
        assert lines[1] == "INSERTED", lines
        print("  ✓ insert 在第 1 行后插入成功")


async def test_validation_errors():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = build_context(tmp)
        agent = FakeAgent(tmp)
        # 非绝对路径
        t1, e1 = await call(ctx, agent, {"command": "view", "path": "relative/path.py"})
        assert e1, "相对路径应报错"
        # 未知命令
        t2, e2 = await call(ctx, agent, {"command": "bogus", "path": tmp})
        assert e2, "未知命令应报错"
        # create 缺 file_text
        t3, e3 = await call(ctx, agent, {"command": "create", "path": os.path.join(tmp, "z.py")})
        assert e3, "create 缺 file_text 应报错"
        print("  ✓ 参数/路径校验：相对路径、未知命令、缺 file_text 均报错")


async def main():
    await test_view_file_shows_line_numbers()
    await test_view_range()
    await test_view_directory_lists()
    await test_create_and_overwrite_guard()
    await test_str_replace_unique()
    await test_str_replace_not_unique()
    await test_str_replace_not_found()
    await test_insert()
    await test_validation_errors()
    print("test_tool_str_replace_editor: 全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
