"""工具服务（tools seam）：登记工具 schema 并执行（对标 dsh 的 ``dsh-tools``）。

工具是一个 ``name`` 对应一段 handler：``async def handler(arguments: dict) -> str``，
接收模型解析后的参数字典，返回面向模型的文本结果。模型可见的 schema 由
``list_schemas()`` 产出，供请求时塞进 ``tools`` 字段。

**执行前参数校验**（对标 dsh 的 ``validateArgs`` / ``validateJsonSchemaValue``）：
``parameters`` 是 JSON Schema（``{"type":"object","properties":{...},"required":[...]}``），
执行前先经 :func:`validate_args` 校验；不合法参数作为错误文本回流，绝不调用 handler。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.core.schema import Schema, SchemaError
from dsh_py.core.service import Service

# handler 接收参数字典，返回（结果文本, 是否错误）
ToolHandler = Callable[[dict], Awaitable[tuple[str, bool]]]


@dataclass
class ToolEntry:
    """一个已登记工具。"""
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    # 声明式截止（毫秒）：timeout-policy 插件据此强制工具执行时限；未声明为 None
    timeout_ms: Optional[int] = None


def json_schema_to_schema(node: dict) -> Schema:
    """把 JSON Schema 节点转换为 :mod:`dsh_py.core.schema` 的 Schema（schemastery 风格）。

    支持 ``object / array / string / number / integer / boolean``；
    ``required`` 外的属性自动 ``optional()``；未知类型 / 无 type 不校验（any）。
    """
    t = node.get("type")
    if t == "object":
        props = {k: json_schema_to_schema(v) for k, v in node.get("properties", {}).items()}
        required = set(node.get("required", []) or [])
        fields = {
            k: (s.optional() if k not in required else s)
            for k, s in props.items()
        }
        return z.object(fields, extra="strip")
    if t == "array":
        items = node.get("items")
        return z.array(json_schema_to_schema(items) if isinstance(items, dict) else z.any())
    if t == "integer":
        return z.integer()
    if t == "number":
        return z.number()
    if t == "boolean":
        return z.boolean()
    if t == "string":
        return z.string()
    return z.any()


def validate_args(arguments: dict, parameters: dict) -> Optional[str]:
    """按 JSON Schema 校验工具参数；合法返回 None，否则返回错误文本。

    ``parameters`` 为空（未声明 schema）时直接通过。
    """
    if not parameters:
        return None
    try:
        json_schema_to_schema(parameters).validate(arguments)
        return None
    except SchemaError as exc:
        return str(exc)


class ToolService(Service):
    """``tools`` 服务：工具注册表与执行器，``ctx.tools``。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "tools")
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: ToolHandler,
        timeout_ms: Optional[int] = None,
    ) -> None:
        """登记一个工具（同名覆盖）。

        :param timeout_ms: 声明式执行截止（毫秒），供 timeout-policy 插件强制。
        """
        self._tools[name] = ToolEntry(
            name=name, description=description, parameters=parameters,
            handler=handler, timeout_ms=timeout_ms,
        )

    def get(self, name: str, agent: Any = None) -> Optional[ToolEntry]:
        """按名取已登记工具条目（含 ``timeout_ms``）。``agent`` 参数保留以对齐 dsh 的
        ``ctx.tools.get(name, agent)`` 签名，当前所有工具全局可见，忽略 agent 维度。"""
        return self._tools.get(name)

    def list_schemas(self) -> list[dict]:
        """产出供模型使用的工具 schema 列表。"""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute_with_agent(
        self, name: str, arguments_json: str, agent: Any = None, signal: Any = None,
    ) -> tuple[str, bool, list]:
        """带拦截上下文执行工具（agent 主循环使用）。

        经两条瀑布流（对标 dsh 的 ``tools/execute`` → ``tools/post-execute``）：
        - ``tools/execute``：内层执行真实 handler；监听器可拦截/替换（如 timeout-policy）。
        - ``tools/post-execute``：执行后富化；监听器可返回 ``additionalContexts``
          （额外的 user 消息，如 repeat-tool-reminder 的重复提醒）。

        返回 ``(结果文本, 是否错误, additional_contexts)``。``additional_contexts``
        是 :class:`dsh_py.services.message.Message` 列表，由 agent 主循环注入下一
        步。参数/工具/异常错误均归为文本结果，不向上抛。
        """
        import json

        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}", True, []
        exec = {"name": name, "arguments": arguments_json, "agent": agent, "signal": signal}

        # 内层生产者：解析 + 校验 + 调用 handler（对标 dsh 的 ToolRuntime 内层）
        async def inner() -> tuple[str, bool]:
            try:
                arguments = json.loads(arguments_json) if arguments_json else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError as exc:
                return f"参数 JSON 解析失败：{exc}", True
            invalid = validate_args(arguments, entry.parameters)
            if invalid is not None:
                return f"工具 {name!r} 参数校验失败：{invalid}", True
            try:
                # 需要 agent/信号上下文的工具（如 schedule_*）声明两参 (arguments, exec)
                import inspect
                params = inspect.signature(entry.handler).parameters
                if len(params) >= 2:
                    return await entry.handler(arguments, exec)
                return await entry.handler(arguments)
            except Exception as exc:  # noqa: BLE001
                return f"工具执行异常：{exc}", True

        result = await self.ctx.waterfall("tools/execute", {"exec": exec}, inner=inner)
        # 监听器（如 timeout-policy）可能返回结构化结果，统一归约为 (text, is_error)
        if isinstance(result, tuple) and len(result) >= 2:
            text, is_error = result[0], result[1]
        else:
            text, is_error = str(result), True

        # 执行后富化瀑布流：收集 additionalContexts（默认空）
        async def default_post() -> dict:
            return {"kind": "pass", "additionalContexts": []}

        post = await self.ctx.waterfall(
            "tools/post-execute",
            {"exec": exec, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}},
            inner=default_post,
        )
        additional = post.get("additionalContexts", []) if isinstance(post, dict) else []
        # 对齐 dsh 的 post-execute 决策：``accept`` 可携带 ``content`` 替换最终
        # 模型可见文本（如 spill-policy 把超大结果替换为预览 + 取回定位符）。
        # 现有监听器（hooks/guard）不携带 content，行为不受影响。
        if isinstance(post, dict) and post.get("content") is not None:
            replaced = post["content"]
            joined = "".join(
                b["text"] for b in replaced
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            )
            if joined:
                text = joined
        return text, is_error, list(additional)

    async def execute(self, name: str, arguments_json: str) -> tuple[str, bool]:
        """按名字执行工具；``arguments_json`` 是模型给出的原始 JSON 字符串。

        返回 ``(结果文本, 是否错误)``。直接/跨进程调用走此入口；agent 主循环改走
        :meth:`execute_with_agent` 以收集 ``additionalContexts``。二者都经过
        ``tools/execute`` 瀑布流（无监听器时透明）。
        """
        text, is_error, _ = await self.execute_with_agent(name, arguments_json)
        return text, is_error


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``tools`` 服务（工具注册表 + 执行器）。

    本服务本身也是「一切皆插件」的一等公民：想换成别的工具注册表实现（例如
    带权限校验、沙箱执行的版本），提供另一个注册了同名 ``tools`` 服务的插件即可。
    """
    ToolService(ctx)


apply.provides = ["tools"]  # 声明：本插件提供 tools 服务（供 loader 拓扑排序）
