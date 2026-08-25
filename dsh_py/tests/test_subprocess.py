"""subprocess seam + 本地实现的验证（第 3 层，对标 dsh 的 subprocess 系列测试）。

运行：python dsh_py/tests/test_subprocess.py

覆盖：scrub 环境、可执行文件解析、collect 输出、stdin 批处理、截断+spill、
树级终止升级、pipe 模式。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.subprocess import SubprocessCollect, SubprocessSpawnSpec, SubprocessStdio, scrubbed_parent_env
from dsh_py.services.subprocess_local import (
    OutputCollector,
    apply as apply_subprocess_local,
    apply_local_invariant,
    child_env,
    parse_proc_stat,
)


def _setup():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_subprocess_local(ctx, {})
    return ctx


def _collect_spec(argv, max_bytes=1_000_000, spill=None, stdin="ignore"):
    return SubprocessSpawnSpec(
        argv=tuple(argv),
        cwd=os.getcwd(),
        stdio=SubprocessStdio(
            stdin=stdin,
            stdout=SubprocessCollect(maxBytes=max_bytes, spill=spill),
            stderr=SubprocessCollect(maxBytes=16_384, spill=spill),
        ),
        graceMs=2000,
    )


async def test_scrubbed_parent_env():
    os.environ["DSH_LEAK_TEST"] = "1"
    os.environ["MY_TEST_API_KEY"] = "secret"
    env = scrubbed_parent_env()
    assert "DSH_LEAK_TEST" not in env
    assert "MY_TEST_API_KEY" not in env
    assert "PATH" in env
    os.environ.pop("DSH_LEAK_TEST", None)
    os.environ.pop("MY_TEST_API_KEY", None)


async def test_resolve_executable():
    ctx = _setup()
    resolved = await ctx.subprocess.resolve_executable(sys.executable)
    assert resolved == os.path.abspath(sys.executable)
    try:
        await ctx.subprocess.resolve_executable("some/relative/path")
        assert False, "相对路径应拒绝"
    except RuntimeError as exc:
        assert "relative path" in str(exc)
    try:
        await ctx.subprocess.resolve_executable("definitely-not-a-real-command-xyz")
        assert False, "PATH 未命中应拒绝"
    except RuntimeError as exc:
        assert "not found" in str(exc)


async def test_spawn_collect_echo():
    ctx = _setup()
    handle = ctx.subprocess.spawn(_collect_spec([
        sys.executable, "-c", "print('hello subprocess')",
    ]))
    outcome = await asyncio.wait_for(handle.done, timeout=10)
    assert outcome.exitCode == 0
    read = handle.collected.stdout.read_from(0)
    assert "hello subprocess" in read["text"]
    assert not read["lossy"]
    assert await handle.wait_for_exit() is True


async def test_spawn_stdin_batch():
    ctx = _setup()
    handle = ctx.subprocess.spawn(_collect_spec([
        sys.executable, "-c", "import sys; data=sys.stdin.read(); print('GOT:'+data.upper())",
    ], stdin={"data": "abc"}))
    outcome = await asyncio.wait_for(handle.done, timeout=10)
    assert outcome.exitCode == 0
    read = handle.collected.stdout.read_from(0)
    assert "GOT:ABC" in read["text"]


async def test_spawn_truncation_and_spill():
    ctx = _setup()
    handle = ctx.subprocess.spawn(_collect_spec([
        sys.executable, "-c", "print('x' * 5000)",
    ], max_bytes=200, spill={"maxBytes": 50_000}))
    outcome = await asyncio.wait_for(handle.done, timeout=10)
    assert outcome.exitCode == 0
    read = handle.collected.stdout.read_from(0)
    assert read["lossy"] is True
    assert read["spillPath"] is not None
    assert len(read["text"]) <= 200
    with open(read["spillPath"], "r", encoding="utf-8", errors="replace") as f:
        full = f.read()
    assert "xxxxx" in full and len(full) >= 5000


async def test_spawn_terminate_escalation():
    ctx = _setup()
    handle = ctx.subprocess.spawn(_collect_spec([
        sys.executable, "-c", "import time; time.sleep(30)",
    ]))
    await asyncio.sleep(0.2)
    handle.terminate()
    outcome = await asyncio.wait_for(handle.done, timeout=10)
    # POSIX 死于 SIGTERM（exitCode None）；Windows taskkill 为非零退出码——两者都
    # 表明树被终止且 done 从未拒绝。
    assert outcome.exitCode is None or outcome.exitCode != 0


async def test_spawn_pipe_mode():
    ctx = _setup()
    spec = SubprocessSpawnSpec(
        argv=(sys.executable, "-c", "import sys; sys.stdout.write('raw'); sys.stdout.flush()"),
        cwd=os.getcwd(),
        stdio=SubprocessStdio(stdin="ignore", stdout="pipe", stderr="inherit"),
        graceMs=2000,
    )
    handle = ctx.subprocess.spawn(spec)
    await asyncio.sleep(0.1)  # dsh_py 的 asyncio 创建异步接线：等后台任务完成
    assert handle.stdout is not None  # 原始管道流暴露给调用方
    outcome = await asyncio.wait_for(handle.done, timeout=10)
    assert outcome.exitCode == 0
    data = await asyncio.wait_for(handle.stdout.read(16), timeout=5)
    assert b"raw" in data


async def test_child_env_scrubs_and_overrides():
    os.environ["DSH_INHERITED"] = "1"
    os.environ["PARENT_API_TOKEN"] = "secret"
    env = child_env({"DSH_INHERITED": "explicit", "MY_VAR": "x", "PARENT_API_TOKEN": None})
    # 显式 DSH_ 覆盖 scrub（显式条目在 scrub 后合并）
    assert env.get("DSH_INHERITED") == "explicit"
    assert env.get("MY_VAR") == "x"
    # 显式 None 墓碑移除凭据形状条目
    assert "PARENT_API_TOKEN" not in env
    # 未显式处理的凭据形状仍被 scrub
    assert "PARENT_API_TOKEN" not in scrubbed_parent_env()
    os.environ.pop("DSH_INHERITED", None)
    os.environ.pop("PARENT_API_TOKEN", None)


async def test_collector_read_offsets():
    collector = OutputCollector(max_bytes=10_000, max_spill_bytes=None, label="t", spill_dir=os.getcwd())
    collector.push(b"hello ")
    first = collector.read_from(0)
    assert first["text"] == "hello " and first["nextOffset"] == 6 and not first["lossy"]
    collector.push(b"world")
    second = collector.read_from(first["nextOffset"])
    assert second["text"] == "world" and second["nextOffset"] == 11 and not second["lossy"]
    # 请求偏移滑出尾部窗口 → lossy
    small = OutputCollector(max_bytes=4, max_spill_bytes=None, label="s", spill_dir=os.getcwd())
    small.push(b"abcdef")
    read = small.read_from(0)
    assert read["lossy"] and read["text"] == "cdef"  # 尾部 4 字节
    assert read["nextOffset"] == 6


async def test_parse_proc_stat():
    # 样例：pid 1234，comm 含空格，state R，ppid 1，pgrp 1234，session 2，tpgid 1234
    sample = "1234 (some process name) R 1 1234 2 34816 1234 4196 0 0 0 0 0 0 0 0 20 0 1 0 11111 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    stat = parse_proc_stat(sample)
    assert stat is not None
    assert stat["pid"] == 1234
    assert stat["parentPid"] == 1
    assert stat["pgrp"] == 1234
    assert stat["state"] == "R"
    assert stat["started"] == "11111"
    # 畸形输入
    assert parse_proc_stat("not a stat line") is None
    assert parse_proc_stat("") is None


async def test_invariant_noop():
    from dsh_py.services.invariants import apply as apply_invariants
    from dsh_py.services.subprocess import apply_seam_invariant

    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    apply_invariants(ctx, {})
    apply_seam_invariant(ctx)
    apply_local_invariant(ctx)
    names = ctx.invariants.list()
    assert "dsh-subprocess" in names
    assert "dsh-subprocess-local" in names


async def _main():
    tests = [
        test_scrubbed_parent_env,
        test_resolve_executable,
        test_spawn_collect_echo,
        test_spawn_stdin_batch,
        test_spawn_truncation_and_spill,
        test_spawn_terminate_escalation,
        test_spawn_pipe_mode,
        test_child_env_scrubs_and_overrides,
        test_collector_read_offsets,
        test_parse_proc_stat,
        test_invariant_noop,
    ]
    failures = 0
    for test in tests:
        try:
            await test()
            print(f"  ✓ {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"  ✗ {test.__name__}: {exc}")
    print(f"subprocess: {'全部通过' if failures == 0 else f'{failures} 个失败'}")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(_main()) else 0)
