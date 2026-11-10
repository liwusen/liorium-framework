"""VFS 节点模型。

设计约束（计划 §D1/§C6）：

- VFS 是**读模型 / 操作界面**，节点内容要么是显式 ``VfsContent``，
  要么由 ``provider`` 从真相源动态投影；
- realm 状态存自己的 store，VFS 只出**投影节点**（``should_be_included_in_search=False``），
  所以这个字段是一等公民；
- 内容类型是 ``liorium.core.types.VfsContent`` 的封闭三元联合，本包不认识图片格式，
  只搬运 ``Image`` / ``Binary``。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from liorium.core.types import VfsContent

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，避免循环导入
    from .fs import VirtualFileSystem

NodeProvider = Callable[["VfsNode"], "VfsContent | None | Awaitable[VfsContent | None]"]
"""动态内容提供者：读节点时现算，签名 ``(node) -> VfsContent | None``，可 async。"""

WriteHandler = Callable[["VfsNode", VfsContent], Any]
"""自定义写入：签名 ``(node, content) -> None``，支持 sync/async。存在时 VFS 不替换节点内容。"""

EditHandler = Callable[["VfsNode", str, dict[str, Any]], Any]
"""自定义编辑：签名 ``(node, old, kwargs) -> None``，支持 sync/async。"""

CreateHandler = Callable[["VfsNode", str, VfsContent], Any]
"""目录节点上的"新建文件"处理器（v1.1，能力包的投影目录用）。

签名 ``(dir_node, relative_path, content) -> Any``，其中 ``relative_path`` 是相对
该目录的路径（用 ``/`` 分隔，可包含中间目录）。存在时，写一个**本树里还不存在**
的路径会交给它（而不是在 VFS 里凭空建一个临时节点）；同步处理器返回的字符串会
作为 ACK 通道内容（``↳ ack``）。只读目录仍会拒绝写入。
"""

ChildProvider = Callable[["VfsNode", str], "VfsNode | None"]
"""目录节点上的"按名字取子节点"（v1.2，懒加载投影用）。

签名 ``(dir_node, child_name) -> VfsNode | None``，**必须是同步函数**（``_resolve``
是同步路径）；查不到返回 ``None``。结果不被缓存 —— 投影的真相源可能在外部变化，
每次解析都问一次（DB 有索引，代价可忽略）。
"""

ChildrenProvider = Callable[["VfsNode"], "Iterable[VfsNode]"]
"""目录节点上的"列出子节点"（v1.2，懒加载投影用）。

签名 ``(dir_node) -> Iterable[VfsNode]``，**必须同步**；返回的节点与 ``children``
里已有的按名字合并（``children`` 优先）。用于 ``list()`` / ``walk()`` / ``dump_tree()``。
返回的节点必须严格位于 ``dir_node`` 之下 —— 遍历只按挂载做环检测，不做投影环检测。
"""


@dataclass
class VfsNode:
    """树上的一个节点。

    目录节点用 ``is_directory=True`` + ``children``；文件节点用 ``content``（或
    ``provider`` / 读写处理器）。挂载点用 ``mount`` 指向另一个 VFS 的根。
    """

    name: str
    is_directory: bool = False
    content: VfsContent | None = None
    children: dict[str, "VfsNode"] = field(default_factory=dict)

    mount: "VirtualFileSystem | None" = None
    """目录节点上的挂载点。遍历时穿透到被挂载的 VFS。"""

    provider: NodeProvider | None = None
    """动态内容提供者。与 ``content`` 互斥（provider 优先）。"""

    write_handler: WriteHandler | None = None
    edit_handler: EditHandler | None = None
    create_handler: CreateHandler | None = None
    """目录节点：新建子文件时接管（v1.1，投影目录用）。见 ``CreateHandler``。"""

    child_provider: "ChildProvider | None" = None
    """目录节点：按名字取子节点（v1.2，懒加载投影用）。见 ``ChildProvider``。"""

    children_provider: "ChildrenProvider | None" = None
    """目录节点：列出全部子节点（v1.2，懒加载投影用）。见 ``ChildrenProvider``。"""

    should_be_included_in_search: bool = True
    """``search`` / ``find`` 是否包含此节点。realm 状态投影必须置 False（§C6）。"""

    writable: bool = True
    """节点自身是否可写（``write`` / ``edit`` 用）。"""

    readonly: bool = False
    """节点及其子树是否只读。挂载点用它表达"整个挂载只读"（§D4）。"""
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    # -- 便捷判断 -------------------------------------------------------

    @property
    def is_symbolic(self) -> bool:
        return self.provider is not None

    @property
    def has_write_handler(self) -> bool:
        return self.write_handler is not None

    @property
    def has_edit_handler(self) -> bool:
        return self.edit_handler is not None

    @property
    def has_create_handler(self) -> bool:
        return self.create_handler is not None

    @property
    def has_child_provider(self) -> bool:
        return self.child_provider is not None

    @property
    def has_children_provider(self) -> bool:
        return self.children_provider is not None

    @property
    def is_lazy(self) -> bool:
        """目录是否按需展开（懒加载投影）。"""
        return self.child_provider is not None or self.children_provider is not None

    @property
    def is_mount_point(self) -> bool:
        return self.mount is not None

    def child(self, name: str) -> "VfsNode | None":
        return self.children.get(name)

    def add_child(self, node: "VfsNode") -> "VfsNode":
        self.children[node.name] = node
        return node

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = "dir" if self.is_directory else "file"
        flags = ""
        if self.is_mount_point:
            flags += " mount"
        if self.is_symbolic:
            flags += " provider"
        return f"<VfsNode {self.name!r} {kind}{flags}>"


@dataclass(frozen=True)
class VfsMount:
    """realm 声明的挂载描述（``Realm.mounts()`` 的返回类型）。

    ``prefix`` 是与 scheme 同名的挂载点（计划 §3.6 规则 1）：``/skills`` ⇔ ``skills://``。
    """

    prefix: str
    vfs: "VirtualFileSystem | None" = None
    node: VfsNode | None = None
    readonly: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        prefix = self.prefix if self.prefix.startswith("/") else "/" + self.prefix
        object.__setattr__(self, "prefix", prefix.rstrip("/") or "/")
        if (self.vfs is None) == (self.node is None):
            raise ValueError("VfsMount 必须且只能提供 vfs 或 node 之一")


async def maybe_await(value: Any) -> Any:
    """``value`` 是 awaitable 就 await，否则原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def resolve_content(node: VfsNode) -> VfsContent | None:
    """取节点当前内容（provider 优先，支持 async）。"""
    if node.provider is not None:
        return await maybe_await(node.provider(node))
    return node.content


__all__ = [
    "ChildProvider",
    "ChildrenProvider",
    "CreateHandler",
    "EditHandler",
    "NodeProvider",
    "VfsMount",
    "VfsNode",
    "WriteHandler",
    "maybe_await",
    "resolve_content",
]
