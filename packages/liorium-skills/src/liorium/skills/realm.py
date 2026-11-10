"""``SkillsRealm`` —— 技能能力包（``[skills]`` extra）。

计划依据：§C1（工具 + 挂载 + 状态 + ``prompt_fragment()``）、§F2（技能清单预载进
prompt）、§3.6（``/skills`` ⇔ ``skills://``）。

行为：

- **挂载树**：``<skills_dir>/<slug>/`` 原样投影到 ``/skills``，只读；目录节点的
  ``description`` 是技能说明（``read skills://`` 直接能看到）；
- **工具**：``list_skills``（计划里的 ``listSkills``）；
- **状态**：``state_scope="agent"``，记根目录与已发现技能（realm 状态不放 checkpoint）；
- **dirty**：技能目录的 mtime 指纹变了（装了 / 卸了 / 改了 SKILL.md）⇒ ``dirty()``，
  Agent 在回合边界重挂载 + 重取工具；技能清单进 prompt，所以 ``CacheEpoch`` 会 +1。

技能目录只读：安装/卸载是宿主的事（``liorium.skills.catalog`` 提供纯函数发现，
宿主的安装器可以直接复用）。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from liorium.core.log import get_logger
from liorium.core.types import Binary, Text, VfsContent
from liorium.vfs import VirtualFileSystem, VfsMount, VfsNode

from .catalog import Skill, catalog_signature, discover_skills, is_safe_skill_entry
from .frontmatter import SKILL_FILE

log = get_logger("liorium.skills.realm")

MOUNT_PREFIX = "/skills"
SCHEME = "skills"
SKILLS_DIR_NAME = "skill.d"
"""默认技能目录名（与既有实现的 ``agents/<name>/skill.d`` 布局对齐）。"""

_MAX_TREE_DEPTH = 12
_MAX_SKILL_FILE_BYTES = 256 * 1024
"""单个技能文件的读取上限：技能是说明书，不是大文件仓库；避免一次读爆上下文。"""


class SkillsRealm:
    """技能 realm：挂载树 + ``list_skills`` + prompt 技能清单。"""

    id = "skills"
    requires: tuple[str, ...] = ()
    state_scope = "agent"
    instance_scope = "tree"
    """整棵 Agent 树一份：``/skills`` 挂载是全树唯一的（只读投影，共享安全）。"""

    def __init__(self, spec: Any = None, *, skills_dir: str | Path | None = None) -> None:
        params = dict(getattr(spec, "params", {}) or {})
        raw_dir = skills_dir if skills_dir is not None else params.get("skills_dir")
        if raw_dir is not None and not isinstance(raw_dir, (str, Path)):
            raise TypeError(f"skills_dir 必须是 str/Path，得到 {type(raw_dir).__name__}")
        self._explicit_dir = Path(raw_dir).expanduser() if raw_dir is not None else None

        self.max_prompt_skills = int(params.get("max_prompt_skills", 20))
        if self.max_prompt_skills < 0:
            raise ValueError("max_prompt_skills 不能为负")

        self._root: Path | None = None
        self._ctx: Any = None
        self._catalog: list[Skill] = []
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._vfs: VirtualFileSystem | None = None
        self._tools: list[Any] | None = None
        self._forced = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, ctx: Any) -> None:
        self._ctx = ctx
        self._root = self._resolve_root(ctx)
        self._ensure_catalog(force=True)
        await ctx.state.update_many(
            {
                "root": str(self._root),
                "skills": [skill.slug for skill in self._catalog],
                "count": len(self._catalog),
            }
        )
        log.info("SkillsRealm 启动: root=%s skills=%d", self._root, len(self._catalog))

    async def stop(self, ctx: Any) -> None:
        self._ctx = None

    def _resolve_root(self, ctx: Any) -> Path:
        if self._explicit_dir is not None:
            root = self._explicit_dir
            inbox = getattr(ctx, "inbox_dir", None)
            if not root.is_absolute() and inbox is not None:
                root = Path(inbox) / root
            return root
        inbox = getattr(ctx, "inbox_dir", None)
        if inbox is not None:
            return Path(inbox) / SKILLS_DIR_NAME
        return Path(SKILLS_DIR_NAME)

    # ------------------------------------------------------------------
    # realm 协议
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        if self._tools is None:
            from .tools import build_list_skills_tool

            self._tools = [build_list_skills_tool(self)]
        return list(self._tools)

    def prompt_fragment(self) -> str:
        self._ensure_catalog()
        if not self._catalog:
            return ""
        return (
            "技能库（`skills://`，只读）：\n"
            "- `read skills://` 列出全部技能；`read skills://<slug>/SKILL.md` 读技能说明。\n"
            "- 需要某项能力前先读对应 SKILL.md，按里面的步骤/工具调用，不要凭技能名猜用法。\n"
            "- 安装/卸载技能由宿主负责（会触发缓存世代 +1）。"
        )

    def mounts(self) -> dict[str, VfsMount]:
        self._ensure_catalog()
        if self._vfs is None:
            self._vfs = self._build_vfs([])
        return {
            MOUNT_PREFIX: VfsMount(
                prefix=MOUNT_PREFIX,
                vfs=self._vfs,
                readonly=True,
                description=f"skills:{self._root or ''}",
            )
        }

    def middleware(self) -> list[Any]:
        """技能不追加中间件（能力面只有挂载树 + 清单）。"""
        return []

    def skills(self) -> list[str]:
        """预载进 system prompt「技能清单」段的条目（计划 §F2）。"""
        self._ensure_catalog()
        if not self._catalog:
            return []
        lines = []
        for skill in self._catalog[: self.max_prompt_skills]:
            description = skill.description or "（无说明）"
            lines.append(f"{skill.slug}：{description}（`skills://{skill.slug}/SKILL.md`）")
        remaining = len(self._catalog) - len(lines)
        if remaining > 0:
            lines.append(f"…另有 {remaining} 个技能，用 `list_skills` 查看")
        return lines

    def dirty(self) -> bool:
        if self._root is None:
            return False
        return self._forced or catalog_signature(self._root) != self._signature

    def mark_dirty(self, flag: bool = True) -> None:
        self._forced = bool(flag)

    def clear_dirty(self) -> None:
        self._forced = False
        if self._root is not None:
            self._signature = catalog_signature(self._root)

    # ------------------------------------------------------------------
    # 查询（工具用）
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self._root

    def list_skills(self) -> list[dict[str, str]]:
        self._ensure_catalog()
        return [skill.to_dict() for skill in self._catalog]

    def list_skills_json(self) -> str:
        return json.dumps({"skills": self.list_skills()}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 挂载树
    # ------------------------------------------------------------------

    def _ensure_catalog(self, *, force: bool = False) -> None:
        if self._root is None:
            return
        signature = catalog_signature(self._root)
        if not force and signature == self._signature:
            return
        self._signature = signature
        self._catalog = discover_skills(self._root)
        self._vfs = self._build_vfs(self._catalog)

    def _build_vfs(self, catalog: list[Skill]) -> VirtualFileSystem:
        fs = VirtualFileSystem(owner_id="skills")
        for skill in catalog:
            node = VfsNode(
                name=skill.slug,
                is_directory=True,
                description=skill.description,
                meta={"slug": skill.slug, "version": skill.version, "path": str(skill.path)},
            )
            fs.root.add_child(node)
            self._populate(node, skill.path, depth=0)
        return fs

    def _populate(self, parent: VfsNode, directory: Path, *, depth: int) -> None:
        if depth >= _MAX_TREE_DEPTH:
            log.warning("技能目录层级过深，已截断: %s", directory)
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            log.warning("技能目录读取失败: %s: %s", directory, exc)
            return
        root = self._root if self._root is not None else directory
        for entry in entries:
            if entry.name.startswith("."):
                continue
            # 只读挂载必须挡住符号链接穿透：链接节点与解析后越界的节点一律不投影。
            if not is_safe_skill_entry(root, entry):
                log.warning("跳过不安全的技能节点（符号链接/越界）: %s", entry)
                continue
            if entry.is_dir():
                node = VfsNode(name=entry.name, is_directory=True)
                parent.add_child(node)
                self._populate(node, entry, depth=depth + 1)
                continue
            if not entry.is_file():
                continue
            parent.add_child(
                VfsNode(
                    name=entry.name,
                    content=None,
                    provider=_file_provider(entry, root),
                    writable=False,
                    meta={"path": str(entry)},
                )
            )


def _file_provider(path: Path, root: Path):
    """按需读文件（技能文件可能被宿主的安装器替换，不能在建树时缓存）。"""

    def load(_node: VfsNode) -> VfsContent:
        # 建树后文件可能被替换成符号链接：读取前再校验一次（TOCTOU）。
        if path.is_symlink() or not is_safe_skill_entry(root, path):
            log.warning("拒绝读取不安全的技能文件: %s", path)
            return Text("")
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_SKILL_FILE_BYTES + 1)
        except OSError as exc:
            log.warning("技能文件读取失败: %s: %s", path, exc)
            return Text("")
        truncated = len(raw) > _MAX_SKILL_FILE_BYTES
        if truncated:
            raw = raw[:_MAX_SKILL_FILE_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return Binary(raw, mime=mime)
        if truncated:
            text += f"\n\n…（文件超过 {_MAX_SKILL_FILE_BYTES} 字节，已截断）"
        return Text(text)

    return load


__all__ = ["MOUNT_PREFIX", "SCHEME", "SKILLS_DIR_NAME", "SKILL_FILE", "SkillsRealm"]
