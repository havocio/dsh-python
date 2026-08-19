"""用真实 httpx + 本地 stdlib SSE 服务器验证 OpenAI 适配器（Step 3 网络路径）。

运行：python dsh_py/tests/test_adapter_http.py
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.services.adapters.openai_compatible import apply as apply_adapter
from dsh_py.services.llm import ChunkType, GenerateOptions, LlmService
from dsh_py.services.message import TextBlock, create_user_message
from dsh_py.core.context import AppContext


SSE_BODY = (
    'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: [DONE]\n\n'
)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(SSE_BODY.encode("utf-8"))

    def log_message(self, *args):  # 静默
        pass


def start_server():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


async def test_adapter_against_local_server():
    server, port = start_server()
    try:
        ctx = AppContext()
        LlmService(ctx)
        local_url = f"http://127.0.0.1:{port}/v1"
        apply_adapter(ctx, {
            "providers": [{
                "provider": "local", "displayName": "Local",
                "baseURL": local_url, "apiKeyEnv": "LOCAL_KEY",
                "allowEmptyKey": True,
            }]
        })
        opts = GenerateOptions(provider="local", model="m", messages=[create_user_message([TextBlock("hi")])])
        chunks = [c async for c in ctx.llm.stream(opts)]
        assert any(c.type == ChunkType.TEXT_DELTA and c.text == "hi" for c in chunks)
        finish = [c for c in chunks if c.type == ChunkType.FINISH][0]
        assert finish.finish == {"kind": "stop"}
    finally:
        server.shutdown()


def main():
    # httpx + asyncio 在解释器退出时会对尚未显式关闭的内部异步生成器调用
    # aclose()，偶发打印 "aclose(): asynchronous generator is already running"。
    # 这是已知良性 artifact，与功能正确性无关，这里仅静默该噪声（仅过滤这两行，
    # 不影响正常输出）。
    benign = ("asynchronous generator is already running", "asyncgen:")

    class _Filter:
        def __init__(self, stream):
            self._stream = stream

        def write(self, s):
            if any(token in s for token in benign):
                return len(s)
            return self._stream.write(s)

        def flush(self):
            return self._stream.flush()

        def __getattr__(self, name):
            return getattr(self._stream, name)

    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Filter(old_out), _Filter(old_err)
    try:
        asyncio.run(test_adapter_against_local_server())
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    print("OK: OpenAI 适配器经真实 httpx + 本地 SSE 服务器验证通过")


if __name__ == "__main__":
    main()
