"""对象 JSON Schema 子集校验（对标 dsh-tools 的 ``assertObjectJsonSchema``）。

dsh 的 ``agent()`` 结构化输出只支持一个刻意收窄的 subset：
``type / properties / required / additionalProperties / items / enum / const / oneOf``
——没有 pattern/format/数值边界。本模块对 schema 形状做遍历校验，超出子集的
键按违规拒绝（运行时包装为 ``UNSUPPORTED_SCHEMA``）。
"""

from __future__ import annotations

from typing import Any

SUPPORTED_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "const", "oneOf"}
)
ALLOWED_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})


class JsonSchemaError(Exception):
    """schema 超出受支持子集时抛出（携带人类可读原因）。"""


def _check(schema: Any, path: str) -> None:
    if not isinstance(schema, dict):
        raise JsonSchemaError(f"{path} must be an object schema")
    for key in schema:
        if key not in SUPPORTED_KEYS:
            raise JsonSchemaError(f"{path}.{key} is not in the supported subset (type/properties/required/additionalProperties/items/enum/const/oneOf)")
    schema_type = schema.get("type")
    if schema_type is not None:
        if not isinstance(schema_type, str) or schema_type not in ALLOWED_TYPES:
            raise JsonSchemaError(f"{path}.type must be one of {sorted(ALLOWED_TYPES)}")
        if schema_type == "object":
            properties = schema.get("properties")
            if properties is not None:
                if not isinstance(properties, dict):
                    raise JsonSchemaError(f"{path}.properties must be an object")
                for name, child in properties.items():
                    _check(child, f"{path}.properties.{name}")
            required = schema.get("required")
            if required is not None:
                if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
                    raise JsonSchemaError(f"{path}.required must be a list of strings")
            if schema.get("additionalProperties") is not None and not isinstance(schema.get("additionalProperties"), bool):
                raise JsonSchemaError(f"{path}.additionalProperties must be a boolean")
        elif schema_type == "array":
            items = schema.get("items")
            if items is not None:
                _check(items, f"{path}.items")
    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or len(one_of) == 0:
            raise JsonSchemaError(f"{path}.oneOf must be a non-empty array")
        for index, child in enumerate(one_of):
            _check(child, f"{path}.oneOf[{index}]")


def assert_object_json_schema(schema: Any) -> None:
    """校验一个 schema 值在受支持的 subset 内；违规抛 :class:`JsonSchemaError`。

    :param schema: 候选 ``agent()`` 结构化输出 schema。
    """
    _check(schema, "schema")
