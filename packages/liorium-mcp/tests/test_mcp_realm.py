"""``McpRealm``：运行时发现工具、单 server 降级、mark dirty 重连。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from liorium.agent.realm.base import JsonStateStore
from liorium.mcp import McpError, McpRealm, McpServerConfig, McpToolSpec


class FakeClient:
    """假 MCP 客户端：记录调用，可配置工具与失败。"""

    def __init__(self, config: McpServerConfig, *, tools=None, fail: str = "") -> None:
        self.config = config
        self.server_id = config.id
        self._tools = tools or []
        self._fail = fail
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> None:
        if self._fail:
            raise McpError(self._fail)
        self.connected = True

    async def list_tools(self) -> list[McpToolSpec]:
        return list(self._tools)

    async def call_tool(self, tool_name: str, arguments) -> str:
        self.calls.append((tool_name, dict(arguments)))
        return f"{self.server_id}:{tool_name} ok"

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeFactory:
    def __init__(self, tools: dict[str, list[McpToolSpec]] | None = None, fail: dict[str, str] | None = None) -> None:
        self.tools = tools or {}
        self.fail = fail or {}
        self.created: dict[str, FakeClient] = {}

    def __call__(self, config: McpServerConfig) -> FakeClient:
        client = FakeClient(
            config, tools=self.tools.get(config.id, []), fail=self.fail.get(config.id, "")
        )
        self.created[config.id] = client
        return client


class HangingClient(FakeClient):
    """连上但不回包：list_tools 永远不返回。"""

    async def list_tools(self) -> list[McpToolSpec]:
        await asyncio.sleep(5)
        return []


class FailingListClient(FakeClient):
    async def list_tools(self) -> list[McpToolSpec]:
        raise McpError("list 失败")


class BadCallClient(FakeClient):
    async def call_tool(self, tool_name: str, arguments) -> str:
        raise McpError("调用失败")


def make_ctx(tmp_path: Path):
    state = JsonStateStore(tmp_path / "realm_state" / "mcp.json", realm_id="mcp")
    state.load()
    return SimpleNamespace(state=state, inbox_dir=tmp_path, params={}, conversation_id="main", agent_id="test")


def make_realm(factory: FakeFactory, *, servers=None, strict: bool = False, prefix: bool = True) -> McpRealm:
    params = {
        "servers": servers
        or {
            "web": {"command": "python", "args": ["-m", "fake"], "description": "网页"},
        },
        "strict": strict,
        "prefix_tools": prefix,
    }
    return McpRealm(SimpleNamespace(params=params), client_factory=factory)


WEB_TOOLS = [
    McpToolSpec(
        name="open",
        description="打开网页",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "地址"}},
            "required": ["url"],
        },
    ),
    McpToolSpec(name="close", description="关闭浏览器"),
]


@pytest.mark.asyncio
async def test_start_discovers_prefixed_tools(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS})
    realm = make_realm(factory)
    await realm.start(make_ctx(tmp_path))
    try:
        assert realm.tool_names() == ["web_close", "web_open"]
        tools = {tool.name: tool for tool in realm.tools()}
        assert set(tools) == {"web_open", "web_close"}
        assert tools["web_open"].description.startswith("打开网页")
        assert "url: 地址" in tools["web_open"].description

        result = await tools["web_open"].ainvoke({"url": "https://example.com"})
        assert result == "web:open ok"
        assert factory.created["web"].calls == [("open", {"url": "https://example.com"})]

        status = realm.status()
        assert status[0]["connected"] is True
        assert status[0]["tools"] == ["open", "close"]
    finally:
        await realm.stop(make_ctx(tmp_path))
    assert factory.created["web"].closed is True


@pytest.mark.asyncio
async def test_prompt_fragment_lists_servers(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS})
    realm = make_realm(factory)
    assert realm.prompt_fragment() == ""
    await realm.start(make_ctx(tmp_path))
    try:
        fragment = realm.prompt_fragment()
        assert "web" in fragment and "open" in fragment
        assert "不要假装调用" in fragment
        assert realm.skills() == []
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_one_server_failure_degrades_but_keeps_others(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS}, fail={"bad": "连不上"})
    realm = make_realm(
        factory,
        servers={
            "web": {"command": "python"},
            "bad": {"command": "python"},
        },
    )
    await realm.start(make_ctx(tmp_path))
    try:
        assert realm.tool_names() == ["web_close", "web_open"]
        status = {item["id"]: item for item in realm.status()}
        assert status["bad"]["connected"] is False
        assert "连不上" in status["bad"]["error"]
        assert status["web"]["connected"] is True
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_all_servers_failing_is_strict_error(tmp_path: Path) -> None:
    factory = FakeFactory(fail={"bad": "连不上"})
    realm = make_realm(factory, servers={"bad": {"command": "python"}}, strict=True)
    with pytest.raises(McpError, match="所有 MCP server 都连接失败"):
        await realm.start(make_ctx(tmp_path))

    lenient = make_realm(factory, servers={"bad": {"command": "python"}}, strict=False)
    await lenient.start(make_ctx(tmp_path))
    try:
        assert lenient.tools() == []
        assert lenient.prompt_fragment() == ""
    finally:
        await lenient.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_mark_dirty_reconnects_new_servers(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS, "db": [McpToolSpec(name="query")]})
    realm = make_realm(factory, servers={"web": {"command": "python"}})
    await realm.start(make_ctx(tmp_path))
    try:
        assert realm.tool_names() == ["web_close", "web_open"]
        assert realm.dirty() is False

        realm.set_servers(
            {
                "web": {"command": "python"},
                "db": {"command": "python"},
            }
        )
        assert realm.dirty() is True

        await realm.refresh()
        assert realm.dirty() is False
        assert realm.tool_names() == ["db_query", "web_close", "web_open"]
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_removed_server_is_disconnected(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS, "db": [McpToolSpec(name="query")]})
    realm = make_realm(
        factory, servers={"web": {"command": "python"}, "db": {"command": "python"}}
    )
    await realm.start(make_ctx(tmp_path))
    try:
        realm.set_servers({"web": {"command": "python"}})
        await realm.refresh()
        assert factory.created["db"].closed is True
        assert realm.tool_names() == ["web_close", "web_open"]
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_disabled_server_is_skipped(tmp_path: Path) -> None:
    factory = FakeFactory({"web": WEB_TOOLS})
    realm = make_realm(
        factory,
        servers={"web": {"command": "python", "enabled": False}},
    )
    await realm.start(make_ctx(tmp_path))
    try:
        assert realm.tools() == []
        assert realm.enabled_servers == []
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_realm_assembles_through_manager(tmp_path: Path) -> None:
    """经 ``RealmManager`` 装配必须能起来（它无条件调 ``mounts()``）。"""
    from liorium.agent.realm.manager import RealmManager
    from liorium.agent.spec import RealmSpec
    from liorium.vfs import VirtualFileSystem

    realm = McpRealm(client_factory=FakeFactory())
    manager = RealmManager(
        agent_id="mcp-test",
        vfs=VirtualFileSystem(owner_id="mcp-test"),
        state_dir=tmp_path / "state",
        factories={"mcp": lambda spec: realm},
    )
    manager.assemble([RealmSpec(id="mcp", params={"servers": []})])
    await manager.start_all()
    try:
        assert manager._records["mcp"].state == "running"  # noqa: SLF001
        assert realm.mounts() == {}
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_hanging_list_tools_times_out_and_closes(tmp_path: Path) -> None:
    clients: list[HangingClient] = []

    def factory(config: McpServerConfig) -> HangingClient:
        client = HangingClient(config)
        clients.append(client)
        return client

    realm = McpRealm(
        SimpleNamespace(
            params={
                "servers": {
                    "slow": {
                        "command": "python",
                        "connect_timeout": 0.05,
                        "call_timeout": 0.05,
                    }
                }
            }
        ),
        client_factory=factory,
    )
    await asyncio.wait_for(realm.start(make_ctx(tmp_path)), timeout=2.0)
    try:
        assert clients and clients[0].closed is True
        status = realm.status()[0]
        assert status["connected"] is False
        assert "TimeoutError" in status["error"] or "超时" in status["error"]
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_list_tools_failure_closes_client(tmp_path: Path) -> None:
    clients: list[FailingListClient] = []

    def factory(config: McpServerConfig) -> FailingListClient:
        client = FailingListClient(config)
        clients.append(client)
        return client

    realm = McpRealm(
        SimpleNamespace(params={"servers": {"web": {"command": "python"}}}),
        client_factory=factory,
    )
    await realm.start(make_ctx(tmp_path))
    try:
        assert clients and clients[0].closed is True
        assert realm.status()[0]["connected"] is False
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_call_failure_marks_stale_and_reconnects_after_backoff(
    tmp_path: Path,
) -> None:
    factory = FakeFactory({"web": WEB_TOOLS})

    def make_bad(config: McpServerConfig) -> BadCallClient:
        client = BadCallClient(config, tools=factory.tools.get(config.id, []))
        factory.created[config.id] = client
        return client

    realm = McpRealm(
        SimpleNamespace(
            params={
                "servers": {"web": {"command": "python"}},
                "retry_initial": 0.01,
                "retry_max": 0.02,
            }
        ),
        client_factory=make_bad,
    )
    await realm.start(make_ctx(tmp_path))
    try:
        old = factory.created["web"]
        with pytest.raises(McpError):
            await realm.call_tool("web", "open", {"url": "x"})
        assert realm.dirty() is False, "退避期内不应每回合重连"

        await asyncio.sleep(0.03)
        assert realm.dirty() is True
        await realm.refresh()
        assert old.closed is True
        assert factory.created["web"] is not old
    finally:
        await realm.stop(make_ctx(tmp_path))


def test_server_config_validation() -> None:
    with pytest.raises(ValueError, match="没有 command"):
        McpServerConfig(id="x")
    with pytest.raises(ValueError, match="没有 url"):
        McpServerConfig(id="x", transport="sse")
    with pytest.raises(ValueError, match="不支持的 MCP transport"):
        McpServerConfig(id="x", transport="carrier-pigeon")

    config = McpServerConfig.from_dict(
        "remote", {"transport": "http", "url": "https://mcp.example.com", "headers": {"a": 1}}
    )
    assert config.transport == "streamable-http"
    assert config.headers == {"a": "1"}
    assert config.to_dict()["id"] == "remote"
