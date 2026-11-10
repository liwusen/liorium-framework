"""``ToolRuntime``：路径解析 / 文件 helper / VFS helper / 进程端口。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from liorium.core.errors import ConfigError, VfsError, VfsReadOnlyError
from liorium.core.types import Text
from liorium.core_tool.execute import make_execute_tool
from liorium.core_tool.runtime import (
    ROOT_SCHEME,
    ToolRuntime,
    decode_text,
    detect_newline,
    encode_text,
)
from liorium.vfs import VirtualFileSystem


# ---------------------------------------------------------------------------
# 路径解析（§3.6 规则 4）
# ---------------------------------------------------------------------------


def test_bare_path_is_relative_to_workspace(runtime: ToolRuntime, workspace: Path) -> None:
    resolved = runtime.resolve_path("notes/todo.md")
    expected = (workspace / "notes" / "todo.md").resolve()
    assert resolved.is_vfs is False
    assert resolved.scheme == "file"
    assert Path(resolved.path) == expected
    assert resolved.display == str(expected)


def test_empty_path_is_workspace_root(runtime: ToolRuntime, workspace: Path) -> None:
    resolved = runtime.resolve_path("")
    assert resolved.is_vfs is False
    assert Path(resolved.path) == workspace.resolve()


def test_file_uri_is_absolute(runtime: ToolRuntime, tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("x", encoding="utf-8")
    uri = "file:///" + target.as_posix().lstrip("/")
    resolved = runtime.resolve_path(uri)
    assert resolved.is_vfs is False
    assert Path(resolved.path) == target.resolve()


def test_agent_scheme_maps_to_vfs_root(runtime: ToolRuntime) -> None:
    assert ROOT_SCHEME == "agent"
    resolved = runtime.resolve_path("agent://notes/todo.md")
    assert resolved.is_vfs is True
    assert resolved.path == "/notes/todo.md"
    assert resolved.display == "agent://notes/todo.md"


def test_scheme_matches_mount_point(runtime: ToolRuntime) -> None:
    resolved = runtime.resolve_path("skills://demo/SKILL.md")
    assert resolved.is_vfs is True
    assert resolved.path == "/skills/demo/SKILL.md"
    assert resolved.display == "skills://demo/SKILL.md"


# ---------------------------------------------------------------------------
# 真实文件 helper
# ---------------------------------------------------------------------------


async def test_write_bytes_creates_parent_dirs(runtime: ToolRuntime, workspace: Path) -> None:
    resolved = runtime.resolve_path("a/b/c.bin")
    await runtime.write_bytes(resolved, b"\x00\x01")
    assert (workspace / "a" / "b" / "c.bin").read_bytes() == b"\x00\x01"
    assert await runtime.read_bytes(resolved) == b"\x00\x01"


async def test_read_text_uses_universal_newlines(runtime: ToolRuntime, workspace: Path) -> None:
    target = workspace / "crlf.txt"
    target.write_bytes(b"a\r\nb\r\nc")
    resolved = runtime.resolve_path("crlf.txt")
    assert await runtime.read_text(resolved) == "a\nb\nc"


async def test_write_text_keeps_requested_newline(runtime: ToolRuntime) -> None:
    resolved = runtime.resolve_path("crlf.txt")
    await runtime.write_text(resolved, "a\nb\n", newline="\r\n")
    assert Path(resolved.path).read_bytes() == b"a\r\nb\r\n"
    text, newline = await runtime.read_text_with_newline(resolved)
    assert text == "a\nb\n"
    assert newline == "\r\n"


def test_newline_helpers() -> None:
    assert detect_newline(b"a\r\nb") == "\r\n"
    assert detect_newline(b"a\nb") == "\n"
    assert encode_text("a\nb", "\r\n") == b"a\r\nb"
    assert decode_text(b"a\r\nb") == "a\nb"


# ---------------------------------------------------------------------------
# VFS helper（只读 / 挂载 / ACK）
# ---------------------------------------------------------------------------


async def test_read_through_readonly_mount(runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="skills")
    await sub.write("/demo/SKILL.md", Text("hello"))
    runtime.vfs.mount_vfs("/skills", sub, readonly=True)

    resolved = runtime.resolve_path("skills://demo/SKILL.md")
    assert resolved.path == "/skills/demo/SKILL.md"
    assert await runtime.vfs_read_text(resolved.path) == "hello"


async def test_write_to_readonly_mount_raises(runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="skills")
    await sub.write("/demo/SKILL.md", Text("hello"))
    runtime.vfs.mount_vfs("/skills", sub, readonly=True)

    with pytest.raises(VfsReadOnlyError):
        await runtime.vfs_write_text("/skills/demo/SKILL.md", "hacked")
    assert await sub.read_text("/demo/SKILL.md") == "hello"


async def test_vfs_write_returns_handler_ack(runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/task.md", Text(""))
    node = await runtime.vfs.stat("/task.md")
    assert node is not None
    seen: list[Text] = []

    def handler(target, content):
        seen.append(content)
        return "已提交 task_7"

    node.write_handler = handler
    ack = await runtime.vfs_write_text("/task.md", "正文")
    assert ack == "已提交 task_7"
    assert seen == [Text("正文")]


async def test_vfs_write_ignores_non_str_ack(runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/task.md", Text(""))
    node = await runtime.vfs.stat("/task.md")
    assert node is not None
    node.write_handler = lambda target, content: {"ok": True}
    assert await runtime.vfs_write_text("/task.md", "x") is None


async def test_vfs_write_creates_missing_node(runtime: ToolRuntime) -> None:
    assert await runtime.vfs_write_text("/notes/new.md", "fresh") is None
    assert await runtime.vfs_read_text("/notes/new.md") == "fresh"


async def test_vfs_write_rejects_projection_node(runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/projected.md", Text("truth"))
    node = await runtime.vfs.stat("/projected.md")
    assert node is not None
    node.write_handler = lambda target, content: None  # 有 handler 的节点不替换内容

    # 有 handler 的节点允许写（副作用由 handler 负责）
    assert await runtime.vfs_write_text("/projected.md", "x") is None

    # 没有 handler 的投影节点不允许覆盖
    node2 = await runtime.vfs.mkdir("/realms")
    assert node2 is not None
    await runtime.vfs.write("/realms/state.md", Text("投影"))
    projected = await runtime.vfs.stat("/realms/state.md")
    assert projected is not None
    projected.provider = lambda target: Text("derived")
    with pytest.raises(VfsError):
        await runtime.vfs_write_text("/realms/state.md", "overwrite")


async def test_write_through_writable_mount(runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="work")
    await sub.write("/demo/a.md", Text("x"))
    runtime.vfs.mount_vfs("/mnt", sub, readonly=False)

    assert await runtime.vfs_write_text("/mnt/demo/a.md", "y") is None
    assert await sub.read_text("/demo/a.md") == "y"
    assert await runtime.vfs_read_text("/mnt/demo/a.md") == "y"


async def test_create_inside_writable_mount_lands_in_mounted_vfs(runtime: ToolRuntime) -> None:
    """挂载子树内新建节点必须落到被挂载的 VFS（VFS 负责委托，不静默丢失）。"""
    sub = VirtualFileSystem(owner_id="work")
    runtime.vfs.mount_vfs("/mnt", sub, readonly=False)

    assert await runtime.vfs_write_text("/mnt/new.md", "x") is None
    assert await sub.read_text("/new.md") == "x"
    assert await runtime.vfs_read_text("/mnt/new.md") == "x"


async def test_create_inside_readonly_mount_raises_readonly(runtime: ToolRuntime) -> None:
    sub = VirtualFileSystem(owner_id="work")
    runtime.vfs.mount_vfs("/mnt", sub, readonly=True)

    with pytest.raises(VfsReadOnlyError):
        await runtime.vfs_write_text("/mnt/new.md", "x")


async def test_create_under_readonly_directory_raises(runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/locked/keep.md", Text("x"))
    locked = await runtime.vfs.stat("/locked")
    assert locked is not None
    locked.writable = False

    with pytest.raises(VfsReadOnlyError):
        await runtime.vfs_write_text("/locked/new.md", "x")
    assert runtime.vfs.exists("/locked/new.md") is False


async def test_vfs_edit_dispatches_edit_handler(runtime: ToolRuntime) -> None:
    await runtime.vfs.write("/post.md", Text("beta"))
    node = await runtime.vfs.stat("/post.md")
    assert node is not None
    calls: list[tuple[str, dict]] = []

    def edit_handler(target, old, kwargs):
        calls.append((old, kwargs))
        return "已改为 gamma"

    node.edit_handler = edit_handler
    node.write_handler = lambda target, content: pytest.fail("不应触发 write_handler")
    ack = await runtime.vfs_edit_text("/post.md", "beta", "gamma")
    assert ack == "已改为 gamma"
    assert calls == [("beta", {"new": "gamma"})]


# ---------------------------------------------------------------------------
# 端口 / 参数校验
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """记录调用参数的 ``ProcessRunner`` 替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, command, *, cwd=None, timeout=None, env=None):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        return 0, f"ran: {command}", ""


async def test_process_runner_port_is_used(
    workspace: Path, vfs: VirtualFileSystem, store
) -> None:
    runner = _RecordingRunner()
    runtime = ToolRuntime(
        vfs=vfs,
        workspace_root=workspace,
        artifacts=store,
        process_runner=runner,
        env={"LIORIUM_TEST_ENV": "1"},
        default_timeout=42.0,
    )
    tool = make_execute_tool(runtime)
    output = await tool.ainvoke({"command": "hello"})
    assert output.startswith("[Shell: ")
    assert output.split("\n", 1)[1] == "ran: hello"
    assert runner.calls[0]["command"] == "hello"
    assert Path(str(runner.calls[0]["cwd"])) == workspace.resolve()
    assert runner.calls[0]["timeout"] == 42.0
    assert runner.calls[0]["env"] == {"LIORIUM_TEST_ENV": "1"}


def test_artifacts_must_implement_protocol(workspace: Path, vfs: VirtualFileSystem) -> None:
    with pytest.raises(ConfigError):
        ToolRuntime(vfs=vfs, workspace_root=workspace, artifacts=object())


def test_limits_are_validated(workspace: Path, vfs: VirtualFileSystem, store) -> None:
    with pytest.raises(ConfigError):
        ToolRuntime(vfs=vfs, workspace_root=workspace, artifacts=store, max_output_chars=-1)
    with pytest.raises(ConfigError):
        ToolRuntime(vfs=vfs, workspace_root=workspace, artifacts=store, default_timeout=0)


# ---------------------------------------------------------------------------
# tools.py：按名字装配（计划 §F1）
# ---------------------------------------------------------------------------


def test_build_core_tools_default_order(runtime: ToolRuntime) -> None:
    from liorium.core_tool.tools import build_core_tools

    try:  # read 由另一位同事实现，可能尚未合入
        from liorium.core_tool.read import build_read_tool  # noqa: F401

        read_available = True
    except ImportError:
        read_available = False

    names = [tool.name for tool in build_core_tools(runtime)]
    expected = (["read"] if read_available else []) + [
        "write",
        "edit",
        "search",
        "find",
        "execute",
    ]
    assert names == expected


def test_build_core_tools_description_is_chinese(runtime: ToolRuntime) -> None:
    from liorium.core_tool.tools import build_core_tools

    for tool in build_core_tools(runtime):
        assert tool.description and any("\u4e00" <= ch <= "\u9fff" for ch in tool.description)


def test_build_core_tools_filters_and_dedupes(runtime: ToolRuntime) -> None:
    from liorium.core_tool.tools import build_core_tools

    tools = build_core_tools(runtime, names=["find", "write", "find"])
    assert [tool.name for tool in tools] == ["find", "write"]


def test_build_core_tools_unknown_name_raises(runtime: ToolRuntime) -> None:
    from liorium.core_tool.tools import build_core_tools

    with pytest.raises(ValueError):
        build_core_tools(runtime, names=["write", "nope"])


def test_build_core_tools_read_is_optional(runtime: ToolRuntime, caplog) -> None:
    """``read`` 模块缺席时跳过并 warning；存在时正常装配（S-READ 随时可能合入）。"""
    import logging

    from liorium.core_tool.tools import build_core_tools

    with caplog.at_level(logging.WARNING, logger="liorium.core_tool.tools"):
        tools = build_core_tools(runtime, names=["read"])
    if tools:
        assert [tool.name for tool in tools] == ["read"]
    else:
        assert any("read" in record.getMessage() for record in caplog.records)


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 有 \\\\?\\ 前缀")
def test_extended_length_workspace_root_is_normalized(
    workspace: Path, vfs: VirtualFileSystem, store
) -> None:
    """2026-11-08：``write`` 回显时而是 ``D:\\x``、时而是 ``\\\\?\\D:\\x``。

    只要 ``workspace_root`` 带了前缀，所有派生路径都会跟着带 —— 读/写/edit 必须统一。
    """
    runtime = ToolRuntime(
        vfs=vfs,
        workspace_root=Path("\\\\?\\" + str(workspace)),
        artifacts=store,
    )
    assert str(runtime.workspace_root) == str(workspace.resolve())
    resolved = runtime.resolve_path("sub/a.txt")
    assert not resolved.path.startswith("\\\\?\\")
    assert Path(resolved.path) == workspace.resolve() / "sub" / "a.txt"
