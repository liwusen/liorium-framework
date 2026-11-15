"""Realm 主动能力（2026-11-14）：``ctx.emit`` / ``ctx.push`` / ``ctx.interrupt`` /
``ctx.turn`` 与六个只读钩子。

契约要点（见 ``docs/design/event-stream.md`` 与 ``docs/EXTENDING.md``）：

- ``realm`` 帧非终态、不开回合、回合内带 ``turn_id``；
- ``ctx.push`` 默认 bucket 永不开回合（显式 ``"major"`` 才开）；
- 钩子没有 ``nxt()``、没有返回值、拿不到请求对象；顺序是"进入声明序、退出逆序"；
- 钩子异常只记日志，不影响回合。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from liorium.agent import (
    AgentSpec,
    BaseRealm,
    Bucket,
    Limits,
    LioriumAgent,
    RealmSpec,
)
from liorium.agent.realm.hooks import RealmHooksMiddleware
from liorium.agent.realm.host import RealmHostPort
from liorium.core.errors import RealmError
from liorium.realm_base import JsonStateStore, RealmContext
from liorium_testkit import collect_turn, make_scripted_factory

TERMINAL = ("done", "interrupted", "error")


# ---------------------------------------------------------------------------
# 道具
# ---------------------------------------------------------------------------


def tool_call(action: str, *, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "act", "args": {"action": action}, "id": call_id, "type": "tool_call"}
        ],
    )


class ProbeRealm(BaseRealm):
    """一个 realm：一个 ``act`` 工具 + 六个钩子，把过程记进共享日志。"""

    id = "probe"

    def __init__(
        self,
        name: str = "probe",
        *,
        log: list[str] | None = None,
        raise_on: str | None = None,
        with_tools: bool = True,
    ) -> None:
        super().__init__()
        self.id = name
        self.log = log if log is not None else []
        self.raise_on = raise_on
        self.with_tools = with_tools
        self.pushed: list[str] = []
        self.model_calls: list[Any] = []
        self.tool_calls: list[Any] = []
        self.tool_outcomes: list[Any] = []

    # -- 工具 -----------------------------------------------------------

    def tools(self) -> list[Any]:
        realm = self
        if not self.with_tools:
            return []

        @tool
        async def act(action: str) -> str:
            """按 action 做事：emit / push / push_major / interrupt / boom。"""
            ctx = realm.ctx
            assert ctx is not None
            if action == "emit":
                ctx.emit("probe.event", {"turn": ctx.turn.id if ctx.turn else None})
                return "emitted"
            if action == "push":
                realm.pushed.append(ctx.push("环境：42号在线"))
                realm.pushed.append(ctx.push("环境：晚了 30 秒的更新"))
                return "pushed"
            if action == "push_major":
                realm.pushed.append(ctx.push("我主动说一句", bucket=Bucket.MAJOR))
                return "pushed"
            if action == "interrupt":
                ctx.interrupt("realm")
                return "interrupted"
            if action == "boom":
                raise RuntimeError("工具炸了")
            return "ok"

        return [act]

    # -- 钩子 -----------------------------------------------------------

    def _note(self, text: str) -> None:
        self.log.append(f"{self.id}:{text}")
        if self.raise_on == text:
            raise RuntimeError(f"{text} 故意炸")

    async def on_turn_start(self, turn: Any) -> None:
        self._note("on_turn_start")

    async def on_turn_end(self, turn: Any, outcome: Any) -> None:
        self.log.append(f"{self.id}:on_turn_end:{outcome.kind}")

    async def on_model_start(self, call: Any) -> None:
        self.model_calls.append(call)
        self._note("on_model_start")

    async def on_model_end(self, call: Any, outcome: Any) -> None:
        self._note("on_model_end")

    async def on_tool_start(self, call: Any) -> None:
        self.tool_calls.append(call)
        self._note("on_tool_start")

    async def on_tool_end(self, call: Any, outcome: Any) -> None:
        self.tool_outcomes.append(outcome)
        self._note("on_tool_end")


def make_agent(
    workspace: Path,
    responses: list[Any],
    *,
    realms: list[BaseRealm] | None = None,
) -> tuple[LioriumAgent, Any]:
    factory = make_scripted_factory(responses)
    realms = list(realms or [])
    spec = AgentSpec(
        id="caps",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=tuple(RealmSpec(id=realm.id) for realm in realms),
        limits=Limits(context_window=8000, max_turns=10),
        workspace_root=workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        realm_factories={realm.id: (lambda _spec, r=realm: r) for realm in realms},
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


# ---------------------------------------------------------------------------
# ctx.emit
# ---------------------------------------------------------------------------


async def test_emit_inside_turn_carries_turn_id(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(
        agent_workspace, [tool_call("emit"), "好了。"], realms=[realm]
    )
    await agent.start()
    try:
        events = await collect_turn(agent, "在吗", id="q1")
    finally:
        await agent.close()

    frames = [event for event in events if event.type == "realm"]
    assert len(frames) == 1
    frame = frames[0]
    assert frame.get("realm") == "probe"
    assert frame.get("name") == "probe.event"
    start = next(event for event in events if event.type == "start")
    assert frame.get("data") == {"turn": start.get("turn_id")}
    # 回合内发出 ⇒ 带 turn_id；且它**不是**终态帧，done 仍然是最后一条。
    assert frame.get("turn_id") == start.get("turn_id")
    assert events[-1].type == "done"
    assert sum(1 for event in events if event.type in TERMINAL) == 1


async def test_emit_while_idle_opens_no_turn(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(agent_workspace, ["你好。"], realms=[realm])
    await agent.start()
    stream = agent.stream()
    iterator = stream.__aiter__()
    try:
        assert realm.ctx is not None
        realm.ctx.emit("idle.ping", {"n": 1})
        event = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        assert event.type == "realm"
        assert event.get("name") == "idle.ping"
        assert "turn_id" not in event.payload
        await asyncio.sleep(0.05)
        status = agent.status()
        assert status.running is False
        assert status.queue_depth == 0
    finally:
        await stream.aclose()
        await agent.close()


async def test_emit_without_host_is_silent_and_push_interrupt_raise(tmp_path: Path) -> None:
    """独立使用 ``RealmContext``：emit 是空操作，push / interrupt 必须报错。"""
    ctx = RealmContext(
        realm_id="solo",
        vfs=None,  # type: ignore[arg-type]
        state=JsonStateStore(tmp_path / "solo.json", realm_id="solo"),
    )
    ctx.emit("anything", {"x": 1})  # 不抛
    assert ctx.turn is None
    with pytest.raises(RealmError):
        ctx.push("喂")
    with pytest.raises(RealmError):
        ctx.interrupt()
    with pytest.raises(RealmError):
        ctx.emit("   ")


# ---------------------------------------------------------------------------
# ctx.push
# ---------------------------------------------------------------------------


async def test_push_environment_never_opens_a_turn(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, model = make_agent(
        agent_workspace,
        [tool_call("push"), "好的。", "第二轮的回答。"],
        realms=[realm],
    )
    await agent.start()
    stream = agent.stream()
    iterator = stream.__aiter__()
    try:
        # 用无过滤的流：``collect_turn`` 会按 ids 过滤掉环境条目自己的 input 帧。
        agent.push("第一轮", id="q1")
        first = await drain(iterator)
        assert [event.type for event in first].count("start") == 1

        delivered = {
            event.get("id")
            for event in first
            if event.type == "input" and event.get("state") == "delivered"
        }
        # 工具里推的那条在下一个安全点投递（首次没有节流计时）。
        assert realm.pushed[0] in delivered
        # 但 ENVIRONMENT 永远不开回合：整轮仍然只有一条 start。
        assert [event.type for event in first].count("start") == 1

        # 空闲时再推一条：不投递、不等回合结束就躺在 Inbox 里等下一回合。
        assert realm.ctx is not None
        idle_id = realm.ctx.push("空闲时推的环境")
        await asyncio.sleep(0.05)
        assert agent.status().running is False
        assert dict(agent.status().pending_by_bucket)["ENVIRONMENT"] == 1

        agent.push("第二轮", id="q2")
        second = await drain(iterator)
        start = next(event for event in second if event.type == "start")
        assert start.get("inputs") == ["q2", idle_id]
    finally:
        await stream.aclose()
        await agent.close()
    assert len(model.calls) >= 3


async def test_push_major_opens_a_new_turn_after_current(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(
        agent_workspace,
        [tool_call("push_major"), "第一回合说完。", "我主动说一句。"],
        realms=[realm],
    )
    await agent.start()
    stream = agent.stream()
    iterator = stream.__aiter__()
    try:
        agent.push("开始", id="q1")
        first = await drain(iterator)
        assert first[-1].type == "done"
        assert [event.type for event in first].count("start") == 1

        second = await drain(iterator)
        assert second[0].type == "start"
        assert second[0].get("inputs") == [realm.pushed[0]]
        assert second[-1].type == "done"
    finally:
        await stream.aclose()
        await agent.close()


def test_push_rejects_unknown_bucket() -> None:
    port = RealmHostPort(SimpleNamespace(), "probe")
    with pytest.raises(RealmError, match="未知 bucket"):
        port.push("喂", bucket="urgent")


# ---------------------------------------------------------------------------
# ctx.interrupt / ctx.turn
# ---------------------------------------------------------------------------


async def test_interrupt_from_tool_ends_turn_with_interrupted(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(agent_workspace, [tool_call("interrupt"), "不该出现"], realms=[realm])
    await agent.start()
    try:
        events = await collect_turn(agent, "打断我", id="q1")
    finally:
        await agent.close()

    terminal = [event for event in events if event.type in TERMINAL]
    assert [event.type for event in terminal] == ["interrupted"]
    assert terminal[0].get("reason") == "realm"
    assert all(event.type != "done" for event in events)


async def test_turn_is_visible_inside_tool_and_gone_after(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    seen: list[Any] = []

    class TurnSpy(ProbeRealm):
        def tools(self) -> list[Any]:
            inner = super().tools()

            @tool
            async def act(action: str) -> str:  # type: ignore[no-redef]
                """按 action 做事。"""
                assert realm.ctx is not None
                if action == "turn":
                    seen.append(realm.ctx.turn)
                    return "seen"
                return await inner[0].ainvoke({"action": action})

            return [act]

    spy = TurnSpy()
    realm = spy
    agent, _model = make_agent(agent_workspace, [tool_call("turn"), "好。"], realms=[spy])
    await agent.start()
    try:
        await collect_turn(agent, "看回合", id="q1")
        assert spy.ctx is not None
        assert spy.ctx.turn is None  # 回合外
    finally:
        await agent.close()

    assert len(seen) == 1
    assert seen[0].id == "caps#1" and seen[0].origin == "user"


# ---------------------------------------------------------------------------
# 六个钩子
# ---------------------------------------------------------------------------


async def test_hook_order_is_forward_in_and_reverse_out(agent_workspace: Path) -> None:
    log: list[str] = []
    first = ProbeRealm("a", log=log)
    second = ProbeRealm("b", log=log, with_tools=False)
    agent, _model = make_agent(
        agent_workspace, [tool_call("emit"), "好了。"], realms=[first, second]
    )
    await agent.start()
    try:
        await collect_turn(agent, "在吗", id="q1")
    finally:
        await agent.close()

    assert log == [
        "a:on_turn_start",
        "b:on_turn_start",
        "a:on_model_start",
        "b:on_model_start",
        "b:on_model_end",
        "a:on_model_end",
        "a:on_tool_start",
        "b:on_tool_start",
        "b:on_tool_end",
        "a:on_tool_end",
        "a:on_model_start",
        "b:on_model_start",
        "b:on_model_end",
        "a:on_model_end",
        "b:on_turn_end:done",
        "a:on_turn_end:done",
    ]


async def test_tool_failure_reaches_on_tool_end(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(agent_workspace, [tool_call("boom"), "它炸了。"], realms=[realm])
    await agent.start()
    try:
        events = await collect_turn(agent, "炸一个", id="q1")
    finally:
        await agent.close()

    assert events[-1].type == "done"  # 工具错误不终结回合
    assert len(realm.tool_outcomes) == 1
    assert realm.tool_outcomes[0].ok is False
    assert "RuntimeError" in (realm.tool_outcomes[0].error or "")


async def test_hook_exception_is_logged_not_fatal(agent_workspace: Path) -> None:
    realm = ProbeRealm(raise_on="on_model_start")
    agent, _model = make_agent(agent_workspace, ["我还在。"], realms=[realm])
    await agent.start()
    try:
        events = await collect_turn(agent, "在吗", id="q1")
    finally:
        await agent.close()
    assert events[-1].type == "done"
    assert "".join(
        str(event.get("content") or "") for event in events if event.type == "delta"
    ) == "我还在。"


async def test_model_call_snapshot_is_read_only(agent_workspace: Path) -> None:
    realm = ProbeRealm()
    agent, _model = make_agent(agent_workspace, [tool_call("emit"), "好了。"], realms=[realm])
    await agent.start()
    try:
        await collect_turn(agent, "在吗", id="q1")
    finally:
        await agent.close()

    call = realm.model_calls[0]
    assert isinstance(call.messages, tuple)
    assert call.index == 1
    assert call.turn is not None and call.turn.id == "caps#1"
    assert call.model == "fake::chat"
    tool = realm.tool_calls[0]
    assert isinstance(tool.args, MappingProxyType)
    assert tool.name == "act" and tool.call_id == "c1"
    with pytest.raises(TypeError):
        tool.args["action"] = "x"  # type: ignore[index]


async def test_hook_middleware_only_installed_when_overridden(agent_workspace: Path) -> None:
    class SilentRealm(BaseRealm):
        id = "silent"

    silent = SilentRealm()
    agent, _model = make_agent(agent_workspace, ["嗯。"], realms=[silent])
    await agent.start()
    try:
        installed = [mw for mw in agent._middleware if isinstance(mw, RealmHooksMiddleware)]
        assert installed == []
    finally:
        await agent.close()

    probe = ProbeRealm()
    agent, _model = make_agent(agent_workspace, ["嗯。"], realms=[probe])
    await agent.start()
    try:
        installed = [mw for mw in agent._middleware if isinstance(mw, RealmHooksMiddleware)]
        assert len(installed) == 1 and installed[0].realm_id == "probe"
    finally:
        await agent.close()


async def test_turn_end_runs_before_terminal_frame(agent_workspace: Path) -> None:
    """``on_turn_end`` 在终态帧之前：它 emit 的帧排在 done 前面。"""

    class EndingRealm(ProbeRealm):
        async def on_turn_end(self, turn: Any, outcome: Any) -> None:
            self.ctx.emit("turn.summary", {"ms": outcome.elapsed_ms})  # type: ignore[union-attr]
            await super().on_turn_end(turn, outcome)

    realm = EndingRealm()
    agent, _model = make_agent(agent_workspace, ["收工。"], realms=[realm])
    await agent.start()
    try:
        events = await collect_turn(agent, "收工", id="q1")
    finally:
        await agent.close()

    types = [event.type for event in events]
    summary = [event for event in events if event.type == "realm"]
    assert len(summary) == 1 and summary[0].get("name") == "turn.summary"
    assert types.index("realm") < types.index("done")
    assert types.count("done") == 1
