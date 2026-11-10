"""``liorium.vfs`` 行为回归（计划 §D1-§D4 / §3.6 规则 6 / §4.5）。

覆盖：

- 读写往返、目录列举排序、缺失节点报 ``NodeNotFoundError``；
- 只读挂载写入 → ``VfsReadOnlyError``；挂载冲突 → ``MountConflictError``；
  A 挂 B 后 B 挂 A、以及挂到自己 → ``MountCycleError``；
- 穿透挂载的 ``walk`` / ``find`` / ``stat``；
- 投影节点（``should_be_included_in_search=False``）在 ``find`` 里被跳过；
- ``provider`` 动态内容（sync / async 各一例）；
- 并发读写冒烟（10 个协程写不同路径不丢数据）。

注意：本模块的用例全部在**正确语义**上断言。若实现有 bug，请修实现，不要改断言。
"""

from __future__ import annotations

import asyncio

import pytest

from liorium.core.errors import (
    MountConflictError,
    MountCycleError,
    NodeNotFoundError,
    VfsError,
    VfsReadOnlyError,
)
from liorium.core.types import Binary, Text, VfsContent
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.locks import AsyncRWLock
from liorium.vfs.node import VfsMount, VfsNode

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def new_vfs(name: str = "root") -> VirtualFileSystem:
    """建一棵独立的 VFS（``owner_id`` 只用于日志/环检测）。"""
    return VirtualFileSystem(root_name=name, owner_id=name)


def file_node(name: str, content: VfsContent, *, searchable: bool = True) -> VfsNode:
    return VfsNode(
        name=name,
        is_directory=False,
        content=content,
        should_be_included_in_search=searchable,
    )


def dir_node(name: str, *children: VfsNode) -> VfsNode:
    node = VfsNode(name=name, is_directory=True)
    for child in children:
        node.add_child(child)
    return node


# ---------------------------------------------------------------------------
# 读 / 写 / 列举
# ---------------------------------------------------------------------------


async def test_write_read_roundtrip() -> None:
    vfs = new_vfs()
    await vfs.write("/logs/a.txt", Text("第一行\n第二行"))

    assert await vfs.read_text("/logs/a.txt") == "第一行\n第二行"
    assert await vfs.read("/logs/a.txt") == Text("第一行\n第二行")
    assert vfs.exists("/logs/a.txt") is True


async def test_write_creates_intermediate_directories() -> None:
    vfs = new_vfs()
    await vfs.write("/a/b/c.txt", Text("x"))

    assert [node.name for node in await vfs.list("/")] == ["a"]
    assert [node.name for node in await vfs.list("/a")] == ["b"]
    assert [node.name for node in await vfs.list("/a/b")] == ["c.txt"]


async def test_write_overwrites_existing_content() -> None:
    vfs = new_vfs()
    await vfs.write("/a.txt", Text("一"))
    await vfs.write("/a.txt", Text("二"))

    assert await vfs.read_text("/a.txt") == "二"


async def test_read_missing_node_raises_node_not_found() -> None:
    vfs = new_vfs()

    with pytest.raises(NodeNotFoundError):
        await vfs.read("/missing.txt")
    with pytest.raises(NodeNotFoundError):
        await vfs.read_text("/missing.txt")
    with pytest.raises(NodeNotFoundError):
        vfs.resolve("/missing.txt")


async def test_stat_returns_none_for_missing_node() -> None:
    """``stat`` 是"探测"语义（返回 None），``resolve`` 是"必须存在"语义（抛错）。"""
    vfs = new_vfs()
    assert await vfs.stat("/missing.txt") is None
    assert vfs.exists("/missing.txt") is False

    await vfs.write("/a.txt", Text("x"))
    node = await vfs.stat("/a.txt")
    assert node is not None
    assert node.name == "a.txt"
    assert node.is_directory is False


async def test_read_directory_returns_none_and_read_text_raises() -> None:
    vfs = new_vfs()
    await vfs.write("/dir/a.txt", Text("x"))

    assert await vfs.read("/dir") is None
    with pytest.raises(VfsError):
        await vfs.read_text("/dir")


async def test_list_is_sorted_by_name() -> None:
    vfs = new_vfs()
    for name in ("z.txt", "a.txt", "m.txt"):
        await vfs.write(f"/{name}", Text(name))
    await vfs.mkdir("/b-dir")

    assert [node.name for node in await vfs.list("/")] == ["a.txt", "b-dir", "m.txt", "z.txt"]


async def test_list_missing_or_file_raises() -> None:
    vfs = new_vfs()
    await vfs.write("/a.txt", Text("x"))

    with pytest.raises(NodeNotFoundError):
        await vfs.list("/missing")
    with pytest.raises(VfsError):
        await vfs.list("/a.txt")


async def test_mkdir_is_idempotent_and_remove_deletes() -> None:
    vfs = new_vfs()
    await vfs.mkdir("/x/y")
    await vfs.mkdir("/x/y")
    await vfs.write("/x/y/a.txt", Text("x"))

    assert await vfs.remove("/x/y/a.txt") is True
    assert await vfs.remove("/x/y/a.txt") is False
    assert vfs.exists("/x/y/a.txt") is False


async def test_edit_replaces_first_occurrence() -> None:
    vfs = new_vfs()
    await vfs.write("/a.txt", Text("hello hello"))

    await vfs.edit("/a.txt", "hello", "hi")
    assert await vfs.read_text("/a.txt") == "hi hello"

    with pytest.raises(VfsError):
        await vfs.edit("/a.txt", "找不到的片段", "x")


async def test_edit_on_non_text_node_raises() -> None:
    vfs = new_vfs()
    await vfs.write("/b.bin", Binary(data=b"raw", mime="application/zip"))

    with pytest.raises(VfsError):
        await vfs.edit("/b.bin", "raw", "x")


async def test_path_escape_is_rejected_not_clamped() -> None:
    """``..`` 越界必须报错：静默夹紧会让挂载隔离形同虚设。"""
    vfs = new_vfs()

    with pytest.raises(VfsError):
        await vfs.read("/../outside.txt")
    with pytest.raises(VfsError):
        vfs.resolve("/skills/../../etc/passwd")
    with pytest.raises(VfsError):
        await vfs.write("/../evil.txt", Text("x"))


# ---------------------------------------------------------------------------
# 挂载：只读 / 冲突 / 环
# ---------------------------------------------------------------------------


async def test_readonly_mount_rejects_writes() -> None:
    parent = new_vfs("parent")
    skills = new_vfs("skills")
    await skills.write("/my-skill/SKILL.md", Text("原始内容"))
    parent.mount_vfs("/skills", skills, readonly=True)

    assert await parent.read_text("/skills/my-skill/SKILL.md") == "原始内容"

    with pytest.raises(VfsReadOnlyError):
        await parent.write("/skills/my-skill/SKILL.md", Text("篡改"))
    with pytest.raises(VfsReadOnlyError):
        await parent.edit("/skills/my-skill/SKILL.md", "原始", "篡改")

    # readonly 是"挂载侧"语义，源 VFS 自己仍可写；父侧读到新值
    await skills.write("/my-skill/SKILL.md", Text("源头更新"))
    assert await parent.read_text("/skills/my-skill/SKILL.md") == "源头更新"


async def test_readonly_mount_rejects_new_file() -> None:
    """往只读挂载点下**新建**文件也必须被拒绝（不是只拦覆盖已有文件）。"""
    parent = new_vfs("parent")
    skills = new_vfs("skills")
    parent.mount_vfs("/skills", skills, readonly=True)

    with pytest.raises(VfsReadOnlyError):
        await parent.write("/skills/new.md", Text("新文件"))


async def test_writable_mount_write_lands_in_mounted_vfs() -> None:
    """可写挂载上写新文件必须落到被挂载的 VFS，双方都能读到。"""
    parent = new_vfs("parent")
    work = new_vfs("work")
    parent.mount_vfs("/work", work, readonly=False)

    await parent.write("/work/a.txt", Text("a"))

    assert await parent.read_text("/work/a.txt") == "a"
    assert await work.read_text("/a.txt") == "a"


async def test_mount_point_behaves_like_directory() -> None:
    parent = new_vfs("parent")
    skills = new_vfs("skills")
    await skills.write("/b.md", Text("b"))
    await skills.write("/a.md", Text("a"))
    parent.mount_vfs("/skills", skills, readonly=True)

    assert [node.name for node in await parent.list("/skills")] == ["a.md", "b.md"]
    node = await parent.stat("/skills")
    assert node is not None
    assert node.is_directory is True


async def test_mount_conflict_with_existing_file() -> None:
    vfs = new_vfs()
    await vfs.write("/skills", Text("我是文件，不是目录"))

    with pytest.raises(MountConflictError):
        vfs.mount_vfs("/skills", new_vfs("skills"))


async def test_mount_conflict_with_existing_directory() -> None:
    vfs = new_vfs()
    await vfs.mkdir("/skills")

    with pytest.raises(MountConflictError):
        vfs.mount_vfs("/skills", new_vfs("skills"))


async def test_mount_conflict_with_existing_mount() -> None:
    vfs = new_vfs()
    vfs.mount_vfs("/skills", new_vfs("skills-a"))

    with pytest.raises(MountConflictError):
        vfs.mount_vfs("/skills", new_vfs("skills-b"))


async def test_mount_root_is_forbidden() -> None:
    vfs = new_vfs()

    with pytest.raises(MountConflictError):
        vfs.mount_vfs("/", new_vfs("other"))


async def test_mount_self_raises_cycle() -> None:
    vfs = new_vfs()

    with pytest.raises(MountCycleError):
        vfs.mount_vfs("/self", vfs)


async def test_mutual_mount_raises_cycle() -> None:
    """A 挂 B 之后 B 再挂 A ⇒ ``MountCycleError``（否则遍历会无限递归）。"""
    a = new_vfs("a")
    b = new_vfs("b")
    a.mount_vfs("/b", b, readonly=True)

    with pytest.raises(MountCycleError):
        b.mount_vfs("/a", a, readonly=True)


async def test_unmount_removes_mount_point() -> None:
    parent = new_vfs("parent")
    skills = new_vfs("skills")
    parent.mount_vfs("/skills", skills, readonly=True)

    assert "/skills" in parent.mounts
    assert parent.unmount("/skills") is True
    assert "/skills" not in parent.mounts
    assert await parent.stat("/skills") is None
    assert parent.unmount("/skills") is False
    # 反向索引也要解除，否则后续环检测会误判
    assert parent not in skills.mount_parents()


# ---------------------------------------------------------------------------
# 穿透挂载的遍历 / 查找 / stat
# ---------------------------------------------------------------------------


async def test_walk_pierces_mount() -> None:
    parent = new_vfs("parent")
    await parent.write("/local.txt", Text("local"))
    inner = new_vfs("inner")
    await inner.write("/deep/x.txt", Text("x"))
    parent.mount_vfs("/mnt", inner, readonly=True)

    paths = [path for path, _node in await parent.walk()]
    assert "/local.txt" in paths
    assert "/mnt" in paths
    assert "/mnt/deep/x.txt" in paths


async def test_find_pierces_mount() -> None:
    parent = new_vfs("parent")
    await parent.write("/local.txt", Text("local"))
    inner = new_vfs("inner")
    await inner.write("/deep/x.txt", Text("x"))
    parent.mount_vfs("/mnt", inner, readonly=True)

    assert await parent.find("*.txt") == ["/local.txt", "/mnt/deep/x.txt"]


async def test_read_and_stat_pierce_mount() -> None:
    parent = new_vfs("parent")
    inner = new_vfs("inner")
    await inner.write("/deep/x.txt", Text("穿透内容"))
    parent.mount_vfs("/mnt", inner, readonly=True)

    assert await parent.read_text("/mnt/deep/x.txt") == "穿透内容"
    node = await parent.stat("/mnt/deep/x.txt")
    assert node is not None
    assert node.name == "x.txt"


# ---------------------------------------------------------------------------
# 投影节点 / provider 动态内容
# ---------------------------------------------------------------------------


async def test_find_skips_projected_nodes() -> None:
    """realm 状态投影必须置 ``should_be_included_in_search=False``（计划 §C6）。"""
    vfs = new_vfs()
    await vfs.write("/visible.txt", Text("v"))
    vfs.mount_node(
        "/state",
        VfsNode(
            name="state",
            is_directory=False,
            provider=lambda node: Text("投影"),
            should_be_included_in_search=False,
        ),
        readonly=True,
    )

    assert await vfs.find("*") == ["/visible.txt"]
    assert await vfs.find("state") == []
    assert await vfs.find("state", include_projected=True) == ["/state"]


async def test_provider_node_returns_sync_content() -> None:
    vfs = new_vfs()
    vfs.mount_node(
        "/dynamic",
        VfsNode(name="dynamic", is_directory=False, provider=lambda node: Text(f"name={node.name}")),
        readonly=True,
    )

    assert await vfs.read_text("/dynamic") == "name=dynamic"


async def test_provider_node_returns_async_content() -> None:
    vfs = new_vfs()

    async def provider(node: VfsNode) -> VfsContent | None:
        await asyncio.sleep(0)
        return Text(f"async:{node.name}")

    vfs.mount_node(
        "/dynamic", VfsNode(name="dynamic", is_directory=False, provider=provider), readonly=True
    )

    assert await vfs.read_text("/dynamic") == "async:dynamic"


async def test_provider_content_is_computed_on_each_read() -> None:
    """provider 是"现算"：真相源变了，下次读就到新值（VFS 不缓存真相）。"""
    vfs = new_vfs()
    counter = {"n": 0}

    def provider(node: VfsNode) -> Text:
        counter["n"] += 1
        return Text(str(counter["n"]))

    vfs.mount_node(
        "/live", VfsNode(name="live", is_directory=False, provider=provider), readonly=True
    )

    assert await vfs.read_text("/live") == "1"
    assert await vfs.read_text("/live") == "2"


async def test_projection_node_rejects_direct_write() -> None:
    """投影节点不许直接覆盖（真相在 realm 自己的 store，计划 §C6）。"""
    vfs = new_vfs()
    vfs.mount_node(
        "/state",
        VfsNode(name="state", is_directory=False, provider=lambda node: Text("投影"), writable=True),
        readonly=False,
    )

    with pytest.raises(VfsError):
        await vfs.write("/state", Text("直接覆盖"))


# ---------------------------------------------------------------------------
# 并发冒烟
# ---------------------------------------------------------------------------


async def test_concurrent_writes_keep_every_record() -> None:
    """10 个协程写不同路径：一条都不许丢（写锁必须真的串行化）。"""

    vfs = new_vfs()

    async def write_one(index: int) -> None:
        await asyncio.sleep(0)
        await vfs.write(f"/concurrent/{index}.txt", Text(f"value-{index}"))

    await asyncio.gather(*(write_one(index) for index in range(10)))

    for index in range(10):
        assert await vfs.read_text(f"/concurrent/{index}.txt") == f"value-{index}"
    names = [node.name for node in await vfs.list("/concurrent")]
    assert names == sorted(names)
    assert len(names) == 10


async def test_concurrent_readers_and_writer_do_not_deadlock() -> None:
    vfs = new_vfs()
    await vfs.write("/shared.txt", Text("0"))

    async def reader() -> None:
        for _ in range(5):
            assert await vfs.read_text("/shared.txt") is not None

    async def writer() -> None:
        for index in range(5):
            await vfs.write("/shared.txt", Text(str(index)))

    await asyncio.wait_for(asyncio.gather(reader(), reader(), writer()), timeout=10)


async def test_rwlock_writer_waits_for_reader() -> None:
    lock = AsyncRWLock()
    await lock.acquire_read()

    async def writer() -> str:
        async with lock.write_lock():
            return "done"

    task = asyncio.create_task(writer())
    await asyncio.sleep(0)
    assert not task.done()  # 写者被读者挡住

    await lock.release_read()
    assert await asyncio.wait_for(task, timeout=2) == "done"


async def test_rwlock_readers_share() -> None:
    lock = AsyncRWLock()
    await lock.acquire_read()
    await asyncio.wait_for(lock.acquire_read(), timeout=2)  # 第二个读者不被阻塞
    await lock.release_read()
    await lock.release_read()


# ---------------------------------------------------------------------------
# VfsMount 描述对象
# ---------------------------------------------------------------------------


def test_vfs_mount_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        VfsMount(prefix="skills")
    with pytest.raises(ValueError):
        VfsMount(prefix="skills", vfs=new_vfs("a"), node=VfsNode(name="x"))


def test_vfs_mount_normalizes_prefix() -> None:
    mount = VfsMount(prefix="skills/", node=VfsNode(name="skills", is_directory=True))
    assert mount.prefix == "/skills"
    assert mount.readonly is True


async def test_mount_descriptor_is_applied() -> None:
    """``VirtualFileSystem.mount(VfsMount)`` 是 realm 装配的入口。"""
    parent = new_vfs("parent")
    node = dir_node("skills", file_node("SKILL.md", Text("能力说明")))
    parent.mount(VfsMount(prefix="/skills", node=node, readonly=True))

    assert await parent.read_text("/skills/SKILL.md") == "能力说明"

# ---------------------------------------------------------------------------
# 投影目录：目录节点上的 create_handler（v1.1，能力包用）
# ---------------------------------------------------------------------------


def _create_capture() -> tuple[dict, object]:
    created: dict[tuple[str, str], str] = {}

    def create(dir_node, relative: str, content) -> str:
        created[(str(dir_node.meta.get("path")), relative)] = getattr(content, "text", "")
        return "已创建"

    return created, create


async def test_create_handler_takes_new_paths() -> None:
    vfs = new_vfs("agent")
    created, create = _create_capture()
    node = VfsNode(
        name="memories", is_directory=True, create_handler=create, meta={"path": "/memories"}
    )
    vfs.mount_node("/memories", node, readonly=False)

    await vfs.write("/memories/a.md", Text("内容"))
    await vfs.write("/memories/x/y.md", Text("嵌套"))

    assert created == {("/memories", "a.md"): "内容", ("/memories", "x/y.md"): "嵌套"}
    # 没有在 VFS 里凭空建节点（投影目录不缓存真相）
    assert vfs._resolve("/memories/a.md") is None


async def test_create_handler_respects_readonly_mount() -> None:
    vfs = new_vfs("agent")
    _created, create = _create_capture()
    node = VfsNode(name="memories", is_directory=True, create_handler=create)
    vfs.mount_node("/memories", node, readonly=True)

    with pytest.raises(VfsReadOnlyError):
        await vfs.write("/memories/a.md", Text("x"))


async def test_create_handler_async_requires_write_async() -> None:
    vfs = new_vfs("agent")
    seen: list[str] = []

    async def create(_dir_node, relative: str, content) -> None:
        seen.append(relative)

    node = VfsNode(name="memories", is_directory=True, create_handler=create)
    vfs.mount_node("/memories", node, readonly=False)

    with pytest.raises(VfsError, match="create_handler 是异步的"):
        await vfs.write("/memories/a.md", Text("x"))
    await vfs.write_async("/memories/a.md", Text("x"))
    assert seen == ["a.md"]

# ---------------------------------------------------------------------------
# 懒加载投影：child_provider / children_provider（v1.2，memories:// 用）
# ---------------------------------------------------------------------------


def _parent_path(path: str) -> str:
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def _lazy_dir(path: str, *, child=None, children=None, create=None) -> VfsNode:
    name = path.rsplit("/", 1)[-1] or "memories"
    return VfsNode(
        name=name,
        is_directory=True,
        child_provider=child,
        children_provider=children,
        create_handler=create,
        meta={"path": path},
    )


def _lazy_tree(
    dirs: set[str], files: dict[str, str], *, created: list[tuple[str, str]] | None = None
):
    """按 ``dirs`` / ``files`` 现算的投影（模拟跑在 SQLite 上的真相源）。"""

    def create(dir_node, relative: str, content) -> str:
        if created is not None:
            created.append((str(dir_node.meta.get("path")), relative))
        return "已创建"

    def child(node, name: str):
        path = (str(node.meta.get("path") or "/").rstrip("/") or "") + "/" + name
        if path in dirs:
            return _lazy_dir(path, child=child, children=children, create=create)
        if path in files:
            return VfsNode(name=name, content=Text(files[path]), meta={"path": path})
        return None

    def children(node):
        base = str(node.meta.get("path") or "/").rstrip("/") or "/"
        out: list[VfsNode] = []
        for path in sorted(dirs):
            if path != base and _parent_path(path) == base:
                out.append(_lazy_dir(path, child=child, children=children, create=create))
        for path, text in sorted(files.items()):
            if _parent_path(path) == base:
                out.append(
                    VfsNode(name=path.rsplit("/", 1)[-1], content=Text(text), meta={"path": path})
                )
        return out

    return child, children


def _mounted_lazy_tree(created: list[tuple[str, str]] | None = None) -> VirtualFileSystem:
    dirs = {"/a", "/a/sub", "/c"}
    files = {"/a/b.md": "内容 b", "/a/sub/b.md": "内容 sub", "/c/d.md": "内容 d"}
    child, children = _lazy_tree(dirs, files, created=created)
    vfs = new_vfs("agent")
    vfs.mount_node(
        "/memories",
        _lazy_dir("/", child=child, children=children, create=None),
        readonly=False,
    )
    return vfs


async def test_child_provider_resolves_on_demand() -> None:
    vfs = _mounted_lazy_tree()
    # 两层都靠 provider 展开：树里没有预先建好的子节点（读完也不留缓存）
    assert vfs._mounts["/memories"].children == {}
    assert await vfs.read_text("/memories/a/b.md") == "内容 b"
    assert await vfs.read_text("/memories/a/sub/b.md") == "内容 sub"
    assert vfs._mounts["/memories"].children == {}
    node = await vfs.stat("/memories/a/sub")
    assert node is not None and node.is_directory
    with pytest.raises(NodeNotFoundError):
        await vfs.read("/memories/a/none.md")


async def test_child_provider_results_are_not_cached() -> None:
    dirs = {"/a"}
    files = {"/a/b.md": "内容 b"}
    child, children = _lazy_tree(dirs, files)
    vfs = new_vfs("agent")
    vfs.mount_node("/memories", _lazy_dir("/", child=child, children=children), readonly=False)

    assert await vfs.read_text("/memories/a/b.md") == "内容 b"
    files.pop("/a/b.md")  # 真相源被外部改了
    with pytest.raises(NodeNotFoundError):
        await vfs.read("/memories/a/b.md")


async def test_children_provider_lists_and_sorts() -> None:
    vfs = _mounted_lazy_tree()
    names = [node.name for node in await vfs.list("/memories/a")]
    assert names == ["b.md", "sub"]
    root_names = [node.name for node in await vfs.list("/memories")]
    assert root_names == ["a", "c"]


async def test_walk_and_find_see_lazy_nodes() -> None:
    vfs = _mounted_lazy_tree()
    walked = [path for path, _node in await vfs.walk("/memories")]
    assert "/memories/a/b.md" in walked
    assert "/memories/a/sub/b.md" in walked
    assert "/memories/c/d.md" in walked
    assert await vfs.find("*.md", root="/memories") == [
        "/memories/a/b.md",
        "/memories/a/sub/b.md",
        "/memories/c/d.md",
    ]


async def test_write_into_lazy_dir_uses_create_handler() -> None:
    created: list[tuple[str, str]] = []
    vfs = _mounted_lazy_tree(created)
    # 目录节点自己也带 create_handler：新的子文件落到最深已存在的投影目录上
    node = await vfs.stat("/memories/a")
    assert node is not None
    node.create_handler = lambda _dir, rel, _content: created.append(("/a", rel)) or "已创建"
    await vfs.write("/memories/a/new.md", Text("新内容"))
    assert created == [("/a", "new.md")]
    # 没有在 VFS 里凭空建节点
    assert vfs._resolve("/memories/a/new.md") is None


async def test_remove_projected_node_is_refused() -> None:
    vfs = _mounted_lazy_tree()
    with pytest.raises(VfsError, match="投影节点"):
        await vfs.remove("/memories/a/b.md")
    assert await vfs.remove("/memories/a/ghost.md") is False


async def test_async_child_provider_is_rejected() -> None:
    async def child(_node, _name):
        return None

    vfs = new_vfs("agent")
    vfs.mount_node("/memories", _lazy_dir("/", child=child), readonly=False)
    with pytest.raises(VfsError, match="child_provider 必须是同步函数"):
        await vfs.read("/memories/x")


def test_dump_tree_includes_lazy_children() -> None:
    vfs = _mounted_lazy_tree()
    dumped = vfs.dump_tree("/memories")
    assert "a/" in dumped and "b.md" in dumped
