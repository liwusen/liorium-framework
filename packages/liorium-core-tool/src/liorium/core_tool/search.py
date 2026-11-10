"""``search`` 工具：按正则搜索文本内容，输出 ``path:line: text``。

语义（计划 §4.5 / vfs-output-mw §1.6）：

- 只搜 **Text** 内容：VFS 里 ``content_to_text`` 返回 None 的节点（``Image`` /
  ``Binary`` / 投影节点）直接跳过；真实文件用 ``sniff.sniff_text`` 判类型，
  UTF-16 / GB18030 这类本地编码的文本照样能搜到，判不出来的才按二进制跳过；
- 行号风格与 ``read`` 一致：``行号:内容`` 的**绝对行号**（首行 1），
  这里组合成 ``path:line: text``，模型可以直接拿它去 ``read("path:N-M")``；
- 每个输入都先过 ``uri.parse``：裸路径 = 真实文件系统，``<scheme>://`` = VFS；
- 结果最多 ``max_results`` 条，命中超过上限时在末尾追加截断提示（不静默丢结果）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from langchain_core.tools import tool

from liorium.core.log import get_logger
from liorium.core.types import content_to_text
from liorium.core.uri import normalize_vfs_path

from .runtime import ResolvedPath, ToolRuntime, vfs_display
from .sniff import sniff_text

logger = get_logger("core_tool.search")

_MAX_RESULTS_CAP = 1000
_MAX_LINE_CHARS = 200
"""单行展示上限：命中行太长时截断，避免一行的压缩包/JSON 顶满上下文。"""


def _fs_display(path: Path, workspace_root: Path) -> str:
    """真实文件展示：工作区内用相对路径，否则绝对路径。"""
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return str(path)


def _is_hidden(path: Path, root: Path) -> bool:
    """工作区内的隐藏文件/目录（``.git`` 之类）一律跳过，避免搜到噪声。"""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in relative.parts)


def _hits_to_text(
    hits: list[str], pattern: str, max_results: int, *, noun: str
) -> str:
    if not hits:
        return f"未找到匹配: {pattern}"
    text = "\n".join(hits[:max_results])
    if len(hits) > max_results:
        text += f"\n[已达到上限 {max_results} {noun}，结果被截断]"
    return text


async def _collect_fs_hits(
    runtime: ToolRuntime, regex: re.Pattern[str], root: ResolvedPath, max_results: int
) -> list[str]:
    root_path = Path(root.path)
    if not await asyncio.to_thread(root_path.exists):
        return []

    if await asyncio.to_thread(root_path.is_file):
        files = [root_path]
    else:
        files = await asyncio.to_thread(
            lambda: sorted(p for p in root_path.rglob("*") if p.is_file())
        )

    hits: list[str] = []
    for file_path in files:
        if _is_hidden(file_path, root_path):
            continue
        try:
            raw = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:  # pragma: no cover - 权限/占用等环境问题
            logger.warning("search 读取失败，跳过 %s: %s", file_path, exc)
            continue
        if not raw:
            continue  # 空文件
        sniff = sniff_text(raw)
        if sniff.text is None:
            continue  # 二进制（编码嗅探判不出来；UTF-16 / GBK 文本仍会被搜到）
        text = sniff.text
        for line_no, line in enumerate(text.split("\n"), 1):
            if not regex.search(line):
                continue
            hits.append(
                f"{_fs_display(file_path, runtime.workspace_root)}:"
                f"{line_no}: {line.strip()[:_MAX_LINE_CHARS]}"
            )
            if len(hits) > max_results:
                return hits
    return hits


async def _collect_vfs_hits(
    runtime: ToolRuntime, regex: re.Pattern[str], root: ResolvedPath, max_results: int
) -> list[str]:
    entries = await runtime.vfs.walk(normalize_vfs_path(root.path))
    hits: list[str] = []
    for vfs_path, node in entries:
        if node.is_directory:
            continue
        # 投影节点（realm 状态）不参与搜索：双保险见计划 §4.5 / §C6
        if not node.should_be_included_in_search:
            continue
        content = await runtime.vfs.read(vfs_path)
        text = content_to_text(content) if content is not None else None
        if text is None:
            continue
        display = vfs_display(vfs_path, root.scheme)
        for line_no, line in enumerate(text.split("\n"), 1):
            if not regex.search(line):
                continue
            hits.append(f"{display}:{line_no}: {line.strip()[:_MAX_LINE_CHARS]}")
            if len(hits) > max_results:
                return hits
    return hits


def make_search_tool(runtime: ToolRuntime):
    """构造绑定 ``runtime`` 的 ``search`` 工具。"""

    @tool
    async def search(
        pattern: str,
        path: str = "",
        root: str = "",
        max_results: int = 50,
    ) -> str:
        """按正则搜索文件内容，返回 `路径:行号: 命中行`。

        何时用：知道要查什么字/正则，但不知道在哪个文件里（找函数定义、报错文本、
        配置项）。只知道文件名用 find；只想看某个已知文件用 read。

        语义：
        - `pattern` 是 Python 正则；要当普通字符串搜时先 re.escape（比如含 `.` `(` 时）。
        - `path`（别名 `root`）为空 = 搜 Agent 工作区；裸路径 = 真实文件系统；
          `<scheme>://` = 虚拟文件系统（例如 `agent://`、`skills://demo`）。
        - 只搜文本：二进制文件、图片节点、realm 投影节点都会跳过；隐藏文件/目录也跳过。
        - 行号是文件里的绝对行号，可直接拼成 `路径:行号-行号` 交给 read 看上下文。
        - 最多返回 max_results 条，超出会在末尾提示截断——缩小根目录或正则更精确。

        Args:
            pattern (str): 正则表达式。
            path (str): 搜索根路径 / URI；省略 = 工作区。
            root (str): `path` 的别名，二选一即可。
            max_results (int): 最多返回条数，默认 50，上限 1000。
        """
        expression = str(pattern or "").strip()
        if not expression:
            return "错误: 搜索模式不能为空"
        try:
            regex = re.compile(expression)
        except re.error as exc:
            return f"正则表达式错误: {exc}"

        try:
            limit = max(1, min(int(max_results or 50), _MAX_RESULTS_CAP))
        except (TypeError, ValueError):
            limit = 50
        target = str(path or root or "").strip()
        resolved = runtime.resolve_path(target)

        if resolved.is_vfs:
            if not runtime.vfs.exists(resolved.path):
                return f"[路径不存在: {resolved.display}]"
            hits = await _collect_vfs_hits(runtime, regex, resolved, limit)
        else:
            if not await asyncio.to_thread(Path(resolved.path).exists):
                return f"[路径不存在: {resolved.display}]"
            hits = await _collect_fs_hits(runtime, regex, resolved, limit)

        return _hits_to_text(hits, expression, limit, noun="条")

    return search


__all__ = ["make_search_tool"]
