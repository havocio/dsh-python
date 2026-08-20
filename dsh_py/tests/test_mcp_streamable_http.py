"""MCP streamable-http 传输端到端测试（对标 stdio 端到端，补全 http 路由）。

用 ``MockStreamableHttpServer``（标准库零依赖脚手架）起一个真实 HTTP 服务，
驱动 dsh_py 的 ``StreamableHttpTransport`` + ``McpClient``，验证：
- 握手（initialize 拿到 serverInfo + mcp-session-id）
- 工具列表拉取（tools/list）
- 工具调用（tools/call：正常文本回流 + isError 回流）
- GET 通知流能收到 server 推送的 notifications/tools/list_changed
- 插件集成：apply_mcp + streamable-http 配置把工具注册进 ctx.tools 并可调用

直接运行：``python -m dsh_py.tests.test_mcp_streamable_http``
"""

from __future__ import annotations

import asyncio

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.plugins.mcp_client import apply as apply_mcp
from dsh_py.plugins.mcp_client.bridge import extract_text
from dsh_py.plugins.mcp_client.client import McpClient, StreamableHttpTransport
from dsh_py.tests.mcp_streamable_http_server import MockStreamableHttpServer


async def _wait_tool(ctx, name: str, timeout: float = 5.0) -> bool:
    """轮询等待工具注册（apply 后台异步完成首次连接）。"""
    for _ in range(int(timeout / 0.05)):
        if ctx.tools.has(name):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_http_transport_connect_and_call():
    server = MockStreamableHttpServer()
    server.start()
    transport = StreamableHttpTransport(url=server.url)
    client = McpClient(transport)
    try:
        info = await client.connect()
        assert info["serverInfo"]["name"] == "mock-streamable-http"
        assert transport._session_id is not None, "握手后应拿到 mcp-session-id"

        tools, _ = await client.list_tools()
        names = [t["name"] for t in tools]
        assert "echo" in names

        res = await client.call_tool("echo", {"text": "你好"})
        text = extract_text(res["content"], "echo")
        assert text == "echo: 你好"

        res = await client.call_tool("boom", {})
        assert res.get("isError") is True
    finally:
        await client.close()
        server.stop()
    print("  ✓ http 传输：握手→list→call（正常/isError 回流）")


async def test_http_get_notification_delivered():
    """验证 GET 通知流能送达 server 推送的 tools/list_changed。"""
    server = MockStreamableHttpServer()
    server.start()
    transport = StreamableHttpTransport(url=server.url)
    client = McpClient(transport)
    event = asyncio.Event()

    async def on_changed(_params: dict) -> None:
        event.set()

    try:
        client.on_notification("notifications/tools/list_changed", on_changed)
        await client.connect()  # initialize 时 server 排入 list_changed；握手后 GET 流建立
        await asyncio.wait_for(event.wait(), timeout=5.0)
    finally:
        await client.close()
        server.stop()
    print("  ✓ http GET 通知流：收到 tools/list_changed")


async def test_http_apply_integration():
    """验证 apply_mcp + streamable-http 配置把工具注册进 ctx.tools 并可调用。"""
    server = MockStreamableHttpServer()
    server.start()
    config = {
        "transport": "streamable-http",
        "serverName": "httpmock1",
        "url": server.url,
        "headers": {},
        "toolCallTimeoutMs": 5000,
    }
    ctx = AppContext()
    load_profile(ctx, [*CORE_PROFILE, {"apply": apply_mcp, "config": config}])
    try:
        assert await _wait_tool(ctx, "mcp__httpmock1__echo"), "echo 工具应注册"
        text, is_error = await ctx.tools.execute(
            "mcp__httpmock1__echo", '{"text": "你好"}')
        assert is_error is False and text == "echo: 你好"
    finally:
        ctx.dispose()
        server.stop()
    print("  ✓ http 插件集成：apply→工具注册→调用")


async def main():
    print("== test_mcp_streamable_http ==")
    await test_http_transport_connect_and_call()
    await test_http_get_notification_delivered()
    await test_http_apply_integration()
    print("OK: MCP streamable-http 端到端测试通过")


if __name__ == "__main__":
    asyncio.run(main())
