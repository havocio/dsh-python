"""消息与内容块模型（对标 dsh 的 ``message.ts``）。

内容块是「标签联合」：文本、推理、工具调用、工具结果。消息是不可变 dataclass，
携带角色（system/user/assistant）与来源（区分 user/model/tool/plugin）。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Union

from dsh_py.services.llm import ChunkType


def new_id() -> str:
    """生成一条消息的稳定标识。"""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# 内容块（ContentBlock）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TextBlock:
    """纯文本块。"""
    text: str


@dataclass(frozen=True)
class ReasoningBlock:
    """推理过程（thinking）块。"""
    text: str


@dataclass(frozen=True)
class ToolCallBlock:
    """模型发起的一次工具调用（参数保持原始 JSON 字符串）。"""
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResultBlock:
    """工具执行结果，回填到对应工具调用。"""
    tool_call_id: str
    content: tuple                # 结果文本块列表（文本块）
    is_error: bool


# 内容块的联合类型
ContentBlock = Union[TextBlock, ReasoningBlock, ToolCallBlock, ToolResultBlock]


def as_text(content: tuple) -> str:
    """把消息内容里的所有文本块拼成一段字符串。"""
    return "".join(block.text for block in content if isinstance(block, TextBlock))


# --------------------------------------------------------------------------- #
# 来源（MessageSource）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MessageSource:
    """消息来源：who produced this。plugin 来源可携带 form（instructions/recall…）。"""
    kind: str                    # 'user' | 'model' | 'tool' | 'plugin'
    plugin: str = ""            # kind=='plugin' 时的插件名
    form: str = ""              # 语义形态：instructions | recall | catalog | snapshot | notice | relay
    provider: str = ""          # kind=='model' 时的供应商
    model: str = ""             # kind=='model' 时的模型


# --------------------------------------------------------------------------- #
# 消息（Message）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Message:
    """一条不可变消息，跨交付、持久化历史与模型请求共享。"""
    id: str
    role: str                   # 'system' | 'user' | 'assistant'
    content: tuple              # ContentBlock 列表
    source: MessageSource


def create_user_message(content: list[ContentBlock], source: MessageSource | None = None) -> Message:
    """创建一条 user 角色消息。"""
    return Message(id=new_id(), role="user", content=tuple(content), source=source or MessageSource("user"))


def create_assistant_message(
    content: list[ContentBlock],
    provider: str = "",
    model: str = "",
) -> Message:
    """创建一条 model 产出的 assistant 消息（携带供应商/模型来源）。"""
    return Message(
        id=new_id(),
        role="assistant",
        content=tuple(content),
        source=MessageSource("model", provider=provider, model=model),
    )


def is_token_delta(chunk_type: ChunkType) -> bool:
    """判断一个分块是否携带可见的模型输出（首 token 边界）。"""
    return chunk_type in (ChunkType.TEXT_DELTA, ChunkType.REASONING_DELTA, ChunkType.TOOL_CALL_DELTA)


# --------------------------------------------------------------------------- #
# 序列化（供 session 持久化使用）
# --------------------------------------------------------------------------- #
def _encode_value(value: Any) -> Any:
    """递归编码：Message / 内容块 / MessageSource → JSON 安全结构（带类型标记）。"""
    if isinstance(value, Message):
        return {"__msg__": {
            "id": value.id, "role": value.role,
            "content": [_encode_value(b) for b in value.content],
            "source": _encode_value(value.source),
        }}
    if isinstance(value, TextBlock):
        return {"__block__": "text", "text": value.text}
    if isinstance(value, ReasoningBlock):
        return {"__block__": "reasoning", "text": value.text}
    if isinstance(value, ToolCallBlock):
        return {"__block__": "tool-call", "id": value.id, "name": value.name, "arguments": value.arguments}
    if isinstance(value, ToolResultBlock):
        return {"__block__": "tool-result", "tool_call_id": value.tool_call_id,
                "content": [_encode_value(b) for b in value.content], "is_error": value.is_error}
    if isinstance(value, MessageSource):
        return {"__source__": value.kind, "plugin": value.plugin, "form": value.form,
                "provider": value.provider, "model": value.model}
    if isinstance(value, tuple):
        return [_encode_value(x) for x in value]
    if isinstance(value, list):
        return [_encode_value(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _encode_value(x) for k, x in value.items()}
    if is_dataclass(value):
        # 通用 dataclass（如 StreamChunk）→ asdict 后递归编码
        return _encode_value(asdict(value))
    return value


def _decode_value(value: Any) -> Any:
    """递归解码 :func:`_encode_value` 的产物，还原消息对象。"""
    if isinstance(value, dict):
        if "__msg__" in value:
            m = value["__msg__"]
            return Message(
                id=m["id"], role=m["role"],
                content=tuple(_decode_value(b) for b in m["content"]),
                source=_decode_value(m["source"]),
            )
        if "__block__" in value:
            kind = value["__block__"]
            if kind == "text":
                return TextBlock(value["text"])
            if kind == "reasoning":
                return ReasoningBlock(value["text"])
            if kind == "tool-call":
                return ToolCallBlock(id=value["id"], name=value["name"], arguments=value["arguments"])
            if kind == "tool-result":
                return ToolResultBlock(tool_call_id=value["tool_call_id"],
                                       content=tuple(_decode_value(b) for b in value["content"]),
                                       is_error=value["is_error"])
            raise ValueError(f"未知内容块类型：{kind}")
        if "__source__" in value:
            return MessageSource(kind=value["__source__"], plugin=value.get("plugin", ""),
                                 form=value.get("form", ""), provider=value.get("provider", ""),
                                 model=value.get("model", ""))
        return {k: _decode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_value(x) for x in value]
    return value


def encode_payload(value: Any) -> Any:
    """把事件载荷编码成 JSON 安全结构（session 持久化写路径用）。"""
    return _encode_value(value)


def decode_payload(value: Any) -> Any:
    """把 JSON 载荷还原为事件原始结构（session 持久化读路径用）。"""
    return _decode_value(value)
