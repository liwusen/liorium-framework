"""把 MCP 工具规格变成 LangChain 工具（名字 ``<server>_<tool>``）。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .client import McpToolSpec
from .schema import build_args_schema, build_tool_description

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from .realm import McpRealm


def build_mcp_tools(realm: "McpRealm") -> list[Any]:
    """按当前已连接 server 的工具规格构造 LangChain 工具。"""
    from langchain_core.tools import StructuredTool

    tools: list[Any] = []
    for server_id, client in sorted(realm.clients.items()):
        for spec in realm.tool_specs.get(server_id, []):
            exposed = realm.exposed_name(server_id, spec.name)
            args_schema = build_args_schema(exposed, spec.input_schema)
            description = build_tool_description(spec.description, args_schema) or (
                f"MCP tool {spec.name} from {server_id}"
            )
            tool = _make_tool(realm, server_id, spec, exposed, description, args_schema)
            tools.append(tool)
    tools.sort(key=lambda item: item.name)
    return tools


def _make_tool(
    realm: "McpRealm",
    server_id: str,
    spec: McpToolSpec,
    exposed: str,
    description: str,
    args_schema: Any,
) -> Any:
    from langchain_core.tools import StructuredTool

    async def _call(**kwargs: Any) -> str:
        return await realm.call_tool(server_id, spec.name, kwargs)

    return StructuredTool.from_function(
        coroutine=_call,
        name=exposed,
        description=description,
        args_schema=args_schema,
    )


__all__ = ["build_mcp_tools"]
