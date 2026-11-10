"""MCP 工具的 JSON Schema → pydantic 参数模型。

LangChain 的工具参数需要 pydantic 模型（或 dict schema）；MCP 服务端给的是 JSON
Schema。这里只覆盖常用子集（object / string / number / integer / boolean / array /
enum / anyOf(null)），不认识的关键字忽略而不是报错——MCP server 的 schema 往往
带一堆 ``title`` / ``additionalProperties`` 噪音。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

_PRIMITIVES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "null": type(None),
}


def python_type(schema: dict[str, Any] | None) -> Any:
    """JSON Schema 片段 → Python 类型（不认识就 ``Any``）。"""
    if not isinstance(schema, dict):
        return Any
    if "const" in schema:
        return Literal[schema["const"]]  # type: ignore[valid-type]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        values = tuple(value for value in schema["enum"])
        return Literal[values]  # type: ignore[valid-type]
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        members = [python_type(item) for item in schema["anyOf"]]
        non_null = [member for member in members if member is not type(None)]
        if len(non_null) == 1:
            return Optional[non_null[0]] if len(members) > 1 else non_null[0]  # noqa: UP045
        return Any
    kind = str(schema.get("type") or "").strip()
    if kind == "array":
        return list[python_type(schema.get("items"))]  # type: ignore[valid-type]
    return _PRIMITIVES.get(kind, Any)


def build_args_schema(tool_name: str, schema: dict[str, Any] | None) -> Any:
    """把 MCP ``inputSchema`` 变成 pydantic 模型；没有参数时返回 ``None``。

    ``None`` 表示"这个工具不吃参数"，LangChain 会当成无参函数调用。
    """
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    required = {str(item) for item in (schema.get("required") or [])}

    from pydantic import Field, create_model

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
        annotation = python_type(prop_schema)
        description = str(prop_schema.get("description") or "").strip()
        if prop_name in required:
            default: Any = ...
        else:
            default = None
            if annotation is not Any and type(None) not in _union_args(annotation):
                annotation = Optional[annotation]  # noqa: UP045 - 动态类型
        fields[str(prop_name)] = (
            annotation,
            Field(default=default, description=description) if description else default,
        )
    try:
        return create_model(f"{tool_name}_Args", **fields)  # type: ignore[call-overload]
    except Exception:  # noqa: BLE001 - schema 太怪就退化成无 schema，不阻断工具发现
        return None


def _union_args(annotation: Any) -> tuple[Any, ...]:
    return tuple(getattr(annotation, "__args__", ()) or ())


def build_tool_description(description: str, args_schema: Any) -> str:
    """工具描述：原文 + ``Args:`` 字段说明（与既有实现同格式）。"""
    base = str(description or "").strip()
    fields = getattr(args_schema, "model_fields", None) or {}
    lines: list[str] = []
    for name, model_field in fields.items():
        field_description = str(getattr(model_field, "description", "") or "").strip()
        lines.append(f"{name}: {field_description}" if field_description else str(name))
    if not lines:
        return base
    suffix = "Args:\n" + "\n".join(lines)
    return f"{base}\n{suffix}" if base else suffix


__all__ = ["build_args_schema", "build_tool_description", "python_type"]
