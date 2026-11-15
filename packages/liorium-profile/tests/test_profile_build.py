"""build：profile → AgentSpec + 工厂 + 池；优先级与路径（计划 §3.1 / §3.4 / §8.2）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from liorium.agent import LioriumAgent
from liorium.profile import AgentProfile, ProfileBuildError, from_dict
from liorium.provider import ModelProvider, ModelProviders, ProviderRegistry


def make_registry(
    *, main_model: str | None = "P::m", context_window: int = 4096
) -> ProviderRegistry:
    providers = ModelProviders(
        providers=[
            ModelProvider(
                name="P",
                base_url="https://x",
                key="sk-test",
                models=["m", "m2"],
                model_context_lengths={"m": context_window, "m2": context_window},
            )
        ],
        main_model=main_model,
    )
    return ProviderRegistry(providers)


def base_profile(tmp_path: Path, **updates: Any) -> AgentProfile:
    raw = {
        "version": 1,
        "id": "p",
        "realms": [{"id": "subagents"}],
    }
    raw.update(updates)
    return from_dict(raw, base_dir=tmp_path)


def test_build_maps_every_field(tmp_path: Path) -> None:
    profile = base_profile(
        tmp_path,
        workspace="ws",
        inbox="agent",
        prompt=["AGENT.md", "ROLE.md"],
        boot={"message": "hello"},
        limits={"max_turns": 7},
        toolsets={"default": ["read", "find"]},
    )
    build = profile.build(registry=make_registry())

    spec = build.spec
    assert spec.id == "p"
    assert spec.workspace_root == (tmp_path / "ws").resolve()
    assert spec.inbox_dir == (tmp_path / "ws" / "agent").resolve()
    assert spec.model == "P::m"
    assert spec.prompt_sources == ("AGENT.md", "ROLE.md")
    assert spec.realms[0].id == "subagents"
    assert spec.toolsets == {"default": ("read", "find")}
    assert spec.limits.max_turns == 7
    assert spec.limits.context_window == 4096  # 未显式给出 ⇒ 从 provider 补
    assert spec.boot is not None and spec.boot.message == "hello"


def test_workspace_and_inbox_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    profile = base_profile(tmp_path)
    spec = profile.build(registry=make_registry()).spec
    assert spec.workspace_root == tmp_path
    assert spec.inbox_dir == tmp_path / ".liorium" / "agent"


def test_model_precedence(tmp_path: Path) -> None:
    registry = make_registry()
    profile = base_profile(tmp_path)

    assert profile.build(registry=registry).spec.model == "P::m"  # main_model
    assert profile.build(registry=registry, model="P::m2").spec.model == "P::m2"
    assert (
        base_profile(tmp_path, model="P::m2").build(registry=registry).spec.model == "P::m2"
    )


def test_no_model_anywhere_raises(tmp_path: Path) -> None:
    registry = ProviderRegistry(ModelProviders())
    with pytest.raises(ProfileBuildError, match="没有可用模型"):
        base_profile(tmp_path).build(registry=registry)


def test_explicit_context_window_zero_wins(tmp_path: Path) -> None:
    profile = base_profile(tmp_path, limits={"context_window": 0})
    spec = profile.build(registry=make_registry(context_window=999_999)).spec
    assert spec.limits.context_window == 0


def test_provider_file_is_loaded_relative_to_profile_dir(tmp_path: Path) -> None:
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "P",
                        "base_url": "https://x",
                        "key": "sk-test",
                        "models": ["m"],
                    }
                ],
                "main_model": "P::m",
            }
        ),
        encoding="utf-8",
    )
    profile = base_profile(tmp_path, provider_file="providers.json")
    build = profile.build()
    assert build.spec.model == "P::m"
    assert build.registry.source == tmp_path / "providers.json"


def test_missing_provider_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileBuildError, match="provider"):
        base_profile(tmp_path).build()
    with pytest.raises(ProfileBuildError, match="不存在"):
        base_profile(tmp_path, provider_file="nope.json").build()


def test_fingerprint_is_stable(tmp_path: Path) -> None:
    profile = base_profile(tmp_path)
    first = profile.build(registry=make_registry()).spec.fingerprint()
    second = profile.build(registry=make_registry()).spec.fingerprint()
    assert first == second


def test_create_agent_ports(tmp_path: Path) -> None:
    build = base_profile(tmp_path).build(registry=make_registry())

    with pytest.raises(TypeError, match="spec"):
        build.create_agent(spec=build.spec)
    with pytest.raises(TypeError, match="model_factory"):
        build.create_agent(model_factory=lambda label: None)
    with pytest.raises(TypeError, match="realm_factories"):
        build.create_agent(realm_factories={})

    agent = build.create_agent()
    assert isinstance(agent, LioriumAgent)
    assert agent.spec is build.spec
