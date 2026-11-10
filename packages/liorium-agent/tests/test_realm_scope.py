"""realm 实例作用域：树级实例「借用」、按 Agent 隔离、以及框架的强制不变式。

设计见 ``docs/design/realm-scope.md``：

- ``instance_scope="tree"``：整棵树一份实例（状态/挂载/连接唯一），子 Agent 只借工具；
- ``instance_scope="agent"``：每个 Agent 一份，状态落在各自 inbox 下；
- 不变式：``agent`` 级 realm **不许**返回挂载点（挂载点是全树共享的）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import tool

from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent
from liorium.core.errors import RealmError
from liorium.core.types import ContextItem
from liorium.core_tool_realm import CoreToolRealm
from liorium.realm_base import BaseRealm, TurnInfo
from liorium.todo_realm import TodoRealm
from liorium.vfs import VirtualFileSystem, VfsMount
from liorium_testkit import make_scripted_factory


class TreeRealm(BaseRealm):
    """树级能力包：有挂载 + 周期上下文 + 一个工具。"""

    id = "tree-x"
    instance_scope = "tree"
    context_every = 1
    context_first = True

    def __init__(self, spec: Any = None) -> None:
        super().__init__(spec)
        self._vfs = VirtualFileSystem(owner_id="tree-x")
        self.started = 0
        self.stopped = 0

        @tool("tree_tool")
        def tree_tool(text: str) -> str:
            """树级工具。"""
            return text

        self._tool = tree_tool

    async def on_start(self) -> None:
        self.started += 1

    async def on_stop(self) -> None:
        self.stopped += 1

    def tools(self) -> list[Any]:
        return [self._tool]

    def mounts(self) -> dict[str, VfsMount]:
        return {"/tree-x": VfsMount(prefix="/tree-x", vfs=self._vfs, readonly=True)}

    def prompt_fragment(self) -> str:
        return "tree-x 的说明"

    def context_items(self, turn: TurnInfo) -> ContextItem:
        return ContextItem(source="realm:tree-x", text="树级提醒")


class MountingAgentRealm(BaseRealm):
    """错误示范：agent 级却返回挂载点。"""

    id = "bad"

    def __init__(self, spec: Any = None) -> None:
        super().__init__(spec)
        self._vfs = VirtualFileSystem(owner_id="bad")

    def mounts(self) -> dict[str, VfsMount]:
        return {"/bad": VfsMount(prefix="/bad", vfs=self._vfs, readonly=True)}


def write_prompt(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENT.md").write_text("你是测试角色。", encoding="utf-8")
    return root


def make_spec(
    root: Path,
    *,
    agent_id: str,
    realms: tuple[RealmSpec, ...],
    toolsets: dict[str, tuple[str, ...]] | None = None,
) -> AgentSpec:
    write_prompt(root)
    return AgentSpec(
        id=agent_id,
        inbox_dir=root,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=realms,
        toolsets=toolsets or {},
        limits=Limits(context_window=8000, max_turns=4),
        workspace_root=root.parent,
    )


async def make_parent(tmp_path: Path) -> LioriumAgent:
    root = write_prompt(tmp_path / "root")
    spec = make_spec(
        root,
        agent_id="root",
        realms=(RealmSpec(id="tree-x"), RealmSpec(id="core"), RealmSpec(id="todo")),
    )
    agent = LioriumAgent(
        spec=spec,
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"tree-x": TreeRealm, "core": CoreToolRealm, "todo": TodoRealm},
    )
    await agent.start()
    return agent


async def make_child(parent: LioriumAgent, tmp_path: Path, *, toolsets: dict[str, tuple[str, ...]] | None = None) -> LioriumAgent:
    root = write_prompt(tmp_path / "child")
    spec = make_spec(
        root,
        agent_id="child",
        realms=parent.spec.realms,
        toolsets=toolsets or {"default": ("read", "find")},
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


# ---------------------------------------------------------------------------
# 不变式：agent 级不许有挂载
# ---------------------------------------------------------------------------


async def test_agent_scoped_realm_with_mounts_fails_loudly(tmp_path: Path) -> None:
    root = write_prompt(tmp_path / "a")
    spec = make_spec(root, agent_id="a", realms=(RealmSpec(id="bad"),))
    agent = LioriumAgent(
        spec=spec, model_factory=make_scripted_factory(["好"]), realm_factories={"bad": MountingAgentRealm}
    )
    await agent.start()
    try:
        record = agent.realms.record("bad")
        assert record.state == "failed"
        assert "instance_scope" in (record.error or "")
        assert "/bad" not in agent.vfs.mounts
    finally:
        await agent.close()


async def test_unknown_instance_scope_fails_loudly(tmp_path: Path) -> None:
    class WeirdRealm(TreeRealm):
        instance_scope = "galaxy"

    root = write_prompt(tmp_path / "w")
    spec = make_spec(root, agent_id="w", realms=(RealmSpec(id="tree-x"),))
    agent = LioriumAgent(
        spec=spec, model_factory=make_scripted_factory(["好"]), realm_factories={"tree-x": WeirdRealm}
    )
    await agent.start()
    try:
        record = agent.realms.record("tree-x")
        assert record.state == "failed"
        assert "instance_scope" in (record.error or "") and "非法" in (record.error or "")
    finally:
        await agent.close()


# ---------------------------------------------------------------------------
# 借用：树级 realm 只有一份实例
# ---------------------------------------------------------------------------


async def test_child_borrows_tree_realm_instead_of_restarting(tmp_path: Path) -> None:
    parent = await make_parent(tmp_path)
    child = await make_child(parent, tmp_path)
    try:
        tree = parent.realms.record("tree-x").instance
        assert child.realms.is_borrowed("tree-x") is True
        assert child.realms.record("tree-x").instance is tree
        assert tree.started == 1  # 子 Agent 没有再 start
        # 工具跟着借用过来
        assert "tree_tool" in {tool.name for tool in child.tools}
        # 挂载只做了一次（子 Agent 不重复挂）
        assert "/tree-x" in parent.vfs.mounts
        assert child.vfs is parent.vfs
        # status 标出 shared
        row = next(r for r in child.status().realms if r.id == "tree-x")
        assert row.shared is True and row.state == "running"
        # 提示词 / 周期上下文 / 中间件都不重复生效
        assert "tree-x" not in [rid for rid, _fragment in child.realms.prompt_fragments()]
        assert await child.realms.context_for_turn(TurnInfo(index=1, id="c#1")) == []
        assert await parent.realms.context_for_turn(TurnInfo(index=1, id="p#1")) != []
    finally:
        await child.close()
        await parent.close()


async def test_child_cannot_toggle_or_stop_borrowed_realm(tmp_path: Path) -> None:
    parent = await make_parent(tmp_path)
    child = await make_child(parent, tmp_path)
    try:
        with pytest.raises(RealmError, match="树级共享实例"):
            await child.realms.set_enabled("tree-x", False)
        with pytest.raises(RealmError, match="树级共享实例"):
            await child.realms.stop("tree-x")
        with pytest.raises(RealmError, match="树级共享实例"):
            await child.realms.restart("tree-x")
        # 非借用的 realm 照常可开关
        assert await child.realms.set_enabled("todo", False) is True
    finally:
        await child.close()
        await parent.close()


async def test_closing_child_does_not_stop_shared_realm(tmp_path: Path) -> None:
    parent = await make_parent(tmp_path)
    child = await make_child(parent, tmp_path)
    tree = parent.realms.record("tree-x").instance
    await child.close()
    try:
        assert tree.stopped == 0
        assert parent.realms.record("tree-x").state == "running"
        assert "/tree-x" in parent.vfs.mounts
    finally:
        await parent.close()


# ---------------------------------------------------------------------------
# 隔离：agent 级 realm 每个 Agent 一份
# ---------------------------------------------------------------------------


async def test_agent_scoped_realms_are_isolated_per_agent(tmp_path: Path) -> None:
    parent = await make_parent(tmp_path)
    child = await make_child(parent, tmp_path)
    try:
        parent_todo = parent.realms.record("todo").instance
        child_todo = child.realms.record("todo").instance
        assert child_todo is not parent_todo

        parent_tool = next(tool for tool in parent.tools if tool.name == "todo")
        child_tool = next(tool for tool in child.tools if tool.name == "todo")
        await parent_tool.ainvoke({"action": "add", "text": "父的待办"})
        await child_tool.ainvoke({"action": "add", "text": "子的待办"})

        parent_state = (parent.spec.inbox_dir / "realm_state" / "todo.json").read_text(encoding="utf-8")
        child_state = (child.spec.inbox_dir / "realm_state" / "todo.json").read_text(encoding="utf-8")
        assert "父的待办" in parent_state and "子的待办" not in parent_state
        assert "子的待办" in child_state and "父的待办" not in child_state
    finally:
        await child.close()
        await parent.close()


async def test_child_core_realm_honours_its_own_toolset(tmp_path: Path) -> None:
    parent = await make_parent(tmp_path)
    child = await make_child(parent, tmp_path)  # toolsets={"default": ("read", "find")}
    try:
        names = {tool.name for tool in child.tools}
        assert "read" in names and "find" in names
        assert "write" not in names and "execute" not in names
        # 父不受影响，仍是六个全给
        assert {"write", "execute"} <= {tool.name for tool in parent.tools}
    finally:
        await child.close()
        await parent.close()


# ---------------------------------------------------------------------------
# 子代理（真实链路）：继承父的 realm 声明，subagents 除外
# ---------------------------------------------------------------------------


async def test_subagent_inherits_realms_but_not_subagents(tmp_path: Path) -> None:
    root = write_prompt(tmp_path / "root")
    spec = make_spec(
        root,
        agent_id="root",
        realms=(
            RealmSpec(id="tree-x"),
            RealmSpec(id="core"),
            RealmSpec(id="todo"),
            RealmSpec(id="subagents"),
        ),
    )
    parent = LioriumAgent(
        spec=spec,
        model_factory=make_scripted_factory(["好"]),
        realm_factories={
            "tree-x": TreeRealm,
            "core": CoreToolRealm,
            "todo": TodoRealm,
        },
    )
    await parent.start()
    try:
        realm = parent.realms.record("subagents").instance
        await realm.create_child("a", "统计文件数", toolsets="read,find")
        record = realm.children["a"]
        child = await realm._ensure_agent(record)  # noqa: SLF001 - 直接验装配结果
        try:
            assert child.spec.realm_ids == ("tree-x", "core", "todo")
            assert child.realms.is_borrowed("tree-x") is True
            names = {tool.name for tool in child.tools}
            assert {"read", "find"} <= names and "write" not in names
            assert "newSubagent" not in names
            assert child.tree_root is parent
            assert parent.realms.record("todo").instance is not child.realms.record("todo").instance
        finally:
            await child.close()
    finally:
        await parent.close()
