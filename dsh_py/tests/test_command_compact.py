"""command-compact 测试：commands 注册表（register/invoke）、``/compact`` 命令
成功路径、无历史、参数拒绝、六类预期失败映射、端到端手工压缩。

运行：``python dsh_py/tests/test_command_compact.py``
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext
from dsh_py.core.signal import CancelSignal
from dsh_py.plugins import command_compact as CC
from dsh_py.services import agent as A
from dsh_py.services import commands as CM
from dsh_py.services import compaction as CP
from dsh_py.services import compaction_basic as CB
from dsh_py.services import llm as L
from dsh_py.services import session as S
from dsh_py.services import token_meter as TM
from dsh_py.services.message import (
    MessageSource,
    TextBlock,
    create_assistant_message,
    create_user_message,
)


class _RoundAdapter(L.LlmAdapter):
    async def resolve_model(self, provider, model):
        return {"provider": provider, "id": model, "name": model,
                "context": {"context_window": 1000}}

    async def stream(self, options):
        if options.purpose == "compaction":
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text="## Primary Request and Intent\n- 测试\n## Next Step\n- 继续")
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})
        else:
            yield L.StreamChunk(L.ChunkType.BLOCK_START, block_type="text", index=0)
            yield L.StreamChunk(L.ChunkType.TEXT_DELTA, text="工作内容" * 50)
            yield L.StreamChunk(L.ChunkType.BLOCK_END, index=0, block=None)
            yield L.StreamChunk(L.ChunkType.FINISH, finish={"kind": "stop"})


async def _invoke(ctx, name, agent=None, raw_input="", signal=None):
    """按现行 API 执行一条命令（``execute(agent, line, signal)``）。

    现行语义：语法/名字未命中时 ``execute`` 返回 ``None``（不写任何日志），
    故此处解包 ``.result`` 并原样透传 ``None`` 供调用方断言。
    """
    line = f"/{name}" + (f" {raw_input}" if raw_input else "")
    execution = await ctx.commands.execute(agent, line, signal or CancelSignal())
    return execution.result if execution is not None else None


def _ctx() -> AppContext:
    ctx = AppContext()
    CM.apply(ctx)
    S.apply(ctx)
    TM.apply(ctx)
    L.apply(ctx)
    ctx.llm.register_adapter(["mock"], _RoundAdapter())
    CB.apply(ctx, {"summarizationProvider": "mock", "summarizationModel": "m"})
    CC.apply(ctx)
    A.apply_registry(ctx)
    A.apply_loop(ctx)
    return ctx


def _populated_agent(ctx: AppContext):
    session = ctx.sessions.create()
    session.request_header = {"config": {"provider": "mock", "model": "m"}, "system": None, "tools": None}
    for i in range(4):
        session.append("user/message", create_user_message([TextBlock("你好")], MessageSource("user")))
        session.append("assistant/message", {
            "turn": i + 1, "step": 1,
            "message": create_assistant_message([TextBlock("工作内容" * 50)], provider="mock", model="m"),
        })
    return ctx.agents.create_agent(session, A.AgentOptions(provider="mock", model="m"))


# --------------------------------------------------------------------------- #
# 1. commands 注册表
# --------------------------------------------------------------------------- #
def test_commands_registry() -> None:
    ctx = AppContext()
    CM.apply(ctx)
    S.apply(ctx)  # execute 需真实会话（写 command/run + command/done）
    assert ctx.commands.list(None) == []

    async def handler(invocation):
        return CM.CommandResult(kind="success", text=f"echo:{invocation.rawInput}")

    ctx.commands.register(CM.CommandDefinition(name="echo", description="回显", handler=handler))
    assert ctx.commands.find(None, "echo") is not None
    assert ctx.commands.list(None)[0].name == "echo"

    async def main() -> None:
        # execute 把生命周期事件写进 agent.session，故宿主需持有真实会话。
        host = type("H", (), {"session": ctx.sessions.create()})()
        result = await _invoke(ctx, "echo", host, "hi")
        # parseCommand 不做尾部输入归一化：rawInput 保留命令名后的分隔空格
        assert result.kind == "success" and result.text == "echo: hi"
        # 未知命令：execute 返回 None（不写任何日志）
        result = await _invoke(ctx, "nope", host)
        assert result is None, "未命中命令应返回 None"
        # handler 未捕获的异常向上传播（命令自身负责把预期失败映射为 error result）
        def bad(invocation):
            raise RuntimeError("boom")

        ctx.commands.register(CM.CommandDefinition(name="bad", description="坏命令", handler=bad))
        try:
            await _invoke(ctx, "bad", host)
        except RuntimeError as exc:
            assert "boom" in str(exc), exc
        else:
            raise AssertionError("handler 异常应传播给调用方")

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 2. /compact 成功路径
# --------------------------------------------------------------------------- #
def test_compact_command_success() -> None:
    async def main() -> None:
        ctx = _ctx()
        agent = _populated_agent(ctx)
        result = await _invoke(ctx, "compact", agent, signal=CancelSignal())
        assert result.kind == "success"
        assert "已压缩" in result.text and "tokens" in result.text
        assert result.sourceEventSeq is not None
        # 表面确实被替换
        assert agent.session.surface["replace_generation"] >= 1
        types = [e.type for e in agent.session.events]
        assert "compaction/summary" in types

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 3. 无历史 / 参数拒绝
# --------------------------------------------------------------------------- #
def test_compact_command_edge_cases() -> None:
    async def main() -> None:
        ctx = _ctx()
        # 空会话 → 无可用历史
        agent = ctx.agents.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"))
        result = await _invoke(ctx, "compact", agent, signal=CancelSignal())
        assert result.kind == "success" and "尚无" in result.text
        # 带参数 → 用法错误
        agent2 = _populated_agent(ctx)
        result = await _invoke(ctx, "compact", agent2, "extra args", CancelSignal())
        assert result.kind == "error" and "用法" in result.text

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 4. 六类预期失败映射
# --------------------------------------------------------------------------- #
def test_compact_command_failure_mapping() -> None:
    async def main() -> None:
        ctx = AppContext()
        CM.apply(ctx)
        S.apply(ctx)
        TM.apply(ctx)
        L.apply(ctx)
        A.apply_registry(ctx)
        A.apply_loop(ctx)
        CB.apply(ctx, {"summarizationProvider": "mock", "summarizationModel": "m"})
        CC.apply(ctx)
        agent = ctx.agents.create_agent(ctx.sessions.create(), A.AgentOptions(provider="mock", model="m"))

        # 用假压缩后端替换：compactNow 抛不同 ManualCompactionError
        class FakeEngine(CP.CompactionEngine):
            def __init__(self, ctx, code: str) -> None:
                super().__init__(ctx)
                self._code = code

            async def compact_if_needed(self, agent, trigger, signal=None):
                return None

            async def compact_now(self, agent, signal=None, source_command_id=None):
                raise CP.ManualCompactionError(self._code, f"fail-{self._code}", None)

            async def compact_region(self, start, end, agent, signal=None):
                raise NotImplementedError

        for code in ("busy", "cancelled", "changed", "summary", "commit", "persistence"):
            ctx.provide("compaction", FakeEngine(ctx, code))
            result = await _invoke(ctx, "compact", agent, signal=CancelSignal())
            assert result.kind == "error", f"{code} 应映射为错误"
            assert result.text, f"{code} 应有文案"
            # 取消优先级：signal 已中止 → cancelled 文案
        ctx.provide("compaction", FakeEngine(ctx, "busy"))
        cancelled = CancelSignal()
        cancelled.abort("cancelled-by-user")
        # 取消优先级：signal 已中止 → execute 抛出中止错误（不再结算为 error result）
        try:
            await _invoke(ctx, "compact", agent, signal=cancelled)
        except RuntimeError as exc:
            assert "cancelled" in str(exc), exc
        else:
            raise AssertionError("已中止的 signal 应让命令执行抛出")

    asyncio.run(main())


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main() -> None:
    tests = [
        test_commands_registry,
        test_compact_command_success,
        test_compact_command_edge_cases,
        test_compact_command_failure_mapping,
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
