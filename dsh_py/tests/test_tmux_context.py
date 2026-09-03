"""tmux-context 契约单测（对照 dsh 临时冒烟脚本，正式入库）。

覆盖：状态渲染、读数渲染、刷新间隔校验、时间函数、位置查询（mock bash/logger）、
最近注入状态提取（mock events）。均不依赖真实 tmux 环境。
"""

from __future__ import annotations

from types import SimpleNamespace

from dsh_py.plugins.tmux_context import (
    NAME,
    READING_PREFIX,
    _latest_injected_state,
    _query_tmux_location,
    _render_reading,
    _render_state,
    _validate_refresh_interval,
    time_now,
)
from dsh_py.services.message import MessageSource, TextBlock


def _loc() -> dict:
    return {
        "sessionName": "sess", "windowIndex": "0", "windowName": "wn",
        "paneIndex": "1", "paneId": "%1", "windowActive": "1",
        "paneActive": "1", "windowLayout": "layout",
    }


class _Bash:
    """mock shell 执行器：可控返回或抛错。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def execute(self, command, timeout_ms=None):
        self.calls.append((command, timeout_ms))
        if self.exc is not None:
            raise self.exc
        return self.result


class _Logger:
    def __init__(self):
        self.warns = []

    def warn(self, msg):
        self.warns.append(msg)


async def test_render_state_and_reading() -> None:
    loc = _loc()
    state = _render_state(loc)
    assert "session sess" in state and "window 0 'wn'" in state and "%1" in state
    reading = _render_reading(loc, 7)
    assert reading.startswith(f"{READING_PREFIX}7):")
    assert state in reading
    print("  ✓ _render_state / _render_reading 正确")


async def test_validate_refresh_interval() -> None:
    _validate_refresh_interval(None)
    _validate_refresh_interval(0)
    _validate_refresh_interval(5000)
    for bad in (-1, "x", 1.5):
        try:
            _validate_refresh_interval(bad)
            raise AssertionError(f"应拒绝非法间隔: {bad!r}")
        except TypeError:
            pass
    print("  ✓ _validate_refresh_interval 正确")


async def test_time_now() -> None:
    t = time_now()
    assert isinstance(t, float) and t > 0
    print("  ✓ time_now 返回毫秒时间戳")


async def test_query_location() -> None:
    fields = ["sess", "0", "wn", "1", "%1", "1", "1", "layout"]
    good = "\\t".join(fields)  # 字面 '\t' 两字符分隔，对齐实现
    # 执行器拒绝（异常分支）
    bash = _Bash(exc=RuntimeError("denied"))
    logger = _Logger()
    assert _query_tmux_location(bash, logger, 1234, None) is None
    assert logger.warns  # 记录 warning
    # 非零退出
    bash = _Bash(result={"exit_code": 1, "stdout": ""})
    assert _query_tmux_location(bash, _Logger(), 1234, None) is None
    # 字段数不匹配
    bash = _Bash(result={"exit_code": 0, "stdout": "\\t".join(fields[:7])})
    assert _query_tmux_location(bash, _Logger(), 1234, None) is None
    # pane_id 空
    bad = list(fields)
    bad[4] = ""
    bash = _Bash(result={"exit_code": 0, "stdout": "\\t".join(bad)})
    assert _query_tmux_location(bash, _Logger(), 1234, None) is None
    # 正常
    bash = _Bash(result={"exit_code": 0, "stdout": good})
    res = _query_tmux_location(bash, _Logger(), 1234, None)
    assert res is not None and res["paneId"] == "%1" and res["sessionName"] == "sess"
    print("  ✓ _query_tmux_location 各分支正确")


async def test_latest_injected_state() -> None:
    block = TextBlock(text=f"{READING_PREFIX}1):\nstate-line")
    event = SimpleNamespace(
        type="user/message",
        time=42.0,
        data={"source": MessageSource(kind="plugin", plugin=NAME),
              "content": (block,)},
    )
    agent = SimpleNamespace(session=SimpleNamespace(events=[event]))
    state = _latest_injected_state(agent)
    assert state is not None and state["time"] == 42.0 and state["state"] == "state-line"
    # 非本插件来源 → None
    ev2 = SimpleNamespace(type="user/message", time=1.0,
                          data={"source": MessageSource(kind="plugin", plugin="other"),
                                "content": (block,)})
    assert _latest_injected_state(SimpleNamespace(session=SimpleNamespace(events=[ev2]))) is None
    # 无消息 → None
    assert _latest_injected_state(SimpleNamespace(session=SimpleNamespace(events=[]))) is None
    print("  ✓ _latest_injected_state 正确")


async def main() -> None:
    print("== test_tmux_context ==")
    await test_render_state_and_reading()
    await test_validate_refresh_interval()
    await test_time_now()
    await test_query_location()
    await test_latest_injected_state()
    print("OK: tmux-context 契约单测通过")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
