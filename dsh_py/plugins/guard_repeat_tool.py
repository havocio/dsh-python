"""重复工具调用提醒守卫（guard/repeat-tool-reminder，治理类）。

监听 ``tools/post-execute`` 瀑布流，统计单个 agent 连续以**完全相同参数**重复调用
同一工具的次数；命中配置的阈值（默认 ``[3, 5, 8]``）时，向模型注入一条温和 /
详细的提醒（作为 plugin 来源的 user/message），**只提醒、不否决、不改写**调用。

- 计数发生在「执行后」而非「执行前」：被拒绝的调用同样流经该瀑布流，模型反复
  撞墙的循环同样值得打断；
- ``include``/``exclude`` 是基于工具名的 ``*`` 通配谓词（调用期求值，不引用注册
  表条目）——匹配不到任何已注册工具的模式依然合法；
- 用户插入新消息会重置计数（重复跨越用户输入不构成循环）；
- 提醒消息携带 ``form='notice'`` 标签，渲染进派生历史时不会被误当作普通用户提示。

配置错误「启动期即失败」（fail-loud）：``thresholds`` 为空、含非整数、含 <2 的值、
或含重复值时直接抛错，绝不静默回退。
"""

from __future__ import annotations

import json
import re

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.message import MessageSource, TextBlock, create_user_message


# 本守卫注入提醒时打在消息来源上的标签（承重：未打标签的上下文会作为 user 提示渲染）
PLUGIN_SOURCE = MessageSource("plugin", plugin="repeat-tool-reminder")

# 首个阈值之前的温和提醒（按阈值首个值触发，而非硬编码计数，便于自定义升级节奏）
GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)


def detailed_reminder(tool_name: str, count: int, canonical_arguments: str) -> str:
    """达到后续阈值时的详细提醒：点名工具、连续次数、规范化参数。"""
    return (
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- consecutive_calls: {count}\n"
        f"- arguments: {canonical_arguments}\n"
        "The repeated calls are not making progress. Do not call this tool with "
        "these exact arguments again. Inspect the latest result and choose a "
        "different action, different arguments, or finish the task if enough "
        "evidence has been gathered."
    )


def sort_json_value(value):
    """深度按 key 排序一个 JSON 值，使仅属性顺序不同的两个参数对象归一到同一串。"""
    if isinstance(value, list):
        return [sort_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: sort_json_value(value[k]) for k in sorted(value)}
    return value


def canonicalize(arguments_value) -> str:
    """参数的规范化字符串形式：深度 key 排序后 JSON 化（比较始终用完整串）。"""
    return json.dumps(sort_json_value(arguments_value), ensure_ascii=False)


def wildcard_to_regexp(pattern: str) -> re.Pattern:
    """把一个 ``*`` 通配模式编译成锚定的正则（其余正则元字符按字面匹配）。"""
    escaped = re.escape(pattern)
    return re.compile("^" + escaped.replace(r"\*", ".*") + "$")


def preview_arguments(canonical: str, cap: int) -> str:
    """截断规范化参数用于详细提醒（仅限制模型可见文本，比较键始终用完整串）。"""
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canon)-cap} more chars)"


def validate_thresholds(values: list) -> list:
    """fail-loud 校验阈值并升序返回（升级规则读 thresholds[0] 作为温和档）。"""
    if len(values) == 0:
        raise ValueError("repeat-tool-reminder: `thresholds` 不得为空")
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            raise ValueError(
                f"repeat-tool-reminder: 非法阈值 {value!r} —— 每个阈值必须是 >= 2 的整数"
            )
    if len(set(values)) != len(values):
        raise ValueError("repeat-tool-reminder: `thresholds` 不得包含重复值")
    return sorted(values)


def prepend_context(ours, theirs):
    """把本守卫的提醒插到最前，保留下游上下文的来源与元数据。"""
    return [ours, *(theirs or [])]


# --------------------------------------------------------------------------- #
# 配置 schema（对齐 dsh 的 ``Config``）
# --------------------------------------------------------------------------- #
Config = z.object(
    {
        "thresholds": z.array(z.integer()).default([3, 5, 8]),
        "include": z.array(z.string()).default([]),
        "exclude": z.array(z.string()).default([]),
        "argumentsPreviewChars": z.integer().default(500),
    },
    extra="strip",
)


def apply(ctx: AppContext, config: dict | None = None) -> None:
    """安装重复工具调用提醒守卫的监听器。"""
    config = config or {}
    thresholds = validate_thresholds([int(v) for v in config["thresholds"]])
    threshold_set = set(thresholds)
    include_patterns = [wildcard_to_regexp(p) for p in config["include"]]
    exclude_patterns = [wildcard_to_regexp(p) for p in config["exclude"]]
    arguments_preview_chars = int(config["argumentsPreviewChars"])
    if arguments_preview_chars < 1:
        raise ValueError(
            f"repeat-tool-reminder: 非法 argumentsPreviewChars {arguments_preview_chars} —— 必须是 >= 1 的整数"
        )

    # 每个 agent 的连续重复链：上次被跟踪调用的身份键与其累计次数（按 id 弱索引）
    chains: dict[int, dict] = {}

    def tracked(tool_name: str) -> bool:
        """工具是否参与计数（未跟踪的调用透明：既不计数也不重置）。"""
        if include_patterns and not any(p.match(tool_name) for p in include_patterns):
            return False
        return not any(p.match(tool_name) for p in exclude_patterns)

    def observe(exec: dict):
        """推进调用方的重复链，命中阈值则返回要注入的提醒消息（否则 None）。"""
        # 直接 ctx.tools.execute() 的调用方没有模型可提醒、没有 id 可键控；
        # 仅 agent 主循环的调用参与。
        agent = exec.get("agent")
        if agent is None:
            return None
        if not tracked(exec["name"]):
            return None
        # 参数是模型给出的原始 JSON 字符串；JSON 解析失败则回退原始串（仍规范化比较）
        try:
            arguments_value = json.loads(exec["arguments"]) if exec["arguments"] else {}
        except json.JSONDecodeError:
            arguments_value = exec["arguments"]
        canonical = canonicalize(arguments_value)
        key = json.dumps([exec["name"], canonical], ensure_ascii=False)
        chain = chains.get(id(agent))
        count = chain["count"] + 1 if chain is not None and chain["key"] == key else 1
        chains[id(agent)] = {"key": key, "count": count}
        if count not in threshold_set:
            return None
        text = (
            GENTLE_REMINDER if count == thresholds[0]
            else detailed_reminder(exec["name"], count, preview_arguments(canonical, arguments_preview_chars))
        )
        return create_user_message(
            [TextBlock(text)],
            source=MessageSource("plugin", plugin="repeat-tool-reminder", form="notice"),
        )

    # 观察并富化，绝不否决：先计数（状态无论下游结果都推进），DELEGATE 让后续监听
    # 仍可拦截/替换，再把提醒折叠到返回结果上（additionalContexts 两种决策都携带）
    @ctx.on("tools/post-execute")
    async def on_post_execute(event, next):
        reminder = observe(event["exec"])
        downstream = await next()
        if reminder is None:
            return downstream
        if downstream.get("kind") == "block":
            return {
                "kind": "block",
                "feedback": downstream.get("feedback"),
                "additionalContexts": prepend_context(reminder, downstream.get("additionalContexts")),
            }
        return {
            **downstream,
            "additionalContexts": prepend_context(reminder, downstream.get("additionalContexts")),
        }

    # 用户插入新消息改变上下文：跨其重复不算循环。纯重置钩子：始终 delegate（不挂接、
    # 不否决）。
    @ctx.on("agent/pre-step")
    async def on_pre_step(event, next):
        if any(message.source.kind == "user" for message in event["messages"]):
            chains.pop(id(event["agent"]), None)
        return await next()


apply.Config = Config
apply.name = "repeat-tool-reminder"
