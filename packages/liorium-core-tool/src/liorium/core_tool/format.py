"""``read`` 的纯格式化内核：只做字符串运算，无 IO / 无 VFS / 无全局状态。

冻结依据 ``docs/design/read-tool.md``（逐字模板，改一个空格都算破坏）：

- §4.2/§4.3 行选择器（clamp、反向交换、负数是"倒数第 N 行"）与两种行号格式
  —— 冻结 ``N:line``（无空格）vs 结构摘要 ``N: line``（有空格）；
- §5.2 结构摘要：装饰器 / docstring 跳过、关键字回退、footer 逐字
  （``用read(`` 故意缺一个空格）；
- §5.3 300 行截断：阈值按 ``split("\n")`` 的元素数（有结尾换行时实际只显示 299 行）；
- §6.1/§6.3 元数据：FS ``大小, N lines, MM/DD`` vs memory ``MM/DD, N lines, #tag…``
  （顺序相反，memory 只取前 3 个 tag）。

选择器的**唯一语法真源**是 ``liorium.core.uri.SELECTOR_RE`` / ``ParsedUri.selector_lines``，
本模块不复制正则，只借用解析结果。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Sequence

from liorium.core.uri import SCHEME_FILE, ParsedUri

DEFAULT_MAX_LINES = 300
"""``truncate_300`` 的默认阈值（``read-tool.md`` §5.3）。"""

SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".ts",
        ".js",
        ".rs",
        ".go",
        ".java",
        ".cpp",
        ".c",
        ".h",
        ".jsx",
        ".tsx",
        ".vue",
        ".rb",
        ".swift",
    }
)
"""结构摘要与"统计行数"共用的源码扩展名白名单（§5.2/§6.1）。"""

SUMMARY_PREFIXES = (
    "def ",
    "class ",
    "async def ",
    "import ",
    "from ",
    "const ",
    "let ",
    "function ",
    "export ",
)
"""行首命中即入选摘要的前缀元组（顺序与既有实现一致）。"""

SUMMARY_KEYWORDS = (
    "def",
    "class",
    "async def",
    "import",
    "from",
    "const",
    "let",
    "function",
    "export",
    "include",
    "require",
    "public",
    "private",
    "protected",
    "interface",
    "struct",
    "enum",
)
"""行首前缀不命中时的子串回退关键字（§5.2 第⑥步）。"""

STRUCTURE_FOOTER = (
    '\n[文件 {path}: {total} 行, 显示结构摘要。用 read("{path}:N-M") 查看具体行范围'
    '或用read("{path}:raw")关闭结构摘要]'
)
"""结构摘要 footer：**逐字**，``用read(`` 缺一个空格是冻结行为（§5.2/§7）。"""


# ---------------------------------------------------------------------------
# 行选择器
# ---------------------------------------------------------------------------


def selector_lines(selector: str | None) -> tuple[int, int] | None:
    """选择器字符串 → 1-indexed 闭区间；``:raw`` 单独使用 → ``None``。

    解析逻辑完全委托 ``ParsedUri.selector_lines``（唯一语法真源），本函数只是
    让调用方不必先造 URI。``:-5+3`` 这类未定义写法会让 ``int()`` 抛 ``ValueError``
    —— **故意保留**现状（``read-tool.md`` §3.6-1 `[bug]`），由测试锁死。
    """
    if not selector:
        return None
    return ParsedUri(raw="", scheme=SCHEME_FILE, path="", selector=selector).selector_lines


def format_with_line_numbers(
    text: str,
    *,
    start_line: int = 1,
    spaced: bool = False,
) -> str:
    """给每行加绝对行号。

    - ``spaced=False`` → 冻结格式 ``N:line``（行选择器路径，§4.3）；
    - ``spaced=True`` → 结构摘要格式 ``N: line``（§5.2）。

    行号始终是**文件绝对行号**（首行 = 1），与选择器怎么写无关。
    """
    separator = ": " if spaced else ":"
    return "\n".join(
        f"{start_line + index}{separator}{line}"
        for index, line in enumerate(text.split("\n"))
    )


def apply_selector(
    text: str,
    selector: str | None,
    *,
    show_line_number: bool = False,
) -> str:
    """应用行选择器（``read-tool.md`` §4.2 的等价实现）。

    - 无行选择器（含 ``:raw``）：``show_line_number`` 打开时给全文编号，否则原样返回；
      空文本直接返回 ``""``（不编号）。
    - 负数 = 倒数第 N 行（``len(lines) + n + 1``）。
    - 两端 clamp 到 ``[1, len(lines)]``；``s > e`` 时自动交换（``:10-3`` → 3~10 行）。
    - ``split("\\n")`` 驱动 ⇒ 结尾换行会多出一个空元素，``:-1`` 拿到的是它（冻结行为）。
    """
    lines = text.split("\n")
    resolved = selector_lines(selector)

    if not resolved:
        if not text or not show_line_number:
            return text
        return format_with_line_numbers(text, start_line=1)

    start, end = resolved

    def _resolve(line_no: int) -> int:
        if line_no < 0:
            return len(lines) + line_no + 1
        return line_no

    first = max(1, _resolve(start))
    last = max(1, _resolve(end))
    if first > last:
        first, last = last, first
    first = min(first, len(lines))
    last = min(last, len(lines))

    selected = lines[first - 1 : last]
    if not show_line_number:
        return "\n".join(selected)
    return format_with_line_numbers("\n".join(selected), start_line=first)


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------


def truncate_300(
    text: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
) -> tuple[str, bool]:
    """按 ``split("\\n")`` 元素数截断，返回 ``(结果, 是否截断)``。

    阈值是**元素数**不是逻辑行数（§5.3）⇒ 300 行 + 结尾换行 = 301 个元素 = 被截断；
    299 行 + 结尾换行 = 300 个元素 = 原样。提示行固定为
    ``\\n[... 共 {n} 行, 已截断]``，``n`` 是元素总数。
    """
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text, False
    return "\n".join(lines[:max_lines]) + f"\n[... 共 {len(lines)} 行, 已截断]", True


# ---------------------------------------------------------------------------
# 结构摘要
# ---------------------------------------------------------------------------


def format_structure_summary(text: str, path: str) -> str:
    """源码文件结构摘要（``read-tool.md`` §5.2 的逐行移植）。

    ``path`` 只出现在 footer 里（句柄写给模型看，通常是磁盘绝对路径或可回读的 URI）；
    ``total`` 是 ``len(text.split("\\n"))``（含结尾空元素）。

    零命中 → 回退原文并做 300 行截断（与既有实现的 ``_truncate_long(content)`` 等价）。
    """
    lines = text.split("\n")
    result: list[str] = []
    in_docstring = False

    for line_no, line in enumerate(lines, 1):
        stripped = line.strip()
        # ① 装饰器整行跳过
        if stripped.startswith("@"):
            continue
        # ② 三引号 docstring 的开/闭（含单行 docstring）
        if stripped.startswith(('"""', "'''")):
            if in_docstring:
                in_docstring = False
                continue
            if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                continue
            in_docstring = True
            continue
        if in_docstring:
            if (
                stripped.endswith(('"""', "'''"))
                or stripped.count('"""') >= 1
                or stripped.count("'''") >= 1
            ):
                in_docstring = False
            continue
        # ③ 行首前缀命中：`N: 内容`（冒号后有空格）
        if stripped.startswith(SUMMARY_PREFIXES):
            result.append(f"{line_no}: {stripped}")
            continue
        # ④ 子串回退。既有实现此处 `i` 被关键字循环变量遮蔽，实际输出
        #    `{kw}: {stripped}`（无行号）—— 冻结行为，逐字保留；
        #    修法见 read-tool.md §11.2（改成 f"{line_no}: {stripped}"）。
        for keyword in SUMMARY_KEYWORDS:
            if keyword in stripped:
                result.append(f"{keyword}: {stripped}")
                break

    if not result:
        return truncate_300(text)[0]
    footer = STRUCTURE_FOOTER.format(path=path, total=len(lines))
    return "\n".join(result) + footer


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


def format_size(size: int) -> str:
    """``<1024`` → ``NB``；``<1MiB`` → ``NKB``（整除）；否则 ``N.NMB``。"""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def format_file_metadata(
    *,
    size: int,
    mtime: float,
    line_count: int | None = None,
) -> str:
    """FS 条目元数据：``大小, N lines, MM/DD``（顺序固定，§6.1）。

    ``line_count`` 为 ``None`` 时整段省略（非源码后缀 / 超 2MiB / 非 UTF-8 可解码）。
    """
    parts = [format_size(size)]
    if line_count is not None:
        parts.append(f"{line_count} lines")
    parts.append(time.strftime("%m/%d", time.localtime(mtime)))
    return ", ".join(parts)


def mmdd(value: str) -> str:
    """ISO 时间戳 → 本地 ``MM/DD``；无法解析返回 ``""``（该段省略）。"""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone().strftime("%m/%d")


def format_memory_metadata(
    *,
    updated_at: str = "",
    line_count: int | None = None,
    tags: Sequence[str] | None = None,
) -> str:
    """memory 条目元数据：``MM/DD, N lines, #tag…``（顺序与 FS **相反**，§6.3）。

    只保留前 3 个非空 tag，第 4 个及以后丢弃。
    """
    parts: list[str] = []
    updated = str(updated_at or "").strip()
    if updated:
        day = mmdd(updated)
        if day:
            parts.append(day)
    if isinstance(line_count, int):
        parts.append(f"{line_count} lines")
    cleaned = [str(tag).strip() for tag in (tags or ()) if str(tag).strip()]
    parts += [f"#{tag}" for tag in cleaned[:3]]
    return ", ".join(parts)


__all__ = [
    "DEFAULT_MAX_LINES",
    "SOURCE_EXTENSIONS",
    "STRUCTURE_FOOTER",
    "SUMMARY_KEYWORDS",
    "SUMMARY_PREFIXES",
    "apply_selector",
    "format_file_metadata",
    "format_memory_metadata",
    "format_size",
    "format_structure_summary",
    "format_with_line_numbers",
    "mmdd",
    "selector_lines",
    "truncate_300",
]
