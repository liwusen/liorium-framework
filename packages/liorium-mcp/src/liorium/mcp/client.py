"""MCP 客户端：配置、协议、基于官方 SDK 的实现。

设计要点（计划 §B2 / §B6 / §Phase 2）：

- **MCP 工具不进 ``AgentSpec``**：它们是运行时发现的，spec 只写"要连哪些 server"；
- 客户端是**可替换的端口**（``McpClient`` 协议）：宿主可以注入自己的传输/审计实现，
  测试也不需要真的起子进程；
- SDK 是**延迟导入**的：没装 ``mcp`` 时导入本包仍然安全，真正连接才报错。

传输支持 ``stdio`` / ``sse`` / ``streamable-http``（与常见 MCP 配置一致）。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

TRANSPORTS = ("stdio", "sse", "streamable-http")
_TRANSPORT_ALIASES = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "streamable_http": "streamable-http",
}


class McpError(RuntimeError):
    """MCP 连接 / 调用失败。"""


@dataclass(frozen=True)
class McpServerConfig:
    """一个 MCP server 的连接配置（可序列化，进 ``RealmSpec.params``）。"""

    id: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    connect_timeout: float = 10.0
    """连接 + 握手超时（秒）；防止"连上但不回包"的 server 永久挂起。"""
    call_timeout: float = 60.0
    """``list_tools`` / ``call_tool`` 超时（秒）。"""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("McpServerConfig.id 不能为空")
        if float(self.connect_timeout) <= 0:
            raise ValueError("McpServerConfig.connect_timeout 必须 > 0")
        if float(self.call_timeout) <= 0:
            raise ValueError("McpServerConfig.call_timeout 必须 > 0")
        transport = _TRANSPORT_ALIASES.get(str(self.transport or "stdio").strip().lower())
        if transport is None:
            raise ValueError(
                f"不支持的 MCP transport: {self.transport!r}（允许 {TRANSPORTS}）"
            )
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "args", tuple(str(a) for a in self.args))
        object.__setattr__(self, "headers", {str(k): str(v) for k, v in self.headers.items()})
        object.__setattr__(self, "env", {str(k): str(v) for k, v in self.env.items()})
        if transport == "stdio" and not str(self.command or "").strip():
            raise ValueError(f"MCP server {self.id!r} 用 stdio 但没有 command")
        if transport != "stdio" and not str(self.url or "").strip():
            raise ValueError(f"MCP server {self.id!r} 用 {transport} 但没有 url")

    @classmethod
    def from_dict(cls, server_id: str, raw: Mapping[str, Any]) -> "McpServerConfig":
        if not isinstance(raw, Mapping):
            raise TypeError(f"MCP server {server_id!r} 的配置必须是对象")
        return cls(
            id=str(server_id),
            transport=str(raw.get("transport") or "stdio"),
            command=str(raw.get("command") or ""),
            args=tuple(raw.get("args") or ()),
            url=str(raw.get("url") or ""),
            headers=dict(raw.get("headers") or {}),
            env=dict(raw.get("env") or {}),
            enabled=bool(raw.get("enabled", True)),
            description=str(raw.get("description") or ""),
            connect_timeout=float(raw.get("connect_timeout") or 10.0),
            call_timeout=float(raw.get("call_timeout") or 60.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "headers": dict(self.headers),
            "env": dict(self.env),
            "enabled": self.enabled,
            "description": self.description,
            "connect_timeout": self.connect_timeout,
            "call_timeout": self.call_timeout,
        }


@dataclass(frozen=True)
class McpToolSpec:
    """MCP server 暴露的一个工具（运行时发现，不进 spec）。"""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


@runtime_checkable
class McpClient(Protocol):
    """传输无关的 MCP 客户端端口。"""

    server_id: str

    async def connect(self) -> None: ...

    async def list_tools(self) -> list[McpToolSpec]: ...

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> str: ...

    async def close(self) -> None: ...


class SdkMcpClient:
    """基于官方 ``mcp`` Python SDK 的客户端（stdio / sse / streamable-http）。"""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.server_id = config.id
        self._session: Any = None
        self._transport_ctx: Any = None
        self._connected = False

    # -- 生命周期 -------------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        try:
            await asyncio.wait_for(
                self._connect(), timeout=float(self.config.connect_timeout)
            )
        except asyncio.TimeoutError as exc:
            await self.close()
            raise McpError(
                f"MCP server {self.server_id} 连接超时"
                f"（{self.config.connect_timeout}s）"
            ) from exc
        except McpError:
            raise

    async def _connect(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise McpError(
                "未安装可用的 MCP SDK（pip install 'mcp>=1.28,<2'）: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            if self.config.transport == "stdio":
                env = dict(os.environ)
                env.update(self.config.env)
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=env,
                )
                self._transport_ctx = stdio_client(params)
                read_stream, write_stream = await self._transport_ctx.__aenter__()
            elif self.config.transport == "sse":
                self._transport_ctx = sse_client(
                    self.config.url, headers=dict(self.config.headers) or None
                )
                read_stream, write_stream = await self._transport_ctx.__aenter__()
            else:
                self._transport_ctx = streamablehttp_client(
                    self.config.url, headers=dict(self.config.headers) or None
                )
                opened = await self._transport_ctx.__aenter__()
                read_stream, write_stream = opened[0], opened[1]

            self._session = ClientSession(read_stream, write_stream)
            await self._session.__aenter__()
            await self._session.initialize()
            self._connected = True
        except Exception as exc:  # noqa: BLE001 - 统一包成 McpError，带上 server id
            await self.close()
            raise McpError(f"MCP server {self.server_id} 连接失败: {exc}") from exc

    async def close(self) -> None:
        session, transport = self._session, self._transport_ctx
        self._session = None
        self._transport_ctx = None
        self._connected = False
        if session is not None:
            try:
                await asyncio.wait_for(session.__aexit__(None, None, None), timeout=5.0)
            except Exception:  # noqa: BLE001 - 关闭失败不影响后续清理
                pass
        if transport is not None:
            try:
                await asyncio.wait_for(transport.__aexit__(None, None, None), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> "SdkMcpClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- 能力 -----------------------------------------------------------

    async def list_tools(self) -> list[McpToolSpec]:
        if self._session is None:
            raise McpError(f"MCP server {self.server_id} 未连接")
        try:
            result = await asyncio.wait_for(
                self._session.list_tools(), timeout=float(self.config.call_timeout)
            )
        except asyncio.TimeoutError as exc:
            raise McpError(
                f"MCP server {self.server_id} list_tools 超时"
                f"（{self.config.call_timeout}s）"
            ) from exc
        specs: list[McpToolSpec] = []
        for item in list(getattr(result, "tools", []) or []):
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            specs.append(
                McpToolSpec(
                    name=name,
                    description=str(getattr(item, "description", "") or "").strip(),
                    input_schema=dict(getattr(item, "inputSchema", None) or {}),
                )
            )
        return specs

    async def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> str:
        if self._session is None:
            raise McpError(f"MCP server {self.server_id} 未连接")
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(
                    tool_name, arguments=_clean_arguments(arguments)
                ),
                timeout=float(self.config.call_timeout),
            )
        except asyncio.TimeoutError as exc:
            raise McpError(
                f"MCP server {self.server_id} 调用 {tool_name} 超时"
                f"（{self.config.call_timeout}s）"
            ) from exc
        return _result_to_text(result)


def _clean_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """去掉值为 ``None`` 的参数：很多 MCP server 把 null 当成"传了这个字段"。"""
    cleaned: dict[str, Any] = {}
    for key, value in dict(arguments or {}).items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            cleaned[str(key)] = _clean_arguments(value)
        elif isinstance(value, list):
            cleaned[str(key)] = [
                _clean_arguments(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            cleaned[str(key)] = value
    return cleaned


def _result_to_text(result: Any) -> str:
    """把 MCP ``CallToolResult`` 压成文本（图片只留说明，不内联 base64）。"""
    parts: list[str] = []
    for item in list(getattr(result, "content", None) or []):
        kind = str(getattr(item, "type", "") or "")
        if kind == "text":
            parts.append(str(getattr(item, "text", "")))
        elif kind == "image":
            mime = str(getattr(item, "mimeType", "") or "image/*")
            data = getattr(item, "data", None)
            size = len(data) if isinstance(data, (bytes, str)) else 0
            parts.append(f"[MCP 图片: {mime}，{size} bytes]")
        elif kind in ("resource", "resource_link"):
            resource = getattr(item, "resource", None)
            uri = getattr(resource, "uri", None) or getattr(item, "uri", "")
            text = getattr(resource, "text", None)
            parts.append(f"[MCP 资源: {uri}]" + (f"\n{text}" if text else ""))
        else:
            parts.append(str(item))
    text = "\n".join(part for part in parts if part)
    if getattr(result, "isError", False):
        return f"MCP 工具错误: {text or '(无输出)'}"
    return text or "(MCP 工具没有返回内容)"


__all__ = [
    "TRANSPORTS",
    "McpClient",
    "McpError",
    "McpServerConfig",
    "McpToolSpec",
    "SdkMcpClient",
]
