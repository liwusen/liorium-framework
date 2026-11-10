"""``VirtualFileSystem`` —— 树状读模型。

计划依据：

- §D1 VFS 是读模型 / 操作界面，不是状态存储
- §D2 所有权单位是**血缘根**：独立顶层 Agent 各自一棵；Subagent 共享父的实例
- §D4 需要 ``mount_vfs(prefix, other, readonly)`` + **环检测**（记 owner 链）
- §3.6 规则 6 挂载前缀是保留目录，与已有文件/目录冲突时报错，不静默覆盖

线程/协程安全：所有遍历/写入走 ``AsyncRWLock``（写者优先）。
"""

from __future__ import annotations

import fnmatch
import inspect
from dataclasses import dataclass
from typing import Any, Iterator

from liorium.core.errors import (
    MountConflictError,
    MountCycleError,
    NodeNotFoundError,
    VfsError,
    VfsReadOnlyError,
)
from liorium.core.types import VfsContent, content_to_text
from liorium.core.uri import normalize_vfs_path

from .locks import AsyncRWLock
from .node import VfsMount, VfsNode, maybe_await, resolve_content


def _close_coroutine(value: Any) -> None:
    """同步路径拒绝 async handler 时把协程关掉（否则会有"从未 await"的警告）。"""
    close = getattr(value, "close", None)
    if callable(close):
        close()


@dataclass(frozen=True)
class ResolvedNode:
    """一次路径解析的结果。"""

    node: VfsNode
    path: str
    readonly: bool
    owner: "VirtualFileSystem"


_DELEGATED = object()
"""``_delegate_to_mount`` 的哨兵：没有跨挂载，调用方继续走本源逻辑。"""


class VirtualFileSystem:
    """一棵可挂载的虚拟文件系统。

    ``owner_id`` 是该血缘根的标识（日志/环检测/诊断用），不参与权限判断。
    """

    def __init__(self, *, root_name: str = "", owner_id: str = "") -> None:
        self.root = VfsNode(name=root_name, is_directory=True)
        self.owner_id = owner_id
        self._lock = AsyncRWLock()
        self._mounts: dict[str, VfsNode] = {}
        # 挂载反向索引：谁挂了我（用于环检测）。保存实例的弱引用语义不重要：
        # 挂载关系由 unmount 显式解除。
        self._parents: list["VirtualFileSystem"] = []

    # ------------------------------------------------------------------
    # 锁
    # ------------------------------------------------------------------

    @property
    def lock(self) -> AsyncRWLock:
        return self._lock

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parts(path: str) -> list[str]:
        norm = normalize_vfs_path(path)
        return [p for p in norm.split("/") if p]

    def _child(self, node: VfsNode, name: str) -> VfsNode | None:
        """取子节点：先查 ``children``，再问 ``child_provider``（懒加载投影）。

        不缓存 provider 的结果：投影的真相源可能在 VFS 之外被改（另一个进程写了库），
        缓存会让 ``read memories://`` 看到过期目录。每次问一次真相源，DB 有索引。
        """
        child = node.children.get(name)
        if child is not None:
            return child
        provider = node.child_provider
        if provider is None:
            return None
        found = provider(node, name)
        if inspect.isawaitable(found):
            _close_coroutine(found)
            raise VfsError("child_provider 必须是同步函数（懒加载投影不支持 async）")
        return found

    def _children_of(self, node: VfsNode) -> list[VfsNode]:
        """列出一层子节点：``children`` + ``children_provider`` 合并（同名以 ``children`` 为准）。"""
        provider = node.children_provider
        if provider is None:
            return [node.children[k] for k in sorted(node.children)]
        extra = provider(node)
        if inspect.isawaitable(extra):
            _close_coroutine(extra)
            raise VfsError("children_provider 必须是同步函数（懒加载投影不支持 async）")
        merged: dict[str, VfsNode] = dict(node.children)
        for item in extra or ():  # type: ignore[union-attr]
            merged.setdefault(item.name, item)
        return [merged[k] for k in sorted(merged)]

    def _resolve(self, path: str) -> ResolvedNode | None:
        """解析路径（穿透挂载点），不取锁。"""
        parts = self._parts(path)
        node = self.root
        owner: VirtualFileSystem = self
        readonly = False
        seen: set[int] = set()
        trail: list[str] = []
        for part in parts:
            guard = 0
            while node.mount is not None:
                if id(node) in seen:
                    raise MountCycleError(f"挂载环：{path!r}")
                seen.add(id(node))
                if node.readonly:
                    readonly = True
                owner = node.mount
                node = node.mount.root
                guard += 1
                if guard > 64:
                    raise MountCycleError(f"挂载链过深：{path!r}")
            nxt = self._child(node, part)
            if nxt is None:
                return None
            node = nxt
            trail.append(part)
        # 路径正好停在挂载点上：它自己仍可读（目录）
        return ResolvedNode(
            node=node,
            path="/" + "/".join(trail),
            readonly=readonly or node.readonly,
            owner=owner,
        )

    def resolve(self, path: str) -> ResolvedNode:
        """解析路径，缺失即抛 ``NodeNotFoundError``。"""
        found = self._resolve(path)
        if found is None:
            raise NodeNotFoundError(f"VFS 节点不存在: {path}")
        return found

    def exists(self, path: str) -> bool:
        return self._resolve(path) is not None

    # ------------------------------------------------------------------
    # 挂载
    # ------------------------------------------------------------------

    def mount_vfs(self, prefix: str, other: "VirtualFileSystem", *, readonly: bool = True) -> VfsNode:
        """把另一个 VFS 挂到 ``prefix``。

        实现即计划 §D4 的 ``mount_vfs(prefix, other_vfs, readonly)``：
        记 owner 链并做环检测（A 挂 B、B 挂 A 会无限递归）。
        """
        prefix = normalize_vfs_path(prefix)
        if prefix == "/":
            raise MountConflictError("不能把其它 VFS 挂到根 /（根属于本源生 Agent）")
        if other is self:
            raise MountCycleError(f"不能把 VFS 挂到自己: {prefix}")
        # 环检测：加边 self -> other 成环 ⇔ 已存在 other ->* self 的路径，
        # 即 self 的挂载祖先链里已经有 other（A 挂 B 后 B 再挂 A）。
        if self._is_reachable_from(self, other):
            raise MountCycleError(
                f"挂载成环：{prefix} 处的 VFS 已经是当前 VFS 的祖先"
            )

        parts = self._parts(prefix)
        name = parts[-1]
        parent = self._ensure_dir("/".join(parts[:-1]))
        existing = parent.children.get(name)
        if existing is not None:
            if existing.mount is not None:
                raise MountConflictError(f"挂载点已存在: {prefix}")
            raise MountConflictError(
                f"挂载前缀与已有{'目录' if existing.is_directory else '文件'}冲突: {prefix}"
            )

        node = VfsNode(
            name=name,
            is_directory=True,
            mount=other,
            readonly=readonly,
            description=f"mounted:{other.owner_id or 'vfs'}",
        )
        parent.add_child(node)
        self._mounts[prefix] = node
        other._parents.append(self)
        return node

    def mount_node(
        self,
        prefix: str,
        node: VfsNode,
        *,
        readonly: bool = True,
        description: str = "",
    ) -> VfsNode:
        """把单个节点（可含子树）挂到 ``prefix``。"""
        prefix = normalize_vfs_path(prefix)
        if prefix == "/":
            raise MountConflictError("不能覆盖根 /")
        parts = self._parts(prefix)
        name = parts[-1]
        parent = self._ensure_dir("/".join(parts[:-1]))
        if name in parent.children:
            raise MountConflictError(f"挂载前缀与已有节点冲突: {prefix}")
        node.name = name
        node.readonly = readonly or node.readonly
        if description:
            node.description = description
        parent.add_child(node)
        self._mounts[prefix] = node
        return node

    def mount(self, mount: VfsMount) -> VfsNode:
        """按 ``VfsMount`` 描述挂载（realm 装配走这里）。"""
        if mount.vfs is not None:
            return self.mount_vfs(mount.prefix, mount.vfs, readonly=mount.readonly)
        assert mount.node is not None
        return self.mount_node(
            mount.prefix, mount.node, readonly=mount.readonly, description=mount.description
        )

    def unmount(self, prefix: str) -> bool:
        prefix = normalize_vfs_path(prefix)
        parts = self._parts(prefix)
        parent = self._resolve("/".join(parts[:-1])) if len(parts) > 1 else None
        container = parent.node if parent else self.root
        node = container.children.get(parts[-1])
        if node is None or node.mount is None and prefix not in self._mounts:
            return False
        if node.mount is not None and self in node.mount._parents:
            node.mount._parents.remove(self)
        container.children.pop(parts[-1], None)
        self._mounts.pop(prefix, None)
        return True

    @property
    def mounts(self) -> dict[str, VfsNode]:
        """``prefix -> 挂载点节点`` 的快照。"""
        return dict(self._mounts)

    def mount_parents(self) -> list["VirtualFileSystem"]:
        """挂载到我的 VFS 列表（环检测用）。"""
        return list(self._parents)

    @staticmethod
    def _is_reachable_from(start: "VirtualFileSystem", target: "VirtualFileSystem") -> bool:
        """``target`` 是否在 ``start`` 的挂载祖先链上（含 start 自身）。"""
        seen: set[int] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur is target:
                return True
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            stack.extend(cur._parents)
        return False

    def _ensure_dir(self, path: str) -> VfsNode:
        node = self.root
        for part in self._parts(path):
            nxt = node.children.get(part)
            if nxt is None:
                nxt = VfsNode(name=part, is_directory=True)
                node.add_child(nxt)
            elif not nxt.is_directory:
                raise MountConflictError(f"路径中间是文件，不能当目录用: {path}")
            node = nxt
        return node

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def read(self, path: str) -> VfsContent | None:
        """读节点内容；目录返回 None（目录用 ``list``），缺失抛 ``NodeNotFoundError``。"""
        async with self._lock.read_lock():
            found = self.resolve(path)
            if found.node.is_directory:
                return None
            return await resolve_content(found.node)

    async def read_text(self, path: str) -> str:
        """读文本内容；非文本节点抛 ``VfsError``。"""
        content = await self.read(path)
        text = content_to_text(content) if content is not None else None
        if text is None:
            raise VfsError(f"{path} 不是文本节点")
        return text

    async def stat(self, path: str) -> VfsNode | None:
        async with self._lock.read_lock():
            found = self._resolve(path)
            return found.node if found else None

    async def list(self, path: str = "/") -> list[VfsNode]:
        """列目录（穿透挂载点），按名字排序。懒加载目录按需展开（不缓存）。"""
        async with self._lock.read_lock():
            found = self.resolve(path)
            node = found.node
            if node.mount is not None:
                node = node.mount.root
            if not node.is_directory:
                raise VfsError(f"{path} 不是目录")
            return self._children_of(node)

    async def walk(self, root: str = "/") -> list[tuple[str, VfsNode]]:
        """深度优先遍历（含挂载子树，跳过投影标记为不可搜索的节点不在此层处理）。"""
        async with self._lock.read_lock():
            out: list[tuple[str, VfsNode]] = []
            visited: set[int] = set()
            self._walk_into(self._parts_root(root), out, visited)
            return out

    def _parts_root(self, root: str) -> ResolvedNode:
        found = self._resolve(root)
        if found is None:
            raise NodeNotFoundError(f"VFS 节点不存在: {root}")
        return found

    def _walk_into(
        self, start: ResolvedNode, out: list[tuple[str, VfsNode]], visited: set[int]
    ) -> None:
        base_path = start.path.rstrip("/")
        stack: list[tuple[str, VfsNode]] = [(base_path, start.node)]
        while stack:
            path, node = stack.pop()
            if id(node) in visited:
                raise MountCycleError(f"遍历时发现挂载环: {path}")
            visited.add(id(node))
            if node.mount is not None:
                mounted = node.mount.root
                # 挂载点自身先入结果，再遍历被挂载树（其子节点路径前缀不变）
                out.append((path, node))
                visited.discard(id(node))
                visited.add(id(mounted))
                stack.extend(
                    reversed([(f"{path}/{item.name}", item) for item in self._children_of(mounted)])
                )
                continue
            out.append((path, node))
            if node.is_directory:
                for item in reversed(self._children_of(node)):
                    stack.append((f"{path}/{item.name}", item))

    async def find(
        self, pattern: str, root: str = "/", *, include_projected: bool = False
    ) -> list[str]:
        """按 glob 找路径。

        ``pattern`` 不含 ``/`` 时匹配 basename；含 ``/`` 时匹配相对 ``root`` 的路径。
        """
        matches: list[str] = []
        for path, node in await self.walk(root):
            if node.is_directory:
                continue
            if not include_projected and not node.should_be_included_in_search and node.is_symbolic:
                continue
            target = node.name if "/" not in pattern else path[len(root.rstrip("/")) :].lstrip("/")
            if fnmatch.fnmatch(target, pattern):
                matches.append(path)
        return sorted(matches)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def write(self, path: str, content: VfsContent) -> None:
        """写文件内容；缺失则创建（中间目录自动补齐，跨挂载时委托给被挂载的 VFS）。

        返回节点 handler（``write_handler`` / ``create_handler``）给出的 ACK 文本
        （没有 handler 或 handler 不返回字符串时返回 ``None``）。
        """
        async with self._lock.write_lock():
            result = await self._delegate_to_mount(path, "write", (content,))
            if result is not _DELEGATED:
                return result if isinstance(result, str) and result else None
            return self._write_locked(path, content)

    async def _delegate_to_mount(self, path: str, op: str, args: tuple[Any, ...]) -> Any:
        """如果路径跨入挂载点，把剩余路径委托给被挂载的 VFS（不改本树）。

        返回 ``_DELEGATED`` 的子类语义：跨挂载时返回被委托方法的返回值，
        否则返回模块级哨兵 ``_DELEGATED``，调用方继续走本源逻辑。
        """
        parts = self._parts(path)
        node = self.root
        for index, part in enumerate(parts):
            if node.mount is not None:
                if node.readonly:
                    raise VfsReadOnlyError(f"{path} 位于只读挂载 {node.name!r} 下")
                rest = "/".join(parts[index:])
                return await getattr(node.mount, op)(rest, *args)
            child = node.children.get(part)
            if child is None:
                return _DELEGATED
            node = child
        if node.mount is not None:
            # 路径正好停在挂载点上：目录操作交给被挂载的 VFS
            if node.readonly:
                raise VfsReadOnlyError(f"{path} 位于只读挂载 {node.name!r} 下")
            return await getattr(node.mount, op)("/", *args)
        return _DELEGATED

    def _write_locked(self, path: str, content: VfsContent) -> str | None:
        found = self._resolve(path)
        if found is not None:
            if found.node.is_directory:
                raise VfsError(f"{path} 是目录，不能写文件")
            if found.readonly or not found.node.writable:
                raise VfsReadOnlyError(f"{path} 是只读节点")
            if found.node.has_write_handler:
                result = found.node.write_handler(found.node, content)
                if inspect.isawaitable(result):
                    _close_coroutine(result)
                    raise VfsError("write_handler 是异步的，请使用 write_async")
                return result if isinstance(result, str) and result else None
            if found.node.has_edit_handler or found.node.provider is not None:
                raise VfsError(f"{path} 是投影节点，不能直接覆盖（请通过 realm 的能力/工具修改真相源）")
            found.node.content = content
            return None

        parts = self._parts(path)
        if not parts:
            raise VfsError("不能写根")
        # 投影目录（v1.1）：本树里还不存在的路径交给最深已存在目录的 create_handler，
        # 而不是在 VFS 里凭空建一个临时节点（那节点不会进真相源，重启即丢）。
        container, rest = self._deepest_existing(path)
        if container is not None and container.create_handler is not None:
            if container.readonly or not container.writable:
                raise VfsReadOnlyError(f"{path} 位于只读节点 {container.name!r} 下")
            result = container.create_handler(container, rest, content)
            if inspect.isawaitable(result):
                _close_coroutine(result)
                raise VfsError("create_handler 是异步的，请使用 write_async")
            return result if isinstance(result, str) and result else None

        parent = self._ensure_dir("/".join(parts[:-1]))
        node = VfsNode(name=parts[-1], is_directory=False, content=content)
        parent.add_child(node)
        return None

    def _deepest_existing(self, path: str) -> tuple[VfsNode | None, str]:
        """返回 ``(最深已存在的祖先节点, 剩余相对路径)``；根本身也算已存在。

        懒加载目录也参与"已存在"判定：写 ``memories://a/b/c.md`` 时，若 ``a``、``a/b``
        由 provider 提供，落到最深的那个目录节点上（它带 ``create_handler``）。
        """
        parts = self._parts(path)
        node = self.root
        index = 0
        for i, part in enumerate(parts):
            child = self._child(node, part)
            if child is None:
                break
            node = child
            index = i + 1
        return node, "/".join(parts[index:])

    async def write_async(self, path: str, content: VfsContent) -> str | None:
        """写文件内容，但允许节点上的 async handler；返回 handler 的 ACK。"""
        async with self._lock.write_lock():
            result = await self._delegate_to_mount(path, "write_async", (content,))
            if result is not _DELEGATED:
                return result if isinstance(result, str) and result else None
            found = self._resolve(path)
            if found is not None and found.node.has_write_handler and not found.node.is_directory:
                if found.readonly or not found.node.writable:
                    raise VfsReadOnlyError(f"{path} 是只读节点")
                ack = await maybe_await(found.node.write_handler(found.node, content))  # type: ignore[misc]
                return ack if isinstance(ack, str) and ack else None
            if found is None:
                container, rest = self._deepest_existing(path)
                if container is not None and container.create_handler is not None:
                    if container.readonly or not container.writable:
                        raise VfsReadOnlyError(f"{path} 位于只读节点 {container.name!r} 下")
                    ack = await maybe_await(container.create_handler(container, rest, content))  # type: ignore[misc]
                    return ack if isinstance(ack, str) and ack else None
            return self._write_locked(path, content)

    async def edit(self, path: str, old: str, new: str, **kwargs: Any) -> None:
        """按字符串替换编辑；投影节点走 ``edit_handler``。"""
        async with self._lock.write_lock():
            found = self.resolve(path)
            if found.node.is_directory:
                raise VfsError(f"{path} 是目录")
            if found.readonly or not found.node.writable:
                raise VfsReadOnlyError(f"{path} 是只读节点")
            if found.node.has_edit_handler:
                await maybe_await(found.node.edit_handler(found.node, old, {"new": new, **kwargs}))  # type: ignore[misc]
                return
            content = await resolve_content(found.node)
            if content is None:
                raise VfsError(f"{path} 没有内容")
            text = content_to_text(content)
            if text is None:
                raise VfsError(f"{path} 不是文本节点")
            if old not in text:
                raise VfsError(f"{path} 中找不到要替换的片段")
            found.node.content = type(content)(text.replace(old, new, 1))  # type: ignore[call-arg]

    async def mkdir(self, path: str) -> VfsNode:
        async with self._lock.write_lock():
            result = await self._delegate_to_mount(path, "mkdir", ())
            if result is not _DELEGATED:
                return result
            return self._ensure_dir(path)

    async def remove(self, path: str) -> bool:
        async with self._lock.write_lock():
            result = await self._delegate_to_mount(path, "remove", ())
            if result is not _DELEGATED:
                return bool(result)
            parts = self._parts(path)
            if not parts:
                raise VfsError("不能删除根")
            parent = self._resolve("/".join(parts[:-1]))
            container = parent.node if parent else self.root
            node = container.children.get(parts[-1])
            if node is None:
                if self._child(container, parts[-1]) is not None:
                    raise VfsError(
                        f"{path} 是投影节点，不能直接删除（请通过 realm 的能力/工具修改真相源）"
                    )
                return False
            if node.readonly:
                raise VfsReadOnlyError(f"{path} 是只读节点")
            if node.mount is not None and self in node.mount._parents:
                node.mount._parents.remove(self)
                self._mounts.pop(normalize_vfs_path(path), None)
            container.children.pop(parts[-1], None)
            return True

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def dump_tree(self, path: str = "/", max_depth: int = 3) -> str:
        """把树渲染成缩进文本（日志/调试）。"""
        found = self._resolve(path)
        if found is None:
            raise NodeNotFoundError(f"VFS 节点不存在: {path}")

        lines: list[str] = []

        def _render(node: VfsNode, prefix: str, depth: int) -> None:
            lines.append(prefix + node.name + ("/" if node.is_directory else ""))
            if depth >= max_depth or not node.is_directory:
                return
            target = node.mount.root if node.mount is not None else node
            for child in self._children_of(target):
                _render(child, prefix + "  ", depth + 1)

        _render(found.node, "", 0)
        return "\n".join(lines)

    def __iter__(self) -> Iterator[str]:
        return iter(self._mounts)


__all__ = ["ResolvedNode", "VirtualFileSystem"]
