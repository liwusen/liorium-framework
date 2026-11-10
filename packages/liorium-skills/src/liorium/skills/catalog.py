"""技能目录发现（``<skills_dir>/<slug>/SKILL.md``）。

纯文件系统视图，不 import 任何 Agent / VFS 类型：发现结果给 ``SkillsRealm``
建挂载树、算 prompt 技能清单用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import SKILL_FILE, read_skill_meta, summarize_skill_md

_HIDDEN_PREFIXES = ("_", ".")


def _within(root: Path, path: Path) -> bool:
    """``path`` 解析符号链接后是否仍在 ``root`` 内（Windows 用 normcase 比较）。"""
    try:
        root_resolved = root.resolve()
        path_resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    root_key = os.path.normcase(str(root_resolved))
    path_key = os.path.normcase(str(path_resolved))
    return path_key == root_key or path_key.startswith(root_key + os.sep)


def is_safe_skill_entry(root: Path, entry: Path) -> bool:
    """技能目录里的节点是否允许读取：不跟随符号链接、且解析后仍在根内。"""
    if entry.is_symlink():
        return False
    return _within(root, entry)


@dataclass(frozen=True)
class Skill:
    """一个已安装技能（只描述磁盘上的事实）。"""

    slug: str
    path: Path
    name: str
    description: str
    version: str

    @property
    def skill_md(self) -> Path:
        return self.path / SKILL_FILE

    @property
    def uri(self) -> str:
        return f"skills://{self.slug}/SKILL.md"

    def to_dict(self) -> dict[str, str]:
        return {
            "slug": self.slug,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "path": str(self.path),
            "skill_md": self.uri,
        }


def discover_skills(root: Path | str) -> list[Skill]:
    """扫描 ``root`` 下的技能目录；没有 ``SKILL.md`` 的目录跳过（不算技能）。"""
    base = Path(root)
    if not base.is_dir():
        return []
    out: list[Skill] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(_HIDDEN_PREFIXES):
            continue
        if not is_safe_skill_entry(base, entry):
            continue
        if not entry.is_dir():
            continue
        skill_md = entry / SKILL_FILE
        if skill_md.is_symlink() or not skill_md.is_file():
            continue
        if not _within(base, skill_md):
            continue
        meta = read_skill_meta(entry)
        slug = str(meta.get("slug") or entry.name).strip() or entry.name
        description = str(meta.get("description") or "").strip()
        if not description:
            description = summarize_skill_md(entry)
        out.append(
            Skill(
                slug=slug,
                path=entry,
                name=str(meta.get("name") or slug),
                description=description,
                version=str(meta.get("version") or "0.0.0"),
            )
        )
    return out


def catalog_signature(root: Path | str) -> tuple[tuple[str, int, int], ...]:
    """目录指纹：``(slug, SKILL.md mtime_ns, 目录 mtime_ns)`` 排序后的元组。

    realm 用它判断"技能被装了/卸了/改了"，比每次 full scan 便宜，也不会因为
    ``SKILL.md`` 内容变化而漏（内容改了 mtime 一定变）。
    """
    base = Path(root)
    if not base.is_dir():
        return ()
    items: list[tuple[str, int, int]] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(_HIDDEN_PREFIXES):
            continue
        if not is_safe_skill_entry(base, entry):
            continue
        if not entry.is_dir():
            continue
        skill_md = entry / SKILL_FILE
        if skill_md.is_symlink() or not skill_md.is_file():
            continue
        if not _within(base, skill_md):
            continue
        try:
            md_mtime = skill_md.stat().st_mtime_ns
        except OSError:
            continue
        try:
            dir_mtime = entry.stat().st_mtime_ns
        except OSError:
            dir_mtime = 0
        items.append((entry.name, md_mtime, dir_mtime))
    return tuple(items)


__all__ = ["Skill", "catalog_signature", "discover_skills", "is_safe_skill_entry"]
