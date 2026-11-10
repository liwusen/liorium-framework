"""``SubagentRealm``：子代理是同类的 Agent，共享父的 vfs，事件不打断父回合。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent
from liorium_testkit import make_scripted_factory


def make_parent(workspace: Path, responses: list) -> tuple[LioriumAgent, object]:
    factory = make_scripted_factory(responses)
    spec = AgentSpec(
        id="parent",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="subagents"),),
        limits=Limits(context_window=8000, max_turns=6),
        workspace_root=workspace,
    )
    agent = LioriumAgent(spec=spec, model_factory=factory)
    return agent, factory.model


def tools_by_name(agent: LioriumAgent) -> dict[str, object]:
    realm = agent._subagent_realm
    assert realm is not None
    return {t.name: t for t in realm.tools()}


@pytest.mark.asyncio
async def test_create_invoke_wait_and_read_result(agent_workspace: Path) -> None:
    agent, _model = make_parent(agent_workspace, ["子代理的结论：42"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        created = json.loads(
            await tools["newSubagent"].ainvoke(
                {"name": "research-1", "prompt": "你负责调研，把结论写清楚。", "toolsets": "read,find"}
            )
        )
        assert created["ok"] is True
        assert created["agent_id"] == "subagent-research-1"

        started = json.loads(
            await tools["invokeSubagent"].ainvoke({"name": "research-1", "message": "查一下 6x7"})
        )
        assert started["ok"] is True

        waited = json.loads(
            await tools["wait_for_subagent"].ainvoke({"name": "research-1", "timeout": 30})
        )
        assert waited["ok"] is True
        assert waited["status"] == "done"
        assert "42" in waited["final_result"]

        # VFS 投影：父 Agent 能通过 subagents:// 读到结果
        result = await agent.vfs.read_text("/subagents/research-1/finalResult.md")
        assert "42" in result
        overview = await agent.vfs.read_text("/subagents/research-1/overview.md")
        assert "research-1" in overview
        task = await agent.vfs.read_text("/subagents/research-1/TASK.md")
        assert "调研" in task
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_duplicate_and_invalid_names(agent_workspace: Path) -> None:
    agent, _model = make_parent(agent_workspace, ["x"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        ok = json.loads(await tools["newSubagent"].ainvoke({"name": "a", "prompt": "p"}))
        assert ok["ok"] is True
        dup = json.loads(await tools["newSubagent"].ainvoke({"name": "a", "prompt": "p"}))
        assert dup["ok"] is False and "已存在" in dup["error"]
        bad = json.loads(await tools["newSubagent"].ainvoke({"name": "bad name!", "prompt": "p"}))
        assert bad["ok"] is False
        missing = json.loads(await tools["invokeSubagent"].ainvoke({"name": "nope", "message": "m"}))
        assert missing["ok"] is False
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_stop_and_remove(agent_workspace: Path) -> None:
    agent, _model = make_parent(agent_workspace, ["x"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        await tools["newSubagent"].ainvoke({"name": "worker", "prompt": "p"})
        await tools["invokeSubagent"].ainvoke({"name": "worker", "message": "m"})
        stopped = json.loads(await tools["stopSubagent"].ainvoke({"name": "worker"}))
        assert stopped["ok"] is True
        removed = json.loads(await tools["removeSubagent"].ainvoke({"name": "worker"}))
        assert removed["ok"] is True
        assert agent._subagent_realm.children == {}
        again = json.loads(await tools["removeSubagent"].ainvoke({"name": "worker"}))
        assert again["ok"] is False
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_events_stream(agent_workspace: Path) -> None:
    agent, _model = make_parent(agent_workspace, ["事件结论"])
    await agent.start()
    try:
        realm = agent._subagent_realm
        tools = tools_by_name(agent)
        queue = realm.subscribe()
        await tools["newSubagent"].ainvoke({"name": "e1", "prompt": "p"})
        await tools["invokeSubagent"].ainvoke({"name": "e1", "message": "m"})
        await tools["wait_for_subagent"].ainvoke({"name": "e1", "timeout": 30})

        seen: list[dict] = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        realm.unsubscribe(queue)
        types = [e["type"] for e in seen]
        assert "queued" in types
        assert any(e.get("agent_id") == "subagent-e1" for e in seen)
        assert "finalResult" in types
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_subagent_cannot_exceed_parent_depth(agent_workspace: Path) -> None:
    """子代理默认工具集不含 realm 工具，因此不会再 spawn（v1 保守）。"""
    agent, _model = make_parent(agent_workspace, ["x"])
    await agent.start()
    try:
        realm = agent._subagent_realm
        assert realm._max_depth >= 1
        tools = tools_by_name(agent)
        await tools["newSubagent"].ainvoke({"name": "c1", "prompt": "p"})
        record = realm.children["c1"]
        assert "read" in record.toolsets
        assert "newSubagent" not in record.toolsets
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_parent_unaffected_by_child_error(agent_workspace: Path) -> None:
    """子代理失败不拖垮父：父回合仍能正常完成。"""

    class BoomModel:
        pass

    agent, model = make_parent(agent_workspace, [AIMessage(content="父回答")])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        await tools["newSubagent"].ainvoke({"name": "bad", "prompt": "p"})
        # 让子代理指向一个会抛错的模型：直接摘掉 model_factory 的实例
        record = agent._subagent_realm.children["bad"]
        record.model = "boom::x"

        def factory(_label: str):
            raise RuntimeError("模型不可用")

        agent.model_factory = factory
        await tools["invokeSubagent"].ainvoke({"name": "bad", "message": "m"})
        waited = json.loads(
            await tools["wait_for_subagent"].ainvoke({"name": "bad", "timeout": 10})
        )
        assert waited["ok"] is False
        assert record.status == "error"

        agent.model_factory = lambda _label: model
        events = [e async for e in agent.run("还在吗")]
        assert events[-1].type == "done"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_subagent_count_has_no_hard_cap(agent_workspace: Path) -> None:
    """不再把 8 写死：N 由调用方决定（默认不限制）。"""
    parent, _model = make_parent(agent_workspace, ["x"])
    await parent.start()
    try:
        tools = tools_by_name(parent)
        for index in range(12):
            created = json.loads(
                await tools["newSubagent"].ainvoke(
                    {"name": f"c{index}", "prompt": "p"}
                )
            )
            assert created["ok"] is True, created
        assert len(parent._subagent_realm.children) == 12
    finally:
        await parent.close()


@pytest.mark.asyncio
async def test_wait_for_subagents_all(agent_workspace: Path) -> None:
    parent, _model = make_parent(agent_workspace, ["并行结论"])
    await parent.start()
    try:
        tools = tools_by_name(parent)
        for name in ("a", "b"):
            await tools["newSubagent"].ainvoke({"name": name, "prompt": "p"})
            started = json.loads(
                await tools["invokeSubagent"].ainvoke({"name": name, "message": "m"})
            )
            assert started["ok"] is True

        waited = json.loads(
            await tools["wait_for_subagents"].ainvoke({"names": "a,b", "timeout": 30})
        )
        assert waited["ok"] is True
        assert {item["name"] for item in waited["subagents"]} == {"a", "b"}
        assert waited["missing"] == []
    finally:
        await parent.close()


@pytest.mark.asyncio
async def test_update_toolsets_without_deleting_the_child(agent_workspace: Path) -> None:
    """2026-11-08：改工具集不该付"删库重建"的代价。"""
    agent, _model = make_parent(agent_workspace, ["ok"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        await tools["newSubagent"].ainvoke(
            {"name": "w", "prompt": "p", "toolsets": "read"}
        )
        updated = json.loads(
            await tools["updateSubagent"].ainvoke({"name": "w", "toolsets": "read,write"})
        )
        assert updated["ok"] is True
        assert updated["changed"] is True
        assert updated["previous"] == ["read"]
        assert updated["toolsets"] == ["read", "write"]

        realm = agent._subagent_realm
        assert realm is not None
        assert realm.children["w"].toolsets == ("read", "write")
        # 目录还在：没有删库重建
        assert (realm.children["w"].inbox).exists()

        again = json.loads(
            await tools["updateSubagent"].ainvoke({"name": "w", "toolsets": "read,write"})
        )
        assert again["changed"] is False

        missing = json.loads(
            await tools["updateSubagent"].ainvoke({"name": "nope", "toolsets": "read"})
        )
        assert missing["ok"] is False
        empty = json.loads(
            await tools["updateSubagent"].ainvoke({"name": "w", "toolsets": "  "})
        )
        assert empty["ok"] is False
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_unknown_toolset_name_is_reported_not_raised(agent_workspace: Path) -> None:
    """父 Agent 没有的工具只在返回里提示，不拦创建（父可能没装 core 工具）。"""
    agent, _model = make_parent(agent_workspace, ["ok"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        created = json.loads(
            await tools["newSubagent"].ainvoke(
                {"name": "w", "prompt": "p", "toolsets": "read,execute"}
            )
        )
        assert created["ok"] is True
        assert created["toolsets"] == ["read", "execute"]
        assert "父 Agent 没有这些工具" in created["warning"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_child_completion_is_pushed_as_context(agent_workspace: Path) -> None:
    """子代理跑完不靠模型轮询：终态排一条 context，下一回合自动送达。"""
    from liorium.realm_base import TurnInfo

    agent, _model = make_parent(agent_workspace, ["子代理结论"])
    await agent.start()
    try:
        tools = tools_by_name(agent)
        await tools["newSubagent"].ainvoke({"name": "r1", "prompt": "p"})
        await tools["invokeSubagent"].ainvoke({"name": "r1", "message": "m"})
        await tools["wait_for_subagent"].ainvoke({"name": "r1", "timeout": 30})

        realm = agent._subagent_realm
        assert realm is not None
        items = realm.context_items(TurnInfo(index=2, id="parent#2"))
        assert [item.source for item in items] == ["subagent:r1"]
        assert "已完成" in items[0].text
        assert "subagents://r1/finalResult.md" in items[0].text
        # 取走即清空：不会每回合重复提醒
        assert realm.context_items(TurnInfo(index=3, id="parent#3")) == []
    finally:
        await agent.close()
