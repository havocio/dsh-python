"""OpenTelemetry 上报后端（对标 dsh 的 ``session-telemetry-otel``）。

按原样组合 OTel SDK——一个 ``LoggerProvider`` + ``BatchLogRecordProcessor`` +
OTLP/HTTP 日志导出器——并把协调器交来的每条记录映射到 ``logger.emit()``。此后一切
（批处理、重试、队列、丢包策略）都用 SDK 文档化的行为，经 ``exporter`` /
``processor`` 直通配置。本包拥有捕获模式与外层 shutdown 截止期：SDK 的 export 超时
不约束其前的 ``forceFlush()`` 等待。

依赖：``opentelemetry-api`` / ``opentelemetry-sdk`` / ``opentelemetry-exporter-otlp-proto-http``
（隔离 venv 安装；见 profile.py 的接线注释）。
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse

from dsh_py.core import schema as z
from dsh_py.services.anonymous_user_id import get_or_create_anonymous_user_id
from dsh_py.services.session_telemetry import (
    SessionTelemetryBackend,
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    SessionTelemetrySharingStatus,
)

# OpenTelemetry SDK（隔离 venv 安装；本模块顶层 import 即要求可用）
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource


# 本包 manifest 即 instrumentation-scope 版本的唯一来源（对齐 dsh-llm 的 attribution 身份）
DSH_PRODUCT = "dsh_py"
DSH_VERSION = "0.1.0"

DEFAULT_TELEMETRY_MODE = "DISABLED"
DEFAULT_SHUTDOWN_TIMEOUT_MILLIS = 3_000
# Node 会把更大的定时器延迟钳到 1ms；这是运行时协议上限，不是部署默认值。
MAX_TIMER_DELAY_MILLIS = 2_147_483_647

DISABLED_FEEDBACK_WARNING = (
    "session telemetry 处于 DISABLED：不会共享任何数据，本次反馈仅留本地"
)
NON_CANONICAL_FEEDBACK_WARNING = (
    "session telemetry 忽略了不在标准会话日志中的反馈事件"
)


class SessionTelemetryMode(str, Enum):
    """由 :class:`Config.mode` 选定的共享策略。"""

    FULL = "FULL"
    FEEDBACK_ONLY = "FEEDBACK_ONLY"
    DISABLED = "DISABLED"


def _drop_record(record: SessionTelemetryRecord) -> None:  # noqa: ARG001 - DISABLED 模式丢弃
    """DISABLED 模式下直接丢弃（``SessionTelemetrySink.emit`` 的空实现）。"""


def _resolve_mode(mode: Optional[str]) -> str:
    """解析默认并在运行期拒绝未知值（先于传输层装配）。"""
    resolved = mode or DEFAULT_TELEMETRY_MODE
    if resolved in (SessionTelemetryMode.FULL.value, SessionTelemetryMode.FEEDBACK_ONLY.value,
                    SessionTelemetryMode.DISABLED.value):
        return resolved
    raise ValueError(f"session-telemetry-otel: 不支持的模式 {mode!r}")


def _sharing_status_for(mode: str) -> SessionTelemetrySharingStatus:
    """把序列化模式映射到 seam 后端无关的共享词汇。"""
    return {
        SessionTelemetryMode.FULL.value: "full",
        SessionTelemetryMode.FEEDBACK_ONLY.value: "feedback-only",
        SessionTelemetryMode.DISABLED.value: "disabled",
    }[mode]


# 三级词汇 -> OTel 严重度号 + 文本
_SEVERITY = {
    "info": (SeverityNumber.INFO, "INFO"),
    "warn": (SeverityNumber.WARN, "WARN"),
    "error": (SeverityNumber.ERROR, "ERROR"),
}


# 插件配置：一个共享策略 + 两个直通 SDK 选项对象 + 一个 DSH 自有的 shutdown 截止期。
# 上传模式在插件加载时校验其端点与 shutdown 截止期；DISABLED 不读二者。
Config = z.object({
    "mode": z.union([
        z.const(SessionTelemetryMode.FULL.value),
        z.const(SessionTelemetryMode.FEEDBACK_ONLY.value),
        z.const(SessionTelemetryMode.DISABLED.value),
    ]).default(DEFAULT_TELEMETRY_MODE),
    # 直通给 OTLP/HTTP 日志导出器：完整 OTLPExporterNodeConfigBase 形状（headers /
    # timeoutMillis / compression / keepAlive …），由 SDK 自有并校验；本包只校验并
    # 取用其中一个字段 url（映射到 Python 的 endpoint）。
    "exporter": z.any().optional(),
    # 直通给 BatchLogRecordProcessor（除 exporter 槽，由本插件填充）；SDK 自有并校验。
    "processor": z.any().optional(),
    # 等待 SDK provider 完整 shutdown 路径的最大时间（毫秒）。
    "shutdownTimeoutMillis": z.number().optional(),
})


class OpenTelemetrySessionBackend(SessionTelemetryBackend):
    """OTel 上报后端（唯一部署加载入口）。

    总是注册 ``sessionTelemetry`` 服务（重复加载即抛）。上传模式装配 SDK 管线并组合
    :class:`SessionTelemetryCoordinator`；DISABLED 不构造任何 SDK 状态，只在记录到反馈
    时告警（数据不出网）。
    """

    inject = ["sessions"]
    Config = Config

    @property
    def sharing(self) -> SessionTelemetrySharingStatus:
        """部署选定的共享策略（后端无关词汇）。"""
        return self._sharing

    def __init__(self, ctx: Any, config: Optional[dict] = None) -> None:
        cfg = config or {}
        mode = _resolve_mode(cfg.get("mode"))
        super().__init__(ctx)
        self._sharing: SessionTelemetrySharingStatus = _sharing_status_for(mode)
        self.provider: Optional[LoggerProvider] = None
        self.shutdown_timeout_millis = DEFAULT_SHUTDOWN_TIMEOUT_MILLIS
        self._ctx = ctx

        if mode == SessionTelemetryMode.DISABLED.value:
            self._direct_emit = _drop_record
            ctx.on("session/event", lambda _s, event: (
                ctx.logger.warn(DISABLED_FEEDBACK_WARNING)
                if getattr(event, "type", None) == "feedback/record" else None
            ))
            return

        exporter = cfg.get("exporter") or {}
        url = exporter.get("url") or exporter.get("endpoint")
        if not url:
            raise ValueError("session-telemetry-otel: exporter.url（或 endpoint）必填（完整 OTLP logs 端点）")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"session-telemetry-otel: exporter.url 必须是 http(s)，实际 {parsed.scheme!r}")

        # 唯一超出 SDK 自有校验的处理器字段：非正批量大小会让 shutdown drain 拼接空批
        # 而不消费队列——部署期即失败而非拆除时挂死。
        processor = cfg.get("processor") or {}
        batch_size = processor.get("maxExportBatchSize")
        if batch_size is not None and (not isinstance(batch_size, int) or batch_size < 1):
            raise ValueError(
                f"session-telemetry-otel: processor.maxExportBatchSize 必须是正整数，实际 {batch_size!r}"
            )

        shutdown_timeout = cfg.get("shutdownTimeoutMillis") or DEFAULT_SHUTDOWN_TIMEOUT_MILLIS
        if not (isinstance(shutdown_timeout, (int, float)) and shutdown_timeout > 0
                and shutdown_timeout <= MAX_TIMER_DELAY_MILLIS):
            raise ValueError(
                f"session-telemetry-otel: shutdownTimeoutMillis 必须是 (0, {MAX_TIMER_DELAY_MILLIS}] 的有限数，"
                f"实际 {shutdown_timeout!r}"
            )
        self.shutdown_timeout_millis = shutdown_timeout

        # 装配 LoggerProvider（OTel JS 的 {resource, processors} 在 Python 里是「构造 +
        # add_log_record_processor」两步）。
        resource = Resource.create({
            "service.name": DSH_PRODUCT,
            "service.version": DSH_VERSION,
            # OTel semconv 标准用户属性，挂在 Resource 上随每批导出携带（收集器按 Resource
            # 聚合，且 id 进程稳定）。
            "user.id": get_or_create_anonymous_user_id(),
        })
        provider = LoggerProvider(resource=resource)
        exporter_kwargs: dict[str, Any] = {"endpoint": url}
        if exporter.get("headers") is not None:
            exporter_kwargs["headers"] = exporter.get("headers")
        if exporter.get("timeoutMillis") is not None:
            # dsh 用 ms，Python 导出器用秒
            exporter_kwargs["timeout"] = exporter.get("timeoutMillis") / 1000.0
        if exporter.get("compression") is not None:
            exporter_kwargs["compression"] = exporter.get("compression")
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(**exporter_kwargs))
        )
        self.provider = provider

        ledger = provider.get_logger("dsh_py.session_telemetry", DSH_VERSION)
        ops = provider.get_logger("dsh_py.session_telemetry.ops", DSH_VERSION)

        def enqueue(record: SessionTelemetryRecord) -> None:
            logger = ops if record.channel == "ops" else ledger
            sev_num, sev_text = _SEVERITY[record.severity]
            logger.emit(LogRecord(
                timestamp=int(record.time * 1_000_000_000),  # 秒 -> 纳秒
                severity_number=sev_num,
                severity_text=sev_text,
                body=record.body,
                attributes=dict(record.attributes),
            ))

        # 协调器使用的 sink：FULL 与 FEEDBACK_ONLY 都用 enqueue（仅 coordinator 触发）；
        # 对外直接 service 调用（self.emit）在 FEEDBACK_ONLY/DISABLED 下丢弃。
        sink = _Sink(emit=enqueue, shutdown=self.shutdown)

        if mode == SessionTelemetryMode.FULL.value:
            self._direct_emit = enqueue
            SessionTelemetryCoordinator(ctx, sink, "live")
            return

        # FEEDBACK_ONLY：直接调用丢弃，仅反馈事件触发标准日志回放（走私有 sink）。
        self._direct_emit = _drop_record
        coordinator = SessionTelemetryCoordinator(ctx, sink, "on-demand")

        def on_event(session: Any, event: Any) -> None:
            if getattr(event, "type", None) != "feedback/record":
                return
            # 同意是已提交记录，而非独立发射的总线值：校验其确在标准日志中。
            idx = event.seq - 1
            if idx < 0 or idx >= len(session.events) or session.events[idx] is not event:
                ctx.logger.warn(NON_CANONICAL_FEEDBACK_WARNING)
                return
            coordinator.capture_session(session, event.seq)

        ctx.on("session/event", on_event)

    def emit(self, record: SessionTelemetryRecord) -> None:
        """仅 FULL 模式把直接服务记录交予 SDK；FEEDBACK_ONLY/DISABLED 直接丢弃。"""
        self._direct_emit(record)

    def shutdown(self) -> None:
        """请求 SDK drain 并静默，但在后端自有截止期后拒绝等待。

        Python OTel 的 ``LoggerProvider.shutdown()`` 是同步的（内部 force_flush + 关闭
        处理器），本方法在守护线程中运行并以 ``shutdownTimeoutMillis`` 为 join 上限，
        超时仅告警（线程后台继续）；DISABLED 无 provider，立即返回。
        """
        provider = self.provider
        if provider is None:
            return
        captured: list[BaseException] = []

        def _run() -> None:
            try:
                provider.shutdown()
            except BaseException as exc:  # noqa: BLE001 - 静默失败，best-effort
                captured.append(exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=self.shutdown_timeout_millis / 1000.0)
        if worker.is_alive():
            try:
                self._ctx.logger.warn(
                    f"telemetry: provider shutdown 超过 {self.shutdown_timeout_millis}ms"
                )
            except Exception:  # noqa: BLE001
                pass
        if captured:
            try:
                self._ctx.logger.warn(f"telemetry: provider shutdown 失败：{captured[0]}")
            except Exception:  # noqa: BLE001
                pass


class _Sink:
    """协调器所需的后端最小契约实现（emit 直连 SDK，shutdown 委托后端）。"""

    __slots__ = ("emit", "shutdown")

    def __init__(self, emit: Any, shutdown: Any) -> None:
        self.emit = emit
        self.shutdown = shutdown


def apply_session_telemetry_otel(ctx: Any, config: Any = None) -> None:
    """插件入口：注册 ``sessionTelemetry`` 服务（OTel 上报后端）。

    默认 mode=DISABLED（仅本地、不出网）。启用示例（profile.py）：

        (apply_session_telemetry_otel, {"mode": "FULL", "exporter": {"url": "http://localhost:4318/v1/logs"}})
    """
    OpenTelemetrySessionBackend(ctx, config or {})


apply_session_telemetry_otel.inject = ["sessions"]
apply_session_telemetry_otel.Config = Config
