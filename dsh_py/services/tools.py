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
    ) -> None:
        """登记一个工具（同名覆盖）。"""
        self._tools[name] = ToolEntry(name=name, description=description, parameters=parameters, handler=handler)

    def list_schemas(self) -> list[dict]:
        """产出供模型使用的工具 schema 列表。"""
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def has(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, arguments_json: str) -> tuple[str, bool]:
        """按名字执行工具；``arguments_json`` 是模型给出的原始 JSON 字符串。

        返回 ``(结果文本, 是否错误)``。参数解析失败 / 校验失败 / 工具不存在 /
        handler 异常均归为错误结果，不向上抛——工具错误应当作为可观测的文本
        回流给模型。
        """
        entry = self._tools.get(name)
        if entry is None:
            return f"未知工具：{name}", True
        try:
            import json
            arguments = json.loads(arguments_json) if arguments_json else {}
            if not isinstance(arguments, dict):
                arguments = {}
        except json.JSONDecodeError as exc:
            return f"参数 JSON 解析失败：{exc}", True
        # 执行前参数校验（对标 dsh 的 validateArgs）
        invalid = validate_args(arguments, entry.parameters)
        if invalid is not None:
            return f"工具 {name!r} 参数校验失败：{invalid}", True
        try:
            return await entry.handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return f"工具执行异常：{exc}", True


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``tools`` 服务（工具注册表 + 执行器）。

    本服务本身也是「一切皆插件」的一等公民：想换成别的工具注册表实现（例如
    带权限校验、沙箱执行的版本），提供另一个注册了同名 ``tools`` 服务的插件即可。
    """
    ToolService(ctx)


apply.provides = ["tools"]  # 声明：本插件提供 tools 服务（供 loader 拓扑排序）
