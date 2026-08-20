"""SDK 跨进程协议测试：帧解析 / 服务器端到端 / 子进程客户端 / 高层 run。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncIterator, Optional

from dsh_py.api.client import (
    DeepSeekHarness,
    HarnessClient,
    SdkProtocolError,
    TransportClosedError,
)
from dsh_py.api.protocol import (
    JsonRpcLineTransport,
    JsonRpcProtocolError,
    JsonRpcResponseError,
)
from dsh_py.api.server import HarnessSdkJsonRpcServer
from dsh_py.cli import MockAdapter
from dsh_py.config import AppConfig
from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSONRPC_CMD = [sys.executable, "-m", "dsh_py.cli", "--jsonrpc", "--mock"]


# --------------------------------------------------------------------------- #
# 协议帧解析
# --------------------------------------------------------------------------- #
def _mem_transport():
    """内存管道 transport：server 收到的行与发出的行都由测试驱动。"""
    received: list[str] = []       # 注入给 reader 的行（测试发帧）
    sent: list[dict] = []          # writer 捕获的帧
    sent_futures: dict[str, asyncio.Future] = {}

    async def reader() -> Optional[str]:
        while not received:
            await asyncio.sleep(0.01)
        return received.pop(0)

    def writer(line: str) -> None:
        frame = json.loads(line)
        sent.append(frame)
        if "id" in frame and "method" not in frame:
            future = sent_futures.pop(str(frame["id"]), None)
            if future is not None and not future.done():
                future.set_result(frame)

    async def next_response(timeout: float = 2.0) -> dict:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        sent_futures["__probe__"] = future  # 占位防回收；实际按 id 匹配
        sent_futures.pop("__probe__", None)
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for idx, frame in enumerate(sent):
                if "id" in frame and "method" not in frame and frame["id"] != "__probe__":
                    return sent.pop(idx)
            await asyncio.sleep(0.01)
        raise AssertionError("no response frame within timeout")

    transport = JsonRpcLineTransport(reader=reader, writer=writer)
    return transport, received, sent


async def test_protocol_frame_dispatch():
    transport, received, sent = _mem_transport()
    requests: list[tuple[str, dict]] = []
    notifications: list[tuple[str, dict]] = []
    transport.on_request(lambda method, params: _echo(requests, method, params))
    transport.on_notification(lambda method, params: notifications.append((method, params)))
    transport.start()
    # 请求 → 响应（handler 返回值）
    received.append(json.dumps({"jsonrpc": "2.0", "id": "a1", "method": "ping", "params": {"x": 1}}))
    await asyncio.sleep(0.1)
    assert requests == [("ping", {"x": 1})]
    assert any(f["id"] == "a1" and f["result"] == "pong" for f in sent), sent
    # 通知
    received.append(json.dumps({"jsonrpc": "2.0", "method": "n", "params": {"y": 2}}))
    await asyncio.sleep(0.1)
    assert ("n", {"y": 2}) in notifications
    # 畸形行 / 空行忽略
    received.append("{not json")
    received.append("")
    await asyncio.sleep(0.1)
    assert len(requests) == 1
    # handler 抛错 → -32603
    def boom(_m: str, _p: dict) -> Any:
        raise ValueError("boom")
    transport.on_request(boom)
    received.append(json.dumps({"jsonrpc": "2.0", "id": "a2", "method": "x"}))
    await asyncio.sleep(0.1)
    assert any(f["id"] == "a2" and f["error"]["code"] == -32603 and f["error"]["message"] == "boom"
               for f in sent), sent
    # 未注册 handler → -32601
    transport.on_request(None)  # type: ignore[arg-type]
    received.append(json.dumps({"jsonrpc": "2.0", "id": "a3", "method": "x"}))
    await asyncio.sleep(0.1)
    assert any(f["id"] == "a3" and f["error"]["code"] == -32601 for f in sent), sent
    await transport.close()
    print("  ✓ 帧解析：请求/响应/通知/畸形行忽略/-32603/-32601")


async def _echo(requests: list, method: str, params: dict) -> str:
    requests.append((method, params))
    return "pong"


async def test_protocol_request_response_error():
    transport, received, sent = _mem_transport()
    transport.start()
    # 对端回 error 帧 → JsonRpcResponseError
    task = asyncio.ensure_future(transport.request("m", {}))
    await asyncio.sleep(0.05)
    assert len(sent) == 1, sent  # 请求已发出
    request_id = sent[-1]["id"]
    # 模拟对端：先错后对
    frames = [
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "custom", "data": {"k": 1}}},
    ]
    sent_frames: dict = {}
    orig_writer = None
    # 直接把响应注入 transport 的内部 pending
    async def inject():
        for frame in frames:
            received.append(json.dumps(frame))
    await inject()
    try:
        await asyncio.wait_for(task, timeout=1.0)
        raise AssertionError("应抛 JsonRpcResponseError")
    except JsonRpcResponseError as exc:
        assert exc.code == -32000 and exc.data == {"k": 1}
    await transport.close()
    print("  ✓ 请求/响应：error 帧抛 JsonRpcResponseError")


async def test_protocol_request_success():
    transport, received, sent = _mem_transport()
    transport.start()
    task = asyncio.ensure_future(transport.request("m", {"v": 1}))
    await asyncio.sleep(0.05)
    request_id = sent[-1]["id"]
    received.append(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}}))
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == {"ok": True}
    await transport.close()
    print("  ✓ 请求/响应：result 帧解析")


# --------------------------------------------------------------------------- #
# 服务器（进程内，直接调 handle_request + 捕获通知）
# --------------------------------------------------------------------------- #
def _inproc_server():
    """构造 mock 装配的 ctx + 捕获通知的 server。"""
    ctx = AppContext()
    ctx.provide("appConfig", AppConfig({}))
    load_profile(ctx, [*CORE_PROFILE])
    ctx.llm.register_adapter(["deepseek-official"], MockAdapter(), replace=True)

    notifications: list[dict] = []
    async def never() -> Optional[str]:
        await asyncio.sleep(3600)
        return None
    transport = JsonRpcLineTransport(reader=never,
                                     writer=lambda line: notifications.append(json.loads(line)))
    server = HarnessSdkJsonRpcServer(ctx, transport)
    transport.start()
    return ctx, server, transport, notifications


async def test_server_initialize_and_errors():
    ctx, server, transport, notifications = _inproc_server()
    try:
        # initialize 缺省路由
        result = await server.handle_request("initialize", {
            "cwd": PROJECT_ROOT, "provider": "deepseek-official", "model": "deepseek-v4-flash",
        })
        assert result["serverInfo"]["name"] == "deepseek-harness-sdk-runtime"
        assert result["serverInfo"]["version"] == "0.0.1"
        # maxTokens 非法 → TypeError
        try:
            await server.handle_request("initialize", {"maxTokens": 0})
            raise AssertionError("应抛 TypeError")
        except TypeError:
            pass
        # 未知方法 → RuntimeError
        try:
            await server.handle_request("nope", {})
            raise AssertionError("应抛 RuntimeError")
        except RuntimeError:
            pass
        # 未注册 provider → RuntimeError（deepseek 之外的）
        try:
            await server.handle_request("initialize", {"provider": "ghost"})
            raise AssertionError("应抛 RuntimeError")
        except RuntimeError:
            pass
        print("  ✓ initialize：serverInfo / maxTokens 校验 / 未知方法 / 未注册 provider")
    finally:
        await server.handle_request("shutdown", {})
        await transport.close()
        ctx.dispose()


async def test_server_prompt_streams_events():
    ctx, server, transport, notifications = _inproc_server()
    try:
        await server.handle_request("initialize", {
            "cwd": PROJECT_ROOT, "provider": "deepseek-official", "model": "deepseek-v4-flash",
        })
        result = await server.handle_request("session/prompt", {
            "sessionId": "s-test-1",
            "contentBlocks": [{"type": "text", "text": "你好"}],
        })
        assert isinstance(result.get("messageId"), str)
        # 等 agent 跑完：session.status idle + session.event 送达
        for _ in range(100):
            if any(n["method"] == "session.status" and n["params"]["status"] == "idle"
                   and n["params"]["sessionId"] == "s-test-1" for n in notifications):
                break
            await asyncio.sleep(0.05)
        methods = [n["method"] for n in notifications]
        assert "session.event" in methods and "session.status" in methods, methods
        statuses = [n["params"]["status"] for n in notifications
                    if n["method"] == "session.status" and n["params"]["sessionId"] == "s-test-1"]
        assert "running" in statuses and "idle" in statuses, statuses
        events = [n["params"]["event"] for n in notifications if n["method"] == "session.event"]
        assert any(e["type"] == "assistant/message" for e in events), events
        # 会话已登记
        assert ctx.sessions.get("s-test-1") is not None
        print("  ✓ session/prompt：session.event 流 + status running→idle + 会话登记")
    finally:
        await server.handle_request("shutdown", {})
        await transport.close()
        ctx.dispose()


async def test_server_shutdown_idempotent():
    ctx, server, transport, notifications = _inproc_server()
    try:
        await server.handle_request("initialize", {
            "cwd": PROJECT_ROOT, "provider": "deepseek-official", "model": "deepseek-v4-flash",
        })
        await server.handle_request("session/prompt", {
            "sessionId": "s-shut", "contentBlocks": [{"type": "text", "text": "x"}],
        })
        await asyncio.sleep(0.2)
        r1 = await server.handle_request("shutdown", {})
        r2 = await server.handle_request("shutdown", {})
        assert r1 == {} and r2 == {}
        assert server._records == {} and server._disposers == []
        print("  ✓ shutdown：幂等 + 会话清空 + 退订")
    finally:
        await transport.close()
        ctx.dispose()


# --------------------------------------------------------------------------- #
# 子进程客户端端到端（完整 wire）
# --------------------------------------------------------------------------- #
async def test_client_subprocess_end_to_end():
    client = HarnessClient(command=JSONRPC_CMD)
    try:
        await client.start()
        result = await client.request("initialize", {
            "cwd": PROJECT_ROOT, "provider": "deepseek-official", "model": "deepseek-v4-flash",
        })
        assert result["serverInfo"]["name"] == "deepseek-harness-sdk-runtime"
        # 订阅 + prompt
        sub = client.subscribe()
        await client.request("session/prompt", {
            "sessionId": "s-sub-1", "contentBlocks": [{"type": "text", "text": "你好"}],
        })
        seen_status: list[str] = []
        events: list = []
        for _ in range(200):
            notification = await asyncio.wait_for(sub.next(), timeout=5)
            if notification["method"] == "session.event" and notification["params"]["sessionId"] == "s-sub-1":
                events.append(notification["params"]["event"])
            if notification["method"] == "session.status" and notification["params"]["sessionId"] == "s-sub-1":
                seen_status.append(notification["params"]["status"])
                if seen_status[-1] == "idle":
                    break
        assert "running" in seen_status and "idle" in seen_status, seen_status
        # 通知里应含 assistant/message
        assert any(e.get("type") == "assistant/message" for e in events), events
        sub.close()
        # 未知方法 → -32603 透传
        try:
            await client.request("nope/method", {})
            raise AssertionError("应抛 JsonRpcResponseError")
        except JsonRpcResponseError as exc:
            assert exc.code == -32603
        print("  ✓ 子进程端到端：initialize/prompt/通知流/-32603 透传")
    finally:
        await client.close()


def _drain(sub) -> list:
    items = []
    while True:
        item = sub.try_next()
        if item is None:
            break
        items.append(item)
    return items


async def test_harness_run_high_level():
    harness = DeepSeekHarness(command=JSONRPC_CMD, provider="deepseek-official",
                              model="deepseek-v4-flash")
    try:
        session = harness.session()
        result = await session.run("写一句话介绍你自己")
        assert result.session_id == session.id
        assert result.final_response, "应有最终回复"
        assert any(e.get("type") == "assistant/message" for e in result.events)
        # 复用同一会话再跑一轮
        result2 = await session.run("再说一遍")
        assert result2.final_response
        print("  ✓ 高层 run：finalResponse 提取 + 会话复用")
    finally:
        await harness.close()


async def test_client_close_ladder():
    client = HarnessClient(command=JSONRPC_CMD)
    await client.start()
    await client.request("initialize", {"cwd": PROJECT_ROOT, "provider": "deepseek-official"})
    # shutdown 阶梯：应干净退出（exit code 0）
    await client.close()
    assert client._exit_code == 0, f"exit={client._exit_code}"
    # close 后再 request → TransportClosedError
    try:
        await client.request("initialize", {})
        raise AssertionError("应抛 TransportClosedError")
    except TransportClosedError:
        pass
    print("  ✓ close 阶梯：shutdown → 退出 → 后续请求拒绝")


async def main():
    print("== test_api_sdk ==")
    await test_protocol_frame_dispatch()
    await test_protocol_request_response_error()
    await test_protocol_request_success()
    await test_server_initialize_and_errors()
    await test_server_prompt_streams_events()
    await test_server_shutdown_idempotent()
    await test_client_subprocess_end_to_end()
    await test_harness_run_high_level()
    await test_client_close_ladder()
    print("OK: SDK 跨进程协议测试通过")


if __name__ == "__main__":
    asyncio.run(main())
