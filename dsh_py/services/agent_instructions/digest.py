"""内容身份（agent-instructions/digest，对标 dsh 的 ``digest.ts``）。

为工作区指令的「逐目录去重」与「会话内版本快路径」提供内容指纹。
"""

from __future__ import annotations

import hashlib


def instruction_content_sha1(content: str) -> str:
    """计算贯穿指令加载与会话状态的内容身份。

    :param content: 精确 UTF-8 指令文本。
    :returns: 小写十六进制 SHA-1 摘要。
    """
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def trimmed_instruction_digest(content: str) -> str:
    """计算大小写无关的「去首尾空白」身份，用于逐目录重复抑制。

    首尾空白被裁掉后再哈希，因此一个被符号链接或字节拷贝、仅首尾空白不同的
    兄弟文件仍会折叠成一份渲染文件。
    """
    return instruction_content_sha1(content.strip())
