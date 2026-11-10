"""``liorium.realm_base``：契约 + 写作 API（``@realm_tool`` / ``save()`` / ``on_start``）。"""

from __future__ import annotations

from typing import Any

import pytest

from liorium.core.errors import RealmError
from liorium.realm_base import (
    BaseRealm,
    JsonStateStore,
    RealmContext,
    TurnInfo,
    build_realm_tools,
    realm_tool,
)
from liorium.vfs.fs import VirtualFileSystem


def make_ctx(tmp_path, realm_id: str = "counter") -> RealmContext:
    return RealmContext(
        realm_id=realm_id,
        vfs=VirtualFileSystem(owner_id=realm_id),
        state=JsonStateStore(tmp_path / "realm_state" / f"{realm_id}.json", realm_id=realm_id),
    )


class CounterRealm(BaseRealm):
    """最小写作样板：不写 ``__init__`` / ``start`` / ``stop`` / ``tools``。"""

    id = "counter"

    async def on_start(self) -> None:
        self.items: list[str] = list(await self.state.get("items", []))

    async def on_stop(self) -> None:
        await self.save(items=self.items)

    @realm_tool
    async def add(self, text: str) -> str:
        """加一条。"""
        self.items.append(text)
        await self.save(items=self.items)
        return f"ok:{text}"

    @realm_tool(name="count_all", description="显式描述优先于 docstring")
    def count(self) -> int:
        return len(self.items)


# ---------------------------------------------------------------------------
# 契约默认值
# ---------------------------------------------------------------------------


def test_base_realm_is_constructible_with_spec() -> None:
    class Spec:
        params = {"k": 1}

    realm = BaseRealm(Spec())
    assert realm.params == {"k": 1}
    assert realm.ctx is None
    assert BaseRealm().params == {}


def test_defaults_are_inert() -> None:
    realm = BaseRealm()
    assert realm.tools() == []
    assert realm.mounts() == {}
    assert realm.middleware() == []
    assert realm.skills() == []
    assert realm.dirty() is False
    assert realm.prompt_fragment() == ""
    # 第五拍：默认不参与（context_every=0 ⇒ 框架一次都不问）
    assert realm.context_every == 0
    assert realm.context_first is False
    assert realm.context_origins == ()
    assert realm.context_items(TurnInfo(index=1, id="a#1")) is None


def test_state_requires_start() -> None:
    with pytest.raises(RealmError, match="还没 start"):
        _ = BaseRealm().state


# ---------------------------------------------------------------------------
# 生命周期钩子
# ---------------------------------------------------------------------------


async def test_on_start_and_on_stop_are_called(tmp_path) -> None:
    realm = CounterRealm()
    ctx = make_ctx(tmp_path)
    await realm.start(ctx)
    assert realm.ctx is ctx
    assert realm.items == []
    realm.items.append("x")
    await realm.stop(ctx)
    assert realm.ctx is None
    # on_stop 里的 save() 已经落盘
    reloaded = JsonStateStore(ctx.state.path, realm_id="counter")
    reloaded.load()
    assert await reloaded.get("items") == ["x"]


async def test_override_start_without_super_still_works(tmp_path) -> None:
    """老写法（自己实现 start/stop、不调 super）必须照旧可用。"""

    class LegacyRealm(BaseRealm):
        id = "legacy"

        async def start(self, ctx) -> None:
            self.ctx = ctx

        async def stop(self, ctx) -> None:
            self.ctx = None

    realm = LegacyRealm()
    ctx = make_ctx(tmp_path, "legacy")
    await realm.start(ctx)
    assert realm.state is ctx.state
    await realm.stop(ctx)
    assert realm.ctx is None


# ---------------------------------------------------------------------------
# @realm_tool
# ---------------------------------------------------------------------------


def test_tools_are_collected_in_definition_order() -> None:
    tools = CounterRealm().tools()
    assert [tool.name for tool in tools] == ["add", "count_all"]


def test_tool_description_defaults_to_docstring() -> None:
    tools = {tool.name: tool for tool in CounterRealm().tools()}
    assert tools["add"].description == "加一条。"
    assert tools["count_all"].description == "显式描述优先于 docstring"


async def test_tools_are_bound_to_the_instance(tmp_path) -> None:
    realm = CounterRealm()
    await realm.start(make_ctx(tmp_path))
    tools = {tool.name: tool for tool in realm.tools()}
    assert await tools["add"].ainvoke({"text": "写周报"}) == "ok:写周报"
    assert await tools["count_all"].ainvoke({}) == 1
    assert await realm.state.get("items") == ["写周报"]


def test_subclass_override_replaces_parent_tool() -> None:
    class Child(CounterRealm):
        @realm_tool
        def add(self, text: str) -> str:
            """子类版本。"""
            return f"child:{text}"

    tools = {tool.name: tool for tool in Child().tools()}
    assert set(tools) == {"add", "count_all"}
    assert tools["add"].description == "子类版本。"


def test_custom_tools_override_is_respected() -> None:
    class Manual(CounterRealm):
        def tools(self) -> list[Any]:
            return ["hand-made"]

    assert Manual().tools() == ["hand-made"]


def test_build_realm_tools_empty_without_marks() -> None:
    assert build_realm_tools(BaseRealm()) == []


def test_realm_tool_accepts_kwargs() -> None:
    @realm_tool(return_direct=True)
    def ping() -> str:
        return "pong"

    from liorium.realm_base import marked_methods

    assert [name for name, _ in marked_methods(type("X", (), {"ping": ping}))] == ["ping"]


# ---------------------------------------------------------------------------
# save() / state
# ---------------------------------------------------------------------------


async def test_save_flushes_immediately(tmp_path) -> None:
    realm = CounterRealm()
    ctx = make_ctx(tmp_path)
    await realm.start(ctx)
    await realm.save(items=["a"], note="b")
    # 不调 stop / flush，直接读文件：save 已经落盘
    reloaded = JsonStateStore(ctx.state.path, realm_id="counter")
    reloaded.load()
    assert await reloaded.get("items") == ["a"]
    assert await reloaded.get("note") == "b"


async def test_save_without_values_just_flushes(tmp_path) -> None:
    realm = CounterRealm()
    ctx = make_ctx(tmp_path)
    await realm.start(ctx)
    await realm.state.set("k", 1)
    await realm.save()  # 只 flush，不写新键
    reloaded = JsonStateStore(ctx.state.path, realm_id="counter")
    reloaded.load()
    assert await reloaded.get("k") == 1


# ---------------------------------------------------------------------------
# 第五拍：推送侧 ctx.inject()
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def queue(self, items) -> None:
        self.batches.append(list(items))


async def test_inject_without_channel_raises(tmp_path) -> None:
    realm = CounterRealm()
    await realm.start(make_ctx(tmp_path))
    with pytest.raises(RealmError, match="没有 Context 通道"):
        realm.ctx.inject("后台发现了一条新事实")


async def test_inject_queues_item_with_default_source(tmp_path) -> None:
    realm = CounterRealm()
    ctx = make_ctx(tmp_path)
    sink = _Sink()
    ctx.context = sink
    await realm.start(ctx)

    realm.ctx.inject("新事实", meta={"k": "v"})
    assert len(sink.batches) == 1
    item = sink.batches[0][0]
    assert item.source == "realm:counter"
    assert item.text == "新事实"
    assert item.meta == {"k": "v"}

    # 空文本 / 空序列：不排队，也不静默吞掉错误用法
    realm.ctx.inject_items([])
    assert len(sink.batches) == 1
    with pytest.raises(RealmError, match="非空文本"):
        realm.ctx.inject("   ")

