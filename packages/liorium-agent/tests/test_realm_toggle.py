"""运行期开关 realm：``LioriumAgent.set_realm_enabled()``。

开关只动**已装配**的一个 realm（停 / 起，装配集合不变），然后重建运行时 ——
工具、挂载、prompt 段随状态一起出现或消失，``CacheEpoch`` 相应 +1。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from liorium.agent import AgentSpec, BaseRealm, Limits, RealmSpec, LioriumAgent
from liorium.core.errors import RealmNotFoundError
from liorium_testkit import make_scripted_factory


class ToggleRealm(BaseRealm):
    """带工具 / prompt 段 / 启动计数的可开关 realm。"""

    id = "toggle"

    def __init__(self) -> None:
        self.boots = 0
        self.fail = False

    def tools(self) -> list[Any]:
        @tool("toggle_ping")
        async def toggle_ping(text: str) -> str:
            """回显。"""
            return text

        return [toggle_ping]

    def prompt_fragment(self) -> str:
        return "## 开关 realm\n\n这段只在运行中进 prompt。"

    async def start(self, ctx: Any) -> None:
        if self.fail:
            raise RuntimeError("起不来")
        self.boots += 1
        await super().start(ctx)


def make_spec(workspace: Path) -> AgentSpec:
    return AgentSpec(
        id="toggle-agent",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="toggle"),),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=workspace,
    )


def make_agent(workspace: Path, realm: ToggleRealm) -> LioriumAgent:
    return LioriumAgent(
        spec=make_spec(workspace),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"toggle": lambda _spec: realm},
    )


def tool_names(agent: LioriumAgent) -> set[str]:
    return {tool.name for tool in agent.tools}


@pytest.mark.asyncio
async def test_disable_then_enable_moves_tools_and_prompt(agent_workspace: Path) -> None:
    realm = ToggleRealm()
    agent = make_agent(agent_workspace, realm)
    await agent.start()
    try:
        assert "toggle_ping" in tool_names(agent)
        assert "开关 realm" in agent._prompt.text  # noqa: SLF001
        epoch = agent.epoch.value

        assert await agent.set_realm_enabled("toggle", False) is False
        assert "toggle_ping" not in tool_names(agent)
        assert "开关 realm" not in agent._prompt.text  # noqa: SLF001
        assert agent.realms.record("toggle").state == "stopped"
        assert agent.epoch.value == epoch + 1
        assert realm.boots == 1  # 停用不重启实例

        assert await agent.set_realm_enabled("toggle", True) is True
        assert "toggle_ping" in tool_names(agent)
        assert "开关 realm" in agent._prompt.text  # noqa: SLF001
        assert agent.realms.record("toggle").state == "running"
        assert realm.boots == 2  # 复用同一个实例重新 start
        assert agent.epoch.value == epoch + 2
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_toggle_is_idempotent(agent_workspace: Path) -> None:
    agent = make_agent(agent_workspace, ToggleRealm())
    await agent.start()
    try:
        epoch = agent.epoch.value
        assert await agent.set_realm_enabled("toggle", True) is True  # 已在运行
        assert agent.epoch.value == epoch  # 没变就不重建运行时

        assert await agent.set_realm_enabled("toggle", False) is False
        assert agent.epoch.value == epoch + 1
        # 已经关着：不再 stop / 不再重建
        assert await agent.set_realm_enabled("toggle", False) is False
        assert agent.epoch.value == epoch + 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_unknown_realm_raises(agent_workspace: Path) -> None:
    agent = make_agent(agent_workspace, ToggleRealm())
    await agent.start()
    try:
        with pytest.raises(RealmNotFoundError):
            await agent.set_realm_enabled("nope", True)
        # 装配集合由 spec 决定，运行期不会多出 realm
        assert agent.realms.order == ["toggle"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_enable_failure_degrades_without_raising(agent_workspace: Path) -> None:
    realm = ToggleRealm()
    agent = make_agent(agent_workspace, realm)
    await agent.start()
    try:
        assert await agent.set_realm_enabled("toggle", False) is False
        realm.fail = True
        assert await agent.set_realm_enabled("toggle", True) is False  # 起不来但不抛
        record = agent.realms.record("toggle")
        assert record.state == "failed"
        assert "起不来" in (record.error or "")
        assert "toggle_ping" not in tool_names(agent)
        assert "开关 realm" not in agent._prompt.text  # noqa: SLF001
    finally:
        await agent.close()
