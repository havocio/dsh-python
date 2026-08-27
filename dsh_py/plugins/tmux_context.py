"""可选启用的请求准备（request-preparation）tmux 位置上下文。

每个回合的第一步（``step === 1``）通过 ``ctx.shell`` 执行器运行一次
``tmux display-message``，把本 Agent 进程所在的 tmux 会话 / 窗口 / 面板，连同窗口的
面板树布局，作为带来源归属的耐久快照注入对话。

插件通过比对 ``$TMUX_PANE`` 所指向面板的 ``#{pane_tty}`` 与本进程的 Controlling
Terminal，确认本进程确实运行在该面板内——避免「从 tmux 祖先继承 ``$TMUX``/
``$TMUX_PANE``」的终端（如 VS Code 集成终端）被误判为「在 tmux 内」。仅当渲染出的
tmux 状态相对上次注入发生变化（移动、改名、改变布局）时才重新注入，并支持可选的
``refreshIntervalMs`` 注入下限。

缺少 tmux 环境、继承型环境、未挂载 ``ctx.shell``、或查询失败时一律 no-op，绝不报错：
执行器拒绝会被捕获并以 warning 记录，回合照常继续。

对标 dsh 的 ``@deepseek-ai/dsh-tmux-context``。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from dsh_py.services.message import MessageSource, TextBlock, create_user_message

# 包名，供加载器诊断与消息来源标记
NAME = "tmux-context"

# 每次查询的 tmux 格式字段（按查询顺序排列）。布局（window_layout）是面板树描述；
# 面板/窗口的像素尺寸刻意排除（对标包范围：仅本机位置与布局）。
TMUX_FIELDS = [
    "#{session_name}",
    "#{window_index}",
    "#{window_name}",
    "#{pane_index}",
    "#{pane_id}",
    "#{window_active}",
    "#{pane_active}",
    "#{window_layout}",
]

# 一次 display-message 读取的最后结构化位置
TmuxLocation = dict[str, str]

# 渲染读数的易变回合前导语行前缀
READING_PREFIX = "tmux location (turn "

# 字段分隔符。tmux 不会解释格式串中的 C 转义，所以字面量两字符序列 '\t'
# 会被原样输出，这里再按它切分；这样避免了在命令里嵌入原始空白字符。
FIELD_SEP = "\\t"


def _query_tmux_location(
    bash: Any,
    logger: Any,
    process_id: int,
    signal: Any,
) -> Optional[TmuxLocation]:
    """通过 shell 执行器读取本进程的 tmux 位置；不在真实面板内或查询失败时返回 ``None``。

    ``$TMUX_PANE`` 单独不足判定：从 tmux shell 启动的终端（如 VS Code 集成终端、
    桌面启动器）会从祖先继承 ``$TMUX`` 与 ``$TMUX_PANE``，变量存在但本进程并不在该
    面板里。因此命令还会把面板的 ``#{pane_tty}`` 与本进程自身的 controlling terminal
    （``ps -o tty=`` 查 ``process_id``）比对；只有匹配才输出字段，继承型环境读为
    「不在 tmux」，不注入任何内容。

    位置信息是可选上下文，所以执行器拒绝是「查询失败」而非「回合失败」：``bash`` 的
    ``execute`` 可能在策略上拒绝命令，调用方承诺对非零退出、超时、取消都给出结果，故二者
    都被容纳并以 warning 上报。

    :param bash: 用于运行只读 tmux/ps 命令的执行器。
    :param logger: 执行器拒绝查询时记录 warning。
    :param process_id: 本 Agent 进程 pid，其 controlling tty 必须与面板一致。
    :param signal: 转发给执行器的取消信号。
    :returns: 解析出的位置，或不在真实面板 / 任何失败时返回 ``None``。
    """
    fmt = FIELD_SEP.join(TMUX_FIELDS)
    command = "\n".join([
        '[ -n "$TMUX_PANE" ] || exit 1',
        f'self_tty=$(ps -o tty= -p {process_id} | tr -d " ")',
        '[ -n "$self_tty" ] || exit 1',
        'pane_tty=$(tmux display-message -t "$TMUX_PANE" -p \'#{pane_tty}\') || exit 1',
        '[ "$pane_tty" = "/dev/$self_tty" ] || exit 1',
        f'exec tmux display-message -t "$TMUX_PANE" -p \'{fmt}\'',
    ])
    try:
        result = bash.execute(command, timeout_ms=60_000)
    except Exception as error:  # noqa: BLE001 — 执行器契约允许任意拒绝
        logger.warn(
            f"tmux location query failed: {error!r}; injecting no location this turn"
        )
        return None
    if result.get("exit_code", -1) != 0:
        return None
    stdout_text = result.get("stdout", "") or ""
    line = stdout_text.split("\n", 1)[0]
    parts = line.split(FIELD_SEP)
    if len(parts) != len(TMUX_FIELDS):
        return None
    (
        session_name,
        window_index,
        window_name,
        pane_index,
        pane_id,
        window_active,
        pane_active,
        window_layout,
    ) = parts
    if len(pane_id) == 0:
        return None
    return {
        "sessionName": session_name,
        "windowIndex": window_index,
        "windowName": window_name,
        "paneIndex": pane_index,
        "paneId": pane_id,
        "windowActive": window_active,
        "paneActive": pane_active,
        "windowLayout": window_layout,
    }


def _render_state(location: TmuxLocation) -> str:
    """渲染稳定的 tmux 状态块：变化抑制所比对的部分。

    排除回合导语，使重新注入仅由 tmux 状态而非循环位置驱动。
    """
    return (
        f"session {location['sessionName']}, "
        f"window {location['windowIndex']} {location['windowName']!r}, "
        f"pane {location['paneIndex']} {location['paneId']}\n"
        f"window active={location['windowActive']}, pane active={location['paneActive']}, "
        f"layout {location['windowLayout']}"
    )


def _render_reading(location: TmuxLocation, turn: int) -> str:
    """渲染完整耐久读数，含易变回合导语。"""
    return f"{READING_PREFIX}{turn}):\n{_render_state(location)}"


def _latest_injected_state(agent: Any) -> Optional[dict]:
    """本插件最近一次耐久注入的稳定状态块，没有则返回 ``None``。

    扫描原始耐久事件，使调度在 compaction 与恢复进程后无需进程本地缓存状态即可存活。
    """
    for event in reversed(list(agent.session.events)):
        if event.type == "user/message" and isinstance(event.data, dict):
            source = event.data.get("source")
            if isinstance(source, MessageSource) and source.kind == "plugin" and source.plugin == NAME:
                content = event.data.get("content") or ()
                block = next((b for b in content if isinstance(b, TextBlock)), None)
                if block is None:
                    return None
                newline = block.text.find("\n")
                state = "" if newline == -1 else block.text[newline + 1:]
                return {"state": state, "time": event.time}
    return None


def _validate_refresh_interval(refresh_interval_ms: Optional[int]) -> None:
    """拒绝无法精确表示「已流逝毫秒阈值」的刷新间隔。"""
    if refresh_interval_ms is not None and (
        not isinstance(refresh_interval_ms, int) or refresh_interval_ms < 0
    ):
        raise TypeError(
            f"tmux-context: refreshIntervalMs must be a non-negative integer, got {refresh_interval_ms!r}"
        )


def apply(ctx: Any, config: dict | None = None) -> None:
    """为 ``ctx`` 的整个生命周期注册一个前置 pre-step 监听器（prepend）。

    :param ctx: 插件上下文；监听器随其销毁。
    :param config: 耐久刷新调度配置（``refreshIntervalMs``）。
    :raises TypeError: 刷新间隔非法时。
    """
    config = config or {}
    refresh_interval_ms = config.get("refreshIntervalMs")
    _validate_refresh_interval(refresh_interval_ms)

    @ctx.on("agent/pre-step", prepend=True)
    async def _pre_step(payload: dict, next):  # noqa: ANN001
        decision = await next()
        signal = payload["signal"]
        if decision.get("kind") == "reject":
            return decision
        # 仅回合第一步（step === 1）才会注入
        if payload["step"] != 1:
            return decision
        if getattr(signal, "aborted", False):
            return decision
        agent = payload["agent"]
        bash = ctx.get("shell")
        if bash is None:
            return decision
        previous = _latest_injected_state(agent)
        if refresh_interval_ms is not None and refresh_interval_ms > 0 and previous is not None:
            now = time_now()
            if now >= previous["time"] and now - previous["time"] < refresh_interval_ms:
                return decision
        location = _query_tmux_location(bash, ctx.logger, os.getpid(), payload["signal"])
        if location is None:
            return decision
        state = _render_state(location)
        if previous is not None and previous["state"] == state:
            return decision
        text = _render_reading(location, payload["turn"])
        return {
            "kind": "enter",
            "messages": [
                create_user_message(
                    content=[TextBlock(text)],
                    source=MessageSource(
                        kind="plugin",
                        plugin=NAME,
                        form="snapshot",
                        sections=( {"name": NAME, "text": text}, ),
                    ),
                ),
                *decision["messages"],
            ],
        }


def time_now() -> float:
    """当前时间戳（毫秒）；独立函数便于测试替换。"""
    import time as _t
    return _t.time() * 1000.0
