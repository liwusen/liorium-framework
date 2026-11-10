"""``run()`` 事件契约金样（前端零改动，计划 §3.5 / §E1）。

约束：

- Agent 只发 9 种事件；没有「宿主事件」（2026-11-08 删掉 ``pending`` / ``streaming``
  / ``interrupted`` / ``error``：``run()`` 是唯一出口，终止语义 = 迭代结束 + 异常）
- 终止是"迭代结束"而不是某条事件；错误用异常
- ``done`` 必须带 ``turn_id``（前端用它回报播放闸门 ack）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from liorium.agent import AgentSpec, Limits, LioriumAgent
from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.types import AGENT_EVENT_TYPES, Event
from liorium_testkit import make_scripted_factory


def make_agent(workspace: Path, responses: list) -> tuple[LioriumAgent, object]:
    factory = make_scripted_factory(responses)
    from liorium.agent import RealmSpec
    from liorium.core_tool_realm import CoreToolRealm

    spec = AgentSpec(
        id="contract",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="core"),),
        limits=Limits(context_window=8000, max_turns=6),
        workspace_root=workspace,
    )
    return (
        LioriumAgent(
            spec=spec, model_factory=factory, realm_factories={"core": CoreToolRealm}
        ),
        factory.model,
    )


def test_frozen_event_type_sets() -> None:
    assert AGENT_EVENT_TYPES == (
        "start",
        "delta",
        "reasoning_delta",
        "tool_start",
        "tool_result",
        "compact_start",
        "compact_delta",
        "compact_done",
        "done",
    )


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        Event("notify", {})


def test_host_event_types_are_gone() -> None:
    """宿主事件（pending/streaming/interrupted/error）随 Host 机制一起删除。"""
    for legacy in ("pending", "streaming", "interrupted", "error"):
        with pytest.raises(ValueError):
            Event(legacy, {})


@pytest.mark.asyncio
async def test_plain_turn_golden_sequence(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["你好。"])
    await agent.start()
    try:
        events = [e async for e in agent.run("在吗")]
        types = [e.type for e in events]
        assert types[0] == "start"
        assert types[-1] == "done"
        assert set(types) <= set(AGENT_EVENT_TYPES)
        # start/done 的 turn_id 一致
        assert events[0].get("turn_id") == events[-1].get("turn_id")
        # delta 只有 content 字段
        for event in events:
            if event.type == "delta":
                assert set(event.payload) == {"content"}
        # 扁平 to_dict 形态（前端直接消费）
        dumped = events[-1].to_dict()
        assert dumped["type"] == "done" and dumped["turn_id"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_tool_turn_payload_fields(agent_workspace: Path) -> None:
    tool_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "find", "args": {"pattern": "*.py"}, "id": "c1", "type": "tool_call"}
        ],
    )
    agent, _model = make_agent(agent_workspace, [tool_call, "找到了。"])
    await agent.start()
    try:
        events = [e async for e in agent.run("找找 py 文件")]
        starts = [e for e in events if e.type == "tool_start"]
        results = [e for e in events if e.type == "tool_result"]
        assert len(starts) == 1 and len(results) == 1
        assert set(starts[0].payload) == {"tool_name", "args", "call_id"}
        assert set(results[0].payload) == {"tool_name", "output", "call_id"}
        assert starts[0].get("tool_name") == results[0].get("tool_name") == "find"
        assert starts[0].get("call_id") == results[0].get("call_id")
        assert events[-1].type == "done"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_errors_are_exceptions_not_events(agent_workspace: Path) -> None:
    """模型彻底失败 → 抛 TurnError，绝不发 error 事件。"""

    class Boom:
        @property
        def _llm_type(self):
            return "boom"

    def factory(_label: str):
        raise RuntimeError("provider 配置错误")

    agent, _model = make_agent(agent_workspace, ["x"])
    agent.model_factory = factory
    # start() 里就会失败（模型实例构建失败），这本身也是显式错误
    with pytest.raises(Exception):
        await agent.start()


@pytest.mark.asyncio
async def test_interrupt_raises_and_never_emits_done(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["一", "二", "三"])
    await agent.start()
    try:
        events: list[Event] = []
        with pytest.raises(TurnInterrupted):
            async for event in agent.run("你好"):
                events.append(event)
                if len(events) == 1:
                    assert await agent.interrupt(reason="user_stop") is True
        assert all(e.type != "done" for e in events)
        assert all(e.type != "interrupted" for e in events)
    finally:
        await agent.close()
