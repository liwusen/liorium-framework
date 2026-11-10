"""CLI Agent 的端到端行为（桩模型，不联网）。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from liorium.cli.app import run_cli
from liorium.cli.providers import ProviderRegistry
from liorium.cli.session import CliOptions, CliSession, load_capability_realms
from liorium.cli.ui import _local_time, _memory_meta
from liorium.core.types import Text
from liorium_testkit import ScriptedChatModel, write_workspace

CONFIG = {
    "main_model": "stub::demo",
    "providers": [
        {
            "name": "stub",
            "base_url": "https://stub.invalid/v1",
            "key": "sk-stub",
            "models": ["demo", "other"],
            "model_context_lengths": {"demo": 64_000, "other": 32_000},
        }
    ],
}

CORE_TOOLS = {"read", "write", "edit", "search", "find", "execute"}
EXAMPLE_SKILLS = Path(__file__).resolve().parents[3] / "examples" / "skills"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def write_skill(root: Path, slug: str, description: str) -> Path:
    """在 ``root/<slug>/SKILL.md`` 写一个最小技能。"""
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {slug}\ndescription: {description}\nversion: 0.1.0\n---\n\n# {slug}\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def stub_factory(responses: list[Any] | None = None):
    model = ScriptedChatModel(responses=responses or ["桩模型回复"])

    def factory(_label: str) -> ScriptedChatModel:
        return model

    factory.model = model  # type: ignore[attr-defined]
    return factory


def make_session(
    tmp_path: Path,
    *,
    responses: list[Any] | None = None,
    input_lines: list[str] | None = None,
    **overrides: Any,
):
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("stub::demo")
    workspace = write_workspace(tmp_path / "ws")
    options = CliOptions(
        workspace=workspace,
        inbox=tmp_path / "inbox",
        color=False,
        **overrides,
    )
    lines = input_lines or []
    iterator: Iterator[str] = iter(lines)
    session = CliSession(
        registry,
        resolved,
        options,
        input_fn=lambda _prompt: next(iterator),
        model_factory=stub_factory(responses),
    )
    return session, registry


@pytest.mark.asyncio
async def test_core_tools_only_when_capabilities_disabled(tmp_path: Path) -> None:
    session, _registry = make_session(
        tmp_path,
        enable_memory=False,
        enable_skills=False,
        enable_todo=False,
        enable_subagents=False,
    )
    await session.start()
    try:
        assert session.agent.spec.realm_ids == ("core",)
        assert {tool.name for tool in session.agent.tools} == CORE_TOOLS
        assert session.agent.spec.limits.context_window == 64_000
        assert session.agent.spec.effective_workspace_root == (tmp_path / "ws")
        # 默认 prompt 落在 inbox 里
        assert (tmp_path / "inbox" / "AGENT.md").is_file()
        assert set(session.agent.vfs.mounts) == {"/artifacts"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_core_tools_can_be_disabled(tmp_path: Path) -> None:
    """``--no-core-tools`` ⇒ Agent 一个工具都没有（零工具是合法装配）。"""
    session, _registry = make_session(
        tmp_path,
        enable_core_tools=False,
        enable_memory=False,
        enable_skills=False,
        enable_todo=False,
        enable_subagents=False,
    )
    await session.start()
    try:
        assert session.agent.spec.realm_ids == ()
        assert session.agent.tools == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_tools_option_selects_subset(tmp_path: Path) -> None:
    """``--tools read,find`` 写进 ``toolsets["default"]``，由 core realm 解释。"""
    session, _registry = make_session(
        tmp_path,
        tools=("read", "find"),
        enable_memory=False,
        enable_skills=False,
        enable_todo=False,
        enable_subagents=False,
    )
    await session.start()
    try:
        assert session.agent.spec.toolsets == {"default": ("read", "find")}
        assert {tool.name for tool in session.agent.tools} == {"read", "find"}
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_capability_realms_wired_by_default(tmp_path: Path) -> None:
    session, _ = make_session(tmp_path)
    await session.start()
    try:
        assert session.agent.spec.realm_ids == ("core", "memory", "skills", "todo", "subagents")
        names = {tool.name for tool in session.agent.tools}
        assert CORE_TOOLS <= names
        assert {"memory_search", "list_skills", "todo", "todo_list", "newSubagent", "invokeSubagent"} <= names
        assert {"/memories", "/skills", "/subagents"} <= set(session.agent.vfs.mounts)
        status = session.agent.status()
        assert {r.id for r in status.realms} == {"core", "memory", "skills", "todo", "subagents"}
        assert all(r.state == "running" for r in status.realms)
        # 默认记忆库落在 <inbox>/memory，默认技能目录为空
        assert (tmp_path / "inbox" / "memory" / "memory.sqlite").is_file()
        assert session._realm_instance("skills").root == tmp_path / "inbox" / "skill.d"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_memory_dir_and_skills_dir_params(tmp_path: Path) -> None:
    memory_dir = tmp_path / "mem"
    skills_dir = tmp_path / "skills"
    session, _ = make_session(tmp_path, memory_dir=memory_dir, skills_dir=skills_dir)
    await session.start()
    try:
        spec = session.agent.spec
        assert spec.realm("memory").params == {
            "db_path": str(memory_dir / "memory.sqlite"),
            "content_dir": str(memory_dir / "content"),
        }
        assert spec.realm("skills").params == {"skills_dir": str(skills_dir)}
        assert (memory_dir / "memory.sqlite").is_file()
        assert (memory_dir / "content").is_dir()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_single_turn_renders_delta(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, responses=["你好，我是桩模型。"])
    await session.start()
    try:
        assert await session.run_turn("在吗") is True
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "你好，我是桩模型。" in out


@pytest.mark.asyncio
async def test_repl_commands(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        responses=["收到"],
        input_lines=[
            "/help",
            "/status",
            "/tools",
            "/memory",
            "/skills",
            "/todo",
            "/subagent",
            "/reload",
            "/context",
            "你好",
            "/exit",
        ],
    )
    await session.start()
    try:
        code = await session.repl()
    finally:
        await session.close()
    assert code == 0
    out = capsys.readouterr().out
    assert "/model" in out            # help 列表
    assert "epoch" in out             # status
    assert "read" in out and "execute" in out  # tools
    assert "还没有记忆" in out          # /memory（空库）
    assert "技能目录为空" in out        # /skills（空目录）
    assert "（没有待办" in out          # /todo（空清单）
    assert "还没有子代理" in out        # /subagent（空）
    assert "已刷新 realm" in out        # /reload
    assert "还没有物化的 Context" in out
    assert "收到" in out


@pytest.mark.asyncio
async def test_repl_model_switch(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        input_lines=["/model", "/model stub::other", "/status", "/exit"],
    )
    await session.start()
    try:
        code = await session.repl()
    finally:
        await session.close()
    assert code == 0
    assert session.resolved.label == "stub::other"
    assert session.resolved.context_window == 32_000
    out = capsys.readouterr().out
    assert "已切换到" in out


@pytest.mark.asyncio
async def test_inject_command(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        responses=["收到"],
        input_lines=["/inject 用户在测试 CLI", "你好", "/context", "/exit"],
    )
    await session.start()
    try:
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "已排队注入" in out
    assert "cli:inject" in out


@pytest.mark.asyncio
async def test_memory_command_lists_and_searches(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        input_lines=["/memory", "/memory 打呼噜", "/memory 不存在的词", "/exit"],
    )
    await session.start()
    try:
        await session.agent.vfs.write("/memories/notes/猫.md", Text("猫会打呼噜。"))
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "memories://notes/猫.md" in out
    assert "命中 1 条" in out
    assert "没有匹配 '不存在的词'" in out


@pytest.mark.asyncio
async def test_memory_graph_command(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, input_lines=["/memory graph", "/memory graph 爱丽", "/exit"])
    await session.start()
    try:
        graph = session.agent.realms.record("memory").instance.graph
        alice = graph.add("爱丽丝", "person", description="住上海的人")
        shanghai = graph.add("上海", "place")
        graph.add_relation(alice.id, shanghai.id, "located_at")
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "ent_" in out
    assert "爱丽丝" in out and "person" in out
    # 按名字模糊命中后展示邻居
    assert "上海" in out
    assert "关系 1 条" in out


def test_memory_services_follow_cli_options(tmp_path: Path) -> None:
    """没给 --embed-model 就只跑关键词；给了才注入向量/抽取服务。"""
    session, _ = make_session(tmp_path)
    assert session._memory_services() == {}  # noqa: SLF001

    session.options.embed_model = "fake-embed"
    services = session._memory_services()  # noqa: SLF001
    assert {"embedder", "vectors", "entity_vectors"} <= set(services)
    assert "extractor" in services
    assert services["embedder"].dim == 1536
    assert services["vectors"].path == session.options.inbox / "memory" / "index" / "chunks.vdb"

    session.options.enable_vectors = False
    assert session._memory_services() == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_skills_command_with_example_dir(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        input_lines=["/skills", "/exit"],
        skills_dir=EXAMPLE_SKILLS,
    )
    await session.start()
    try:
        skill_lines = session.agent.realms.skill_names()
        # 技能清单已预载进 system prompt
        prompt = session.agent._system_prompt_loaded  # noqa: SLF001
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "session-log" in out
    assert "skills://session-log/SKILL.md" in out
    assert any("session-log" in line for line in skill_lines)
    assert "session-log" in prompt


@pytest.mark.asyncio
async def test_todo_command_lists_items(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, input_lines=["/todo", "/todo all", "/exit"])
    await session.start()
    try:
        tool = next(tool for tool in session.agent.tools if tool.name == "todo")
        await tool.ainvoke({"action": "add", "text": "写周报"})
        await tool.ainvoke({"action": "add", "text": "回邮件"})
        await tool.ainvoke({"action": "update", "id": "t2", "status": "processing"})
        await session.repl()
        # 状态行显示待办摘要与下次提醒回合
        await session.handle_command("/status")
    finally:
        await session.close()

    out = capsys.readouterr().out
    assert "写周报" in out and "回邮件" in out
    assert "待办提醒" not in out  # /todo 只是列表，不是注入正文
    assert "条未完成" in out
    assert "下次提醒还有" in out  # /status


@pytest.mark.asyncio
async def test_todo_can_be_disabled(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, input_lines=["/todo", "/exit"], enable_todo=False)
    await session.start()
    try:
        assert "todo" not in session.agent.spec.realm_ids
        assert "todo" not in {tool.name for tool in session.agent.tools}
        await session.repl()
    finally:
        await session.close()
    assert "待办功能未启用" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_realm_command_lists_and_toggles(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path)
    await session.start()
    try:
        epoch = session.agent.epoch.value
        await session.handle_command("/realm")
        await session.handle_command("/realm todo")
        await session.handle_command("/realm enable memory")  # 已在运行 → 不重建
        assert session.agent.epoch.value == epoch

        await session.handle_command("/realm disable todo")
        assert "todo" not in {tool.name for tool in session.agent.tools}
        assert session.agent.realms.record("todo").state == "stopped"
        assert session.agent.epoch.value == epoch + 1

        await session.handle_command("/todo")  # 停用后给恢复提示，而不是「未启用」
        await session.handle_command("/realm todo")
        await session.handle_command("/realm enable todo")
        assert "todo" in {tool.name for tool in session.agent.tools}
        assert session.agent.realms.record("todo").state == "running"
        assert session.agent.epoch.value == epoch + 2
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "每 50 回合" in out
    assert "memory" in out and "subagents" in out
    assert "已在运行" in out
    assert "已停用 todo" in out
    assert "待办功能已停用（/realm enable todo 恢复）" in out
    assert "已启用 todo" in out


@pytest.mark.asyncio
async def test_realm_command_startup_disabled_and_unknown(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, enable_todo=False, enable_subagents=False)
    await session.start()
    try:
        await session.handle_command("/realm")
        await session.handle_command("/realm enable todo")
        await session.handle_command("/realm enable nope")
        await session.handle_command("/realm disable")
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "被 --no-todo 关掉了" in out
    assert "没有装配 realm: nope" in out
    assert "用法: /realm disable <id>" in out


@pytest.mark.asyncio
async def test_realm_command_without_any_realm(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        enable_core_tools=False,
        enable_memory=False,
        enable_skills=False,
        enable_todo=False,
        enable_subagents=False,
    )
    await session.start()
    try:
        await session.handle_command("/realm")
    finally:
        await session.close()
    assert "没有装配任何 realm" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_realm_disable_subagents_keeps_monitor_in_sync(tmp_path: Path) -> None:
    """``SubagentRealm.shutdown()`` 会清订阅者：事件监控必须断开 / 重连。"""
    session, _ = make_session(tmp_path)
    await session.start()
    try:
        monitor = session.subagents
        assert monitor is not None
        await session.handle_command("/realm disable subagents")
        assert session.subagents is None
        assert session.agent.realms.record("subagents").state == "stopped"
        await session.handle_command("/realm enable subagents")
        assert session.subagents is not None
        assert session.subagents is not monitor  # 旧监控已关闭，重新订阅
        assert session.agent.realms.record("subagents").state == "running"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_reload_picks_up_new_skill(tmp_path: Path, capsys) -> None:
    skills_dir = tmp_path / "skill.d"
    session, _ = make_session(
        tmp_path,
        input_lines=["/reload", "/skills", "/exit"],
        skills_dir=skills_dir,
    )
    await session.start()
    try:
        assert session.agent.realms.skill_names() == []
        epoch_before = session.agent.epoch.value
        write_skill(skills_dir, "late-skill", "后装的技能")
        await session.repl()
        assert session.agent.epoch.value > epoch_before
        assert any("late-skill" in line for line in session.agent.realms.skill_names())
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "已刷新 realm" in out
    assert "后装的技能" in out


@pytest.mark.asyncio
async def test_reload_without_capabilities(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        input_lines=["/reload", "/memory", "/skills", "/exit"],
        enable_memory=False,
        enable_skills=False,
    )
    await session.start()
    try:
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "没有运行中的 realm 需要刷新" in out
    assert "记忆功能未启用" in out
    assert "技能功能未启用" in out


def test_load_capability_realms_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "liorium.skills", None)
    factories, errors = load_capability_realms(["memory", "skills", "nonsense"])
    assert "memory" in factories
    assert set(errors) == {"skills"}
    assert "liorium.skills" in errors["skills"]


def test_memory_meta_shows_local_time() -> None:
    local = _local_time("2026-11-04T12:51:00Z")
    assert local.startswith("2026-11-04 ")
    assert _memory_meta("2026-11-04T12:51:00Z", ("人物",)) == f"{local}  #人物"
    assert _local_time("") == "?"
    assert _local_time("不是时间") == "不是时间"  # 非预期格式原样显示


@pytest.mark.asyncio
async def test_missing_skill_package_degrades(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "liorium.skills", None)
    session, _ = make_session(tmp_path, input_lines=["/skills", "/exit"])
    await session.start()
    try:
        assert session.agent.spec.realm_ids == ("core", "memory", "todo", "subagents")
        await session.repl()
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "skills 能力不可用" in out
    assert "liorium-agent[skills]" in out
    assert "技能功能未启用" in out


async def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """轮询事件循环直到条件成立（监控任务是异步消费，不能直接断言）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


@pytest.mark.asyncio
async def test_subagents_can_be_disabled(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path,
        enable_memory=False,
        enable_skills=False,
        enable_todo=False,
        enable_subagents=False,
    )
    await session.start()
    try:
        assert session.agent.spec.realm_ids == ("core",)
        assert "newSubagent" not in {tool.name for tool in session.agent.tools}
        assert session.subagents is None
        await session.handle_command("/subagent")
    finally:
        await session.close()
    assert "子代理功能未启用" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_subagent_command_lists_snapshot_and_controls(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, enable_memory=False, enable_skills=False)
    await session.start()
    try:
        realm = session.agent.realms.record("subagents").instance
        created = json.loads(await realm.create_child("work", "统计仓库里的 py 文件数量"))
        assert created["ok"] is True
        assert session.subagents is not None
        assert await _wait_until(lambda: session.subagents.count("work") >= 1)

        await session.handle_command("/subagent")
        await session.handle_command("/subagent work")
        await session.handle_command("/subagent subagent-work 5")
        out = capsys.readouterr().out
        assert "work" in out
        assert "已创建，等待启动" in out
        assert "统计仓库里的 py 文件数量" in out

        await session.handle_command("/subagent stop work")
        await session.handle_command("/subagent rm work")
        out = capsys.readouterr().out
        assert "已停止子代理 work" in out
        assert "已删除子代理 work" in out
        assert "work" not in realm.children
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_subagent_snapshot_shows_real_run_events(tmp_path: Path, capsys) -> None:
    session, _ = make_session(
        tmp_path, responses=["子代理结论：42"], enable_memory=False, enable_skills=False
    )
    await session.start()
    try:
        realm = session.agent.realms.record("subagents").instance
        await realm.create_child("worker", "回答 6x7")
        started = json.loads(await realm.invoke_child("worker", "开始"))
        assert started["ok"] is True
        assert await _wait_until(lambda: realm.children["worker"].status == "done")
        assert session.subagents is not None
        assert await _wait_until(
            lambda: any(
                event.type == "finalResult"
                for event in session.subagents.snapshot("worker", 500)
            )
        )
        await session.handle_command("/subagent worker 50")
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "子代理 worker" in out
    assert "启动" in out
    assert "回合完成" in out
    assert "最终结果" in out


@pytest.mark.asyncio
async def test_subagent_live_events_are_key_only(tmp_path: Path, capsys) -> None:
    session, _ = make_session(tmp_path, enable_memory=False, enable_skills=False)
    await session.start()
    try:
        session.busy = True  # 模拟父回合进行中
        realm = session.agent.realms.record("subagents").instance
        await realm.create_child("work", "p")
        realm._publish(  # noqa: SLF001 - 测试直接注入事件，省掉一次真实模型调用
            {"type": "delta", "agent_id": "subagent-work", "content": "SHOULD_NOT_PRINT"}
        )
        realm._publish(
            {
                "type": "tool_start",
                "agent_id": "subagent-work",
                "tool_name": "read",
                "args": {"path": "README.md"},
            }
        )
        assert session.subagents is not None
        assert await _wait_until(lambda: session.subagents.count("work") >= 3)
    finally:
        await session.close()
    out = capsys.readouterr().out
    assert "[sub:work]" in out
    assert "已创建" in out and "read" in out
    assert "SHOULD_NOT_PRINT" not in out  # 正文增量不实时混排


@pytest.mark.asyncio
async def test_run_cli_json_mode(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    code = await run_cli(
        ["--config", str(config), "-p", "你好", "--json", "--workspace", str(tmp_path / "ws")],
        model_factory=stub_factory(["json 模式回复"]),
        install_signal=False,
    )
    assert code == 0
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    streamed = "".join(str(e.get("content") or "") for e in events if e["type"] == "delta")
    assert "json 模式回复" in streamed


@pytest.mark.asyncio
async def test_run_cli_capability_flags(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    code = await run_cli(
        [
            "--config",
            str(config),
            "-p",
            "你好",
            "--json",
            "--workspace",
            str(tmp_path / "ws"),
            "--inbox",
            str(tmp_path / "inbox"),
            "--no-memory",
            "--skills-dir",
            str(EXAMPLE_SKILLS),
            "--memory-dir",
            str(tmp_path / "mem"),
        ],
        model_factory=stub_factory(["flag 回复"]),
        install_signal=False,
    )
    assert code == 0
    # 只关记忆：技能目录仍然生效，记忆库不该被创建
    assert (tmp_path / "inbox" / "skill.d").parent.is_dir()
    assert not (tmp_path / "mem").exists()
    out = capsys.readouterr().out
    assert any(json.loads(line)["type"] == "done" for line in out.splitlines() if line.startswith("{"))


@pytest.mark.asyncio
async def test_run_cli_tools_subset(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    code = await run_cli(
        [
            "--config",
            str(config),
            "-p",
            "你好",
            "--json",
            "--workspace",
            str(tmp_path / "ws"),
            "--inbox",
            str(tmp_path / "inbox"),
            "--tools",
            "read,find",
            "--no-memory",
            "--no-skills",
            "--no-todo",
            "--no-subagents",
        ],
        model_factory=stub_factory(["tools 回复"]),
        install_signal=False,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert any(json.loads(line)["type"] == "done" for line in out.splitlines() if line.startswith("{"))


@pytest.mark.asyncio
async def test_run_cli_rejects_conflicting_tool_flags(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    code = await run_cli(
        ["--config", str(config), "-p", "hi", "--tools", "read", "--no-core-tools"],
        model_factory=stub_factory(),
        install_signal=False,
    )
    assert code == 2
    assert "--tools 与 --no-core-tools" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_cli_missing_config_returns_2(tmp_path: Path, capsys) -> None:
    code = await run_cli(
        ["--config", str(tmp_path / "nope.json"), "-p", "hi"],
        install_signal=False,
    )
    assert code == 2
    assert "配置错误" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_cli_list_models(tmp_path: Path, capsys) -> None:
    config = write_config(tmp_path)
    code = await run_cli(["--config", str(config), "--list-models"], install_signal=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "stub::demo" in out and "stub::other" in out
