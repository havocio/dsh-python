"""session-telemetry 捕获协调器测试（B/C 类）：固定分块投影、逻辑记录模型、
身份/严重度映射、redact 瀑布、live 采纳 + 游标、on-demand 回放、ops 记录与卸载清扫。

运行：``python dsh_py/tests/test_session_telemetry.py``（无外部依赖）。
"""

from __future__ import annotations

from types import SimpleNamespace

from dsh_py.core.context import AppContext
from dsh_py.services.session import Session, SessionEvent, SessionHeader
from dsh_py.services.session_telemetry import (
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    _HANDOFF_CURSOR,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.warns: list[str] = []

    def warn(self, message: str) -> None:
        self.warns.append(message)


class _FakeSessions:
    """极简 sessions 服务：list/get 供协调器 live 清扫使用。"""

    def list(self):  # noqa: ANN201 - 对齐 dsh_py 返回 id 列表
        return []

    def get(self, sid):  # noqa: ANN201
        return None


class _RecordingBackend:
    """捕获协调器所需的后端最小契约实现（记录交给它的每条记录）。"""

    def __init__(self) -> None:
        self.records: list[SessionTelemetryRecord] = []
        self.flushed = 0
        self.shutdowns = 0

    def emit(self, record: SessionTelemetryRecord) -> None:
        self.records.append(record)

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


def _ctx() -> AppContext:
    ctx = AppContext()
    ctx.provide("sessions", _FakeSessions())
    ctx.provide("logger", _FakeLogger())
    return ctx


def _make_session(ctx, sid="s1", cwd=None, seed_length=0, seed_events=None):
    header = SessionHeader(
        version=0, id=sid, created_at=0.0, cwd=cwd, seed_length=seed_length
    )
    return Session(ctx, sid, seed_events=seed_events, meta=header)


def _append_messages(session: Session) -> None:
    """向会话追加一组典型事件（含一个重复分块用于去重验证）。"""
    session.append("user/message", {"role": "user", "content": [object()]})
    session.append("assistant/chunk", {"turn": 1, "step": 1, "chunk": "a"})
    session.append("assistant/chunk", {"turn": 1, "step": 1, "chunk": "b"})  # 同 (turn,step) 应被丢弃
    session.append("assistant/message", {"turn": 1, "step": 1, "message": object()})
    session.append(
        "tool/result",
        {"turn": 1, "step": 1, "message": _tool_result(is_error=False)},
    )
    session.append(
        "tool/result",
        {"turn": 1, "step": 2, "message": _tool_result(is_error=True)},
    )
    session.append("turn/end", {"turn": 1, "reason": {"kind": "error", "error": "boom"}})


def _tool_result(is_error: bool):
    """构造一个带 content[0].is_error 的 tool/result message 替身。"""
    block = SimpleNamespace(is_error=is_error)
    return SimpleNamespace(content=[block])


# --------------------------------------------------------------------------- #
# 1. live 捕获：固定分块投影 + 严重度/身份映射
# --------------------------------------------------------------------------- #
def test_live_capture_and_projection() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    SessionTelemetryCoordinator(ctx, backend, "live")

    session = _make_session(ctx, "s1")
    ctx.emit("session/created", session)
    _append_messages(session)

    # assistant/chunk 重复 (turn,step) 应只交付 1 条 ledger 记录
    chunk_records = [r for r in backend.records if r.attributes.get("event.type") == "assistant/chunk"]
    assert len(chunk_records) == 1, f"分块去重失败：{len(chunk_records)} 条"

    # tool/result 两条都在，且严重度按 isError 映射
    tr = [r for r in backend.records if r.attributes.get("event.type") == "tool/result"]
    assert len(tr) == 2
    sev = {r.body and None or r.attributes["event.seq"]: r.severity for r in tr}
    # seq 5 -> isError False -> info；seq 6 -> isError True -> error
    by_seq = {r.attributes["event.seq"]: r.severity for r in tr}
    assert by_seq[5] == "info", by_seq
    assert by_seq[6] == "error", by_seq

    # turn/end 的 reason.kind=='error' -> error
    te = [r for r in backend.records if r.attributes.get("event.type") == "turn/end"]
    assert te and te[0].severity == "error", te

    # 身份属性携带 session.id
    assert all(r.attributes.get("session.id") == "s1" for r in backend.records)
    # 游标已推进到最大 seq
    assert _HANDOFF_CURSOR.get(session) == 7, _HANDOFF_CURSOR.get(session)


def test_identity_attributes_cwd_and_seed() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    SessionTelemetryCoordinator(ctx, backend, "live")
    session = _make_session(ctx, "s2", cwd="/work", seed_length=3)
    ctx.emit("session/created", session)
    session.append("user/message", {"role": "user", "content": [object()]})
    rec = backend.records[0]
    assert rec.attributes["session.cwd"] == "/work"
    assert rec.attributes["session.seed_length"] == 3


# --------------------------------------------------------------------------- #
# 2. redact 瀑布：部署挂载规则改写导出副本
# --------------------------------------------------------------------------- #
def test_redact_waterfall_transform() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    SessionTelemetryCoordinator(ctx, backend, "live")

    def _redact(record, next_fn):  # noqa: ANN001
        record.attributes["redacted"] = "yes"
        return next_fn()

    ctx.on("session-telemetry/record", _redact)
    session = _make_session(ctx, "s3")
    ctx.emit("session/created", session)
    session.append("user/message", {"role": "user", "content": [object()]})
    assert backend.records[0].attributes.get("redacted") == "yes"


# --------------------------------------------------------------------------- #
# 3. on-demand 回放 + 游标（种子只投影不交接）
# --------------------------------------------------------------------------- #
def test_on_demand_replay_with_cursor() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    c = SessionTelemetryCoordinator(ctx, backend, "on-demand")  # 不注册 live 监听器

    # 构造带种子（seq 1,2）的会话：first_live_seq = 3，游标默认 = 2
    seeds = [
        SessionEvent(type="user/message", seq=1, time=0.0, data={}),
        SessionEvent(type="assistant/message", seq=2, time=0.0, data={}),
    ]
    session = _make_session(ctx, "s4", seed_events=seeds)
    assert session.first_live_seq == 3
    # 活事件 seq 3,4
    session.append("user/message", {"role": "user", "content": [object()]})
    session.append("assistant/message", {"turn": 1, "step": 1, "message": object()})

    # 显式回放（不传 through_seq = 全部）：游标以下种子只投影不交接
    c.capture_session(session)
    seqs = [r.attributes["event.seq"] for r in backend.records]
    assert seqs == [3, 4], f"游标以下种子不应被交接：{seqs}"
    assert _HANDOFF_CURSOR.get(session) == 4


# --------------------------------------------------------------------------- #
# 4. ops 记录：agent-error 转发 + 卸载清扫 shutdown 标记
# --------------------------------------------------------------------------- #
def test_agent_error_relay() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    SessionTelemetryCoordinator(ctx, backend, "live")
    session = _make_session(ctx, "s5")
    ctx.emit("session/created", session)
    agent = SimpleNamespace(session=session, id="a1")
    ctx.emit("agent/error", agent, 2, 3, RuntimeError("kaboom"))
    ops = [r for r in backend.records if r.channel == "ops"]
    assert ops, "应有 agent-error ops 记录"
    assert ops[0].attributes["telemetry.op"] == "agent-error"
    assert ops[0].attributes["agent.id"] == "a1"
    assert ops[0].attributes["turn"] == 2 and ops[0].attributes["step"] == 3
    assert ops[0].severity == "error"


def test_dispose_emits_shutdown_markers() -> None:
    ctx = _ctx()
    backend = _RecordingBackend()
    SessionTelemetryCoordinator(ctx, backend, "live")
    session = _make_session(ctx, "s6")
    ctx.emit("session/created", session)
    session.append("user/message", {"role": "user", "content": [object()]})

    # 卸载上下文 → 协调器 effect 为仍采纳的会话捕获 shutdown ops 标记 + 调后端 shutdown
    ctx.dispose()
    shutdowns = [r for r in backend.records if r.channel == "ops" and r.attributes.get("telemetry.op") == "shutdown"]
    assert shutdowns, "卸载应产生 shutdown ops 记录"
    assert backend.shutdowns == 1


if __name__ == "__main__":
    tests = [
        test_live_capture_and_projection,
        test_identity_attributes_cwd_and_seed,
        test_redact_waterfall_transform,
        test_on_demand_replay_with_cursor,
        test_agent_error_relay,
        test_dispose_emits_shutdown_markers,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"== {passed}/{len(tests)} passed ==")
    if passed != len(tests):
        raise SystemExit(1)
