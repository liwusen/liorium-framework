"""``ctx.model``：aux 模型端口（计划 C3/C4 / §8.2）。

规格：

- ``RealmContext.model`` 是 property，背后是**零参工厂**：每次访问都取当前模型；
- 独立使用时为 ``None``（realm 据此关闭抽取类功能）；
- Agent 注入的是 ``compact_model_factory``（不思考、无工具），``apply()`` 换模型后
  realm 不重启也能拿到新模型。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from liorium.agent import BaseRealm, LioriumAgent, RealmContext
from liorium.agent.realm.manager import RealmManager
from liorium.agent.spec import AgentSpec, RealmSpec
from liorium.vfs.fs import VirtualFileSystem
from liorium_testkit import ScriptedChatModel


def make_spec(tmp_path: Path, *, model: str = "P::m1", realms: tuple[RealmSpec, ...] = ()) -> AgentSpec:
    return AgentSpec(id="ctx-model", inbox_dir=tmp_path / "agent", model=model, realms=realms)


class EchoRealm(BaseRealm):
    """最小 realm：start 时把 ctx 记下来，便于断言 ``ctx.model``。"""

    id = "echo"

    def __init__(self, spec: Any = None) -> None:
        super().__init__(spec)
        self.contexts: list[RealmContext] = []

    async def start(self, ctx: RealmContext) -> None:
        await super().start(ctx)
        self.contexts.append(ctx)


def test_standalone_context_has_no_aux_model() -> None:
    ctx = RealmContext(realm_id="r", vfs=None, state=None)  # type: ignore[arg-type]
    assert ctx.model is None


def test_aux_model_is_refreshed_on_every_access() -> None:
    created: list[object] = []

    def factory() -> object:
        created.append(object())
        return created[-1]

    ctx = RealmContext(realm_id="r", vfs=None, state=None, model_factory=factory)  # type: ignore[arg-type]

    assert ctx.model is created[0]
    assert ctx.model is created[1]  # 不是快照，每次访问都问工厂


async def test_manager_injects_config_and_aux_factory(tmp_path: Path) -> None:
    realm = EchoRealm()
    manager = RealmManager(
        agent_id="a",
        vfs=VirtualFileSystem(owner_id="a"),
        state_dir=tmp_path / "realm_state",
        factories={"echo": lambda spec: realm},
    )
    aux = ScriptedChatModel(responses=["ok"])
    manager.configure(aux_model_factory=lambda: aux)
    manager.assemble([RealmSpec(id="echo", config={"k": 1})])

    await manager.start_all()

    ctx = realm.contexts[0]
    assert ctx.config == {"k": 1}
    assert ctx.params == {"k": 1}  # 旧名别名
    assert ctx.model is aux

    await manager.stop_all()


async def test_agent_uses_compact_factory_and_apply_refreshes_it(tmp_path: Path) -> None:
    aux_labels: list[str] = []

    def aux_factory(label: str) -> ScriptedChatModel:
        aux_labels.append(label)
        return ScriptedChatModel(responses=["ok"])

    realm = EchoRealm()
    spec = make_spec(tmp_path, realms=(RealmSpec(id="echo"),))
    agent = LioriumAgent(
        spec=spec,
        model_factory=lambda label: ScriptedChatModel(responses=["ok"]),
        compact_model_factory=aux_factory,
        realm_factories={"echo": lambda s: realm},
    )
    await agent.start()
    try:
        ctx = realm.contexts[0]
        before = ctx.model
        assert before is not None
        assert set(aux_labels) == {"P::m1"}  # 运行时构建 + ctx.model 都走 compact 工厂

        await agent.apply(replace(spec, model="P::m2"))

        after = ctx.model
        assert after is not before
        assert aux_labels[-1] == "P::m2"
    finally:
        await agent.close()
