"""profile 语法糖（计划 §C2 / §Phase 2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.agent import AgentSpec, Limits, Profile, RealmSpec
from liorium.core.errors import SpecError


def make_spec(**kwargs) -> AgentSpec:
    base = dict(
        id="p",
        inbox_dir=Path("agents/p"),
        model="fake::chat",
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="memory"),),
        toolsets={"default": ("read",)},
    )
    base.update(kwargs)
    return AgentSpec(**base)


def test_profile_expands_realms_prompt_and_toolsets() -> None:
    profile = Profile(
        id="chat",
        realms=(RealmSpec(id="skills"), RealmSpec(id="memory", params={"top_k": 3})),
        prompt_sources=("ROLE.md",),
        toolsets={"sub": ("read", "search")},
    )
    spec = make_spec()
    merged = spec.with_profile(profile)

    # profile 的 realm 在前；同 id 时以 spec 显式声明为准
    assert merged.realm_ids == ("skills", "memory")
    assert merged.realm("memory").params == {}  # spec 的 memory 覆盖 profile 的
    assert merged.prompt_sources == ("ROLE.md", "AGENT.md")
    assert merged.toolsets == {"sub": ("read", "search"), "default": ("read",)}
    # 原 spec 不变
    assert spec.realm_ids == ("memory",)
    assert spec.prompt_sources == ("AGENT.md",)


def test_profile_keeps_extra_realms_after_profile_ones() -> None:
    profile = Profile(id="chat", realms=(RealmSpec(id="skills"),))
    spec = make_spec(realms=(RealmSpec(id="memory"), RealmSpec(id="mcp")))
    merged = spec.with_profile(profile)
    assert merged.realm_ids == ("skills", "memory", "mcp")


def test_profile_changes_fingerprint() -> None:
    spec = make_spec()
    merged = spec.with_profile(Profile(id="chat", realms=(RealmSpec(id="skills"),)))
    assert merged.fingerprint() != spec.fingerprint()
    assert merged.part_fingerprints()["realms"] != spec.part_fingerprints()["realms"]


def test_empty_profile_is_noop() -> None:
    spec = make_spec()
    merged = spec.with_profile(Profile(id="noop"))
    assert merged.fingerprint() == spec.fingerprint()
    assert merged.realms == spec.realms


def test_profile_rejects_duplicate_realms() -> None:
    with pytest.raises(SpecError, match="realm id 重复"):
        Profile(id="bad", realms=(RealmSpec(id="a"), RealmSpec(id="a")))


def test_profile_rejects_non_realm_spec() -> None:
    with pytest.raises(SpecError, match="非 RealmSpec"):
        Profile(id="bad", realms=("skills",))  # type: ignore[arg-type]


def test_profile_requires_id() -> None:
    with pytest.raises(SpecError, match="Profile.id"):
        Profile(id="")


def test_with_profile_requires_profile_instance() -> None:
    with pytest.raises(SpecError, match="需要 Profile"):
        make_spec().with_profile("chat")  # type: ignore[arg-type]


def test_profile_merges_into_limits_untouched() -> None:
    spec = make_spec(limits=Limits(max_turns=7))
    merged = spec.with_profile(Profile(id="chat", realms=(RealmSpec(id="skills"),)))
    assert merged.limits == spec.limits
