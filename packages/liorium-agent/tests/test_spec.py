"""``liorium.agent.spec`` 的回归测试（计划 §4.2 指纹 / §3.9 装配）。

两个核心契约：

1. **指纹只覆盖结构**：``model`` / ``prompt_sources`` / ``realms``（含 params）/
   ``toolsets`` / ``limits`` 参与指纹；``id`` / ``inbox_dir`` / ``boot`` 是身份与引导语，
   **不**参与。改结构必须变指纹（否则 reconcile 不会 apply），改身份必须不变
   （否则每次重启都全量重建）。
2. **spec 只装能被读出来的东西**：``params`` 不可序列化就报错，不许静默放过
   （``repr`` 里的 ``id`` 会让同一结构产生不同指纹）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from liorium.agent.spec import AgentSpec, BootSpec, Limits, RealmSpec
from liorium.core.canonical import fingerprint
from liorium.core.errors import SpecError


def make_spec(**overrides: object) -> AgentSpec:
    """一份"最小可跑"的 spec；测试只覆盖关心的字段。"""
    base: dict[str, object] = {
        "id": "main",
        "inbox_dir": Path("D:/liorium/agents/main"),
        "model": "provider::model-a",
        "prompt_sources": ("AGENT.md", "ROLE.md"),
        "realms": (
            RealmSpec(id="memory", params={"top_k": 5}),
            RealmSpec(id="skills"),
        ),
        "toolsets": {"core": ("read", "write")},
        "limits": Limits(),
        "boot": BootSpec(message="初次见面，介绍一下你自己"),
    }
    base.update(overrides)
    return AgentSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 指纹稳定性
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_instances() -> None:
    """同一结构两次构造 → 同一指纹（CI 断言"重复 apply 零变更"）。"""
    assert make_spec().fingerprint() == make_spec().fingerprint()


def test_fingerprint_is_stable_when_only_declaration_style_changes() -> None:
    """dict / tuple 字面量写法不同但结构相同 → 指纹必须相同。"""
    left = make_spec(toolsets={"core": ("read", "write")})
    right = make_spec(toolsets={"core": ["read", "write"]})
    assert left.fingerprint() == right.fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "provider::model-b"},
        {"realms": (RealmSpec(id="memory", params={"top_k": 9}), RealmSpec(id="skills"))},
        {"realms": (RealmSpec(id="memory", params={"top_k": 5}),)},
        {"toolsets": {"core": ("read",)}},
        {"toolsets": {"core": ("read", "write"), "memory": ("recall",)}},
        {"limits": Limits(max_turns=10)},
        {"prompt_sources": ("AGENT.md",)},
    ],
    ids=[
        "model",
        "realm-params",
        "realm-list",
        "toolset-tools",
        "toolset-extra",
        "limits",
        "prompt-sources",
    ],
)
def test_structural_change_changes_fingerprint(overrides: dict[str, object]) -> None:
    """任何结构性改动都必须变指纹，否则 reconcile 不会重建部件。"""
    assert make_spec(**overrides).fingerprint() != make_spec().fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "another-agent"},
        {"inbox_dir": Path("D:/agent/agents/other")},
        {"boot": BootSpec(message="换一句引导语")},
        {"boot": None},
        {"workspace_root": Path("D:/agent/workspace")},
    ],
    ids=["id", "inbox-dir", "boot-message", "boot-none", "workspace-root"],
)
def test_identity_and_boot_do_not_change_fingerprint(overrides: dict[str, object]) -> None:
    """身份（id / 目录 / 工作区根）与引导语不进指纹：改它们不该触发全量重建。"""
    assert make_spec(**overrides).fingerprint() == make_spec().fingerprint()


# ---------------------------------------------------------------------------
# 分部指纹
# ---------------------------------------------------------------------------


def test_part_fingerprints_key_set_is_frozen() -> None:
    """``apply()`` 的差量替换靠这五个键，增删即破坏 reconcile。"""
    assert set(make_spec().part_fingerprints()) == {
        "model",
        "prompt",
        "realms",
        "toolsets",
        "limits",
    }


def test_part_fingerprints_match_individual_fingerprints() -> None:
    spec = make_spec()
    parts = spec.part_fingerprints()
    assert parts["model"] == fingerprint(spec.model)
    assert parts["prompt"] == fingerprint(spec.prompt_sources)
    assert parts["realms"] == fingerprint(spec.realms)
    assert parts["toolsets"] == fingerprint(spec.toolsets)
    assert parts["limits"] == fingerprint(spec.limits)


def test_only_the_changed_part_fingerprint_changes() -> None:
    before = make_spec().part_fingerprints()
    after = make_spec(limits=Limits(max_turns=42)).part_fingerprints()

    assert before["limits"] != after["limits"]
    for key in ("model", "prompt", "realms", "toolsets"):
        assert before[key] == after[key]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_duplicate_realm_id_is_rejected() -> None:
    with pytest.raises(SpecError):
        make_spec(
            realms=(RealmSpec(id="memory"), RealmSpec(id="skills"), RealmSpec(id="memory"))
        )


def test_unserializable_realm_params_are_rejected() -> None:
    """``params`` 会进指纹：lambda / 连接对象必须在构造期炸掉。"""
    with pytest.raises(SpecError) as excinfo:
        RealmSpec(id="weather", params={"callback": lambda: None})
    assert "weather" in str(excinfo.value)
    assert "params" in str(excinfo.value)


def test_realm_spec_normalizes_params_and_requires() -> None:
    params: dict[str, object] = {"k": 1}
    realm = RealmSpec(id="memory", params=params, requires=["core"])
    params["k"] = 2  # 外部改动不许影响已建 spec

    assert realm.params == {"k": 1}
    assert realm.requires == ("core",)
    assert isinstance(realm.requires, tuple)


def test_empty_realm_id_is_rejected() -> None:
    with pytest.raises(SpecError):
        RealmSpec(id="")


def test_empty_agent_id_is_rejected() -> None:
    with pytest.raises(SpecError):
        make_spec(id="")


def test_empty_toolset_name_is_rejected() -> None:
    with pytest.raises(SpecError):
        make_spec(toolsets={"": ("read",)})


def test_non_realm_spec_entry_is_rejected() -> None:
    with pytest.raises(SpecError):
        make_spec(realms=("memory",))


@pytest.mark.parametrize(
    "limits",
    [
        {"max_turns": 0},
        {"max_turns": -1},
        {"compact_threshold_ratio": 0.0},
        {"compact_threshold_ratio": 1.5},
        {"compact_keep_rounds": 0},
        {"context_window": -1},
    ],
)
def test_limits_validation(limits: dict[str, object]) -> None:
    with pytest.raises(SpecError):
        Limits(**limits)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 派生属性与变换
# ---------------------------------------------------------------------------


def test_workspace_root_defaults_to_inbox_dir() -> None:
    """计划 §3.9：``workspace_root`` 默认 = ``inbox_dir``（裸路径解析的边界）。"""
    spec = make_spec()
    assert spec.workspace_root is None
    assert spec.effective_workspace_root == Path("D:/liorium/agents/main")
    assert spec.effective_workspace_root == spec.inbox_dir


def test_explicit_workspace_root_wins() -> None:
    spec = make_spec(workspace_root=Path("D:/work"))
    assert spec.effective_workspace_root == Path("D:/work")
    assert spec.inbox_dir == Path("D:/liorium/agents/main")


def test_realm_lookup() -> None:
    spec = make_spec()
    assert spec.realm_ids == ("memory", "skills")
    assert spec.realm("skills").id == "skills"

    with pytest.raises(SpecError):
        spec.realm("mcp")


def test_with_realms_returns_new_spec_and_changes_fingerprint() -> None:
    spec = make_spec()
    changed = spec.with_realms((RealmSpec(id="skills"),))

    assert changed is not spec
    assert changed.id == spec.id
    assert changed.fingerprint() != spec.fingerprint()
    assert spec.realm_ids == ("memory", "skills")  # 原 spec 不可变


def test_with_toolsets_and_with_limits_are_pure() -> None:
    spec = make_spec()
    with_toolsets = spec.with_toolsets({"core": ("read",)})
    with_limits = spec.with_limits(Limits(max_turns=7))

    assert with_toolsets.toolsets == {"core": ("read",)}
    assert spec.toolsets == {"core": ("read", "write")}
    assert with_limits.limits.max_turns == 7
    assert spec.limits.max_turns == Limits().max_turns


def test_spec_is_frozen() -> None:
    spec = make_spec()
    with pytest.raises(FrozenInstanceError):
        spec.model = "other"  # type: ignore[misc]
