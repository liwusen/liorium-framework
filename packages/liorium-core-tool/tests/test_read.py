"""``read`` 工具与 ``format`` 纯函数的冻结行为测试。

判据来自 ``docs/design/read-tool.md``：选择器全集（含已知异常）、两种行号格式、
结构摘要（footer 逐字）、300 行截断边界、两种元数据格式、目录列举、VFS 挂载点、
图片/二进制的内容类型分派、缺失与未知 scheme 文案。

``read`` 依赖真实的 ``liorium.core_tool.runtime.ToolRuntime``；共用夹具
（``runtime`` / ``vfs`` / ``workspace`` / 内存 ``ArtifactStore``）来自
``tests/conftest.py``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from liorium.core.types import Binary, Image, Text
from liorium.core_tool.format import (
    apply_selector,
    format_file_metadata,
    format_memory_metadata,
    format_size,
    format_structure_summary,
    format_with_line_numbers,
    truncate_300,
)
from liorium.core_tool.read import build_read_tool
from liorium.core_tool.runtime import ToolRuntime
from liorium.vfs import VfsNode

PNG_BYTES = b"\x89PNG\r\n\x1a\n"
"""假装是 PNG 的 8 字节；测试只关心 base64 与 mime，不解码图片。"""


@pytest.fixture
def read(runtime: ToolRuntime) -> BaseTool:
    return build_read_tool(runtime)


async def _call(tool: BaseTool, uri: str, **kwargs: object) -> str:
    result = await tool.ainvoke({"uri": uri, **kwargs})
    assert isinstance(result, str)
    return result


def _file(
    name: str,
    content: object,
    *,
    description: str = "",
    meta: dict | None = None,
) -> VfsNode:
    return VfsNode(name=name, content=content, description=description, meta=meta or {})


def _dir(name: str, *children: VfsNode, description: str = "") -> VfsNode:
    node = VfsNode(name=name, is_directory=True, description=description)
    for child in children:
        node.add_child(child)
    return node


def _lines(count: int) -> str:
    return "\n".join(f"line{index:03d}" for index in range(1, count + 1))


# ---------------------------------------------------------------------------
# 选择器（read-tool.md §4.1/§4.2）
# ---------------------------------------------------------------------------


class TestApplySelector:
    def test_single_line(self) -> None:
        assert apply_selector("l1\nl2\nl3\nl4\nl5", ":5") == "l5"

    def test_negative_single_line(self) -> None:
        assert apply_selector(_lines(400), ":-3") == "line398"

    def test_forward_range(self) -> None:
        assert apply_selector("l1\nl2\nl3\nl4\nl5", ":3-5") == "l3\nl4\nl5"

    def test_negative_range_absolute_line_numbers(self) -> None:
        out = apply_selector(_lines(40), ":-20:-10", show_line_number=True)
        rows = out.split("\n")
        assert len(rows) == 11
        assert rows[0] == "21:line021"
        assert rows[-1] == "31:line031"

    def test_double_dash_negative_range(self) -> None:
        assert apply_selector(_lines(40), ":-20--10", show_line_number=True) == apply_selector(
            _lines(40), ":-20:-10", show_line_number=True
        )

    def test_count_form(self) -> None:
        assert apply_selector(_lines(10), ":5+3") == "line005\nline006\nline007"

    def test_clamp_tail(self) -> None:
        assert apply_selector("l1\nl2\nl3\nl4\nl5", ":999-1000") == "l5"

    def test_clamp_head(self) -> None:
        assert apply_selector("l1\nl2\nl3\nl4\nl5", ":-999") == "l1"

    def test_reverse_range_is_swapped(self) -> None:
        assert apply_selector("l1\nl2\nl3\nl4\nl5", ":10-3") == "l3\nl4\nl5"

    def test_range_with_raw_suffix_is_equivalent(self) -> None:
        text = "l1\nl2\nl3\nl4\nl5"
        assert apply_selector(text, ":3-5:raw") == apply_selector(text, ":3-5")

    def test_raw_returns_whole_text(self) -> None:
        text = "a\nb\nc"
        assert apply_selector(text, ":raw") == text

    def test_no_selector_numbers_whole_text(self) -> None:
        assert apply_selector("a\nb", None, show_line_number=True) == "1:a\n2:b"

    def test_empty_text_stays_empty(self) -> None:
        assert apply_selector("", None, show_line_number=True) == ""

    def test_negative_count_is_known_bug(self) -> None:
        """``:-5+3`` 通过 SELECTOR_RE 但解析期 ``int("-5+3")`` 抛 ValueError（冻结 bug）。"""
        with pytest.raises(ValueError):
            apply_selector(_lines(10), ":-5+3")


class TestLineNumberFormat:
    def test_frozen_format_has_no_space(self) -> None:
        assert format_with_line_numbers("a\nb") == "1:a\n2:b"

    def test_summary_format_has_space(self) -> None:
        assert format_with_line_numbers("a\nb", spaced=True) == "1: a\n2: b"

    def test_start_line_is_absolute(self) -> None:
        assert format_with_line_numbers("a\nb", start_line=5) == "5:a\n6:b"


# ---------------------------------------------------------------------------
# 结构摘要（read-tool.md §5.2）
# ---------------------------------------------------------------------------

DEMO_SOURCE = (
    "import os\n"
    "\n"
    "@decorator\n"
    "def foo():\n"
    '    """文档"""\n'
    "    return 42\n"
    "\n"
    "class MyClass:\n"
    "    def method(self):\n"
    "        pass\n"
)


class TestStructureSummary:
    def test_python_summary_skips_decorator_and_docstring(self) -> None:
        summary = format_structure_summary(DEMO_SOURCE, "demo.py")
        body = summary.split("\n[文件 ")[0]
        assert body == "1: import os\n4: def foo():\n8: class MyClass:\n9: def method(self):"

    def test_footer_is_verbatim(self) -> None:
        summary = format_structure_summary(DEMO_SOURCE, "demo.py")
        footer = (
            '\n[文件 demo.py: 11 行, 显示结构摘要。用 read("demo.py:N-M") 查看具体行范围'
            '或用read("demo.py:raw")关闭结构摘要]'
        )
        assert summary.endswith(footer)
        # 冻结差异：`用 read(` 有空格，`用read(` 故意缺一个空格
        assert '用 read("demo.py:N-M")' in summary
        assert '或用read("demo.py:raw")' in summary

    def test_keyword_fallback_keeps_frozen_bug(self) -> None:
        """关键字子串回退在既有实现里因变量遮蔽丢了行号，逐字保留（§5.2 `[bug]`）。"""
        summary = format_structure_summary("value = struct_thing\n", "x.py")
        assert summary.startswith("struct: value = struct_thing")

    def test_no_hits_falls_back_to_original(self) -> None:
        text = "x = 1\ny = 2\n"
        assert format_structure_summary(text, "x.py") == text

    def test_fallback_is_truncated(self) -> None:
        text = "\n".join(f"value{i} = {i}" for i in range(1, 350))
        out = format_structure_summary(text, "x.py")
        assert out.endswith("[... 共 349 行, 已截断]")


# ---------------------------------------------------------------------------
# 300 行截断（read-tool.md §5.3）
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_299_lines_with_trailing_newline_is_untouched(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 300)) + "\n"
        out, truncated = truncate_300(text)
        assert truncated is False
        assert out == text

    def test_300_lines_with_trailing_newline_is_truncated(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 301)) + "\n"
        out, truncated = truncate_300(text)
        assert truncated is True
        assert out.endswith("\n[... 共 301 行, 已截断]")
        assert out.count("\n") == 300

    def test_301_lines_without_trailing_newline_is_truncated(self) -> None:
        text = "\n".join(f"l{i}" for i in range(1, 302))
        out, truncated = truncate_300(text)
        assert truncated is True
        assert out.endswith("[... 共 301 行, 已截断]")
        assert out.split("\n")[299] == "l300"


# ---------------------------------------------------------------------------
# 元数据（read-tool.md §6.1/§6.3）
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_size_format(self) -> None:
        assert format_size(3) == "3B"
        assert format_size(2048) == "2KB"
        assert format_size(2 * 1024 * 1024) == "2.0MB"

    def test_file_metadata_order(self, tmp_path: Path) -> None:
        target = tmp_path / "alpha.py"
        target.write_text("a\nb\nc", encoding="utf-8")
        st = target.stat()
        meta = format_file_metadata(size=st.st_size, mtime=st.st_mtime, line_count=3)
        # 3 行 Python 源码：Windows 下 write_text 会写 CRLF，所以大小按 stat 算
        assert re.fullmatch(rf"{st.st_size}B, 3 lines, \d{{2}}/\d{{2}}", meta)

    def test_file_metadata_omits_lines_when_unknown(self, tmp_path: Path) -> None:
        target = tmp_path / "blob.bin"
        target.write_bytes(b"\x00\x01\x02")
        st = target.stat()
        meta = format_file_metadata(size=st.st_size, mtime=st.st_mtime, line_count=None)
        assert re.fullmatch(r"3B, \d{2}/\d{2}", meta)

    def test_memory_metadata_keeps_first_three_tags(self) -> None:
        meta = format_memory_metadata(
            updated_at="2026-11-04T12:00:00",
            line_count=3,
            tags=["工作", "计划", "重要", "第四个", " "],
        )
        assert meta == "11/04, 3 lines, #工作, #计划, #重要"

    def test_memory_metadata_skips_unparsable_date(self) -> None:
        assert format_memory_metadata(updated_at="not-a-date", line_count=2) == "2 lines"


# ---------------------------------------------------------------------------
# 真实文件系统（裸路径 / file://）
# ---------------------------------------------------------------------------


class TestFileRead:
    def test_tool_schema(self, read: BaseTool) -> None:
        assert read.name == "read"
        assert set(read.args) == {"uri", "force_plain_text", "show_line_number", "with_metadata"}

    async def test_line_range(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "alpha.txt").write_text("\n".join(f"row{i}" for i in range(1, 11)), "utf-8")
        assert await _call(read, "alpha.txt:3-5") == "row3\nrow4\nrow5"

    async def test_line_range_with_numbers(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "alpha.txt").write_text("\n".join(f"row{i}" for i in range(1, 11)), "utf-8")
        assert await _call(read, "alpha.txt:3-5", show_line_number=True) == (
            "3:row3\n4:row4\n5:row5"
        )

    async def test_show_line_number_ignored_without_selector(
        self, read: BaseTool, workspace: Path
    ) -> None:
        """普通文本文件无选择器时 ``show_line_number`` 被忽略（§4.4 冻结矩阵）。"""
        content = "alpha\nbeta"
        (workspace / "alpha.txt").write_text(content, "utf-8")
        assert await _call(read, "alpha.txt", show_line_number=True) == content

    async def test_directory_listing_with_metadata(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "empty").mkdir()
        (workspace / "sub").mkdir()
        (workspace / "alpha.py").write_text("a\nb\nc", "utf-8")
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")

        rows = (await _call(read, "")).split("\n")
        assert rows[0] == "  empty/"
        assert rows[1] == "  sub/"
        assert rows[2] == "  alpha.py"
        size = (workspace / "alpha.py").stat().st_size
        assert re.fullmatch(rf"    \[{size}B, 3 lines, \d{{2}}/\d{{2}}\]", rows[3])
        assert rows[4] == "  blob.bin"
        assert re.fullmatch(r"    \[3B, \d{2}/\d{2}\]", rows[5])
        assert len(rows) == 6

    async def test_directory_listing_without_metadata_is_byte_exact(
        self, read: BaseTool, workspace: Path
    ) -> None:
        (workspace / "empty").mkdir()
        (workspace / "sub").mkdir()
        (workspace / "alpha.py").write_text("a\nb\nc", "utf-8")
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
        assert await _call(read, "", with_metadata=False) == (
            "  empty/\n  sub/\n  alpha.py\n  blob.bin"
        )

    async def test_empty_directory_is_empty_string(self, read: BaseTool) -> None:
        assert await _call(read, "") == ""

    async def test_source_file_gets_structure_summary(
        self, read: BaseTool, workspace: Path
    ) -> None:
        target = workspace / "demo.py"
        target.write_text(DEMO_SOURCE, "utf-8")
        out = await _call(read, "demo.py")
        assert out.startswith("1: import os\n4: def foo():")
        assert out.endswith(
            f'\n[文件 {target}: 11 行, 显示结构摘要。用 read("{target}:N-M") 查看具体行范围'
            f'或用read("{target}:raw")关闭结构摘要]'
        )
        assert "return 42" not in out

    async def test_raw_disables_structure_summary(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "demo.py").write_text(DEMO_SOURCE, "utf-8")
        assert await _call(read, "demo.py:raw") == DEMO_SOURCE

    async def test_non_whitelist_suffix_is_plain_text(
        self, read: BaseTool, workspace: Path
    ) -> None:
        content = "# 标题\n\n正文 def foo():"
        (workspace / "notes.md").write_text(content, "utf-8")
        assert await _call(read, "notes.md") == content

    async def test_plain_text_is_truncated_at_300(self, read: BaseTool, workspace: Path) -> None:
        content = "\n".join(f"l{i}" for i in range(1, 302))
        (workspace / "big.txt").write_text(content, "utf-8")
        out = await _call(read, "big.txt")
        assert out.endswith("[... 共 301 行, 已截断]")

    async def test_file_url_is_absolute_path(self, read: BaseTool, workspace: Path) -> None:
        target = workspace / "alpha.txt"
        target.write_text("abs", "utf-8")
        assert await _call(read, f"file:///{target.as_posix()}") == "abs"

    async def test_binary_file_returns_text_note(self, read: BaseTool, workspace: Path) -> None:
        """Liorium 修正（§11.2）：真实二进制文件只回说明，不乱码进上下文。"""
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
        assert await _call(read, "blob.bin") == (
            "[二进制: blob.bin]\n"
            "类型: application/octet-stream\n"
            "大小: 3 bytes\n"
            "引用: artifacts://blob.bin"
        )

    async def test_non_utf8_file_returns_text_note(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "broken.dat").write_bytes(b"\xff\xfe\x00broken")
        assert await _call(read, "broken.dat") == (
            "[二进制: broken.dat]\n"
            "类型: application/octet-stream\n"
            "大小: 9 bytes\n"
            "引用: artifacts://broken.dat"
        )

    async def test_utf16_text_file_is_readable(self, read: BaseTool, workspace: Path) -> None:
        """宿主实测：``>`` 重定向出来的 UTF-16 文本（ASCII + 间隔 NUL）曾被判成二进制。

        改前它只会返回 ``[二进制: out.txt]``，模型因此读不到 pytest 的 traceback。
        """
        (workspace / "out.txt").write_bytes("1 passed\n".encode("utf-16"))
        out = await _call(read, "out.txt")
        assert out.startswith("[编码: ")
        assert "UTF-16" in out.split("\n", 1)[0]
        assert "1 passed" in out

    async def test_gbk_text_file_is_readable(self, read: BaseTool, workspace: Path) -> None:
        """中文 Windows 的 GBK 报错文本同样要能读（非 UTF-8 ≠ 二进制）。"""
        (workspace / "gbk.txt").write_bytes("系统找不到指定的文件\n".encode("gb18030"))
        out = await _call(read, "gbk.txt")
        assert "GB18030" in out.split("\n", 1)[0]
        assert "系统找不到指定的文件" in out

    async def test_binary_txt_does_not_claim_text_plain(
        self, read: BaseTool, workspace: Path
    ) -> None:
        """``类型:`` 与内容判定必须一致：.txt 的二进制不再自称 ``text/plain``。"""
        (workspace / "blob.txt").write_bytes(b"\x00\x01\x02")
        out = await _call(read, "blob.txt")
        assert "类型: application/octet-stream" in out
        assert "类型: text/plain" not in out
        # 扩展名猜测降级成注脚，不再冒充内容类型
        assert "猜到的类型是 text/plain" in out


# ---------------------------------------------------------------------------
# 缺失 / 未知 scheme 文案（read-tool.md §3.6-5 / §5.1）
# ---------------------------------------------------------------------------


class TestMissingAndUnknownScheme:
    async def test_missing_file(self, read: BaseTool) -> None:
        assert await _call(read, "nope.txt") == "[文件不存在: nope.txt]"

    async def test_unknown_scheme_keeps_original_string(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "https://example.com/a") == (
            "[文件不存在: https://example.com/a]"
        )

    async def test_unknown_scheme_root(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "nosuch://") == "[文件不存在: nosuch://]"


# ---------------------------------------------------------------------------
# VFS 挂载点（scheme == 挂载点）
# ---------------------------------------------------------------------------


class TestVfsMounts:
    async def test_skills_listing(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/skills",
            _dir(
                "skills",
                _dir(
                    "demo",
                    _file("SKILL.md", Text("# demo")),
                    _file(".hidden", Text("x")),
                    _dir("sub"),
                    description="演示技能",
                ),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "skills://") == (
            "skills:// 可用 skill:\n  skills://demo/\n    [演示技能]"
        )
        assert await _call(read, "skills://", with_metadata=False) == (
            "skills:// 可用 skill:\n  skills://demo/"
        )

    async def test_skills_directory_listing(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/skills",
            _dir(
                "skills",
                _dir(
                    "demo",
                    _file("SKILL.md", Text("# demo"), description="演示技能"),
                    _file(".hidden", Text("x")),
                    _dir("sub"),
                ),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "skills://demo/") == (
            "skills://demo/ 内容:\n"
            "  skills://demo/sub/\n"
            "  skills://demo/SKILL.md\n"
            "    [演示技能]"
        )

    async def test_skills_file_and_missing_texts(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/skills",
            _dir(
                "skills",
                _dir(
                    "demo",
                    _file("SKILL.md", Text("# demo")),
                    _file("util.py", Text("def download(url):\n    return url\n")),
                ),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "skills://demo/SKILL.md") == "# demo"
        assert await _call(read, "skills://demo/util.py:raw") == (
            "def download(url):\n    return url\n"
        )
        assert await _call(read, "skills://ghost") == "[skill 不存在: ghost]"
        assert await _call(read, "skills://demo/nope.md") == "[skill 文件不存在: demo/nope.md]"
        assert await _call(read, "skills://demo/../etc") == "[不允许越界访问 skill 目录]"

    async def test_skills_absent(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "skills://") == "(没有可用的 skill)"

    async def test_subagents_listing_and_file(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/subagents",
            _dir(
                "subagents",
                _dir("research-1", _file("finalResult.md", Text("结论：可行"))),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "subagents://") == (
            "subagents:// 可用资源:\n  subagents://research-1/"
        )
        assert await _call(read, "subagents://research-1/finalResult.md") == "结论：可行"

    async def test_artifacts_listing_and_range(self, runtime: ToolRuntime) -> None:
        rows = "\n".join(f"row{i}" for i in range(1, 8))
        runtime.vfs.mount_node(
            "/artifacts",
            _dir(
                "artifacts",
                _file("read_3", Text(rows)),
                _file("read_4", Text("other")),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "artifacts://") == (
            "可用的 artifact:\n  artifact://read_3\n  artifact://read_4"
        )
        # 单数 `artifact://` 与挂载点等价（冻结的工具输出文本）
        assert await _call(read, "artifact://read_3:5-7") == "row5\nrow6\nrow7"
        assert await _call(read, "artifact://read_3") == rows
        assert await _call(read, "artifact://nope") == "[找不到 artifact: nope]"

    async def test_artifacts_empty(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "artifact://") == "(没有可用的 artifact)"

    async def test_memory_tree_and_document(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/memories",
            _dir(
                "memories",
                _dir("archive"),
                _file(
                    "work",
                    Text("a\nb"),
                    meta={
                        "updated_at": "2026-11-04T12:00:00",
                        "line_count": 3,
                        "tags": ["工作", "计划", "重要", "第四个"],
                    },
                ),
            ),
        )
        read = build_read_tool(runtime)
        tree = await _call(read, "memories://")
        assert tree.split("\n") == [
            "//",
            "  archive/",
            "  work",
            "    [11/04, 3 lines, #工作, #计划, #重要]",
        ]
        assert "#第四个" not in tree
        assert await _call(read, "memories://", with_metadata=False) == "//\n  archive/\n  work"
        assert await _call(read, "memories://work") == "a\nb"
        assert await _call(read, "memories://work", show_line_number=True) == "1:a\n2:b"
        assert await _call(read, "memories://nope") == "[记忆文档不存在: nope]"

    async def test_memory_absent(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "memories://") == "(记忆模块不可用)"

    async def test_agent_lives_at_vfs_root(self, runtime: ToolRuntime) -> None:
        await runtime.vfs.write("/index.md", Text("# 索引"))
        read = build_read_tool(runtime)
        assert await _call(read, "agent://") == (
            "agent:// 可用资源:\n  agent://index.md"
        )
        assert await _call(read, "agent://index.md") == "# 索引"
        # agent 无选择器也编号（§4.4）
        assert await _call(read, "agent://index.md", show_line_number=True) == "1:# 索引"
        assert await _call(read, "agent://nope.md") == "[未知 agent 资源: nope.md]"

    async def test_agent_empty_node_is_unknown(self, runtime: ToolRuntime) -> None:
        await runtime.vfs.write("/empty.txt", Text(""))
        read = build_read_tool(runtime)
        assert await _call(read, "agent://empty.txt") == "[未知 agent 资源: empty.txt]"

    async def test_code_tree_listing_and_summary(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/code",
            _dir(
                "code",
                _file("README.md", Text("# 仓库")),
                _dir("backend", _file("main.py", Text(DEMO_SOURCE))),
            ),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "code://") == "code:// 仓库根 内容:\n  code://README.md\n  code://backend/"
        out = await _call(read, "code://backend/main.py")
        assert out.startswith("1: import os\n4: def foo():")
        assert out.endswith(
            '\n[文件 code://backend/main.py: 11 行, 显示结构摘要。'
            '用 read("code://backend/main.py:N-M") 查看具体行范围'
            '或用read("code://backend/main.py:raw")关闭结构摘要]'
        )
        assert await _call(read, "code://nope.py") == "[源码文件不存在: nope.py]"

    async def test_source_code_alias_keeps_scheme_spelling(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node("/code", _dir("code", _file("main.py", Text(DEMO_SOURCE))))
        read = build_read_tool(runtime)
        out = await _call(read, "sourceCode://main.py")
        assert out.endswith(
            '\n[文件 sourceCode://main.py: 11 行, 显示结构摘要。'
            '用 read("sourceCode://main.py:N-M") 查看具体行范围'
            '或用read("sourceCode://main.py:raw")关闭结构摘要]'
        )

    async def test_img_source_menu(self, runtime: ToolRuntime) -> None:
        read = build_read_tool(runtime)
        assert await _call(read, "img_source://") == (
            "img_source:// 可用资源:\n"
            "  img_source://screenshot\n"
            "  img_source://camera_0\n"
            '使用 read("img_source://screenshot?grid=true&scale=0.5") 截图，'
            '使用 read("img_source://camera_0") 访问摄像头。'
        )
        assert await _call(read, "img_source://screenshot") == (
            "[未知 img_source 资源: screenshot]"
        )


# ---------------------------------------------------------------------------
# 内容类型分派（Text / Image / Binary）
# ---------------------------------------------------------------------------


class TestContentKind:
    async def test_image_node_returns_multimodal_json(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node("/screen", _dir("screen", _file("shot", Image(data=PNG_BYTES, mime="image/png"))))
        read = build_read_tool(runtime)
        payload = json.loads(await _call(read, "screen://shot"))
        assert payload["kind"] == "multimodal_tool_result"
        assert payload["text"] == "图片: shot (8 bytes)"
        assert payload["images"][0]["url"].startswith("data:image/png;base64,")
        # 绝不能把 base64 拼进给人看的文本
        assert "base64" not in payload["text"]
        assert "data:image" not in payload["text"]

    async def test_image_node_force_plain_text(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node("/screen", _dir("screen", _file("shot", Image(data=PNG_BYTES, mime="image/png"))))
        read = build_read_tool(runtime)
        assert await _call(read, "screen://shot", force_plain_text=True) == (
            "[图片: shot]\n大小: 8 bytes\n类型: image/png"
        )

    async def test_binary_node_returns_text_note(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/subagents",
            _dir("subagents", _file("blob.bin", Binary(data=b"\x00\x01", mime="application/octet-stream"))),
        )
        read = build_read_tool(runtime)
        assert await _call(read, "subagents://blob.bin") == (
            "[二进制: blob.bin]\n"
            "类型: application/octet-stream\n"
            "大小: 2 bytes\n"
            "引用: artifacts://subagents/blob.bin"
        )

    async def test_artifact_image_is_reference_form(self, runtime: ToolRuntime) -> None:
        runtime.vfs.mount_node(
            "/artifacts",
            _dir(
                "artifacts",
                _file("read_9", Image(data=PNG_BYTES, mime="image/png"), description="截图"),
            ),
        )
        read = build_read_tool(runtime)
        payload = json.loads(await _call(read, "artifact://read_9"))
        assert payload == {
            "kind": "multimodal_tool_result",
            "text": "截图\n[图片: artifact://read_9]",
            "artifact": "read_9",
        }
        assert await _call(read, "artifact://read_9", force_plain_text=True) == "截图"

    async def test_image_file_is_multimodal_json(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "shot.png").write_bytes(PNG_BYTES)
        payload = json.loads(await _call(read, "shot.png"))
        assert payload["kind"] == "multimodal_tool_result"
        assert payload["text"] == "图片文件: shot.png (8 bytes)"
        assert payload["images"][0]["url"].startswith("data:image/png;base64,")
        assert "base64" not in payload["text"]

    async def test_image_file_force_plain_text(self, read: BaseTool, workspace: Path) -> None:
        (workspace / "shot.png").write_bytes(PNG_BYTES)
        assert await _call(read, "shot.png", force_plain_text=True) == (
            "[图片文件: shot.png]\n大小: 8 bytes\n类型: .png"
        )
