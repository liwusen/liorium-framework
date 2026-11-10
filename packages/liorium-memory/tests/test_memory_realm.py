"""``MemoryRealm``：SQLite 投影挂载 + 读/写/编辑 + 检索 + dirty。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from liorium.agent.realm.base import JsonStateStore
from liorium.core.types import Image, Text
from liorium.memory import MemoryRealm, SqliteMemoryStore
from liorium.vfs import VirtualFileSystem


def make_ctx(tmp_path: Path):
    state = JsonStateStore(tmp_path / "realm_state" / "memory.json", realm_id="memory")
    state.load()
    return SimpleNamespace(state=state, inbox_dir=tmp_path, params={}, conversation_id="main", agent_id="test")


def mounted(store: SqliteMemoryStore, tmp_path: Path) -> tuple[MemoryRealm, VirtualFileSystem]:
    realm = MemoryRealm(store=store)
    parent = VirtualFileSystem(owner_id="agent")
    parent.mount(realm.mounts()["/memories"])
    return realm, parent


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


@pytest.mark.asyncio
async def test_start_projects_tree_and_state(tmp_path: Path, store: SqliteMemoryStore) -> None:
    store.write("/人物/爱丽丝.md", "喜欢猫。\n住在上海。", description="角色", tags=["人物"])
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(store=store)
    await realm.start(ctx)
    try:
        parent = VirtualFileSystem(owner_id="agent")
        parent.mount(realm.mounts()["/memories"])
        assert await parent.read_text("/memories/人物/爱丽丝.md") == "喜欢猫。\n住在上海。"
        dir_node = await parent.stat("/memories/人物")
        assert dir_node is not None and dir_node.is_directory
        assert await ctx.state.get("db") == str(store.path)
        assert await ctx.state.get("entries") == 1  # 只数文件节点（根/目录不计）
        assert await ctx.state.get("top_level") == 1  # 根下一层：人物目录
    finally:
        await realm.stop(ctx)


@pytest.mark.asyncio
async def test_prompt_fragment_and_tool(tmp_path: Path, store: SqliteMemoryStore) -> None:
    store.write("/n.md", "猫会打呼噜。")
    realm, _parent = mounted(store, tmp_path)
    assert "memories://" in realm.prompt_fragment()
    assert realm.skills() == []
    tools = realm.tools()
    assert [tool.name for tool in tools] == [
        "memory_search",
        "memory_list",
        "memory_delete",
        "memory_tag",
        "memory_score",
        "memory_graph",
        "memory_attach",
    ]

    payload = json.loads(await tools[0].ainvoke({"query": "打呼噜"}))
    assert payload[0]["path"] == "/n.md"
    assert payload[0]["uri"] == "memories://n.md"

    assert json.loads(await tools[0].ainvoke({"query": "  "})) == []


@pytest.mark.asyncio
async def test_write_new_file_through_parent_vfs(tmp_path: Path, store: SqliteMemoryStore) -> None:
    realm, parent = mounted(store, tmp_path)
    ack = await parent.write("/memories/notes/new.md", Text("新记忆"))
    assert ack == "已存入记忆 /notes/new.md"
    assert store.read("/notes/new.md").text == "新记忆"
    # 同一回合内立刻可读（增量挂树，不等重建）
    assert await parent.read_text("/memories/notes/new.md") == "新记忆"
    assert realm.dirty() is False


@pytest.mark.asyncio
async def test_write_and_edit_existing_file(tmp_path: Path, store: SqliteMemoryStore) -> None:
    store.write("/a.md", "原始内容")
    _realm, parent = mounted(store, tmp_path)
    await parent.write("/memories/a.md", Text("覆盖内容"))
    assert store.read("/a.md").text == "覆盖内容"

    await parent.edit("/memories/a.md", "覆盖", "编辑")
    assert store.read("/a.md").text == "编辑内容"
    node = await parent.stat("/memories/a.md")
    assert node is not None
    assert node.meta["line_count"] == 1


@pytest.mark.asyncio
async def test_binary_and_image_projection(tmp_path: Path, store: SqliteMemoryStore) -> None:
    (store.content_dir / "img").mkdir(parents=True, exist_ok=True)
    (store.content_dir / "img" / "cat.png").write_bytes(b"\x89PNG")
    store.write("/img/cat.png", "", index=False)
    with store._transaction() as conn:  # noqa: SLF001
        conn.execute("UPDATE nodes SET content_type='image/png' WHERE id='path:/img/cat.png'")

    _realm, parent = mounted(store, tmp_path)
    content = await parent.read("/memories/img/cat.png")
    assert isinstance(content, Image)
    assert content.mime == "image/png"


@pytest.mark.asyncio
async def test_dirty_tracks_external_writes(tmp_path: Path, store: SqliteMemoryStore) -> None:
    realm, _parent = mounted(store, tmp_path)
    assert realm.dirty() is False
    store.write("/external.md", "外部写入")
    assert realm.dirty() is True
    realm.clear_dirty()
    assert realm.dirty() is False
    realm.mark_dirty()
    assert realm.dirty() is True


@pytest.mark.asyncio
async def test_refresh_reprojects_external_writes(tmp_path: Path, store: SqliteMemoryStore) -> None:
    """外部进程写库后 ``refresh()`` 只要对账（懒投影每次 read 都问 store）。"""
    realm, parent = mounted(store, tmp_path)
    store.write("/external.md", "外部写入")
    assert realm.dirty() is True

    await realm.refresh()
    assert realm.dirty() is False

    # 投影是懒加载的：不需要重挂载就能看到外部写入
    assert await parent.read_text("/memories/external.md") == "外部写入"


@pytest.mark.asyncio
async def test_projection_is_lazy_and_paginated(tmp_path: Path, store: SqliteMemoryStore) -> None:
    """300 个文件：VFS 只投影一层且受 ``list_limit`` 限制，分页走 ``memory_list``。"""
    for index in range(300):
        store.write(f"/bulk/f{index:03d}.md", f"内容 {index}")
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(SimpleNamespace(params={"list_limit": 50}), store=store)
    await realm.start(ctx)
    try:
        parent = VirtualFileSystem(owner_id="agent")
        mount = realm.mounts()["/memories"]
        parent.mount(mount)
        # 挂载时没有预热整棵树
        assert mount.vfs.root.children == {}
        listed = await parent.list("/memories/bulk")
        assert len(listed) == 50
        # VFS 仍然能按需解析到未投影的深层文件
        assert await parent.read_text("/memories/bulk/f299.md") == "内容 299"

        nodes, total = realm.list_dir("/bulk", limit=10, offset=290)
        assert total == 300
        assert [node.name for node in nodes] == [f"f{i:03d}.md" for i in range(290, 300)]
    finally:
        await realm.stop(ctx)


@pytest.mark.asyncio
async def test_delete_tag_score_tools(tmp_path: Path, store: SqliteMemoryStore) -> None:
    store.write("/notes/a.md", "正文")
    store.write("/notes/b.md", "正文")
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(store=store)
    await realm.start(ctx)
    try:
        tools = {tool.name: tool for tool in realm.tools()}

        payload = json.loads(await tools["memory_delete"].ainvoke({"path": "/notes/a.md"}))
        assert payload["status"] == "ok" and payload["deleted"] is True
        assert store.node("/notes/a.md") is None

        # 目录必须显式 recursive
        error = json.loads(await tools["memory_delete"].ainvoke({"path": "/notes"}))
        assert error["status"] == "error" and "recursive" in error["error"]
        ok = json.loads(
            await tools["memory_delete"].ainvoke({"path": "/notes", "recursive": True})
        )
        assert ok["status"] == "ok" and store.node("/notes") is None

        store.write("/t.md", "标签")
        tagged = json.loads(
            await tools["memory_tag"].ainvoke({"path": "/t.md", "tags": "人物, 上海"})
        )
        assert tagged["tags"] == ["人物", "上海"]

        scored = json.loads(
            await tools["memory_score"].ainvoke({"path": "/t.md", "score_patch": "0.1"})
        )
        assert scored["score_patch"] == pytest.approx(0.1)
        bad = json.loads(
            await tools["memory_score"].ainvoke({"path": "/t.md", "score_patch": 1.0})
        )
        assert bad["status"] == "error"
    finally:
        await realm.stop(ctx)


@pytest.mark.asyncio
async def test_search_tool_return_modes(tmp_path: Path, store: SqliteMemoryStore) -> None:
    store.write("/n.md", "猫会打呼噜。")
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(store=store)
    await realm.start(ctx)
    try:
        tool = {tool.name: tool for tool in realm.tools()}["memory_search"]
        paths = json.loads(await tool.ainvoke({"query": "打呼噜", "return_mode": "paths"}))
        assert paths == ["/n.md"]
        compact = json.loads(await tool.ainvoke({"query": "打呼噜"}))
        assert compact[0]["line_count"] == 1
        snippets = json.loads(await tool.ainvoke({"query": "打呼噜", "return_mode": "snippets"}))
        assert "打呼噜" in snippets[0]["snippet"]
        bad = json.loads(await tool.ainvoke({"query": "x", "return_mode": "nope"}))
        assert bad["status"] == "error"
    finally:
        await realm.stop(ctx)


@pytest.mark.asyncio
async def test_stop_closes_owned_store(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm()
    await realm.start(ctx)
    db = tmp_path / "memory" / "memory.sqlite"
    assert db.is_file()
    await realm.stop(ctx)
    assert realm.store is None
