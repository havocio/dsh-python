"""配置 schema 校验（对标 dsh 使用的 schemastery 常用子集）。

插件用 ``Config`` 声明配置结构，``ctx.plugin`` / ``load_profile`` 加载时
**先校验再填充默认值**——配置写错会在启动期明确报错，而不是运行中出怪错。

用法（对齐 schemastery 的链式风格）：

    from dsh_py.core import schema as z

    def apply(ctx, config):
        ...  # config 已是校验后、带默认值的干净结构

    apply.Config = z.object({
        "instructions": z.string().default(""),
        "max_tokens": z.integer().optional(),
        "providers": z.array(z.object({"provider": z.string()})).default([]),
    })

支持的类型：``string / number / integer / boolean / const / any / array /
object / union``，以及组合子 ``optional()`` 与 ``default(v)``。
``object`` 默认**拒绝未知键**（``extra="strip"`` 可改为剥离），配置写错即报错。
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Union


class SchemaError(ValueError):
    """配置校验失败。携带 ``path`` 定位出错字段（如 ``providers.0.baseURL``）。"""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def __str__(self) -> str:
        return f"配置校验失败{(' @ ' + self.path) if self.path else ''}：{self.message}"


class Schema:
    """所有 schema 的基类：``validate(value)`` 返回校验后的值。"""

    def validate(self, value: Any) -> Any:
        raise NotImplementedError

    # 链式组合子
    def optional(self) -> "OptionalSchema":
        """值为 ``None`` 时跳过校验（其余情况照常校验）。"""
        return OptionalSchema(self)

    def default(self, value: Any) -> "DefaultSchema":
        """值为 ``None`` 时填充默认值。"""
        return DefaultSchema(self, value)


class OptionalSchema(Schema):
    def __init__(self, inner: Schema) -> None:
        self.inner = inner

    def validate(self, value: Any) -> Any:
        if value is None:
            return None
        return self.inner.validate(value)


class DefaultSchema(Schema):
    def __init__(self, inner: Schema, default: Any) -> None:
        self.inner = inner
        self._default = default

    def validate(self, value: Any) -> Any:
        if value is None:
            return self._default
        return self.inner.validate(value)


class AnySchema(Schema):
    def validate(self, value: Any) -> Any:
        return value


class StringSchema(Schema):
    def __init__(self, min_length: int = 0, max_length: Optional[int] = None) -> None:
        self.min_length = min_length
        self.max_length = max_length

    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise SchemaError(f"期望字符串，实际为 {type(value).__name__}")
        if len(value) < self.min_length:
            raise SchemaError(f"字符串长度 {len(value)} 小于最小值 {self.min_length}")
        if self.max_length is not None and len(value) > self.max_length:
            raise SchemaError(f"字符串长度 {len(value)} 大于最大值 {self.max_length}")
        return value


class NumberSchema(Schema):
    def __init__(self, minimum: Optional[float] = None, maximum: Optional[float] = None) -> None:
        self.minimum = minimum
        self.maximum = maximum

    def validate(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"期望数字，实际为 {type(value).__name__}")
        if self.minimum is not None and value < self.minimum:
            raise SchemaError(f"数值 {value} 小于最小值 {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise SchemaError(f"数值 {value} 大于最大值 {self.maximum}")
        return value


class IntegerSchema(NumberSchema):
    def validate(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"期望整数，实际为 {type(value).__name__}")
        return int(super().validate(value))


class BooleanSchema(Schema):
    def validate(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise SchemaError(f"期望布尔值，实际为 {type(value).__name__}")
        return value


class ConstSchema(Schema):
    def __init__(self, value: Any) -> None:
        self._value = value

    def validate(self, value: Any) -> Any:
        if value != self._value:
            raise SchemaError(f"期望常量 {self._value!r}，实际为 {value!r}")
        return value


class ArraySchema(Schema):
    def __init__(
        self,
        item: Schema,
        min_length: int = 0,
        max_length: Optional[int] = None,
    ) -> None:
        self.item = item
        self.min_length = min_length
        self.max_length = max_length

    def validate(self, value: Any) -> list:
        if not isinstance(value, list):
            raise SchemaError(f"期望数组，实际为 {type(value).__name__}")
        if len(value) < self.min_length:
            raise SchemaError(f"数组长度 {len(value)} 小于最小值 {self.min_length}")
        if self.max_length is not None and len(value) > self.max_length:
            raise SchemaError(f"数组长度 {len(value)} 大于最大值 {self.max_length}")
        out = []
        for index, item in enumerate(value):
            try:
                out.append(self.item.validate(item))
            except SchemaError as e:
                if e.path:
                    raise SchemaError(e.message, f"{index}.{e.path}") from None
                raise SchemaError(e.message, str(index)) from None
        return out


class ObjectSchema(Schema):
    def __init__(
        self,
        fields: dict[str, Schema],
        extra: Literal["error", "strip"] = "error",
    ) -> None:
        self.fields = fields
        self.extra = extra

    def validate(self, value: Any) -> dict:
        if value is None:
            value = {}  # 缺省视为空对象（对齐 schemastery 的 undefined → {}）
        if not isinstance(value, dict):
            raise SchemaError(f"期望对象，实际为 {type(value).__name__}")
        out: dict[str, Any] = {}
        for key, field_schema in self.fields.items():
            if key in value:
                field_value = value[key]
            else:
                field_value = None
            try:
                out[key] = field_schema.validate(field_value)
            except SchemaError as e:
                if e.path:
                    raise SchemaError(e.message, f"{key}.{e.path}") from None
                raise SchemaError(e.message, key) from None
        unknown = [k for k in value if k not in self.fields]
        if unknown and self.extra == "error":
            raise SchemaError(f"未知字段：{', '.join(map(str, unknown))}")
        return out


class UnionSchema(Schema):
    def __init__(self, branches: list[Schema]) -> None:
        self.branches = branches

    def validate(self, value: Any) -> Any:
        errors = []
        for branch in self.branches:
            try:
                return branch.validate(value)
            except SchemaError as e:
                errors.append(str(e))
        raise SchemaError("不匹配任何联合分支：" + "；".join(errors))


# --------------------------------------------------------------------------- #
# 工厂函数（小写命名，对齐 schemastery 的 z.xxx）
# --------------------------------------------------------------------------- #
def any() -> AnySchema:
    return AnySchema()


def string(min_length: int = 0, max_length: Optional[int] = None) -> StringSchema:
    return StringSchema(min_length, max_length)


def number(minimum: Optional[float] = None, maximum: Optional[float] = None) -> NumberSchema:
    return NumberSchema(minimum, maximum)


def integer(minimum: Optional[int] = None, maximum: Optional[int] = None) -> IntegerSchema:
    return IntegerSchema(minimum, maximum)


def boolean() -> BooleanSchema:
    return BooleanSchema()


def const(value: Any) -> ConstSchema:
    return ConstSchema(value)


def array(item: Schema, min_length: int = 0, max_length: Optional[int] = None) -> ArraySchema:
    return ArraySchema(item, min_length, max_length)


def object(
    fields: dict[str, Schema],
    extra: Literal["error", "strip"] = "error",
) -> ObjectSchema:
    return ObjectSchema(fields, extra)


def union(branches: list[Schema]) -> UnionSchema:
    return UnionSchema(branches)
