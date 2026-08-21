"""名义品牌类型（util/brand，对标 dsh 的 ``@deepseek-ai/dsh-brand``）。

dsh 的 ``Branded<B>`` 是**类型层面**原语（unique symbol，零运行时）：让结构
相同的字符串在类型上不可互换（SessionId 不能传给 CallId）。

Python 无编译期类型系统，品牌用「str 子类 + 类内 ``__new__``」实践表达
（见 :class:`AttachmentId` 的先例）：运行时仍是普通字符串（比较/日志/序列化
行为不变），但构造须经各所有者包自己的工厂，无法被裸字符串静默顶替。

本模块只提供类型注解与文档约定，不拥有任何具体 id。
"""

from __future__ import annotations

from typing import TypeVar

# 品牌参数（编译期注解用；运行时擦除）
B = TypeVar("B", bound=str)

# 类型别名：携带名义品牌 B 的字符串（Python 注解层语义，等价 dsh 的 Branded<B>）
# 用法示例：SessionId = Annotated[str, "brand:SessionId"] 或 str 子类实践。
# 本仓库既有实践：str 子类 + __new__ 品牌化（见 services/attachment.py 的
# AttachmentId），运行时零开销且无法被裸字符串静默顶替。
Branded = str
