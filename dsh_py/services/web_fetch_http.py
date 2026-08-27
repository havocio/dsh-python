"""``web-fetch-http``：向 ``ctx.web`` 注册匿名公共 HTTP(S) 抓取 provider。

对齐 dsh 的 ``@deepseek-ai/dsh-web-fetch-http``：函数 / 命名空间插件（非
default-export 服务）——它注册进 seam 的抓取注册表，不拥有 ``web`` 键。

安全检索：校验 URL（仅 http/https、无内嵌凭据、限长）、只跟随**同源**重定向、
执行时间与尺寸上限、分类并解码文本，展示交给 ``tool-web``。请求不带浏览器
cookie 或环境凭据。私网 / SSRF 防护未实现——勿在本 provider 可达敏感内网时启用。

传输依赖 ``httpx``（懒加载；缺依赖仅在使用时报错）。dsh 用浏览器 ``fetch``
+ ``TextDecoder``；本实现以 ``httpx.AsyncClient`` 承担传输、以 Python
``bytes.decode`` 承担解码（未知 charset 抛 ``LookupError`` → ``WEB_UNSUPPORTED_CONTENT_TYPE``）。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urljoin

from dsh_py.core.context import AppContext
from dsh_py.services.web import (
    WebError,
    WebFetchBody,
    WebFetchProvider,
    WebFetchRequest,
    WebFetchResult,
    service_or_none,
    throw_if_aborted,
    web_aborted,
)

LOCAL_FETCH_PROVIDER_ID = "http"

DEFAULT_USER_AGENT = "deepseek-harness/0.0.1 (+https://github.com/deepseek-ai)"

# 默认上限（对齐 dsh 的 schemastery Config 默认值）
DEFAULT_MAX_URL_LENGTH = 2048
DEFAULT_MAX_RESPONSE_BYTES = 5_000_000
DEFAULT_MAX_BODY_CHARS = 100_000
DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_MAX_REDIRECTS = 5

# Node 计时器上限；Python 无此限制但保留该常量以对齐配置校验语义
MAX_TIMER_DELAY_MS = 2_147_483_647

# 携带 Location 的 HTTP 重定向状态码
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# 请求头：显式产品代理，绝不伪装浏览器
_ACCEPT = "text/html,application/xhtml+xml,text/*;q=0.9,application/json;q=0.8"


# ---------------------------------------------------------------------------
# 纯函数策略（网络无关的一半）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _UrlParts:
    """URL 解析产物：供同源比较与重定向解析。"""

    scheme: str
    hostname: str
    port: str
    origin: str
    value: str


def validate_fetch_url(input_url: str, max_url_length: int) -> _UrlParts:
    """校验请求 URL 的传输卫生（http(s) 仅、无凭据、限长），返回解析产物。

    抛 :class:`WebError`（``WEB_INVALID_URL`` / ``WEB_BLOCKED_URL``）。
    SSRF / 私网阻断未实现（与 dsh 一致，见包注）。
    """
    if len(input_url) > max_url_length:
        raise WebError(f"URL exceeds the maximum length of {max_url_length}", "WEB_INVALID_URL")
    try:
        parts = urlsplit(input_url)
        port = parts.port
    except ValueError as exc:
        raise WebError(f"invalid URL: {input_url}", "WEB_INVALID_URL", cause=exc) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebError(
            f'unsupported URL scheme "{scheme}:" (only http and https are allowed)',
            "WEB_INVALID_URL",
        )
    if parts.username or parts.password:
        raise WebError("credentials in URLs are not allowed", "WEB_BLOCKED_URL")
    hostname = (parts.hostname or "").lower()
    # 对齐 JS URL.port：默认端口记为空串，显式默认端口也归一为空（同源比较才正确）
    default_port = 80 if scheme == "http" else 443
    port = "" if parts.port is None or parts.port == default_port else str(parts.port)
    origin = f"{scheme}://{hostname}" + (f":{port}" if port else "")
    return _UrlParts(scheme=scheme, hostname=hostname, port=port, origin=origin, value=input_url)


def is_same_origin(a: _UrlParts, b: _UrlParts) -> bool:
    """同源 = scheme / hostname / port 全同。跨源重定向被拒绝。"""
    return a.scheme == b.scheme and a.hostname == b.hostname and a.port == b.port


def classify_content_type(content_type: Optional[str]) -> Optional[str]:
    """把响应 ``Content-Type`` 分类为可解码 kind（html / text），不支持返回 None。"""
    mime = re.sub(r";.*$", "", content_type or "").strip().lower()
    if mime in ("text/html", "application/xhtml+xml"):
        return "html"
    if mime.startswith("text/"):
        return "text"
    if mime in ("application/json", "application/xml") or mime.endswith("+json") or mime.endswith("+xml"):
        return "text"
    return None


def parse_charset(content_type: Optional[str]) -> Optional[str]:
    """提取响应 ``Content-Type`` 的 charset 参数（小写）；无则 None。"""
    match = re.search(r';\s*charset\s*=\s*"?([^";]+)"?', content_type or "", re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip().lower()


def decode_for_charset(charset: Optional[str], raw: bytes) -> str:
    """按声明 charset 解码（缺省 UTF-8）；标签不可识别抛
    ``WEB_UNSUPPORTED_CONTENT_TYPE``（响亮失败胜于乱码）。"""
    try:
        if charset is None or charset in ("", "utf-8", "utf8"):
            return raw.decode("utf-8", errors="replace")
        return raw.decode(charset, errors="replace")
    except LookupError as exc:
        raise WebError(f'unsupported charset "{charset}"', "WEB_UNSUPPORTED_CONTENT_TYPE", cause=exc) from exc


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpFetchLimits:
    """provider 的传输与尺寸上限（插件 apply 提供默认值）。"""

    max_url_length: int
    max_response_bytes: int
    max_body_chars: int
    timeout_ms: int
    max_redirects: int
    user_agent: str


class HttpFetchProvider(WebFetchProvider):
    """匿名公共 HTTP(S) 抓取 provider（id = ``http``）。"""

    id = LOCAL_FETCH_PROVIDER_ID

    def __init__(self, limits: HttpFetchLimits) -> None:
        self.limits = limits

    def available(self) -> bool:
        """无凭据可查——匿名公共抓取器恒可用。"""
        return True

    async def fetch(self, request: WebFetchRequest, signal: Any = None) -> WebFetchResult:
        throw_if_aborted(signal)
        # 一个信号同时停请求与正文读取；asyncio.timeout 区分本 provider 超时
        # 与调用方 / 外层 deadline 取消（TimeoutError → WEB_FETCH_TIMEOUT）。
        try:
            async with asyncio.timeout(self.limits.timeout_ms / 1000):
                import httpx  # 懒加载：缺依赖仅在使用时报错

                async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
                    return await self._follow_and_read(client, request.url, signal)
        except TimeoutError as exc:
            raise WebError("web fetch timed out", "WEB_FETCH_TIMEOUT", cause=exc) from exc
        except asyncio.CancelledError as exc:
            raise web_aborted(signal, exc) from exc

    async def _follow_and_read(self, client: Any, initial_url: str, signal: Any) -> WebFetchResult:
        current = validate_fetch_url(initial_url, self.limits.max_url_length)
        redirects_followed = 0

        while True:
            response = await self._request_once(client, current.value, signal)
            if response.status_code not in _REDIRECT_STATUSES:
                return await self._read_body(response, current, signal)

            # 重定向预算：解析 / 校验下一跳前先判上限
            if redirects_followed >= self.limits.max_redirects:
                await response.aclose()
                raise WebError(
                    f"exceeded the maximum of {self.limits.max_redirects} redirects",
                    "WEB_REDIRECT_BLOCKED",
                )
            location = response.headers.get("location")
            if location is None:
                await response.aclose()
                raise WebError(
                    f"redirect response (HTTP {response.status_code}) without a Location header",
                    "WEB_PROVIDER_ERROR",
                )
            target = urljoin(current.value, location)
            try:
                validated = validate_fetch_url(target, self.limits.max_url_length)
                if not is_same_origin(validated, current):
                    raise WebError(
                        f"cross-origin redirect to {validated.origin} is not followed automatically; "
                        "retry against that URL directly",
                        "WEB_REDIRECT_BLOCKED",
                    )
            except WebError:
                await response.aclose()
                raise
            await response.aclose()
            current = validated
            redirects_followed += 1

    async def _request_once(self, client: Any, url: str, signal: Any) -> Any:
        request = client.build_request(
            "GET", url,
            headers={"user-agent": self.limits.user_agent, "accept": _ACCEPT},
        )
        try:
            return await client.send(request, stream=True)
        except asyncio.CancelledError as exc:
            raise web_aborted(signal, exc) from exc
        except Exception as exc:  # noqa: BLE001 -- httpx 传输/网络故障
            if signal is not None and getattr(signal, "aborted", False):
                raise web_aborted(signal, exc) from exc
            raise WebError(f"web fetch failed: {exc}", "WEB_PROVIDER_ERROR", cause=exc) from exc

    async def _read_body(self, response: Any, final_url: _UrlParts, signal: Any) -> WebFetchResult:
        content_type = response.headers.get("content-type")
        kind = classify_content_type(content_type)
        if kind is None:
            await response.aclose()
            raise WebError(
                f'unsupported content type "{content_type or "unknown"}"',
                "WEB_UNSUPPORTED_CONTENT_TYPE",
            )
        charset = parse_charset(content_type)

        # 先解析解码器再读正文：不支持的 charset 无需消费流即失败
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length > self.limits.max_response_bytes:
                await response.aclose()
                raise WebError(
                    f"response exceeds the maximum of {self.limits.max_response_bytes} bytes",
                    "WEB_FETCH_TOO_LARGE",
                )

        bytes_data, truncated_by_bytes = await self._read_capped(response, signal)
        decoded = decode_for_charset(charset, bytes_data)
        truncated_by_chars = len(decoded) > self.limits.max_body_chars
        content = decoded[: self.limits.max_body_chars] if truncated_by_chars else decoded
        await response.aclose()
        return WebFetchResult(
            url=final_url.value,
            status_code=response.status_code,
            body=WebFetchBody(kind=kind, content=content),
            truncated=truncated_by_bytes or truncated_by_chars,
        )

    async def _read_capped(self, response: Any, signal: Any) -> tuple[bytes, bool]:
        """读响应流到 ``max_response_bytes``。

        ``Content-Length`` 超上限立即拒绝（``WEB_FETCH_TOO_LARGE``）；流在读取中
        超过上限则截断（``truncated_by_bytes``）而非拒绝——服务器少报长度仍能
        得到有界的可用正文。只有**被丢弃**的字节才算截断（恰好顶满不误标）。
        """
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            async for chunk in response.aiter_bytes():
                remaining = self.limits.max_response_bytes - total
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    total += remaining
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        except asyncio.CancelledError as exc:
            raise web_aborted(signal, exc) from exc
        except Exception as exc:  # noqa: BLE001 -- 流中段读取故障
            if signal is not None and getattr(signal, "aborted", False):
                raise web_aborted(signal, exc) from exc
            raise WebError(f"web fetch failed: {exc}", "WEB_PROVIDER_ERROR", cause=exc) from exc
        return b"".join(chunks), truncated


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------

def _assert_positive_finite(name: str, value: Any) -> None:
    """资源上限（字节 / 字符 / 长度 / 超时上限）必须是正有限数。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or value != value:
        raise ValueError(f"web-fetch-http: {name} must be a positive finite number")


def _assert_non_negative_integer(name: str, value: Any) -> None:
    """重定向跳数上限必须是非负整数（0 = 不跟随任何重定向）。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"web-fetch-http: {name} must be a non-negative integer")


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：向 ``ctx.web`` 注册本地 HTTP(S) 抓取 provider。"""
    config = config or {}
    max_url_length = config.get("maxUrlLength", DEFAULT_MAX_URL_LENGTH)
    max_response_bytes = config.get("maxResponseBytes", DEFAULT_MAX_RESPONSE_BYTES)
    max_body_chars = config.get("maxBodyChars", DEFAULT_MAX_BODY_CHARS)
    timeout_ms = config.get("timeoutMs", DEFAULT_TIMEOUT_MS)
    max_redirects = config.get("maxRedirects", DEFAULT_MAX_REDIRECTS)
    user_agent = config.get("userAgent", DEFAULT_USER_AGENT)
    _assert_positive_finite("maxUrlLength", max_url_length)
    _assert_positive_finite("maxResponseBytes", max_response_bytes)
    _assert_positive_finite("maxBodyChars", max_body_chars)
    _assert_positive_finite("timeoutMs", timeout_ms)
    if timeout_ms > MAX_TIMER_DELAY_MS:
        raise ValueError(f"web-fetch-http: timeoutMs must be no greater than {MAX_TIMER_DELAY_MS}")
    _assert_non_negative_integer("maxRedirects", max_redirects)
    limits = HttpFetchLimits(
        max_url_length=max_url_length,
        max_response_bytes=max_response_bytes,
        max_body_chars=max_body_chars,
        timeout_ms=timeout_ms,
        max_redirects=max_redirects,
        user_agent=user_agent,
    )
    web = service_or_none(ctx, "web")
    if web is None:
        raise RuntimeError("web-fetch-http: the web service is not mounted (add dsh_py.services.web:apply)")
    web.register_fetch_provider(HttpFetchProvider(limits))


apply.inject = ["web"]  # 声明：本插件需要 web 服务（供 loader 拓扑排序）

__all__ = [
    "LOCAL_FETCH_PROVIDER_ID",
    "DEFAULT_USER_AGENT",
    "HttpFetchLimits",
    "HttpFetchProvider",
    "validate_fetch_url",
    "is_same_origin",
    "classify_content_type",
    "parse_charset",
    "decode_for_charset",
    "apply",
]
