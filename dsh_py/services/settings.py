"""运行时设置（对标 dsh 的 ``dsh-settings``）。

设置是**用户可改的运行时配置**：一个命名空间（namespace）+ 一段 schema 校验的
配置值。服务（如 agent-loop）注册自己的配置区后，用户随时 ``set`` 新值，
``watch`` 的监听器收到变更通知——改配置不需要重启进程。

- :class:`SettingsScope` —— 一个配置区：``get()`` / ``set()``（schema 校验 +
  可选 validate 钩子）/ ``watch()``（变更通知）；
- :class:`Settings`（``ctx.settings``）—— 注册表；
- :func:`install_settings_section` —— 消费者接线（对标 dsh 同名函数）：
  settings 服务存在时注册并用 ``hooks.set_source`` 指向活动作用域；消费者
  卸载时回退到组合条目（``entry``）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.schema import Schema
from dsh_py.core.service import Service


class SettingsNamespace:
    """设置命名空间（对标 dsh 的 SettingsNamespace）。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"SettingsNamespace({self.value!r})"

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, SettingsNamespace) and self.value == other.value


def settings_namespace(value: str) -> SettingsNamespace:
    """构造一个设置命名空间。"""
    return SettingsNamespace(value)


class SettingsScope:
    """一个可读写的设置区（schema 校验 + 变更通知）。"""

    def __init__(
        self,
        namespace: SettingsNamespace,
        schema: Optional[Schema] = None,
        base: Any = None,
        validate: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.namespace = namespace
        self._schema = schema
        self._value = base
        self._validate = validate
        self._watchers: list[Callable[[], None]] = []

    # -- 读写 ---------------------------------------------------------------- #
    def get(self) -> Any:
        """当前权威值。"""
        return self._value

    def set(self, value: Any) -> None:
        """写入新值：先 schema 校验，再跑可选 validate 钩子，成功后通知 watchers。"""
        if self._schema is not None:
            value = self._schema.validate(value)
        if self._validate is not None:
            self._validate(value)
        self._value = value
        for watcher in list(self._watchers):
            try:
                watcher()
            except Exception:  # noqa: BLE001 - 单个 watcher 失败不阻断
                continue

    # -- 监听 ---------------------------------------------------------------- #
    def watch(self, callback: Callable[[], None]) -> Callable[[], bool]:
        """注册变更监听；返回可取消注册的句柄。"""
        self._watchers.append(callback)

        def unwatch() -> bool:
            if callback in self._watchers:
                self._watchers.remove(callback)
                return True
            return False

        return unwatch


class Settings(Service):
    """``settings`` 服务：设置注册表，``ctx.settings``。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "settings")
        self._sections: dict[str, SettingsScope] = {}

    def register(
        self,
        namespace: SettingsNamespace,
        schema: Optional[Schema] = None,
        options: Optional[dict] = None,
    ) -> SettingsScope:
        """注册一个配置区（base 为组合条目配置）。"""
        options = options or {}
        scope = SettingsScope(namespace, schema, options.get("base"), options.get("validate"))
        self._sections[namespace.value] = scope
        return scope

    def get(self, namespace: SettingsNamespace) -> Any:
        """读一个配置区的当前值。"""
        return self._sections[namespace.value].get()

    def set(self, namespace: SettingsNamespace, value: Any) -> None:
        """写一个配置区的当前值（校验 + 通知）。"""
        self._sections[namespace.value].set(value)

    def has(self, namespace: SettingsNamespace) -> bool:
        return namespace.value in self._sections


def install_settings_section(
    ctx: AppContext,
    namespace: SettingsNamespace,
    schema: Optional[Schema],
    entry: Any,
    hooks: dict,
) -> None:
    """消费者接线：settings 服务存在时注册配置区并指向活动作用域。

    :param hooks: 含 ``set_source(current)``（收到权威值 thunk）、
        ``on_change()``（配置变更通知）、可选 ``validate(value)``。
    settings 服务未挂载时直接跳过（消费者保持组合条目配置）。
    """
    if not ctx.has_service("settings"):
        return
    scope = ctx.settings.register(namespace, schema, {
        "base": entry,
        "validate": hooks.get("validate"),
    })
    if hooks.get("set_source"):
        hooks["set_source"](lambda: scope.get())
    if hooks.get("on_change"):
        hooks["on_change"]()
        scope.watch(hooks["on_change"])


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``settings`` 服务（运行时设置注册表）。"""
    Settings(ctx)


apply.provides = ["settings"]  # 声明：本插件提供 settings 服务