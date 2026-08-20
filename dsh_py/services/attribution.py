"""应用归属（attribution）：提供方请求的非机密产品身份（对标 dsh-llm 的 ``attribution``）。

每个 HTTP 型 LLM 适配器都必须在其每次提供方请求上发送静态的
``User-Agent`` 归属标识（RFC 9110 §10.1.5 的 product/comment 语法）：
``product/version (+url)``。归属字段只含公开产品事实——绝不携带密钥、本地路径、
会话 id、提示词文本或逐用户标识，也不受逐请求数据影响（白标部署可通过传入
自定义 :class:`AppIdentity` 覆盖默认值，但省略时回退到 harness 默认而非抑制归属）。

本模块是强制默认归属的唯一权威（对齐 dsh 的 Agent Note
``mandatory-app-attribution-headers``）：适配器无需手动复制版本常量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 产品版本：优先从包元数据读取（对齐 dsh「从 manifest 读取、绝不手抄常量」），
# 未安装时可回退到构建期常量，避免 import 失败。
_APP_VERSION: Optional[str] = None


def _resolve_version() -> str:
    """从安装元数据解析产品版本；失败回退到构建期常量。"""
    global _APP_VERSION
    if _APP_VERSION is not None:
        return _APP_VERSION
    try:
        from importlib.metadata import PackageNotFoundError, version

        _APP_VERSION = version("dsh-py")
    except (PackageNotFoundError, Exception):  # noqa: BLE001 - 任何解析失败都回退常量
        _APP_VERSION = "0.1.0"
    return _APP_VERSION


@dataclass(frozen=True)
class AppIdentity:
    """发送给 LLM 提供方的静态公开应用身份。

    每个字段都是公开产品事实，可在每个请求上安全发送；逐请求数据不得影响取值。
    """

    product: str   # ``User-Agent`` 产品 token（小写、连字符）
    version: str   # 产品版本（来源包元数据，绝不手抄）
    url: str       # 应用仓库主页（用作 ``User-Agent`` 注释）


#: harness 自身的默认身份：每个适配器默认发送。省略时回退到此默认，无法完全抑制归属。
APP_IDENTITY = AppIdentity(
    product="deepseek-harness",
    version=_resolve_version(),
    url="https://github.com/deepseek-ai/deepseek-harness",
)


def user_agent(identity: Optional[AppIdentity] = None) -> str:
    """标准 ``User-Agent`` 值：``product/version (+url)``（RFC 9110 product/comment）。"""
    ident = identity or APP_IDENTITY
    return f"{ident.product}/{ident.version} (+{ident.url})"


def attribution_headers(identity: Optional[AppIdentity] = None) -> dict[str, str]:
    """构造适配器必须在每个提供方请求上发送的归属头（当前仅 ``user-agent``）。

    头部名小写——HTTP 字段名在线上不区分大小写。
    """
    return {"user-agent": user_agent(identity)}
