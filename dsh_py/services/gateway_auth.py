"""网关令牌鉴权（默认关闭、配置驱动）。

为 WebSocket 网关提供**可选**的 bearer 式令牌校验。整体保持「策略在配置里、代码只
提供可插拔钩子」的设计——具体令牌来源/形态由部署决定，本模块不关心：

- 配置了 ``gateway.authToken``（应用配置）**或** 环境变量 ``DSH_GATEWAY_TOKEN`` 时启用；
- 两者皆空 → **不启用**，网关保持开放（本地开发默认，与「部署策略问题」一致）；
- 启用后，客户端须在 ``initialize`` 请求的 ``authToken`` 字段携带令牌，网关以
  常量时间比较匹配后才放行后续请求；鉴权失败统一回 JSON-RPC ``-32099``。

这正好对应差距分析里的「网关鉴权（部署策略问题）」：被移植的是鉴权钩子本身，
而非某一种特定部署形态。
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from dsh_py.api.protocol import JsonRpcError
from dsh_py.core.context import AppContext


class GatewayAuth:
    """可选网关鉴权（令牌匹配）。

    :param token: 期望的令牌；``None`` 或空字符串视为不启用（开放网关）。
    """

    # JSON-RPC 实现自定义错误区间 -32000..-32099 的下界（最大可用码）。
    ERROR_CODE = -32099

    def __init__(self, token: Optional[str]) -> None:
        # 空字符串也视为未启用，避免误配置把空令牌当有效值放行。
        self.enabled = isinstance(token, str) and len(token) > 0
        self._token = token if self.enabled else None

    @classmethod
    def from_config(cls, config: Optional[dict], env: Optional[dict] = None) -> "GatewayAuth":
        """从应用配置或环境变量解析鉴权。

        解析顺序：``config["gateway"]["authToken"]`` → 环境变量 ``DSH_GATEWAY_TOKEN``；
        两者皆空（或类型不符）→ 不启用。
        """
        env = env if env is not None else os.environ
        token: Optional[str] = None
        if isinstance(config, dict):
            gateway = config.get("gateway")
            if isinstance(gateway, dict):
                candidate = gateway.get("authToken")
                if isinstance(candidate, str) and candidate != "":
                    token = candidate
        if token is None:
            env_token = env.get("DSH_GATEWAY_TOKEN")
            token = env_token if isinstance(env_token, str) and env_token != "" else None
        return cls(token)

    def authenticate(self, params: dict) -> bool:
        """校验一次 ``initialize`` 请求是否携带正确令牌（常量时间比较）。

        :param params: ``initialize`` 的 params（可能无 ``authToken`` 字段）。
        :returns: 启用时令牌匹配才为 ``True``；未启用时恒为 ``True``（保持开放）。
        """
        if not self.enabled:
            return True
        if not isinstance(params, dict):
            return False
        presented = params.get("authToken")
        if not isinstance(presented, str):
            return False
        return hmac.compare_digest(presented, self._token)


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：仅在配置了令牌时注册 ``gatewayAuth`` 服务（缺省不注册=开放）。

    令牌优先读 ``ctx.appConfig``（“gateway”.authToken），回退到本插件 profile 配置，
    再回退到环境变量（``from_config`` 内部处理）。未启用则不注册，网关照常开放。
    """
    app_cfg = getattr(ctx, "appConfig", None)
    source = app_cfg if isinstance(app_cfg, dict) else (config if isinstance(config, dict) else {})
    auth = GatewayAuth.from_config(source)
    if auth.enabled:
        ctx.provide("gatewayAuth", auth)
