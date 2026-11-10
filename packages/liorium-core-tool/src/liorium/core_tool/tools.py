"""按名字装配核心工具（``build_core_tools``）。

计划 §F1：工具集由 spec 决定，registry 零全局过滤。这里不做产品状态判断，
只做「按名字取工厂」这一件事；未知名字**报错**而不是静默跳过
（静默跳过等于"配了但没生效且无日志"，违背仓库约定）。

六个工具都在本包里（``read`` 的实现见 ``liorium.core_tool.read``），
导入失败=打包坏了，必须响亮失败。
"""

from __future__ import annotations

from typing import Sequence

from langchain_core.tools import BaseTool

from liorium.core.log import get_logger

from .edit import make_edit_tool
from .execute import make_execute_tool
from .find import make_find_tool
from .read import build_read_tool
from .runtime import ToolRuntime
from .search import make_search_tool
from .write import make_write_tool

logger = get_logger("core_tool.tools")

CORE_TOOL_NAMES = ("read", "write", "edit", "search", "find", "execute")
"""六个核心工具的名字（顺序固定）。"""

_TOOL_FACTORIES = {
    "read": build_read_tool,
    "write": make_write_tool,
    "edit": make_edit_tool,
    "execute": make_execute_tool,
    "search": make_search_tool,
    "find": make_find_tool,
}


def build_core_tools(
    runtime: ToolRuntime, *, names: Sequence[str] | None = None
) -> list[BaseTool]:
    """按名字构造工具列表。

    ``names=None`` → 全部六个核心工具；显式给 ``names`` 时按给定顺序构造，
    名字未知抛 ``ValueError``。
    """
    if names is None:
        wanted = list(CORE_TOOL_NAMES)
    else:
        wanted = []
        for raw in names:
            name = str(raw or "").strip()
            if name and name not in wanted:
                wanted.append(name)

    tools: list[BaseTool] = []
    for name in wanted:
        factory = _TOOL_FACTORIES.get(name)
        if factory is None:
            raise ValueError(
                f"未知的核心工具名: {name!r}（可用: {', '.join(CORE_TOOL_NAMES)}）"
            )
        tools.append(factory(runtime))
    return tools


__all__ = ["CORE_TOOL_NAMES", "build_core_tools"]
