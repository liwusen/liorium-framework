"""v1.1 机制：realm dirty 刷新、技能清单进 prompt、conversation scope。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from liorium.agent import AgentSpec, BaseRealm, Limits, RealmSpec, LioriumAgent
from liorium_testkit import make_scripted_factory


class DirtyRealm(BaseRealm):
    """工具集可变的 realm：``mark_dirty()`` 后多出一个工具。"""

    id = "dirty"

    def __init__(self, spec: Any = None) -> None:
        self._extra = False
        self._dirty = False
        self.prompt = "脏 realm 的用法说明。"

    def tools(self):
        from langchain_core.tools import tool

        @tool("alpha")
        async def alpha(text: str) -> str:
            """第一个工具。"""
            return text

        @tool("beta")
        async def beta(text: str) -> str:
            """第二个工具（只有刷新后才出现）。"""
            return text

        return [alpha, beta] if self._extra else [alpha]

    def prompt_fragment(self) -> str:
        return self.prompt

    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self, flag: bool = True) -> None:
        self._extra = bool(flag) or self._extra
        self._dirty = bool(flag)

    def clear_dirty(self) -> None:
        self._dirty = False


class SkillRealm(BaseRealm):
    id = "skill-ish"

    def skills(self) -> list[str]:
        return ["demo：演示技能（`skills://demo/SKILL.md`）"]

    def prompt_fragment(self) -> str:
        return "技能库说明。"


class ConversationRealm(BaseRealm):
    id = "conv"
    state_scope = "conversation"

    def __init__(self, spec: Any = None) -> None:
        self.boots = 0

    async def start(self, ctx: Any) -> None:
        previous = int(await ctx.state.get("boots", 0) or 0)
        self.boots = previous + 1
        await ctx.state.set("boots", self.boots)
        await ctx.state.set("conversation", ctx.conversation_id)


class AgentScopeRealm(ConversationRealm):
    id = "agent-scope"
    state_scope = "agent"


def make_spec(workspace: Path, realms: list[RealmSpec]) -> AgentSpec:
    return AgentSpec(
        id="v11",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=tuple(realms),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=workspace,
    )


@pytest.mark.asyncio
async def test_dirty_realm_refresh_replaces_tools(agent_workspace: Path) -> None:
    factory = make_scripted_factory(["好", "好"])
    realm = DirtyRealm()
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="dirty")]),
        model_factory=factory,
        realm_factories={"dirty": lambda _spec: realm},
    )
    await agent.start()
    try:
        before_epoch = agent.epoch.value
        assert [tool.name for tool in agent.tools if tool.name in {"alpha", "beta"}] == ["alpha"]

        realm.mark_dirty()
        events = [event async for event in agent.run("一")]
        assert events[-1].type == "done"
        assert [tool.name for tool in agent.tools if tool.name in {"alpha", "beta"}] == [
            "alpha",
            "beta",
        ]
        assert agent.epoch.value == before_epoch + 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_dirty_refresh_keeps_epoch_when_schema_unchanged(agent_workspace: Path) -> None:
    factory = make_scripted_factory(["好", "好"])
    realm = SkillRealm()
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="skill-ish")]),
        model_factory=factory,
        realm_factories={"skill-ish": lambda _spec: realm},
    )
    await agent.start()
    try:
        epoch = agent.epoch.value
        [event async for event in agent.run("一")]
        assert agent.epoch.value == epoch
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_skill_listing_goes_into_prompt(agent_workspace: Path) -> None:
    factory = make_scripted_factory(["好"])
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="skill-ish")]),
        model_factory=factory,
        realm_factories={"skill-ish": lambda _spec: SkillRealm()},
    )
    await agent.start()
    try:
        prompt = agent._prompt.text  # noqa: SLF001 - 断言 system prompt 内容
        assert "## 技能清单" in prompt
        assert "demo：演示技能" in prompt
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_conversation_scope_state_resets_on_reset_dialog(agent_workspace: Path) -> None:
    factory = make_scripted_factory(["好"])
    conv = ConversationRealm()
    agent_scope = AgentScopeRealm()
    agent = LioriumAgent(
        spec=make_spec(
            agent_workspace, [RealmSpec(id="conv"), RealmSpec(id="agent-scope")]
        ),
        model_factory=factory,
        realm_factories={
            "conv": lambda _spec: conv,
            "agent-scope": lambda _spec: agent_scope,
        },
    )
    await agent.start()
    try:
        conv_state = agent_workspace / "realm_state" / "conv" / "conversation-main.json"
        assert conv_state.is_file()
        assert conv.boots == 1

        await agent.reset_dialog()

        # conversation scope：状态文件被重写（boots 归 1），realm 重新 start
        assert conv.boots == 1
        import json

        assert json.loads(conv_state.read_text(encoding="utf-8"))["boots"] == 1
        # agent scope：状态保留（boots 累加到 2）
        agent_state = agent_workspace / "realm_state" / "agent-scope.json"
        assert agent_state.is_file()
        assert json.loads(agent_state.read_text(encoding="utf-8"))["boots"] == 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_with_mounted_realms_remounts_without_conflict(agent_workspace: Path) -> None:
    """改 realm 列表时先停旧 realm：否则新 realm 挂同一前缀会 MountConflictError。"""
    from dataclasses import replace

    from langchain_core.tools import tool

    from liorium.vfs import VirtualFileSystem, VfsMount

    class MountRealm(BaseRealm):
        instance_scope = "tree"

        def __init__(self, spec: Any) -> None:
            self.id = str(spec.id)
            self.prefix = f"/{self.id}"
            self._vfs = VirtualFileSystem(owner_id=self.id)

            @tool(f"tool_{self.id}")
            async def ping(text: str) -> str:
                """测试工具。"""
                return text

            self._tool = ping

        def tools(self):
            return [self._tool]

        def mounts(self):
            return {self.prefix: VfsMount(prefix=self.prefix, vfs=self._vfs, readonly=True)}

        def prompt_fragment(self) -> str:
            return f"{self.id} 的说明"

    factory = make_scripted_factory(["好"])
    spec = make_spec(agent_workspace, [RealmSpec(id="alpha")])
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={"alpha": MountRealm, "beta": MountRealm},
    )
    await agent.start()
    try:
        assert set(agent.vfs.mounts) == {"/alpha", "/artifacts"}
        epoch_before = agent.epoch.value

        result = await agent.apply(replace(spec, realms=(RealmSpec(id="alpha"), RealmSpec(id="beta"))))
        assert result.failed == ()
        assert {status.id: status.state for status in agent.status().realms} == {
            "alpha": "running",
            "beta": "running",
        }
        assert set(agent.vfs.mounts) == {"/alpha", "/beta", "/artifacts"}
        assert {"tool_alpha", "tool_beta"} <= {tool.name for tool in agent.tools}
        assert agent.epoch.value > epoch_before
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_mcp_like_refresh_hook_is_awaited(agent_workspace: Path) -> None:
    """realm 的 ``async refresh()``（MCP 重连）先于取工具执行。"""
    calls: list[str] = []

    class HookRealm(DirtyRealm):
        id = "hook"

        async def refresh(self) -> None:
            calls.append("refresh")
            self._extra = True

    realm = HookRealm()
    factory = make_scripted_factory(["好"])
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="hook")]),
        model_factory=factory,
        realm_factories={"hook": lambda _spec: realm},
    )
    await agent.start()
    try:
        realm.mark_dirty()
        [event async for event in agent.run("一")]
        assert calls == ["refresh"]
        assert "beta" in [tool.name for tool in agent.tools]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_refresh_realms_force_rebuilds_without_a_turn(agent_workspace: Path) -> None:
    """宿主主动刷新：realm 自己没报脏，``force=True`` 也能立刻生效。"""
    realm = DirtyRealm()
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="dirty")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"dirty": lambda _spec: realm},
    )
    await agent.start()
    try:
        before = agent.epoch.value
        assert "beta" not in [tool.name for tool in agent.tools]
        assert realm.dirty() is False

        assert await agent.refresh_realms() is True
        assert "beta" in [tool.name for tool in agent.tools]
        assert agent.epoch.value == before + 1

        # 已经清过账、force=False 时不再刷新（不会每个回合无脑重建）
        assert await agent.refresh_realms(force=False) is False
        assert agent.epoch.value == before + 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_refresh_realms_requires_start(agent_workspace: Path) -> None:
    from liorium.core.errors import TurnError

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="dirty")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"dirty": DirtyRealm},
    )
    with pytest.raises(TurnError):
        await agent.refresh_realms()


@pytest.mark.asyncio
async def test_mark_all_dirty_skips_stopped_realms(agent_workspace: Path) -> None:
    good = DirtyRealm()
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="dirty"), RealmSpec(id="plain")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={
            "dirty": lambda _spec: good,
            "plain": lambda _spec: BaseRealm(),
        },
    )
    await agent.start()
    try:
        marked = agent.realms.mark_all_dirty()
        # 两个都在跑；``BaseRealm.mark_dirty()`` 是无参空钩子，也要能调通
        assert sorted(marked) == ["dirty", "plain"]
        assert good.dirty() is True
        await agent.realms.stop("plain")
        assert agent.realms.mark_all_dirty() == ["dirty"]
    finally:
        await agent.close()
