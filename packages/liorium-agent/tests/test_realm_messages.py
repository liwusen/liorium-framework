"""宿主 → realm 消息（2026-11-14）：``agent.send_to_realm()`` / ``BaseRealm.on_message()``。

契约要点（见 ``docs/EXTENDING.md`` §1.6 与 ``__dev__/Liorium Realm 接收宿主消息 实施计划.md``）：

- **立即执行**：不等当前回合结束，与回合并发（handler 必须廉价）；
- **等结果**：handler 的返回值原样回给宿主；
- 生命周期不对就**抛错**（未 start / 未装配 / 停用 / 失败 / 没实现 on_message / 超时）；
- 同 realm 的消息彼此串行，``stop`` 会等手上那条消息；
- 树级 realm 的消息落到**树根**那份实例；
- 消息不进事件流（想留痕就自己 ``ctx.emit``）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from liorium.agent import AgentSpec, BaseRealm, Limits, LioriumAgent, RealmMessage, RealmSpec
from liorium.core.errors import RealmError, RealmNotFoundError, TurnError
from liorium_testkit import make_scripted_factory, write_workspace

TERMINAL = ("done", "interrupted", "error")


# ---------------------------------------------------------------------------
# 道具
# ---------------------------------------------------------------------------


class MessageRealm(BaseRealm):
    """记录收到了什么、怎么处理的。"""

    id = "msgs"
    instance_scope = "agent"

    def __init__(self, handler: Any = None) -> None:
        super().__init__()
        self.messages: list[RealmMessage] = []
        self.active = 0
        self.max_active = 0
        self._handler = handler

    async def on_message(self, message: RealmMessage) -> Any:
        self.messages.append(message)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._handler is not None:
                return await self._handler(self, message)
            return {"echo": message.name, "data": dict(message.data)}
        finally:
            self.active -= 1


class SyncMessageRealm(BaseRealm):
    """同步 handler 也支持。"""

    id = "sync"

    def on_message(self, message: RealmMessage) -> str:  # 不是 async
        return f"sync:{message.name}"


class TreeMessageRealm(MessageRealm):
    id = "tree-msgs"
    instance_scope = "tree"


class NoMessageRealm(BaseRealm):
    """没实现 on_message：应该报错，不是静默丢。"""

    id = "silent"


class BrokenRealm(BaseRealm):
    """启动就失败（§C5 降级）⇒ 也不该收消息。"""

    id = "broken"

    async def on_start(self) -> None:
        raise RuntimeError("起不来")


def tool_call(name: str, *, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}],
    )


def make_agent(
    workspace: Path,
    *,
    factories: dict[str, Any],
    responses: list[Any] | None = None,
    agent_id: str = "msgs",
) -> LioriumAgent:
    spec = AgentSpec(
        id=agent_id,
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=tuple(RealmSpec(id=realm_id) for realm_id in factories),
        limits=Limits(context_window=8000, max_turns=6),
        workspace_root=workspace,
    )
    return LioriumAgent(
        spec=spec,
        model_factory=make_scripted_factory(responses or ["好"]),
        realm_factories=dict(factories),
    )


async def started_agent(workspace: Path, realm: Any, **kwargs: Any) -> tuple[LioriumAgent, Any]:
    """起一个只装了这一条 realm 的 Agent；返回 ``(agent, realm)``。"""
    agent = make_agent(workspace, factories={realm.id: lambda _spec, r=realm: r}, **kwargs)
    await agent.start()
    return agent, realm


# ---------------------------------------------------------------------------
# 基本语义
# ---------------------------------------------------------------------------


async def test_send_returns_handler_value(agent_workspace: Path) -> None:
    realm = MessageRealm()
    agent, realm = await started_agent(agent_workspace, realm)
    try:
        reply = await agent.send_to_realm("msgs", "todo.add", {"text": "写 demo"})
    finally:
        await agent.close()

    assert reply == {"echo": "todo.add", "data": {"text": "写 demo"}}
    assert len(realm.messages) == 1


async def test_send_supports_sync_handler(agent_workspace: Path) -> None:
    realm = SyncMessageRealm()
    agent, _realm = await started_agent(agent_workspace, realm)
    try:
        assert await agent.send_to_realm("sync", "sync.ping") == "sync:sync.ping"
    finally:
        await agent.close()


async def test_message_fields_and_data_copy(agent_workspace: Path) -> None:
    realm = MessageRealm()
    agent, realm = await started_agent(agent_workspace, realm)
    payload = {"text": "原件"}
    try:
        await agent.send_to_realm("msgs", "  todo.add  ", payload, origin="webui")
        payload["text"] = "宿主后来改了它"
    finally:
        await agent.close()

    message = realm.messages[0]
    assert message.realm_id == "msgs"
    assert message.name == "todo.add"
    assert message.origin == "webui"
    assert message.data == {"text": "原件"}  # 副本：宿主改原 dict 不影响


async def test_empty_name_is_rejected(agent_workspace: Path) -> None:
    agent, _realm = await started_agent(agent_workspace, MessageRealm())
    try:
        with pytest.raises(RealmError, match="非空 name"):
            await agent.send_to_realm("msgs", "   ")
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 生命周期：拿不到就报错，不静默丢
# ---------------------------------------------------------------------------


async def test_unknown_realm_raises_not_found(agent_workspace: Path) -> None:
    agent, _realm = await started_agent(agent_workspace, MessageRealm())
    try:
        with pytest.raises(RealmNotFoundError):
            await agent.send_to_realm("nope", "any.event")
    finally:
        await agent.close()


async def test_realm_without_handler_raises(agent_workspace: Path) -> None:
    agent, _realm = await started_agent(agent_workspace, NoMessageRealm())
    try:
        with pytest.raises(RealmError, match="不接受消息"):
            await agent.send_to_realm("silent", "silent.ping")
    finally:
        await agent.close()


async def test_disabled_realm_raises(agent_workspace: Path) -> None:
    agent, _realm = await started_agent(agent_workspace, MessageRealm())
    try:
        assert await agent.set_realm_enabled("msgs", False) is False
        with pytest.raises(RealmError, match="不能接收消息"):
            await agent.send_to_realm("msgs", "todo.add", {"text": "x"})
        assert await agent.set_realm_enabled("msgs", True) is True
        # 重新启用后又能收（状态从 state 文件读回）
        assert await agent.send_to_realm("msgs", "todo.add", {"text": "x"})
    finally:
        await agent.close()


async def test_failed_realm_raises(agent_workspace: Path) -> None:
    agent = make_agent(agent_workspace, factories={"broken": BrokenRealm})
    await agent.start()
    try:
        record = agent.realms.record("broken")
        assert record.state == "failed"
        with pytest.raises(RealmError, match="不能接收消息"):
            await agent.send_to_realm("broken", "broken.ping")
    finally:
        await agent.close()


async def test_agent_not_started_and_closed_raise(agent_workspace: Path) -> None:
    realm = MessageRealm()
    agent = make_agent(agent_workspace, factories={realm.id: lambda _spec, r=realm: r})
    with pytest.raises(TurnError, match="未 start"):
        await agent.send_to_realm("msgs", "todo.add", {"text": "x"})

    await agent.start()
    await agent.close()
    with pytest.raises(TurnError, match="已 close"):
        await agent.send_to_realm("msgs", "todo.add", {"text": "x"})


# ---------------------------------------------------------------------------
# 并发与调度
# ---------------------------------------------------------------------------


async def test_concurrent_sends_are_serialized(agent_workspace: Path) -> None:
    async def slow(_realm: MessageRealm, message: RealmMessage) -> str:
        await asyncio.sleep(0.02)
        return message.name

    realm = MessageRealm(slow)
    agent, realm = await started_agent(agent_workspace, realm)
    try:
        replies = await asyncio.gather(
            agent.send_to_realm("msgs", "a"),
            agent.send_to_realm("msgs", "b"),
            agent.send_to_realm("msgs", "c"),
        )
    finally:
        await agent.close()

    assert replies == ["a", "b", "c"]  # 顺序由锁保证
    assert realm.max_active == 1  # 没有交错


async def test_send_is_immediate_during_turn(agent_workspace: Path) -> None:
    """回合进行中（工具还卡着）也能立即收到消息，不等回合结束。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowToolRealm(BaseRealm):
        id = "msgs"

        def tools(self) -> list[Any]:
            @tool("slow_tool")
            async def slow_tool() -> str:
                """卡住直到测试放开。"""
                entered.set()
                await release.wait()
                return "slow-done"

            return [slow_tool]

        async def on_message(self, message: RealmMessage) -> str:
            return "handled-during-turn"

    realm = SlowToolRealm()
    agent, _realm = await started_agent(
        agent_workspace, realm, responses=[tool_call("slow_tool"), "好了。"]
    )
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    frames: list[Any] = []

    async def drain_until_done() -> None:
        while True:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5)
            frames.append(event)
            if event.type in TERMINAL:
                return

    try:
        agent.push("跑工具", id="q1")
        # 等工具真的开始执行（此时回合一定没结束）
        while not entered.is_set():
            event = await asyncio.wait_for(iterator.__anext__(), timeout=5)
            frames.append(event)

        reply = await asyncio.wait_for(
            agent.send_to_realm("msgs", "probe.ping"), timeout=2
        )
        assert reply == "handled-during-turn"
        assert not release.is_set()  # 工具还卡着 ⇒ 回合在跑，消息已经处理完

        release.set()
        await drain_until_done()
    finally:
        release.set()
        await subscription.aclose()
        await agent.close()

    starts = [event for event in frames if event.type == "start"]
    assert len(starts) == 1  # 消息没有新开回合
    assert frames[-1].type == "done"


async def test_stop_waits_for_inflight_message(agent_workspace: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(_realm: MessageRealm, _message: RealmMessage) -> str:
        entered.set()
        await release.wait()
        return "晚了"

    realm = MessageRealm(blocked)
    agent, _realm = await started_agent(agent_workspace, realm)
    try:
        send_task = asyncio.create_task(agent.send_to_realm("msgs", "todo.add", {"text": "x"}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        stop_task = asyncio.create_task(agent.set_realm_enabled("msgs", False))
        await asyncio.sleep(0.05)
        assert not stop_task.done()  # stop 在等这条消息放锁

        release.set()
        assert await send_task == "晚了"
        await stop_task
        assert agent.realms.record("msgs").state == "stopped"
    finally:
        release.set()
        await agent.close()


# ---------------------------------------------------------------------------
# 错误：原样上抛 / 超时 / 防互锁
# ---------------------------------------------------------------------------


async def test_handler_exception_propagates(agent_workspace: Path) -> None:
    async def boom(_realm: MessageRealm, _message: RealmMessage) -> None:
        raise ValueError("handler 炸了")

    agent, _realm = await started_agent(agent_workspace, MessageRealm(boom))
    try:
        with pytest.raises(ValueError, match="handler 炸了"):
            await agent.send_to_realm("msgs", "todo.add", {"text": "x"})
    finally:
        await agent.close()


async def test_timeout_raises_realm_error(agent_workspace: Path) -> None:
    async def slow(_realm: MessageRealm, _message: RealmMessage) -> str:
        await asyncio.sleep(1)
        return "太晚了"

    agent, _realm = await started_agent(agent_workspace, MessageRealm(slow))
    try:
        with pytest.raises(RealmError, match="超时"):
            await agent.send_to_realm("msgs", "todo.add", {"text": "x"}, timeout=0.01)
    finally:
        await agent.close()


async def test_handler_cannot_run_maintenance(agent_workspace: Path) -> None:
    holder: dict[str, LioriumAgent] = {}

    async def maintenance(_realm: MessageRealm, _message: RealmMessage) -> None:
        await holder["agent"].refresh_realms()

    agent, _realm = await started_agent(agent_workspace, MessageRealm(maintenance))
    holder["agent"] = agent
    try:
        with pytest.raises(TurnError, match="on_message"):
            await agent.send_to_realm("msgs", "todo.add", {"text": "x"})
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 与事件流的组合
# ---------------------------------------------------------------------------


async def test_handler_can_emit_realm_frame(agent_workspace: Path) -> None:
    async def emit(_realm: MessageRealm, message: RealmMessage) -> str:
        _realm.ctx.emit("msgs.pong", dict(message.data))
        return "ok"

    agent, _realm = await started_agent(agent_workspace, MessageRealm(emit))
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    try:
        assert await agent.send_to_realm("msgs", "msgs.ping", {"n": 1}) == "ok"
        event = await asyncio.wait_for(iterator.__anext__(), timeout=5)
        assert event.type == "realm"
        assert event.get("realm") == "msgs"
        assert event.get("name") == "msgs.pong"
        assert event.get("data") == {"n": 1}
        assert event.get("turn_id") is None  # 空闲时发 ⇒ 不属于任何回合
    finally:
        await subscription.aclose()
        await agent.close()


# ---------------------------------------------------------------------------
# 树级 realm：消息落到树根那份实例
# ---------------------------------------------------------------------------


async def make_child(parent: LioriumAgent, tmp_path: Path) -> LioriumAgent:
    root = write_workspace(tmp_path / "child")
    spec = AgentSpec(
        id="child",
        inbox_dir=root,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=parent.spec.realms,
        limits=Limits(context_window=8000, max_turns=4),
        workspace_root=root,
    )
    child = LioriumAgent(
        spec=spec,
        model_factory=make_scripted_factory(["好"]),
        vfs=parent.vfs,
        root_agent=parent,
        realm_factories=parent.realm_factories,
    )
    await child.start()
    return child


async def test_tree_realm_message_lands_on_root_instance(tmp_path: Path) -> None:
    parent_root = write_workspace(tmp_path / "root")
    tree = TreeMessageRealm()
    parent = make_agent(
        parent_root,
        # agent 级 realm 用工厂建（每个 Agent 一份）；树级那份单独持有，便于断言"同一实例"
        factories={"tree-msgs": lambda _spec: tree, "msgs": lambda _spec: MessageRealm()},
    )
    await parent.start()
    child = await make_child(parent, tmp_path)
    try:
        assert child.realms.record("tree-msgs").instance is tree
        reply = await child.send_to_realm("tree-msgs", "tree.ping", {"n": 7})
        assert reply == {"echo": "tree.ping", "data": {"n": 7}}
        assert len(tree.messages) == 1  # 根的那份实例收到了

        # agent 级 realm 不借：子 Agent 有自己的实例，消息不会串到根
        parent_msgs = parent.realms.record("msgs").instance
        child_msgs = child.realms.record("msgs").instance
        assert child_msgs is not parent_msgs
        await child.send_to_realm("msgs", "child.ping")
        assert len(child_msgs.messages) == 1
        assert parent_msgs.messages == []
    finally:
        await child.close()
        await parent.close()
