"""SDK 跨进程协议（对标 dsh 的 ``@deepseek-ai/dsh-sdk-protocol`` / ``-jsonrpc-server`` / ``-client``）。

- :mod:`dsh_py.api.protocol` —— newline JSON-RPC 2.0 行传输；
- :mod:`dsh_py.api.server` —— :class:`HarnessSdkJsonRpcServer`（运行时进程内）；
- :mod:`dsh_py.api.client` —— :class:`HarnessClient` / :class:`DeepSeekHarness` /
  :class:`HarnessSession`（跨进程客户端，与 ``dsh_py.sdk`` 进程内版同名同 API）。
"""

from dsh_py.api.protocol import (
    JsonRpcLineTransport,
    JsonRpcProtocolError,
    JsonRpcResponseError,
)
from dsh_py.api.server import HarnessSdkJsonRpcServer
from dsh_py.api.client import (
    DeepSeekHarness,
    HarnessClient,
    HarnessSession,
    RunResult,
    SdkProtocolError,
    TransportClosedError,
    final_response,
)

__all__ = [
    "JsonRpcLineTransport",
    "JsonRpcProtocolError",
    "JsonRpcResponseError",
    "HarnessSdkJsonRpcServer",
    "HarnessClient",
    "DeepSeekHarness",
    "HarnessSession",
    "RunResult",
    "TransportClosedError",
    "SdkProtocolError",
    "final_response",
]
