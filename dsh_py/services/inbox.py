"""Agent 收件箱（对标 dsh 的 ``dsh-agent/inbox``）。

``Inbox`` 是「等待中的用户输入」的投影：``next-turn``（等待独立 turn 的提示）
与 ``next-step``（等待下一 step 边界的输入）两个队列。每次变更（追加 / 预置 /
取出 / 移除 / 清空）都落一条 ``agent/inbox/spliced`` 会话事件（data 为 JSON 安全
dict），因此收件箱可持久化、可重放——跨进程恢复后仍能重建未处理的输入。

事件格式（对标 dsh 的 splice）：``{"target", "start", "removedCount"?, "inserted",
"outcome"?}``（``outcome: "canceled"`` 表示移除即取消）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from dsh_py.services.message import Message
from dsh_py.services.session import Session

# 收件箱目标：等待下一 turn / 下一 step
InboxTarget = str  # 'next-turn' | 'next-step'


class Inbox:
    def __init__(
        self,
        session: Session,
        notifications: Optional[dict[str, Callable[..., None]]] = None,
    ) -> None:
        self.session = session
        self._state: dict[str, list[Message]] = {"next-turn": [], "next-step": []}
        # 通知回调：inserted / discarded / claimed
        self.notifications = notifications or {}

    # -- 只读投影 ------------------------------------------------------------ #
    @property
    def next_turn(self) -> list[Message]:
        """等待独立 turn 的提示列表（只读）。"""
        return list(self._state["next-turn"])

    @property
    def next_step(self) -> list[Message]:
        """等待下一 step 边界的输入列表（只读）。"""
        return list(self._state["next-step"])

    @property
    def has_pending(self) -> bool:
        """两个队列中是否还有待处理输入。"""
        return bool(self._state["next-turn"] or self._state["next-step"])

    # -- 变更 ---------------------------------------------------------------- #
    def append(self, target: InboxTarget, message: Message) -> None:
        """追加一条消息到指定队列（持久化记录插入）。"""
        self._splice(target, len(self._state[target]), 0, [message])

    def prepend(self, target: InboxTarget, message: Message) -> None:
        """预置一条消息到队列头。"""
        self._splice(target, 0, 0, [message])

    def claim(self, target: InboxTarget, turn: int) -> list[Message]:
        """取出下一个 step 的输入批次（对标 dsh 的 claim）。

        取出 ``next-step`` 全部；``target == 'next-turn'`` 时再取 ``next-turn``
        队首作为新 turn 的提示。每条被取出的消息发布 ``claimed`` 通知。
        """
        claimed = self._splice("next-step", 0, len(self._state["next-step"]), [])
        if target == "next-turn":
            claimed.extend(self._splice("next-turn", 0, 1, []))
        for message in claimed:
            notify = self.notifications.get("claimed")
            if notify:
                notify(message, turn)
        return claimed

    def remove(self, message_id: str) -> bool:
        """移除一条等待中的消息（持久化记录取消）；成功返回 True。"""
        for target in ("next-step", "next-turn"):
            index = next((i for i, m in enumerate(self._state[target]) if m.id == message_id), -1)
            if index >= 0:
                removed = self._splice(target, index, 1, [])
                if removed:
                    notify = self.notifications.get("discarded")
                    if notify:
                        notify(removed[0])
                return True
        return False

    def clear(self) -> None:
        """清空全部待处理输入（先清 next-step，再清 next-turn）。"""
        self._splice("next-step", 0, len(self._state["next-step"]), [])
        self._splice("next-turn", 0, len(self._state["next-turn"]), [])

    # -- 持久化 splice ------------------------------------------------------- #
    def _splice(
        self,
        target: InboxTarget,
        start: int,
        removed_count: int,
        inserted: list[Message],
    ) -> list[Message]:
        """执行一次 splice：先落持久化事件，再变更内存投影并发布通知。"""
        splice: dict[str, Any] = {"target": target, "start": start, "inserted": inserted}
        if removed_count:
            splice["removedCount"] = removed_count
            if not inserted:
                splice["outcome"] = "canceled"
        self.session.append("agent/inbox/spliced", splice)
        removed = self._state[target][start:start + removed_count]
        self._state[target][start:start + removed_count] = list(inserted)
        for message in inserted:
            notify = self.notifications.get("inserted")
            if notify:
                notify(message)
        return removed
