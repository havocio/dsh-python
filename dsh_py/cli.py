"""端到端 CLI 入口（对标 ``dsh --profile``）。

用法：
    python -m dsh_py.cli --mock                          # 离线 mock 模型演示全链路
    python -m dsh_py.cli --provider deepseek --model deepseek-chat   # 配真实模型
    python -m dsh_py.cli --message "写个冒泡排序"         # headless：一条任务，打印结果即退出

**装配点唯一**：默认与自定义都指向同一个 profile（内置 ``configs/profile.py``）。
自定义组件时直接编辑该文件即可，无需新建第二个 profile。装配走 ``boot`` 管线：
bundle 层（内置核心服务）→ 用户层（``configs/profile.py``）→ ``--patch`` overlay。

**headless 模式**（对齐 ``dsh --profile headless "task"``）：传 ``--message`` 时
只跑这一条任务，创建（并持久化）新会话，打印最终 assistant 文本后退出。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.config import load_app_config
from dsh_py.loader import CORE_PROFILE, boot
from dsh_py.sdk import final_response
from dsh_py.services.agent import AgentOptions
from dsh_py.services.llm import ChunkType, LlmAdapter, StreamChunk


class MockAdapter(LlmAdapter):
    """内置 mock 模型：固定回复，用于离线演示整条流水线。"""

    def __init__(self, reply: str = "（mock）我已收到你的消息，这是一段演示回复。") -> None:
        self.reply = reply

    async def stream(self, options: Any):  # type: ignore[override]
        yield StreamChunk(ChunkType.TEXT_DELTA, text=self.reply)
        yield StreamChunk(ChunkType.FINISH, finish={"kind": "stop"})


def _load_profile_module(path: str) -> list:
    """从 .py 文件加载 PROFILE 列表（返回列表本身，装配交给 boot）。"""
    spec = importlib.util.spec_from_file_location("profile_mod", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.PROFILE


# 唯一装配点：用户层 profile 文件（默认与自定义都指向它）
_DEFAULT_PROFILE = os.path.join(os.path.dirname(__file__), "configs", "profile.py")


async def main() -> None:
    parser = argparse.ArgumentParser(description="dsh_py 端到端对话 CLI")
    parser.add_argument(
        "--profile",
        default=_DEFAULT_PROFILE,
        help="用户层 profile .py 文件路径（默认 %(default)s；自定义组件时直接编辑该文件即可）",
    )
    parser.add_argument(
        "--config", default=None, metavar="FILE",
        help="配置文件 .py 路径（默认 configs/dsh_config.py + ~/.dsh/dsh_config.py 合并）",
    )
    parser.add_argument(
        "--patch", action="append", default=[], metavar="FILE",
        help="overlay patch .py 文件（可多次传入；按命令行顺序叠加在用户层之上）",
    )
    parser.add_argument("--provider", default=None, help="供应商（缺省取配置文件 llm.provider，再兜底 openai）")
    parser.add_argument("--model", default=None, help="模型名（缺省取配置文件 llm.model，再兜底 gpt-4o）")
    parser.add_argument("--system", default=None, help="系统提示词")
    parser.add_argument("--max-tokens", type=int, default=None, help="最大生成长度（缺省取配置文件 llm.max_tokens）")
    parser.add_argument("--mock", action="store_true", help="使用内置 mock 模型，离线演示")
    parser.add_argument(
        "--message", default=None, metavar="TEXT",
        help="headless 模式：跑完这一条任务即退出（对齐 dsh --profile headless \"task\"）",
    )
    parser.add_argument(
        "--jsonrpc", action="store_true",
        help="SDK 运行时模式：stdio 上服务 newline JSON-RPC（stdout 仅承载协议帧）",
    )
    args = parser.parse_args()

    ctx = AppContext()
    # 先加载统一配置文件并注入 ctx（适配器 apply 时即可通过 ctx.appConfig 读取）
    config = load_app_config(args.config)
    ctx.provide("appConfig", config)
    # CLI 显式参数优先；缺省从配置文件取；最后兜底内置默认
    llm_cfg = config.get("llm") or {}
    provider = args.provider or llm_cfg.get("provider") or "openai"
    model = args.model or llm_cfg.get("model") or "gpt-4o"
    system = args.system if args.system is not None else llm_cfg.get("system") or ""
    max_tokens = args.max_tokens if args.max_tokens is not None else llm_cfg.get("max_tokens")
    # boot 管线：bundle 层（内置核心服务）→ 用户层（唯一装配点）→ overlays（--patch）
    layers = [CORE_PROFILE]
    if os.path.exists(args.profile):
        layers.append(_load_profile_module(args.profile))
    for patch_file in args.patch:
        if not os.path.exists(patch_file):
            raise FileNotFoundError(f"--patch 文件不存在：{patch_file}")
        layers.append(_load_profile_module(patch_file))
    boot(ctx, *layers)

    if args.mock:
        # mock 模式：把默认 provider 与 SDK 兜底路由（deepseek-official）都替换为
        # MockAdapter——离线全链路演示不依赖真实端点（bundle 层已注册，需 replace）
        for route in list(dict.fromkeys([provider, "deepseek-official"])):
            ctx.llm.register_adapter([route], MockAdapter(), replace=True)

    # SDK 运行时模式：stdio 上服务 newline JSON-RPC（对齐 dsh 的 sdk-jsonrpc-server）。
    # stdout 只承载协议帧，日志/打印一律禁止（stderr 不受限）。
    if args.jsonrpc:
        import logging

        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        from dsh_py.api.protocol import JsonRpcLineTransport
        from dsh_py.api.server import HarnessSdkJsonRpcServer

        transport = JsonRpcLineTransport()
        server = HarnessSdkJsonRpcServer(ctx, transport)

        async def serve() -> None:
            loop = asyncio.get_running_loop()
            shutdown_done: asyncio.Future = loop.create_future()
            original_handler = server.handle_request

            async def handler(method: str, params: dict) -> Any:
                result = await original_handler(method, params)
                # shutdown 响应写出后，主循环结束并收尾（对齐 dsh 的 setImmediate）
                if method == "shutdown" and not shutdown_done.done():
                    shutdown_done.set_result(None)
                return result

            transport.on_request(handler)
            transport.start()
            try:
                # 退出条件：客户端 shutdown 完成，或 stdin EOF（客户端进程关闭）
                while not shutdown_done.done() and not transport.eof:
                    await asyncio.sleep(0.05)
            finally:
                await transport.close()
                ctx.dispose()

        await serve()
        return

    session = ctx.sessions.create()
    # 经 agents 注册表创建 Agent（智能体循环本身是可替换的插件）
    agent = ctx.agents.create_agent(
        session,
        AgentOptions(
            provider=provider,
            model=model,
            system=system,
            max_tokens=max_tokens,
        ),
    )

    # headless 模式：一条任务 → 打印最终 assistant 文本 → 退出（对齐 dsh 语义）。
    # 不注册流式打印监听器，避免最终文本重复输出。
    if args.message is not None:
        await agent.run(args.message)
        print(final_response(session.events))
        return

    # 订阅 assistant 分块，流式打印文本
    @ctx.on("session/event")
    def on_event(_session: Any, event: Any) -> None:
        if event.type == "assistant/chunk":
            chunk = event.data["chunk"]
            if chunk.type == ChunkType.TEXT_DELTA and chunk.text:
                print(chunk.text, end="", flush=True)
            elif chunk.type == ChunkType.FINISH:
                print()

    print("dsh_py > 输入消息开始对话（Ctrl-C / Ctrl-Z 退出）")
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip()
        if not line:
            continue
        await agent.run(line)
        print()


def run() -> None:
    """同步入口，供 ``python -m dsh_py.cli`` 与 console_scripts 调用。"""
    asyncio.run(main())


if __name__ == "__main__":
    run()
