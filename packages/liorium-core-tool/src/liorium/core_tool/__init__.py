"""liorium.core_tool —— 六个核心工具。

计划 §F1：``read / write / edit / search / find / execute``；工具集由 spec 决定，
registry 零全局过滤。工具只与 VFS 和 ``liorium.core`` 声明的端口打交道，
不 import ``liorium.agent``，不做权限判断。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .read import build_read_tool
from .runtime import ResolvedPath, ToolRuntime
from .tools import CORE_TOOL_NAMES, build_core_tools

__all__ = [
    "CORE_TOOL_NAMES",
    "ResolvedPath",
    "ToolRuntime",
    "__version__",
    "build_core_tools",
    "build_read_tool",
]
