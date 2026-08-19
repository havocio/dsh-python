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
from typing import Any

from dsh_py.core.context import AppContext
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
        "--patch", action="append", default=[], metavar="FILE",
        help="overlay patch .py 文件（可多次传入；按命令行顺序叠加在用户层之上）",
    )
    parser.add_argument("--provider", default="openai", help="供应商（默认 openai）")
    parser.add_argument("--model", default="gpt-4o", help="模型名（默认 gpt-4o）")
    parser.add_argument("--system", default="", help="系统提示词")
    parser.add_argument("--mock", action="store_true", help="使用内置 mock 模型，离线演示")
    parser.add_argument(
        "--message", default=None, metavar="TEXT",
        help="headless 模式：跑完这一条任务即退出（对齐 dsh --profile headless \"task\"）",
    )
    args = parser.parse_args()

    ctx = AppContext()
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
        # mock 模式：覆盖默认 provider 的适配器（bundle 层已注册，需 replace）
        ctx.llm.register_adapter([args.provider], MockAdapter(), replace=True)

    session = ctx.sessions.create()
    # 经 agents 注册表创建 Agent（智能体循环本身是可替换的插件）
    agent = ctx.agents.create_agent(
        session, AgentOptions(provider=args.provider, model=args.model, system=args.system)
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
