"""``search`` / ``find``：行号、二进制跳过、上限、VFS 投影节点、目录后缀。"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.vfs import VirtualFileSystem


@pytest.fixture
def fs_tree(workspace: Path) -> Path:
    """真实文件树：文本 / 二进制 / 隐藏目录 / 子目录。"""
    (workspace / "a.py").write_text("import os\nfoo = 1\nprint(foo)\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.md").write_text("foo in b\n", encoding="utf-8")
    (workspace / "bin.dat").write_bytes(b"\x00foo\x00")
    (workspace / ".hidden").mkdir()
    (workspace / ".hidden" / "x.py").write_text("foo\n", encoding="utf-8")
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return workspace


# ---------------------------------------------------------------------------
# search：真实文件
# ---------------------------------------------------------------------------


async def test_search_reports_line_numbers(search_tool, fs_tree: Path) -> None:
    output = await search_tool.ainvoke({"pattern": "foo"})
    assert output.split("\n") == [
        "a.py:2: foo = 1",
        "a.py:3: print(foo)",
        "sub/b.md:1: foo in b",
    ]


async def test_search_skips_binary_and_hidden(search_tool, fs_tree: Path) -> None:
    output = await search_tool.ainvoke({"pattern": "foo"})
    assert "bin.dat" not in output
    assert ".hidden" not in output


async def test_search_no_match(search_tool, fs_tree: Path) -> None:
    assert await search_tool.ainvoke({"pattern": "zzz"}) == "未找到匹配: zzz"


async def test_search_max_results_truncates(search_tool, fs_tree: Path) -> None:
    lines = (await search_tool.ainvoke({"pattern": "foo", "max_results": 1})).split("\n")
    assert lines == ["a.py:2: foo = 1", "[已达到上限 1 条，结果被截断]"]


async def test_search_regex_not_literal(search_tool, fs_tree: Path) -> None:
    output = await search_tool.ainvoke({"pattern": r"foo\s*=\s*1"})
    assert output == "a.py:2: foo = 1"


async def test_search_invalid_regex(search_tool) -> None:
    output = await search_tool.ainvoke({"pattern": "("})
    assert output.startswith("正则表达式错误: ")


async def test_search_empty_pattern(search_tool) -> None:
    assert await search_tool.ainvoke({"pattern": "  "}) == "错误: 搜索模式不能为空"


async def test_search_missing_root(search_tool, workspace: Path) -> None:
    output = await search_tool.ainvoke({"pattern": "x", "path": "nope"})
    assert output.startswith("[路径不存在: ")
    assert output.endswith("]")


async def test_search_subdir_scope(search_tool, fs_tree: Path) -> None:
    output = await search_tool.ainvoke({"pattern": "foo", "path": "sub"})
    # 路径统一相对工作区展示（与 read 的路径写法一致），不是相对搜索根
    assert output == "sub/b.md:1: foo in b"


# ---------------------------------------------------------------------------
# search：VFS
# ---------------------------------------------------------------------------


async def test_search_vfs_reports_line_numbers(
    search_tool, tree_vfs: VirtualFileSystem
) -> None:
    output = await search_tool.ainvoke({"pattern": "foo", "path": "agent://notes"})
    assert output.split("\n") == [
        "agent://notes/a.md:2: foo",
        "agent://notes/b.md:1: foo",
        "agent://notes/b.md:2: foo",
        "agent://notes/dir/keep.txt:1: foo",
    ]


async def test_search_vfs_accepts_root_alias(search_tool, tree_vfs: VirtualFileSystem) -> None:
    output = await search_tool.ainvoke({"pattern": "alpha", "root": "agent://notes"})
    assert output == "agent://notes/a.md:1: alpha"


async def test_search_vfs_skips_projection_and_binary(
    search_tool, tree_vfs: VirtualFileSystem
) -> None:
    output = await search_tool.ainvoke({"pattern": "foo", "path": "agent://"})
    assert "project.md" not in output
    assert "blob.bin" not in output


async def test_search_vfs_missing_root(search_tool, vfs: VirtualFileSystem) -> None:
    output = await search_tool.ainvoke({"pattern": "x", "path": "agent://nope"})
    assert output == "[路径不存在: agent://nope]"


# ---------------------------------------------------------------------------
# find：真实文件
# ---------------------------------------------------------------------------


async def test_find_glob_is_recursive(find_tool, fs_tree: Path) -> None:
    output = await find_tool.ainvoke({"pattern": "*.py"})
    assert output.split("\n") == ["a.py", "src/pkg/mod.py"]


async def test_find_directory_has_trailing_slash(find_tool, fs_tree: Path) -> None:
    assert await find_tool.ainvoke({"pattern": "pkg"}) == "src/pkg/"


async def test_find_no_match(find_tool, fs_tree: Path) -> None:
    assert await find_tool.ainvoke({"pattern": "*.rs"}) == "未找到匹配的文件或目录: *.rs"


async def test_find_max_results_truncates(find_tool, fs_tree: Path) -> None:
    lines = (await find_tool.ainvoke({"pattern": "*.py", "max_results": 1})).split("\n")
    assert lines == ["a.py", "[已达到上限 1 项，结果被截断]"]


async def test_find_empty_pattern(find_tool) -> None:
    assert await find_tool.ainvoke({"pattern": ""}) == "错误: pattern 不能为空"


async def test_find_missing_root(find_tool) -> None:
    output = await find_tool.ainvoke({"pattern": "*", "root": "nope"})
    assert output.startswith("[路径不存在: ")


# ---------------------------------------------------------------------------
# find：VFS
# ---------------------------------------------------------------------------


async def test_find_vfs_files_skip_projection(
    find_tool, tree_vfs: VirtualFileSystem
) -> None:
    output = await find_tool.ainvoke({"pattern": "*.md", "root": "agent://notes"})
    assert output.split("\n") == ["agent://notes/a.md", "agent://notes/b.md"]
    assert "project.md" not in output


async def test_find_vfs_directory_has_trailing_slash(
    find_tool, tree_vfs: VirtualFileSystem
) -> None:
    assert await find_tool.ainvoke({"pattern": "dir", "root": "agent://"}) == (
        "agent://notes/dir/"
    )


async def test_find_vfs_lists_binary_paths_but_not_projection(
    find_tool, tree_vfs: VirtualFileSystem
) -> None:
    output = await find_tool.ainvoke({"pattern": "*", "root": "agent://notes"})
    assert "agent://notes/blob.bin" in output
    assert "agent://notes/project.md" not in output


async def test_find_vfs_max_results_truncates(
    find_tool, tree_vfs: VirtualFileSystem
) -> None:
    lines = (await find_tool.ainvoke(
        {"pattern": "*", "root": "agent://notes", "max_results": 1}
    )).split("\n")
    assert lines == ["agent://notes/a.md", "[已达到上限 1 项，结果被截断]"]


async def test_find_vfs_missing_root(find_tool, vfs: VirtualFileSystem) -> None:
    output = await find_tool.ainvoke({"pattern": "*", "root": "agent://nope"})
    assert output == "[路径不存在: agent://nope]"


async def test_search_finds_gbk_text(search_tool, workspace: Path) -> None:
    """GBK 文本不再被当二进制跳过（改前 ``decode("utf-8")`` 失败就 continue）。"""
    (workspace / "gbk.log").write_bytes("line one TOKEN\n".encode("gb18030"))
    output = await search_tool.ainvoke({"pattern": "TOKEN"})
    assert "gbk.log:1:" in output


async def test_search_finds_utf16_text(search_tool, workspace: Path) -> None:
    (workspace / "utf16.log").write_bytes("TOKEN 在这里\n".encode("utf-16"))
    output = await search_tool.ainvoke({"pattern": "TOKEN"})
    assert "utf16.log:1:" in output
