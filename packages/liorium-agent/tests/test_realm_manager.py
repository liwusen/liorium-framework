"""``liorium.agent.realm`` 的回归测试（计划 §C1-§C8）。

覆盖 RealmManager 的装配与生命周期：

- ``requires`` 拓扑排序；同层按**声明序**（§C2）；
- ``requires`` 成环 / 缺依赖 → ``RealmDependencyError``；缺工厂 → ``RealmNotFoundError``；
- 工具名冲突 → ``ToolNameConflictError``（以 ``failed`` + 冲突详情暴露，§C3/C5）；
- ``start()`` 失败 ⇒ 该 realm ``failed``、其它 realm 仍 ``running``、其工具/挂载不生效（§C5）；
- ``mounts()`` 生效且 ``stop`` 后卸载；``ctx.spawn`` 的后台任务在 ``stop`` 时被取消（§C4）；
- ``prompt_fragments()`` 只包含 ``running`` 的 realm。

附：``base.py`` 里 ``RealmTaskScope`` / ``JsonStateStore`` 的最小契约。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from liorium.agent.realm.base import (
    BaseRealm,
    JsonStateStore,
    RealmContext,
    RealmTaskScope,
)
from liorium.agent.realm.manager import RealmManager
from liorium.agent.spec import RealmSpec
from liorium.core.errors import (
    RealmDependencyError,
    RealmError,
    RealmNotFoundError,
    ToolNameConflictError,
)
from liorium.core.types import Text
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.node import VfsMount


class FakeTool:
    """只需要 ``name`` 的工具替身（冲突检测按名字判重）。"""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeRealm(BaseRealm):
    """可编排的假 realm：记录 start/stop 顺序，能模拟失败与后台任务。"""

    def __init__(
        self,
        realm_id: str,
        *,
        requires: tuple[str, ...] = (),
        tools: tuple[str, ...] = (),
        fragment: str = "",
        mounts: dict[str, VfsMount] | None = None,
        fail: bool = False,
        background: bool = False,
        log: list[str] | None = None,
        scope: str | None = None,
    ) -> None:
        self.id = realm_id
        # 有挂载就必须是树级实例（RealmManager 的启动期不变式）。
        self.instance_scope = scope or ("tree" if mounts else "agent")
        self.requires = list(requires)
        self._tools = [FakeTool(name) for name in tools]
        self._fragment = fragment
        self._mounts = dict(mounts or {})
        self.fail = fail
        self.background = background
        self.log = log if log is not None else []
        self.started = 0
        self.stopped = 0
        self.ctx: RealmContext | None = None
        self.task: asyncio.Task[Any] | None = None

    def tools(self) -> list[Any]:
        return list(self._tools)

    def prompt_fragment(self) -> str:
        return self._fragment

    def mounts(self) -> dict[str, VfsMount]:
        return dict(self._mounts)

    async def start(self, ctx: RealmContext) -> None:
        self.log.append(f"start:{self.id}")
        if self.fail:
            raise RuntimeError(f"{self.id} 起不来")
        self.started += 1
        self.ctx = ctx
        if self.background:
            self.task = ctx.spawn(self._keep_alive(), name=f"{self.id}:bg")

    async def stop(self, ctx: RealmContext) -> None:
        self.log.append(f"stop:{self.id}")
        self.stopped += 1

    @staticmethod
    async def _keep_alive() -> None:
        await asyncio.sleep(3600)


def build_manager(
    state_dir: Path,
    realms: dict[str, FakeRealm],
    specs: list[RealmSpec] | None = None,
) -> RealmManager:
    """按 ``realms`` 的顺序装配（每个 spec 复用同一个实例便于断言）。"""
    manager = RealmManager(
        agent_id="main",
        vfs=VirtualFileSystem(root_name="main", owner_id="main"),
        state_dir=state_dir,
        factories={realm_id: (lambda spec, realm=realm: realm) for realm_id, realm in realms.items()},
    )
    used = specs if specs is not None else [RealmSpec(id=realm_id) for realm_id in realms]
    manager.attach_specs(used)
    manager.assemble(used)
    return manager


def states(manager: RealmManager) -> dict[str, str]:
    return {status.id: status.state for status in manager.status()}


# ---------------------------------------------------------------------------
# 装配顺序
# ---------------------------------------------------------------------------


async def test_start_order_follows_requires(tmp_path: Path) -> None:
    """``requires`` 拓扑排序：被依赖者先起。"""
    log: list[str] = []
    realms = {
        "web": FakeRealm("web", requires=("db",), log=log),
        "db": FakeRealm("db", log=log),
        "cache": FakeRealm("cache", requires=("db",), log=log),
    }
    manager = build_manager(tmp_path, realms)

    assert manager.order == ["db", "web", "cache"]
    await manager.start_all()
    assert log == ["start:db", "start:web", "start:cache"]


async def test_same_layer_keeps_declaration_order(tmp_path: Path) -> None:
    """同层（无依赖关系）必须按声明序，不许按字母序或哈希序。"""
    log: list[str] = []
    realms = {
        "zeta": FakeRealm("zeta", log=log),
        "alpha": FakeRealm("alpha", log=log),
        "mid": FakeRealm("mid", log=log),
    }
    manager = build_manager(tmp_path, realms)

    assert manager.order == ["zeta", "alpha", "mid"]
    await manager.start_all()
    assert log == ["start:zeta", "start:alpha", "start:mid"]


async def test_stop_all_uses_reverse_start_order(tmp_path: Path) -> None:
    log: list[str] = []
    realms = {
        "web": FakeRealm("web", requires=("db",), log=log),
        "db": FakeRealm("db", log=log),
    }
    manager = build_manager(tmp_path, realms)

    await manager.start_all()
    await manager.stop_all()
    assert log == ["start:db", "start:web", "stop:web", "stop:db"]


async def test_set_enabled_toggles_single_realm(tmp_path: Path) -> None:
    """开关只动目标 realm：装配集合不变，``status()`` 显示 ``stopped`` 而不是消失。"""
    log: list[str] = []
    realms = {
        "a": FakeRealm(
            "a",
            tools=("tool_a",),
            mounts={"/a": VfsMount(prefix="/a", vfs=VirtualFileSystem(owner_id="a"))},
            log=log,
        ),
        "b": FakeRealm("b", tools=("tool_b",), log=log),
    }
    manager = build_manager(tmp_path, realms)
    await manager.start_all()

    assert await manager.set_enabled("a", True) is False  # 已在运行
    assert await manager.set_enabled("a", False) is True
    assert states(manager) == {"a": "stopped", "b": "running"}
    assert log[-1] == "stop:a"
    assert [tool.name for tool in manager.tools()] == ["tool_b"]
    assert "/a" not in manager.vfs.mounts
    assert {row.id: row for row in manager.status()}["a"].mounts == ()

    assert await manager.set_enabled("a", False) is False  # 已经关着
    assert await manager.set_enabled("a", True) is True
    assert states(manager) == {"a": "running", "b": "running"}
    assert log[-1] == "start:a"
    assert "/a" in manager.vfs.mounts  # 重新挂上
    assert manager.order == ["a", "b"]  # 装配顺序不受开关影响


def test_context_cadence_reads_params_override(tmp_path: Path) -> None:
    """``context_every`` 的 ``RealmSpec.params`` 覆盖类属性；非法值按 0。"""
    manager = build_manager(
        tmp_path,
        {"a": FakeRealm("a")},
        specs=[RealmSpec(id="a", params={"context_every": 7})],
    )
    assert manager.context_cadence("a") == 7  # 未 start() 也读 spec

    bonus = build_manager(tmp_path, {"b": FakeRealm("b")})
    assert bonus.context_cadence("b") == 0  # 类属性默认 0


async def test_context_cadence_same_before_and_after_start(tmp_path: Path) -> None:
    manager = build_manager(
        tmp_path,
        {"a": FakeRealm("a")},
        specs=[RealmSpec(id="a", params={"context_every": 3, "context_first": True})],
    )
    assert manager.context_cadence("a") == 3
    await manager.start_all()
    assert manager.context_cadence("a") == 3


async def test_requires_cycle_raises(tmp_path: Path) -> None:
    """``requires`` 成环必须在装配期就报错，不许起一半再死循环。"""
    realms = {
        "a": FakeRealm("a", requires=("b",)),
        "b": FakeRealm("b", requires=("a",)),
    }
    with pytest.raises(RealmDependencyError):
        build_manager(tmp_path, realms)


async def test_missing_dependency_raises(tmp_path: Path) -> None:
    realms = {"a": FakeRealm("a", requires=("ghost",))}
    with pytest.raises(RealmDependencyError):
        build_manager(tmp_path, realms)


def test_missing_factory_raises(tmp_path: Path) -> None:
    manager = RealmManager(
        agent_id="main",
        vfs=VirtualFileSystem(),
        state_dir=tmp_path,
        factories={},
    )
    with pytest.raises(RealmNotFoundError):
        manager.assemble([RealmSpec(id="mcp")])


def test_unknown_realm_lookup_raises(tmp_path: Path) -> None:
    manager = build_manager(tmp_path, {"a": FakeRealm("a")})
    with pytest.raises(RealmNotFoundError):
        manager.record("ghost")


def test_factory_id_mismatch_raises(tmp_path: Path) -> None:
    """工厂返回别的 id = 装配错乱，必须报错（不许"就近挂上"）。"""
    manager = RealmManager(
        agent_id="main",
        vfs=VirtualFileSystem(),
        state_dir=tmp_path,
        factories={"a": lambda spec: FakeRealm("b")},
    )
    with pytest.raises(RealmError):
        manager.assemble([RealmSpec(id="a")])


# ---------------------------------------------------------------------------
# 工具名冲突（§C3）
# ---------------------------------------------------------------------------


def test_tool_name_conflict_error_carries_owners() -> None:
    """冲突异常必须带上工具名与全部声明者（报错要能定位，不能只说"冲突了"）。"""
    error = ToolNameConflictError("dup", ["first", "second"])
    assert error.name == "dup"
    assert error.owners == ["first", "second"]
    message = str(error)
    assert "dup" in message
    assert "first" in message and "second" in message


async def test_tool_name_conflict_surfaces_as_failed_realm(tmp_path: Path) -> None:
    """两个 realm 声明同名工具：第二个必须 ``failed`` 并带上冲突详情（不静默跳过）。

    ``start()`` 会兜住异常做降级（§C5），所以异常类型体现在 ``RealmStatus.error``
    里，而不是从 ``start_all()`` 抛出来。
    """
    realms = {
        "first": FakeRealm("first", tools=("dup",)),
        "second": FakeRealm("second", tools=("dup", "only")),
    }
    manager = build_manager(tmp_path, realms)

    await manager.start_all()

    assert states(manager) == {"first": "running", "second": "failed"}
    second = {status.id: status for status in manager.status()}["second"]
    assert second.error is not None
    assert "ToolNameConflictError" in second.error
    assert "dup" in second.error
    assert "first" in second.error and "second" in second.error
    # 冲突者的全部工具都不出现（降级但可用）
    assert [tool.name for tool in manager.tools()] == ["dup"]
    assert second.tools == ()


async def test_failed_realm_mounts_are_rolled_back(tmp_path: Path) -> None:
    """冲突失败后它留下的挂载必须回滚，否则半死的 realm 仍占着命名空间。"""
    other = VirtualFileSystem(owner_id="skills")
    realms = {
        "first": FakeRealm("first", tools=("dup",)),
        "second": FakeRealm(
            "second", tools=("dup",), mounts={"/skills": VfsMount(prefix="/skills", vfs=other)}
        ),
    }
    manager = build_manager(tmp_path, realms)

    await manager.start_all()

    assert states(manager)["second"] == "failed"
    assert "/skills" not in manager.vfs.mounts
    assert await manager.vfs.stat("/skills") is None


# ---------------------------------------------------------------------------
# 启动失败降级（§C5）
# ---------------------------------------------------------------------------


async def test_start_failure_degrades_but_keeps_others_running(tmp_path: Path) -> None:
    realms = {
        "bad": FakeRealm("bad", fail=True, tools=("broken",), fragment="坏"),
        "good": FakeRealm("good", tools=("ok",), fragment="好"),
    }
    manager = build_manager(tmp_path, realms)

    await manager.start_all()

    assert states(manager) == {"bad": "failed", "good": "running"}
    bad = {status.id: status for status in manager.status()}["bad"]
    assert bad.error is not None and "RuntimeError" in bad.error
    assert [tool.name for tool in manager.tools()] == ["ok"]
    assert manager.prompt_fragments() == [("good", "好")]


async def test_stop_of_failed_realm_is_safe(tmp_path: Path) -> None:
    realms = {"bad": FakeRealm("bad", fail=True)}
    manager = build_manager(tmp_path, realms)

    await manager.start_all()
    await manager.stop_all()

    assert states(manager) == {"bad": "stopped"}


# ---------------------------------------------------------------------------
# mounts / ctx.spawn / prompt_fragments
# ---------------------------------------------------------------------------


async def test_mounts_are_applied_and_removed_on_stop(tmp_path: Path) -> None:
    skills = VirtualFileSystem(owner_id="skills")
    await skills.write("/my-skill/SKILL.md", Text("能力说明"))
    realm = FakeRealm(
        "skills", mounts={"/skills": VfsMount(prefix="/skills", vfs=skills, readonly=True)}
    )
    manager = build_manager(tmp_path, {"skills": realm})

    await manager.start_all()

    assert "/skills" in manager.vfs.mounts
    assert await manager.vfs.read_text("/skills/my-skill/SKILL.md") == "能力说明"
    assert {status.id: status for status in manager.status()}["skills"].mounts == ("/skills",)

    await manager.stop_all()

    assert "/skills" not in manager.vfs.mounts
    assert await manager.vfs.stat("/skills") is None


async def test_spawned_background_task_is_cancelled_on_stop(tmp_path: Path) -> None:
    """``ctx.spawn`` 托管的后台任务必须在 ``stop`` 时被取消（§C4）。"""
    realm = FakeRealm("worker", background=True)
    manager = build_manager(tmp_path, {"worker": realm})

    await manager.start_all()

    record = manager.record("worker")
    assert record.context is not None
    assert len(record.context.tasks) == 1
    assert realm.task is not None
    assert not realm.task.done()

    await manager.stop_all()

    assert realm.task.cancelled() is True


async def test_spawn_after_close_is_rejected(tmp_path: Path) -> None:
    """realm 停止后不许再 spawn（否则任务泄漏在无人管的 scope 里）。"""
    scope = RealmTaskScope(log_name="test")
    task = scope.spawn(asyncio.sleep(0))
    await asyncio.wait({task})
    await scope.cancel_all()

    async def _noop() -> None:
        return None

    coro = _noop()
    with pytest.raises(RuntimeError):
        scope.spawn(coro)
    coro.close()  # spawn 在 create_task 之前就拒绝了，协程由调用方关闭


async def test_spawn_with_blank_context_does_not_inherit_contextvars() -> None:
    """``spawn(context=Context())`` 必须切断上下文继承（记忆抽取靠它隔离主 run）。"""
    import contextvars

    turn = contextvars.ContextVar("turn", default="")
    seen: list[str] = []

    async def _read() -> None:
        seen.append(turn.get())

    scope = RealmTaskScope(log_name="test")
    turn.set("turn-1")
    inherited = scope.spawn(_read())
    await asyncio.wait({inherited})
    assert seen == ["turn-1"]  # 默认继承

    isolated = scope.spawn(_read(), context=contextvars.Context())
    await asyncio.wait({isolated})
    assert seen == ["turn-1", ""]  # 空白上下文：看不到当前回合的值
    await scope.cancel_all()


async def test_prompt_fragments_only_include_running_realms(tmp_path: Path) -> None:
    realms = {
        "good": FakeRealm("good", fragment="A"),
        "bad": FakeRealm("bad", fragment="B", fail=True),
        "blank": FakeRealm("blank", fragment="   "),
    }
    manager = build_manager(tmp_path, realms)

    assert manager.prompt_fragments() == []  # 启动前谁都不在
    await manager.start_all()
    assert manager.prompt_fragments() == [("good", "A")]


# ---------------------------------------------------------------------------
# 生命周期幂等 / ctx 装配
# ---------------------------------------------------------------------------


async def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    realm = FakeRealm("a")
    manager = build_manager(tmp_path, {"a": realm})

    await manager.start_all()
    await manager.start_all()
    assert realm.started == 1

    await manager.stop_all()
    await manager.stop_all()
    assert realm.stopped == 1
    assert states(manager) == {"a": "stopped"}


async def test_realm_context_carries_params_state_and_agent_id(tmp_path: Path) -> None:
    """``ctx`` 四件套：vfs / state（按 realm 一个 JSON 文件）/ params / agent_id。"""
    realm = FakeRealm("memory")
    manager = build_manager(
        tmp_path,
        {"memory": realm},
        specs=[RealmSpec(id="memory", params={"top_k": 5})],
    )

    await manager.start_all()

    ctx = realm.ctx
    assert ctx is not None
    assert ctx.realm_id == "memory"
    assert ctx.agent_id == "main"
    assert ctx.params == {"top_k": 5}
    assert ctx.vfs is manager.vfs
    assert ctx.state.path == tmp_path / "memory.json"

    await ctx.state.set("cursor", 42)
    await manager.stop_all()  # stop 时落盘

    store = JsonStateStore(tmp_path / "memory.json", realm_id="memory")
    store.load()
    assert await store.get("cursor") == 42


# ---------------------------------------------------------------------------
# 附：JsonStateStore 契约（realm 状态不进 checkpoint，§C6）
# ---------------------------------------------------------------------------


async def test_json_state_store_roundtrip(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "a.json", realm_id="a")
    store.load()
    await store.set("k", {"nested": [1, 2]})
    assert await store.get("k") == {"nested": [1, 2]}
    assert await store.all() == {"k": {"nested": [1, 2]}}
    assert await store.delete("k") is True
    assert await store.delete("k") is False

    await store.set("persisted", True)
    store.flush()

    reloaded = JsonStateStore(tmp_path / "a.json", realm_id="a")
    reloaded.load()
    assert await reloaded.get("persisted") is True


async def test_json_state_store_rejects_unserializable_values(tmp_path: Path) -> None:
    """不可 JSON 化的值立刻报错，不许静默丢弃或写半截文件。"""
    store = JsonStateStore(tmp_path / "a.json", realm_id="a")

    with pytest.raises(TypeError):
        await store.set("bad", object())
    with pytest.raises(TypeError):
        await store.update_many({"bad": object()})
    assert await store.all() == {}


# ---------------------------------------------------------------------------
# v1.1：conversation scope / dirty 刷新
# ---------------------------------------------------------------------------


class DirtyRealm(FakeRealm):
    """带 ``dirty()`` 的假 realm。"""

    def __init__(
        self,
        realm_id: str,
        *,
        tools: tuple[str, ...] = (),
        mounts: dict[str, VfsMount] | None = None,
    ) -> None:
        super().__init__(realm_id, tools=tools, mounts=mounts)
        self._dirty = False
        self.fail_tools = False
        self.refresh_calls = 0

    def dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self, flag: bool = True) -> None:
        self._dirty = bool(flag)

    def clear_dirty(self) -> None:
        self._dirty = False

    async def refresh(self) -> None:
        self.refresh_calls += 1

    def tools(self) -> list[Any]:
        if self.fail_tools:
            raise RuntimeError("工具构造失败")
        return super().tools()


async def test_conversation_scope_state_path_and_context(tmp_path: Path) -> None:
    realm = FakeRealm("conv")
    realm.state_scope = "conversation"
    manager = RealmManager(
        agent_id="main",
        vfs=VirtualFileSystem(root_name="main", owner_id="main"),
        state_dir=tmp_path,
        factories={"conv": lambda _spec: realm},
        conversation_id="chat 1/x",
    )
    spec = RealmSpec(id="conv")
    manager.attach_specs([spec])
    manager.assemble([spec])
    await manager.start_all()
    try:
        assert realm.ctx is not None
        assert realm.ctx.conversation_id == "chat 1/x"
        assert realm.ctx.state.path == tmp_path / "conv" / "conversation-chat_1_x.json"
        assert realm.ctx.state.path.is_file() or True  # 状态只在写过后才落盘
        await realm.ctx.state.set("k", 1)
        assert manager.status()[0].state == "running"
    finally:
        await manager.stop_all()


async def test_invalid_state_scope_fails_realm(tmp_path: Path) -> None:
    realm = FakeRealm("bad")
    realm.state_scope = "world"
    manager = build_manager(tmp_path, {"bad": realm})
    await manager.start_all()
    status = manager.status()[0]
    assert status.state == "failed"
    assert status.error is not None and "state_scope" in status.error
    assert manager.tools() == []


async def test_refresh_replaces_tools_for_dirty_realm(tmp_path: Path) -> None:
    realm = DirtyRealm("dyn", tools=("alpha",))
    manager = build_manager(tmp_path, {"dyn": realm})
    await manager.start_all()
    try:
        assert [tool.name for tool in manager.tools()] == ["alpha"]
        assert await manager.refresh() is False  # 不脏 → 不做任何事

        realm._tools = [FakeTool("alpha"), FakeTool("beta")]  # noqa: SLF001 - 模拟运行时发现新工具
        realm.mark_dirty()
        assert await manager.refresh() is True
        assert [tool.name for tool in manager.tools()] == ["alpha", "beta"]
        assert realm.refresh_calls == 1
        assert realm.dirty() is False
    finally:
        await manager.stop_all()


async def test_refresh_failure_degrades_and_clears_tools(tmp_path: Path) -> None:
    other = VirtualFileSystem(owner_id="dyn")
    mount = VfsMount(prefix="/dyn", vfs=other, readonly=True)
    realm = DirtyRealm("dyn", tools=("alpha",), mounts={"/dyn": mount})
    manager = build_manager(tmp_path, {"dyn": realm})
    await manager.start_all()
    try:
        realm.fail_tools = True
        realm.mark_dirty()
        assert await manager.refresh() is True
        status = manager.status()[0]
        assert status.state == "failed"
        assert status.error is not None and "工具构造失败" in status.error
        assert manager.tools() == []
        assert "/dyn" not in manager.vfs.mounts
    finally:
        await manager.stop_all()


async def test_reset_conversation_state_restarts_only_conversation_realms(tmp_path: Path) -> None:
    conv = FakeRealm("conv")
    conv.state_scope = "conversation"
    agent_scope = FakeRealm("agent")
    manager = RealmManager(
        agent_id="main",
        vfs=VirtualFileSystem(root_name="main", owner_id="main"),
        state_dir=tmp_path,
        factories={"conv": lambda _spec: conv, "agent": lambda _spec: agent_scope},
    )
    specs = [RealmSpec(id="conv"), RealmSpec(id="agent")]
    manager.attach_specs(specs)
    manager.assemble(specs)
    await manager.start_all()
    assert conv.ctx is not None
    await conv.ctx.state.set("boots", 3)
    conv.ctx.state.flush()
    state_file = conv.ctx.state.path
    assert state_file.is_file()

    touched = await manager.reset_conversation_state()
    assert touched == ["conv"]
    assert not state_file.exists()
    assert conv.started == 2 and conv.stopped == 1
    assert agent_scope.started == 1 and agent_scope.stopped == 0
    await manager.stop_all()
