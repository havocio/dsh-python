"""WebSocket 网关测试：transport 单测 + 真实 WS 端到端。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from typing import Any, Optional

from dsh_py.api.protocol import JsonRpcResponseError
from dsh_py.api.websocket import JsonRpcWebSocketTransport, WebSocketGatewayServer
from dsh_py.cli import MockAdapter
from dsh_py.config import AppConfig
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- #
# transport 单测（不依赖真实 WS，用内存 send）
# --------------------------------------------------------------------------- #
async def test_ws_transport_dispatch():
    sent: list[dict] = []
    pending_responses: dict[str, asyncio.Future] = {}

    async def send(raw: str) -> None:
        frame = json.loads(raw)
        sent.append(frame)
        if "id" in frame and "method" not in frame:
            future = pending_responses.pop(str(frame["id"]), None)
            if future is not None and not future.done():
                future.set_result(frame)

    transport = JsonRpcWebSocketTransport(send=send)
    requests: list[tuple[str, dict]] = []
    transport.on_request(lambda m, p: _echo(requests, m, p))
    # 请求 → 响应（模拟对端：先收到我们的请求帧，再回 result 帧）
    task = asyncio.ensure_future(transport.request("ping", {"x": 1}))
    await asyncio.sleep(0.05)
    request_id = sent[-1]["id"]
    transport.on_message(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping", "params": {"x": 1}}))
    transport.on_message(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": "pong"}))
    assert await asyncio.wait_for(task, timeout=1) == "pong"
    assert requests == [("ping", {"x": 1})]
    # 对端 error 帧 → JsonRpcResponseError
    task = asyncio.ensure_future(transport.request("m", {}))
    await asyncio.sleep(0.05)
    request_id = sent[-1]["id"]
    transport.on_message(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "m", "params": {}}))
    transport.on_message(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                     "error": {"code": -32000, "message": "boom", "data": {"k": 1}}}))
    try:
        await asyncio.wait_for(task, timeout=1)
        raise AssertionError("应抛 JsonRpcResponseError")
    except JsonRpcResponseError as exc:
        assert exc.code == -32000 and exc.data == {"k": 1}
    # 通知 → handler
    notifications: list[tuple[str, dict]] = []
    transport.on_notification(lambda m, p: notifications.append((m, p)))
    transport.on_message(json.dumps({"jsonrpc": "2.0", "method": "n", "params": {"y": 2}}))
    await asyncio.sleep(0.05)
    assert notifications == [("n", {"y": 2})]
    # 畸形帧忽略（requests 数量不变）
    before = len(requests)
    transport.on_message("{not json")
    await asyncio.sleep(0.05)
    assert len(requests) == before
    # notify 输出
    transport.notify("evt", {"a": 1})
    await asyncio.sleep(0.05)
    assert any(f.get("method") == "evt" for f in sent), sent
    await transport.close()
    print("  ✓ WS transport：请求/响应/error/通知/畸形帧忽略")


async def _echo(requests: list, method: str, params: dict) -> str:
    requests.append((method, params))
    return "pong"


# --------------------------------------------------------------------------- #
# 端到端：真实 WebSocket 服务器 + 客户端
# --------------------------------------------------------------------------- #
def _mock_ctx() -> AppContext:
    ctx = AppContext()
    ctx.provide("appConfig", AppConfig({}))
    load_profile(ctx, [*CORE_PROFILE])
    ctx.llm.register_adapter(["deepseek-official"], MockAdapter(), replace=True)
    return ctx


async def test_gateway_end_to_end():
    ctx = _mock_ctx()
    gateway = WebSocketGatewayServer(ctx)
    port = _free_port()

    import websockets

    async def handler(ws: Any) -> None:
        await gateway.handle_connection(ws)

    server = await websockets.serve(handler, "127.0.0.1", port, max_size=1 << 20)
    try:
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri) as ws:
            # initialize
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": "1", "method": "initialize",
                                      "params": {"cwd": PROJECT_ROOT, "provider": "deepseek-official",
                                                 "model": "deepseek-v4-flash"}}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["result"]["serverInfo"]["name"] == "deepseek-harness-sdk-runtime"
            # session/prompt → 先收响应，再收事件 + status 通知流
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": "2", "method": "session/prompt",
                                      "params": {"sessionId": "ws-s1",
                                                 "contentBlocks": [{"type": "text", "text": "你好"}]}}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["id"] == "2" and "messageId" in frame["result"], frame
            seen_status: list[str] = []
            events: list = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                notification = json.loads(raw)
                assert "id" not in notification  # 服务器主动推的应是通知
                if notification["method"] == "session.event" and notification["params"]["sessionId"] == "ws-s1":
                    events.append(notification["params"]["event"])
                if notification["method"] == "session.status" and notification["params"]["sessionId"] == "ws-s1":
                    seen_status.append(notification["params"]["status"])
                    if seen_status[-1] == "idle":
                        break
            assert "running" in seen_status and "idle" in seen_status, seen_status
            assert any(e.get("type") == "assistant/message" for e in events), events
            # shutdown
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": "3", "method": "shutdown", "params": {}}))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["id"] == "3" and frame["result"] == {}
        # 断开后服务器异步清理连接（轮询等待）
        for _ in range(50):
            if gateway.connection_count() == 0:
                break
            await asyncio.sleep(0.05)
        assert gateway.connection_count() == 0  # 断开后清理
        print("  ✓ 端到端：WS 连接 → initialize/prompt/事件流/shutdown → 清理")
    finally:
        server.close()
        await server.wait_closed()
        await gateway.close()
        ctx.dispose()


async def test_gateway_unknown_method_and_two_connections():
    ctx = _mock_ctx()
    gateway = WebSocketGatewayServer(ctx)
    port = _free_port()

    import websockets

    async def handler(ws: Any) -> None:
        await gateway.handle_connection(ws)

    server = await websockets.serve(handler, "127.0.0.1", port, max_size=1 << 20)
    try:
        uri = f"ws://127.0.0.1:{port}"
        # 双连接并发
        async with websockets.connect(uri) as ws1, websockets.connect(uri) as ws2:
            assert gateway.connection_count() == 2
            # 未知方法 → -32603
            await ws1.send(json.dumps({"jsonrpc": "2.0", "id": "x", "method": "nope"}))
            frame = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
            assert frame["error"]["code"] == -32603, frame
            # 双连接各自 initialize 成功
            for ws in (ws1, ws2):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": "i",
                                          "method": "initialize",
                                          "params": {"cwd": PROJECT_ROOT, "provider": "deepseek-official"}}))
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert frame["result"]["serverInfo"]["name"] == "deepseek-harness-sdk-runtime"
            # 事件广播：ws1 发 prompt，ws2 也能收到 session.event（共享 harness 广播）
            await ws1.send(json.dumps({"jsonrpc": "2.0", "id": "p", "method": "session/prompt",
                                       "params": {"sessionId": "ws-s2",
                                                  "contentBlocks": [{"type": "text", "text": "hi"}]}}))
            # 先等 ws1 的响应，再等 ws2 收到广播事件
            frame = json.loads(await asyncio.wait_for(ws1.recv(), timeout=5))
            assert frame["id"] == "p" and "messageId" in frame["result"]
            got_broadcast = False
            for _ in range(100):
                raw = await asyncio.wait_for(ws2.recv(), timeout=5)
                notification = json.loads(raw)
                if notification["method"] == "session.event" and notification["params"]["sessionId"] == "ws-s2":
                    got_broadcast = True
                    break
            assert got_broadcast, "ws2 应收到 ws1 会话的事件广播"
        for _ in range(50):
            if gateway.connection_count() == 0:
                break
            await asyncio.sleep(0.05)
        assert gateway.connection_count() == 0
        print("  ✓ 多连接：并发连接 + 未知方法 -32603 + 事件跨连接广播")
    finally:
        server.close()
        await server.wait_closed()
        await gateway.close()
        ctx.dispose()


async def main():
    print("== test_gateway ==")
    await test_ws_transport_dispatch()
    await test_gateway_end_to_end()
    await test_gateway_unknown_method_and_two_connections()
    print("OK: WebSocket 网关测试通过")


if __name__ == "__main__":
    asyncio.run(main())
