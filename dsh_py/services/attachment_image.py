"""光栅检测：准入时结构校验，已验证读取时仅头探测（attachment-local）。

dsh 用 ``sharp`` 做全解码（准入）与头元数据（读取）。dsh_py 框架本体零依赖，
这里用手写的四格式头解析替代：PNG/JPEG/WebP/GIF 都是良定义容器格式，可在不
解码像素的前提下可靠取得固有尺寸与媒体类型。

**与 dsh 的差异（已注明）**：
- dsh 的 ``detectImage`` 通过 sharp 全解码像素（可捕捉深层像素损坏）；dsh_py
  做结构级校验（PNG 逐 chunk 走到 IEND、JPEG 逐段走到 EOI、WebP RIFF 长度与
  chunk 结构），不保证捕捉所有深层像素损坏；
- 错误码与元数据形状与 dsh 一致（消费方只依赖这些）。

函数均为同步纯函数，由异步的存储方法包装调用。
"""

from __future__ import annotations

import struct
from typing import Optional

from dsh_py.services.attachment import AttachmentError, IMAGE_MEDIA_TYPES


# 支持格式的媒体类型表（key 为容器格式名）
MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


class DetectedImage(dict):
    """解码元数据：媒体类型 + 固有尺寸。"""

    mediaType: str
    width: int
    height: int

    def __init__(self, media_type: str, width: int, height: int):
        super().__init__(mediaType=media_type, width=width, height=height)
        self.mediaType = media_type
        self.width = width
        self.height = height


# --------------------------------------------------------------------------- #
# 各格式头解析
# --------------------------------------------------------------------------- #
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _parse_png(data: bytes, structural: bool) -> DetectedImage:
    """PNG：签名 + 首个 IHDR（宽高 BE u32）+ 逐 chunk 校验结构到 IEND。"""
    if len(data) < 8 or not data.startswith(_PNG_SIG):
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    width = height = 0
    offset = 8
    first = True
    saw_iend = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        ctype = data[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
        if first:
            if ctype != b"IHDR" or length != 13:
                raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
            width, height = struct.unpack(">II", data[start:start + 8])
            first = False
        elif ctype == b"IEND":
            saw_iend = True
        offset = end + 4  # 跳过 CRC
        if saw_iend:
            break
    if first or width <= 0 or height <= 0:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    # 结构校验：必须走到 IEND（未截断）
    if structural and not saw_iend:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    return DetectedImage("image/png", width, height)


# SOF 标记：承载精度的帧头（排除 C4 霍夫曼表 / C8 JPEG / CC 算术编码）
_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})
# 无长度字段的独立标记
_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})


def _parse_jpeg(data: bytes, structural: bool) -> DetectedImage:
    """JPEG：SOI + 逐段扫描到 SOF 取宽高（BE u16）；结构校验走到 EOI。"""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    width = height = 0
    offset = 2
    saw_eoi = False
    while offset + 1 < len(data):
        if data[offset] != 0xFF:
            # 非 0xFF 出现在段边界外 → 损坏（跳过填充的 FF）
            offset += 1
            continue
        # 跳过填充 FF
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:  # EOI
            saw_eoi = True
            break
        if marker == 0x00:  # 熵编码数据中的字节填充（FF 00）→ 跳过
            continue
        if marker == 0xD8 or marker in _STANDALONE_MARKERS:
            continue
        if offset + 2 > len(data):
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
        seg_len = struct.unpack(">H", data[offset:offset + 2])[0]
        if seg_len < 2 or offset + seg_len > len(data):
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
        if marker in _SOF_MARKERS and width == 0:
            if seg_len < 7:
                raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
        offset += seg_len
    if width <= 0 or height <= 0:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    if structural and not saw_eoi:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    return DetectedImage("image/jpeg", width, height)


def _parse_webp(data: bytes, structural: bool) -> DetectedImage:
    """WebP：RIFF/WEBP 头 + 首个 VP8 /VP8L /VP8X 子块取宽高。"""
    if len(data) < 30 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    riff_size = struct.unpack("<I", data[4:8])[0]
    if structural and riff_size + 8 != len(data):
        # 允许文件尾部追加（部分工具会 pad），只做下限校验
        if riff_size + 8 > len(data):
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    width = height = 0
    canvas_w = canvas_h = 0
    offset = 12
    while offset + 8 <= len(data):
        ctype = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        start = offset + 8
        end = start + size
        if end > len(data):
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
        if ctype == b"VP8 ":
            if size < 10 or data[start:start + 3] != b"\x9d\x01\x2a":
                raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
            w_raw = struct.unpack("<H", data[start + 3:start + 5])[0]
            h_raw = struct.unpack("<H", data[start + 5:start + 7])[0]
            width = (w_raw & 0x3FFF) + 1
            height = (h_raw & 0x3FFF) + 1
            break
        if ctype == b"VP8L":
            if size < 5 or data[start] != 0x2F:
                raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
            bits = struct.unpack("<I", data[start + 1:start + 5])[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            break
        if ctype == b"VP8X":
            if size < 10:
                raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
            canvas_w = struct.unpack("<I", data[start + 4:start + 7] + b"\x00")[0] + 1
            canvas_h = struct.unpack("<I", data[start + 7:start + 10] + b"\x00")[0] + 1
        offset = end + (size & 1)  # chunk 数据按 2 字节对齐填充
    if width == 0:
        # 扩展容器（VP8X）而无子帧时退回画布尺寸
        if canvas_w > 0 and canvas_h > 0:
            width, height = canvas_w, canvas_h
        else:
            raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    if width <= 0 or height <= 0:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    return DetectedImage("image/webp", width, height)


def _parse_gif(data: bytes, structural: bool) -> DetectedImage:
    """GIF：头签名 + 逻辑屏幕宽高（LE u16）。"""
    if len(data) < 10:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    signature = data[0:6]
    if signature not in (b"GIF87a", b"GIF89a"):
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    width, height = struct.unpack("<HH", data[6:10])
    if width <= 0 or height <= 0:
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    if structural and data[-1] != 0x3B:  # 尾字节应为预告片
        raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")
    return DetectedImage("image/gif", width, height)


_PARSERS = {
    "image/png": _parse_png,
    "image/jpeg": _parse_jpeg,
    "image/webp": _parse_webp,
    "image/gif": _parse_gif,
}


def _decode(data: bytes, structural: bool) -> DetectedImage:
    """按签名路由到对应格式解析器；未知/损坏统一 INVALID_IMAGE。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise AttachmentError("图像为空", "INVALID_IMAGE")
    raw = bytes(data)
    for parser in _PARSERS.values():
        try:
            return parser(raw, structural)
        except AttachmentError:
            continue
    # 全部解析器都拒绝 → 未知格式
    raise AttachmentError("不支持的或损坏的图像数据", "INVALID_IMAGE")


# --------------------------------------------------------------------------- #
# 公开 API（对齐 dsh 的 detectImage / probeImage）
# --------------------------------------------------------------------------- #
def probe_image(data: bytes) -> DetectedImage:
    """仅头探测（已验证读取路径用）：解析头部字段，不做像素级解码。

    摘要已证明这些字节是准入时全量解码通过的，读路径只重取引用字段，
    不承担每次请求的像素放大。
    """
    return _decode(data, structural=False)


def detect_image(data: bytes, max_pixels: Optional[int] = None) -> DetectedImage:
    """准入检测：结构校验 + 可选解码像素上限。

    对四类格式做结构级完整校验（PNG 到 IEND、JPEG 到 EOI、GIF 预告片），
    超出 ``max_pixels`` 时抛 ``IMAGE_TOO_MANY_PIXELS``。
    """
    detected = _decode(data, structural=True)
    if max_pixels is not None and detected.width * detected.height > max_pixels:
        raise AttachmentError(
            "图像超过配置的解码像素上限", "IMAGE_TOO_MANY_PIXELS",
        )
    return detected


def image_media_types() -> tuple[str, ...]:
    """当前支持的媒体类型（与 dsh 的 mediaTypes 常量对齐）。"""
    return IMAGE_MEDIA_TYPES
