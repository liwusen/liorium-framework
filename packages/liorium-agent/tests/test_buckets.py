"""Agent 级入站语义：bucket 映射 / 队列纪律 / ``DROPPABLE`` / ``AUTO`` / 生命周期。

定稿 §4（入站语义）与 §8（生命周期规则）的验收测试。Worker 内部的抢占相位表在
``test_worker.py`` 覆盖；这里只验证"调用方通过 ``push`` 能看到什么"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
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
from liorium.core.errors import TurnError
from liorium.core.types import ContextItem
from liorium_testkit import collect_turn, make_scripted_factory

TERMINAL = ("done", "interrupted", "error")


# ---------------------------------------------------------------------------
# 测试道具
# ---------------------------------------------------------------------------


class SlowRealm(BaseRealm):
    """带一个会停住回合的工具：用来确定"回合正在执行工具"。"""

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
    responses: list[Any],
    *,
    realm: SlowRealm | None = None,
    batch_window: BatchWindow | None = None,
    env_interval: float = 30.0,
) -> tuple[LioriumAgent, Any]:
    factory = make_scripted_factory(responses)
    spec = AgentSpec(
        id="buckets",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="slow"),) if realm is not None else (),
        limits=Limits(context_window=8000, max_turns=10),
        workspace_root=workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={"slow": lambda _spec: realm} if realm is not None else None,
        batch_window=batch_window,
        env_interval=env_interval,
    )
    return agent, factory.model


async def drain(iterator: Any, *, timeout: float = 5.0) -> list[Any]:
    """从一个已注册的订阅迭代器收到终态帧为止。"""
    events: list[Any] = []
    while True:
        event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        events.append(event)
        if event.type in TERMINAL:
            return events


def types(events: list[Any]) -> list[str]:
    return [event.type for event in events]


def human_texts(model: Any) -> list[str]:
    texts: list[str] = []
    for call in model.calls:
        for message in reversed(call):
            if isinstance(message, HumanMessage):
                texts.append(str(message.content))
                break
    return texts


async def next_matching(iterator: Any, predicate: Any, *, timeout: float = 5.0) -> Any:
    while True:
        event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        if predicate(event):
            return event


# ---------------------------------------------------------------------------
# bucket 映射
# ---------------------------------------------------------------------------


def test_all_but_environment_open_turns() -> None:
    assert Bucket.IMMEDIATE.opens_turn is True
    assert Bucket.MAJOR.opens_turn is True
    assert Bucket.BATCHED.opens_turn is True  # 窗口到期后自己开回合
    assert Bucket.ENVIRONMENT.opens_turn is False


@pytest.mark.asyncio
async def test_environment_never_opens_turn(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["不该被调用"])
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        try:
            agent.push("状态", id="e1", bucket=Bucket.ENVIRONMENT)
            await asyncio.sleep(0.1)
            assert agent.status().running is False
            assert agent.status().queue_depth == 1

            received: list[Any] = []
            try:
                while True:
                    received.append(await asyncio.wait_for(iterator.__anext__(), timeout=0.1))
            except asyncio.TimeoutError:
                pass
            assert all(event.type == "input" for event in received)
            assert not any(event.type == "start" for event in received)
        finally:
            await subscription.aclose()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_batched_does_not_open_turn_before_window(agent_workspace: Path) -> None:
    """窗口没到期就不开回合（默认 2.5 s 安静期，这里只等 0.1 s）。"""
    agent, _model = make_agent(agent_workspace, ["不该被调用"])
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        try:
            agent.push("背景", id="b1", bucket=Bucket.BATCHED)
            await asyncio.sleep(0.1)
            assert agent.status().running is False
            assert agent.status().queue_depth == 1

            received: list[Any] = []
            try:
                while True:
                    received.append(await asyncio.wait_for(iterator.__anext__(), timeout=0.1))
            except asyncio.TimeoutError:
                pass
            assert not any(event.type == "start" for event in received)
        finally:
            await subscription.aclose()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_batched_turn_inputs_cover_whole_batch(agent_workspace: Path) -> None:
    """契约视角：批回合的 ``start.inputs`` 覆盖整批，任一批次 id 都能命中该回合。"""
    agent, model = make_agent(
        agent_workspace, ["批处理完毕"], batch_window=BatchWindow(quiet_gap=0.05)
    )
    await agent.start()
    try:
        # 只订阅整批里的**第二条**：``start.inputs`` 与它有交集，所以能收到整个回合。
        subscription = agent.stream(ids=["b2"])
        iterator = subscription.__aiter__()
        try:
            agent.push("弹幕一", id="b1", bucket=Bucket.BATCHED, origin="danmaku")
            agent.push("弹幕二", id="b2", bucket=Bucket.BATCHED, origin="danmaku")
            events = await drain(iterator)
            start = next(event for event in events if event.type == "start")
            assert start.get("inputs") == ["b1", "b2"]
            assert start.get("origin") == "danmaku", "origin 取第一条 BATCHED"
            assert events[-1].type == "done"
            assert human_texts(model)[0].startswith("弹幕一\n弹幕二")
        finally:
            await subscription.aclose()
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 队列纪律
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_opens_turn_before_major(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(
        agent_workspace, [TOOL_CALL, "A 完成", "I 完成", "M 完成"], realm=realm
    )
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a", flags=Flag.EXCLUSIVE)
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("M", id="m", bucket=Bucket.MAJOR)
        agent.push("I", id="i", bucket=Bucket.IMMEDIATE)
        realm.release.set()

        await drain(iterator)  # A 的 EXCLUSIVE 回合先收尾
        order: list[str] = []
        while len(order) < 2:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5.0)
            if event.type == "start":
                order.extend(event.get("inputs") or [])
        assert order == ["i", "m"], "IMMEDIATE 必须排在所有 MAJOR 之前"
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# DROPPABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_droppable_is_dropped_at_queue_max(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(
        agent_workspace, [TOOL_CALL, "A 完成", "B 完成"], realm=realm
    )
    await agent.start()
    try:
        agent._inbox._queue_max = 1  # noqa: SLF001 - 测试私有状态：把阈值压到 1
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("B", id="b")  # 非 DROPPABLE：不丢
        agent.push("C", id="c", flags=Flag.DROPPABLE)  # 深度 >= 1：丢
        assert agent.status().queue_depth == 1

        realm.release.set()
        events_a = await drain(iterator)
        dropped = [
            event
            for event in events_a
            if event.type == "input" and event.get("state") == "dropped"
        ]
        assert [event.get("id") for event in dropped] == ["c"]
        assert not any(
            event.type == "start" and "c" in (event.get("inputs") or [])
            for event in events_a
        )

        events_b = await drain(iterator)
        start_b = next(event for event in events_b if event.type == "start")
        assert start_b.get("inputs") == ["b"], "非 DROPPABLE 不能被丢"
        assert agent.status().queue_depth == 0
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# Flag.AUTO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_is_mutually_exclusive_with_immediate(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        with pytest.raises(ValueError):
            agent.push("x", id="x", bucket=Bucket.IMMEDIATE, flags=Flag.AUTO)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_auto_opens_turn_when_idle(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["空闲时开回合"])
    await agent.start()
    try:
        events = await collect_turn(agent, "在吗", flags=Flag.AUTO)
        assert events[-1].type == "done"
        start = next(event for event in events if event.type == "start")
        assert start.get("inputs") == [events[0].get("id")]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_auto_injects_into_running_turn(agent_workspace: Path) -> None:
    """忙碌时 ``AUTO`` 不开新回合：在安全点注入当前回合的 context。"""
    realm = SlowRealm()
    agent, model = make_agent(agent_workspace, [TOOL_CALL, "看到补充了"], realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("补充：外面下雨了", id="auto-1", flags=Flag.AUTO)
        # AUTO 走底层 ContextChannel，不开新回合
        assert agent.status().pending_context == 1
        assert agent.status().queue_depth == 0

        realm.release.set()
        events = await drain(iterator)
        assert types(events).count("start") == 1
        start = next(event for event in events if event.type == "start")
        assert start.get("inputs") == ["a"]
        assert all(
            event.type != "start" or "auto-1" not in (event.get("inputs") or [])
            for event in events
        )
        # 第二次模型调用（工具之后）能看到注入的 <context>
        assert len(model.calls) == 2
        assert any(
            "<context" in str(message.content) and "补充：外面下雨了" in str(message.content)
            for message in model.calls[1]
            if isinstance(message, HumanMessage)
        )
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# push 生命周期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_lifecycle_and_empty_id(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["好"])
    with pytest.raises(TurnError):
        agent.push("start 前", id="before-start")

    await agent.start()
    try:
        with pytest.raises(ValueError):
            agent.push("空 id", id="")
    finally:
        await agent.close()

    with pytest.raises(TurnError):
        agent.push("close 后", id="after-close")


# ---------------------------------------------------------------------------
# timeout / 同 id 替换
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_entry_is_never_materialized(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, model = make_agent(agent_workspace, [TOOL_CALL, "A 完成"], realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        agent.push("过期了", id="x1", timeout=0.01)
        await asyncio.sleep(0.05)
        assert agent.status().queue_depth == 1

        realm.release.set()
        # 过期扫描现在也发生在回合内的安全点（ENVIRONMENT 取用前），所以
        # ``input.expired`` 落在 A 回合的帧流里，而不是回合之后。
        events_a = await drain(iterator)  # A 的回合
        expired = [
            event
            for event in events_a
            if event.type == "input" and event.get("state") == "expired"
        ]
        assert [event.get("id") for event in expired] == ["x1"]
        assert agent.status().queue_depth == 0
        assert all("过期了" not in text for text in human_texts(model))
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_same_id_replaces_in_place(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, model = make_agent(agent_workspace, [TOOL_CALL, "A 完成", "第二版"], realm=realm)
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("第一版", id="dup")
        agent.push("第二版", id="dup")
        status = agent.status()
        assert status.queue_depth == 1, "同 id 必须原位替换，不能排两条"
        assert dict(status.pending_by_bucket)["MAJOR"] == 1

        realm.release.set()
        await drain(iterator)  # A
        events_b = await drain(iterator)
        start_b = next(event for event in events_b if event.type == "start")
        assert start_b.get("inputs") == ["dup"]
        assert "第二版" in human_texts(model)[-1]
        assert "第一版" not in human_texts(model)[-1]
    finally:
        realm.release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_queue_depth_and_bucket_breakdown(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(
        agent_workspace, [TOOL_CALL, "A 完成", "M 完成"], realm=realm
    )
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.inject([ContextItem(source="test:inject", text="注入的上下文")])
        agent.push("M", id="m")
        agent.push("B", id="b", bucket=Bucket.BATCHED)
        agent.push("E", id="e", bucket=Bucket.ENVIRONMENT)

        status = agent.status()
        assert status.queue_depth == 3
        by_bucket = dict(status.pending_by_bucket)
        assert by_bucket["IMMEDIATE"] == 0
        assert by_bucket["MAJOR"] == 1
        assert by_bucket["BATCHED"] == 1
        assert by_bucket["ENVIRONMENT"] == 1
        assert status.pending_context == 1

        realm.release.set()
        await drain(iterator)  # A
        # E 已在 A 回合的安全点按 env_interval 注入（首次投递立即可投），
        # 所以这里只搭车 M + BATCHED。
        events_m = await drain(iterator)
        start_m = next(event for event in events_m if event.type == "start")
        assert start_m.get("inputs") == ["m", "b"]
        assert agent.status().queue_depth == 0
        assert agent.status().pending_context == 0
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_environment_injected_mid_turn_keeps_single_turn(agent_workspace: Path) -> None:
    """环境注入落在当前回合：不新开回合，队列随之清空。"""
    realm = SlowRealm()
    agent, model = make_agent(
        agent_workspace, [TOOL_CALL, "A 完成"], realm=realm, env_interval=0.0
    )
    await agent.start()
    try:
        subscription = agent.stream()
        iterator = subscription.__aiter__()
        agent.push("A", id="a1")
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        agent.push("观众数 42", id="e1", bucket=Bucket.ENVIRONMENT)
        realm.release.set()

        events = await drain(iterator)
        assert len([event for event in events if event.type == "start"]) == 1
        assert dict(agent.status().pending_by_bucket)["ENVIRONMENT"] == 0
        assert "观众数 42" in human_texts(model)[-1], "作为独立 <context> 消息进当前回合"
    finally:
        realm.release.set()
        await agent.close()
