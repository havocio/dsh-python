"""web 前端桥：把「需要人类作答」的两个 seam 桥到浏览器前端（对标 dsh 的前端 UI 适配器）。

dsh 的浏览器前端经宿主 RPC 消费 cordis 服务；dsh_py 的网关是独立进程，因此
把两个**阻塞式人类交互** seam 做成显式桥：

- **批准**（``ctx.approval``）：服务用 ``ctx.waterfall("approval/request", req, inner=...)``
  分派。本桥注册一个监听器——**只有本连接拥有该会话时才作答**，否则
  ``await next()`` 交给其他连接（多标签页时各答各的会话）。
- **用户提问**（``ctx.userQuestions``）：唯一 provider seam。本桥尝试注册
  provider（一个 ctx 只能有一个），注册失败（别的连接已持有）则本连接不接管
  提问（优雅降级：模型侧拿 ``NO_PROVIDER``）。

没有前端应答时：批准请求在超时后按 ``cancelled`` 结算（fail-closed 语义与
服务一致），提问在超时后抛 ``UserQuestionError``（``ASK_ABORTED``）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Optional

#: 批准请求等待人类作答的超时（秒）；超时按 cancelled 结算。
APPROVAL_TIMEOUT_S = 300.0

#: 用户提问等待人类作答的超时（秒）。
QUESTION_TIMEOUT_S = 600.0


class WebBridge:
    """一条 WebSocket 连接上的交互桥。

    :param ctx: 已装配的 :class:`AppContext`。
    :param notify: 向本连接推通知的协程 ``async (method, params) -> None``。
    :param owns_session: 会话归属判定 ``(session_id) -> bool``（本连接创建的会话）。
    :param register_session: 登记会话归属（切换/新建会话时调用）。
    """

    def __init__(
        self,
        ctx: Any,
        notify: Callable[[str, dict], Any],
        owns_session: Callable[[str], bool],
    ) -> None:
        self.ctx = ctx
        self._notify = notify
        self._owns_session = owns_session
        self.session_id: Optional[str] = None
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._pending_questions: dict[str, asyncio.Future] = {}
        self._disposers: list[Callable[[], Any]] = []
        self._owns_questions = False
        self._closed = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def install(self) -> None:
        """挂载监听器 / provider（幂等；缺服务时静默跳过）。"""
        if self.ctx.has_service("approval"):
            self._disposers.append(self.ctx.on("approval/request", self._on_approval_request))
        if self.ctx.has_service("userQuestions"):
            try:
                dispose = self.ctx.userQuestions.registerProvider(self)
                self._disposers.append(dispose)
                self._owns_questions = True
            except Exception:  # noqa: BLE001 - 已有 provider（别的连接持有）
                self._owns_questions = False

    async def close(self) -> None:
        """卸载并结算全部在途请求（前端已断开 → cancelled / 中止）。"""
        if self._closed:
            return
        self._closed = True
        while self._disposers:
            try:
                self._disposers.pop()()
            except Exception:  # noqa: BLE001 - 单个卸载失败不阻断清理
                pass
        self._settle_all(self._pending_approvals, "cancelled")
        self._settle_all(self._pending_questions, None)

    @staticmethod
    def _settle_all(pending: dict, value: Any) -> None:
        for request_id, future in list(pending.items()):
            if not future.done():
                future.set_result(value)
            pending.pop(request_id, None)

    def owns_questions(self) -> bool:
        """本连接是否持有提问 provider（唯一）。"""
        return self._owns_questions

    # ------------------------------------------------------------------ #
    # 批准
    # ------------------------------------------------------------------ #
    async def _on_approval_request(self, req: Any, next: Any) -> Any:
        """批准 answerer：本连接的会话 → 推给前端等待作答；否则交棒。"""
        session_id = str(getattr(getattr(req, "agent", None), "session", None)
                         and req.agent.session.header.id or "")
        if not session_id or not self._owns_session(session_id):
            return await next()

        request_id = f"apr_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[request_id] = future
        await self._notify("approval/request", {
            "requestId": request_id,
            "sessionId": session_id,
            "toolName": getattr(req, "toolName", ""),
            "callId": getattr(req, "callId", None),
            "reason": getattr(req, "reason", None),
        })
        try:
            outcome = await asyncio.wait_for(future, APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            outcome = "cancelled"
        except asyncio.CancelledError:
            outcome = "cancelled"
        finally:
            self._pending_approvals.pop(request_id, None)
        await self._notify("approval/closed", {"requestId": request_id, "outcome": outcome})
        return outcome

    def decide_approval(self, request_id: str, outcome: str) -> bool:
        """前端作答一次批准请求。

        :returns: 是否命中在途请求（迟到/未知 id 返回 False）。
        """
        future = self._pending_approvals.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(outcome if outcome in ("approved", "rejected", "cancelled")
                          else "rejected")
        return True

    # ------------------------------------------------------------------ #
    # 用户提问（provider 接口）
    # ------------------------------------------------------------------ #
    async def ask(self, request: Any) -> Any:
        """``UserQuestionProvider.ask``——推给前端并等待人类回答。"""
        from dsh_py.services.user_questions import (
            AskUserQuestionAnswer,
            UserQuestionError,
        )

        if not self._owns_questions:
            raise UserQuestionError("no user-questions provider is registered", "NO_PROVIDER")
        request_id = f"qst_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_questions[request_id] = future
        questions = [
            {
                "id": getattr(q, "id", ""),
                "header": getattr(q, "header", ""),
                "question": getattr(q, "question", ""),
                "multiSelect": bool(getattr(q, "multiSelect", False)),
                "options": [
                    {"label": getattr(o, "label", ""), "value": getattr(o, "value", "")}
                    for o in (getattr(q, "options", None) or [])
                ],
            }
            for q in (getattr(request, "questions", None) or [])
        ]
        session_id = ""
        agent = getattr(request, "agent", None) or getattr(request, "caller", None)
        session = getattr(agent, "session", None)
        if session is not None:
            session_id = str(getattr(session.header, "id", ""))
        await self._notify("user-questions/ask", {
            "requestId": request_id,
            "sessionId": session_id,
            "questions": questions,
        })
        try:
            answers = await asyncio.wait_for(future, QUESTION_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._pending_questions.pop(request_id, None)
            raise UserQuestionError("the user-questions request timed out", "ASK_ABORTED")
        except asyncio.CancelledError:
            self._pending_questions.pop(request_id, None)
            raise UserQuestionError("the user-questions request was cancelled", "ASK_ABORTED")
        if answers is None:
            self._pending_questions.pop(request_id, None)
            raise UserQuestionError("the user-questions request was cancelled", "ASK_ABORTED")
        self._pending_questions.pop(request_id, None)
        return AskUserQuestionAnswer(answers=answers)

    def answer_question(self, request_id: str, answers: Any) -> bool:
        """前端回答一次提问。

        :returns: 是否命中在途请求。
        """
        future = self._pending_questions.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(answers)
        return True


__all__ = ["WebBridge", "APPROVAL_TIMEOUT_S", "QUESTION_TIMEOUT_S"]
