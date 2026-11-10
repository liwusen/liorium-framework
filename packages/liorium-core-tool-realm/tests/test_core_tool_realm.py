"""``CoreToolRealm``：工具子集 / prompt 段 / 生命周期 / 与 ``RealmContext`` 的接线。

这里只用 ``RealmContext`` 起 realm，**不 import ``liorium.agent``** —— 和 todo-realm 一样，
证明"加一个工具能力包"不需要背 Agent 运行时。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liorium.core.errors import RealmError
from liorium.core_tool.tools import CORE_TOOL_NAMES
from liorium.core_tool_realm import CORE_REALM_ID, CoreToolRealm
from liorium.realm_base import JsonStateStore, RealmContext
from liorium.vfs import VirtualFileSystem


class StubArtifacts:
    """最小 ``ArtifactStore``：``ToolRuntime`` 只做协议检查，测试不关心内容。"""

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self._seq = 0

    def put(self, text: str, *, tool_name: str = "", metadata: Any = None) -> str:
        self._seq += 1
        artifact_id = f"a{self._seq}"
        self.items[artifact_id] = text
        return artifact_id

    def put_multimodal(self, payload: Any, *, tool_name: str = "") -> str:
        return self.put(str(payload), tool_name=tool_name)

    def get(self, artifact_id: str) -> Any:
        return self.items.get(artifact_id)

    def summary(self, artifact_id: str) -> str:
        return str(self.items.get(artifact_id, ""))[:60]

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self.items


async def started(
    tmp_path: Path,
    *,
    params: dict[str, Any] | None = None,
    toolsets: dict[str, Any] | None = None,
    artifacts: Any = "__default__",
) -> tuple[CoreToolRealm, RealmContext]:
    realm = CoreToolRealm(SimpleNamespace(params=dict(params or {})))
    ctx = RealmContext(
        realm_id=CORE_REALM_ID,
        vfs=VirtualFileSystem(owner_id="test"),
        state=JsonStateStore(tmp_path / "realm_state" / "core.json", realm_id=CORE_REALM_ID),
        params=dict(params or {}),
        inbox_dir=tmp_path,
        workspace_root=tmp_path,
        artifacts=StubArtifacts() if artifacts == "__default__" else artifacts,
        agent_spec=SimpleNamespace(toolsets=dict(toolsets or {})),
    )
    # 与 RealmManager.start() 一致：先 load 再 start
    ctx.state.load()
    await realm.start(ctx)
    return realm, ctx


def names(realm: CoreToolRealm) -> list[str]:
    return [tool.name for tool in realm.tools()]


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------


async def test_default_is_the_six_core_tools(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    assert names(realm) == list(CORE_TOOL_NAMES)
    assert realm.id == "core"
    assert realm.instance_scope == "agent"


async def test_params_tools_selects_subset(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path, params={"tools": ["read", "find"]})
    assert names(realm) == ["read", "find"]


async def test_params_tools_accepts_comma_string(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path, params={"tools": "read, write ,read"})
    assert names(realm) == ["read", "write"]


async def test_agent_spec_toolsets_default_is_the_subset(tmp_path: Path) -> None:
    """``AgentSpec.toolsets["default"]`` 仍然是工具子集的既有语义（Q1 方案 A）。"""
    realm, _ctx = await started(tmp_path, toolsets={"default": ("search", "execute")})
    assert names(realm) == ["search", "execute"]


async def test_params_win_over_toolsets(tmp_path: Path) -> None:
    realm, _ctx = await started(
        tmp_path, params={"tools": ["read"]}, toolsets={"default": ("read", "write")}
    )
    assert names(realm) == ["read"]


async def test_empty_subset_means_no_tools(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path, toolsets={"default": ()})
    assert realm.tools() == []
    assert realm.prompt_fragment() == ""


async def test_unknown_tool_name_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知的核心工具名"):
        await started(tmp_path, params={"tools": ["write", "nope"]})


async def test_tools_param_type_error(tmp_path: Path) -> None:
    with pytest.raises(RealmError, match="需要字符串或字符串序列"):
        await started(tmp_path, params={"tools": 42})


async def test_stop_removes_tools(tmp_path: Path) -> None:
    realm, ctx = await started(tmp_path)
    assert names(realm)
    await realm.stop(ctx)
    assert realm.tools() == []


async def test_tools_returns_a_copy(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    listing = realm.tools()
    listing.clear()
    assert len(realm.tools()) == len(CORE_TOOL_NAMES)


# ---------------------------------------------------------------------------
# 接线：工作区根 / artifacts
# ---------------------------------------------------------------------------


async def test_read_uses_workspace_root(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    realm, _ctx = await started(tmp_path)
    read = next(tool for tool in realm.tools() if tool.name == "read")
    output = await read.ainvoke({"uri": "notes.txt"})
    assert "第一行" in str(output)


async def test_missing_artifacts_is_a_loud_error(tmp_path: Path) -> None:
    with pytest.raises(RealmError, match="artifacts 端口"):
        await started(tmp_path, artifacts=None)


async def test_artifacts_override_can_be_passed_to_constructor(tmp_path: Path) -> None:
    realm = CoreToolRealm(SimpleNamespace(params={}), artifacts=StubArtifacts())
    ctx = RealmContext(
        realm_id=CORE_REALM_ID,
        vfs=VirtualFileSystem(owner_id="test"),
        state=JsonStateStore(tmp_path / "realm_state" / "core.json", realm_id=CORE_REALM_ID),
        params={},
        inbox_dir=tmp_path,
        workspace_root=tmp_path,
        artifacts=None,
    )
    ctx.state.load()
    await realm.start(ctx)
    assert names(realm) == list(CORE_TOOL_NAMES)


async def test_invalid_max_output_chars_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RealmError, match="max_tool_output_chars"):
        await started(tmp_path, params={"max_tool_output_chars": -1})


# ---------------------------------------------------------------------------
# prompt 段
# ---------------------------------------------------------------------------


async def test_prompt_fragment_lists_present_tools(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path, params={"tools": ["read"]})
    fragment = realm.prompt_fragment()
    assert "read" in fragment
    assert "write" not in fragment
    assert "execute" not in fragment  # 没装就不吹
    assert "artifact://" in fragment


async def test_prompt_fragment_mentions_execute_when_present(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path, params={"tools": ["execute"]})
    assert "execute" in realm.prompt_fragment()


async def test_prompt_fragment_before_start_assumes_all(tmp_path: Path) -> None:
    realm = CoreToolRealm(SimpleNamespace(params={}))
    fragment = realm.prompt_fragment()
    for name in CORE_TOOL_NAMES:
        assert name in fragment
