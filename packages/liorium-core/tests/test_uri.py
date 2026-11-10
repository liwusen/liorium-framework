"""``liorium.core.uri`` 的回归测试（计划 §3.6 URI 归一化）。

守住四件事：

1. ``<scheme>://a/b`` ≡ ``/<scheme>/a/b``，且解析器**不认识任何具体 scheme**；
2. 裸路径 / ``file://`` / ``C:\\x`` 不许被误判成 scheme（裸路径 = 真实文件系统）；
3. 选择器全集（``:N`` / ``:-N`` / ``:A-B`` / ``:-A--B`` / ``:-A:-B`` / ``:A+C`` /
   ``:raw`` / ``:A-B:raw``）与 ``selector_lines`` / ``is_raw`` / ``is_dir`` 判定；
4. ``?query`` 先剥、``trailing_slash`` 语义、``normalize_vfs_path`` 与 ``vfs_uri`` 往返。
"""

from __future__ import annotations

import pytest

from liorium.core.errors import VfsError
from liorium.core.uri import (
    SCHEME_FILE,
    is_scheme_uri,
    normalize_vfs_path,
    parse,
    vfs_uri,
)

# ---------------------------------------------------------------------------
# scheme ↔ 挂载点（规则 1 / 规则 3）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("skills://my-skill/SKILL.md", "/skills/my-skill/SKILL.md"),
        ("agent://index.md", "/agent/index.md"),
        ("memories://notes/math", "/memories/notes/math"),
        ("artifacts://abc123", "/artifacts/abc123"),
        ("code://backend/main.py", "/code/backend/main.py"),
        ("subagents://research-1/finalResult.md", "/subagents/research-1/finalResult.md"),
    ],
)
def test_scheme_uri_maps_to_same_named_mount_point(raw: str, expected: str) -> None:
    """scheme 与挂载点同名、无翻译表：``<scheme>://a/b`` → ``/<scheme>/a/b``。"""
    parsed = parse(raw)
    assert parsed.is_vfs
    assert parsed.vfs_path() == expected


def test_parser_does_not_know_concrete_schemes() -> None:
    """新挂载点自动获得可用 scheme：解析器只做前缀切分，不查表。"""
    for scheme in ("weather2", "screen", "camera", "my-new_thing", "a.b+c"):
        parsed = parse(f"{scheme}://x/y")
        assert parsed.scheme == scheme
        assert parsed.vfs_path() == f"/{scheme}/x/y"


def test_scheme_is_lowercased() -> None:
    """scheme 归一成小写，避免 ``Skills://`` 与 ``skills://`` 分裂成两个挂载。"""
    parsed = parse("Skills://My-Skill/SKILL.md")
    assert parsed.scheme == "skills"
    assert parsed.path == "My-Skill/SKILL.md"
    assert parsed.vfs_path() == "/skills/My-Skill/SKILL.md"


def test_empty_uri_is_empty_file_path() -> None:
    parsed = parse("")
    assert parsed.scheme == SCHEME_FILE
    assert parsed.path == ""
    assert not parsed.is_vfs


def test_uri_is_stripped() -> None:
    parsed = parse("  skills://a/b  ")
    assert parsed.raw == "skills://a/b"
    assert parsed.path == "a/b"


def test_vfs_path_rejected_for_real_filesystem() -> None:
    """``file`` scheme 的路径不映射进 VFS（调用方必须走 ``paths.py``）。"""
    with pytest.raises(VfsError):
        parse("src/main.py").vfs_path()


# ---------------------------------------------------------------------------
# 裸路径 / file:// / 盘符（规则 4）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "src/main.py",
        "notes/readme.md",
        "./x/y.txt",
        "../up.txt",
        "AGENT.md",
        r"D:\data\x.txt",
        r"C:\x",
    ],
)
def test_bare_paths_are_real_filesystem(raw: str) -> None:
    """裸路径一律 ``file`` scheme：只有 ``<scheme>://`` 才走 VFS。"""
    parsed = parse(raw)
    assert parsed.scheme == SCHEME_FILE
    assert not parsed.is_vfs


@pytest.mark.parametrize("raw", ["src/main.py", "notes/readme.md", r"D:\data\x.txt", r"C:\x"])
def test_bare_path_is_not_mistaken_for_scheme(raw: str) -> None:
    """没有 ``://`` 就不是 scheme；Windows 盘符里的冒号也不例外。"""
    assert not is_scheme_uri(raw)


def test_windows_drive_colon_is_not_a_selector() -> None:
    """``C:\\x`` 的冒号是盘符，不许被当成行选择器。"""
    parsed = parse(r"C:\x")
    assert parsed.selector is None
    assert parsed.path == r"C:\x"
    assert parsed.is_dir is False


def test_file_uri_is_absolute_real_path() -> None:
    """``file:///C:/x/y.txt`` 是真实文件系统绝对路径，不是 VFS。"""
    parsed = parse("file:///C:/x/y.txt")
    assert parsed.scheme == SCHEME_FILE
    assert parsed.path == "/C:/x/y.txt"
    assert not parsed.is_vfs
    assert parsed.selector is None


def test_file_uri_with_selector() -> None:
    parsed = parse("file:///C:/x/y.txt:10-20")
    assert parsed.scheme == SCHEME_FILE
    assert parsed.path == "/C:/x/y.txt"
    assert parsed.selector_lines == (10, 20)


@pytest.mark.parametrize("raw", ["skills://a", "agent://", "artifact://abc123"])
def test_scheme_detection(raw: str) -> None:
    assert is_scheme_uri(raw)


# ---------------------------------------------------------------------------
# 选择器全集（规则 5：从右最长匹配）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "selector", "lines", "is_raw"),
    [
        ("src/a.py:50", ":50", (50, 50), False),
        ("src/a.py:-10", ":-10", (-10, -10), False),
        ("src/a.py:10-20", ":10-20", (10, 20), False),
        ("src/a.py:-20--10", ":-20--10", (-20, -10), False),
        ("src/a.py:-20:-10", ":-20:-10", (-20, -10), False),
        ("src/a.py:10+5", ":10+5", (10, 14), False),
        ("src/a.py:raw", ":raw", None, True),
        ("src/a.py:50-100:raw", ":50-100:raw", (50, 100), True),
    ],
)
def test_selector_forms(
    raw: str, selector: str, lines: tuple[int, int] | None, is_raw: bool
) -> None:
    """八种选择器形态：path 永远只剩选择器之前的部分。"""
    parsed = parse(raw)
    assert parsed.path == "src/a.py"
    assert parsed.selector == selector
    assert parsed.selector_lines == lines
    assert parsed.is_raw is is_raw
    assert parsed.is_dir is False


def test_longest_selector_wins_for_double_negative_range() -> None:
    """``:-20:-10`` 不许被截成 ``:-10``。"""
    parsed = parse("src/a.py:-20:-10")
    assert parsed.selector == ":-20:-10"
    assert parsed.selector_lines == (-20, -10)


def test_range_with_raw_suffix_keeps_both_semantics() -> None:
    parsed = parse("src/a.py:50-100:raw")
    assert parsed.selector_lines == (50, 100)
    assert parsed.is_raw


def test_no_selector_means_none() -> None:
    assert parse("src/a.py").selector is None
    assert parse("src/a.py").selector_lines is None
    assert parse("src/a.py").is_raw is False


def test_vfs_uri_with_selector_and_query() -> None:
    parsed = parse("skills://my-skill/SKILL.md:10-20?raw=1")
    assert parsed.scheme == "skills"
    assert parsed.path == "my-skill/SKILL.md"
    assert parsed.selector_lines == (10, 20)
    assert parsed.query == {"raw": ["1"]}
    assert parsed.vfs_path() == "/skills/my-skill/SKILL.md"


# ---------------------------------------------------------------------------
# is_dir / trailing_slash
# ---------------------------------------------------------------------------


def test_trailing_slash_marks_directory() -> None:
    parsed = parse("src/")
    assert parsed.trailing_slash is True
    assert parsed.path == "src/"
    assert parsed.is_dir is True


def test_empty_path_is_directory() -> None:
    """``skills://`` 的 path 为空，语义是挂载点根目录。"""
    parsed = parse("skills://")
    assert parsed.path == ""
    assert parsed.is_dir is True
    assert parsed.vfs_path() == "/skills"


def test_vfs_dir_with_trailing_slash() -> None:
    parsed = parse("skills://my-skill/")
    assert parsed.trailing_slash is True
    assert parsed.is_dir is True
    assert parsed.vfs_path() == "/skills/my-skill"


def test_line_selector_beats_trailing_slash() -> None:
    """带行选择器就不是目录（哪怕原串以 ``/`` 结尾）。"""
    parsed = parse("src/:50")
    assert parsed.path == "src/"
    assert parsed.trailing_slash is True
    assert parsed.selector_lines == (50, 50)
    assert parsed.is_dir is False


def test_raw_selector_is_not_a_line_selector_but_not_a_dir() -> None:
    parsed = parse("src/a.py:raw")
    assert parsed.selector_lines is None
    assert parsed.is_dir is False


# ---------------------------------------------------------------------------
# query 剥离
# ---------------------------------------------------------------------------


def test_query_is_stripped_before_path() -> None:
    parsed = parse("memory://notes/math?x=1")
    assert parsed.scheme == "memory"
    assert parsed.path == "notes/math"
    assert parsed.vfs_path() == "/memory/notes/math"
    assert parsed.query == {"x": ["1"]}
    assert parsed.trailing_slash is False


def test_query_on_directory_keeps_trailing_slash() -> None:
    parsed = parse("notes/dir/?a=1&b=2")
    assert parsed.path == "notes/dir/"
    assert parsed.trailing_slash is True
    assert parsed.is_dir is True
    assert parsed.query == {"a": ["1"], "b": ["2"]}


def test_query_keeps_blank_and_repeated_values() -> None:
    parsed = parse("memory://x?flag&y=1&y=2")
    assert parsed.query == {"flag": [""], "y": ["1", "2"]}


def test_query_after_selector_is_dropped() -> None:
    """顺序是「先剥 query，再取 selector」，所以选择器不会被 query 污染。"""
    parsed = parse("src/a.py:10?x=1")
    assert parsed.path == "src/a.py"
    assert parsed.selector_lines == (10, 10)
    assert parsed.query == {"x": ["1"]}


# ---------------------------------------------------------------------------
# normalize_vfs_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/skills/a", "/skills/a"),
        ("skills/a", "/skills/a"),
        ("/skills//a", "/skills/a"),
        ("/skills/a/", "/skills/a"),
        ("/skills/./a", "/skills/a"),
        ("/skills/x/../a", "/skills/a"),
        ("", "/"),
        ("/", "/"),
    ],
)
def test_normalize_vfs_path_collapses(raw: str, expected: str) -> None:
    """折叠重复斜杠 / ``.`` / 可回退的 ``..``，并保证以 ``/`` 开头。"""
    assert normalize_vfs_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "/../etc/passwd",
        "/skills/../../etc/passwd",
        "../outside.txt",
        "skills/../../../root",
    ],
)
def test_normalize_vfs_path_rejects_escape(raw: str) -> None:
    """``..`` 越出根目录必须报错，不许静默夹紧（否则挂载隔离失效）。"""
    with pytest.raises(VfsError):
        normalize_vfs_path(raw)


def test_normalize_vfs_path_collapses_leading_double_slash() -> None:
    """开头出现多个斜杠也要归一成单个 ``/``（POSIX 的 ``//`` 特例也不行）。

    否则同一节点会同时出现 ``//skills/a`` 与 ``/skills/a`` 两种键。
    """
    assert normalize_vfs_path("//skills///a//") == "/skills/a"


# ---------------------------------------------------------------------------
# vfs_uri 往返
# ---------------------------------------------------------------------------


def test_vfs_uri_roundtrip() -> None:
    uri = vfs_uri("skills", "/my-skill/SKILL.md")
    assert uri == "skills://my-skill/SKILL.md"
    assert parse(uri).vfs_path() == "/skills/my-skill/SKILL.md"


def test_vfs_uri_roundtrip_root() -> None:
    assert vfs_uri("agent", "/") == "agent://"
    assert parse("agent://").vfs_path() == "/agent"
    assert parse("agent://index.md").vfs_path() == "/agent/index.md"


def test_vfs_uri_strips_leading_slashes_only() -> None:
    assert vfs_uri("code", "//a//b/c.py") == "code://a//b/c.py"
    assert vfs_uri("code", "") == "code://"


@pytest.mark.parametrize(
    ("scheme", "sub"),
    [
        ("skills", "my-skill/SKILL.md"),
        ("memories", "notes/math.md"),
        ("subagents", "research-1/finalResult.md"),
    ],
)
def test_vfs_uri_roundtrip_preserves_hierarchy(scheme: str, sub: str) -> None:
    """``vfs_path()`` 与 ``vfs_uri()`` 必须互逆（层级不丢、不重写）。"""
    parsed = parse(vfs_uri(scheme, sub))
    assert parsed.vfs_path() == normalize_vfs_path(f"/{scheme}/{sub}")
    assert parsed.vfs_path() == f"/{scheme}/{sub}"
