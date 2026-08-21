"""持久附件能力 seam（attachment/attachment，第 3 层）。

不可变二进制附件（当前为光栅图像）的引用、限额与存储服务，暴露为
``ctx.attachments``。实现方在发布引用**之前**必须验证字节；引用是内容寻址的
不透明标识符，绝不是文件系统路径或 bearer URL。

- :class:`AttachmentId` —— 不透明存储标识符品牌（``sha256:<hex>``）。
- :class:`AttachmentError` —— 稳定失败类：携带 ``code`` 供 RPC 路由（消费方
  只按 ``code`` 路由，不依赖原型链）。
- :class:`AttachmentStore` —— 抽象存储服务：``imageLimits`` 解析后的图像策略，
  ``validateImage`` 只验证不落盘，``saveImage`` 验证后耐久提交，``readImage``
  读取并校验字节仍与引用一致。

与 dsh 的差异：dsh 的 ``AttachmentError`` 刻意不复用 llm 的 ``HarnessError``
（避免依赖环），dsh_py 的 llm 无此依赖环问题，但为保持线上形状一致仍独立定义
（消费方按 ``code`` 路由）。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Optional, TypedDict

from dsh_py.core.context import AppContext
from dsh_py.core.service import Service


# --------------------------------------------------------------------------- #
# 品牌
# --------------------------------------------------------------------------- #
class AttachmentId(str):
    """不透明内容寻址附件标识符（品牌化字符串）。

    ``AttachmentId(value)`` 直接构造品牌实例（对齐 dsh 的品牌构造函数；
    Python 以 ``__new__`` 实现，避免同名类/函数遮蔽冲突）。
    """

    def __new__(cls, value: str) -> "AttachmentId":
        return str.__new__(cls, value)


# --------------------------------------------------------------------------- #
# 类型
# --------------------------------------------------------------------------- #
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
"""版本一附件路径接受的四种光栅图像格式。"""


class ImageAttachmentRef(TypedDict):
    """一张不可变图像对象的耐久、可序列化元数据。"""

    attachmentId: str          # 不透明存储标识符；绝不是文件系统路径或 bearer URL
    mediaType: str             # 从存储字节验证的媒体类型
    bytes: int                 # 精确编码字节长度
    width: int                 # 固有编码宽度（像素）
    height: int                # 固有编码高度（像素）
    name: Optional[str]        # 可选显示名（已剥离本地路径信息）


class ImageAttachmentLimits(TypedDict):
    """部署解析的限额：上传准入与请求缓冲使用。"""

    maxImageBytes: int
    maxImagesPerMessage: int
    maxMessageImageBytes: int
    maxImagePixels: int
    mediaTypes: tuple[str, ...]


class SaveImageAttachment(TypedDict):
    """验证并耐久提交一张图像的请求。"""

    data: bytes                       # 编码字节
    mediaType: str                    # 调用方声明类型，须与解码字节一致
    name: Optional[str]               # 可选浏览器/提供方显示名；绝不解释为路径


class StoredImageAttachment(TypedDict):
    """引用与摘要校验后返回的存储图像字节。"""

    ref: ImageAttachmentRef
    data: bytes


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #
class AttachmentError(Exception):
    """附件失败类：携带稳定机器路由 ``code``。"""

    def __init__(self, message: str, code: str, cause: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if cause is not None:
            self.__cause__ = cause


# --------------------------------------------------------------------------- #
# 存储服务 seam
# --------------------------------------------------------------------------- #
class AttachmentStore(Service):
    """不可变二进制附件服务：实现方在发布引用前必须验证字节。"""

    def __init__(self, ctx: AppContext, name: str = "attachments") -> None:
        super().__init__(ctx, name)

    @property
    @abstractmethod
    def imageLimits(self) -> ImageAttachmentLimits:
        """部署解析的图像策略，权威与快速路径校验共用。"""

    @abstractmethod
    async def validateImage(self, input: SaveImageAttachment) -> None:
        """验证一张图像而不持久化（批调用方先整体验证再逐个保存）。"""

    @abstractmethod
    async def saveImage(self, input: SaveImageAttachment) -> ImageAttachmentRef:
        """验证并在所属会话事件追加**之前**耐久提交一张图像。"""

    @abstractmethod
    async def readImage(
        self, ref: ImageAttachmentRef, signal: Any = None,
    ) -> StoredImageAttachment:
        """读取一张图像并校验字节仍与引用一致；中止时抛信号原因。"""
