"""attachment / attachment-local 集成验证（第 3 层）。

运行：python dsh_py/tests/test_attachment.py

覆盖：
- 四格式零依赖头解析（PNG/JPEG/WebP/GIF）：媒体类型 + 固有尺寸；
- 准入策略：字节上限 / 像素上限 / 声明类型与字节不符 / 未知格式；
- 内容寻址保存：sha256 引用、对象路径、同内容去重、显示名清洗；
- 读取校验：摘要 + 头字段比对；缺失 → ATTACHMENT_NOT_FOUND；篡改 → ATTACHMENT_CORRUPT；
- 服务缝：``ctx.attachments`` 的 validateImage / saveImage / readImage。
"""

import asyncio
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dsh_py.core.context import AppContext
from dsh_py.loader import CORE_PROFILE, load_profile
from dsh_py.services.attachment import AttachmentError
from dsh_py.services.attachment_image import detect_image, probe_image
from dsh_py.services.attachment_local import apply as attachment_local_apply


# --------------------------------------------------------------------------- #
# 最小合法图像构造（结构有效、尺寸已知）
# --------------------------------------------------------------------------- #
def _png(w=2, h=3):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    def chunk(ctype, payload):
        return struct.pack(">I", len(payload)) + ctype + payload + b"\x00\x00\x00\x00"
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _jpeg(w=4, h=5):
    # SOI + SOF0（precision 8, h, w, 3 分量）+ EOI
    sof = bytes([8]) + struct.pack(">HH", h, w) + bytes([3]) + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    seg = b"\xFF\xC0" + struct.pack(">H", 2 + len(sof)) + sof
    return b"\xFF\xD8" + seg + b"\xFF\xD9"


def _webp(w=6, h=7):
    # RIFF/WEBP 头 + VP8 有损帧块（14 位 LE 宽高，存的是 值-1）
    frame = b"\x9d\x01\x2a" + struct.pack("<HH", w - 1, h - 1) + b"\x00\x00\x00"
    chunk = b"VP8 " + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def _gif(w=8, h=9):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00\x00\x00\x00" + b"\x3B"


def _ctx(tmp_home):
    ctx = AppContext()
    load_profile(ctx, CORE_PROFILE)
    attachment_local_apply(ctx, {"dshHome": tmp_home})
    return ctx


# --------------------------------------------------------------------------- #
# 头解析（纯函数）
# --------------------------------------------------------------------------- #
def test_detect_all_formats():
    cases = [
        (_png(2, 3), "image/png", 2, 3),
        (_jpeg(4, 5), "image/jpeg", 4, 5),
        (_webp(6, 7), "image/webp", 6, 7),
        (_gif(8, 9), "image/gif", 8, 9),
    ]
    for data, media_type, w, h in cases:
        d = detect_image(data)
        assert d.mediaType == media_type, (media_type, d)
        assert d.width == w and d.height == h
        p = probe_image(data)
        assert p.mediaType == media_type and p.width == w and p.height == h


def test_detect_pixel_limit():
    try:
        detect_image(_png(10, 10), max_pixels=50)
    except AttachmentError as e:
        assert e.code == "IMAGE_TOO_MANY_PIXELS"
    else:
        raise AssertionError("应触发像素上限")
    assert detect_image(_png(5, 5), max_pixels=50).width == 5


def test_detect_garbage_rejected():
    try:
        detect_image(b"\x00\x01\x02garbage\xff\xfe")
    except AttachmentError as e:
        assert e.code == "INVALID_IMAGE"
    else:
        raise AssertionError("垃圾字节应被拒绝")


def test_detect_truncated_png_rejected_but_probe_ok():
    full = _png(2, 3)
    truncated = full[:33]  # 签名 + IHDR chunk（头字段完整，但缺 IEND chunk）
    try:
        detect_image(truncated)
    except AttachmentError as e:
        assert e.code == "INVALID_IMAGE"
    else:
        raise AssertionError("截断 PNG 结构校验应拒绝")
    # 头探测：已摘要验证过的字节，只重取头部字段
    p = probe_image(truncated)
    assert p.mediaType == "image/png" and p.width == 2 and p.height == 3


# --------------------------------------------------------------------------- #
# 存储（经服务缝）
# --------------------------------------------------------------------------- #
async def test_save_read_dedup():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        png = _png(2, 3)

        await ctx.attachments.validateImage({"data": png, "mediaType": "image/png"})

        ref1 = await ctx.attachments.saveImage({"data": png, "mediaType": "image/png", "name": r"C:\Users\me\photo.png"})
        ref2 = await ctx.attachments.saveImage({"data": png, "mediaType": "image/png"})

        # 内容寻址：同内容同引用
        assert ref1["attachmentId"] == ref2["attachmentId"]
        assert ref1["attachmentId"].startswith("sha256:")
        assert ref1["mediaType"] == "image/png"
        assert ref1["bytes"] == len(png) and ref1["width"] == 2 and ref1["height"] == 3
        # 显示名剥离路径与控制字符
        assert ref1.get("name") == "photo.png"
        assert "name" not in ref2

        # 对象文件确实存在（objects/<前2位>/<sha256>）
        sha = ref1["attachmentId"][len("sha256:"):]
        obj = os.path.join(tmp, "attachments", "v1", "objects", sha[:2], sha)
        assert os.path.exists(obj)

        stored = await ctx.attachments.readImage(ref1)
        assert stored["data"] == png
        assert stored["ref"] == ref1


async def test_validate_rejects_mismatch_and_oversize():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        # 声明类型与字节不符
        try:
            await ctx.attachments.saveImage({"data": _png(2, 3), "mediaType": "image/jpeg"})
        except AttachmentError as e:
            assert e.code == "IMAGE_TYPE_MISMATCH"
        else:
            raise AssertionError("应触发类型不符")
        # 字节上限
        try:
            await ctx.attachments.saveImage({"data": b"x" * 100, "mediaType": "image/png"})
        except AttachmentError as e:
            assert e.code == "IMAGE_TOO_LARGE" or e.code == "INVALID_IMAGE"
        else:
            raise AssertionError("应触发限额/非法图像")


async def test_read_missing_and_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        png = _png(2, 3)
        ref = await ctx.attachments.saveImage({"data": png, "mediaType": "image/png"})

        # 缺失对象
        ghost = dict(ref)
        ghost["attachmentId"] = "sha256:" + "0" * 64
        try:
            await ctx.attachments.readImage(ghost)
        except AttachmentError as e:
            assert e.code == "ATTACHMENT_NOT_FOUND"
        else:
            raise AssertionError("缺失对象应报 NOT_FOUND")

        # 篡改对象字节 → 摘要不符
        sha = ref["attachmentId"][len("sha256:"):]
        obj = os.path.join(tmp, "attachments", "v1", "objects", sha[:2], sha)
        with open(obj, "wb") as f:
            f.write(_png(9, 9))
        try:
            await ctx.attachments.readImage(ref)
        except AttachmentError as e:
            assert e.code == "ATTACHMENT_CORRUPT"
        else:
            raise AssertionError("篡改应报 CORRUPT")


async def test_invalid_reference_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _ctx(tmp)
        bad = {"attachmentId": "/etc/passwd", "mediaType": "image/png",
               "bytes": 0, "width": 1, "height": 1}
        try:
            await ctx.attachments.readImage(bad)
        except AttachmentError as e:
            assert e.code == "INVALID_ATTACHMENT_REF"
        else:
            raise AssertionError("非法引用应被拒绝")


def _run_all():
    sync_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and not asyncio.iscoroutinefunction(v)]
    async_tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and asyncio.iscoroutinefunction(v)]
    fails = []
    for t in sync_tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for t in async_tests:
        try:
            asyncio.run(t())
        except Exception as e:  # noqa: BLE001
            fails.append((t.__name__, repr(e)))
    for name, err in fails:
        print(f"FAIL {name}: {err}")
    total = len(sync_tests) + len(async_tests)
    print(f"{total} 项，{len(fails)} 失败")
    if fails:
        raise SystemExit(f"\n{len(fails)} 项失败")


if __name__ == "__main__":
    _run_all()
