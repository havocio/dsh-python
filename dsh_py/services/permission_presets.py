"""面向用户的权限预设：独立旋钮（沙箱模式 / 批准策略）之上的预设捆绑
（对齐 dsh-permission-presets）。

切换先记录所选预设（``permission/preset`` 事件，耐久 log-only 用户意图），
再把变化过的旋钮经各自规范 setter 写入（``sandbox/mode`` /
``approval/policy``）。执行、提示叙述与重放继续读各自旋钮折叠；预设事件在
两个预设共享同一捆绑时保留用户意图。读侧是 ``permissions`` 会话投影；写侧
是 ``/permission`` 命令——两者都是同一服务的可选子项。

差异（相对 dsh）：dsh 从 ``ctx.shell.sandboxMode`` 读取 executor 的沙箱能力
且静态 ``inject = ['shell','approval','sessions']``；dsh_py 的 shell 无
``sandboxMode`` 属性，改用 ``Config.sandboxDefault``（默认
``workspace-write``）作折叠默认，装配处仍要求 shell/approval/sessions 已挂载
（fail-loud，等价 inject）。dsh 监听 ``session/created`` 事件钉初始权限；
dsh_py 无该事件，改为构造时遍历既有会话 + 公开
:meth:`PermissionPresetService.pinInitialPermission` 供装配方在新会话创建后
调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.sandbox_policy import SANDBOX_MODES, effectiveSandboxMode, setSandboxMode
from dsh_py.services.settings import install_settings_section, settings_namespace
from dsh_py.services.user_approval import (
    APPROVAL_POLICIES,
    effectiveApprovalPolicy,
    setApprovalPolicy,
)

# --------------------------------------------------------------------------- #
# 词汇
# --------------------------------------------------------------------------- #

#: 有效旋钮值与预设表不匹配时返回的派生值：可作当前值展示，但绝不是切换
#: 目标或事件载荷。
CUSTOM_PRESET = "custom"

#: 承载未来会话默认值的 settings 命名空间。
PERMISSION_SETTINGS_NAMESPACE = settings_namespace("permission")


@dataclass
class PresetSpec:
    """一个预设的沙箱/批准捆绑与可选客户端展示。"""

    sandbox: str  # SANDBOX_MODES 之一
    approval: str  # APPROVAL_POLICIES 之一
    name: Optional[str] = None  # 客户端展示标签；缺省用表键
    description: Optional[str] = None  # 一句含义说明；缺省不配置


@dataclass
class KnobState:
    """投影单元状态：每个旋钮事件的最后所见值，覆盖前为 None
    （组合默认在视图时应用）。纯 JSON（持久化缓存前置条件）。"""

    preset: Optional[str] = None
    sandbox: Optional[str] = None
    approval: Optional[str] = None


#: 空日志的状态：每个旋钮都在组合默认。
EMPTY_KNOBS = KnobState()


def effectivePermissionPreset(events: list) -> Optional[str]:
    """从耐久日志折叠最后选择的预设；重放无需 catch-up 状态。

    :param events: 按日志顺序的会话事件（其他类型忽略）。
    :returns: 最后记录的预设，无记录时返回 None。
    """
    for event in reversed(events):
        if event.type == "permission/preset":
            return event.data.get("preset")
    return None


def applyKnobEvent(state: KnobState, event: Any) -> KnobState:
    """单事件旋钮转移（投影单元的 ``apply``）。不关心的事件**返回同一引用**
    ——注册表的变更门控（``is`` 比较）。"""
    if event.type == "permission/preset":
        return KnobState(preset=event.data.get("preset"), sandbox=state.sandbox, approval=state.approval)
    if event.type == "sandbox/mode":
        return KnobState(preset=state.preset, sandbox=event.data.get("mode"), approval=state.approval)
    if event.type == "approval/policy":
        return KnobState(preset=state.preset, sandbox=state.sandbox, approval=event.data.get("policy"))
    return state


def foldKnobs(events: list) -> KnobState:
    """整日志旋钮折叠（冷读并行）。"""
    state: KnobState = EMPTY_KNOBS
    for event in events:
        state = applyKnobEvent(state, event)
    return state


@dataclass
class PermissionSettings:
    """新会话接收初始权限时解析的用户设置。"""

    defaultPreset: str


@dataclass
class Config:
    """预设表与组合默认。

    :param presets: 预设表：名字 → 旋钮捆绑。缺省为 ``workspace-write``
        （workspace-write + ask）与 ``danger-full-access``（danger-full-access +
        never）。名字 ``custom`` 保留给派生的非预设状态。
    :param defaultPreset: 新会话默认。缺省时用组合默认匹配到的预设。
    :param sandboxDefault: 沙箱折叠默认（对齐 dsh 的 ``ctx.shell.sandboxMode``
        缺失适配；缺省 ``workspace-write``）。
    """

    presets: dict[str, PresetSpec] = field(default_factory=lambda: dict(_DEFAULT_PRESETS))
    defaultPreset: Optional[str] = None
    sandboxDefault: str = "workspace-write"


_DEFAULT_PRESETS: dict[str, PresetSpec] = {
    "workspace-write": PresetSpec(
        sandbox="workspace-write", approval="ask",
        name="workspace-write",
        description="Write inside the workspace and permitted temporary directories; wider retries require approval.",
    ),
    "danger-full-access": PresetSpec(
        sandbox="danger-full-access", approval="never",
        name="danger-full-access",
        description="Full file access without approval prompts.",
    ),
}


class PermissionPresetService(Service):
    """拥有部署的权限预设及其写路径（``ctx.permissionPresets``）。

    需要 confining 的 shell executor 与 ``ctx.approval``；旋钮值与预设表不匹配
    时报告为 :data:`CUSTOM_PRESET`，不是错误。
    """

    def __init__(self, ctx: AppContext, config: Optional[Config] = None) -> None:
        super().__init__(ctx, "permissionPresets")
        config = config or Config()
        self._presets: dict[str, PresetSpec] = config.presets or dict(_DEFAULT_PRESETS)
        if CUSTOM_PRESET in self._presets:
            raise RuntimeError(
                f'permission: "{CUSTOM_PRESET}" is reserved for the derived not-a-preset '
                "state and cannot name a table entry"
            )
        if config.sandboxDefault not in SANDBOX_MODES:
            raise ValueError(f"permission: sandboxDefault must be one of {SANDBOX_MODES}")
        self._sandbox_default: str = config.sandboxDefault
        # approval 默认：挂载的 approval 服务配置 ?? 'ask'
        self._approval_default: str = "ask"
        if ctx.has_service("approval"):
            self._approval_default = getattr(ctx.approval.config, "policy", None) or "ask"
        if self._approval_default not in APPROVAL_POLICIES:
            raise ValueError(f"permission: approval default must be one of {APPROVAL_POLICIES}")

        inferred_default = self.derive(EMPTY_KNOBS)
        default_preset = config.defaultPreset or inferred_default
        if default_preset == CUSTOM_PRESET:
            raise RuntimeError(
                "permission: composed sandbox and approval defaults match no preset; "
                "configure defaultPreset explicitly"
            )
        self.resolve(default_preset)
        base_settings: PermissionSettings = PermissionSettings(defaultPreset=default_preset)
        self._default_settings: Callable[[], PermissionSettings] = lambda: base_settings

        def _set_source(current: Callable[[], Any]) -> None:
            self._default_settings = current  # type: ignore[assignment]

        install_settings_section(
            ctx, PERMISSION_SETTINGS_NAMESPACE, None, {"defaultPreset": default_preset},
            {"set_source": _set_source, "on_change": lambda: None},
        )

        # dsh_py 无 session/created 事件：构造时对既有会话钉初始权限；新建
        # 会话由装配方在创建后调用 pinInitialPermission。
        for session_id in ctx.sessions.list():
            session = ctx.sessions.get(session_id)
            if session is not None:
                self.pinInitialPermission(session)

        # permissions 投影单元：折叠三个全值旋钮事件；视图基于本服务已拥有
        # 的组合默认派生 select。仅当投影注册表已组合时激活（headless 装配
        # 不受影响）。
        if ctx.has_service("sessionProjections"):
            from dsh_py.core import schema as z
            from dsh_py.services.projection import ProjectionDefinition

            select_schema = z.object({
                "options": z.array(z.object({
                    "value": z.string(),
                    "name": z.string(),
                    "description": z.string().optional(),
                })),
                "currentValue": z.string(),
            }, extra="strip")
            ctx.sessionProjections.register(ProjectionDefinition(
                key="permissions",
                schema=select_schema,
                init=lambda: EMPTY_KNOBS,
                apply=applyKnobEvent,
                view=lambda state: self.selectFor(state),
                state_version=1,
            ))

        # /permission 命令：web 客户端使用的唯一写路径（弹窗贡献提交所选预设
        # 为该行）。仅当命令注册表已组合时激活。
        if ctx.has_service("commands"):
            from dsh_py.services.commands import CommandDefinition, CommandInputDescriptor, CommandResult

            def _handler(invocation: Any) -> CommandResult:
                name = invocation.rawInput.strip()
                agent = invocation.agent
                if name == "":
                    return CommandResult(
                        kind="success",
                        text=f"current preset {self.current(list(agent.session.events))} "
                             f"(available: {', '.join(self.names)})",
                    )
                if name not in self.names:
                    return CommandResult(
                        kind="error",
                        text=f'unknown preset "{name}" (available: {", ".join(self.names)})',
                    )
                self.apply(agent.session, name, lambda policy: self.ctx.approval.setPolicy(agent, policy))
                return CommandResult(kind="success", text=f"preset {name}")

            ctx.commands.register(CommandDefinition(
                name="permission",
                description="Switch the permission preset (sandbox mode + approval policy)",
                input=CommandInputDescriptor(hint="<preset>"),
                handler=_handler,
            ))

    # ------------------------------------------------------------------ #
    # 读侧
    # ------------------------------------------------------------------ #
    @property
    def names(self) -> list[str]:
        """广告的预设名，按预设表声明顺序。"""
        return list(self._presets.keys())

    @property
    def defaultPreset(self) -> str:
        """当前选作未来会话默认的预设。"""
        return self._default_settings().defaultPreset

    def current(self, events: list) -> str:
        """解析匹配有效旋钮值的预设：仍匹配的末次选择赢共享捆绑平局；
        否则首个表匹配赢；无条目匹配时为 :data:`CUSTOM_PRESET`。"""
        return self.derive(foldKnobs(events))

    def derive(self, state: KnobState) -> str:
        """为一个折叠旋钮状态解析预设（``current`` 与投影单元的共享数学）。"""
        sandbox = state.sandbox or self._sandbox_default
        approval = state.approval or self._approval_default

        def matches(spec: PresetSpec) -> bool:
            return spec.sandbox == sandbox and spec.approval == approval

        if state.preset is not None:
            spec = self._presets.get(state.preset)
            if spec is not None and matches(spec):
                return state.preset
        for name, spec in self._presets.items():
            if matches(spec):
                return name
        return CUSTOM_PRESET

    def selectFor(self, state: KnobState) -> dict:
        """为一个折叠旋钮状态构建整个 select 值：声明序的每个表选项，
        恰好当前派生为 custom 时追加它。"""
        current_value = self.derive(state)
        options = [self.optionOf(name) for name in self.names]
        if current_value == CUSTOM_PRESET:
            options.append(self.optionOf(CUSTOM_PRESET))
        return {"options": options, "currentValue": current_value}

    def resolve(self, name: str) -> PresetSpec:
        """解析一个预设的旋钮捆绑。

        :raises RuntimeError: name 不在表中。
        """
        spec = self._presets.get(name)
        if spec is None:
            raise RuntimeError(f'permission: unknown preset "{name}" (known: {", ".join(self._presets.keys())})')
        return spec

    def optionOf(self, name: str) -> dict:
        """构建表条目或 :data:`CUSTOM_PRESET` 的客户端选项；缺失标签回退表键。"""
        if name == CUSTOM_PRESET:
            return {
                "value": CUSTOM_PRESET,
                "name": "Custom",
                "description": "Current sandbox and approval settings do not match a preset.",
            }
        spec = self.resolve(name)
        option: dict = {"value": name, "name": spec.name or name}
        if spec.description is not None:
            option["description"] = spec.description
        return option

    # ------------------------------------------------------------------ #
    # 写侧
    # ------------------------------------------------------------------ #
    def set(self, session: Any, name: str) -> None:
        """记录变更的预设，再经各自 setter 更新每个变化旋钮（初始化路径）。"""
        self.apply(session, name, lambda policy: setApprovalPolicy(session, policy))

    def apply(self, session: Any, name: str, set_approval: Callable[[str], None]) -> None:
        """应用一个预设；调用方选择存活（agent 切换）或初始化策略写入器。"""
        spec = self.resolve(name)
        if self.current(list(session.events)) != name:
            session.append("permission/preset", {"preset": name})
        events = list(session.events)
        if spec.sandbox != (effectiveSandboxMode(events) or self._sandbox_default):
            setSandboxMode(session, spec.sandbox)
        if spec.approval != (effectiveApprovalPolicy(events) or self._approval_default):
            set_approval(spec.approval)

    def pinInitialPermission(self, session: Any) -> None:
        """在会话发布前补齐每个缺失的权限事实。全新会话用当前用户默认；
        已播种或部分初始化的会话保留有效旋钮值，只补缺失的耐久事实。"""
        events = list(session.events)
        selected = effectivePermissionPreset(events)
        sandbox = effectiveSandboxMode(events)
        approval = effectiveApprovalPolicy(events)
        seeded = any(event.type == "session/end-seed" for event in events)
        if selected is None and sandbox is None and approval is None and not seeded:
            name = self.defaultPreset
            spec = self.resolve(name)
            session.append("permission/preset", {"preset": name})
            setSandboxMode(session, spec.sandbox)
            setApprovalPolicy(session, spec.approval)
            return
        state = KnobState(preset=selected, sandbox=sandbox, approval=approval)
        effective = self.derive(state)
        if selected is None and effective != CUSTOM_PRESET:
            session.append("permission/preset", {"preset": effective})
        if sandbox is None:
            setSandboxMode(session, self._sandbox_default)
        if approval is None:
            setApprovalPolicy(session, self._approval_default)


__all__ = [
    "CUSTOM_PRESET",
    "PERMISSION_SETTINGS_NAMESPACE",
    "PresetSpec",
    "KnobState",
    "EMPTY_KNOBS",
    "effectivePermissionPreset",
    "applyKnobEvent",
    "foldKnobs",
    "PermissionSettings",
    "Config",
    "PermissionPresetService",
    "apply",
]


def apply(ctx: AppContext, config: Any = None) -> None:
    """装配：提供 ``ctx.permissionPresets`` 服务。

    要求 shell / approval / sessions 已挂载（对齐 dsh 的静态 inject，
    fail-loud）；settings / sessionProjections / commands 可选（缺则跳过对应
    子项）。
    """
    missing = [name for name in ("shell", "approval", "sessions") if not ctx.has_service(name)]
    if missing:
        raise RuntimeError(
            "permission-presets requires services "
            f"{missing}; compose them first (inject-equivalent, fail loud)"
        )
    normalized = config if isinstance(config, Config) else Config(**(config or {}))
    ctx.provide("permissionPresets", PermissionPresetService(ctx, normalized))
