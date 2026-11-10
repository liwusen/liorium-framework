"""第五拍：``context_items()`` 的节拍器（``context_every`` / ``context_first``）。

对应 ``docs/design/realm-context.md`` §5.2 的语义表：

- ``0`` ⇒ 一次都不问；
- 每 N 个回合问一次（``context_first`` 让第 1 回合先问）；
- 返回空**不消费周期**；
- 非空 ⇒ 相位从成功注入起算并落盘（跨 ``restart`` 保留）；
- 单 realm 抛异常只跳过它自己。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from liorium.agent import AgentSpec, BaseRealm, Limits, RealmSpec, LioriumAgent
from liorium.core.types import ContextItem
from liorium_testkit import make_scripted_factory


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class CadenceRealm(BaseRealm):
    """按脚本返回内容的 realm：``payloads`` 取空后一直返回 ``None``。"""

    id = "cadence"
    context_every = 3
    context_first = True

    def __init__(self, spec: Any = None) -> None:
        super().__init__(spec)
        self.calls: list[int] = []
        self.payloads: list[Any] = []

    def context_items(self, turn: Any) -> Any:
        self.calls.append(turn.index)
        return self.payloads.pop(0) if self.payloads else None


def make_spec(workspace: Path, realms: Sequence[RealmSpec]) -> AgentSpec:
    return AgentSpec(
        id="ctx",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=tuple(realms),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=workspace,
    )


async def run_turns(agent: LioriumAgent, count: int) -> None:
    for index in range(count):
        events = [event async for event in agent.run(f"第{index + 1}回合")]
        assert events[-1].type == "done"


async def messages_of(agent: LioriumAgent) -> list[Any]:
    snapshot = await agent._graph.aget_state(agent._thread_config())  # noqa: SLF001
    return list((getattr(snapshot, "values", None) or {}).get("messages") or [])


def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def context_blocks(messages: Sequence[Any], source: str = "realm:cadence") -> list[Any]:
    needle = f'<context source="{source}"'
    return [message for message in messages if needle in _text(message)]


# ---------------------------------------------------------------------------
# 节奏
# ---------------------------------------------------------------------------


async def test_fires_every_n_turns(agent_workspace: Path) -> None:
    realm = CadenceRealm()
    realm.payloads = ["第一次", "第二次", "第三次"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 7)
        assert realm.calls == [1, 4, 7]
        assert len(context_blocks(await messages_of(agent))) == 3
    finally:
        await agent.close()


async def test_empty_does_not_consume_cycle(agent_workspace: Path) -> None:
    """到期但返回空 ⇒ 下一回合继续问；内容一出现立刻注入。"""
    realm = CadenceRealm()
    realm.payloads = [None, "来了"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 5)
        # 第 1 回合到期但空；第 2 回合立刻补上；相位重置 3 ⇒ 第 5 回合再问
        assert realm.calls == [1, 2, 5]
        assert len(context_blocks(await messages_of(agent))) == 1
    finally:
        await agent.close()


async def test_cadence_without_context_first_waits_n_turns(agent_workspace: Path) -> None:
    class NoFirst(CadenceRealm):
        context_first = False

    realm = NoFirst()
    realm.payloads = ["甲", "乙"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 6)
        assert realm.calls == [3, 6]
    finally:
        await agent.close()


async def test_params_override_class_attributes(agent_workspace: Path) -> None:
    realm = CadenceRealm()
    realm.payloads = ["甲", "乙", "丙"]
    agent = LioriumAgent(
        spec=make_spec(
            agent_workspace, [RealmSpec(id="cadence", params={"context_every": 2})]
        ),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 5)
        assert realm.calls == [1, 3, 5]
    finally:
        await agent.close()


async def test_context_origins_filters_turns(agent_workspace: Path) -> None:
    class OnlyTrigger(CadenceRealm):
        context_every = 1
        context_origins = ("trigger",)

    realm = OnlyTrigger()
    realm.payloads = ["甲", "乙"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        events = [event async for event in agent.run("用户回合")]
        assert events[-1].type == "done"
        assert realm.calls == []  # origin="user" 不匹配，连计时都不走

        [event async for event in agent.run("触发回合", origin="trigger")]
        assert realm.calls == [2]
    finally:
        await agent.close()


async def test_every_zero_never_asks(agent_workspace: Path) -> None:
    class Inert(BaseRealm):
        id = "inert"

    realm = Inert()
    calls: list[int] = []
    realm.context_items = lambda turn: calls.append(turn.index)  # type: ignore[method-assign]

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="inert")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"inert": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 3)
        assert calls == []
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 归一化 / 形状
# ---------------------------------------------------------------------------


async def test_sync_and_async_hooks_are_normalized(agent_workspace: Path) -> None:
    class SyncStr(BaseRealm):
        id = "sync-str"
        context_every = 1
        context_first = True

        def context_items(self, turn: Any) -> str:
            return "同步字符串"

    class AsyncItem(BaseRealm):
        id = "async-item"
        context_every = 1
        context_first = True

        async def context_items(self, turn: Any) -> ContextItem:
            return ContextItem(source="custom:src", text="异步 ContextItem")

    class ListOfStr(BaseRealm):
        id = "list-str"
        context_every = 1
        context_first = True

        def context_items(self, turn: Any) -> list[str]:
            return ["一条", ""]

    agent = LioriumAgent(
        spec=make_spec(
            agent_workspace,
            [RealmSpec(id="sync-str"), RealmSpec(id="async-item"), RealmSpec(id="list-str")],
        ),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"sync-str": SyncStr, "async-item": AsyncItem, "list-str": ListOfStr},
    )
    await agent.start()
    try:
        await run_turns(agent, 1)
        messages = await messages_of(agent)
        sync_blocks = context_blocks(messages, "realm:sync-str")
        assert sync_blocks
        assert context_blocks(messages, "custom:src")
        # 三条正确定源落在同一条安全点消息里；空字符串被丢弃（不是 4 条）
        assert _text(sync_blocks[0]).count("<context") == 3
        assert len(agent.context_channel.history()) == 3
    finally:
        await agent.close()


async def test_materializes_as_separate_message_with_audit(agent_workspace: Path) -> None:
    realm = CadenceRealm()
    realm.context_every = 1
    realm.payloads = ["待办提醒正文"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 1)
        messages = await messages_of(agent)
        blocks = context_blocks(messages)
        assert len(blocks) == 1
        position = messages.index(blocks[0])
        # 顺序：system → 用户那句话 → 独立的 context user 消息 → assistant
        assert messages[position - 1].type == "human"
        assert _text(messages[position - 1]) == "第1回合"
        assert blocks[0].type == "human"
        assert "待办提醒正文" in _text(blocks[0])

        history = agent.context_channel.history()
        assert len(history) == 1
        assert history[0]["source"] == "realm:cadence"
        assert history[0]["turn_id"] == "ctx#1"
        assert history[0]["message_id"] == blocks[0].id
    finally:
        await agent.close()


async def test_inject_from_realm_is_queued_beside_pull(agent_workspace: Path) -> None:
    """realm 自己 ``ctx.inject()``（推）与第五拍（拉）共用同一批安全点。"""

    class Pusher(BaseRealm):
        id = "pusher"
        context_every = 1
        context_first = True

        def context_items(self, turn: Any) -> str:
            self.ctx.inject("推送的", source="realm:pusher:push")
            return "拉取的"

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="pusher")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"pusher": Pusher},
    )
    await agent.start()
    try:
        await run_turns(agent, 1)
        messages = await messages_of(agent)
        assert context_blocks(messages, "realm:pusher:push")
        assert context_blocks(messages, "realm:pusher")
        # 同一批 ⇒ 同一条消息里的两个块
        block = _text(context_blocks(messages, "realm:pusher")[0])
        assert block.count("<context") == 2
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 持久化 / 降级 / 观测
# ---------------------------------------------------------------------------


async def test_cadence_survives_restart(agent_workspace: Path) -> None:
    seen: list[int] = []

    class Always(BaseRealm):
        id = "cadence"
        context_every = 3
        context_first = True

        def __init__(self, spec: Any = None) -> None:
            super().__init__(spec)
            self.calls = seen

        def context_items(self, turn: Any) -> str:
            self.calls.append(turn.index)
            return "内容"

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": Always},
    )
    await agent.start()
    try:
        await run_turns(agent, 1)
        assert seen == [1]

        await agent.realms.restart("cadence", spec=RealmSpec(id="cadence"))
        await run_turns(agent, 3)
        assert seen == [1, 4]
    finally:
        await agent.close()


async def test_hook_failure_degrades_to_other_realms(agent_workspace: Path, caplog) -> None:
    class Broken(BaseRealm):
        id = "broken"
        context_every = 1
        context_first = True

        def context_items(self, turn: Any) -> str:
            raise RuntimeError("列表坏了")

    class Healthy(BaseRealm):
        id = "healthy"
        context_every = 1
        context_first = True

        def context_items(self, turn: Any) -> str:
            return "正常内容"

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="broken"), RealmSpec(id="healthy")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"broken": Broken, "healthy": Healthy},
    )
    await agent.start()
    try:
        with caplog.at_level("ERROR", logger="realm.manager.ctx"):
            await run_turns(agent, 1)

        states = {status.id: status.state for status in agent.status().realms}
        assert states == {"broken": "running", "healthy": "running"}
        assert context_blocks(await messages_of(agent), "realm:healthy")
        assert any("context_items 失败" in record.getMessage() for record in caplog.records)
    finally:
        await agent.close()


async def test_status_reports_due_and_injections(agent_workspace: Path) -> None:
    realm = CadenceRealm()
    realm.payloads = ["甲", "乙"]
    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="cadence")]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"cadence": lambda _spec: realm},
    )
    await agent.start()
    try:
        await run_turns(agent, 2)
        status = agent.status().realms[0]
        assert status.context_injections == 1
        # 第 1 回合注入后重置 3，第 2 回合走一格 ⇒ 还有 2
        assert status.context_due == 2
    finally:
        await agent.close()


@pytest.mark.parametrize("bad", ["3", 1.5, True])
async def test_illegal_cadence_is_treated_as_zero(
    agent_workspace: Path, caplog, bad: Any
) -> None:
    class Odd(BaseRealm):
        id = "odd"
        context_every = 3
        context_first = True

        def context_items(self, turn: Any) -> str:
            return "不该被问"

    agent = LioriumAgent(
        spec=make_spec(agent_workspace, [RealmSpec(id="odd", params={"context_every": bad})]),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"odd": Odd},
    )
    await agent.start()
    try:
        with caplog.at_level("WARNING", logger="realm.manager.ctx"):
            await run_turns(agent, 2)
        assert not context_blocks(await messages_of(agent), "realm:odd")
        assert any("context_every 非法" in record.getMessage() for record in caplog.records)
    finally:
        await agent.close()

