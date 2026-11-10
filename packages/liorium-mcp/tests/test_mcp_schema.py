"""MCP 参数 schema 转换（JSON Schema → pydantic）。"""

from __future__ import annotations

from typing import Any

import pytest

from liorium.mcp.schema import build_args_schema, build_tool_description, python_type


def test_python_type_primitives() -> None:
    assert python_type({"type": "string"}) is str
    assert python_type({"type": "integer"}) is int
    assert python_type({"type": "number"}) is float
    assert python_type({"type": "boolean"}) is bool
    assert python_type({"type": "array", "items": {"type": "string"}}) == list[str]
    assert python_type({"type": "unknown-kind"}) is Any


def test_python_type_enum_and_nullable() -> None:
    from typing import Literal

    assert python_type({"enum": ["a", "b"]}) == Literal["a", "b"]
    assert python_type({"anyOf": [{"type": "string"}, {"type": "null"}]}) == (str | None)


def test_build_args_schema_required_and_optional() -> None:
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要打开的地址"},
            "count": {"type": "integer"},
        },
        "required": ["url"],
    }
    model = build_args_schema("browse", schema)
    assert model is not None
    fields = model.model_fields
    assert fields["url"].is_required() is True
    assert fields["url"].description == "要打开的地址"
    assert fields["count"].is_required() is False

    instance = model(url="https://example.com")
    assert instance.url == "https://example.com"
    assert instance.count is None


def test_build_args_schema_without_properties_returns_none() -> None:
    assert build_args_schema("noop", {"type": "object"}) is None
    assert build_args_schema("noop", None) is None
    assert build_args_schema("noop", {"properties": "not-a-dict"}) is None


def test_build_tool_description_appends_args() -> None:
    schema = {
        "properties": {"q": {"type": "string", "description": "查询词"}},
        "required": ["q"],
    }
    model = build_args_schema("search", schema)
    description = build_tool_description("搜索网页", model)
    assert description.startswith("搜索网页")
    assert "Args:\nq: 查询词" in description
    assert build_tool_description("只有说明", None) == "只有说明"


@pytest.mark.parametrize("schema", [{"type": "object", "properties": {"x": {"type": ["string", "null"]}}}])
def test_weird_schema_does_not_crash(schema) -> None:
    build_args_schema("weird", schema)  # 不抛异常即可
