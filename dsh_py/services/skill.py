"""Agent 技能提供者注册表（``ctx.skills``）。

对齐 dsh 的 ``@deepseek-ai/dsh-skill``（Service Definition 角色）：本包只拥有
技能能力 seam 的契约——合并 provider 目录、按名解析获胜技能、向消费者暴露获胜
摘要与定义。具体 provider（如 ``skill-filesystem``）决定技能来自哪里。

分层语义：注册落入其调用上下文作用域的层（:mod:`dsh_py.services.scope`）——
宿主行与仓库插件落 global 层；agent 预设常驻组合挂载的插件落该预设层。读取合并
global 层与查看作用域的父链，最近层的同名条目直接胜出，rank 只在同层内裁决。

适配（dsh_py 差异，均已注明）：
- 取消以 :class:`~dsh_py.core.signal.CancelSignal` 检查点执行
  （``throw_if_aborted``），不做 dsh 的 promise 竞速（无 ``addEventListener``）。
- 变更通知用 ``ctx.emit("skills/change")``（contained 语义：逐监听器隔离异常）。
- ``SkillProviderControl.signal`` 是 :class:`CancelSignal`，consumer 以轮询
  ``aborted`` 而非事件监听。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Union

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.core.signal import CancelSignal
from dsh_py.services.scope import ScopeKey, ScopedLayers, scope_of

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DEFAULT_COLLECT_CACHE_ENTRIES = 128
MAX_COLLECT_ATTEMPTS = 2
RUNTIME_PROVIDER = "runtime"
RUNTIME_RANK = 250

# 打包技能 provider 与本地内置根的标准优先级
BUNDLED_SKILL_RANK = 600


def is_skill_name(name: str) -> bool:
    """返回是否为合法的 kebab-case 技能名。"""
    return SKILL_NAME_RE.match(name) is not None


# ---------------------------------------------------------------------------
# 词汇
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillResourceBase:
    """provider 专属资源基座：供技能正文解析相对资源。"""

    kind: str  # 'directory' | 'url' | 'opaque'
    path: str = ""
    url: str = ""
    description: str = ""


@dataclass(frozen=True)
class SkillInvocationPolicy:
    """调用控制：模型面 / 用户面可见性。"""

    model_invocable: bool
    user_invocable: bool


@dataclass(frozen=True)
class SkillSummary:
    """调用中立技能元数据（``ctx.skills.list()`` 返回）。"""

    name: str
    description: str
    invocation: SkillInvocationPolicy
    source: str
    provider: str
    when_to_use: Optional[str] = None
    resource_base: Optional[SkillResourceBase] = None


@dataclass(frozen=True)
class SkillCandidate(SkillSummary):
    """provider 目录条目：注册表合并 / 后续加载用。"""

    # kw_only：规避基类含默认字段后的「必填跟在默认后」排序限制
    rank: int = field(kw_only=True)
    locator: Any = field(kw_only=True)
    path: Optional[str] = field(default=None, kw_only=True)
    metadata: Optional[dict] = field(default=None, kw_only=True)


@dataclass(frozen=True)
class SkillDefinition(SkillSummary):
    """完整技能定义，含 provider 加载的正文。"""

    content: str = field(kw_only=True)
    path: Optional[str] = field(default=None, kw_only=True)
    metadata: Optional[dict] = field(default=None, kw_only=True)


@dataclass(frozen=True)
class SkillRegistration:
    """运行时技能注册输入（``ctx.skills.register()``）。"""

    name: str
    description: str
    content: str
    when_to_use: Optional[str] = None
    invocation: Optional[SkillInvocationPolicy] = None
    provider: Optional[str] = None
    source: str = "runtime"
    resource_base: Optional[SkillResourceBase] = None
    path: Optional[str] = None
    metadata: Optional[dict] = None


@dataclass(frozen=True)
class SkillCatalogSnapshot:
    """一次目录观测 + 是否在稳定目录修订内完成发现。"""

    skills: list
    complete: bool


@dataclass(frozen=True)
class SkillProviderObservation:
    """provider 候选 + 当前发现是否权威。"""

    candidates: list
    complete: bool


class SkillProvider(Protocol):
    """一个技能来源（本地目录或远端注册表）。"""

    name: str

    async def list(self, options: dict) -> Union[list, SkillProviderObservation]:
        """列出当前查找上下文下的技能候选。

        :returns: 完整候选数组，或显式观测（候选来自不完整发现）。
        """
        ...

    async def get(self, candidate: SkillCandidate, options: dict) -> Optional[SkillDefinition]:
        """加载先前列出的候选的完整正文；不再可加载返回 None。"""
        ...


class SkillProviderControl:
    """provider 注册期的生命周期与失效控制。"""

    def __init__(self) -> None:
        self.signal = CancelSignal()
        self._active_provider: Any = None
        self._invalidate: Optional[Callable[[], None]] = None

    def invalidate(self) -> None:
        """使已完成目录失效并通知消费者（仅在注册仍存活时生效）。"""
        if self._invalidate is not None:
            self._invalidate()


def is_model_invocable(skill: Any) -> bool:
    """技能是否允许向模型发布 / 由模型加载。"""
    return skill.invocation.model_invocable


def is_user_invocable(skill: Any) -> bool:
    """技能是否允许向用户面命令发布 / 由用户显式调用。"""
    return skill.invocation.user_invocable


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def escape_text(value: str) -> str:
    """转义嵌入技能标记内的模型面散文，防止 provider 文本开/合框架标签。"""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_resource_hint(skill: Any) -> list[str]:
    base = skill.resource_base
    if base is None:
        return [
            f'Resources for this skill are managed by provider "{escape_text(skill.provider)}".',
            "Load referenced resources only as needed.",
        ]
    if base.kind == "directory":
        return [
            f"Base directory for this skill: {escape_text(base.path)}",
            "Resolve relative paths mentioned by this skill against the base directory before using them. Load referenced resources only as needed.",
        ]
    if base.kind == "url":
        return [
            f"Base URL for this skill: {escape_text(base.url)}",
            "Resolve relative URLs mentioned by this skill against the base URL before using them. Load referenced resources only as needed.",
        ]
    return [
        f"Resources for this skill: {escape_text(base.description)}",
        "Load referenced resources only as needed.",
    ]


def render_skill_content(skill: Any) -> str:
    """渲染一个已加载技能给模型；``skill`` 工具结果与用户显式调用注入共用
    同一 ``<skill_content>`` 形状（正文按原样嵌入——技能是可信本地内容）。"""
    resource_hint = _render_resource_hint(skill)
    return "\n".join([
        f'<skill_content name="{_escape_attr(skill.name)}">',
        "<skill_resources>",
        *resource_hint,
        "</skill_resources>",
        "",
        "<skill_instructions>",
        skill.content,
        "</skill_instructions>",
        "</skill_content>",
    ])


# ---------------------------------------------------------------------------
# 分层注册表
# ---------------------------------------------------------------------------

class _SkillLayer:
    """一个作用域的技能注册表贡献。"""

    def __init__(self, scope: Optional[ScopeKey]) -> None:
        from dsh_py.services.scope import NamedEntries

        self.providers: NamedEntries = NamedEntries(
            lambda name: RuntimeError(
                f'a skill provider named "{name}" is already registered'
                if scope is None
                else f'a skill provider named "{name}" is already registered in this scope'
            )
        )
        self.runtime: dict[str, SkillDefinition] = {}

    def isEmpty(self) -> bool:  # noqa: N802 -- 对齐 dsh 命名
        return self.providers.isEmpty() and len(self.runtime) == 0


class _IndexedCandidate:
    """候选 + 其注册上下文（供排序、去重与过期失效校验）。"""

    __slots__ = ("candidate", "provider", "provider_order", "local_order", "layer")

    def __init__(self, candidate, provider, provider_order, local_order, layer) -> None:
        self.candidate = candidate
        self.provider = provider
        self.provider_order = provider_order
        self.local_order = local_order
        self.layer = layer


class SkillRegistry(Service):
    """分层技能 provider 注册表（``ctx.skills``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "skills")
        config = config or {}
        self._collect_cache_max_entries = config.get("collectCacheMaxEntries", DEFAULT_COLLECT_CACHE_ENTRIES)
        if not isinstance(self._collect_cache_max_entries, int) or self._collect_cache_max_entries < 1:
            raise ValueError(f"skill: collectCacheMaxEntries must be an integer greater than or equal to 1")
        self._layers: ScopedLayers = ScopedLayers(
            lambda scope: _SkillLayer(scope),
            self._invalidate_cache,
        )
        self._collect_cache: dict[str, dict] = {}
        self._revision = 0
        self._next_provider_order = 0
        self._scope_ids: dict[ScopeKey, int] = {}
        self._next_scope_id = 1

    # -- 注册 ---------------------------------------------------------------

    def register_provider(self, create: Callable[[SkillProviderControl], SkillProvider]) -> Any:
        """同步注册一个同进程 provider，落入调用上下文作用域的层。

        层内重名 / 保留名（``runtime``）抛错；fiber 销毁注销 provider 并使缓存失效。
        :returns: 注销器（复合 effect 可直接 yield 以保持拆除顺序）。
        """
        control = SkillProviderControl()
        registration: dict = {}

        try:
            provider = create(control)
        except Exception as exc:
            control.signal.abort(exc)
            raise
        name = provider.name
        if name == RUNTIME_PROVIDER:
            control.signal.abort(RuntimeError(f'"{RUNTIME_PROVIDER}" is reserved for runtime skill registrations'))
            raise RuntimeError(f'"{RUNTIME_PROVIDER}" is reserved for runtime skill registrations')
        order = self._next_provider_order
        self._next_provider_order += 1
        scope = scope_of(self.ctx)

        def action(layer: _SkillLayer) -> Callable[[], None]:
            undo = layer.providers.insert(name, {"provider": provider, "order": order})
            registration["layer"] = layer
            registration["name"] = name

            def disposer() -> None:
                undo()
                control.signal.abort(RuntimeError(f'skill provider "{name}" disposed'))

            return disposer

        def invalidate_guard() -> None:
            active_layer = registration.get("layer")
            if active_layer is not None:
                entry = active_layer.providers.get(registration.get("name"))
                if entry is not None and entry["provider"] is provider:
                    self._invalidate_cache()

        control._invalidate = invalidate_guard
        return self._layers.effect(self.ctx, action, "skills.registerProvider()")

    def register(self, skill: SkillRegistration) -> Any:
        """注册一个借用的只读运行时技能，落入调用上下文作用域的层。

        同层同名运行时条目首写获胜；重复登记告警并返回 no-op 注销器。
        """
        _validate_runtime_skill(skill)
        scope = scope_of(self.ctx)
        existing_layer = self._layers.peek(scope) if scope is not None else None
        if scope is None:
            existing_layer = self._layers.global_layer
        if existing_layer is not None and skill.name in existing_layer.runtime:
            self.ctx.logger.warn(f'runtime skill "{skill.name}" ignored because it is already registered')
            return lambda: None
        definition = SkillDefinition(
            name=skill.name,
            description=skill.description,
            content=skill.content,
            when_to_use=skill.when_to_use,
            invocation=skill.invocation or SkillInvocationPolicy(model_invocable=True, user_invocable=True),
            provider=skill.provider or RUNTIME_PROVIDER,
            source=skill.source,
            resource_base=skill.resource_base,
            path=skill.path,
            metadata=skill.metadata,
        )

        def action(layer: _SkillLayer) -> Callable[[], None]:
            layer.runtime[definition.name] = definition
            return lambda: layer.runtime.pop(definition.name, None)

        return self._layers.effect(self.ctx, action, "skills.register()")

    # -- 读取 ---------------------------------------------------------------

    async def list(self, options: Optional[dict] = None) -> list:
        """列出某工作区下的调用中立技能摘要（已排序、已去重）。"""
        return (await self.snapshot(options)).skills

    async def snapshot(self, options: Optional[dict] = None) -> SkillCatalogSnapshot:
        """观测当前调用中立目录 + 是否在稳定修订内完成发现。

        不完整观测绝不缓存；消费者保留 last-good 并在下一请求边界重试。
        """
        options = options or {}
        collected = await self._collect(options)
        entries = sorted(
            (self._to_summary(e.candidate) for e in collected["entries"].values()),
            key=lambda s: s.name,
        )
        return SkillCatalogSnapshot(skills=entries, complete=collected["cacheable"])

    async def get(self, name: str, options: Optional[dict] = None) -> Optional[SkillDefinition]:
        """加载并校验获胜候选；取消在选择后（含缓存命中）复检。

        :returns: 含正文的完整技能；未知/不可加载返回 None。
        """
        if not is_skill_name(name):
            return None
        options = options or {}
        collected = await self._collect(options)
        _throw_if_aborted(options.get("signal"))
        match = collected["entries"].get(name)
        if match is None:
            return None
        try:
            definition = await match.provider.get(match.candidate, options)
        except Exception:
            _throw_if_aborted(options.get("signal"))
            raise
        _throw_if_aborted(options.get("signal"))
        if definition is None:
            return None
        _validate_definition(definition)
        if definition.name != match.candidate.name:
            self._invalidate_entry(match)
            return None
        return definition

    # -- 内部收集 -----------------------------------------------------------

    async def _collect(self, options: dict) -> dict:
        _throw_if_aborted(options.get("signal"))
        attempt = 1
        while True:
            revision = self._revision
            key = self._collect_cache_key(options.get("cwd"), options.get("scope"), revision)
            cached = self._collect_cache.get(key)
            if cached is not None:
                return {"entries": cached, "cacheable": True}
            result = await self._collect_fresh(options)
            _throw_if_aborted(options.get("signal"))
            if revision != self._revision:
                if attempt < MAX_COLLECT_ATTEMPTS:
                    attempt += 1
                    continue
                return {"entries": result["entries"], "cacheable": False}
            if result["cacheable"]:
                self._collect_cache[key] = result["entries"]
                if len(self._collect_cache) > self._collect_cache_max_entries:
                    oldest = next(iter(self._collect_cache))
                    self._collect_cache.pop(oldest, None)
            return result

    async def _collect_fresh(self, options: dict) -> dict:
        # global 先，再父链最远→精确作用域（近层同名条目替换远层——工具注册表
        # 的遮蔽规则）。rank 只裁决同层内重复。
        layers = [self._layers.global_layer, *self._layers.chain_layers(options.get("scope"))]
        merged: dict = {}
        cacheable = True
        for layer in layers:
            collected = await self._collect_layer(layer, options)
            if not collected["cacheable"]:
                cacheable = False
            for entry in collected["entries"]:
                merged[entry.candidate.name] = entry
        return {"entries": merged, "cacheable": cacheable}

    async def _collect_layer(self, layer: _SkillLayer, options: dict) -> dict:
        collected = await self._list_layer_candidates(layer, options)
        collected["entries"].sort(
            key=lambda e: (e.candidate.rank, e.provider_order, e.local_order)
        )
        seen: set = set()
        result = []
        for entry in collected["entries"]:
            skill = entry.candidate
            if skill.name in seen:
                _warn(
                    self.ctx,
                    f'skill "{skill.name}" from {skill.source} ignored because a higher-priority skill already exists',
                )
                continue
            seen.add(skill.name)
            result.append(entry)
        return {"entries": result, "cacheable": collected["cacheable"]}

    async def _list_layer_candidates(self, layer: _SkillLayer, options: dict) -> dict:
        _throw_if_aborted(options.get("signal"))
        candidates: list = []
        cacheable = True
        runtime_order = 0
        for name in sorted(layer.runtime.keys()):
            skill = layer.runtime[name]
            candidates.append(_IndexedCandidate(
                _runtime_candidate(skill), _RUNTIME_SKILL_PROVIDER, -1, runtime_order, layer,
            ))
            runtime_order += 1
        for name, entry in layer.providers.entries():
            provider = entry["provider"]
            local_order = 0
            try:
                output = await provider.list(options)
            except Exception as exc:  # noqa: BLE001 -- provider 发现失败不否决注册表
                signal = options.get("signal")
                if signal is not None and getattr(signal, "aborted", False):
                    raise
                cacheable = False
                _warn(self.ctx, f'skill provider "{provider.name}" skipped: {exc}')
                continue
            if output is None:
                continue
            observation = _normalize_provider_observation(output, provider.name)
            if not observation.complete:
                cacheable = False
            for candidate in observation.candidates:
                _validate_candidate(candidate, provider.name)
                candidates.append(_IndexedCandidate(candidate, provider, entry["order"], local_order, layer))
                local_order += 1
        return {"entries": candidates, "cacheable": cacheable}

    # -- 失效 / 通知 ---------------------------------------------------------

    def _invalidate_cache(self) -> None:
        self._revision += 1
        self._collect_cache.clear()
        self._notify_change()

    def _invalidate_entry(self, entry: _IndexedCandidate) -> None:
        registered = entry.layer.providers.get(entry.provider.name)
        if registered is not None and registered["provider"] is entry.provider:
            self._invalidate_cache()

    def _scope_id(self, key: ScopeKey) -> int:
        scope_id = self._scope_ids.get(key)
        if scope_id is None:
            scope_id = self._next_scope_id
            self._next_scope_id += 1
            self._scope_ids[key] = scope_id
        return scope_id

    def _collect_cache_key(self, cwd: Any, scope: Any, revision: int) -> str:
        from dsh_py.services.scope import scope_chain_of

        chain = scope_chain_of(scope) if scope is not None else []
        return json.dumps({
            "cwd": cwd,
            "scopes": [self._scope_id(k) for k in chain],
            "revision": revision,
        }, sort_keys=True)

    def _notify_change(self) -> None:
        # 通知不含载荷；contained：监听器异常由 ctx.emit 逐个隔离（注册表通知非否决）
        try:
            self.ctx.emit("skills/change")
        except Exception as exc:  # noqa: BLE001
            _warn(self.ctx, f"skills/change notification failed: {exc}")

    def _to_summary(self, skill: Any) -> SkillSummary:
        return SkillSummary(
            name=skill.name,
            description=skill.description,
            when_to_use=getattr(skill, "when_to_use", None),
            invocation=skill.invocation,
            source=skill.source,
            provider=skill.provider,
            resource_base=getattr(skill, "resource_base", None),
        )


# ---------------------------------------------------------------------------
# provider 归一化与校验
# ---------------------------------------------------------------------------

class _RuntimeSkillProvider:
    """运行时技能 provider：目录注入由注册表直写，仅拥有 ``get()``。"""

    name = RUNTIME_PROVIDER

    async def list(self, options: dict) -> list:
        return []

    async def get(self, candidate: SkillCandidate, options: dict) -> SkillDefinition:
        return candidate.locator


_RUNTIME_SKILL_PROVIDER = _RuntimeSkillProvider()


def _runtime_candidate(skill: SkillDefinition) -> SkillCandidate:
    return SkillCandidate(
        name=skill.name,
        description=skill.description,
        when_to_use=skill.when_to_use,
        invocation=skill.invocation,
        source=skill.source,
        provider=skill.provider,
        resource_base=skill.resource_base,
        rank=RUNTIME_RANK,
        locator=skill,
        path=skill.path,
        metadata=skill.metadata,
    )


def _normalize_provider_observation(output: Any, provider_name: str) -> SkillProviderObservation:
    if isinstance(output, list):
        return SkillProviderObservation(candidates=output, complete=True)
    if isinstance(output, SkillProviderObservation):
        return output
    raise TypeError(
        f'skill provider "{provider_name}" list() must return an array or SkillProviderObservation'
    )


def _validate_candidate(candidate: Any, provider_name: str) -> None:
    if not isinstance(candidate.name, str):
        raise TypeError(f'skill provider "{provider_name}" returned a non-string skill name')
    if not is_skill_name(candidate.name):
        raise RuntimeError(f'skill provider "{provider_name}" returned invalid skill name "{candidate.name}"')
    if not isinstance(candidate.description, str):
        raise TypeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with a non-string description')
    if len(candidate.description) == 0:
        raise RuntimeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" without a description')
    _validate_invocation(candidate.invocation, f'skill provider "{provider_name}" returned skill "{candidate.name}"')
    if candidate.when_to_use is not None and not isinstance(candidate.when_to_use, str):
        raise TypeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with a non-string whenToUse')
    if not isinstance(candidate.source, str):
        raise TypeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with a non-string source')
    if not isinstance(candidate.rank, (int, float)) or isinstance(candidate.rank, bool):
        raise RuntimeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with an invalid rank')
    if not isinstance(candidate.provider, str):
        raise TypeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with a non-string provider')
    if candidate.provider != provider_name:
        raise RuntimeError(
            f'skill provider "{provider_name}" returned skill "{candidate.name}" for provider "{candidate.provider}"'
        )
    if candidate.path is not None and not isinstance(candidate.path, str):
        raise TypeError(f'skill provider "{provider_name}" returned skill "{candidate.name}" with a non-string path')


def _validate_runtime_skill(skill: SkillRegistration) -> None:
    if not is_skill_name(skill.name):
        raise RuntimeError(f'invalid skill name "{skill.name}"')
    if len(skill.description) == 0:
        raise RuntimeError(f'skill "{skill.name}" requires a description')
    _validate_invocation(skill.invocation, f'runtime skill "{skill.name}"')


def _validate_definition(skill: SkillDefinition) -> None:
    if not isinstance(skill.name, str):
        raise TypeError("loaded skill name must be a string")
    if not is_skill_name(skill.name):
        raise RuntimeError(f'loaded skill has invalid name "{skill.name}"')
    if not isinstance(skill.description, str):
        raise TypeError(f'loaded skill "{skill.name}" description must be a string')
    if len(skill.description) == 0:
        raise RuntimeError(f'loaded skill "{skill.name}" requires a description')
    _validate_invocation(skill.invocation, f'loaded skill "{skill.name}"')
    if skill.when_to_use is not None and not isinstance(skill.when_to_use, str):
        raise TypeError(f'loaded skill "{skill.name}" whenToUse must be a string')
    if not isinstance(skill.source, str):
        raise TypeError(f'loaded skill "{skill.name}" source must be a string')
    if not isinstance(skill.provider, str):
        raise TypeError(f'loaded skill "{skill.name}" provider must be a string')
    if not isinstance(skill.content, str):
        raise TypeError(f'loaded skill "{skill.name}" content must be a string')
    if skill.path is not None and not isinstance(skill.path, str):
        raise TypeError(f'loaded skill "{skill.name}" path must be a string')


def _validate_invocation(invocation: Any, subject: str) -> None:
    if invocation is None:
        return
    if not isinstance(invocation, SkillInvocationPolicy):
        raise TypeError(f"{subject} with a non-object invocation policy")
    if not isinstance(invocation.model_invocable, bool):
        raise TypeError(f"{subject} with a non-boolean invocation.modelInvocable")
    if not isinstance(invocation.user_invocable, bool):
        raise TypeError(f"{subject} with a non-boolean invocation.userInvocable")


def _compare_code_points(left: str, right: str) -> int:
    return (left > right) - (left < right)


def _compare_indexed_candidates(left: _IndexedCandidate, right: _IndexedCandidate) -> int:
    return (
        (left.candidate.rank > right.candidate.rank) - (left.candidate.rank < right.candidate.rank)
        or (left.provider_order > right.provider_order) - (left.provider_order < right.provider_order)
        or (left.local_order > right.local_order) - (left.local_order < right.local_order)
    )


def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默，绝不级联失败。"""
    try:
        ctx.logger.warn(message)
    except Exception:  # noqa: BLE001
        pass


def _throw_if_aborted(signal: Any) -> None:
    if signal is not None:
        check = getattr(signal, "throw_if_aborted", None)
        if check is not None:
            check()


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：实例化 ``SkillRegistry`` 并挂为 ``ctx.skills``（基类自动 provide）。"""
    SkillRegistry(ctx, config or {})


apply.provides = ["skills"]  # 声明：本插件提供 skills 服务（供 loader 拓扑排序）

__all__ = [
    "BUNDLED_SKILL_RANK",
    "is_skill_name",
    "SkillResourceBase",
    "SkillInvocationPolicy",
    "SkillSummary",
    "SkillCandidate",
    "SkillDefinition",
    "SkillRegistration",
    "SkillCatalogSnapshot",
    "SkillProviderObservation",
    "SkillProvider",
    "SkillProviderControl",
    "SkillRegistry",
    "is_model_invocable",
    "is_user_invocable",
    "escape_text",
    "render_skill_content",
    "apply",
]
