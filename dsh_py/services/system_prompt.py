"""系统提示词组装服务（对标 dsh 的 ``dsh-system-prompt``）。

:class:`SystemPrompt`（``ctx.systemPrompt``）注册「有序的提示片段 / 动态上下文 /
工具 schema / 提示变量」，并在每次模型调用前 :meth:`assemble` 成
:class:`PromptAssembly`，经 ``{{variable}}`` 严格插值后由 :func:`render_prompt`
渲染为最终 system 提示。

- 片段按 ``order`` 升序拼接（约定：-100 为 harness 身份，0 为部署人格，
  100–199 为工具指引；``complete=True`` 的片段独占整条提示，多个则报错）；
- 变量引用 ``{{name}}`` 是**严格**的：未注册 / 无值 / 格式错误都会在渲染时报错
  （孤立 ``{{`` 且其后无 ``}}`` 视为字面文本）；
- 动态上下文经 :func:`render_context_sections` 渲染为模型可见的
  「Current runtime context」快照（对标 dsh 的 joinContextSections）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core import schema as z
from dsh_py.core.service import Service

# 合法变量名：{{name}} 花括号之间须匹配 [a-z][a-z0-9_]*（对齐 dsh 的 VARIABLE_NAME）
VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_GROUP_AT = re.compile(r"^\{\{([^{}]*)\}\}")

# 部署人格片段名与顺序（约定常量，对齐 dsh）
PERSONA_SECTION = "deployment:persona"
PERSONA_ORDER = 0

# toolOrder 的 rest 标记：未列出的工具在此插入（对齐 dsh 的 TOOL_ORDER_REST）
TOOL_ORDER_REST = "<unlisted-tools>"


def order_tools(tools: list[dict], tool_order: Optional[list[str]]) -> list[dict]:
    """按配置的 toolOrder 排序工具；未列出的工具在 rest 标记处按字典序插入。

    - ``tool_order`` 为 None → 纯字典序；
    - 引用了未注册的工具名 → 报错（fail loud）；
    - 未列出的工具在 ``<unlisted-tools>`` 处按名字典序插入。
    """
    if tool_order is None:
        return sorted(tools, key=lambda t: t["name"])
    known = {t["name"] for t in tools}
    unknown = [n for n in tool_order if n != TOOL_ORDER_REST and n not in known]
    if unknown:
        raise ValueError(
            f"toolOrder 列出了未注册的工具：{', '.join(unknown)}；已知工具："
            f"{', '.join(sorted(known)) or '(none)'}")
    listed = set(tool_order)
    rest = sorted((t for t in tools if t["name"] not in listed), key=lambda t: t["name"])
    out: list[dict] = []
    for name in tool_order:
        if name == TOOL_ORDER_REST:
            out.extend(rest)
        else:
            out.extend(t for t in tools if t["name"] == name)
    return out


# --------------------------------------------------------------------------- #
# 注册输入
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PromptSection:
    """一段系统提示片段（注册输入）。"""
    name: str                      # 唯一名（重复注册报错）
    order: int                     # 拼接顺序（升序）
    text: str | Callable[[dict], str]  # 静态文本或按 assembly context 求值的 provider
    complete: bool = False         # True = 独占整条提示（多个生效报错）


@dataclass(frozen=True)
class PromptContext:
    """一段动态上下文（注册输入；空文本不贡献）。"""
    name: str
    order: int
    text: str | Callable[[dict], str]


# --------------------------------------------------------------------------- #
# 组装结果
# --------------------------------------------------------------------------- #
@dataclass
class PromptAssembly:
    """一次组装的结果（sections/contexts 尚未插值，tools 已规范排序）。"""
    sections: list[dict]                        # [{"name","text"}]
    contexts: list[dict]                        # [{"name","text"}]
    tools: list[dict]                           # 工具 schema 列表
    variables: dict[str, Optional[str]]         # 变量名 -> 值


# --------------------------------------------------------------------------- #
# 渲染（严格插值）
# --------------------------------------------------------------------------- #
def interpolate(
    name: str,
    text: str,
    variables: dict[str, Optional[str]],
    kind: str = "section",
) -> str:
    """严格插值 ``{{variable}}``；格式错误 / 未知变量 / 无值都报错。"""
    result = ""
    last = 0
    open_at = text.find("{{", last)
    while open_at >= 0:
        group = _GROUP_AT.match(text[open_at:])
        if group is None:
            # 其后仍有关闭符 → 格式错误；否则是字面文本（孤立 {{）
            if text.find("}}", open_at + 2) >= 0:
                raise ValueError(
                    f"prompt 变量引用格式错误（{kind} \"{name}\" 中 "
                    f"\"{text[open_at:open_at + 16]}…\"，引用必须是完整的 {{name}} 组）")
            result += text[last:open_at + 2]
            last = open_at + 2
            open_at = text.find("{{", last)
            continue
        var_name = group.group(1)
        if not VARIABLE_NAME.fullmatch(var_name):
            raise ValueError(f"prompt 变量名非法 \"{{{{{var_name}}}}}\"（{kind} \"{name}\"）")
        if var_name not in variables:
            known = ", ".join(sorted(variables)) or "(none)"
            raise ValueError(
                f"未知 prompt 变量 \"{{{{{var_name}}}}}\"（{kind} \"{name}\"；已注册：{known}）")
        value = variables[var_name]
        if value is None:
            raise ValueError(f"prompt 变量 \"{{{{{var_name}}}}}\" 本次组装无值（{kind} \"{name}\"）")
        result += text[last:open_at] + value
        last = open_at + group.end()
        open_at = text.find("{{", last)
    return result + text[last:]


def render_prompt(assembly: PromptAssembly) -> str:
    """渲染整条系统提示：插值 → 丢弃空片段 → 空行连接。"""
    parts = []
    for section in assembly.sections:
        rendered = interpolate(section["name"], section["text"], assembly.variables)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts)


def render_context_sections(assembly: PromptAssembly) -> list[dict]:
    """把动态上下文渲染为命名贡献列表（空文本剔除）。"""
    out = []
    for context in assembly.contexts:
        rendered = interpolate(context["name"], context["text"], assembly.variables, "context")
        if rendered:
            out.append({"name": context["name"], "text": rendered})
    return out


def join_context_sections(sections: list[dict]) -> str:
    """把已渲染的上下文片段连接成模型可见快照（对齐 dsh 的 joinContextSections）。"""
    body = "\n\n".join(s["text"] for s in sections)
    if not body:
        return ""
    return f"Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n{body}"


def render_context_snapshot(assembly: PromptAssembly) -> str:
    """渲染完整动态上下文快照。"""
    return join_context_sections(render_context_sections(assembly))


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class SystemPrompt(Service):
    """``systemPrompt`` 服务：系统提示片段 / 上下文 / 变量注册表 + 组装。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "systemPrompt")
        config = config or {}
        self._sections: dict[str, PromptSection] = {}
        self._contexts: dict[str, PromptContext] = {}
        self._variables: dict[str, Callable[[dict], Optional[str]]] = {}
        self._tool_providers: list[Callable[[dict], dict]] = []
        self._runtime_context_suppressed = False
        # 工具排序配置（对齐 dsh 的 Config.toolOrder，含 <unlisted-tools> rest 标记）
        self._tool_order: Optional[list[str]] = None
        tool_order = config.get("tool_order")
        if tool_order is not None:
            if TOOL_ORDER_REST not in tool_order:
                raise ValueError(f"toolOrder 必须包含 rest 标记 {TOOL_ORDER_REST!r}（未列出的工具在此插入）")
            self._tool_order = list(tool_order)

        if config.get("include_harness_identity", True):
            self.section(PromptSection(
                name="harness:identity", order=-100,
                text="You are an AI agent powered by DeepSeek Harness."))
        self.section(PromptSection(
            name=PERSONA_SECTION, order=PERSONA_ORDER,
            text=config.get("persona") or ""))
        if not config.get("include_runtime_context", True):
            self.suppress_runtime_context()

    # -- 注册（均挂到当前 fiber，插件卸载自动清理）------------------------------- #
    def _track(self, disposer: Callable[[], None], label: str) -> Callable[[], bool]:
        return self.ctx.fiber.effect(disposer, label=label)

    def section(self, section: PromptSection) -> Callable[[], bool]:
        """注册一个有序提示片段（重复名 / 非有限 order 报错）。"""
        if not isinstance(section.order, (int, float)) or section.order != section.order:
            raise TypeError(f"prompt section {section.name!r} order 必须是有限数字")
        if section.name in self._sections:
            raise ValueError(f"prompt section {section.name!r} 已注册")
        self._sections[section.name] = section
        return self._track(
            lambda: self._sections.pop(section.name, None), "systemPrompt.section()")

    def context(self, context: PromptContext) -> Callable[[], bool]:
        """注册一段动态上下文。"""
        if context.name in self._contexts:
            raise ValueError(f"prompt context {context.name!r} 已注册")
        self._contexts[context.name] = context
        return self._track(
            lambda: self._contexts.pop(context.name, None), "systemPrompt.context()")

    def variable(self, name: str, provider: Callable[[dict], Optional[str]]) -> Callable[[], bool]:
        """注册一个提示变量（provider 按 assembly context 求值）。"""
        if not VARIABLE_NAME.fullmatch(name):
            raise ValueError(f"prompt 变量名非法 {name!r}（须匹配 {VARIABLE_NAME.pattern}）")
        if name in self._variables:
            raise ValueError(f"prompt variable {name!r} 已注册")
        self._variables[name] = provider
        return self._track(
            lambda: self._variables.pop(name, None), "systemPrompt.variable()")

    def tools(self, provider: Callable[[dict], dict]) -> Callable[[], bool]:
        """注册一个工具 schema 提供者（返回 {"schemas": [...]}）。"""
        self._tool_providers.append(provider)
        return self._track(
            lambda: self._tool_providers.remove(provider), "systemPrompt.tools()")

    def suppress_runtime_context(self) -> Callable[[], bool]:
        """抑制动态上下文贡献（本作用域）。"""
        self._runtime_context_suppressed = True
        return self._track(
            lambda: setattr(self, "_runtime_context_suppressed", False),
            "systemPrompt.suppressRuntimeContext()")

    # -- 组装 ---------------------------------------------------------------- #
    def _resolve_text(self, value: str | Callable[[dict], str], context: dict) -> str:
        return value(context) if callable(value) else value

    async def assemble(self, context: Optional[dict] = None) -> PromptAssembly:
        """按注册表组装：变量求值 → 片段/上下文排序 → 工具收集 → 组装瀑布流。

        生效的 ``complete`` 片段会在瀑布流后恢复为唯一提示片段（多个报错）。
        """
        context = context or {}
        variables: dict[str, Optional[str]] = {
            name: provider(context) for name, provider in self._variables.items()
        }
        sections = []
        complete_sections = [s for s in self._sections.values() if s.complete]
        if len(complete_sections) > 1:
            raise ValueError(
                "同时存在多个 complete prompt section："
                + ", ".join(s.name for s in complete_sections))
        complete_section: Optional[dict] = None
        for section in sorted(self._sections.values(), key=lambda s: s.order):
            assembled = {"name": section.name,
                         "text": self._resolve_text(section.text, context)}
            if section.complete:
                complete_section = dict(assembled)
            sections.append(assembled)

        contexts = []
        if not self._runtime_context_suppressed:
            for entry in sorted(self._contexts.values(), key=lambda c: c.order):
                contexts.append({"name": entry.name,
                                 "text": self._resolve_text(entry.text, context)})

        tools: list[dict] = []
        seen_names = set()
        for provider in self._tool_providers:
            result = provider(context)
            for schema in result.get("schemas", []):
                if schema.get("name") not in seen_names:
                    seen_names.add(schema["name"])
                    tools.append(dict(schema))
        tools = order_tools(tools, self._tool_order)

        assembly = PromptAssembly(
            sections=sections, contexts=contexts, tools=tools, variables=variables)

        # 组装瀑布流（对标 dsh 的 system-prompt/assemble）
        async def default_assembly():
            return assembly

        transformed = await self.ctx.waterfall(
            "system-prompt/assemble", assembly, context, inner=default_assembly)

        if complete_section is None and not self._runtime_context_suppressed:
            return transformed
        return PromptAssembly(
            sections=[complete_section] if complete_section is not None else transformed.sections,
            contexts=[] if self._runtime_context_suppressed else transformed.contexts,
            tools=transformed.tools,
            variables=transformed.variables,
        )


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``systemPrompt`` 服务。

    配置（可选）：``include_harness_identity``（默认 True）、``persona``（部署人格
    文本，order 0）、``include_runtime_context``（默认 True）。
    """
    SystemPrompt(ctx, config)


apply.Config = z.object({
    "include_harness_identity": z.boolean().default(True),
    "include_runtime_context": z.boolean().default(True),
    "persona": z.string().default(""),
    "tool_order": z.array(z.string()).optional(),
})
apply.provides = ["systemPrompt"]  # 声明：本插件提供 systemPrompt 服务