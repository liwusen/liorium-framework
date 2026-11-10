"""Agent 级调度：每次调用优先级 / 抢占 / 独占 / 丢弃 / 事件不串回合。

调度器本身的行为在 ``test_scheduler.py`` 里单测；这里验证 ``run(priority=..., flags=...)``
真的接到了调度器，并且事件流严格属于"这一条消息"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from liorium.agent import AgentSpec, BaseRealm, FLAG, Limits, RealmSpec, LioriumAgent
from liorium.core.errors import TurnDropped, TurnInterrupted
from liorium_testkit import make_scripted_factory


class SlowRealm(BaseRealm):
    """带一个会停住回合的工具：测试用它确定"回合正在跑"。"""

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
    responses: list,
    *,
    realm: SlowRealm | None = None,
) -> tuple[LioriumAgent, object]:
    factory = make_scripted_factory(responses)
    spec = AgentSpec(
        id="flags",
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
    )
    return agent, factory.model


async def collect(agent: LioriumAgent, text: str, **kwargs) -> list:
    return [event async for event in agent.run(text, **kwargs)]


@pytest.mark.asyncio
async def test_run_yields_only_own_turn_events(agent_workspace: Path) -> None:
    """两个并发回合：各自的事件流只含自己的 turn_id 与自己的正文。"""
    agent, _model = make_agent(agent_workspace, ["甲", "乙"])
    await agent.start()
    try:
        first, second = await asyncio.gather(
            collect(agent, "一"), collect(agent, "二")
        )
        for events, expected in ((first, "甲"), (second, "乙")):
            turn_ids = {e.get("turn_id") for e in events if e.get("turn_id")}
            assert len(turn_ids) == 1, f"事件串了回合: {turn_ids}"
            assert next(iter(turn_ids)) == events[0].get("turn_id")
            text = "".join(str(e.get("content") or "") for e in events if e.type == "delta")
            assert text == expected
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_priority_orders_waiting_turns(agent_workspace: Path) -> None:
    """占位回合跑着时，排队的三个回合按 priority 从大到小执行（同档 FIFO）。"""
    realm = SlowRealm()
    agent, model = make_agent(
        agent_workspace, [TOOL_CALL, "ok", "ok", "ok", "ok"], realm=realm
    )
    await agent.start()
    try:
        holder = asyncio.create_task(collect(agent, "A"))
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        tasks = [
            asyncio.create_task(collect(agent, "high", priority=5)),
            asyncio.create_task(collect(agent, "mid", priority=1)),
            asyncio.create_task(collect(agent, "low", priority=0)),
        ]
        await asyncio.sleep(0.02)
        realm.release.set()
        await asyncio.wait_for(asyncio.gather(holder, *tasks), timeout=5.0)

        order: list[str] = []
        for call in model.calls:  # type: ignore[attr-defined]
            human = next(
                (m for m in reversed(call) if isinstance(m, HumanMessage)), None
            )
            if human is None:
                continue
            text = str(human.content).strip()
            if not order or order[-1] != text:
                order.append(text)
        # A 占位（工具调用 + 收尾共两次模型调用 → 去重成一条）；之后 high(5) → mid(1) → low(0)
        assert order == ["A", "high", "mid", "low"], order
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_interrupting_run_preempts_running_turn(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(agent_workspace, [TOOL_CALL, "A 完成", "B 完成"], realm=realm)
    await agent.start()
    try:
        first_task = asyncio.create_task(collect(agent, "A"))
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        second = await collect(agent, "B", priority=5, flags=FLAG.INTERRUPTING)
        assert second[-1].type == "done"

        with pytest.raises(TurnInterrupted) as excinfo:
            await asyncio.wait_for(first_task, timeout=3.0)
        assert excinfo.value.reason == "preempted"
        assert realm.cancelled is True, "被抢占的回合必须真的把工具取消掉"
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_exclusive_turn_is_not_preempted(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(agent_workspace, [TOOL_CALL, "A 完成", "B 完成"], realm=realm)
    await agent.start()
    try:
        first_task = asyncio.create_task(collect(agent, "A", flags=FLAG.EXCLUSIVE))
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        second_task = asyncio.create_task(
            collect(agent, "B", priority=9, flags=FLAG.INTERRUPTING)
        )
        await asyncio.sleep(0.05)
        assert not second_task.done(), "EXCLUSIVE 回合不能被抢占，后来者只能排队"

        realm.release.set()
        first = await asyncio.wait_for(first_task, timeout=3.0)
        assert first[-1].type == "done"
        second = await asyncio.wait_for(second_task, timeout=3.0)
        assert second[-1].type == "done"
        assert realm.cancelled is False
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_interrupting_without_higher_priority_queues(agent_workspace: Path) -> None:
    """同优先级不抢占：INTERRUPTING 只在**严格更高**优先级时才生效。"""
    realm = SlowRealm()
    agent, _model = make_agent(agent_workspace, [TOOL_CALL, "A 完成", "B 完成"], realm=realm)
    await agent.start()
    try:
        first_task = asyncio.create_task(collect(agent, "A", priority=0))
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)

        second_task = asyncio.create_task(
            collect(agent, "B", priority=0, flags=FLAG.INTERRUPTING)
        )
        await asyncio.sleep(0.05)
        assert not second_task.done()

        realm.release.set()
        first = await asyncio.wait_for(first_task, timeout=3.0)
        assert first[-1].type == "done"
        await asyncio.wait_for(second_task, timeout=3.0)
        assert realm.cancelled is False
    finally:
        realm.release.set()
        await agent.close()


@pytest.mark.asyncio
async def test_droppable_turn_is_dropped(agent_workspace: Path) -> None:
    realm = SlowRealm()
    agent, _model = make_agent(agent_workspace, [TOOL_CALL, "A 完成", "B 完成", "C 完成"], realm=realm)
    await agent.start()
    try:
        # 队列上限是调度器常量；这里直接压到 1 条（测试进程内私有状态）
        agent._scheduler._queue_max = 1  # noqa: SLF001
        holder = asyncio.create_task(collect(agent, "A"))
        await asyncio.wait_for(realm.entered.wait(), timeout=2.0)
        queued = asyncio.create_task(collect(agent, "B"))
        await asyncio.sleep(0.02)

        with pytest.raises(TurnDropped) as excinfo:
            await collect(agent, "C", priority=9, flags=FLAG.DROPPABLE)
        assert excinfo.value.reason == "congested"

        realm.release.set()
        await asyncio.wait_for(asyncio.gather(holder, queued), timeout=5.0)
    finally:
        realm.release.set()
        await agent.close()
