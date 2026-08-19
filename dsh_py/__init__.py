"""dsh_py —— DeepSeek Harness（dsh）插件模型 / 全插件式框架的纯 Python 复刻。

本包复刻 dsh（基于 cordis）的**核心**架构：
- 服务容器 + 事件总线（``AppContext``）；
- ``Service`` 服务基类；
- ``Plugin`` 插件加载模型：插件通过 ``inject`` 声明依赖，并向 ``llm``、
  ``agent``、``session`` 等接口（seam）做扩展。

完整分步计划见 ``deepseek-harness-Python复刻计划.md``。
"""

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service
from dsh_py.core.events import EventBus

__all__ = ["AppContext", "Service", "EventBus"]
