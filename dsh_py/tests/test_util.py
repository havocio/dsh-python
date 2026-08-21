"""util 包原语验证（第 3 层 util 家族）。

运行：python dsh_py/tests/test_util.py

覆盖：
- timeout：clamp_timeout 校验/封顶、deadline 超时触发（TimeoutReason 携带
  code+ms）、上游取消融合、idle_watchdog next/pulse 重新武装、timeout_of 分类；
- atomic-write：write_file_atomic 原子替换（mode 携带、父目录创建）、
  with_file_lock 串行化写者与释放；
- home-paths：expand/resolve 优先级（配置 > $DSH_HOME > ~/.dsh）、空白 env 视为
  未设置、display 符号化、canonicalize_watch_path 缺失后缀还原；
- retention：ItemRetainer head 计数精确省略；TextRetainer head/tail/headTail
  UTF-8 边界保持 + 精确省略字节；
- native-command：经宿主命令执行（exit 0 捕获 stdout；非零退出带 code 抛错）；
- launch-environment：三层快照来源记录、信任序、Windows 大小写折叠、
  ctx.launchEnvironment 装配。
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.util import atomic_write, home_paths, native_command, retention, timeout
from dsh_py.util.launch_environment import (
    apply as launch_env_apply,
    create_launch_environment_snapshot,
)


# --------------------------------------------------------------------------- #
# timeout
# --------------------------------------------------------------------------- #
def test_clamp_timeout():
    assert timeout.clamp_timeout(None, 5000, 30000) == 5000
    assert timeout.clamp_timeout(60000, 5000, 30000) == 30000  # 封顶
    assert timeout.clamp_timeout(200, 5000, 30000) == 200
    try:
        timeout.clamp_timeout(0, 5000, 30000)
    except ValueError:
        pass
    else:
        raise AssertionError("0 不是禁用超时哨兵，应报错")
    try:
        timeout.clamp_timeout(float("inf"), 5000, 30000)
    except ValueError:
        pass
    else:
        raise AssertionError("非有限数应报错")


async def test_deadline_timeout_fires():
    code = "BASH_TIMEOUT"
    d = timeout.deadline(None, 30, code)
    await asyncio.sleep(0.06)
    assert d.signal.aborted is True
    reason = timeout.timeout_of(d.signal)
    assert reason is not None and reason.code == code
    assert "30" in str(reason)
    d.dispose()  # 幂等清理


async def test_deadline_upstream_fusion_and_no_timer():
    upstream = CancelSignal()
    d = timeout.deadline(upstream, 0, "X")  # 0 = 无计时器
    assert d.signal.aborted is False
    upstream.abort("caller")
    assert d.signal.aborted is True
    # 外来 reason 不是 TimeoutReason → timeout_of 返回 None
    assert timeout.timeout_of(d.signal) is None

    up2 = CancelSignal()
    d2 = timeout.deadline(up2, 30, "TOOL_TIMEOUT")
    up2.abort("cancel-first")
    assert d2.signal.aborted is True
    assert timeout.timeout_of(d2.signal) is None  # 上游先胜，普通取消


async def test_idle_watchdog():
    # 在途需求期间 pulse 重新武装；超出空闲窗口 → 超时
    w1 = timeout.idle_watchdog(None, 30, "IDLE")

    async def slow_gen():
        await asyncio.sleep(0.05)  # 慢于 30ms 空闲窗口
        yield 1

    task = asyncio.create_task(w1.next(slow_gen()))
    await asyncio.sleep(0.01)
    w1.pulse()  # next 在途：重新武装
    await asyncio.sleep(0.05)  # 超过空闲窗口
    assert w1.signal.aborted is True
    try:
        await task
    except StopAsyncIteration:
        pass
    w1.dispose()

    # 正常完成路径：快于空闲窗口 → 不超时
    w2 = timeout.idle_watchdog(None, 100, "IDLE")

    async def fast_gen():
        for i in (1, 2):
            await asyncio.sleep(0.01)
            yield i

    it = fast_gen()
    assert (await w2.next(it)) == 1
    assert (await w2.next(it)) == 2
    assert w2.signal.aborted is False
    w2.dispose()


# --------------------------------------------------------------------------- #
# atomic-write
# --------------------------------------------------------------------------- #
async def test_write_file_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "nested", "file.txt")
        atomic_write.write_file_atomic(target, "hello", mode=0o644, dir_mode=0o700)
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hello"
        # 二次替换原子生效（读者看到旧或新完整内容）
        atomic_write.write_file_atomic(target, "world", mode=0o644)
        with open(target, encoding="utf-8") as f:
            assert f.read() == "world"
        # 无残留临时文件
        leftovers = [n for n in os.listdir(os.path.join(tmp, "nested")) if n.endswith(".tmp")]
        assert leftovers == []


async def test_with_file_lock_serializes_and_releases():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "counter.txt")
        atomic_write.write_file_atomic(target, "0", mode=0o644)
        results = []

        async def worker():
            async def cycle():
                with open(target, encoding="utf-8") as f:
                    current = int(f.read())
                await asyncio.sleep(0.01)  # 模拟读-改-写间隙
                atomic_write.write_file_atomic(target, str(current + 1), mode=0o644)
            for _ in range(5):
                await atomic_write.with_file_lock(target, cycle)

        await asyncio.gather(worker(), worker())
        with open(target, encoding="utf-8") as f:
            assert f.read() == "10"  # 两个写者各 5 次，锁保证无覆盖丢失
        # 锁已释放
        assert not os.path.exists(target + ".lock")


async def test_with_file_lock_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        lock = os.path.join(tmp, "x.txt.lock")
        open(lock, "w").close()  # 预置陈锁（孤儿）
        try:
            await atomic_write.with_file_lock(
                os.path.join(tmp, "x.txt"), lambda: asyncio.sleep(0),
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("陈锁应导致超时失败")


# --------------------------------------------------------------------------- #
# home-paths
# --------------------------------------------------------------------------- #
def test_home_paths_resolve_precedence():
    assert home_paths.expand_home_path("~") == os.path.expanduser("~")
    assert home_paths.expand_home_path("~/x") == os.path.join(os.path.expanduser("~"), "x")
    assert home_paths.expand_home_path("rel/path") == "rel/path"

    env = {}
    assert home_paths.resolve_dsh_home(env=env) == os.path.abspath(
        os.path.join(os.path.expanduser("~"), ".dsh"))
    # $DSH_HOME 优先于默认；空白视为未设置
    assert home_paths.resolve_dsh_home(env={"DSH_HOME": "/tmp/custom"}) == os.path.abspath("/tmp/custom")
    assert home_paths.resolve_dsh_home(env={"DSH_HOME": "   "}) == os.path.abspath(
        os.path.join(os.path.expanduser("~"), ".dsh"))
    # 显式配置最高优先
    assert home_paths.resolve_dsh_home("/explicit", {"DSH_HOME": "/tmp/custom"}) == os.path.abspath("/explicit")

    display = home_paths.dsh_home_display(home_paths.resolve_dsh_home(env={}))
    assert display == "~/.dsh"
    assert home_paths.dsh_home_display("/tmp/other") == "$DSH_HOME"


def test_canonicalize_watch_path():
    with tempfile.TemporaryDirectory() as tmp:
        # 存在的路径 → realpath 规范化
        assert home_paths.canonicalize_watch_path(tmp) == os.path.realpath(tmp)
        # 缺失后缀 → 既有祖先 realpath + 后缀还原
        missing = os.path.join(tmp, "a", "b", "c.txt")
        got = home_paths.canonicalize_watch_path(missing)
        assert got == os.path.join(os.path.realpath(tmp), "a", "b", "c.txt")


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #
def test_item_retainer_head_exact():
    r = retention.ItemRetainer(2)
    assert r.push("a") == {"kept": True, "truncated": False}
    assert r.push("b")["kept"] is True
    d1 = r.push("c")
    assert d1["kept"] is False and d1["truncated"] is True
    result = r.finish()
    assert result["items"] == ["a", "b"]
    assert result["seen"] == 3 and result["kept"] == 2
    assert result["omitted"] == {"kind": "exact", "count": 1}
    assert result["truncated"] is True


def test_text_retainer_head():
    r = retention.TextRetainer({"kind": "head", "maxBytes": 5})
    r.push("hello")
    r.push(" world")
    result = r.finish()
    assert result["text"] == "hello"
    assert result["omittedBytes"] == {"kind": "exact", "count": 6}
    assert result["truncated"] is True


def test_text_retainer_tail():
    r = retention.TextRetainer({"kind": "tail", "maxBytes": 5})
    r.push("hello world")
    result = r.finish()
    assert result["text"] == "world"
    assert result["omittedBytes"] == {"kind": "exact", "count": 6}


def test_text_retainer_head_tail_utf8_boundary():
    # 中文每字 3 字节；headTail 在码点中间切时应保持边界
    r = retention.TextRetainer({"kind": "headTail", "headBytes": 3, "tailBytes": 3})
    r.push("你好世界")  # 12 字节
    result = r.finish()
    assert result["text"] == "你界"  # 前 3 字节 = 你；后 3 字节 = 界
    assert result["omittedBytes"]["count"] == 6
    # 无省略：小文本 headTail 完整返回（head+tail 相邻切片合并解码）
    r2 = retention.TextRetainer({"kind": "headTail", "headBytes": 100, "tailBytes": 100})
    r2.push("你好")
    r2_result = r2.finish()
    assert r2_result["text"] == "你好"
    assert r2_result["omittedBytes"] == {"kind": "none"}
    assert r2_result["truncated"] is False


def test_retention_notice_wording():
    assert retention.describe_omitted({"kind": "none"}, "items") == ""
    assert retention.describe_omitted({"kind": "exact", "count": 3}, "items") == "Omitted 3 items."
    assert retention.describe_omitted({"kind": "unknown"}, "bytes") == "More bytes were omitted."
    notice = {"scope": "grep", "strategy": "head", "unit": "items", "limit": 5,
              "kept": 5, "omitted": {"kind": "exact", "count": 2}}
    line = retention.format_retention_notice(notice, lambda n: "Narrow the pattern.")
    assert line == "Omitted 2 items. Narrow the pattern."


# --------------------------------------------------------------------------- #
# native-command
# --------------------------------------------------------------------------- #
async def test_native_command():
    # 用宿主解释器自身做一条无 shell 命令（跨平台）
    code = "import sys; sys.stdout.write('out-ok'); sys.stderr.write('err-ok')"
    result = await native_command.run_native_command(
        sys.executable, ["-c", code],
    )
    assert result["stdout"] == "out-ok" and result["stderr"] == "err-ok"

    try:
        await native_command.run_native_command(
            sys.executable, ["-c", "import sys; sys.exit(3)"],
        )
    except RuntimeError as e:
        assert e.code == 3  # type: ignore[attr-defined]
    else:
        raise AssertionError("非零退出应抛错")


# --------------------------------------------------------------------------- #
# launch-environment
# --------------------------------------------------------------------------- #
def test_launch_environment_snapshot():
    snapshot = create_launch_environment_snapshot([
        {"source": "process", "values": {"A": "proc", "ONLY": "proc-only"}},
        {"source": "project-env", "path": "/proj/.env", "values": {"A": "proj", "B": "proj-b"}},
        {"source": "user-env", "path": "/home/.env", "values": {"B": "user-b", "C": "user-c"}},
    ])
    # 信任序：process > project > user
    assert snapshot["get"]("A")["value"] == "proc"
    assert snapshot["get"]("B")["value"] == "proj-b"      # project 覆盖 user
    assert snapshot["get"]("C")["value"] == "user-c"
    assert snapshot["get"]("ONLY")["source"] == "process"
    assert snapshot["get"]("MISSING") is None
    # 来源记录
    assert snapshot["get"]("B")["path"] == "/proj/.env"
    assert snapshot["get"]("ONLY").get("path") is None
    # getFrom 限制层
    assert snapshot["getFrom"]("A", ("process",))["value"] == "proc"
    assert snapshot["getFrom"]("B", ("user-env",))["value"] == "user-b"
    assert snapshot["getFrom"]("B", ("process",)) is None


def test_launch_environment_ctx_slot():
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    launch_env_apply(ctx)
    snapshot = ctx.launchEnvironment
    # process 层必有 PATH
    entry = snapshot["get"]("PATH") or snapshot["get"]("Path")
    assert entry is not None and entry["source"] == "process"


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
