"""工具输出 spill 策略（spill/spill-policy，第 3 层）。

一个 ``tools/post-execute`` 结果转换器：当最终结果的 UTF-8 大小超过
``maxInlineBytes`` 时，把**完整**文本保存到会话作用域 spill 制品
（``ctx.spillStore``），并把面向模型的结果替换为有界 head/tail 预览 + 后端
定位符与取回指引。不注册服务、不拥有存储/预览机制——预览用
:mod:`dsh_py.util.retention`，存储用 ``ctx.spillStore``。

**刻意收窄**：
- 省略 ``maxInlineBytes`` ⇒ 插件注册任何东西（真 no-op）；
- 仅纯文本结果：带任何非 text 块的结果原样保留；
- ``read`` 被跳过，避免 ``read → spill → read again`` 循环；
- best-effort：无会话拥有者、无 ``spillStore`` 后端或保存失败 ⇒ 记录并返回
  原结果——spill 失败**绝不**把成功调用变成错误或隐藏内联结果；
- 通知行成本预留**在** ``maxInlineBytes`` 内（替换永远不超过声明的上限；
  通知行本身超限时保留内联内容——spill 文件是无害孤儿，清理延后）。

与 dsh 差异（已注明）：dsh 的 ``tools/code-dispatch-log`` 第二臂（有界化
run_code 子调用的耐久日志副本）在 dsh_py 无对应事件，未实现；dsh 的
``exec.parent``（嵌套复合调用跳过）在 dsh_py 的执行模型不存在，跳过；
``SpillSource.callId`` 在 dsh_py 无调用 id，传空串。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from dsh_py.core.context import AppContext
from dsh_py.util.retention import TextRetainer, describe_omitted

logger = logging.getLogger("dsh_py.spill_policy")


def _flatten_plain_text(content: list) -> Optional[str]:
    """把所有文本块扁平化为一个 UTF-8 串；含任何非 text 块返回 None。"""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        parts.append(block.get("text", ""))
    return "".join(parts)


def _owner_session_id(exec: dict) -> Optional[str]:
    """拥有会话 id；无 agent（直接/测试调用）返回 None。"""
    agent = exec.get("agent")
    if agent is None:
        return None
    session = getattr(agent, "session", None)
    if session is None:
        return None
    header = getattr(session, "header", None)
    return getattr(header, "id", None) if header is not None else None


def _preview(text: str, budget: int) -> dict:
    """把 ``budget`` 字节均分到两端，做有界 head/tail 预览。"""
    head_bytes = -(-budget // 2)  # ceil
    tail_bytes = budget // 2
    retainer = TextRetainer({"kind": "headTail", "headBytes": head_bytes, "tailBytes": tail_bytes})
    retainer.push(text)
    kept = retainer.finish()
    return {"text": kept["text"], "omitted": kept["omittedBytes"]}


def _spill_notice(omitted: dict, ref: dict) -> str:
    """spill 通知行（无预览、无前导空行）。"""
    omission = describe_omitted(omitted, "bytes")
    return f"({omission} Full formatted result stored at: {ref['locator']}. {ref['retrievalHint']})"


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：挂 ``tools/post-execute`` spill 转换器。"""
    cfg = config or {}
    max_inline_bytes = cfg.get("maxInlineBytes")
    # 省略 ⇒ 不自动 spill：什么都不注册
    if max_inline_bytes is None:
        return
    # 加载期校验（而非每次调用）：负数/小数会进 TextRetainer 的预算断言，
    # 把每次超大结果调用变成错误——坏配置必须让部署失败，而不是让工具失败。
    if not isinstance(max_inline_bytes, int) or isinstance(max_inline_bytes, bool) or max_inline_bytes < 0:
        raise ValueError(
            f"spill-policy: maxInlineBytes 必须是非负整数（得到 {max_inline_bytes!r}）",
        )
    cap: int = max_inline_bytes

    async def spill_replacement(
        text: str,
        total_bytes: int,
        session_id: Optional[str],
        tool_name: str,
    ) -> Optional[str]:
        """spill ``text`` 并构建有界替换（预览 + 通知）；无法替换时返回 None。"""
        if session_id is None:
            logger.warning("spill-policy: %s 无会话拥有者；保留内联内容", tool_name)
            return None
        spill_store = getattr(ctx, "spillStore", None)
        if spill_store is None:
            logger.warning("spill-policy: 未装载 ctx.spillStore 后端；保留内联内容")
            return None
        save = {
            "owner": {"sessionId": session_id},
            "source": {"toolName": tool_name, "callId": "", "label": "result"},
            "suggestedName": f"{tool_name}.txt",
            "content": text,
        }
        try:
            ref = await spill_store.saveText(save)
        except Exception as exc:  # noqa: BLE001 - best-effort：存储失败绝不失败调用
            logger.warning("spill-policy: %s 的 saveText 失败：%r；保留内联内容", tool_name, exc)
            return None

        # 预留通知行成本在 cap 内（按最坏省略数计价，是安全上界）
        reserve = len(_spill_notice({"kind": "exact", "count": total_bytes}, ref).encode("utf-8")) + 2
        preview_budget = max(0, cap - reserve)
        kept = _preview(text, preview_budget)
        notice = _spill_notice(kept["omitted"], ref)
        replaced = f"{kept['text']}\n\n{notice}" if kept["text"] else notice
        # 不变量：策略绝不发出大于 cap 的替换；通知行本身超限（极小 cap 或
        # 超长 spill 根）时保留内联内容
        if len(replaced.encode("utf-8")) > cap:
            logger.warning("spill-policy: %s 的通知行超过 maxInlineBytes；保留内联内容", tool_name)
            return None
        return replaced

    @ctx.on("tools/post-execute")
    async def on_post_execute(event: dict, next):
        # 先委托：下游监听器（如 hooks）settle 结果；我们约束它接受的任何东西
        decision = await next()
        # 仅处理非否决结果；值替换（带 value，注册表再校验/渲染）原样通过。
        # dsh_py 的 post-execute 默认决策是 'pass'（dsh 是 'accept'）——两者
        # 都表示「结果放行」，区别仅在否决（block）与值替换（value）。
        if decision.get("kind") == "block" or "value" in decision:
            return decision
        exec = event["exec"]
        # 跳过 read，避免 read → spill → read again 循环
        if exec.get("name") == "read":
            return decision

        content = decision.get("content") or event.get("result", {}).get("content", [])
        text = _flatten_plain_text(content)
        if text is None:
            return decision
        total_bytes = len(text.encode("utf-8"))
        if total_bytes <= cap:
            return decision

        replaced_text = await spill_replacement(text, total_bytes, _owner_session_id(exec), exec.get("name", ""))
        if replaced_text is None:
            return decision
        replaced: dict = {
            "kind": "accept",
            "content": [{"type": "text", "text": replaced_text}],
        }
        if decision.get("additionalContexts"):
            replaced["additionalContexts"] = decision["additionalContexts"]
        return replaced


apply.name = "spill-policy"
apply.inject = ["tools"]
