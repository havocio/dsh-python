"""session-telemetry-otel 上报后端测试（B/C 类）。

依赖隔离 venv 的 opentelemetry SDK；若未安装则整体 SKIP（不影响全量回归）。

运行：``python -m pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http``
      ``python dsh_py/tests/test_session_telemetry_otel.py``
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

try:
    from opentelemetry._logs import SeverityNumber  # noqa: F401

    from dsh_py.core.context import AppContext
    from dsh_py.services.session import Session, SessionHeader
    from dsh_py.services.session_telemetry_otel import (
        OpenTelemetrySessionBackend,
        apply_session_telemetry_otel,
    )
    _HAVE_OTEL = True
except Exception:  # noqa: BLE001 - 缺 opentelemetry 时整体跳过
    _HAVE_OTEL = False


class _FakeLogger:
    def __init__(self) -> None:
        self.warns: list[str] = []

    def warn(self, message: str) -> None:
        self.warns.append(message)


class _FakeSessions:
    def list(self):  # noqa: ANN201
        return []

    def get(self, sid):  # noqa: ANN201
        return None


class _RecordingLogger:
    """替换 LoggerProvider.get_logger 的录制器：捕获每次 emit 的 LogRecord。"""

    def __init__(self) -> None:
        self.records: list = []

    def emit(self, log_record) -> None:  # noqa: ANN001
        self.records.append(log_record)


def _ctx() -> AppContext:
    ctx = AppContext()
    ctx.provide("sessions", _FakeSessions())
    ctx.provide("logger", _FakeLogger())
    return ctx


def _session(ctx, sid="s1"):
    return Session(ctx, sid, meta=SessionHeader(version=0, id=sid, created_at=0.0))


def _tool_result(is_error: bool):
    return SimpleNamespace(content=[SimpleNamespace(is_error=is_error)])


# --------------------------------------------------------------------------- #
# 0. 依赖缺失 → SKIP
# --------------------------------------------------------------------------- #
def test_otel_dependency_present() -> None:
    assert _HAVE_OTEL, "opentelemetry 未安装，请先在隔离 venv 安装后再跑本用例"


# --------------------------------------------------------------------------- #
# 1. DISABLED 模式：本地、不出网
# --------------------------------------------------------------------------- #
def test_disabled_mode_is_local_only() -> None:
    ctx = _ctx()
    backend = OpenTelemetrySessionBackend(ctx, {"mode": "DISABLED"})
    assert backend.sharing == "disabled"
    assert backend.provider is None
    # 直接 emit 是空操作，不报错
    backend.emit(SimpleNamespace(channel="ledger", time=0.0, severity="info",
                                 attributes={}, body={}))
    # feedback/record 仅告警不出网
    session = _session(ctx, "d1")
    ctx.emit("session/created", session)
    ctx.emit("session/event", session, SimpleNamespace(type="feedback/record", seq=1, time=0.0, data={}))
    assert any("DISABLED" in w for w in ctx.logger.warns)
    # shutdown 无 provider 立即返回
    backend.shutdown()


# --------------------------------------------------------------------------- #
# 2. 配置校验：缺端点 / 非法模式 / 非法批量 / 非法超时
# --------------------------------------------------------------------------- #
def test_full_requires_url() -> None:
    ctx = _ctx()
    try:
        OpenTelemetrySessionBackend(ctx, {"mode": "FULL"})
        raise AssertionError("FULL 模式缺少 exporter.url 应当报错")
    except ValueError as exc:
        assert "url" in str(exc)


def test_unknown_mode_rejected() -> None:
    ctx = _ctx()
    try:
        OpenTelemetrySessionBackend(ctx, {"mode": "NOPE"})
        raise AssertionError("未知模式应当报错")
    except ValueError:
        pass


def test_bad_batch_size_rejected() -> None:
    ctx = _ctx()
    try:
        OpenTelemetrySessionBackend(ctx, {
            "mode": "FULL",
            "exporter": {"url": "http://localhost:4318/v1/logs"},
            "processor": {"maxExportBatchSize": 0},
        })
        raise AssertionError("非正批量大小应当报错")
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# 3. FULL 模式：严重度映射 + 属性透传（不触网）
# --------------------------------------------------------------------------- #
def test_full_mode_severity_and_attribute_mapping() -> None:
    ctx = _ctx()
    recorder = _RecordingLogger()

    def _fake_get_logger(self, name, version=None, **kw):  # noqa: ANN001, ANN002, ANN003
        return recorder

    with patch("dsh_py.services.session_telemetry_otel.LoggerProvider.get_logger", _fake_get_logger):
        backend = OpenTelemetrySessionBackend(ctx, {
            "mode": "FULL",
            "exporter": {"url": "http://127.0.0.1:9/v1/logs"},
        })
        assert backend.sharing == "full"
        session = _session(ctx, "f1")
        ctx.emit("session/created", session)
        session.append("user/message", {"role": "user", "content": [object()]})
        session.append("tool/result", {"turn": 1, "step": 1, "message": _tool_result(is_error=True)})
        session.append("turn/end", {"turn": 1, "reason": {"kind": "error"}})
        backend.shutdown()

    # 至少捕获到 3 条 ledger 记录（user/message + tool/result + turn/end）
    sevs = [r.severity_number for r in recorder.records]
    assert SeverityNumber.ERROR in sevs, f"应含 error 严重度：{sevs}"
    # turn/end error 与 tool/result isError → ERROR；user/message → INFO
    attrs = [dict(r.attributes) for r in recorder.records]
    assert any(a.get("event.type") == "user/message" for a in attrs)
    assert any(a.get("session.id") == "f1" for a in attrs)


# --------------------------------------------------------------------------- #
# 4. 插件入口 + inject 声明
# --------------------------------------------------------------------------- #
def test_plugin_entrypoint() -> None:
    assert getattr(apply_session_telemetry_otel, "inject", None) == ["sessions"]
    assert getattr(apply_session_telemetry_otel, "Config", None) is not None
    ctx = _ctx()
    apply_session_telemetry_otel(ctx, {"mode": "DISABLED"})
    assert ctx.sessionTelemetry is not None  # 已注册 ctx.sessionTelemetry


if __name__ == "__main__":
    if not _HAVE_OTEL:
        print("SKIP test_session_telemetry_otel.py：未安装 opentelemetry SDK（隔离 venv 安装后重跑）")
        raise SystemExit(0)
    tests = [
        test_otel_dependency_present,
        test_disabled_mode_is_local_only,
        test_full_requires_url,
        test_unknown_mode_rejected,
        test_bad_batch_size_rejected,
        test_full_mode_severity_and_attribute_mapping,
        test_plugin_entrypoint,
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
