"""本地持久附件后端（attachment/attachment-local，第 3 层）。

内容寻址、所有者私有的本地附件存储，根目录为 ``DSH_HOME/attachments/v1``
（``DSH_HOME`` 解析：显式配置 > 环境变量 > ``~/.dsh``）。

- 保存：先完整准入校验，再以 sha256 内容寻址落盘（对象路径
  ``objects/<前2位>/<sha256>``）；临时文件 + 硬链接原子发布，同内容去重，
  目录项 fsync 后才报告耐久引用（Windows 由 NTFS 元数据日志承担，跳过）；
- 读取：按引用取回字节并重算摘要校验，头探测重取媒体类型/尺寸与引用比对；
- 引用为 ``sha256:<hex>`` 品牌字符串，绝不含文件系统路径。

与 dsh 的差异（已注明）：dsh 用 sharp 全解码准入（可抓深层像素损坏），
dsh_py 用结构级头解析（见 :mod:`dsh_py.services.attachment_image`）。
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Any, Optional

from dsh_py.core import schema as z
from dsh_py.core.context import AppContext
from dsh_py.services.attachment import (
    AttachmentError,
    AttachmentId,
    AttachmentStore,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    SaveImageAttachment,
    StoredImageAttachment,
)
from dsh_py.services.attachment_image import detect_image, image_media_types, probe_image
from dsh_py.util.home_paths import resolve_dsh_home


# --------------------------------------------------------------------------- #
# 默认限额
# --------------------------------------------------------------------------- #
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_IMAGES_PER_MESSAGE = 20
DEFAULT_MAX_MESSAGE_IMAGE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000

ID_PATTERN = re.compile(r"^sha256:([a-f0-9]{64})$")

# 本进程已证明耐久的 DSH_HOME（目录存在 ≠ 目录项已耐久）
_durable_homes: set[str] = set()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _display_name(value: Optional[str]) -> Optional[str]:
    """剥离两种分隔符（跨平台客户端会携带反斜杠路径）并清洗控制字符。

    只取叶子名：POSIX 主机把 ``\\`` 当普通字符，若用 basename 会保留
    Windows 客户端的完整本地路径并泄漏进引用与会话日志。
    """
    if value is None:
        return None
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1:]
    clean = "".join(ch for ch in leaf if ch >= " " and ch != "\x7f").strip()[:255]
    return clean if clean != "" else None


def _object_path(root: str, sha256: str) -> str:
    return os.path.join(root, "objects", sha256[:2], sha256)


def _ensure_reference(ref: ImageAttachmentRef) -> str:
    match = ID_PATTERN.match(str(ref.get("attachmentId", "")))
    if match is None:
        raise AttachmentError("附件引用无效", "INVALID_ATTACHMENT_REF")
    return match.group(1)


async def _inspect_metadata(
    data: bytes, declared_media_type: str, max_pixels: Optional[int] = None,
) -> dict:
    """全量准入校验并把编码字节解析成引用元数据（对齐 dsh 的 inspectMetadata）。"""
    if len(data) == 0:
        raise AttachmentError("图像为空", "INVALID_IMAGE")
    detected = detect_image(data, max_pixels)
    if detected.mediaType != declared_media_type:
        raise AttachmentError("声明的图像类型与字节不一致", "IMAGE_TYPE_MISMATCH")
    return {"mediaType": detected.mediaType, "width": detected.width,
            "height": detected.height, "bytes": len(data)}


# --------------------------------------------------------------------------- #
# 文件级操作（对齐 dsh 的 store.ts）
# --------------------------------------------------------------------------- #
def _sync_directory(path: str) -> None:
    """使一个目录的条目耐久（只读目录句柄 fsync）。

    Windows 无法打开目录句柄，NTFS 元数据日志负责目录项耐久——直接跳过
    （对齐 dsh 的 ``process.platform === 'win32'`` 分支）。
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_durable_directory(path: str, boundary: str) -> None:
    """创建一层私有目录并持久化到调用方担保的耐久边界之上的每个祖先条目。

    有意忽略 mkdir 报告的新建与否：并发首个保存可能创建了本进程随后只是
    观察到的层级——「已存在」不等于「已耐久」（创建者可能尚未 sync，崩溃会
    丢掉会话检查点已引用的目录）。重复 sync 无害，跳过未 sync 的有害。
    """
    target = os.path.abspath(path)
    stop = os.path.abspath(boundary)
    os.makedirs(target, mode=0o700, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    level = target
    while level != stop:
        parent = os.path.dirname(level)
        _sync_directory(parent)
        if parent == level:
            return
        level = parent


def _ensure_durable_home(path: str) -> str:
    """建立本进程对 DSH_HOME 条目及其到文件系统根每个祖先的耐久证明。"""
    home = os.path.abspath(path)
    if home not in _durable_homes:
        _ensure_durable_directory(home, os.path.splitdrive(home)[0] + os.sep)
        _durable_homes.add(home)
    return home


async def validate_image_file(input: SaveImageAttachment, limits: ImageAttachmentLimits) -> None:
    """完整准入策略：不触碰存储（对齐 dsh 的 validateImageFile）。"""
    if len(input.get("data", b"")) > limits["maxImageBytes"]:
        raise AttachmentError("图像超过配置的字节上限", "IMAGE_TOO_LARGE")
    await _inspect_metadata(input["data"], input["mediaType"], limits["maxImagePixels"])


async def save_image_file(
    root: str, input: SaveImageAttachment, limits: ImageAttachmentLimits,
) -> ImageAttachmentRef:
    """保存并校验不可变图像字节到版本化附件根之下（内容寻址去重）。"""
    data = input["data"]
    if len(data) > limits["maxImageBytes"]:
        raise AttachmentError("图像超过配置的字节上限", "IMAGE_TOO_LARGE")
    metadata = await _inspect_metadata(data, input["mediaType"], limits["maxImagePixels"])
    sha256 = _digest(data)
    bucket = os.path.join(root, "objects", sha256[:2])
    staging = os.path.join(root, "tmp")
    # 先对 DSH_HOME 本身（相对文件系统根）做一次每进程耐久证明
    boundary = _ensure_durable_home(os.path.dirname(os.path.dirname(os.path.abspath(root))))
    _ensure_durable_directory(bucket, boundary)
    _ensure_durable_directory(staging, boundary)
    temporary = os.path.join(staging, uuid.uuid4().hex)
    target = _object_path(root, sha256)
    handle: Optional[int] = None
    try:
        # Windows 上 os.open 默认文本模式（会把 \n→\r\n、把 0x1A 当 EOF 截断）；
        # 附件字节是二进制，必须显式 O_BINARY（非 Windows 平台该标志为 0）。
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        handle = os.open(temporary, flags, 0o600)
        # os.write 可能部分写入（低层语义），循环写满再 fsync
        view = memoryview(data)
        while view:
            written = os.write(handle, view)
            view = view[written:]
        os.fsync(handle)
        os.close(handle)
        handle = None
        try:
            os.link(temporary, target)
        except FileExistsError:
            # 同一文件系统私有目录下，EEXIST 是唯一可恢复的链接竞态
            with open(target, "rb") as existing_f:
                existing = existing_f.read()
            if _digest(existing) != sha256:
                raise AttachmentError(
                    "已存附件完整性校验失败", "ATTACHMENT_CORRUPT",
                )
        # 目标条目落盘并关闭并发建桶窗口（去重路径重复两次 sync，
        # 因为它可能在对方到达自身耐久边界前观察到对方的链接）
        _sync_directory(bucket)
        _sync_directory(os.path.join(root, "objects"))
        os.unlink(temporary)
    except Exception as error:  # noqa: BLE001
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        if isinstance(error, AttachmentError):
            raise
        raise AttachmentError("无法持久化图像附件", "ATTACHMENT_WRITE_FAILED") from error
    name = _display_name(input.get("name"))
    ref: ImageAttachmentRef = {
        "attachmentId": AttachmentId(f"sha256:{sha256}"),
        **metadata,
    }
    if name is not None:
        ref["name"] = name
    return ref


async def read_image_file(
    root: str, ref: ImageAttachmentRef, signal: Any = None,
) -> StoredImageAttachment:
    """读取一张内容寻址图像并校验（对齐 dsh 的 readImageFile）。"""
    if signal is not None and getattr(signal, "throw_if_aborted", None):
        signal.throw_if_aborted()
    sha256 = _ensure_reference(ref)
    path = _object_path(root, sha256)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError as error:
        if signal is not None and getattr(signal, "throw_if_aborted", None):
            signal.throw_if_aborted()
        raise AttachmentError("附件对象缺失", "ATTACHMENT_NOT_FOUND") from error
    except OSError as error:
        raise AttachmentError("无法读取图像附件", "ATTACHMENT_READ_FAILED") from error
    if signal is not None and getattr(signal, "throw_if_aborted", None):
        signal.throw_if_aborted()
    if _digest(data) != sha256:
        raise AttachmentError("已存附件完整性校验失败", "ATTACHMENT_CORRUPT")
    # 摘要已证明这些字节是准入时全量解码通过的；读路径只重取头部字段
    metadata = probe_image(data)
    if signal is not None and getattr(signal, "throw_if_aborted", None):
        signal.throw_if_aborted()
    if (metadata.mediaType != ref.get("mediaType")
            or len(data) != ref.get("bytes")
            or metadata.width != ref.get("width")
            or metadata.height != ref.get("height")):
        raise AttachmentError("已存附件元数据与引用不一致", "ATTACHMENT_CORRUPT")
    return {"ref": ref, "data": data}


# --------------------------------------------------------------------------- #
# 配置与插件入口
# --------------------------------------------------------------------------- #
Config = z.object({
    "dshHome": z.string().optional(),
    "maxImageBytes": z.integer().default(DEFAULT_MAX_IMAGE_BYTES),
    "maxImagesPerMessage": z.integer().default(DEFAULT_MAX_IMAGES_PER_MESSAGE),
    "maxMessageImageBytes": z.integer().default(DEFAULT_MAX_MESSAGE_IMAGE_BYTES),
    "maxImagePixels": z.integer().default(DEFAULT_MAX_IMAGE_PIXELS),
})


class LocalAttachmentStore(AttachmentStore):
    """持久内容寻址本地附件存储（``ctx.attachments``）。"""

    def __init__(self, ctx: AppContext, config: Optional[dict] = None) -> None:
        super().__init__(ctx, "attachments")
        cfg = config or {}
        self.root = os.path.join(
            resolve_dsh_home(cfg.get("dshHome")), "attachments", "v1",
        )
        self._image_limits: ImageAttachmentLimits = {
            "maxImageBytes": int(cfg.get("maxImageBytes", DEFAULT_MAX_IMAGE_BYTES)),
            "maxImagesPerMessage": int(cfg.get("maxImagesPerMessage", DEFAULT_MAX_IMAGES_PER_MESSAGE)),
            "maxMessageImageBytes": int(cfg.get("maxMessageImageBytes", DEFAULT_MAX_MESSAGE_IMAGE_BYTES)),
            "maxImagePixels": int(cfg.get("maxImagePixels", DEFAULT_MAX_IMAGE_PIXELS)),
            "mediaTypes": image_media_types(),
        }

    @property
    def imageLimits(self) -> ImageAttachmentLimits:
        return self._image_limits

    async def validateImage(self, input: SaveImageAttachment) -> None:
        await validate_image_file(input, self.imageLimits)

    async def saveImage(self, input: SaveImageAttachment) -> ImageAttachmentRef:
        return await save_image_file(self.root, input, self.imageLimits)

    async def readImage(
        self, ref: ImageAttachmentRef, signal: Any = None,
    ) -> StoredImageAttachment:
        return await read_image_file(self.root, ref, signal)


def apply(ctx: AppContext, config: Optional[dict] = None) -> None:
    """插件入口：注册 ``ctx.attachments``（本地内容寻址后端）。"""
    LocalAttachmentStore(ctx, config or {})


apply.Config = Config
apply.name = "attachment-local"
apply.provides = ["attachments"]
