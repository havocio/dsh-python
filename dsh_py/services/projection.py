"""会话投影（projection）：状态驱动计算单元的注册表与驱动器（对标 dsh 的 ``dsh-session-projection``）。

投影是一种「用日志事件折算出只读视图」的能力 seam：领域插件贡献纯数学
（:class:`ProjectionDefinition` 的 ``init/apply/view``），框架拥有订阅、逐会话
水印缓存与变更通知，消费方读取一致快照或订阅变更流——两边互不相知。

**全值事件规则（承重）**：携带状态的事件必须携带完整的变更后状态，绝不只带
增量——这使每个单元的转移保持廉价、每个被服务的值自描述。

**读阶梯（持久化缓存读侧）**：
- :meth:`SessionProjectionRegistry.snapshot` —— 一致读切面（水印缓存，缺 cell 时
  惰性折整段内存日志）；
- :meth:`SessionProjectionRegistry.checkpoint` —— 状态级检查点（写侧，持久化缓存）；
- :meth:`SessionProjectionRegistry.restore_floor` / ``view_checkpoint`` / ``restore`` ——
  冷读三件套：缓存行 + 尾部重放 + 全值刷新。
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.services.session import Session, SessionEvent


@dataclass
class ProjectionDefinition:
    """一个领域的状态驱动计算单元：三个纯同步函数加声明（绝不是一个不透明 getter）。

    框架在每条已提交会话事件上驱动 :meth:`apply`；领域不持有订阅，只拥有数学。
    三个函数都必须**同步**（异步单元会撕裂消费方的一致性切面），且 ``state``
    必须是纯 JSON（持久化缓存的前置条件）。

    :param key: 本单元拥有的投影键（``SessionProjectionMap`` 的条目名）。
    :param schema: 校验线上载荷（``view`` 输出）的 schema（``core/schema`` 的
        ``validate`` 语义），离开宿主前校验。
    :param init: 空日志的初始状态。
    :param apply: 纯转移：前状态 + 一条已提交事件 → 后状态。单元不关心的事件
        **必须返回同一引用**（``is`` 门控变更通知，产生零下游工作）。
    :param view: 状态 → 线上载荷（读侧投影）。
    :param state_version: 持久化缓存失效版本：序列化状态字段或折叠语义变化时
        递增，旧版本行被丢弃而非前向应用成垃圾。非负整数。
    """

    key: str
    schema: Any
    init: Callable[[], Any]
    apply: Callable[[Any, SessionEvent], Any]
    view: Callable[[Any], Any]
    state_version: int = 1


# 变更流监听器：一个单元对一个会话的值变了。
# 签名：(session, key, value, seq) -> None
ProjectionChangeListener = Callable[[Session, str, Any, int], None]


def _validate(schema: Any, value: Any) -> Any:
    """schema 校验（``None`` 表示透传，测试/简单单位可用）。"""
    return schema.validate(value) if schema is not None else value


class SessionProjectionRegistry(Service):
    """``sessionProjections`` 服务：投影单元表与其驱动器（``ctx.sessionProjections``）。

    服务订阅一次 ``session/event``；每条已提交事件经过每个已注册单元的
    ``apply``（急切驱动），状态引用变化时以 schema 校验过的视图通知变更流。
    cell 惰性构建——事件流过后才注册的单元、或早于注册表的会话，在首次触碰
    （事件或读取）时把 ``init`` 折过内存日志。注册是作用域效果（随调用 fiber
    卸载）：领域插件卸载后其键从快照消失，消费方视作能力缺失。共享同一键的
    注册者共用同一单元并被计数：同插件在 N 个 agent preset 挂载 N 次，键存活到
    最后一个注册者卸载。
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "sessionProjections")
        self._registrations: dict[str, dict] = {}  # key -> {def, cells, refs}
        self._listeners: list[ProjectionChangeListener] = []
        ctx.on("session/event", self._drive)

    # ------------------------------------------------------------------ #
    # 注册 / 订阅
    # ------------------------------------------------------------------ #
    def register(self, definition: ProjectionDefinition) -> Callable[[], None]:
        """注册一个领域的单元；返回精确的注销函数（随调用 fiber 卸载亦可）。"""
        if not isinstance(definition.state_version, int) or definition.state_version < 0:
            raise ValueError(
                f"session projection {definition.key!r} state_version 必须是非负整数，"
                f"得到 {definition.state_version!r}"
            )
        key = definition.key
        existing = self._registrations.get(key)
        if existing is None:
            self._registrations[key] = {
                "def": definition,
                "cells": weakref.WeakKeyDictionary(),
                "refs": 1,
            }
        else:
            # stateVersion 不一致是唯一可命名的兼容性冲突：版本化契约声明缓存
            # 状态形状不同，两个注册者不能共享 cell。
            if existing["def"].state_version != definition.state_version:
                raise ValueError(
                    f"session projection key {key!r} 已以 state_version "
                    f"{existing['def'].state_version} 注册；拒绝与 {definition.state_version} 共享"
                )
            existing["refs"] += 1

        def dispose() -> None:
            live = self._registrations.get(key)
            if live is None:
                return
            live["refs"] -= 1
            if live["refs"] <= 0:
                self._registrations.pop(key, None)

        return dispose

    def on_changed(self, listener: ProjectionChangeListener) -> Callable[[], None]:
        """订阅变更流：每个已注册单元状态引用变化时调用（每提交事件一次）。

        返回精确注销函数。
        """
        self._listeners.append(listener)

        def dispose() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return dispose

    # ------------------------------------------------------------------ #
    # 读侧
    # ------------------------------------------------------------------ #
    def snapshot(self, session: Session) -> dict:
        """对一个会话的所有已注册单元做一次一致读切面（全同步）。

        返回 ``{"as_of_seq": int, "values": {...}}``；``as_of_seq`` 是共享水印
        （每条值所反映的最后一条事件的 seq；空日志为 -1），``values`` 为空表示
        无单元注册。每个值在离开前经过其单元的 schema 校验。
        """
        values: dict[str, Any] = {}
        for registration in self._registrations.values():
            cell = self._cell_for(registration, session)
            values[registration["def"].key] = _validate(registration["def"].schema, 
                registration["def"].view(cell["state"])
            )
        return {"as_of_seq": session.seq - 1, "values": values}

    def checkpoint(self, session: Session) -> dict:
        """对一个会话的所有已注册单元取状态级检查点（持久化缓存的写侧）。

        返回 ``{key: {"ver": int, "seq": int, "val": state}}``。``val`` 是**分离的
        深拷贝**——水印缓存是本注册表的权威可变状态，调用方触达活引用会腐蚀后续
        每次快照与帧。
        """
        rows: dict[str, dict] = {}
        for registration in self._registrations.values():
            cell = self._cell_for(registration, session)
            rows[registration["def"].key] = {
                "ver": registration["def"].state_version,
                "seq": cell["observed_seq"],
                "val": _deep_copy(cell["state"]),
            }
        return rows

    def restore_floor(self, checkpoint: dict) -> Optional[int]:
        """冷读尾部起点：低于最低可用水印一条的事件 seq（一条都不可用时为 0）。

        无单元注册时返回 ``None``（无需读取——``restore`` 无论如何都会提供空值）。
        """
        floor: Optional[int] = None
        for registration in self._registrations.values():
            row = checkpoint.get(registration["def"].key)
            need = (
                (row.get("seq", -1) + 1) if row is not None and row.get("ver") == registration["def"].state_version else 0
            )
            floor = need if floor is None else min(floor, need)
        if floor is None:
            return None
        return max(floor - 1, 0)

    def view_checkpoint(self, checkpoint: dict) -> dict:
        """零 IO 查看：对每个版本匹配的行，直接提供 schema 校验过的 ``view`` 值。

        版本不匹配或缺失的行让该键缺席（冷/列表消费方视作暂不可用，更完整的读
        路径会重折它）。值与行一样陈旧，但绝不错误。
        """
        values: dict[str, Any] = {}
        for registration in self._registrations.values():
            definition = registration["def"]
            row = checkpoint.get(definition.key)
            if row is None or row.get("ver") != definition.state_version:
                continue
            values[definition.key] = _validate(definition.schema, definition.view(row["val"]))
        return values

    def restore(self, checkpoint: dict, events: list[SessionEvent], base_seq: int) -> dict:
        """冷读：把每个已注册单元折过一段存储日志后缀（可用时以其检查点行播种）。

        行的可用条件：``ver`` 匹配活单元的 ``state_version``、不早于 ``base_seq``
        （``seq >= base_seq - 1``）、不越过提供的日志末端（``seq <= end_seq``）。
        不可用行被丢弃且其键从 ``init`` 重折——仅当 ``base_seq == 0`` 时才成立
        （``base_seq > 0`` 时抛出，调用方须从 seq 0 重读）。

        返回 ``{"snapshot": {...}, "checkpoint": {...}}``：切面 + 刷新后的检查点
        行（可直接耐久写回）。
        """
        end_seq = events[-1].seq if events else base_seq - 1
        values: dict[str, Any] = {}
        refreshed: dict[str, dict] = {}
        for registration in self._registrations.values():
            definition = registration["def"]
            row = checkpoint.get(definition.key)
            usable = (
                row is not None
                and row.get("ver") == definition.state_version
                and row.get("seq", -1) >= base_seq - 1
                and row.get("seq", -1) <= end_seq
            )
            if not usable and base_seq > 0:
                raise RuntimeError(
                    f"session projection {definition.key!r} 无法从 seq {base_seq} 恢复："
                    "检查点行缺失、版本不匹配或越过日志末端；请从 seq 0 重读"
                )
            state = row["val"] if usable else definition.init()
            from_seq = row.get("seq", base_seq - 1) if usable else base_seq - 1
            for event in events:
                if event.seq > from_seq:
                    state = definition.apply(state, event)
            values[definition.key] = _validate(definition.schema, definition.view(state))
            refreshed[definition.key] = {"ver": definition.state_version, "seq": end_seq, "val": _deep_copy(state)}
        return {"snapshot": {"as_of_seq": end_seq, "values": values}, "checkpoint": refreshed}

    # ------------------------------------------------------------------ #
    # 驱动
    # ------------------------------------------------------------------ #
    def _build_cell(self, definition: ProjectionDefinition, events: list[SessionEvent]) -> dict:
        """从 ``init`` 把单元折过 ``events``，产出一个水印在最后折叠事件上的 cell。"""
        state = definition.init()
        for event in events:
            state = definition.apply(state, event)
        return {"state": state, "observed_seq": events[-1].seq if events else -1}

    def _cell_for(self, registration: dict, session: Session) -> dict:
        """读取（或惰性构建：折整段内存日志）一个单元的 cell。"""
        cell = registration["cells"].get(session)
        if cell is None:
            cell = self._build_cell(registration["def"], session.events)
            registration["cells"][session] = cell
        return cell

    def _drive(self, session: Session, event: SessionEvent) -> None:
        """急切驱动：把一条已提交事件送过每个已注册单元；引用变化时通知变更流。"""
        for registration in self._registrations.values():
            cell = registration["cells"].get(session)
            if cell is None:
                # 流中途迟到构建：先折该事件之前的日志前缀（seq 即日志索引，
                # 前缀切片精确），再走正常门控。
                prefix = [e for e in session.events if e.seq < event.seq]
                cell = self._build_cell(registration["def"], prefix)
                registration["cells"][session] = cell
            definition = registration["def"]
            next_state = definition.apply(cell["state"], event)
            changed = next_state is not cell["state"]
            cell["state"] = next_state
            cell["observed_seq"] = event.seq
            if changed and self._listeners:
                value = _validate(definition.schema, definition.view(next_state))
                for listener in list(self._listeners):
                    listener(session, definition.key, value, event.seq)


def _deep_copy(value: Any) -> Any:
    """深度拷贝（``state`` 是纯 JSON，拷贝是完备的）。"""
    import copy

    return copy.deepcopy(value)


def apply(ctx: AppContext, config: Any = None) -> None:
    """插件入口：注册 ``sessionProjections`` 服务（投影单元注册表 + 驱动器）。"""
    SessionProjectionRegistry(ctx)


apply.provides = ["sessionProjections"]  # 声明：本插件提供 sessionProjections 服务
