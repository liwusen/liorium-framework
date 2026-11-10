"""写作 API 端到端：只写 ``on_start`` + ``@realm_tool`` 的 realm 能装配、有工具、状态落盘。"""

from __future__ import annotations

from pathlib import Path

from liorium.agent import AgentSpec, BaseRealm, Limits, RealmSpec, LioriumAgent, realm_tool
from liorium.realm_base import BaseRealm as RealmBaseRealm
from liorium_testkit import make_scripted_factory


class TodoRealm(BaseRealm):
    """业务代码 15 行：没有 ``__init__`` / ``start`` / ``stop`` / ``tools``。"""

    id = "todo"

    async def on_start(self) -> None:
        self.items: list[str] = list(await self.state.get("items", []))

    @realm_tool
    async def todo_add(self, text: str) -> str:
        """加一条待办。"""
        self.items.append(text)
        await self.save(items=self.items)
        return f"已加入：{text}"

    def prompt_fragment(self) -> str:
        return "待办：用 `todo_add` 维护。"


def make_spec(workspace: Path) -> AgentSpec:
    return AgentSpec(
        id="kit",
        inbox_dir=workspace,
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="todo"),),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=workspace,
    )


def test_agent_reexports_realm_base(agent_workspace: Path) -> None:
    """``liorium.agent`` 的 re-export 必须和 ``liorium.realm_base`` 是同一个对象。"""
    assert BaseRealm is RealmBaseRealm
    assert realm_tool is not None


async def test_sugar_only_realm_assembles_and_persists(agent_workspace: Path) -> None:
    agent = LioriumAgent(
        spec=make_spec(agent_workspace),
        model_factory=make_scripted_factory(["好"]),
        # 类本身就是 (spec) -> Realm 工厂
        realm_factories={"todo": TodoRealm},
    )
    await agent.start()
    try:
        assert [tool.name for tool in agent.tools if tool.name == "todo_add"] == ["todo_add"]

        realm = agent.realms.record("todo").instance
        assert realm.ctx is not None
        tool = next(tool for tool in agent.tools if tool.name == "todo_add")
        assert await tool.ainvoke({"text": "写周报"}) == "已加入：写周报"

        # save() 立刻落盘：不用等 stop / flush
        state_file = agent_workspace / "realm_state" / "todo.json"
        assert state_file.is_file()
        assert "写周报" in state_file.read_text(encoding="utf-8")
        assert await realm.state.get("items") == ["写周报"]
    finally:
        await agent.close()


async def test_prompt_fragment_reaches_system_prompt(agent_workspace: Path) -> None:
    agent = LioriumAgent(
        spec=make_spec(agent_workspace),
        model_factory=make_scripted_factory(["好"]),
        realm_factories={"todo": TodoRealm},
    )
    await agent.start()
    try:
        prompt = agent._prompt.text  # noqa: SLF001 - 断言装配产物，测试内允许探针
        assert "todo_add" in prompt
    finally:
        await agent.close()
