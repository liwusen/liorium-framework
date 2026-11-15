"""事件契约金样（事件流原生，定稿 §6 / §7）。

约束：

- Agent 只发 ``AGENT_EVENT_TYPES`` 里的 14 种事件（``realm`` 2026-11-14 追加）；
- 每个回合**恰好一条**终态帧（``done`` / ``interrupted`` / ``error``），且是该回合
  最后一条事件；
- 入站生命周期帧（``input``）只描述条目状态，不属于"回合正文事件"；
- ``done`` 必须带 ``turn_id``（前端用它回报播放闸门 ack）；
- ``start`` 带 ``origin`` / ``inputs``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from liorium.agent import AgentSpec, Limits, LioriumAgent
from liorium.core.types import AGENT_EVENT_TYPES, Event
from liorium_testkit import ScriptedChatModel, collect_turn, make_scripted_factory

TERMINAL = ("done", "interrupted", "error")


class GatedChatModel(ScriptedChatModel):
    """卡在 ``gate`` 上、放行后才吐字的假模型（让中断发生在模型调用中途）。"""

    gate: Any = None

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(list(messages))
        if self.gate is not None:
            await self.gate.wait()
        item = self._next()
        if isinstance(item, AIMessage):
            if item.content:
                yield ChatGenerationChunk(message=AIMessageChunk(content=item.content))
            if item.tool_calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="", tool_calls=item.tool_calls)
                )
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content=str(item)))


class FailingModel(BaseChatModel):
    """每次调用都抛错：回合必须以 ``error`` 终态帧收尾，不许静默。"""

    @property
    def _llm_type(self) -> str:
        return "failing"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FailingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("provider 配置错误")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        raise RuntimeError("provider 配置错误")
        yield  # pragma: no cover - 让类型检查把它当生成器


def make_agent(
    workspace: Path, responses: list, *, model: Any | None = None
) -> tuple[LioriumAgent, object]:
    if model is None:
        factory = make_scripted_factory(responses)
        built = factory.model
    else:
        built = model

        def factory(_label: str) -> Any:
            return model

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
        built,
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
        "round_aborted",
        "interrupted",
        "error",
        "input",
        "realm",
    )


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        Event("notify", {})


def test_legacy_host_event_types_are_gone() -> None:
    """``pending`` / ``streaming`` 仍随旧 Host 机制删除。

    ``interrupted`` / ``error`` 在看板上重新出现是**事件流原生化**的结果：
    它们现在是回合终态帧（与 ``done`` 互斥），不再是宿主事件。
    """
    for legacy in ("pending", "streaming"):
        with pytest.raises(ValueError):
            Event(legacy, {})
    for terminal in ("done", "interrupted", "error"):
        assert Event(terminal, {"turn_id": "t1"}).type == terminal


@pytest.mark.asyncio
async def test_plain_turn_golden_sequence(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["你好。"])
    await agent.start()
    try:
        events = await collect_turn(agent, "在吗")
        # 入站帧（input.accepted/delivered）不属于回合正文事件
        turn_events = [event for event in events if event.type != "input"]
        types = [event.type for event in turn_events]
        assert types[0] == "start"
        assert types[-1] == "done"
        assert set(types) <= set(AGENT_EVENT_TYPES)
        assert [event.type for event in turn_events].count("done") == 1
        assert sum(1 for event in turn_events if event.type in TERMINAL) == 1
        # start/done 的 turn_id 一致；start 带 origin / inputs
        start, done = turn_events[0], turn_events[-1]
        assert start.get("turn_id") == done.get("turn_id")
        assert start.get("origin") == "user"
        assert len(start.get("inputs") or []) == 1
        # delta 只有 content 字段
        for event in turn_events:
            if event.type == "delta":
                assert set(event.payload) == {"content"}
        # 扁平 to_dict 形态（前端直接消费）
        dumped = done.to_dict()
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
        events = await collect_turn(agent, "找找 py 文件")
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
async def test_start_failure_is_exception_not_event(agent_workspace: Path) -> None:
    """模型实例构建失败发生在 ``start()``，此时还没有事件流可发，只能抛异常。"""

    def factory(_label: str):
        raise RuntimeError("provider 配置错误")

    agent, _model = make_agent(agent_workspace, ["x"])
    agent.model_factory = factory
    with pytest.raises(Exception):
        await agent.start()


@pytest.mark.asyncio
async def test_turn_failure_is_error_terminal_frame(agent_workspace: Path) -> None:
    """回合内模型失败 → ``error`` 终态帧（``error`` 字段带类名: 消息），不抛异常。"""
    agent, _model = make_agent(agent_workspace, [], model=FailingModel())
    await agent.start()
    try:
        events = await collect_turn(agent, "你好")
        terminal = events[-1]
        assert terminal.type == "error"
        assert set(terminal.payload) == {"turn_id", "error"}
        assert "RuntimeError" in str(terminal.get("error") or "")
        assert all(event.type != "done" for event in events)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_interrupt_ends_with_interrupted_terminal_frame(agent_workspace: Path) -> None:
    gate = asyncio.Event()
    model = GatedChatModel(responses=["不该发出去"], gate=gate)
    agent, _model = make_agent(agent_workspace, [], model=model)
    await agent.start()
    subscription = agent.stream(ids=["interrupt-me"])
    iterator = subscription.__aiter__()
    try:
        agent.push("你好", id="interrupt-me")
        events: list[Event] = []
        while True:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
            events.append(event)
            if event.type == "start":
                break
        # 模型调用已经卡在 gate 上：现在中断必然命中"进行中的回合"
        assert await agent.interrupt(reason="user_stop") is True
        while True:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
            events.append(event)
            if event.type in TERMINAL:
                break
        assert events[-1].type == "interrupted"
        assert events[-1].get("reason") == "user_stop"
        assert all(event.type != "done" for event in events)
        assert [event.type for event in events].count("interrupted") == 1
    finally:
        gate.set()
        await subscription.aclose()
        await agent.close()
