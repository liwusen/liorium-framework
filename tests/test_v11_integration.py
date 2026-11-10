"""v1.1 端到端：memory + skills 两个能力包接到真实 Agent 上（假模型，离线）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent
from liorium.core_tool_realm import CoreToolRealm
from liorium.memory import MemoryRealm, SqliteMemoryStore
from liorium.skills import SkillsRealm
from liorium.todo_realm import TodoRealm
from liorium_testkit import make_scripted_factory


def tool_call(call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def write_skill(root: Path, slug: str, description: str) -> None:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n# {slug} 用法\n", encoding="utf-8", newline="\n"
    )


async def collect(agent: LioriumAgent, text: str) -> list:
    return [event async for event in agent.run(text)]


@pytest.mark.asyncio
async def test_agent_with_memory_and_skills(agent_workspace: Path) -> None:
    write_skill(agent_workspace / "skill.d", "demo", "演示技能")
    responses = [
        tool_call("c1", "write", {"path": "memories://notes/cat.md", "content": "猫喜欢晒太阳。"}),
        tool_call("c2", "read", {"uri": "memories://notes/cat.md"}),
        tool_call("c3", "read", {"uri": "memories://notes"}),
        tool_call("c4", "memory_search", {"query": "晒太阳"}),
        tool_call("c5", "read", {"uri": "skills://"}),
        "记住了。",
    ]
    factory = make_scripted_factory(responses)
    spec = AgentSpec(
        id="v11-e2e",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="core"), RealmSpec(id="memory"), RealmSpec(id="skills")),
        limits=Limits(context_window=16000, max_turns=8),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={
            "core": CoreToolRealm,
            "memory": MemoryRealm,
            "skills": SkillsRealm,
        },
    )
    await agent.start()
    try:
        # 1) 工具与 prompt 都装上了
        names = [tool.name for tool in agent.tools]
        assert {"read", "write", "memory_search", "list_skills"} <= set(names)
        prompt = agent._prompt.text  # noqa: SLF001 - 断言 system prompt
        assert "## 技能清单" in prompt
        assert "demo：演示技能" in prompt
        assert "memories://" in prompt

        # 2) 跑一轮：写记忆 → 读记忆 → 列目录 → 检索 → 列技能
        events = await collect(agent, "记住猫喜欢晒太阳")
        assert events[-1].type == "done"
        results = [event for event in events if event.type == "tool_result"]
        outputs = [event["output"] for event in results]
        assert any("已存入记忆 /notes/cat.md" in text for text in outputs)  # ACK 通道
        assert any("猫喜欢晒太阳" in text for text in outputs)
        assert any("1 lines" in text for text in outputs)  # 记忆元数据投影
        search_payload = json.loads(next(text for text in outputs if text.startswith("[")))
        assert search_payload[0]["path"] == "/notes/cat.md"
        assert any("skills:// 可用 skill" in text and "demo" in text for text in outputs)

        # 3) 落盘：SQLite 里真有这条记忆
        store = SqliteMemoryStore(agent_workspace / "memory" / "memory.sqlite")
        try:
            assert store.read("/notes/cat.md").text == "猫喜欢晒太阳。"
        finally:
            store.close()

        # 4) 装一个新技能 → 下一回合边界 dirty 刷新，prompt 与 epoch 变化
        epoch_before = agent.epoch.value
        write_skill(agent_workspace / "skill.d", "extra", "后来装的技能")
        events = await collect(agent, "还有技能吗")
        assert events[-1].type == "done"
        assert "extra" in agent._prompt.text  # noqa: SLF001
        assert agent.epoch.value == epoch_before + 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_with_todo_realm_injects_context(agent_workspace: Path) -> None:
    """待办：工具可用 + 下一回合就收到周期提醒（第五拍端到端）。

    ``context_first=True`` 且第 1 回合清单还是空的 ⇒ 不消费周期；第 2 回合模型刚加了
    一条 ``pending``，节拍器立刻注入 —— 这正是"一有事情马上提醒"的语义。
    """
    responses = [
        tool_call("d1", "todo", {"action": "add", "text": "写周报"}),
        "记下了。",
        tool_call("d2", "todo", {"action": "update", "id": "t1", "status": "processing"}),
        "在做。",
    ]
    spec = AgentSpec(
        id="todo-e2e",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="todo"),),
        limits=Limits(context_window=16000, max_turns=8),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=make_scripted_factory(responses),
        realm_factories={"todo": TodoRealm},
    )
    await agent.start()
    try:
        assert {"todo", "todo_list"} <= {tool.name for tool in agent.tools}
        assert "`todo_list`" in agent._prompt.text  # noqa: SLF001 - prompt_fragment 进 system prompt

        first = await collect(agent, "帮我记一条待办")
        assert first[-1].type == "done"
        outputs = [event["output"] for event in first if event.type == "tool_result"]
        assert any("已添加 t1 [pending] 写周报" in text for text in outputs)

        second = await collect(agent, "我开始写周报了")
        assert second[-1].type == "done"
        outputs = [event["output"] for event in second if event.type == "tool_result"]
        assert any("已更新 t1 [processing] 写周报" in text for text in outputs)

        # 历史里有一条独立的 <context> 消息，内容就是这份清单 + 行为提醒
        snapshot = await agent._graph.aget_state(agent._thread_config())  # noqa: SLF001
        messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])
        blocks = [
            str(message.content)
            for message in messages
            if message.type == "human" and '<context source="realm:todo"' in str(message.content)
        ]
        assert len(blocks) == 1
        assert "待办提醒：1 条未完成" in blocks[0]
        assert "- [pending] t1 写周报" in blocks[0]
        assert "做完 → finished" in blocks[0]

        # 相位已从成功注入起算（50）并落盘；审计记录挂在第 2 回合
        payload = json.loads(
            (agent_workspace / "realm_state" / "todo.json").read_text(encoding="utf-8")
        )
        assert payload["__context_due__"] == 50
        assert payload["items"][0]["state"] == "processing"
        history = agent.context_channel.history()
        assert [record["turn_id"] for record in history] == ["todo-e2e#2"]

        status = agent.status().realms[0]
        assert status.id == "todo"
        assert status.context_injections == 1
        assert status.context_due == 50
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_memory_edit_through_core_tool(agent_workspace: Path) -> None:
    """``edit`` 走 VFS 的 edit_handler，改的是 SQLite 里的正文。"""
    store = SqliteMemoryStore(agent_workspace / "memory" / "memory.sqlite")
    store.write("/a.md", "旧内容")
    store.close()

    responses = [
        tool_call("e1", "edit", {"path": "memories://a.md", "old_str": "旧", "new_str": "新"}),
        "改好了。",
    ]
    factory = make_scripted_factory(responses)
    spec = AgentSpec(
        id="v11-edit",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="core"), RealmSpec(id="memory")),
        limits=Limits(context_window=8000, max_turns=5),
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={"core": CoreToolRealm, "memory": MemoryRealm},
    )
    await agent.start()
    try:
        events = await collect(agent, "把旧改成新")
        assert events[-1].type == "done"
        results = [event["output"] for event in events if event.type == "tool_result"]
        assert any("已更新记忆 /a.md" in text for text in results)

        check = SqliteMemoryStore(agent_workspace / "memory" / "memory.sqlite")
        try:
            assert check.read("/a.md").text == "新内容"
        finally:
            check.close()
    finally:
        await agent.close()
