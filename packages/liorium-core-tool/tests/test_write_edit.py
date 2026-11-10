"""``write`` / ``edit``：真实文件与 VFS 的写入、唯一匹配、ACK、行尾保留。"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.core.errors import VfsReadOnlyError
from liorium.core.types import Text
from liorium.core_tool.edit import replace_exact
from liorium.core_tool.runtime import ToolRuntime
from liorium.vfs import VirtualFileSystem


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


async def test_write_creates_parents_and_roundtrips(
    write_tool, runtime: ToolRuntime, workspace: Path
) -> None:
    content = "你好\nworld\n"
    output = await write_tool.ainvoke({"path": "notes/deep/a.txt", "content": content})

    target = (workspace / "notes" / "deep" / "a.txt").resolve()
    assert output == f"已写入 {target} ({len(content.encode('utf-8'))} bytes)"
    assert target.read_bytes() == content.encode("utf-8")
    assert await runtime.read_text(runtime.resolve_path("notes/deep/a.txt")) == content


async def test_write_empty_path(write_tool) -> None:
    assert await write_tool.ainvoke({"path": "  ", "content": "x"}) == "错误: path 不能为空"


async def test_write_vfs_node(write_tool, runtime: ToolRuntime) -> None:
    output = await write_tool.ainvoke(
        {"path": "agent://notes/todo.md", "content": "hello"}
    )
    assert output == "已写入 agent://notes/todo.md (5 bytes)"
    assert await runtime.vfs.read_text("/notes/todo.md") == "hello"


async def test_write_vfs_empty_target(write_tool) -> None:
    assert (
        await write_tool.ainvoke({"path": "agent://", "content": "x"})
        == "错误: agent:// 路径不能为空"
    )


async def test_write_readonly_mount_raises(write_tool, runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="skills")
    await sub.write("/demo/SKILL.md", Text("hello"))
    runtime.vfs.mount_vfs("/skills", sub, readonly=True)

    with pytest.raises(VfsReadOnlyError) as excinfo:
        await write_tool.ainvoke({"path": "skills://demo/SKILL.md", "content": "hacked"})
    message = str(excinfo.value)
    assert "只读" in message
    assert "skills://demo/SKILL.md" in message
    assert await sub.read_text("/demo/SKILL.md") == "hello"


async def test_write_appends_handler_ack(write_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/task.md", Text(""))
    node = await runtime.vfs.stat("/task.md")
    assert node is not None
    node.write_handler = lambda target, content: "已提交 task_7"

    output = await write_tool.ainvoke({"path": "agent://task.md", "content": "正文"})
    assert output == "已写入 agent://task.md (6 bytes)\n↳ 已提交 task_7"


async def test_write_ignores_empty_or_non_str_ack(write_tool, runtime: ToolRuntime) -> None:
    for index, ack in enumerate(("", None, {"ok": True}, 42)):
        vfs_path = f"/ack_{index}.md"
        await runtime.vfs.write(vfs_path, Text(""))
        node = await runtime.vfs.stat(vfs_path)
        assert node is not None
        node.write_handler = lambda target, content, value=ack: value
        output = await write_tool.ainvoke(
            {"path": f"agent://ack_{index}.md", "content": "x"}
        )
        assert "↳" not in output


async def test_write_rejects_selector(write_tool) -> None:
    output = await write_tool.ainvoke({"path": "note.txt:5", "content": "x"})
    assert output.startswith("错误: ")
    assert "行选择器" in output


async def test_write_handler_error_is_readable(write_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/task.md", Text(""))
    node = await runtime.vfs.stat("/task.md")
    assert node is not None

    def handler(target, content):
        raise ValueError("信封不是合法 JSON")

    node.write_handler = handler
    output = await write_tool.ainvoke({"path": "agent://task.md", "content": "x"})
    assert output == "写入 agent://task.md 出错: 信封不是合法 JSON"


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


async def test_edit_unique_match(edit_tool, runtime: ToolRuntime, workspace: Path) -> None:
    target = workspace / "note.txt"
    target.write_bytes("alpha\nbeta\ngamma\n".encode())
    label = str(target.resolve())

    output = await edit_tool.ainvoke(
        {"path": "note.txt", "old_str": "beta", "new_str": "BETA"}
    )
    assert output == f"已编辑 {label} (1 处替换)"
    assert target.read_bytes() == "alpha\nBETA\ngamma\n".encode()


async def test_edit_zero_match_text(edit_tool, workspace: Path) -> None:
    target = workspace / "note.txt"
    target.write_bytes(b"alpha\nbeta\n")
    label = str(target.resolve())

    output = await edit_tool.ainvoke(
        {"path": "note.txt", "old_str": "nosuch", "new_str": "x"}
    )
    expected = (
        f"edit: old_str 在 {label} 中匹配 0 处，文件未被修改。\n"
        f"处理: 1) 用 read(\"{label}\") 重新读取，从输出中逐字复制 old_str；"
        "2) 检查缩进、行尾空格、全角/半角字符是否一致；"
        "3) old_str 是普通字符串不是正则，不要包含 \\n 转义符——多行直接写真实换行。"
    )
    assert output == expected
    assert target.read_bytes() == b"alpha\nbeta\n"


async def test_edit_multi_match_text(edit_tool, workspace: Path) -> None:
    target = workspace / "note.txt"
    target.write_bytes(b"foo\nfoo\n")
    label = str(target.resolve())

    output = await edit_tool.ainvoke({"path": "note.txt", "old_str": "foo", "new_str": "bar"})
    expected = (
        f"edit: old_str 在 {label} 中匹配 2 处（要求恰好 1 处），文件未被修改。\n"
        f"处理: 在 old_str 前后多包含几行上下文使其唯一"
        f"（可用 read(\"{label}\") 查看各处差异）；"
        f"若确实要替换所有 2 处相同片段，"
        f"请改为逐处编辑（每处 old_str 带不同上下文）。"
    )
    assert output == expected
    assert target.read_bytes() == b"foo\nfoo\n"


async def test_edit_empty_old_and_same(edit_tool, workspace: Path) -> None:
    target = workspace / "note.txt"
    target.write_bytes(b"foo\n")

    empty_old = await edit_tool.ainvoke({"path": "note.txt", "old_str": "", "new_str": "x"})
    assert empty_old == (
        "edit: old_str 不能为空（空串会匹配整个文件）。\n"
        "处理: 新建文件用 write(path, content)；在文件末尾追加时，old_str 应包含"
        "文件末尾的锚点行（先用 read 确认最后一行）。"
    )
    same = await edit_tool.ainvoke({"path": "note.txt", "old_str": "foo", "new_str": "foo"})
    assert same == "edit: old_str 与 new_str 完全相同，本次编辑无任何变更，未写入。"
    assert target.read_bytes() == b"foo\n"


async def test_edit_missing_file_text(edit_tool, workspace: Path) -> None:
    label = str((workspace / "nope.txt").resolve())
    output = await edit_tool.ainvoke({"path": "nope.txt", "old_str": "a", "new_str": "b"})
    assert output == (
        f"文件不存在: {label}\n"
        f"建议: 先用 read(\"nope.txt\") 确认路径，或 write(\"nope.txt\", content) 创建文件。"
    )


async def test_edit_on_directory_returns_text(
    edit_tool, runtime: ToolRuntime, workspace: Path
) -> None:
    label = str((workspace / "adir").resolve())
    (workspace / "adir").mkdir()
    assert (
        await edit_tool.ainvoke({"path": "adir", "old_str": "a", "new_str": "b"})
        == f"无法编辑 {label}: 是目录，不是文件"
    )

    await runtime.vfs.write("/vdir/keep.md", Text("x"))
    assert (
        await edit_tool.ainvoke({"path": "agent://vdir", "old_str": "a", "new_str": "b"})
        == "无法编辑 agent://vdir: 是目录，不是文件"
    )


async def test_edit_preserves_crlf(edit_tool, workspace: Path) -> None:
    target = workspace / "crlf.txt"
    target.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    output = await edit_tool.ainvoke(
        {"path": "crlf.txt", "old_str": "line2", "new_str": "LINE2"}
    )
    assert "(1 处替换)" in output
    assert target.read_bytes() == b"line1\r\nLINE2\r\nline3\r\n"


async def test_edit_keeps_lf_for_lf_file(edit_tool, workspace: Path) -> None:
    target = workspace / "lf.txt"
    target.write_bytes(b"line1\nline2\n")
    await edit_tool.ainvoke({"path": "lf.txt", "old_str": "line2", "new_str": "LINE2"})
    assert target.read_bytes() == b"line1\nLINE2\n"


async def test_edit_vfs_node(edit_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/notes/x.md", Text("alpha\nbeta\n"))
    output = await edit_tool.ainvoke(
        {"path": "agent://notes/x.md", "old_str": "beta", "new_str": "gamma"}
    )
    assert output == "已编辑 agent://notes/x.md (1 处替换)"
    assert await runtime.vfs.read_text("/notes/x.md") == "alpha\ngamma\n"


async def test_edit_vfs_missing_node(edit_tool) -> None:
    output = await edit_tool.ainvoke(
        {"path": "agent://nope.md", "old_str": "a", "new_str": "b"}
    )
    assert output == "文档不存在: agent://nope.md"


async def test_edit_vfs_multi_match(edit_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/notes/x.md", Text("foo\nfoo\n"))
    output = await edit_tool.ainvoke(
        {"path": "agent://notes/x.md", "old_str": "foo", "new_str": "bar"}
    )
    assert "匹配 2 处（要求恰好 1 处）" in output
    assert await runtime.vfs.read_text("/notes/x.md") == "foo\nfoo\n"


async def test_edit_vfs_handler_ack(edit_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/post.md", Text("beta"))
    node = await runtime.vfs.stat("/post.md")
    assert node is not None
    node.edit_handler = lambda target, old, kwargs: "已改为 gamma"
    node.write_handler = lambda target, content: pytest.fail("edit 不应触发 write_handler")

    output = await edit_tool.ainvoke(
        {"path": "agent://post.md", "old_str": "beta", "new_str": "gamma"}
    )
    assert output == "已编辑 agent://post.md (1 处替换)\n↳ 已改为 gamma"


async def test_edit_rejects_selector(edit_tool, workspace: Path) -> None:
    (workspace / "note.txt").write_bytes(b"foo\n")
    output = await edit_tool.ainvoke(
        {"path": "note.txt:1", "old_str": "foo", "new_str": "bar"}
    )
    assert output.startswith("edit: ")
    assert "行选择器" in output


async def test_edit_handler_error_is_readable(edit_tool, runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/post.md", Text("beta"))
    node = await runtime.vfs.stat("/post.md")
    assert node is not None

    def edit_handler(target, old, kwargs):
        raise ValueError("节点拒绝这次编辑")

    node.edit_handler = edit_handler
    output = await edit_tool.ainvoke(
        {"path": "agent://post.md", "old_str": "beta", "new_str": "gamma"}
    )
    assert output == "无法写入 agent://post.md: 节点拒绝这次编辑"


async def test_edit_readonly_mount_raises(edit_tool, runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="skills")
    await sub.write("/demo/SKILL.md", Text("hello"))
    runtime.vfs.mount_vfs("/skills", sub, readonly=True)

    with pytest.raises(VfsReadOnlyError):
        await edit_tool.ainvoke(
            {"path": "skills://demo/SKILL.md", "old_str": "hello", "new_str": "bye"}
        )
    assert await sub.read_text("/demo/SKILL.md") == "hello"


def test_replace_exact_kernel() -> None:
    assert replace_exact("a b", "b", "c") == ("a c", 1)
    assert replace_exact("a b b", "b", "c") == (None, 2)
    assert replace_exact("a b", "z", "c") == (None, 0)
    # new 为空串 = 删除
    assert replace_exact("a b", " b", "") == ("a", 1)
