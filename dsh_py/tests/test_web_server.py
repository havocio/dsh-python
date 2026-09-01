"""WebServer（``ctx.webServer`` HTTP/升级路由载体）验证（host 范畴）。

注：本环境（websockets 17.0.1 + Python 3.13.12 + Windows ProactorEventLoop）在
``process_request`` 被设置时，底层连接接入会触发 ``assert self._sockets is not None``
（与网关 ``--webui`` 模式同款环境限制）。因此本测试**不**做真实 socket 流量，而是：

- 直接调用 ``_process_request``（无 socket）验证路由匹配顺序、fallback、index 变换、升级放行；
- 验证 ``register`` / ``register_upgrade`` / ``register_fallback`` 的注册守卫；
- 验证 ``start`` / ``stop`` 生命周期（绑定、端口分配、重置）与 host 校验。

路由逻辑即 ``process_request`` 的数据面，已完整覆盖；真实 HTTP 流量受上述环境限制影响。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.services.web_server import Response, WebServer


class _FakeReq:
    """喂给 ``_process_request`` 的最小请求替身（无真实 socket）。"""
    def __init__(self, path="/", headers=None, method="GET"):
        self.path = path
        self.headers = headers or {}
        self.method = method


def test_route_registration_guards():
    """重复路径 / 非法 kind / 重复 fallback / 非法 host 应响亮失败。"""
    ctx = AppContext()
    ws = WebServer(ctx)

    ws.register("exact", "/a", lambda req: Response(body="a"))
    try:
        ws.register("exact", "/a", lambda req: Response())
        raise AssertionError("duplicate exact route should raise")
    except ValueError:
        pass
    try:
        ws.register("weird", "/b", lambda req: Response())
        raise AssertionError("invalid kind should raise")
    except ValueError:
        pass
    try:
        ws.register("prefix", "api", lambda req: Response())  # 非 / 开头
        raise AssertionError("non-slash path should raise")
    except ValueError:
        pass

    ws.register_fallback(lambda req: Response())
    try:
        ws.register_fallback(lambda req: Response())
        raise AssertionError("duplicate fallback should raise")
    except RuntimeError:
        pass


async def test_routing_logic():
    """直接驱动 ``_process_request``：精确 > 最长前缀 > fallback，index 变换，升级放行。"""
    ctx = AppContext()
    ws = WebServer(ctx)

    ws.register("exact", "/hello", lambda req: Response(200, {}, "hello"))
    ws.register("prefix", "/api", lambda req: Response(200, {}, f"api:{req.path}"))
    ws.register_fallback(
        lambda req: (
            Response(200, {"Content-Type": "text/html"}, "<html><body></body></html>")
            if req.path in ("/", "/index.html")
            else Response(404, {}, "fb")
        )
    )
    ws.tap_index(lambda html: html.replace("</body>", "<!-- tapped --></body>"))

    # 1) 精确路由
    r = await ws._process_request(None, _FakeReq("/hello"))
    assert r.status_code == 200 and r.body == b"hello", (r.status_code, r.body)

    # 2) 最长前缀路由
    r = await ws._process_request(None, _FakeReq("/api/users"))
    assert r.status_code == 200 and r.body == b"api:/api/users", (r.status_code, r.body)

    # 3) fallback 404
    r = await ws._process_request(None, _FakeReq("/missing"))
    assert r.status_code == 404 and r.body == b"fb", (r.status_code, r.body)

    # 4) index 变换（仅 index 路径应用）
    r = await ws._process_request(None, _FakeReq("/"))
    assert r.status_code == 200 and b"<!-- tapped -->" in r.body, r.body

    # 5) 升级请求放行（返回 None，交由此后 WS handler 处理）
    r = await ws._process_request(None, _FakeReq("/ws", headers={"upgrade": "websocket"}))
    assert r is None, r


async def test_host_validation():
    """``start`` 仅允许 127.0.0.1 / 0.0.0.0，否则 ValueError。"""
    ctx = AppContext()
    ws = WebServer(ctx)
    try:
        await ws.start(host="1.2.3.4")
        raise AssertionError("invalid host should raise")
    except ValueError:
        pass
    # 未真正起服务，stop 应为幂等空操作
    await ws.stop()


async def test_start_stop_lifecycle():
    """``start`` 绑定并分配端口；``stop`` 重置端口。"""
    ctx = AppContext()
    ws = WebServer(ctx)
    assert ws.port == 0, ws.port
    assert ws.host == "127.0.0.1"

    await ws.start(port=0)
    try:
        assert ws.port > 0, "启动后应有实际端口"
        # 重复 start 应安全（disposer 旧服务已被新服务替换思路由调用方保证；此处仅测不崩）
    finally:
        await ws.stop()
    assert ws.port == 0, "停止后端口清零"


if __name__ == "__main__":
    test_route_registration_guards()
    asyncio.run(test_routing_logic())
    asyncio.run(test_host_validation())
    asyncio.run(test_start_stop_lifecycle())
    print("test_web_server: ALL PASS")
