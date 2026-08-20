"""品牌化 id（brand）：跨边界稳定身份的命名标记（对标 dsh-llm 的 ``brand``）。

dsh 用 TypeScript 的名义类型（``Branded<B>``）为跨包传递的 id 打标，使
「工具调用关联」「提供方请求诊断」等身份在收件箱、日志与模型请求边界间保持
可读、不可混淆。Python 无名义类型，这里用 :data:`typing.NewType` 提供最接近的
等价物——运行时仍是底层字符串（``NewType`` 本身即可调用构造器），但类型检查器
可区分不同品牌。
"""

from __future__ import annotations

from typing import NewType

#: 一条消息在收件箱、日志与模型请求边界间携带的稳定身份。
MessageId = NewType("MessageId", str)

#: 关联模型发出的工具调用与其结果的 id。真实适配器由提供方签发；mock/回退合成。
CallId = NewType("CallId", str)

#: 提供方签发的请求 id，跨包边界保留用于诊断。
ProviderRequestId = NewType("ProviderRequestId", str)

#: 适配器拥有的、某个模型可选推理强度的标识符。
ReasoningEffortId = NewType("ReasoningEffortId", str)
