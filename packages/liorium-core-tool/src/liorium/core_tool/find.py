"""``find`` 工具：按 glob 找路径（只给路径，不含内容）。

语义（计划 §4.5 / vfs-output-mw §1.6）：

- VFS 用 ``vfs.find`` 取文件命中（复用其「不含 ``/`` 匹配 basename、含 ``/`` 匹配
  相对 root 的路径」语义），目录另外遍历一次 ``vfs.walk`` 收集——因为
  ``vfs.find`` 只返回文件，而模型需要看到目录；
- ``should_be_included_in_search=False`` 的投影节点一律跳过（realm 状态不是资源）；
- 真实文件用 ``Path.rglob``；
- **目录结果带 ``/`` 后缀**，一眼能区分文件和目录；
- 结果最多 ``max_results`` 条，超出追加截断提示。
"""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path

from langchain_core.tools import tool

from liorium.core.log import get_logger
from liorium.core.uri import normalize_vfs_path

from .runtime import ResolvedPath, ToolRuntime, vfs_display

logger = get_logger("core_tool.find")

_MAX_RESULTS_CAP = 1000


def _fs_display(path: Path, workspace_root: Path) -> str:
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _is_hidden(path: Path, root: Path) -> bool:
    """相对 ``root`` 的路径里出现以 ``.`` 开头的段（``.git`` / ``.venv`` 等）。

    与既有实现的 ``glob.glob`` 一致（它默认不匹配隐藏项）：否则在工作区里
    ``find("*.py")`` 会把整个虚拟环境翻出来。显式把隐藏目录当 root 时仍然可用。
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts)


def _format(matches: list[str], pattern: str, max_results: int) -> str:
    if not matches:
        return f"未找到匹配的文件或目录: {pattern}"
    text = "\n".join(matches[:max_results])
    if len(matches) > max_results:
        text += f"\n[已达到上限 {max_results} 项，结果被截断]"
    return text


async def _find_vfs(
    runtime: ToolRuntime, pattern: str, root: ResolvedPath, max_results: int
) -> list[str]:
    base = normalize_vfs_path(root.path)
    found: list[str] = []

    # 文件：交给 vfs.find（它已处理 basename / 相对路径两种匹配）
    file_paths = await runtime.vfs.find(pattern, root=base)
    for vfs_path in file_paths:
        node = await runtime.vfs.stat(vfs_path)
        if node is None or node.is_directory:
            continue
        if not node.should_be_included_in_search:
            continue  # 非 symbolic 的投影节点 vfs.find 不会过滤，这里补齐
        found.append(vfs_display(vfs_path, root.scheme))

    # 目录：vfs.find 不返回目录，单独遍历一次
    for vfs_path, node in await runtime.vfs.walk(base):
        if not node.is_directory:
            continue
        if vfs_path == base or vfs_path in ("", "/"):
            continue  # 不把搜索根本身当成结果
        if not node.should_be_included_in_search:
            continue
        relative = (
            vfs_path[len(base.rstrip("/")):].lstrip("/")
            if base != "/"
            else vfs_path.lstrip("/")
        )
        target = node.name if "/" not in pattern else relative
        if not fnmatch.fnmatch(target, pattern):
            continue
        found.append(vfs_display(vfs_path, root.scheme) + "/")

    return sorted(dict.fromkeys(found))


async def _find_fs(
    runtime: ToolRuntime, pattern: str, root: ResolvedPath, max_results: int
) -> list[str]:
    root_path = Path(root.path)
    if not await asyncio.to_thread(root_path.exists):
        return []

    if await asyncio.to_thread(root_path.is_file):
        candidates = [root_path] if fnmatch.fnmatch(root_path.name, pattern) else []
    else:
        try:
            candidates = await asyncio.to_thread(lambda: sorted(root_path.rglob(pattern)))
        except (ValueError, NotImplementedError):
            logger.warning("find 收到非相对 glob，已忽略: %s", pattern)
            return []
        except OSError as exc:  # pragma: no cover - 权限等环境问题
            logger.warning("find 遍历失败: %s", exc)
            return []

    matches: list[str] = []
    for candidate in candidates:
        if _is_hidden(candidate, root_path):
            continue
        display = _fs_display(candidate, runtime.workspace_root)
        if await asyncio.to_thread(candidate.is_dir):
            display += "/"
        matches.append(display)
        if len(matches) > max_results:
            break
    return sorted(dict.fromkeys(matches))


def make_find_tool(runtime: ToolRuntime):
    """构造绑定 ``runtime`` 的 ``find`` 工具。"""

    @tool
    async def find(pattern: str, root: str = "", max_results: int = 50) -> str:
        """按 glob 查找文件与目录（只返回路径，不含内容）。

        何时用：知道文件名/后缀但不知道位置（`*.toml`、`src/**/*.py`、`test_*`）；
        要找内容用 search，要看某个文件内容用 read。

        语义：
        - 递归匹配：`*.py` 等于所有层级的 `.py` 文件；`sub/*.md` 按相对路径匹配。
        - `root` 为空 = Agent 工作区；裸路径 = 真实文件系统；
          `<scheme>://` = 虚拟文件系统（例如 `agent://`、`skills://demo`）。
        - **目录结果带 `/` 后缀**；隐藏文件/目录默认跳过（避免把 `.venv` 整个翻出来），
          显式把隐藏目录当 root 时仍然可用。
        - realm 投影节点（`should_be_included_in_search=False`）不出现。
        - 最多返回 max_results 条，超出会在末尾提示截断。

        Args:
            pattern (str): glob 模式（`*`、`?`、`[abc]`，支持 `**` 递归）。
            root (str): 搜索根路径 / URI；省略 = 工作区。
            max_results (int): 最多返回条数，默认 50，上限 1000。
        """
        expression = str(pattern or "").strip()
        if not expression:
            return "错误: pattern 不能为空"

        try:
            limit = max(1, min(int(max_results or 50), _MAX_RESULTS_CAP))
        except (TypeError, ValueError):
            limit = 50
        resolved = runtime.resolve_path(str(root or "").strip())

        if resolved.is_vfs:
            if not runtime.vfs.exists(resolved.path):
                return f"[路径不存在: {resolved.display}]"
            matches = await _find_vfs(runtime, expression, resolved, limit)
        else:
            if not await asyncio.to_thread(Path(resolved.path).exists):
                return f"[路径不存在: {resolved.display}]"
            matches = await _find_fs(runtime, expression, resolved, limit)

        return _format(matches, expression, limit)

    return find


__all__ = ["make_find_tool"]
