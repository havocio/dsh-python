"""有界输出保留库（util/output-retention，对标 dsh 的 ``dsh-output-retention``）。

给必须封顶返回上下文的工具提供「有界模型可见输出」：调用方把条目或文本块
喂进一个有界收集器，得到保留内容 + 精确省略元数据。

库只拥有「保留了什么、省略了什么」这一机械问题；工具特定语义（文件分组、
行号、退出码、错误状态、逐行预览截断、spill 文件、面向模型的散文）仍归工具
所有。``truncated`` 只表示「因预算省略了本可获得的内容」，**绝不**表示上游
不完整。

- :class:`ItemRetainer` —— 有界有序逻辑单元（路径、grep 命中）。v1 只做
  ``head`` 保留。
- :class:`TextRetainer` —— 面向字节的文本流（stdout/stderr、web body）：
  ``head`` / ``tail`` / ``headTail``，finish 时保持 UTF-8 边界。

纯库：无 ctx、无注册、无事件；保留器状态是每实例的（一次累积），绝不跨调用。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 类型（对齐 dsh 的判别联合，Python 用 dict 表达）
# --------------------------------------------------------------------------- #
def omitted_none() -> dict:
    return {"kind": "none"}


def omitted_exact(count: int) -> dict:
    return {"kind": "exact", "count": count}


def omitted_unknown() -> dict:
    return {"kind": "unknown"}


def _assert_budget(value: Any, name: str) -> None:
    """预算字段必须是非负整数（保留器请求契约）。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")


# --------------------------------------------------------------------------- #
# 条目保留（head）
# --------------------------------------------------------------------------- #
class ItemRetainer:
    """有界有序逻辑单元流：保留前 ``max_items`` 个。

    分组、排序、路径映射、逐单元预览截断、任何 ``incomplete`` 状态都留在
    保留器之外：它只计数和保留。调用方推进准备好的逻辑单元，finish 后自行
    对保留子集做分组/排序。
    """

    def __init__(self, max_items: int) -> None:
        _assert_budget(max_items, "maxItems")
        self._max_items = max_items
        self._items: list = []
        self._seen = 0
        self._omitted_count = 0

    def push(self, item: Any) -> dict:
        """提供一个单元：低于上限时保留，否则丢弃并计入省略。"""
        self._seen += 1
        if len(self._items) < self._max_items:
            self._items.append(item)
            return {"kept": True, "truncated": self._omitted_count > 0}
        self._omitted_count += 1
        return {"kept": False, "truncated": True}

    def finish(self) -> dict:
        """返回保留结果：条目 + 精确省略计数。"""
        omitted = omitted_exact(self._omitted_count) if self._omitted_count > 0 else omitted_none()
        return {
            "items": list(self._items),
            "truncated": self._omitted_count > 0,
            "seen": self._seen,
            "kept": len(self._items),
            "omitted": omitted,
        }


# --------------------------------------------------------------------------- #
# 文本保留（head / tail / headTail，UTF-8 边界保持）
# --------------------------------------------------------------------------- #
def _trim_trailing_partial_utf8(data: bytes) -> bytes:
    """去掉末尾构成半个码点的字节（保留完整的码点前缀）。

    从末尾回退续字节（0x80-0xBF）定位可能的末码点首字节；首字节声明的
    长度越过末尾 → 该码点不完整，整体截掉。
    """
    end = len(data)
    if end == 0:
        return b""
    start = end - 1
    while start >= 0 and (data[start] & 0xC0) == 0x80:
        start -= 1
    if start < 0:
        return b""  # 全部是续字节：整个序列不完整
    first = data[start]
    if first & 0x80 == 0:
        return data  # 单字节 ASCII，必然完整
    if first & 0xE0 == 0xC0:
        length = 2
    elif first & 0xF0 == 0xE0:
        length = 3
    elif first & 0xF8 == 0xF0:
        length = 4
    else:
        return data[:start]  # 非法首字节
    if start + length > end:
        return data[:start]  # 不完整码点 → 截掉
    return data


def _trim_leading_continuation_utf8(data: bytes) -> bytes:
    """去掉开头属于被截断码点的续字节（0x80-0xBF）。"""
    start = 0
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:]


class TextRetainer:
    """面向字节的文本流保留：``head`` 前缀 / ``tail`` 后缀 / ``headTail`` 两端。

    省略按**字节**计（进程/body 安全）；每次切口的 UTF-8 边界都被保持，
    ``text`` 绝不携带切口自身引入的替换字符。无省略时前缀与后缀是同一流的
    相邻切片（head|tail 切分是人为的，码点可能跨切分线）——此时合并解码，
    绝不把边界码点拆半。
    """

    def __init__(self, strategy: dict) -> None:
        kind = strategy["kind"]
        if kind == "head":
            _assert_budget(strategy["maxBytes"], "maxBytes")
            self._prefix_cap = strategy["maxBytes"]
            self._suffix_cap = 0
        elif kind == "tail":
            _assert_budget(strategy["maxBytes"], "maxBytes")
            self._prefix_cap = 0
            self._suffix_cap = strategy["maxBytes"]
        elif kind == "headTail":
            _assert_budget(strategy["headBytes"], "headBytes")
            _assert_budget(strategy["tailBytes"], "tailBytes")
            self._prefix_cap = strategy["headBytes"]
            self._suffix_cap = strategy["tailBytes"]
        else:
            raise ValueError(f"未知文本保留策略：{kind!r}")
        self._prefix_chunks: list[bytes] = []
        self._prefix_held = 0
        self._suffix_chunks: list[bytes] = []
        self._suffix_held = 0
        self._total = 0

    def push(self, chunk: bytes | str) -> dict:
        """提供一个块（bytes 或按 UTF-8 编码的 str）。``kept`` 仅当本块无字节被丢弃。"""
        data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        before = self._total
        self._total += len(data)

        # 前缀：只取到 cap；本块其余部分「未入前缀」
        room = self._prefix_cap - self._prefix_held
        take = max(0, min(room, len(data)))
        if take > 0:
            self._prefix_chunks.append(data[:take])
            self._prefix_held += take

        # 后缀：整块入列，再丢掉完全滑出最后 suffixCap 字节的前导块（有界内存）
        if self._suffix_cap > 0:
            self._suffix_chunks.append(data)
            self._suffix_held += len(data)
            while (self._suffix_chunks
                   and self._suffix_held - len(self._suffix_chunks[0]) >= self._suffix_cap):
                head = self._suffix_chunks.pop(0)
                self._suffix_held -= len(head)
            # 头块可能仍持有超出窗口的前导字节（单块大于窗口时循环保留整块）：
            # 剪掉这些前导字节，让累积器（及 finish 的 concat）受 suffixCap 约束
            if self._suffix_chunks and self._suffix_held > self._suffix_cap:
                excess = self._suffix_held - self._suffix_cap
                self._suffix_chunks[0] = self._suffix_chunks[0][excess:]
                self._suffix_held -= excess

        dropped_this = self._omitted_at(self._total) > self._omitted_at(before)
        return {"kept": not dropped_this, "truncated": self._omitted_at(self._total) > 0}

    def _omitted_at(self, total: int) -> int:
        """已见 ``total`` 字节时的省略数：``total − keptPrefix − keptSuffix``。"""
        prefix_len = min(total, self._prefix_cap)
        suffix_len = min(total - prefix_len, self._suffix_cap)
        return total - prefix_len - suffix_len

    def finish(self) -> dict:
        """定稿：解码保留的前缀与后缀（各自剪到切口 UTF-8 边界）+ 精确省略字节数。"""
        prefix_len = min(self._total, self._prefix_cap)
        suffix_len = min(self._total - prefix_len, self._suffix_cap)

        prefix = b"".join(self._prefix_chunks)  # 恰好 prefixLen 字节
        suffix = b"".join(self._suffix_chunks)
        suffix = suffix[self._suffix_held - suffix_len:]

        budget_omitted = self._omitted_at(self._total)
        if budget_omitted > 0:
            kept_prefix = _trim_trailing_partial_utf8(prefix)
            kept_suffix = _trim_leading_continuation_utf8(suffix)
            text = kept_prefix.decode("utf-8", "replace") + kept_suffix.decode("utf-8", "replace")
        else:
            # 无省略：前缀后缀是同一流的相邻切片，合并解码避免拆半码点
            text = (prefix + suffix).decode("utf-8", "replace")

        # 省略按「实际返回的字节」计（边界修剪也丢弃半码点字节）
        omitted = self._total - len(kept_prefix) - len(kept_suffix) if budget_omitted > 0 else 0
        return {
            "text": text,
            "truncated": omitted > 0,
            "omittedBytes": omitted_exact(omitted) if omitted > 0 else omitted_none(),
        }


# --------------------------------------------------------------------------- #
# 省略措辞与脚注
# --------------------------------------------------------------------------- #
def describe_omitted(omitted: dict, unit: str) -> str:
    """一个 Omitted 值的标准措辞（避免虚假精度）。``none`` 返回空串。"""
    kind = omitted["kind"]
    if kind == "none":
        return ""
    if kind == "exact":
        return f"Omitted {omitted['count']} {unit}."
    return f"More {unit} were omitted."


def format_retention_notice(
    notice: dict,
    recovery: Callable[[dict], str],
) -> str:
    """合成一行脚注：库所有的标准省略子句 + 工具自己的恢复指引。

    库永不拥有恢复措辞——只有工具知道动作（「收窄模式」「取更具体的 URL」
    「读 spill 文件」）；``recovery`` 接收完整 notice（``kept``/``limit``/
    ``omitted``…）返回一句话（或空串）。两半可为空，用单个空格连接。
    """
    parts = [describe_omitted(notice["omitted"], notice["unit"]), recovery(notice)]
    return " ".join(p for p in parts if p != "")
