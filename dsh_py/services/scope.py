"""dsh-scope 的 Python 移植：作用域化注册表的共享存储与 effect 所有权。

对齐 dsh 的 ``@deepseek-ai/dsh-scope``：为「宿主 + 每作用域」形状的注册表
（tools / skills 等）提供分层存储原语。

- :class:`ScopeKey`：不透明、按身份比较的作用域键（对象身份即相等）。
- :func:`bind_scope_parent` / :func:`scope_parent_of` / :func:`scope_chain_of`：
  作用域父链（注册视图沿链向下继承：子作用域可见祖先层；事件准入沿链向上）。
- :func:`create_scope` / :func:`scope_of`：铸造带作用域标签的子上下文；读标签。
- :class:`NamedEntries` / :class:`AnonymousEntries`：插入有序条目表，带幂等 undo。
- :class:`ScopedLayers`：global 层 + 精确作用域层的聚合存储；effect 所有权
  跟随注册上下文，层在完全清空时回收。

适配（dsh_py 差异，已注明）：
- dsh 用 Cordis ``ctx.plugin`` + ``ctx.extend({symbol: key})`` 铸造作用域；dsh_py
  以 ``ctx.extend()`` + 实例属性 ``_dsh_scope`` 打标（:func:`scope_of` 读取）。
- dsh 的 ``ctx.effect`` 是生成器（立即执行 action、yield 返回 undo）；dsh_py 的
  :func:`~dsh_py.core.fiber.Fiber.effect` 直接登记无参清理函数——故
  :meth:`ScopedLayers.effect` 先执行 action 取得 undo，再登记「undo + 层回收 +
  变更通知」为一次性清理，并立即触发变更通知。
- 事件载波（``scopeTarget``/``carrierKeyOf``）与 Cordis filter 路由是 dsh 事件层
  专用设施，dsh_py 事件总线无此概念，故未移植（本包使用者均不需要）。
"""

from __future__ import annotations

from typing import Any, Callable, Generic, Iterator, Optional, TypeVar

from dsh_py.core.context import AppContext

T = TypeVar("T")

# ---------------------------------------------------------------------------
# 作用域键与父链
# ---------------------------------------------------------------------------


class ScopeKey:
    """不透明、按身份比较的作用域键（object 身份即相等；仅作标识符）。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- 诊断用
        return f"<ScopeKey {id(self):x}>"


# 作用域父链：一个键至多一个父（绑定时防环）
_SCOPE_PARENTS: dict[ScopeKey, ScopeKey] = {}

# 上下文上的作用域标签属性名
_SCOPE_ATTR = "_dsh_scope"


def bind_scope_parent(key: ScopeKey, parent: ScopeKey) -> Callable[[ScopeKey], None]:
    """把 ``parent`` 绑为 ``key`` 的外层作用域（一次性；防环）。

    已绑定父的键再次绑定会抛错（重链只能经返回的 binding 完成）。
    :returns: ``rebind(next_parent)`` —— 唯一可重链该键的句柄。
    """
    if key in _SCOPE_PARENTS:
        raise RuntimeError(
            "dsh-scope: scope key is already bound to a parent; re-linking requires the binding returned by the original bind"
        )
    _link_scope_parent(key, parent)
    return lambda next_parent: _link_scope_parent(key, next_parent)


def _link_scope_parent(key: ScopeKey, parent: ScopeKey) -> None:
    cursor: Optional[ScopeKey] = parent
    while cursor is not None:
        if cursor is key:
            raise RuntimeError("dsh-scope: scope parent link would form a cycle")
        cursor = _SCOPE_PARENTS.get(cursor)
    _SCOPE_PARENTS[key] = parent


def scope_parent_of(key: ScopeKey) -> Optional[ScopeKey]:
    """读一个键的外层作用域；根作用域返回 None。"""
    return _SCOPE_PARENTS.get(key)


def scope_chain_of(key: Optional[ScopeKey]) -> list[ScopeKey]:
    """从键到根祖先的链，近者在前：``[key, parent, grandparent, …]``。"""
    chain: list[ScopeKey] = []
    cursor = key
    while cursor is not None:
        chain.append(cursor)
        cursor = _SCOPE_PARENTS.get(cursor)
    return chain


def create_scope(ctx: AppContext, key: ScopeKey, parent: Optional[ScopeKey] = None) -> AppContext:
    """铸造一个以 ``key`` 为作用域标签的子上下文。

    :param parent: 可选；指定后先绑定父链（键必须尚未绑定）。
    :returns: 带标签的子上下文；其 ``dispose()`` 回收该作用域下全部注册。
    """
    if parent is not None:
        bind_scope_parent(key, parent)
    scoped = ctx.extend("scope")
    setattr(scoped, _SCOPE_ATTR, key)
    return scoped


def scope_of(ctx: AppContext) -> Optional[ScopeKey]:
    """读上下文继承的最远作用域标签；未打标返回 None。"""
    return getattr(ctx, _SCOPE_ATTR, None)


# ---------------------------------------------------------------------------
# 条目表
# ---------------------------------------------------------------------------


class NamedEntries(Generic[T]):
    """插入有序命名条目表，带调用方拥有的重名诊断。

    值借用持有。迭代在同一个非空表代内存活；清空后新插入不污染旧迭代器。
    每次成功插入返回该条目的幂等 undo。
    """

    def __init__(self, duplicate_error: Callable[[str], Exception]) -> None:
        self._duplicate_error = duplicate_error
        self._data: dict[str, Any] = {}

    def insert(self, name: str, value: Any) -> Callable[[], None]:
        data = self._data
        if name in data:
            raise self._duplicate_error(name)
        data[name] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            if data.get(name) is value:
                data.pop(name, None)
            if len(data) == 0 and self._data is data:
                self._data = {}

        return undo

    def get(self, name: str) -> Any:
        return self._data.get(name)

    def has(self, name: str) -> bool:
        return name in self._data

    def keys(self) -> Iterator[str]:
        return iter(self._data)

    def entries(self) -> Iterator[tuple[str, Any]]:
        return iter(self._data.items())

    def values(self) -> Iterator[Any]:
        return iter(self._data.values())

    def isEmpty(self) -> bool:  # noqa: N802 -- 对齐 dsh 命名
        return len(self._data) == 0


class AnonymousEntries(Generic[T]):
    """插入有序匿名条目表，各自独立的注册身份（相等值仍是独立注册）。"""

    def __init__(self) -> None:
        self._data: dict[int, Any] = {}
        self._next_id = 0

    def append(self, value: Any) -> Callable[[], None]:
        data = self._data
        entry_id = self._next_id
        self._next_id += 1
        data[entry_id] = value
        active = True

        def undo() -> None:
            nonlocal active
            if not active:
                return
            active = False
            data.pop(entry_id, None)
            if len(data) == 0 and self._data is data:
                self._data = {}

        return undo

    def values(self) -> Iterator[Any]:
        return iter(self._data.values())

    def isEmpty(self) -> bool:  # noqa: N802 -- 对齐 dsh 命名
        return len(self._data) == 0


# ---------------------------------------------------------------------------
# 分层存储
# ---------------------------------------------------------------------------


class ScopeLayer:
    """一个作用域对某注册表的全部贡献的聚合。"""

    def isEmpty(self) -> bool:  # noqa: N802 -- 对齐 dsh 命名
        raise NotImplementedError


class ScopedLayers(Generic[T]):
    """拥有一个注册表的 global 层与精确作用域层。

    读从不创建作用域层。注册同时从上下文推导可见性与 effect 所有权；变更通知
    在 undo 收集后发出；只有完全清空的聚合层才被回收。
    """

    def __init__(
        self,
        create_layer: Callable[[Optional[ScopeKey]], Any],
        on_change: Callable[[], None],
    ) -> None:
        self._create_layer = create_layer
        self._on_change = on_change
        self.global_layer: Any = create_layer(None)
        self._scoped: dict[ScopeKey, Any] = {}

    def peek(self, scope: Optional[ScopeKey]) -> Any:
        """读已存在的精确作用域层；不创建。故意链盲：寻址某作用域**自己的**
        贡献（其限制、其守卫）不得静默拾取祖先的——继承场景用 :meth:`chain_layers`。"""
        if scope is None:
            return None
        return self._scoped.get(scope)

    def chain_layers(self, scope: Optional[ScopeKey]) -> list[Any]:
        """沿父链的已存在层：最远祖先在前、精确作用域最后（按序叠加时
        最近者最后裁决）。"""
        layers: list[Any] = []
        for key in reversed(scope_chain_of(scope)):
            layer = self._scoped.get(key)
            if layer is not None:
                layers.append(layer)
        return layers

    def merge(self, scope: Optional[ScopeKey], pick: Callable[[Any], NamedEntries]) -> dict:
        """global 命名条目 + 作用域链阴影（远→近），近层同名条目胜出。"""
        merged: dict = dict(pick(self.global_layer).entries())
        for layer in self.chain_layers(scope):
            for name, value in pick(layer).entries():
                merged[name] = value
        return merged

    def effect(
        self,
        ctx: AppContext,
        action: Callable[[Any], Callable[[], None]],
        label: str,
        notify: bool = True,
    ) -> Callable[[], bool]:
        """把一次同步层变更挂到其注册上下文：立即执行 action，把「undo + 层
        回收 + 变更通知」登记为 fiber 清理，并立即触发一次变更通知。"""
        scope = scope_of(ctx)
        if scope is None:
            layer = self.global_layer
            created = False
        else:
            existing = self._scoped.get(scope)
            if existing is None:
                layer = self._create_layer(scope)
                self._scoped[scope] = layer
                created = True
            else:
                layer = existing
                created = False

        try:
            undo = action(layer)
        except Exception:
            if scope is not None and created and layer.isEmpty():
                self._scoped.pop(scope, None)
            raise

        def disposer() -> None:
            undo()
            if scope is not None and layer.isEmpty():
                self._scoped.pop(scope, None)
            if notify:
                self._on_change()

        # dsh_py 的 ctx.effect 直接登记无参清理（fiber 卸载时逆序执行），
        # 返回可单独取消的句柄；语义与 dsh 生成器版 effect 一致。注册成功即
        # 立即触发一次变更通知（dsh 在 effect 主体执行后 notify）。
        handle = ctx.effect(disposer, label)
        if notify:
            self._on_change()
        return handle


__all__ = [
    "ScopeKey",
    "ScopeLayer",
    "NamedEntries",
    "AnonymousEntries",
    "ScopedLayers",
    "bind_scope_parent",
    "scope_parent_of",
    "scope_chain_of",
    "create_scope",
    "scope_of",
]
