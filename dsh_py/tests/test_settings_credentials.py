"""settings + credentials 的验证（第 3 层第四批）。

运行：python dsh_py/tests/test_settings_credentials.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.agent import AgentOptions, apply_loop
from dsh_py.services.credentials import CredentialRefError, apply as apply_credentials, credential_ref
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk
from dsh_py.services.message import TextBlock, ToolCallBlock, ToolResultBlock
from dsh_py.services.settings import apply as apply_settings, install_settings_section, settings_namespace


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def test_settings_scope_get_set_watch():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.settings:apply"])
    ns = settings_namespace("demo")
    scope = ctx.settings.register(ns, z.object({"limit": z.integer().default(1)}),
                                  {"base": {"limit": 1}})
    assert ctx.settings.get(ns) == {"limit": 1}

    changed = []
    scope.watch(lambda: changed.append(ctx.settings.get(ns)))
    ctx.settings.set(ns, {"limit": 4})
    assert ctx.settings.get(ns) == {"limit": 4}
    assert changed == [{"limit": 4}]

    # schema 校验：非法值被拒
    try:
        ctx.settings.set(ns, {"limit": "不是整数"})
    except Exception as e:
        assert "整数" in str(e)
    else:  # pragma: no cover
        raise AssertionError("非法设置应被 schema 拒绝")
    assert ctx.settings.get(ns) == {"limit": 4}  # 值未变


def test_install_settings_section_hooks():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.settings:apply"])
    ns = settings_namespace("consumer")
    entry = {"max_parallel_tool_calls": 1}
    source = {"thunk": lambda: entry}

    install_settings_section(ctx, ns, z.object({"max_parallel_tool_calls": z.integer().default(1)}),
                             entry, {
                                 "set_source": lambda current: source.update({"thunk": current}),
                                 "on_change": lambda: None,
                             })
    # 初始指向组合条目
    assert source["thunk"]() == {"max_parallel_tool_calls": 1}
    # 用户修改设置 → thunk 读到新值
    ctx.settings.set(ns, {"max_parallel_tool_calls": 8})
    assert source["thunk"]() == {"max_parallel_tool_calls": 8}


def test_install_settings_section_without_service_skips():
    ctx = AppContext()
    # 无 settings 服务 → install 直接跳过（不抛错）
    install_settings_section(ctx, settings_namespace("x"), None, {"a": 1},
                             {"set_source": lambda c: None, "on_change": lambda: None})


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def test_credential_ref_validation():
    assert credential_ref("DEEPSEEK_API_KEY") == "DEEPSEEK_API_KEY"
    assert credential_ref("_ok_1") == "_ok_1"
    for bad in ("bad-key", "1abc", "with space", ""):
        try:
            credential_ref(bad)
        except CredentialRefError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"非法引用应报错：{bad!r}")


async def test_credentials_resolve_env_and_store():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.credentials:apply"])
    os.environ["DSH_TEST_CRED"] = "from-env"
    # 解析：先环境变量
    resolved = await ctx.credentials.resolve("DSH_TEST_CRED")
    assert resolved == {"value": "from-env", "source": "env"}
    # 显式写入 → store 优先
    await ctx.credentials.set("DSH_TEST_CRED", "from-store")
    assert (await ctx.credentials.resolve("DSH_TEST_CRED"))["source"] == "store"
    # 不存在 → None
    assert await ctx.credentials.resolve("DSH_NO_SUCH_CRED") is None
    # describe
    info = await ctx.credentials.describe("DSH_TEST_CRED")
    assert info["available"] is True and info["source"] == "store"
    # delete 后回落 env
    assert await ctx.credentials.delete("DSH_TEST_CRED") is True
    assert (await ctx.credentials.resolve("DSH_TEST_CRED"))["source"] == "env"


async def test_credentials_updated_event():
    ctx = AppContext()
    load_profile(ctx, ["dsh_py.services.credentials:apply"])
    seen = []
    ctx.on("credentials/updated", lambda ref: seen.append(ref))
    await ctx.credentials.set("X_KEY", "v")
    await ctx.credentials.delete("X_KEY")
    assert seen == ["X_KEY", "X_KEY"]


# --------------------------------------------------------------------------- #
# agent-loop × settings：运行时改并发上限即时生效
# --------------------------------------------------------------------------- #
class _TwoCallAdapter(LlmAdapter):
    async def stream(self, options):
        if any(isinstance(b, ToolResultBlock) for m in options.messages for b in m.content):
            yield StreamChunk(ChunkType.TEXT_DELTA, text="done")
            yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})
            return
        for i in (0, 1):
            yield StreamChunk(ChunkType.BLOCK_START, index=i, block_type="tool-call")
            yield StreamChunk(ChunkType.TOOL_CALL_DELTA, index=i, tool_call_id=f"c{i}",
                              tool_call_name=f"tool_{i}", arguments_delta="{}")
            yield StreamChunk(ChunkType.BLOCK_END, index=i,
                              block=ToolCallBlock(id=f"c{i}", name=f"tool_{i}", arguments="{}"))
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "tool-calls"})


async def test_agent_loop_settings_runtime_change():
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, "dsh_py.services.settings:apply"])
    apply_loop(ctx, {"agents": [], "max_parallel_tool_calls": 1})
    ctx.llm.register_adapter(["mock"], _TwoCallAdapter())
    ctx.tools.register("tool_0", "t", {}, lambda a: _ok("0"))
    ctx.tools.register("tool_1", "t", {}, lambda a: _ok("1"))

    session = ctx.sessions.create()
    agent = ctx.agents.create_agent(session, AgentOptions(provider="mock", model="m"))
    await agent.run("hi")
    # 默认串行完成（并发上限 1）
    assert _last_turn_reason(session) == {"kind": "completed"}

    # 运行时修改设置：并发上限 2（settings 命名空间）
    ctx.settings.set(settings_namespace("agent-loop"), {"max_parallel_tool_calls": 2})
    assert ctx.agentLoop.current_parallel_limit() == 2

    # 新一轮仍正常工作（设置读取不报错）
    session2 = ctx.sessions.create()
    agent2 = ctx.agents.create_agent(session2, AgentOptions(provider="mock", model="m"))
    await agent2.run("hi again")
    assert _last_turn_reason(session2) == {"kind": "completed"}


async def _ok(tag):
    return f"ok-{tag}", False


def _last_turn_reason(session):
    ends = [e for e in session.events if e.type == "turn/end"]
    return ends[-1].data["reason"] if ends else None


async def main():
    test_settings_scope_get_set_watch()
    test_install_settings_section_hooks()
    test_install_settings_section_without_service_skips()
    test_credential_ref_validation()
    await test_credentials_resolve_env_and_store()
    await test_credentials_updated_event()
    await test_agent_loop_settings_runtime_change()
    print("OK: settings + credentials 测试通过（设置区读写/watch、接线 hooks、凭据解析/事件、agent-loop 运行时设置）")


if __name__ == "__main__":
    asyncio.run(main())
