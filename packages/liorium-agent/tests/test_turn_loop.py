"""回合循环单测：假模型 + 真实 ``create_agent`` 图。

覆盖计划 §4.3（Context 的两种物化形态）、§4.4（中断）、§E1（单消费者生成器）
与 ``docs/design/turn-loop.md`` §1.1/§1.3/§3：

1. 纯文本一轮 → ``[start, delta..., done]``，``done.turn_id`` 正确；
2. 带工具调用的一轮 → ``tool_start`` / ``tool_result`` 顺序正确、``call_id`` 一致；
3. ``interrupt`` 置位 → 抛 ``TurnInterrupted``，且没有 ``done``；
4. 回合开始带 ContextItem → user 消息里有 ``<context source=...>``，``state.context`` 有记录；
5. 运行中 ``channel.queue([...])`` → 下一次模型调用前多一条独立 user 消息；
6. ``max_turns`` 触发 → ``TurnError``；
7. 模型异常 → ``TurnError`` 且保留原异常为 ``cause``。

测试刻意不 import ``liorium.agent.agent``：直接构造 ``TurnRuntime`` + ``create_agent``。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from liorium.agent.context import ContextChannel
from liorium.agent.realm.base import RealmTaskScope
from liorium.agent.runtime.loop import execute_turn
from liorium.agent.runtime.middleware import BASE_ORDER, build_chain
from liorium.agent.runtime.middleware.context_merge import ContextMergeMiddleware
from liorium.agent.runtime.state import LioriumState
from liorium.agent.runtime.types import TurnRequest, TurnRuntime
from liorium.agent.spec import AgentSpec, Limits
from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.types import AGENT_EVENT_TYPES, ContextItem, Event

# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------


class _RecordingModel(BaseChatModel):
    """假模型公共部分：按脚本返回消息，并记录每次调用收到的 messages。"""

    script: list[AIMessage]
    received: list[list[BaseMessage]] = Field(default_factory=list)
    error: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any) -> Any:
        """假模型不真的绑定工具，只把自身当作已绑定的 runnable。"""
        return self

    def _next_message(self) -> AIMessage:
        if self.error is not None:
            raise self.error
        if not self.script:
            raise AssertionError("假模型脚本已用完（模型调用次数超出预期）")
        return self.script.pop(0)


class ScriptedChatModel(_RecordingModel):
    """流式假模型。

    会记录**每次**调用实际收到的 messages（用于断言中间件/回合开始物化的消息真的
    进了请求），并支持：

    - ``on_call``：每次调用开始时回调（测试里用来置中断 / 往 channel 里塞 Context）；
    - ``on_chunk``：每产出一个分片前回调（测试里用来做"流中途中断"）；
    - ``error``：调用时直接抛出（测试异常包装）；
    - 流式分片：正文按两段切，``tool_calls`` 走 ``tool_call_chunks``
      （``generate_from_stream`` 会把它们合并回 ``tool_calls``）。
    """

    on_call: Any = None
    on_chunk: Any = None

    def _chunks(self, message: AIMessage) -> list[AIMessageChunk]:
        chunks: list[AIMessageChunk] = []
        content = message.content
        if isinstance(content, str) and content:
            half = max(1, len(content) // 2)
            for piece in (content[:half], content[half:]):
                if piece:
                    chunks.append(AIMessageChunk(content=piece, id=message.id))
        elif isinstance(content, list) and content:
            chunks.append(AIMessageChunk(content=content, id=message.id))
        if message.additional_kwargs:
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs=dict(message.additional_kwargs),
                    id=message.id,
                )
            )
        for index, call in enumerate(message.tool_calls):
            chunks.append(
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"], ensure_ascii=False),
                            "id": call["id"],
                            "index": index,
                        }
                    ],
                    id=message.id,
                )
            )
        if not chunks:
            chunks.append(AIMessageChunk(content="", id=message.id))
        return chunks

    # -- 模型接口 -------------------------------------------------------

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self._next_message())])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.received.append(list(messages))
        message = self._next_message()
        for chunk in self._chunks(message):
            yield ChatGenerationChunk(message=chunk)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.received.append(list(messages))
        if self.on_call is not None:
            result = self.on_call(len(self.received) - 1, list(messages))
            if asyncio.iscoroutine(result):
                await result
        message = self._next_message()
        for chunk in self._chunks(message):
            if self.on_chunk is not None:
                self.on_chunk(chunk)
            yield ChatGenerationChunk(message=chunk)


class NonStreamingModel(_RecordingModel):
    """不实现流式的假模型：``astream_events`` 下一条分片都不会有。"""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self._next_message())])


@tool
def add(a: int, b: int) -> str:
    """两数相加。"""
    return str(a + b)


# ---------------------------------------------------------------------------
# 组装辅助
# ---------------------------------------------------------------------------


def _spec(tmp_path: Path, *, max_turns: int = 10, recursion_limit: int = 50) -> AgentSpec:
    return AgentSpec(
        id="test-agent",
        inbox_dir=tmp_path,
        model="fake::scripted",
        limits=Limits(max_turns=max_turns, recursion_limit=recursion_limit),
    )


def _build(
    tmp_path: Path,
    model: _RecordingModel,
    *,
    tools: list[Any] | None = None,
    middleware: list[Any] | None = None,
    channel: ContextChannel | None = None,
    max_turns: int = 10,
    recursion_limit: int = 50,
) -> tuple[TurnRuntime, InMemorySaver]:
    saver = InMemorySaver()
    graph = create_agent(
        model=model,
        tools=list(tools or []),
        middleware=list(middleware or []),
        state_schema=LioriumState,
        checkpointer=saver,
    )
    spec = _spec(tmp_path, max_turns=max_turns, recursion_limit=recursion_limit)
    runtime = TurnRuntime(
        spec=spec,
        graph=graph,
        checkpointer=saver,
        model=model,
        tools=list(tools or []),
        middleware=list(middleware or []),
        artifacts=None,  # type: ignore[arg-type] - 回合循环不碰 artifact
        vfs=None,  # type: ignore[arg-type] - 同上
        emit=_noop_emit,
        interrupt=asyncio.Event(),
        tasks=RealmTaskScope(log_name="test.turn"),
        thread_id="main",
        api_format="openai_chat",
        extra={"context_channel": channel} if channel is not None else {},
    )
    return runtime, saver


async def _noop_emit(event: Event) -> None:
    """``TurnRuntime.emit`` 的占位实现（``execute_turn`` 直接 yield，不走它）。"""
    return None


async def _drain(runtime: TurnRuntime, request: TurnRequest) -> list[Event]:
    return [event async for event in execute_turn(runtime, request)]


def _types(events: list[Event]) -> list[str]:
    return [event.type for event in events]


def _joined(events: list[Event], event_type: str) -> str:
    return "".join(str(event.payload.get("content") or "") for event in events if event.type == event_type)


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


# ---------------------------------------------------------------------------
# 1. 纯文本一轮
# ---------------------------------------------------------------------------


async def test_plain_text_turn_emits_start_delta_done(tmp_path: Path) -> None:
    """一轮纯文本：事件序列 ``[start, delta..., done]``，``done.turn_id`` 正确。"""
    model = ScriptedChatModel(script=[AIMessage(content="你好呀")])
    runtime, _ = _build(tmp_path, model)

    events = await _drain(runtime, TurnRequest(agent_id="test-agent", turn_id="turn-1", text="hi"))

    assert _types(events)[0] == "start"
    assert _types(events)[-1] == "done"
    assert _types(events).count("start") == 1
    assert _types(events).count("done") == 1
    assert events[0].payload["turn_id"] == "turn-1"
    assert events[-1].payload["turn_id"] == "turn-1"
    assert _joined(events, "delta") == "你好呀"
    # 只发 Agent 事件，绝不发宿主事件
    assert set(_types(events)) <= set(AGENT_EVENT_TYPES)
    # 模型确实只被调了一次，且收到的是回合开始的那条 user 消息
    assert len(model.received) == 1
    assert [type(message) for message in model.received[0]] == [HumanMessage]
    assert model.received[0][0].content == "hi"


async def test_liorium_state_compiles_with_create_agent(tmp_path: Path) -> None:
    """``create_agent(state_schema=LioriumState, ...)`` 能编译，且带 context channel。"""
    model = ScriptedChatModel(script=[AIMessage(content="ok")])
    runtime, _ = _build(tmp_path, model)

    assert runtime.graph is not None
    assert "messages" in runtime.graph.channels
    assert "context" in runtime.graph.channels


async def test_non_streaming_model_still_emits_delta(tmp_path: Path) -> None:
    """不实现流式的模型也必须出正文（由 ``on_chat_model_end`` 兜底，绝不静默丢）。"""
    model = NonStreamingModel(script=[AIMessage(content="非流式正文")])
    runtime, _ = _build(tmp_path, model)

    events = await _drain(runtime, TurnRequest(agent_id="test-agent", turn_id="turn-ns", text="hi"))

    assert _types(events) == ["start", "delta", "done"]
    assert _joined(events, "delta") == "非流式正文"


# ---------------------------------------------------------------------------
# 2. 带工具调用的一轮
# ---------------------------------------------------------------------------


async def test_tool_turn_emits_start_result_in_order(tmp_path: Path) -> None:
    """工具一轮：``tool_start`` 在 ``tool_result`` 之前，且两者 ``call_id`` 一致。"""
    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"name": "add", "args": {"a": 2, "b": 3}, "id": "call_abc"}],
            ),
            AIMessage(content="结果是 5"),
        ]
    )
    runtime, _ = _build(tmp_path, model, tools=[add])

    events = await _drain(runtime, TurnRequest(agent_id="test-agent", turn_id="turn-2", text="2+3=?"))
    types = _types(events)

    assert types[0] == "start"
    assert types[-1] == "done"
    assert "tool_start" in types and "tool_result" in types
    start_index = types.index("tool_start")
    result_index = types.index("tool_result")
    assert start_index < result_index < types.index("done")

    started = events[start_index]
    finished = events[result_index]
    assert started.payload["tool_name"] == "add"
    assert started.payload["args"] == {"a": 2, "b": 3}
    assert finished.payload["tool_name"] == "add"
    assert finished.payload["output"] == "5"
    assert started.payload["call_id"] == finished.payload["call_id"]
    assert started.payload["call_id"]
    # 工具结果之后的正文仍然要出来
    assert _joined(events, "delta") == "结果是 5"
    # 两次模型调用：第一次带工具定义，第二次能看到 ToolMessage
    assert len(model.received) == 2
    assert any(isinstance(message, ToolMessage) for message in model.received[1])


# ---------------------------------------------------------------------------
# 3. 中断
# ---------------------------------------------------------------------------


async def test_interrupt_raises_and_never_emits_done(tmp_path: Path) -> None:
    """``interrupt`` 置位后抛 ``TurnInterrupted``，且没有 ``done``。"""
    interrupt = asyncio.Event()

    def _interrupt(index: int, messages: list[BaseMessage]) -> None:
        interrupt.set()

    model = ScriptedChatModel(script=[AIMessage(content="不该发出去")], on_call=_interrupt)
    runtime, _ = _build(tmp_path, model)
    runtime.interrupt = interrupt

    events: list[Event] = []
    with pytest.raises(TurnInterrupted):
        async for event in execute_turn(
            runtime, TurnRequest(agent_id="test-agent", turn_id="turn-3", text="hi")
        ):
            events.append(event)

    assert _types(events) == ["start"]
    assert "done" not in _types(events)


async def test_interrupt_mid_stream_stops_without_done(tmp_path: Path) -> None:
    """流中途置位：已产出的 delta 保留，随后抛 ``TurnInterrupted``，没有 done。"""
    interrupt = asyncio.Event()
    seen: list[AIMessageChunk] = []

    def _interrupt_on_second_chunk(chunk: AIMessageChunk) -> None:
        seen.append(chunk)
        if len(seen) == 2:
            interrupt.set()

    model = ScriptedChatModel(script=[AIMessage(content="一二三四")], on_chunk=_interrupt_on_second_chunk)
    runtime, _ = _build(tmp_path, model)
    runtime.interrupt = interrupt

    events: list[Event] = []
    with pytest.raises(TurnInterrupted):
        async for event in execute_turn(
            runtime, TurnRequest(agent_id="test-agent", turn_id="turn-3b", text="hi")
        ):
            events.append(event)

    assert "done" not in _types(events)
    # 第一帧 delta 已经交付（中断延迟 = 一个事件），之后不再有新的 delta
    assert _types(events) == ["start", "delta"]
    assert _joined(events, "delta") == "一二"


# ---------------------------------------------------------------------------
# 4. 回合开始带 ContextItem（形态①）
# ---------------------------------------------------------------------------


async def test_turn_start_context_merges_into_user_message(tmp_path: Path) -> None:
    """回合开始的 Context 并入 user 消息，并留下 ``state.context`` 审计记录。"""
    model = ScriptedChatModel(script=[AIMessage(content="记住了")])
    channel = ContextChannel()
    runtime, _ = _build(tmp_path, model, channel=channel)
    item = ContextItem(source="memory:recall", text="用户喜欢猫", meta={"score": 0.9})

    events = await _drain(
        runtime,
        TurnRequest(agent_id="test-agent", turn_id="turn-4", text="我养了什么？", context=[item]),
    )
    assert _types(events)[-1] == "done"

    user_message = model.received[0][-1]
    assert isinstance(user_message, HumanMessage)
    assert user_message.content.startswith("我养了什么？")
    assert '<context source="memory:recall" at="' in user_message.content
    assert "用户喜欢猫" in user_message.content
    assert user_message.content.rstrip().endswith("</context>")

    state = await runtime.graph.aget_state({"configurable": {"thread_id": "main"}})
    records = state.values.get("context") or []
    assert len(records) == 1
    assert records[0]["source"] == "memory:recall"
    assert records[0]["text"] == "用户喜欢猫"
    assert records[0]["message_id"] == user_message.id
    assert records[0]["turn_id"] == "turn-4"
    assert len(records[0]["at"]) == 5
    # 有 channel 时也进了 channel 的历史（集中一处审计）
    assert channel.history()[-1]["message_id"] == user_message.id


async def test_turn_without_context_keeps_state_context_empty(tmp_path: Path) -> None:
    """没有 ContextItem 时消息保持原样，``state.context`` 为空。"""
    model = ScriptedChatModel(script=[AIMessage(content="ok")])
    runtime, _ = _build(tmp_path, model)

    await _drain(runtime, TurnRequest(agent_id="test-agent", turn_id="turn-5", text="hi"))

    assert model.received[0][0].content == "hi"
    state = await runtime.graph.aget_state({"configurable": {"thread_id": "main"}})
    assert not (state.values.get("context") or [])


async def test_state_context_appends_across_turns(tmp_path: Path) -> None:
    """``state.context`` 是追加语义：多个回合的审计记录只增长、不互相覆盖。"""
    model = ScriptedChatModel(script=[AIMessage(content="一"), AIMessage(content="二")])
    runtime, _ = _build(tmp_path, model)

    await _drain(
        runtime,
        TurnRequest(
            agent_id="test-agent",
            turn_id="turn-a",
            text="第一轮",
            context=[ContextItem(source="memory:recall", text="甲")],
        ),
    )
    await _drain(
        runtime,
        TurnRequest(
            agent_id="test-agent",
            turn_id="turn-b",
            text="第二轮",
            context=[ContextItem(source="trigger:clock", text="乙")],
        ),
    )

    state = await runtime.graph.aget_state({"configurable": {"thread_id": "main"}})
    records = state.values.get("context") or []
    assert [record["source"] for record in records] == ["memory:recall", "trigger:clock"]
    assert [record["turn_id"] for record in records] == ["turn-a", "turn-b"]


# ---------------------------------------------------------------------------
# 5. 运行中注入（形态②）
# ---------------------------------------------------------------------------


async def test_pending_context_becomes_separate_user_message_before_next_model_call(
    tmp_path: Path,
) -> None:
    """运行中 ``queue()`` 的 Context 在下一次模型调用前变成独立 user 消息。"""
    channel = ContextChannel()
    pending = [ContextItem(source="subagent:research-1", text="已完成，结论见 subagents://r1/final.md")]

    def _queue_context(index: int, messages: list[BaseMessage]) -> None:
        if index == 0:
            channel.queue(pending)

    model = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[{"name": "add", "args": {"a": 1, "b": 1}, "id": "call_1"}],
            ),
            AIMessage(content="收到补充信息"),
        ],
        on_call=_queue_context,
    )
    middleware = [ContextMergeMiddleware(channel)]
    runtime, _ = _build(tmp_path, model, tools=[add], middleware=middleware)

    events = await _drain(runtime, TurnRequest(agent_id="test-agent", turn_id="turn-6", text="继续"))
    assert _types(events)[-1] == "done"

    assert len(model.received) == 2
    second_call = model.received[1]
    context_messages = [
        message
        for message in second_call
        if isinstance(message, HumanMessage) and "<context source=" in str(message.content)
    ]
    assert len(context_messages) == 1
    injected = context_messages[0]
    assert '<context source="subagent:research-1" at="' in injected.content
    assert "已完成" in injected.content
    # 位置：工具组完整之后（ToolMessage 之后），绝不插进 AIMessage(tool_calls) 与 ToolMessage 之间
    tool_index = next(
        index for index, message in enumerate(second_call) if isinstance(message, ToolMessage)
    )
    assert second_call.index(injected) > tool_index
    # 队列已清空，且审计记录指向这条消息（回合 id 由 ContextVar 带进图内）
    assert channel.pending_count() == 0
    record = channel.history()[-1]
    assert record["message_id"] == injected.id
    assert record["turn_id"] == "turn-6"


async def test_context_merge_middleware_returns_none_without_pending() -> None:
    """无 pending 时中间件不写状态（返回 ``None``）。"""
    channel = ContextChannel()
    middleware = ContextMergeMiddleware(channel)
    assert await middleware.abefore_model({"messages": [HumanMessage(content="hi")]}) is None
    assert middleware.before_model({"messages": []}) is None


async def test_context_merge_middleware_appends_independent_message() -> None:
    """有 pending 时追加一条独立 HumanMessage，并登记 ``message_id``。"""
    channel = ContextChannel()
    middleware = ContextMergeMiddleware(channel)
    channel.queue([ContextItem(source="trigger:desktop", text="用户开始直播")])

    update = await middleware.abefore_model({"messages": [HumanMessage(content="hi")]})

    assert update is not None
    messages = update["messages"]
    assert len(messages) == 2
    assert messages[0].content == "hi"
    assert '<context source="trigger:desktop"' in messages[1].content
    assert channel.history()[-1]["message_id"] == messages[1].id
    assert channel.history()[-1]["source"] == "trigger:desktop"


# ---------------------------------------------------------------------------
# 6. max_turns 保护
# ---------------------------------------------------------------------------


async def test_max_turns_raises_turn_error(tmp_path: Path) -> None:
    """模型调用次数超过 ``limits.max_turns`` → ``TurnError``（且不发 done）。"""
    looping = AIMessage(
        content="",
        tool_calls=[{"name": "add", "args": {"a": 1, "b": 1}, "id": "call_loop"}],
    )
    model = ScriptedChatModel(script=[looping] * 10)
    runtime, _ = _build(tmp_path, model, tools=[add], max_turns=2, recursion_limit=40)

    events: list[Event] = []
    with pytest.raises(TurnError) as excinfo:
        async for event in execute_turn(
            runtime, TurnRequest(agent_id="test-agent", turn_id="turn-7", text="死循环")
        ):
            events.append(event)

    assert "上限" in str(excinfo.value)
    assert "done" not in _types(events)
    # 第 3 次调用一开始就被拦下（on_chat_model_start 到达即抛），最多再多跑一次
    assert 2 <= len(model.received) <= 3


# ---------------------------------------------------------------------------
# 7. 异常包装
# ---------------------------------------------------------------------------


async def test_model_error_becomes_turn_error_with_cause(tmp_path: Path) -> None:
    """模型异常 → ``TurnError``，原异常挂在 ``cause`` 上（不静默吞）。"""
    model = ScriptedChatModel(script=[AIMessage(content="ok")], error=RuntimeError("boom"))
    runtime, _ = _build(tmp_path, model)

    events: list[Event] = []
    with pytest.raises(TurnError) as excinfo:
        async for event in execute_turn(
            runtime, TurnRequest(agent_id="test-agent", turn_id="turn-8", text="hi")
        ):
            events.append(event)

    assert excinfo.value.cause is not None
    assert any("boom" in str(found) for found in _cause_chain(excinfo.value))
    assert "done" not in _types(events)


async def test_error_with_empty_message_still_names_the_type(tmp_path: Path) -> None:
    """``str(exc)`` 为空时也要把类名写进 ``TurnError``（不产出空消息）。"""
    model = ScriptedChatModel(script=[AIMessage(content="ok")], error=NotImplementedError())
    runtime, _ = _build(tmp_path, model)

    with pytest.raises(TurnError) as excinfo:
        async for _event in execute_turn(
            runtime, TurnRequest(agent_id="test-agent", turn_id="turn-8b", text="hi")
        ):
            pass

    assert "NotImplementedError" in str(excinfo.value)
    assert str(excinfo.value).strip() != "回合执行失败:"


# ---------------------------------------------------------------------------
# 8. 中间件链装配
# ---------------------------------------------------------------------------


def test_build_chain_wires_context_merge_only_when_channel_given() -> None:
    """``context_channel`` 为 None 时不装；给了就装在最外层（mm_bridge 之前）。"""
    plain = build_chain(artifacts=object())
    assert not any(isinstance(entry, ContextMergeMiddleware) for entry in plain)
    assert BASE_ORDER[0] == "context_merge"

    channel = ContextChannel()
    wired = build_chain(artifacts=object(), context_channel=channel)
    assert isinstance(wired[0], ContextMergeMiddleware)
    assert wired[0].channel is channel
    # realm 中间件永远更外层
    marker = AgentMiddleware()
    outermost = build_chain(
        artifacts=object(), context_channel=channel, realm_middlewares=[marker]
    )
    assert outermost[0] is marker
    assert isinstance(outermost[1], ContextMergeMiddleware)
