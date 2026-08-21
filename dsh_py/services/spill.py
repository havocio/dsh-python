"""工具输出 spill 存储 seam（spill/spill，第 3 层）。

``ctx.spillStore``：抽象服务，定义**做什么**——持久化工具的超大文本并返回
面向模型的定位符 + 取回指引——而不说**怎么做**。实现子类化
:class:`SpillStore` 并以插件注册（每个上下文一个实现）。

Service Definition 刻意最小：只有 :meth:`SpillStore.saveText`。它不拥有保留
策略（那是 ``util/output-retention``）、不拥有工具结果替换（那是
spill-policy）、不拥有取回/搜索 API。后端提供适合其存储基质定位符与取回提示。

实现必须遵守的语义：
- ``saveText`` 原样持久化**完整** ``content``，返回不透明定位符、精确字节
  长度与面向模型的取回指引；
- 存储按请求的 ``owner`` 会话作用域；后端选择私有（非全局可读）位置与从
  ``suggestedName`` 派生的无冲突名字（派生绝不等于建议名）；
- 真实存储失败（权限/ENOSPC/后端不可用）时 **REJECT**——由调用方决定如何
  降级（spill-policy 把拒绝当作 best-effort，保留内联结果）。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


class SpillLocator(str):
    """一个已溢出制品的不透明面向模型句柄（品牌字符串）。

    本地后端可能用文件系统路径；远程/数据库后端可能用 URI 或 key。消费方用
    ``retrievalHint`` 渲染它，但**不解析**它。
    """

    def __new__(cls, value: str) -> "SpillLocator":
        return str.__new__(cls, value)


class SpillStore(Service):
    """抽象 spill 存储服务（``ctx.spillStore``，每上下文一个实现）。"""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx, "spillStore")

    @abstractmethod
    async def saveText(self, input: dict) -> dict:
        """把 ``input['content']`` 持久化到会话作用域的 spill 制品。

        :param input: 拥有者、调用方提供的来源字段、建议名与完整文本。
        :returns: 已存制品的 ``{"locator", "bytes", "retrievalHint"}``。
        :raises: 真实存储失败时拒绝（由调用方决定降级）。
        """
