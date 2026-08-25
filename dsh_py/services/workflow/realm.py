"""把离开脚本执行环境的值物化为纯 JSON，并总化渲染脚本抛出的值。

物化 walk 拒绝 JSON 无法无损保留的值，但信任模型编写的 workflow 脚本：
getter 与协议陷阱可能运行，脚本环境不是安全边界（dsh 的隔离由 worker 提供
强制终止，而非恶意值包含——dsh_py 内联引擎只有事件循环隔离，信任前提一致）。
对标 ``@deepseek-ai/dsh-workflow-worker-thread/realm``。
"""

from __future__ import annotations

from typing import Any


class MaterializeError(Exception):
    """由 :func:`materialize_from_realm` 抛出；调用方把它包装成正确的
    ``WorkflowError`` 码。"""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def render_thrown(error: Any) -> str:
    """把抛出的值渲染为失败文本，绝不抛错。

    优先 ``stack``（宿主或脚本环境的错误 stack 是普通字符串读），回退
    ``message``，再回退 ``str()``。读取这些属性可能运行脚本代码（getter、
    ``__str__``）——在本模块的信任前提内可接受；若该代码本身抛错，则返回
    固定标签（渲染必须全总，保证 ``drive()`` 的永不拒绝契约）。
    """
    try:
        stack = getattr(error, "stack", None) if error is not None else None
        if isinstance(stack, str) and len(stack) > 0:
            return stack
        message = getattr(error, "message", None) if error is not None else None
        if isinstance(message, str) and len(message) > 0:
            return message
        return str(error)
    except Exception:  # noqa: BLE001 - 抛错值上的访问器/toString 抛错
        return "[unrenderable thrown value]"


def _has_plain_prototype(value: Any) -> bool:
    """对象的原型链是否代表纯数据对象：``object``（直接或一层）、``None``。

    Python 里 dict/list/None 都是纯数据容器；这里放宽为「不是自定义类实例」。
    dataclass / 自定义 class 实例拒绝（对标 dsh 的 exotic prototype 拒绝）。
    """
    t = type(value)
    return t in (dict, list) or t.__module__ == "builtins"


def _materialize(value: Any, path: str, seen: set) -> Any:
    t = type(value)
    if value is None or t in (bool, int, float, str):
        if t is float and not _is_finite(value):
            raise MaterializeError(path, "non-finite numbers are not JSON data")
        return value
    if t in (bytes, bytearray):
        raise MaterializeError(path, "bytes are not plain JSON data")
    if isinstance(value, complex):
        raise MaterializeError(path, "complex numbers are not plain JSON data")
    if t is set or t is frozenset:
        raise MaterializeError(path, "sets are not plain JSON data")
    if callable(value):
        raise MaterializeError(path, "functions are not plain JSON data")
    if value is Ellipsis or value is NotImplemented:
        raise MaterializeError(path, "Ellipsis/NotImplemented are not plain JSON data")
    # 其余对象路径
    if id(value) in seen:
        raise MaterializeError(path, "circular references are not JSON data")
    seen.add(id(value))
    try:
        if isinstance(value, list):
            return _materialize_list(value, path, seen)
        if isinstance(value, dict):
            return _materialize_dict(value, path, seen)
        # 自定义类实例 / dataclass / 枚举等 exotic 原型
        raise MaterializeError(path, "only plain JSON data is supported (exotic value)")
    finally:
        seen.discard(id(value))


def _is_finite(value: float) -> bool:
    import math

    return math.isfinite(value)


def _materialize_list(value: list, path: str, seen: set) -> list:
    out: list = []
    for index, item in enumerate(value):
        out.append(_materialize(item, f"{path}[{index}]", seen))
    return out


def _materialize_dict(value: dict, path: str, seen: set) -> dict:
    out: dict = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise MaterializeError(f"{path}.{key}", "non-string keys are not plain JSON data")
        out[key] = _materialize(item, f"{path}.{key}", seen)
    return out


def materialize_from_realm(value: Any, root: str = "value") -> Any:
    """把 ``value``（通常来自脚本环境）复制为普通宿主 JSON 数据。

    根 ``None``/``undefined`` 原样返回（脚本无返回值时 result 用 ``None``）；
    嵌套的不支持值以违规路径失败。属性访问器正常执行，读取抛错被包装为渲染
    后的失败。

    :param value: 要物化的环境值。
    :param root: 根值的路径标签（错误消息用）。
    :returns: 宿主环境副本（仅纯对象/数组/标量）。
    :raises MaterializeError: 不支持的值、循环、非有限数、exotic 原型或抛错读取。
    """
    if value is None:
        return None
    try:
        return _materialize(value, root, set())
    except MaterializeError:
        raise
    except Exception as error:  # noqa: BLE001 - 属性读取运行了脚本代码
        raise MaterializeError(root, f"reading the value threw: {render_thrown(error)}") from error
