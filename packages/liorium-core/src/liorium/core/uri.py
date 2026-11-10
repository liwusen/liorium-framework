"""URI 归一化：``<scheme>://a/b`` ≡ ``/<scheme>/a/b``。

计划依据 §3.6：

1. **scheme 与挂载点同名，无翻译表** —— 解析器不认识任何具体 scheme；
2. **解析顺序沿用现状** —— 先剥 ``?query``，再从右最长匹配 ``selector``，
   剩下的才是 path；
3. **裸路径 = 真实文件系统**（相对 ``AgentSpec.workspace_root``）；
   ``file://`` 表示真实文件系统绝对路径；只有 ``<scheme>://`` 走 VFS。

本模块只依赖标准库，不做任何 IO。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from .errors import VfsError

SCHEME_FILE = "file"
"""裸路径与 ``file://`` 的 scheme。"""

_SCHEME_RE = re.compile(r"^(?P<scheme>[a-zA-Z][\w+.-]*)://")

SELECTOR_RE = re.compile(r"^(?::raw|:-?\d+(?:(?::|-)-?\d+|\+-?\d+)?(:raw)?)$")
""":N / :-N / :A-B / :-A--B / :A+C / :-A:-B / :raw，可追加 :raw。"""


@dataclass(frozen=True, slots=True)
class ParsedUri:
    """一次解析的结果。"""

    raw: str
    scheme: str
    """小写 scheme；裸路径与 ``file://`` 都是 ``"file"``。"""
    path: str
    """去掉 selector / query 后的路径。scheme URI 里是 scheme 内相对路径。"""
    selector: str | None = None
    """:50-100 / :raw / :50-100:raw，或 None。"""
    query: dict[str, list[str]] = field(default_factory=dict)
    trailing_slash: bool = False
    """原始 URI 的路径是否以 ``/`` 结尾（区分目录，行选择器不影响）。"""

    # -- 选择器 ---------------------------------------------------------

    @property
    def selector_lines(self) -> tuple[int, int] | None:
        """把 selector 解析成 1-indexed 闭区间；``:raw`` 单独使用返回 None。"""
        if not self.selector:
            return None
        s = self.selector.lstrip(":")
        if s == "raw":
            return None
        if s.endswith(":raw"):
            s = s[:-4]
        if not s:
            return None
        # 双冒号负范围：:-20:-10 → 倒数第 20 行 ~ 倒数第 10 行
        if ":" in s and not s.startswith("+"):
            a, b = s.split(":", 1)
            try:
                return (int(a), int(b))
            except ValueError:
                pass
        if "+" in s and not s.startswith("-"):
            offset, length = s.split("+", 1)
            start = int(offset)
            return (start, start + int(length) - 1)
        m = re.match(r"^(-?\d+)-(-?\d+)$", s)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        n = int(s)
        return (n, n)

    @property
    def is_raw(self) -> bool:
        """True 表示关闭结构化摘要（源码文件返回原文）。"""
        if not self.selector:
            return False
        return self.selector == ":raw" or self.selector.endswith(":raw")

    @property
    def is_dir(self) -> bool:
        """True 表示目录语义（路径为空或以 ``/`` 结尾，且没有行选择器）。"""
        return self.selector_lines is None and (self.path == "" or self.trailing_slash)

    # -- 路径 -----------------------------------------------------------

    @property
    def is_vfs(self) -> bool:
        """True 表示走 VFS（有 scheme 且不是 ``file``）。"""
        return self.scheme != SCHEME_FILE

    def vfs_path(self) -> str:
        """映射到 VFS 内的绝对路径：``/<scheme>/<path>``。"""
        if not self.is_vfs:
            raise VfsError(f"{self.raw!r} 不是 VFS URI")
        return normalize_vfs_path(f"/{self.scheme}/{self.path}")


def parse(raw: str) -> ParsedUri:
    """解析任意 URI / 路径。

    =========================  ========  ================  ==============
    输入                        scheme    path              selector
    =========================  ========  ================  ==============
    ``src/main.py``             file      ``src/main.py``   None
    ``src/main.py:50-100``      file      ``src/main.py``   ``:50-100``
    ``src/``                    file      ``src/``          None
    ``artifact://abc123``       artifact  ``abc123``        None
    ``artifact://abc123:5-9``   artifact  ``abc123``        ``:5-9``
    ``skills://my/SKILL.md``    skills    ``my/SKILL.md``   None
    ``file:///C:/x/y.txt``      file      ``/C:/x/y.txt``   None
    =========================  ========  ================  ==============
    """
    text = str(raw or "").strip()
    if not text:
        return ParsedUri(raw="", scheme=SCHEME_FILE, path="")

    m = _SCHEME_RE.match(text)
    if m:
        scheme = m.group("scheme").lower()
        rest = text[m.end():]
    else:
        scheme = SCHEME_FILE
        rest = text

    selector: str | None = None
    query: dict[str, list[str]] = {}

    # 0) Windows 盘符：`D:\x` / `D:/x` / `D:50` 不是 scheme，也不能把盘符后的
    #    冒号当成选择器起点 —— 先把盘符切下来，只对剩余部分找选择器。
    drive = ""
    drive_match = re.match(r"^([A-Za-z]:)(?=$|[\\/])", rest)
    if drive_match:
        drive = drive_match.group(1)
        rest = rest[len(drive):]

    # 1) query 先剥（`?` 之后，`#` 之前）
    qpos = rest.find("?")
    if qpos > -1:
        qpart = rest[qpos + 1:]
        rest = rest[:qpos]
        hpos = qpart.find("#")
        if hpos > -1:
            qpart = qpart[:hpos]
        if qpart:
            query = parse_qs(qpart, keep_blank_values=True)

    # 2) selector：从右最长匹配
    if ":" in rest:
        selector = _extract_selector(rest)
        if selector:
            rest = rest[: len(rest) - len(selector)]

    return ParsedUri(
        raw=text,
        scheme=scheme,
        path=drive + rest,
        selector=selector,
        query=query,
        trailing_slash=rest.endswith("/"),
    )


def _extract_selector(rest: str) -> str | None:
    """从 ``rest`` 里取出最长的 selector 后缀。

    逐个冒号位置从右往左试，取最长匹配 —— 这样 ``:-20:-10`` 不会被截成 ``:-10``，
    ``:50-100:raw`` 也能整体匹配。
    """
    best: str | None = None
    for pos in range(len(rest) - 1, -1, -1):
        if rest[pos] != ":":
            continue
        candidate = rest[pos:]
        if SELECTOR_RE.match(candidate):
            if best is None or len(candidate) > len(best):
                best = candidate
    return best


def normalize_vfs_path(path: str) -> str:
    """把 VFS 路径归一化成以 ``/`` 开头、无 ``.``/``..``/重复斜杠的形式。

    ``..`` 越出根目录 → **报错**（不做"静默夹紧"，否则挂载隔离失效）。
    开头重复斜杠也会被折叠（``//a//b`` → ``/a/b``），避免同一节点出现两种键。
    """
    raw = str(path or "").replace("\\", "/")
    if not raw.startswith("/"):
        raw = "/" + raw
    out: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise VfsError(f"VFS 路径越界（.. 逃出根）: {path!r}")
            out.pop()
            continue
        out.append(part)
    return "/" + "/".join(out)


def vfs_uri(scheme: str, path: str) -> str:
    """``(scheme, path)`` → ``scheme://path``（去重斜杠，保留 path 内层级）。"""
    clean = path.lstrip("/")
    return f"{scheme}://{clean}" if clean else f"{scheme}://"


def is_scheme_uri(raw: str) -> bool:
    """是否带 ``<scheme>://`` 前缀。"""
    return bool(_SCHEME_RE.match(str(raw or "").strip()))


__all__ = [
    "ParsedUri",
    "SCHEME_FILE",
    "SELECTOR_RE",
    "is_scheme_uri",
    "normalize_vfs_path",
    "parse",
    "vfs_uri",
]
