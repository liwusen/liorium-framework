"""``LioriumAgent`` 集成测试（假模型，不联网）。

覆盖计划 Phase 1 验收清单里能离线验证的部分：
- ``start()`` 不调模型；system prompt 进 checkpoint 且不重复植入
- 单轮对话事件序列；工具循环；工具结果进历史
- Context 的形态①（回合开始并入 user 消息）
- ``apply()`` 差量：同 spec 零变更、改 model 升 epoch
- checkpoint 跨实例持久；旧 ``checkpoint.db`` 能加载继续对话
- ``reset_dialog()`` 只留 system 段
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from liorium_testkit import make_scripted_factory
from liorium.agent import AgentSpec, Limits, LioriumAgent
from liorium.core.types import ContextItem, ev_done
from liorium.vfs.fs import VirtualFileSystem


def make_agent(
    workspace: Path,
    responses: list,
    *,
    realms: tuple = (),
    realm_factories: dict | None = None,
    core_tools: bool = True,
    **kwargs,
) -> tuple[LioriumAgent, object]:
    """建一个测试 Agent；默认装 ``core`` realm（Agent 不再自带任何工具）。"""
    from liorium.agent import RealmSpec
    from liorium.core_tool_realm import CoreToolRealm

    factory = make_scripted_factory(responses)
    realm_specs = tuple(realms)
    factories = dict(realm_factories or {})
    if core_tools:
        realm_specs = (RealmSpec(id="core"), *realm_specs)
        factories.setdefault("core", CoreToolRealm)
    spec = AgentSpec(
        id="test-agent",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md", "ROLE.md"),
        realms=realm_specs,
        limits=Limits(context_window=8000, max_turns=10),
        workspace_root=workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=factory,
        vfs=kwargs.pop("vfs", None),
        realm_factories=factories,
        **kwargs,
    )
    return agent, factory.model


async def graph_messages(agent: LioriumAgent) -> list:
    snapshot = await agent.graph.aget_state(agent._thread_config())
    return list(snapshot.values.get("messages") or [])


async def collect(agent: LioriumAgent, text: str, **kwargs) -> list:
    events = []
    async for event in agent.run(text, **kwargs):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_start_does_not_call_model(agent_workspace: Path) -> None:
    agent, model = make_agent(agent_workspace, ["你好"])
    await agent.start()
    try:
        assert model.calls == []
        assert agent.status().started is True
        messages = await graph_messages(agent)
        assert len(messages) == 1
        assert isinstance(messages[0], SystemMessage)
        assert "Liorium 的测试角色" in messages[0].content
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_simple_turn_events(agent_workspace: Path) -> None:
    agent, model = make_agent(agent_workspace, ["你好，我是 Liorium。"])
    await agent.start()
    try:
        events = await collect(agent, "在吗")
        types = [e.type for e in events]
        assert types[0] == "start"
        assert types[-1] == "done"
        text = "".join(e.get("content") or "" for e in events if e.type == "delta")
        assert text == "你好，我是 Liorium。"
        assert events[-1].get("turn_id")
        # 用户消息进历史
        messages = await graph_messages(agent)
        assert any(isinstance(m, HumanMessage) and "在吗" in str(m.content) for m in messages)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_turn_start_context_merged(agent_workspace: Path) -> None:
    agent, model = make_agent(agent_workspace, ["收到"])
    await agent.start()
    try:
        await collect(
            agent,
            "今天出门吗",
            context=[ContextItem(source="memory:recall", text="用户住在上海")],
        )
        user_text = str(model.calls[-1][-1].content)
        assert '<context source="memory:recall"' in user_text
        assert "用户住在上海" in user_text
        assert agent.context_channel.history()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_inject_queues_without_interrupt(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        await agent.inject([ContextItem(source="trigger:desktop", text="开始下雨了")])
        assert agent.status().pending_context == 1
        events = await collect(agent, "在吗")
        assert [e.type for e in events][-1] == "done"
        assert agent.status().pending_context == 0
        # 注入内容确实进了模型看到的上下文
        user_messages = [
            m for m in await graph_messages(agent) if isinstance(m, HumanMessage)
        ]
        assert any("开始下雨了" in str(m.content) for m in user_messages)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_tool_cycle(agent_workspace: Path) -> None:
    tool_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {"uri": "notes.txt"}, "id": "call_read_1", "type": "tool_call"}
        ],
    )
    agent, _model = make_agent(agent_workspace, [tool_call, "文件有三行。"])
    await agent.start()
    try:
        events = await collect(agent, "读一下 notes.txt")
        types = [e.type for e in events]
        assert "tool_start" in types
        assert "tool_result" in types
        starts = [e for e in events if e.type == "tool_start"]
        results = [e for e in events if e.type == "tool_result"]
        assert starts[0].get("tool_name") == "read"
        assert starts[0].get("call_id") == results[0].get("call_id")
        assert "第一行" in str(results[0].get("output"))
        assert types[-1] == "done"
        messages = await graph_messages(agent)
        assert any(isinstance(m, ToolMessage) for m in messages)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_same_spec_is_noop(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        before = agent.epoch.value
        result = await agent.apply(agent.spec)
        assert result.changed == ()
        assert result.failed == ()
        assert agent.epoch.value == before
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_model_change_does_not_bump_epoch(agent_workspace: Path) -> None:
    """计划 §E4：CacheEpoch 只跟"工具 schema + system prompt"，换模型不升 epoch。"""
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        before = agent.epoch.value
        new_spec = replace(agent.spec, model="fake::other")
        result = await agent.apply(new_spec)
        assert result.changed == ("model",)
        assert result.failed == ()
        assert agent.epoch.value == before
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_force_rebuilds_unchanged_spec(agent_workspace: Path) -> None:
    """force=True：spec 没变也重建模型（宿主侧模型参数变了，如思考强度）。"""
    from liorium_testkit import ScriptedChatModel

    calls: list[str] = []

    def factory(label: str) -> ScriptedChatModel:
        calls.append(label)
        return ScriptedChatModel(responses=["好"])

    spec = AgentSpec(
        id="force-agent",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=8000),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(spec=spec, model_factory=factory)
    await agent.start()
    try:
        assert len(calls) == 1
        noop = await agent.apply(agent.spec)
        assert noop.changed == ()
        assert noop.forced is False
        assert len(calls) == 1

        forced = await agent.apply(agent.spec, force=True)
        assert forced.changed == ()
        assert forced.forced is True
        assert forced.failed == ()
        assert len(calls) == 2
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_compact_model_factory_is_used(agent_workspace: Path) -> None:
    from liorium_testkit import ScriptedChatModel

    main_model = ScriptedChatModel(responses=["好"])
    compact_model = ScriptedChatModel(responses=["摘要"])
    compact_calls: list[str] = []

    def compact_factory(label: str) -> ScriptedChatModel:
        compact_calls.append(label)
        return compact_model

    spec = AgentSpec(
        id="compact-agent",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=8000),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=lambda _label: main_model,
        compact_model_factory=compact_factory,
    )
    await agent.start()
    try:
        assert compact_calls == ["fake::chat"]
        assert agent._compact.compact_model is compact_model
        assert agent.model is main_model
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_realm_tool_change_bumps_epoch(agent_workspace: Path) -> None:
    """加一个带新工具的 realm ⇒ 工具 schema 变化 ⇒ epoch +1。"""
    from langchain_core.tools import tool

    from liorium.agent import BaseRealm, RealmSpec

    class WeatherRealm(BaseRealm):
        id = "weather"

        @tool
        async def get_weather(city: str) -> str:
            """查询城市天气。"""
            return "晴"

        def tools(self):
            return [self.get_weather]

    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        before = agent.epoch.value
        new_spec = replace(agent.spec, realms=(RealmSpec(id="weather"),))
        agent.realm_factories["weather"] = lambda _spec: WeatherRealm()
        result = await agent.apply(new_spec)
        assert result.failed == ()
        assert "realms" in result.changed
        assert agent.epoch.value > before
        assert any(t.name == "get_weather" for t in agent.tools)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_runtime_failure_keeps_old_spec_and_graph(agent_workspace: Path) -> None:
    """构建新运行时失败时不能提前改 spec：否则指纹相同 → 永远用旧 graph。"""
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        old_spec = agent.spec
        old_graph = agent.graph
        old_fingerprint = old_spec.fingerprint()

        def broken_factory(_label: str):
            raise RuntimeError("模型构建失败")

        agent.model_factory = broken_factory
        result = await agent.apply(replace(old_spec, model="fake::other"))

        assert result.failed and result.failed[0][0] == "runtime"
        assert agent.spec is old_spec
        assert agent.spec.fingerprint() == old_fingerprint
        assert agent.graph is old_graph
        assert agent.status().spec_fingerprint == old_fingerprint
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_artifact_uri_reads_full_output(agent_workspace: Path) -> None:
    """``artifact://`` 必须能从 VFS 回读到完整正文（Agent 自动挂 /artifacts）。"""
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        long_text = "第一行\n" + ("x" * 2000)
        output_id = agent.artifacts.put(long_text, tool_name="demo")
        tools = {tool.name: tool for tool in agent.tools}
        text = await tools["read"].ainvoke({"uri": f"artifact://{output_id}"})
        assert "第一行" in text
        assert "x" * 2000 in text
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_concurrent_runs_are_serialized(agent_workspace: Path) -> None:
    """同一 Agent 并发 run()：第二个回合必须等第一个跑完，不能并发 reconcile。"""
    import asyncio

    agent, _model = make_agent(agent_workspace, ["第一", "第二"])
    await agent.start()
    try:
        first = agent.run("a")
        first_event = await asyncio.wait_for(first.__anext__(), timeout=2.0)
        assert first_event is not None

        second = agent.run("b")
        pending = asyncio.ensure_future(second.__anext__())
        await asyncio.sleep(0.05)
        assert not pending.done(), "第二个回合在第一个回合结束前就开始了"

        # 把第一个回合跑完
        async for _event in first:
            pass
        event = await asyncio.wait_for(pending, timeout=2.0)
        assert event is not None
        await second.aclose()
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_apply_realm_impl_change_keeps_epoch(agent_workspace: Path) -> None:
    """验收 5：改工具**实现**（schema 不变）不升 epoch，但新实现立即生效。"""
    from langchain_core.tools import tool

    from liorium.agent import BaseRealm, RealmSpec

    class WeatherRealm(BaseRealm):
        id = "weather"

        def __init__(self, answer: str) -> None:
            self.answer = answer

        def tools(self):
            impl = self.answer

            @tool("get_weather")
            async def get_weather(city: str) -> str:
                """查询城市天气。"""
                return impl

            return [get_weather]

    agent, _model = make_agent(agent_workspace, ["好"])
    agent.realm_factories["weather"] = lambda spec: WeatherRealm(
        str(spec.params.get("answer", "晴"))
    )
    agent.spec = replace(agent.spec, realms=(RealmSpec(id="weather", params={"answer": "晴"}),))
    await agent.start()
    try:
        before = agent.epoch.value
        # 只改 realm 参数（工具 schema 不变）⇒ 重新装配，但 epoch 不该变
        new_spec = replace(
            agent.spec, realms=(RealmSpec(id="weather", params={"answer": "雨"}),)
        )
        result = await agent.apply(new_spec)
        assert result.failed == ()
        assert "realms" in result.changed
        assert agent.epoch.value == before, "实现变化不应升 epoch（schema 未变）"
        tool_impl = next(t for t in agent.tools if t.name == "get_weather")
        assert await tool_impl.ainvoke({"city": "上海"}) == "雨", "新实现应立即可用"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_checkpoint_survives_restart(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["第一次"])
    await agent.start()
    await collect(agent, "记住这句话")
    count_after_first = len(await graph_messages(agent))
    await agent.close()

    agent2, _model2 = make_agent(agent_workspace, ["第二次"])
    await agent2.start()
    try:
        messages = await graph_messages(agent2)
        assert len(messages) >= count_after_first
        assert any("记住这句话" in str(m.content) for m in messages)
        events = await collect(agent2, "我说了什么")
        assert [e.type for e in events][-1] == "done"
    finally:
        await agent2.close()


@pytest.mark.asyncio
async def test_reset_dialog_keeps_only_system(agent_workspace: Path) -> None:
    agent, _model = make_agent(agent_workspace, ["好"])
    await agent.start()
    try:
        await collect(agent, "你好")
        await agent.reset_dialog()
        messages = await graph_messages(agent)
        assert len(messages) == 1
        assert isinstance(messages[0], SystemMessage)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_shared_vfs_between_parent_and_child(agent_workspace: Path) -> None:
    shared = VirtualFileSystem(owner_id="shared")
    agent, _model = make_agent(agent_workspace, ["好"], vfs=shared)
    await agent.start()
    try:
        assert agent.vfs is shared
        await shared.write("/shared-note.md", __import__("liorium.core.types", fromlist=["Text"]).Text("hi"))
        assert (await shared.read_text("/shared-note.md")) == "hi"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_loads_legacy_checkpoint(agent_workspace: Path) -> None:
    """验收 6：真实旧 checkpoint 能加载并继续对话（ishmael 库很小，直接复制）。"""
    source = Path.home() / ".liorium" / "agents" / "ishmael" / "checkpoint.db"
    if not source.exists():
        pytest.skip("本机没有旧 checkpoint.db")
    target = agent_workspace / "checkpoint.db"
    shutil.copy2(source, target)

    factory = make_scripted_factory(["接着聊"])
    spec = AgentSpec(
        id="ishmael",
        inbox_dir=agent_workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=8000, max_turns=10),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(spec=spec, model_factory=factory, checkpoint_path=target)
    await agent.start()
    try:
        messages = await graph_messages(agent)
        assert len(messages) >= 2, "旧 checkpoint 里的历史应被保留"
        assert isinstance(messages[0], SystemMessage)
        # 旧库里的 user 消息（与本次 prompt 无关）必须还在
        assert any(isinstance(m, HumanMessage) for m in messages[1:])
        events = await collect(agent, "继续")
        assert [e.type for e in events][-1] == "done"
        after = await graph_messages(agent)
        assert len(after) > len(messages)
    finally:
        await agent.close()


def test_reads_large_legacy_checkpoint_blob() -> None:
    """真实 311 条消息的旧库：LangGraph serde 能原样解出消息对象。

    不复制 987MB 的文件，直接只读打开并用 langgraph 自己的序列化器解码最新 checkpoint，
    证明"表结构 + message 序列化格式"与我们依赖的版本一致。
    """
    import sqlite3

    source = Path.home() / ".liorium" / "legacy" / "checkpoint.db"
    if not source.exists():
        pytest.skip("本机没有大的旧 checkpoint.db")

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select type, checkpoint from checkpoints where thread_id='main' "
            "order by rowid desc limit 1"
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "旧库里应有 thread_id='main' 的 checkpoint"
    serde_type, blob = row
    data = JsonPlusSerializer().loads_typed((serde_type, blob))
    messages = data["channel_values"].get("messages") or []
    assert len(messages) > 100
    assert isinstance(messages[0], SystemMessage)
    assert all(hasattr(m, "content") for m in messages)


def _rebind_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "重复绑定" in r.getMessage()]


@pytest.mark.asyncio
async def test_consecutive_turns_do_not_rebind_emitter(
    agent_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """同一实例连跑两回合：出口要正常解绑，不能每回合都误报"旧回合收尾异常"。"""
    agent, _model = make_agent(agent_workspace, ["第一", "第二"])
    await agent.start()
    try:
        with caplog.at_level(logging.ERROR, logger="liorium.agent"):
            await collect(agent, "a")
            await collect(agent, "b")
        assert _rebind_logs(caplog) == []
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_abandoned_turn_is_closed_before_release(
    agent_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """消费者中途 aclose：旧回合的收尾（解绑 / 取消任务）必须同步完成，不能落到下一回合里。"""
    agent, _model = make_agent(agent_workspace, ["第一", "第二"])
    await agent.start()
    try:
        stream = agent.run("a")
        await stream.__anext__()
        await stream.aclose()
        assert agent._turn_scope is None, "aclose 之后本回合的作用域应已清空"
        assert agent._turn_task is None, "aclose 之后本回合的任务应已清空"
        with caplog.at_level(logging.ERROR, logger="liorium.agent"):
            events = await collect(agent, "b")
        assert [e.type for e in events][-1] == "done"
        assert _rebind_logs(caplog) == []
    finally:
        await agent.close()

