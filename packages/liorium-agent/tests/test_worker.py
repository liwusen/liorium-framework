"""TurnWorker：常驻循环 / bucket 队列纪律 / ``IMMEDIATE`` 抢占 / 终态帧 / 维护插队。

对应定稿 §12 P2 与 §13 测试矩阵里能离线验证的部分（假模型 / 假 realm，不联网）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

from liorium.agent import (
    AgentSpec,
    BaseRealm,
    BatchWindow,
    Bucket,
    Flag,
    Limits,
    LioriumAgent,
    RealmSpec,
)
from liorium_testkit import make_scripted_factory

TERMINAL = ("done", "interrupted", "error")


# ---------------------------------------------------------------------------
# 测试道具
# ---------------------------------------------------------------------------


class GatedChatModel(BaseChatModel):
    """先卡在 ``gate`` 上、放行后才吐字的分块假模型（确定"正在流式输出"的时刻）。"""

    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []
    entered: Any = None
    gate: Any = None
    yield_before_gate: int = 0
    """放行前先吐几个字（用来构造"已外化"的抢占场景）。"""

    @property
    def _llm_type(self) -> str:
        return "liorium-gated"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "GatedChatModel":
        return self

    def _next(self) -> Any:
        index = len(self.calls) - 1
        if 0 <= index < len(self.responses):
            return self.responses[index]
        return self.responses[-1] if self.responses else ""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        item = self._next()
        message = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(list(messages))
        item = self._next()
        if self.entered is not None:
            self.entered.set()
        text = str(item)
        prefix = text[: self.yield_before_gate] if self.yield_before_gate else ""
        if prefix:
            yield ChatGenerationChunk(message=AIMessageChunk(content=prefix))
        if self.gate is not None:
            await self.gate.wait()
        rest = text[len(prefix) :]
        for chunk in (rest[: max(1, len(rest) // 2)], rest[max(1, len(rest) // 2) :]):
            if chunk:
                yield ChatGenerationChunk(message=AIMessageChunk(content=chunk))


class SlowRealm(BaseRealm):
    """一个会停住回合的工具：用来确定"回合正在执行工具"。"""

    id = "slow"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    def tools(self) -> list:
        realm = self

        @tool
        async def slow() -> str:
            """停住回合，直到测试放行（被取消时记一笔）。"""
            realm.entered.set()
            try:
                await realm.release.wait()
            except asyncio.CancelledError:
                realm.cancelled = True
                raise
            return "放行了"

        return [slow]


TOOL_CALL = AIMessage(
    content="",
    tool_calls=[{"name": "slow", "args": {}, "id": "c1", "type": "tool_call"}],
)


def make_agent(
    workspace: Path,
    model: Any,
    *,
    realm: SlowRealm | None = None,
    batch_window: BatchWindow | None = None,
    env_interval: float = 30.0,
) -> LioriumAgent:
    spec = AgentSpec(
        id="worker",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="slow"),) if realm is not None else (),
        limits=Limits(context_window=8000, max_turns=20),
        workspace_root=workspace,
    )

    def factory(_label: str) -> Any:
        return model

    return LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={"slow": lambda _spec: realm} if realm is not None else None,
        batch_window=batch_window,
        env_interval=env_interval,
    )


def make_gated(responses: list[Any]) -> tuple[GatedChatModel, asyncio.Event]:
    gate = asyncio.Event()
    model = GatedChatModel(responses=responses, entered=asyncio.Event(), gate=gate)
    return model, gate


async def collect(queue: Any, *, timeout: float = 5.0) -> list[Any]:
    """从一个已注册的 ``StreamSubscription`` 迭代器收到终态帧为止。"""
    events: list[Any] = []
    while True:
        event = await asyncio.wait_for(queue.__anext__(), timeout=timeout)
        events.append(event)
        if event.type in TERMINAL:
            return events


def types(events: list[Any]) -> list[str]:
    return [event.type for event in events]


def deltas(events: list[Any]) -> str:
    return "".join(str(e.get("content") or "") for e in events if e.type == "delta")


def human_texts(model: Any) -> list[str]:
    texts: list[str] = []
    for call in model.calls:
        for message in reversed(call):
            if isinstance(message, HumanMessage):
                texts.append(str(message.content))
                break
    return texts


# ---------------------------------------------------------------------------
# 基本回合 / 终态帧
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_stream_round_trip(agent_workspace: Path) -> None:
    agent = make_agent(agent_workspace, make_scripted_factory(["你好，我是 Liorium。"]).model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("在吗", id="m1", bucket=Bucket.MAJOR)
        events = await collect(iterator)

        assert events[0].type == "input" and events[0].get("id") == "m1"
        assert events[0].get("state") == "accepted"
        start = next(e for e in events if e.type == "start")
        assert start.get("inputs") == ["m1"]
        assert start.get("origin") == "user"
        assert deltas(events) == "你好，我是 Liorium。"
        assert events[-1].type == "done"
        assert events[-1].get("turn_id") == start.get("turn_id")
        delivery = [e for e in events if e.type == "input" and e.get("state") == "delivered"]
        assert delivery and delivery[0].get("turn_id") == start.get("turn_id")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_exactly_one_terminal_frame_per_turn(agent_workspace: Path) -> None:
    agent = make_agent(agent_workspace, make_scripted_factory(["甲", "乙"]).model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("一", id="a")
        agent.push("二", id="b")
        seen: list[str] = []
        while len(seen) < 2:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            if event.type in TERMINAL:
                seen.append(str(event.get("turn_id")))
        assert len(set(seen)) == 2
        await asyncio.sleep(0.05)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_fifo_major_order(agent_workspace: Path) -> None:
    model = make_scripted_factory(["一", "二", "三"]).model
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        agent.push("B", id="b")
        agent.push("C", id="c")
        starts: list[list[str]] = []
        while len(starts) < 3:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            if event.type == "start":
                starts.append(list(event.get("inputs") or []))
        # 三个回合串行：回合开始的顺序 = 投递顺序
        assert starts == [["a"], ["b"], ["c"]]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_immediate_runs_before_queued_major(agent_workspace: Path) -> None:
    """``EXCLUSIVE`` 占位回合跑着时：``IMMEDIATE`` 排在所有 ``MAJOR`` 之前。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "B 完成", "C 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a", bucket=Bucket.MAJOR, flags=Flag.EXCLUSIVE)
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("M", id="m", bucket=Bucket.MAJOR)
        agent.push("I", id="i", bucket=Bucket.IMMEDIATE)
        realm.release.set()
        # 先收完 A（EXCLUSIVE）的回合，再看后面两个回合的顺序
        await collect(iterator)
        order: list[str] = []
        while len(order) < 2:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            if event.type == "start":
                order.extend(event.get("inputs") or [])
        assert order == ["i", "m"]
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# IMMEDIATE 相位表
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_preempts_streaming_round(agent_workspace: Path) -> None:
    """流式输出中收到 ``IMMEDIATE``：同一回合取消本轮、补 round_aborted、换消息续跑。"""
    model, gate = make_gated(["第一段", "回应B"])
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(model.entered.wait(), timeout=2.0)
        agent.push("B", id="b1", bucket=Bucket.IMMEDIATE)
        gate.set()
        events = await collect(iterator)

        starts = [e for e in events if e.type == "start"]
        assert len(starts) == 1, "抢占不得开新回合"
        assert starts[0].get("inputs") == ["a1"]
        assert types(events).count("round_aborted") == 1
        assert types(events).count("done") == 1
        # 被切掉的那一轮一个字都没流出来；续跑内容来自新消息
        assert deltas(events) == "回应B"
        aborted_index = types(events).index("round_aborted")
        assert aborted_index < types(events).index("delta")
        delivered = [e for e in events if e.type == "input" and e.get("id") == "b1"]
        assert delivered and delivered[-1].get("state") == "delivered"
        assert delivered[-1].get("turn_id") == starts[0].get("turn_id")
    finally:
        gate.set()
        await agent.close()


@pytest.mark.asyncio
async def test_immediate_after_delta_keeps_prefix(agent_workspace: Path) -> None:
    """已外化的抢占：已经流出去的正文前缀保留，``round_aborted`` 只标记半句话。"""
    gate = asyncio.Event()
    model = GatedChatModel(
        responses=["第一段", "回应B"], entered=asyncio.Event(), gate=gate, yield_before_gate=1
    )
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(model.entered.wait(), timeout=2.0)
        first = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        while first.type != "delta":
            first = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        assert first.get("content") == "第"

        agent.push("B", id="b1", bucket=Bucket.IMMEDIATE)
        gate.set()
        events = await collect(iterator)
        text = deltas([first, *events])
        assert "第" in text, "已发出的前缀必须保留"
        assert "回应B" in deltas(events)
        assert types(events).count("round_aborted") == 1
        assert types(events).count("done") == 1
    finally:
        gate.set()
        await agent.close()


@pytest.mark.asyncio
async def test_immediate_waits_for_tool_group(agent_workspace: Path) -> None:
    """工具执行中不打断：等整组工具跑完，在安全点换消息续跑。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "新回答"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("B", id="b1", bucket=Bucket.IMMEDIATE)
        await asyncio.sleep(0.1)
        assert realm.cancelled is False, "工具执行中不得被抢占取消"
        realm.release.set()
        events = await collect(iterator)

        assert realm.cancelled is False
        assert "tool_result" in types(events)
        assert types(events).count("round_aborted") == 1
        assert types(events).count("done") == 1
        assert deltas(events) == "新回答"
        # 注入消息在工具结果之后（provider 协议：AI(tool_calls) 与 ToolMessage 必须紧邻）
        assert types(events).index("tool_result") < types(events).index("round_aborted")
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_exclusive_turn_is_not_preempted(agent_workspace: Path) -> None:
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "B 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1", flags=Flag.EXCLUSIVE)
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("B", id="b1", bucket=Bucket.IMMEDIATE)
        await asyncio.sleep(0.05)
        realm.release.set()

        first = await collect(iterator)
        assert "round_aborted" not in types(first)
        assert first[-1].type == "done"
        second = await collect(iterator)
        assert second[-1].type == "done"
        assert [e for e in second if e.type == "start"][0].get("inputs") == ["b1"]
        assert realm.cancelled is False
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# 入站生命周期 / bucket 语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_entry_is_never_materialized(agent_workspace: Path) -> None:
    """过期条目永不物化：worker 忙时投递、到期后只发 ``input.expired``。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "不该被调用"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("过期了", id="x1", timeout=0.01)
        await asyncio.sleep(0.05)
        assert agent.status().queue_depth == 1
        realm.release.set()
        # 过期扫描现在也发生在回合内的安全点（环境取用前），所以 ``input.expired``
        # 落在本回合的帧流里，而不是回合之后。
        events = await collect(iterator)
        expired = [
            event
            for event in events
            if event.type == "input" and event.get("state") == "expired"
        ]
        assert [event.get("id") for event in expired] == ["x1"]
        assert agent.status().queue_depth == 0
        assert all("过期了" not in text for text in human_texts(model))
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_same_id_reuse_is_a_new_entry(agent_workspace: Path) -> None:
    """已物化的 id 再次 push = 普通新条目（不报错，各自有完整生命周期帧）。"""
    model = make_scripted_factory(["一", "二"]).model
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        subscription = agent.stream(ids=["same"])
        iterator = subscription.__aiter__()
        agent.push("第一次", id="same")
        first = await collect(iterator)
        agent.push("第二次", id="same")
        second = await collect(iterator)

        assert [e.type for e in first].count("done") == 1
        assert [e.type for e in second].count("done") == 1
        assert any(e.type == "input" and e.get("state") == "delivered" for e in second)
        assert human_texts(model)[:2] == ["第一次", "第二次"]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_batched_rides_along_with_pending_major(agent_workspace: Path) -> None:
    """窗口内的批次不单独开回合；有 ``MAJOR`` 排队时按形态①并入它的回合。"""
    model = make_scripted_factory(["收到"]).model
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("背景信息", id="b1", bucket=Bucket.BATCHED, origin="environment")
        await asyncio.sleep(0.1)
        assert agent.status().running is False, "合批窗口没到期，不该开回合"
        assert agent.status().queue_depth == 1

        agent.push("请回答", id="m1")
        events = await collect(iterator)
        starts = [e for e in events if e.type == "start"]
        assert len(starts) == 1
        assert starts[0].get("inputs") == ["m1", "b1"]
        # 合批内容并入本回合的 user 消息
        assert "背景信息" in human_texts(model)[0]
        assert "<context" in human_texts(model)[0]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_batched_opens_turn_when_idle(agent_workspace: Path) -> None:
    """空闲：窗口到期 ⇒ 整批合成一条 user 消息开回合（``ENVIRONMENT`` 一起搭车）。"""
    model = make_scripted_factory(["收到"]).model
    agent = make_agent(agent_workspace, model, batch_window=BatchWindow(quiet_gap=0.05))
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("弹幕一", id="b1", bucket=Bucket.BATCHED)
        agent.push("弹幕二", id="b2", bucket=Bucket.BATCHED)
        agent.push("观众数 42", id="e1", bucket=Bucket.ENVIRONMENT)

        events = await collect(iterator)
        starts = [event for event in events if event.type == "start"]
        assert len(starts) == 1, "一批只开一个回合"
        assert starts[0].get("inputs") == ["b1", "b2", "e1"]

        text = human_texts(model)[0]
        assert text.startswith("弹幕一\n弹幕二"), "正文按队列序换行拼接"
        assert "<context" in text and "观众数 42" in text, "ENVIRONMENT 并入同一条 user 消息"

        delivered = [
            event for event in events if event.type == "input" and event.get("state") == "delivered"
        ]
        assert [event.get("id") for event in delivered] == ["b1", "b2", "e1"]
        assert all(event.get("turn_id") == starts[0].get("turn_id") for event in delivered)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_batched_waits_for_running_turn(agent_workspace: Path) -> None:
    """忙：窗口到期也不插进正在跑的回合，等它 ``done`` 之后再开新回合。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "批次收到"]).model
    agent = make_agent(
        agent_workspace, model, realm=realm, batch_window=BatchWindow(quiet_gap=0.05)
    )
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("弹幕一", id="b1", bucket=Bucket.BATCHED)
        agent.push("弹幕二", id="b2", bucket=Bucket.BATCHED)
        await asyncio.sleep(0.2)  # 窗口早已到期，但回合在跑
        assert agent.status().running is True
        assert dict(agent.status().pending_by_bucket)["BATCHED"] == 2

        realm.release.set()
        first = await collect(iterator)
        assert [e for e in first if e.type == "start"][0].get("inputs") == ["a1"]

        second = await collect(iterator)
        start = [e for e in second if e.type == "start"][0]
        assert start.get("inputs") == ["b1", "b2"], "整批一个回合"
        assert human_texts(model)[-1].startswith("弹幕一\n弹幕二")
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_environment_injected_mid_turn(agent_workspace: Path) -> None:
    """回合进行中 push 的环境条目：下一次安全点物化成独立 ``<context>`` 消息。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm, env_interval=0.0)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("观众数 42", id="e1", bucket=Bucket.ENVIRONMENT)
        realm.release.set()

        events = await collect(iterator)
        starts = [event for event in events if event.type == "start"]
        assert len(starts) == 1, "中途注入不开新回合"
        assert starts[0].get("inputs") == ["a1"], "环境条目不属于开回合的那批输入"
        assert events[-1].type == "done"

        delivered = [
            event
            for event in events
            if event.type == "input" and event.get("state") == "delivered"
        ]
        assert [event.get("id") for event in delivered] == ["a1", "e1"]
        assert all(event.get("turn_id") == starts[0].get("turn_id") for event in delivered)

        texts = human_texts(model)
        assert "观众数 42" not in texts[0], "第一条普通消息里没有它"
        assert "<context" in texts[1] and "观众数 42" in texts[1], "注入后那次调用才看得到"
        assert agent.status().queue_depth == 0
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_environment_throttled_mid_turn(agent_workspace: Path) -> None:
    """距上次投递不足 ``env_interval``：留在队列，等下一个回合开始时按形态①搭车。"""
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "M 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm, env_interval=100.0)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("环境：开播 3 分钟", id="e0", bucket=Bucket.ENVIRONMENT)
        agent.push("A", id="a1")  # 回合开始：e0 以形态①搭车，环境计时器起算
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("观众数 42", id="e1", bucket=Bucket.ENVIRONMENT)
        realm.release.set()

        first = await collect(iterator)
        start_a = [e for e in first if e.type == "start"][0]
        assert start_a.get("inputs") == ["a1", "e0"]
        assert all("观众数 42" not in text for text in human_texts(model)), "未到期不注入"
        assert dict(agent.status().pending_by_bucket)["ENVIRONMENT"] == 1, "还在队列里等"

        agent.push("M", id="m1")
        second = await collect(iterator)
        start_m = [e for e in second if e.type == "start"][0]
        assert start_m.get("inputs") == ["m1", "e1"], "下一回合开始时搭车"
        assert "观众数 42" in human_texts(model)[-1]
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_status_pending_by_bucket(agent_workspace: Path) -> None:
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "B 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("M", id="m1")
        agent.push("B", id="b1", bucket=Bucket.BATCHED)
        agent.push("E", id="e1", bucket=Bucket.ENVIRONMENT)
        status = agent.status()
        assert status.queue_depth == 3
        assert dict(status.pending_by_bucket)["MAJOR"] == 1
        assert dict(status.pending_by_bucket)["BATCHED"] == 1
        assert dict(status.pending_by_bucket)["ENVIRONMENT"] == 1
        realm.release.set()
        await collect(iterator)
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# 维护操作 / 关闭
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_dialog_drops_pending(agent_workspace: Path) -> None:
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成", "B 完成", "C 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("B", id="b1")
        agent.push("C", id="c1")
        reset = asyncio.create_task(agent.reset_dialog())
        realm.release.set()

        events = await collect(iterator)
        await asyncio.wait_for(reset, timeout=5.0)
        dropped: list[str] = []
        while len(dropped) < 2:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            if event.type == "input" and event.get("state") == "dropped":
                dropped.append(str(event.get("id")))
        assert sorted(dropped) == ["b1", "c1"]
        # 队列真的空了
        assert agent.status().queue_depth == 0
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_close_emits_interrupted_close(agent_workspace: Path) -> None:
    realm = SlowRealm()
    model = make_scripted_factory([TOOL_CALL, "A 完成"]).model
    agent = make_agent(agent_workspace, model, realm=realm)
    await agent.start()
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    agent.push("A", id="a1")
    await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
    await agent.close()
    events = await collect(iterator, timeout=5.0)
    assert events[-1].type == "interrupted"
    assert events[-1].get("reason") == "close"


@pytest.mark.asyncio
async def test_ids_filter_sees_only_its_turn(agent_workspace: Path) -> None:
    model = make_scripted_factory(["甲", "乙"]).model
    agent = make_agent(agent_workspace, model)
    await agent.start()
    try:
        only_a = agent.stream(ids=["a1"])
        iterator = only_a.__aiter__()
        agent.push("A", id="a1")
        agent.push("B", id="b1")
        events = await collect(iterator, timeout=5.0)
        assert all(e.type != "delta" or e.get("content") != "乙" for e in events)
        assert deltas(events) == "甲"
        start = next(e for e in events if e.type == "start")
        assert start.get("inputs") == ["a1"]
        assert len([e for e in events if e.type == "done"]) == 1
        await asyncio.sleep(0.05)
    finally:
        await agent.close()
