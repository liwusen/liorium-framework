"""``SkillsRealm`` 的工具。

只有 ``list_skills``：技能的**读**走 VFS（``read skills://`` /
``read skills://<slug>/SKILL.md``），不需要额外工具；这里给的是"一次拿到
全部技能的结构化清单"，方便模型做发现（计划 §F2 的 ``listSkills``）。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from .realm import SkillsRealm


def build_list_skills_tool(realm: "SkillsRealm") -> Any:
    """构造 ``list_skills`` 工具（返回 JSON，便于模型稳定解析）。"""
    from langchain_core.tools import tool

    @tool("list_skills")
    async def list_skills() -> str:
        """列出当前可用技能的 slug、名称、版本、说明与 SKILL.md 路径。"""
        return realm.list_skills_json()

    return list_skills


__all__ = ["build_list_skills_tool"]
