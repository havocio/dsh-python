"""fs / shell / terminal 内置工具测试：fs 服务（行窗口/原子写/字面替换）、
tool_fs / tool_bash / tool_terminal 工具注册与执行、shell 执行、terminal 持久会话。

运行：``python dsh_py/tests/test_tool_fs_shell_terminal.py``
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from dsh_py.core.context import AppContext
from dsh_py.plugins import tool_bash as TB
from dsh_py.plugins import tool_fs as TF
from dsh_py.plugins import tool_terminal as TT
from dsh_py.services import fs as FS
from dsh_py.services import shell as SH
from dsh_py.services import terminal as TERM
from dsh_py.services import tools as T


def _ctx() -> AppContext:
    ctx = AppContext()
    T.apply(ctx)
    FS.apply(ctx)
    SH.apply(ctx)
    TERM.apply(ctx)
    TF.apply(ctx)
    TB.apply(ctx)
    TT.apply(ctx)
    return ctx


def _j(**kwargs) -> str:
    return json.dumps(kwargs)


# --------------------------------------------------------------------------- #
# 1. fs 服务：行窗口 / 原子写 / 字面替换 / 列表
# --------------------------------------------------------------------------- #
def test_fs_service() -> None:
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "a.txt")
    ctx = _ctx()
    # 原子写
    written = ctx.fs.write_text(path, "l1\nl2\nl3\nl4\nl5")
    assert written["bytes"] == len("l1\nl2\nl3\nl4\nl5".encode("utf-8"))
    assert ctx.fs.exists(path)
    # 行窗口
    result = ctx.fs.read_text(path, offset=2, limit=2)
    assert [n for n, _ in result["lines"]] == [2, 3]
    assert [t for _, t in result["lines"]] == ["l2", "l3"]
    assert result["total_lines"] == 5
    # 单行超长截断
    long_path = os.path.join(tmp, "long.txt")
    ctx.fs.write_text(long_path, "x" * 3000)
    long_read = ctx.fs.read_text(long_path, max_line_length=100)
    assert long_read["truncated"] is True
    assert len(long_read["lines"][0][1]) <= 101  # 100 + 省略号
    # 字面替换（唯一）
    edited = ctx.fs.edit_text(path, "l3", "L3")
    assert edited["replaced"] is True and edited["count"] == 1
    assert "L3" in ctx.fs.read_text(path)["lines"][2][1]
    # 非唯一 → 抛错
    ctx.fs.write_text(path, "same\nsame\n")
    try:
        ctx.fs.edit_text(path, "same", "diff")
        raise AssertionError("非唯一匹配应抛错")
    except ValueError:
        pass
    # replace_all 通过
    edited = ctx.fs.edit_text(path, "same", "diff", replace_all=True)
    assert edited["count"] == 2
    # 列表
    entries = ctx.fs.list(tmp)
    names = {e["name"] for e in entries}
    assert "a.txt" in names and "long.txt" in names


# --------------------------------------------------------------------------- #
# 2. tool_fs：read / write / edit 工具注册与执行
# --------------------------------------------------------------------------- #
def test_tool_fs_registration() -> None:
    ctx = _ctx()
    names = [s["name"] for s in ctx.tools.list_schemas()]
    assert {"read", "write", "edit", "bash", "terminal"} <= set(names)
    # 参数 schema 校验（缺 required）
    async def main() -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.txt")
        result, is_error = await ctx.tools.execute("write", _j(file_path=path, content="hello\nworld"))
        assert not is_error
        result, is_error = await ctx.tools.execute("read", _j(file_path=path))
        assert not is_error
        assert "1 | hello" in result and "2 | world" in result
        result, is_error = await ctx.tools.execute("edit", _j(file_path=path, old_string="world", new_string="WORLD"))
        assert not is_error and "已替换 1 处" in result
        result, is_error = await ctx.tools.execute("read", _j(file_path=path))
        assert "WORLD" in result
        # 缺失 file_path → schema 校验错误回流
        result, is_error = await ctx.tools.execute("read", _j(offset=1))
        assert is_error and "参数" in result

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 3. shell 服务 + tool_bash
# --------------------------------------------------------------------------- #
def test_shell_and_bash() -> None:
    async def main() -> None:
        ctx = _ctx()
        # 服务层
        result = await ctx.shell.execute("echo shell-ok")
        assert result["exit_code"] == 0
        assert "shell-ok" in result["stdout"]
        # 工作目录（Git Bash 输出 POSIX 路径，用唯一目录 basename 断言）
        tmp = tempfile.mkdtemp()
        result = await ctx.shell.execute("pwd", cwd=tmp)
        assert os.path.basename(tmp) in result["stdout"]
        # 工具层
        result, is_error = await ctx.tools.execute("bash", _j(command="echo bash-tool"))
        assert not is_error
        assert "bash-tool" in result and "exit code: 0" in result
        # 失败命令：非零退出码但工具不抛错（文本回流）
        result, is_error = await ctx.tools.execute("bash", _j(command="exit 3"))
        assert not is_error
        assert "exit code: 3" in result
        # 超时
        result, is_error = await ctx.tools.execute("bash", _j(command="sleep 5", timeout_ms=200))
        assert not is_error
        assert "被终止" in result
        # 空命令 → 错误文本
        result, is_error = await ctx.tools.execute("bash", _j(command="   "))
        assert is_error

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 4. terminal：持久会话 start / send / close
# --------------------------------------------------------------------------- #
def test_terminal() -> None:
    async def main() -> None:
        ctx = _ctx()
        session = ctx.terminals.spawn(cwd=tempfile.mkdtemp())
        # 会话内持久：cd 之后 pwd 保持
        out = session.send("echo t1")
        assert "t1" in out
        out = session.send("echo t2")
        assert "t2" in out
        assert "t1" not in out  # send 只返回新输出
        # 工具层
        result, is_error = await ctx.tools.execute("terminal", _j(operation="start"))
        assert not is_error
        session_id = result.split(":")[1].strip().split("（")[0]
        result, is_error = await ctx.tools.execute("terminal", _j(operation="send", session_id=session_id, command="echo persist"))
        assert not is_error and "persist" in result
        result, is_error = await ctx.tools.execute("terminal", _j(operation="close", session_id=session_id))
        assert not is_error
        # 已关闭会话 send → 错误文本
        result, is_error = await ctx.tools.execute("terminal", _j(operation="send", session_id=session_id, command="echo x"))
        assert is_error or "错误" in result
        # 未知 operation
        result, is_error = await ctx.tools.execute("terminal", _j(operation="explode"))
        assert "错误" in result
        session.close()
        ctx.terminals.close_all()

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_fs_service,
        test_tool_fs_registration,
        test_shell_and_bash,
        test_terminal,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"FAIL {test.__name__}: {exc}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")
    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
