"""``liorium-core-tool`` 测试共用夹具。

真实的 ``OutputStore`` 属于 ``liorium-agent`` 且尚未实现，这里用满足
``liorium.core.ports.ArtifactStore`` 协议的内存替身；工具本身不写 store
（截断由 Agent 的包装层负责，§4.6）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from liorium.core.types import Text
from liorium.core_tool.edit import make_edit_tool
from liorium.core_tool.execute import make_execute_tool
from liorium.core_tool.find import make_find_tool
from liorium.core_tool.runtime import ToolRuntime
from liorium.core_tool.search import make_search_tool
from liorium.core_tool.write import make_write_tool
from liorium.vfs import VirtualFileSystem


class FakeArtifactStore:
    """内存版 ``ArtifactStore``：只满足协议，行为与真实 store 的摘要逻辑无关。"""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}
        self._counter = 0

    def put(
        self,
        text: str,
        *,
        tool_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        self._counter += 1
        artifact_id = f"fake_{self._counter}"
        self._items[artifact_id] = text
        return artifact_id

    def put_multimodal(self, payload: Mapping[str, Any], *, tool_name: str = "") -> str:
        return self.put(str(payload.get("text", "")), tool_name=tool_name)

    def get(self, artifact_id: str) -> str | None:
        return self._items.get(artifact_id)

    def summary(self, artifact_id: str) -> str:
        return self._items.get(artifact_id, f"[找不到 artifact: {artifact_id}]")

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._items


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Agent 工作区（裸路径的基准目录）。"""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def store() -> FakeArtifactStore:
    return FakeArtifactStore()


@pytest.fixture
def vfs() -> VirtualFileSystem:
    return VirtualFileSystem(owner_id="test")


@pytest.fixture
def runtime(workspace: Path, vfs: VirtualFileSystem, store: FakeArtifactStore) -> ToolRuntime:
    return ToolRuntime(vfs=vfs, workspace_root=workspace, artifacts=store)


@pytest.fixture
def write_tool(runtime: ToolRuntime):
    return make_write_tool(runtime)


@pytest.fixture
def edit_tool(runtime: ToolRuntime):
    return make_edit_tool(runtime)


@pytest.fixture
def execute_tool(runtime: ToolRuntime):
    return make_execute_tool(runtime)


@pytest.fixture
def search_tool(runtime: ToolRuntime):
    return make_search_tool(runtime)


@pytest.fixture
def find_tool(runtime: ToolRuntime):
    return make_find_tool(runtime)


@pytest.fixture
async def tree_vfs(vfs: VirtualFileSystem) -> VirtualFileSystem:
    """search/find 用的 VFS 小树（含投影节点与目录）。

    结构::

        /notes/a.md          "alpha\\nfoo\\n"
        /notes/b.md          "foo\\nfoo\\n"
        /notes/dir/keep.txt  "foo\\n"
        /notes/project.md    "foo projection"（should_be_included_in_search=False）
        /notes/blob.bin      Binary("foo")（非文本，search/find 跳过）
    """
    from liorium.core.types import Binary

    await vfs.write("/notes/a.md", Text("alpha\nfoo\n"))
    await vfs.write("/notes/b.md", Text("foo\nfoo\n"))
    await vfs.write("/notes/dir/keep.txt", Text("foo\n"))
    await vfs.write("/notes/project.md", Text("foo projection"))
    projection = await vfs.stat("/notes/project.md")
    assert projection is not None
    projection.should_be_included_in_search = False
    await vfs.write("/notes/blob.bin", Binary(b"foo\x00", "application/octet-stream"))
    return vfs
