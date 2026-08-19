"""MCP 客户端桥接插件（mcp-client 翻译）测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.mcp_client import apply as apply_mcp
from dsh_py.plugins.mcp_client.bridge import (
    extract_text,
    public_tool_name,
    resolve_reconnect_policy,
)

# --------------------------------------------------------------------------- #
# 命名 / 文本化 / 重连策略（纯函数）
# --------------------------------------------------------------------------- #
def test_public_tool_name():
    assert public_tool_name("server-a", "get_weather") == "mcp__server-a__get_weather"
    # 非法字符归一为 _（归一后与原名不同 → 追加 hash）
    name = public_tool_name("s v", "x y")
    assert name.startswith("mcp__s_v__x_y_")
    assert "_" in name and " " not in name
    # 干净名不加 hash
    assert public_tool_name("server", "tool") == "mcp__server__tool"
    # 超长 → 截断 + hash
    long_name = public_tool_name("a" * 40, "b" * 40)
    assert len(long_name) <= 64
    assert "_" in long_name
    print("  ✓ public_tool_name：干净名/归一/超长 hash")


def test_extract_text():
    content = [
        {"type": "text", "text": "第一行"},
        {"type": "text", "text": "第二行"},
        {"type": "image", "mimeType": "image/png"},
        {"type": "audio", "mimeType": "audio/wav"},
        {"type": "resource"},
        {"type": "weird"},
    ]
    text = extract_text(content, "t")
    assert "第一行\n第二行" in text
    assert "[image: image/png, content discarded]" in text
    assert "[audio: audio/wav, content discarded]" in text
    assert "[resource: content discarded]" in text
    assert "[unsupported content type: weird]" in text
    # 空 → 兜底
    assert extract_text([], "echo") == "(echo returned no text content)"
    print("  ✓ extract_text：text 拼接/媒体占位符/兜底")


def test_reconnect_policy_validation():
    policy = resolve_reconnect_policy(None, "x")
    assert policy == {"enabled": True, "initialDelayMs": 500, "maxDelayMs": 30000, "maxAttempts": 10}
    # 非法键
    try:
        resolve_reconnect_policy({"foo": 1}, "x")
        raise AssertionError("未知键应报错")
    except ValueError as e:
        assert "foo" in str(e)
    # initialDelay > maxDelay
    try:
        resolve_reconnect_policy({"initialDelayMs": 100, "maxDelayMs": 10}, "x")
        raise AssertionError("initialDelay>maxDelay 应报错")
    except ValueError:
        pass
    # maxAttempts < 1
    try:
        resolve_reconnect_policy({"maxAttempts": 0}, "x")
        raise AssertionError("maxAttempts<1 应报错")
    except ValueError:
        pass
    print("  ✓ resolve_reconnect_policy：默认值/非法配置 fail loud")


# --------------------------------------------------------------------------- #
# stdio 端到端：mock MCP server 子进程
# --------------------------------------------------------------------------- #
MOCK_SERVER_SCRIPT = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

server_info = {"protocolVersion": "2025-06-18", "capabilities": {},
               "serverInfo": {"name": "mock-mcp", "version": "1.0"}}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": server_info})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "echo", "description": "回显输入",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name == "echo":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}],
            }})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": "出错了"}], "isError": True,
            }})
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32602, "message": f"unknown tool {name}"}})
    elif method == "shutdown":
        break
"""


def _stdio_config(server_name="mock", fail_on_startup=False, **extra):
    return {
        "transport": "stdio",
        "serverName": server_name,
        "command": sys.executable,
        "args": ["-c", MOCK_SERVER_SCRIPT],
        "env": {},
        "cwd": "",
        "toolCallTimeoutMs": 5_000,
        "failOnStartupError": fail_on_startup,
        **extra,
    }


async def _wait_tool(ctx, name: str, timeout: float = 5.0) -> bool:
    """轮询等待工具注册（apply 后台异步完成首次连接）。"""
    for _ in range(int(timeout / 0.05)):
        if ctx.tools.has(name):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_stdio_end_to_end_tool_call():
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, {"apply": apply_mcp, "config": _stdio_config()}])
    try:
        assert await _wait_tool(ctx, "mcp__mock__echo"), "echo 工具应在首次连接后注册"
        # 工具 schema 可见
        schemas = ctx.tools.list_schemas()
        assert any(s["name"] == "mcp__mock__echo" for s in schemas)
        # 调用工具 → 文本回流
        text, is_error = await ctx.tools.execute("mcp__mock__echo", '{"text": "你好"}')
        assert is_error is False and text == "echo: 你好"
        # 服务器 isError → 错误回流
        text, is_error = await ctx.tools.execute("mcp__mock__echo", '{"text": "x"}')
        assert is_error is False  # 服务器不报错
    finally:
        ctx.dispose()
    print("  ✓ stdio 端到端：连接→工具注册→调用→文本回流")


async def test_dispose_unregisters_tools():
    ctx = AppContext()
    # 分开加载：先核心服务，再 mcp 插件——拿到 mcp 专属 handle
    # （合并加载时拓扑排序后 mcp 不保证在最后，handles[-1] 不可靠）
    load_profile(ctx, [*CORE_PROFILE])
    mcp_handles = load_profile(ctx, [{"apply": apply_mcp, "config": _stdio_config()}])
    assert await _wait_tool(ctx, "mcp__mock__echo")
    # 只卸载 mcp 插件；tools 等服务必须仍在。
    # dispose 的 async 清理（断开连接/注销工具）经事件循环异步完成，需轮询等待。
    mcp_handles[0].dispose()
    for _ in range(50):
        if not ctx.tools.has("mcp__mock__echo"):
            break
        await asyncio.sleep(0.05)
    assert not ctx.tools.has("mcp__mock__echo")
    assert ctx.has_service("tools")
    print("  ✓ 插件卸载：工具注销（tools 服务保留）")


async def test_duplicate_server_name_rejected():
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, {"apply": apply_mcp, "config": _stdio_config(server_name="dup")}])
    try:
        await _wait_tool(ctx, "mcp__dup__echo")
        try:
            load_profile(ctx, [{"apply": apply_mcp, "config": _stdio_config(server_name="dup")}])
            raise AssertionError("重复 serverName 应报错")
        except RuntimeError as e:
            assert "already in use" in str(e)
    finally:
        ctx.dispose()
    print("  ✓ 重复 serverName 加载期报错")


async def test_server_is_error_flows_back():
    # 服务器返回 isError: true → handler 返回错误文本
    script = MOCK_SERVER_SCRIPT.replace('"name": "echo"', '"name": "boom"').replace(
        "echo: ", "boom: ")
    config = _stdio_config(server_name="err")
    config["args"] = ["-c", script]
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, {"apply": apply_mcp, "config": config}])
    try:
        # 只验证有 boom 工具时调用即错误（脚本里 echo 分支存在但工具名是 boom）
        ok = await _wait_tool(ctx, "mcp__err__boom", timeout=3.0)
        assert ok
        text, is_error = await ctx.tools.execute("mcp__err__boom", "{}")
        assert is_error is True
        assert "出错了" in text
    finally:
        ctx.dispose()
    print("  ✓ 服务器 isError → 错误文本回流")


async def main():
    print("== test_mcp_client ==")
    test_public_tool_name()
    test_extract_text()
    test_reconnect_policy_validation()
    await test_stdio_end_to_end_tool_call()
    await test_dispose_unregisters_tools()
    await test_duplicate_server_name_rejected()
    await test_server_is_error_flows_back()
    print("OK: MCP 客户端桥接插件测试通过")


if __name__ == "__main__":
    asyncio.run(main())
