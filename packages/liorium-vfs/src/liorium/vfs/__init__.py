"""liorium.vfs —— 虚拟文件系统（读模型 / 操作界面，不是状态存储）。

计划依据 §D1-§D4、§3.6、§4.5。实现见 ``liorium.vfs.fs``。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .fs import VirtualFileSystem
from .node import VfsMount, VfsNode

__all__ = ["VirtualFileSystem", "VfsMount", "VfsNode", "__version__"]
