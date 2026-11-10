"""``SubagentRealm`` 状态持久化：名册跨进程、正文落盘、重启后接着对话。

设计见 ``docs/design/subagent-state.md``：结构化元数据进
``inbox/realm_state/subagents.json``，任务/转录/结果进
``inbox/subagents/<name>/{TASK,output,finalResult}.md``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent
from liorium_testkit import make_scripted_factory


def make_parent(
    workspace: Path,
    responses: list,
    *,
    params: dict | None = None,
) -> LioriumAgent:
    spec = AgentSpec(
        id="parent",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="subagents", params=dict(params or {})),),
        limits=Limits(context_window=8000, max_turns=6),
        workspace_root=workspace,
    )
    return LioriumAgent(spec=spec, model_factory=make_scripted_factory(responses))


def tools_of(agent: LioriumAgent) -> dict[str, object]:
    realm = agent._subagent_realm
    assert realm is not None
    return {t.name: t for t in realm.tools()}


def registry_path(workspace: Path) -> Path:
    return workspace / "realm_state" / "subagents.json"


def read_registry(workspace: Path) -> dict:
    return json.loads(registry_path(workspace).read_text(encoding="utf-8"))


def realm_status(agent: LioriumAgent, realm_id: str = "subagents"):
    return next(item for item in agent.status().realms if item.id == realm_id)


async def run_child(
    agent: LioriumAgent,
    *,
    name: str = "r1",
    prompt: str = "调研 6x7",
    message: str = "查一下",
    toolsets: str = "read,find",
) -> dict:
    """建一个子代理并跑完一个回合，返回 ``wait_for_subagent`` 的结果。"""
    tools = tools_of(agent)
    created = json.loads(
        await tools["newSubagent"].ainvoke(
            {"name": name, "prompt": prompt, "toolsets": toolsets}
        )
    )
    assert created["ok"] is True, created
    await tools["invokeSubagent"].ainvoke({"name": name, "message": message})
    waited = json.loads(
        await tools["wait_for_subagent"].ainvoke({"name": name, "timeout": 30})
    )
    assert waited["ok"] is True, waited
    return waited


@pytest.mark.asyncio
async def test_registry_and_texts_survive_restart(agent_workspace: Path) -> None:
    first = make_parent(agent_workspace, ["结论：42"])
    await first.start()
    try:
        await run_child(first, name="r1", prompt="调研 6x7", toolsets="read,find")
    finally:
        await first.close()

    # 名册只放结构化元数据，转录/结果不进 JSON（否则每回合重写会越来越慢）
    raw = read_registry(agent_workspace)
    assert raw["version"] == 1
    assert [item["name"] for item in raw["children"]] == ["r1"]
    assert raw["children"][0]["toolsets"] == ["read", "find"]
    assert "output" not in raw["children"][0]
    assert "final_result" not in raw["children"][0]
    texts = {
        path.name
        for path in (agent_workspace / "subagents" / "r1").iterdir()
        if path.is_file()
    }
    assert {"TASK.md", "output.md", "finalResult.md"} <= texts

    second = make_parent(agent_workspace, ["不该被调用"])
    await second.start()
    try:
        listed = json.loads(await tools_of(second)["listSubagents"].ainvoke({}))
        assert [item["name"] for item in listed["subagents"]] == ["r1"]
        assert listed["subagents"][0]["status"] == "done"
        assert listed["subagents"][0]["toolsets"] == ["read", "find"]

        # 重启后不用重跑就能拿到结果（VFS 投影与工具返回一致）
        assert "42" in await second.vfs.read_text("/subagents/r1/finalResult.md")
        assert "调研" in await second.vfs.read_text("/subagents/r1/TASK.md")
        waited = json.loads(
            await tools_of(second)["wait_for_subagent"].ainvoke({"name": "r1", "timeout": 1})
        )
        assert waited["status"] == "done" and "42" in waited["final_result"]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_running_child_becomes_interrupted(agent_workspace: Path) -> None:
    first = make_parent(agent_workspace, ["结论：42"])
    await first.start()
    try:
        await run_child(first)
    finally:
        await first.close()

    # 模拟"上次进程在这个回合中途退出"
    raw = read_registry(agent_workspace)
    raw["children"][0]["status"] = "running"
    registry_path(agent_workspace).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    second = make_parent(agent_workspace, ["x"])
    await second.start()
    try:
        record = second._subagent_realm.children["r1"]
        assert record.status == "interrupted"
        assert record.error and "重启" in record.error
        # 名册被回写成诚实的状态，而不是一直显示"运行中"
        assert read_registry(agent_workspace)["children"][0]["status"] == "interrupted"
        waited = json.loads(
            await tools_of(second)["wait_for_subagent"].ainvoke({"name": "r1", "timeout": 1})
        )
        assert waited["ok"] is False and waited["status"] == "interrupted"
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_invoke_after_restart_continues_conversation(agent_workspace: Path) -> None:
    first = make_parent(agent_workspace, ["结论：42"])
    await first.start()
    try:
        await run_child(first, name="r1", message="查一下 6x7")
    finally:
        await first.close()

    second = make_parent(agent_workspace, ["再来一次：49"])
    await second.start()
    try:
        tools = tools_of(second)
        started = json.loads(
            await tools["invokeSubagent"].ainvoke({"name": "r1", "message": "再查 7x7"})
        )
        assert started["ok"] is True
        waited = json.loads(
            await tools["wait_for_subagent"].ainvoke({"name": "r1", "timeout": 30})
        )
        assert waited["ok"] is True and "49" in waited["final_result"]

        # 子代理的 checkpoint 是确定路径/thread：重启后再 invoke 是接着聊，不是重开
        child = second._subagent_realm.children["r1"].agent
        assert child is not None and child.started
        snapshot = await child.graph.aget_state(child._thread_config())
        messages = (snapshot.values or {}).get("messages") or []
        humans = [str(m.content) for m in messages if isinstance(m, HumanMessage)]
        assert "查一下 6x7" in humans  # 重启前那轮
        assert "再查 7x7" in humans  # 重启后这轮
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_new_subagent_reports_resumed_when_directory_exists(
    agent_workspace: Path,
) -> None:
    first = make_parent(agent_workspace, ["结论：42"])
    await first.start()
    try:
        await run_child(first)
    finally:
        await first.close()

    # 名册丢了（或上次没来得及写），目录与 checkpoint 还在 ⇒ 明确告诉模型"会接着聊"
    registry_path(agent_workspace).write_text(
        json.dumps({"version": 1, "children": []}), encoding="utf-8"
    )

    second = make_parent(agent_workspace, ["x"])
    await second.start()
    try:
        assert second._subagent_realm.children == {}
        created = json.loads(
            await tools_of(second)["newSubagent"].ainvoke({"name": "r1", "prompt": "再来"})
        )
        assert created["ok"] is True
        assert created["resumed"] is True
        fresh = json.loads(
            await tools_of(second)["newSubagent"].ainvoke({"name": "r2", "prompt": "新的"})
        )
        assert fresh["resumed"] is False
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_remove_deletes_child_directory(agent_workspace: Path) -> None:
    agent = make_parent(agent_workspace, ["结论：42"])
    await agent.start()
    try:
        await run_child(agent)
        directory = agent_workspace / "subagents" / "r1"
        assert (directory / "checkpoint.db").exists()
        removed = json.loads(
            await tools_of(agent)["removeSubagent"].ainvoke({"name": "r1"})
        )
        assert removed["ok"] is True, removed
        assert not directory.exists()
        assert agent._subagent_realm.children == {}
        assert read_registry(agent_workspace)["children"] == []
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_restored_children_count_against_max_children(agent_workspace: Path) -> None:
    first = make_parent(agent_workspace, ["结论：42"], params={"max_children": 1})
    await first.start()
    try:
        await run_child(first, name="only")
    finally:
        await first.close()

    second = make_parent(agent_workspace, ["x"], params={"max_children": 1})
    await second.start()
    try:
        assert list(second._subagent_realm.children) == ["only"]
        too_many = json.loads(
            await tools_of(second)["newSubagent"].ainvoke({"name": "extra", "prompt": "p"})
        )
        assert too_many["ok"] is False and "上限" in too_many["error"]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_corrupt_registry_fails_loudly(agent_workspace: Path) -> None:
    registry_path(agent_workspace).parent.mkdir(parents=True, exist_ok=True)
    registry_path(agent_workspace).write_text("{ not json", encoding="utf-8")

    agent = make_parent(agent_workspace, ["父回答"])
    await agent.start()
    try:
        status = realm_status(agent)
        # 不静默吞：该 realm 降级 failed，原因带路径（§C5 降级但可用）
        assert status.state == "failed"
        assert "subagents.json" in (status.error or "")
        events = [event async for event in agent.run("还在吗")]
        assert events[-1].type == "done"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_realm_toggle_keeps_children(agent_workspace: Path) -> None:
    """``/realm disable subagents`` 再 enable：名册从磁盘读回，投影与结果都还在。"""
    agent = make_parent(agent_workspace, ["结论：42", "第二轮回答"])
    await agent.start()
    try:
        await run_child(agent)
        assert await agent.set_realm_enabled("subagents", False) is False
        assert agent._subagent_realm.children == {}
        assert "/subagents" not in set(agent.vfs.mounts)
        assert read_registry(agent_workspace)["children"][0]["status"] == "done"

        assert await agent.set_realm_enabled("subagents", True) is True
        assert list(agent._subagent_realm.children) == ["r1"]
        assert sorted(agent._subagent_realm._vfs.root.children) == ["r1"]
        assert "42" in await agent.vfs.read_text("/subagents/r1/finalResult.md")

        tools = tools_of(agent)
        await tools["invokeSubagent"].ainvoke({"name": "r1", "message": "第二轮"})
        waited = json.loads(
            await tools["wait_for_subagent"].ainvoke({"name": "r1", "timeout": 30})
        )
        assert waited["ok"] is True and "第二轮回答" in waited["final_result"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_restored_output_is_available_to_vfs(agent_workspace: Path) -> None:
    first = make_parent(agent_workspace, [AIMessage(content="逐字输出")])
    await first.start()
    try:
        await run_child(first)
    finally:
        await first.close()

    second = make_parent(agent_workspace, ["x"])
    await second.start()
    try:
        record = second._subagent_realm.children["r1"]
        assert record.output == "逐字输出"
        assert record.output == (agent_workspace / "subagents" / "r1" / "output.md").read_text(
            encoding="utf-8"
        )
        assert "逐字输出" in await second.vfs.read_text("/subagents/r1/output.md")
    finally:
        await second.close()
