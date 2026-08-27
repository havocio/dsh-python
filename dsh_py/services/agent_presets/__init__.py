"""Agent 预设注册表（``ctx.agentPresets``）。

对齐 dsh 的 ``@deepseek-ai/dsh-agent-presets``：每个会话从一份预设
``agent.cordis.yml`` 组合其模型面插件集——每个预设**只挂载一次**为常驻作用域，
所有命名它的 agent 加入其中。

- 发现不缓存：``list()``/``resolve()`` 每次重读根——运行期创作的预设立即可见。
- 常驻挂载按预设 id 单飞；组合文件显式变化（mtime+size 戳）开启下一代。
- 加入 = 把 agent 作用域键的父链绑到常驻键（dsh-scope 的唯一重链能力）；绑定向
  本服务私有持有，服务是唯一能把 agent 移出常驻组合的权威。
- 创作唯一写是整目录复制；删除保留已加入会话的常驻挂载。

适配（dsh_py 差异，均已注明）：
- dsh 的 ``agent/created`` 告警监听（dsh_py 无该事件）省略。
- ``ctx.loader`` 注入 → 无（dsh_py loader 非服务；装载走 ``load_profile``）。
- dsh_py 的工具 / 提示 / 技能注册表为全局集合：预设行注册其中即为全 agent 可见
  （无 per-agent 遮蔽——与 dsh_py 既有简化一致）；预设行提供的**服务**落在常驻
  作用域上下文（沿其父链可见），``service_for`` 以 agent 自身 ctx 链尽力解析。
- ``service.init`` → 无异步初始化（构造即就绪；settings 区可选挂载）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.agent_presets.authoring import (
    copy_composition,
    delete_composition,
    read_composition,
)
from dsh_py.services.agent_presets.discovery import USER_PRESET_DIR, discover_presets
from dsh_py.services.agent_presets.mount import mount_preset, standing_mount_for
from dsh_py.services.agent_presets.preset import PresetMountError, UnknownPresetError
from dsh_py.services.scope import ScopeKey, bind_scope_parent, create_scope, scope_of
from dsh_py.services.settings import settings_namespace
from dsh_py.util.home_paths import resolve_dsh_home

SETTINGS_NAMESPACE = "agent-presets"


def _warn(ctx: AppContext, message: str) -> None:
    """防御性告警：无 logger 服务（裸 ctx）时静默。"""
    try:
        ctx.logger.warn(message)
    except Exception:  # noqa: BLE001
        pass


def _composition_stamp(path: str) -> Optional[dict]:
    """组合文件的身份戳（mtime 毫秒 + 字节数）；不可 stat 返回 None。"""
    try:
        stat = os.stat(path)
        return {"mtimeMs": stat.st_mtime_ns / 1_000_000, "size": stat.st_size}
    except OSError:
        return None


def _same_stamp(a: dict, b: dict) -> bool:
    return a["mtimeMs"] == b["mtimeMs"] and a["size"] == b["size"]


class AgentPresets(Service):
    """部署 agent 预设的注册表（``ctx.agentPresets``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "agentPresets")
        config = config or {}
        self.config = config
        self._self_ctx = ctx
        # 发现与创作实际扫描的根：每个配置根按序 + harness home 用户根
        self._resolved_roots: list = [*config.get("roots", [])]
        if config.get("includeUserRoot", True):
            self._resolved_roots.append({
                "path": os.path.join(resolve_dsh_home(), USER_PRESET_DIR),
                "trust": "user",
            })
        # 用户层 default 覆盖（可选）：settings 文档热重载即时生效
        self._settings_scope: Any = None
        if ctx.has_service("settings"):
            self._settings_scope = ctx.settings.register(
                settings_namespace(SETTINGS_NAMESPACE),
                None,
                {"base": {"default": config.get("default", "")}},
            )
        # 常驻挂载：preset id → 单飞 future（settled 失败移除以便重试）
        self._standing: dict[str, asyncio.Future] = {}
        # 已结算的常驻挂载（进程生命期存活，同 dsh 的 whole-tree teardown 语义）
        self._live_mounts: list[dict] = []
        # agent 作用域键 → 重链句柄（服务是唯一可移动 agent 的权威）
        self._bindings: dict[ScopeKey, Callable[[ScopeKey], None]] = {}

        # 耐久记录是提交点：公开通知只带客户端需要的稳定身份
        @ctx.on("session/event")
        def _forward_selected(session, event):  # noqa: ANN001
            if getattr(event, "type", None) != "agent-preset/selected":
                return
            data = getattr(event, "data", None)
            agent_preset = data.get("agentPreset") if isinstance(data, dict) else None
            if agent_preset is not None:
                header = getattr(session, "header", None)
                session_id = getattr(header, "id", None) if header is not None else None
                if session_id is not None:
                    try:
                        ctx.emit("agent-preset/selected", session_id, agent_preset)
                    except Exception as exc:  # noqa: BLE001 -- 转发不否决
                        _warn(ctx, f"agent-preset/selected notification failed: {exc}")

    # -- 读取 ---------------------------------------------------------------

    @property
    def default_id(self) -> str:
        """未命名时挂载的预设 id。逐次读取（settings 文档热重载），换默认对
        下一个创建的会话生效，运行中会话保持其组合来源。"""
        if self._settings_scope is not None:
            current = self._settings_scope.get()
            if isinstance(current, dict) and current.get("default"):
                return current["default"]
        return self.config.get("default", "")

    @property
    def roots(self) -> list:
        """本 roster 扫描的根（非 ``config.roots``：含用户根追加）。"""
        return list(self._resolved_roots)

    @property
    def authorable(self) -> bool:
        """部署是否有本地创作可写入的根。"""
        return any(r["trust"] == "user" for r in self._resolved_roots)

    async def list(self) -> list:
        """配置根当前供应的全部预设（每 id 首根胜出）。"""
        return await asyncio.to_thread(discover_presets, self._resolved_roots)

    async def resolve(self, id: Optional[str] = None) -> dict:
        """按 id 解析一个预设；未知抛 :class:`UnknownPresetError`。坏预设照常
        解析（删除/读取/报告都需要行），挂载路径在 resolve_mountable 拒绝。"""
        wanted = id if id is not None else self.default_id
        presets = await self.list()
        found = next((p for p in presets if p["id"] == wanted), None)
        if found is None:
            raise UnknownPresetError(wanted, [p["id"] for p in presets])
        return found

    async def _resolve_mountable(self, id: Optional[str] = None) -> dict:
        """解析将组合 agent 的预设；坏预设（discovery 报告）以
        :class:`PresetMountError` 拒绝。"""
        preset = await self.resolve(id)
        if preset.get("broken") is not None:
            raise PresetMountError(preset["id"], preset["broken"])
        return preset

    # -- 加入 / 挂载 ---------------------------------------------------------

    async def mount(self, agent_ctx: AppContext, id: Optional[str] = None) -> dict:
        """从预设组合一个 agent：确保预设的常驻挂载，然后把 agent 作用域键的
        父链绑到常驻键。

        :raises RuntimeError: agent_ctx 无作用域键；UnknownPresetError /
          PresetMountError: 预设未知或组合不可用。
        """
        agent_key = scope_of(agent_ctx)
        if agent_key is None:
            raise RuntimeError(
                "agent-presets: refusing to compose an unscoped context; "
                "the scope key is what joins an agent to its preset"
            )
        preset = await self._resolve_mountable(id)
        standing = await self._ensure_standing(preset)
        self._bindings[agent_key] = bind_scope_parent(agent_key, standing["key"])
        return preset

    def compose_from(self, agent_ctx: AppContext, parent_ctx: AppContext) -> Optional[str]:
        """把 agent 加入父 agent 已在跑的**同一**常驻组合（绑定而非挂载：
        子 agent 继承父的能力实例）。同步、无自身失败模式（不读 roster、不挂载、
        不碰文件）——子 agent 创建窗口可用。父未加入任何预设（无 roster 部署）
        返回 None 且不报错。"""
        agent_key = scope_of(agent_ctx)
        if agent_key is None:
            raise RuntimeError(
                "agent-presets: refusing to compose an unscoped context; "
                "the scope key is what joins an agent to its preset"
            )
        standing = standing_mount_for(parent_ctx, self._live_mounts)
        if standing is None:
            return None
        self._bindings[agent_key] = bind_scope_parent(agent_key, standing["key"])
        return standing["preset_id"]

    def composed_preset(self, agent_ctx: AppContext) -> Optional[str]:
        """一个在线 agent 运行的预设（读实时作用域链而非会话记录）。"""
        standing = standing_mount_for(agent_ctx, self._live_mounts)
        return standing["preset_id"] if standing is not None else None

    async def recompose(self, agent_ctx: AppContext, id: str) -> dict:
        """把 agent 重链到另一预设的常驻组合。

        仅当 agent 尚未产出任何内容时有效（调用方持有该检查）；换是父链重链
        而非卸载——常驻挂载共享且永久，新组合在移动前确保（未知/不可用预设
        抛出时 agent 原样，无待恢复的拆除状态）。从未组合过的 agent 无重链：
        首次绑定即挂载。
        """
        agent_key = scope_of(agent_ctx)
        if agent_key is None:
            raise RuntimeError("agent-presets: refusing to recompose an unscoped context")
        preset = await self._resolve_mountable(id)
        standing = await self._ensure_standing(preset)
        binding = self._bindings.get(agent_key)
        if binding is None:
            self._bindings[agent_key] = bind_scope_parent(agent_key, standing["key"])
        else:
            binding(standing["key"])
        return preset

    async def standing_key_for(self, id: Optional[str] = None) -> ScopeKey:
        """一个预设的常驻作用域键（供无 agent 的宿主读者作注册表视图 scope）。"""
        preset = await self._resolve_mountable(id)
        return (await self._ensure_standing(preset))["key"]

    # -- 创作 ---------------------------------------------------------------

    async def read(self, id: str) -> str:
        """读一个预设的组合文本（原样）。"""
        return read_composition(await self.resolve(id))

    async def copy(self, from_id: str, id: str, name: Optional[str] = None) -> None:
        """复制一个既有预设为新预设（唯一创作写）。复制不挂载验证——今天能挂
        载的源，其复制今天也能。"""
        source = await self.resolve(from_id)
        if any(p["id"] == id for p in await self.list()):
            from dsh_py.services.agent_presets.authoring import PresetExistsError

            raise PresetExistsError(id)
        await asyncio.to_thread(copy_composition, self._resolved_roots, source, id, name)
        self._standing.pop(id, None)  # 已结算挂载只会是陈旧（源已删除）

    async def remove(self, id: str) -> None:
        """删除本地创作预设；shipped 预设拒绝。"""
        preset = await self.resolve(id)
        await asyncio.to_thread(delete_composition, self._resolved_roots, preset)
        self._standing.pop(id, None)
        # 删除的默认必须清除：否则每个未显式挑选的会话都起不来（暴露部署默认）
        if self._settings_scope is not None:
            current = self._settings_scope.get()
            if isinstance(current, dict) and current.get("default") == id:
                try:
                    self._settings_scope.set({"default": self.config.get("default", "")})
                except Exception as exc:  # noqa: BLE001 -- 设置清理失败不阻断删除
                    _warn(self.ctx, f"agent-presets: failed to clear deleted default: {exc}")

    # -- 服务读取 -----------------------------------------------------------

    def service_for(self, agent: Any, name: str) -> Any:
        """一个 agent 挂载的某服务实例（尽力解析）。

        dsh 经 reflect.store 读预设行发布的 isolate 领域服务；dsh_py 无
        per-fiber 服务存储——退化为在 agent 自身 ctx 链上解析（预设行提供的
        服务若在常驻 ctx 且 agent ctx 沿其解析，可命中；否则 None，文档化）。
        """
        agent_ctx = getattr(agent, "ctx", None)
        if agent_ctx is None:
            return None
        return getattr(agent_ctx, name) if agent_ctx.has_service(name) else None

    # -- 常驻挂载 -----------------------------------------------------------

    async def _ensure_standing(self, preset: dict) -> dict:
        pending = self._standing.get(preset["id"])
        if pending is not None:
            mounted = await pending
            current = _composition_stamp(preset["path"])
            if current is None or _same_stamp(mounted["stamp"], current):
                return mounted
            # 文件是唯一组合编辑器（创作是复制/删除），戳的变化开启下一代
            if self._standing.get(preset["id"]) is pending:
                self._standing.pop(preset["id"], None)
            return await self._ensure_standing(preset)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._standing[preset["id"]] = future
        try:
            key = ScopeKey()
            scoped_ctx = create_scope(self._self_ctx, key)
            stamp = _composition_stamp(preset["path"])
            if stamp is None:
                raise PresetMountError(preset["id"], f"composition file is unreadable: {preset['path']}")
            mount_preset(scoped_ctx, preset)
            mounted = {"key": key, "ctx": scoped_ctx, "stamp": stamp, "preset_id": preset["id"]}
            self._live_mounts.append(mounted)
            future.set_result(mounted)
        except Exception as exc:  # noqa: BLE001
            self._standing.pop(preset["id"], None)
            future.set_exception(exc)
        return await future


__all__ = ["AgentPresets", "SETTINGS_NAMESPACE", "apply"]


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：创建 agent 预设注册表并挂为 ``ctx.agentPresets``。"""
    AgentPresets(ctx, config or {})


apply.provides = ["agentPresets"]  # 声明：本插件提供 agentPresets 服务（供 loader 拓扑排序）
