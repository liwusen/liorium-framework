"""profile schema：严格性、必填项、与 ``Limits`` 的字段一致性（计划 D2 / B7）。"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from liorium.agent.spec import Limits
from liorium.profile import AgentProfile, LimitsConfig
from liorium.profile.schema import RealmEntry


def test_limits_config_mirrors_agent_limits() -> None:
    expected = {item.name: item.default for item in dataclasses.fields(Limits)}
    actual = {name: field.default for name, field in LimitsConfig.model_fields.items()}
    assert actual == expected


def test_version_is_required_and_must_be_one() -> None:
    with pytest.raises(ValidationError, match="version"):
        AgentProfile.model_validate({})
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({"version": 2})
    assert AgentProfile.model_validate({"version": 1}).version == 1


def test_unknown_keys_are_rejected_everywhere() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        AgentProfile.model_validate({"version": 1, "unknown": 1})
    with pytest.raises(ValidationError):
        AgentProfile.model_validate(
            {"version": 1, "realms": [{"id": "a", "nope": 1}]}
        )
    with pytest.raises(ValidationError):
        RealmEntry.model_validate({"id": "a", "config": {}, "extra": True})


def test_realm_ids_are_unique_and_non_blank() -> None:
    with pytest.raises(ValidationError, match="重复"):
        AgentProfile.model_validate(
            {"version": 1, "realms": [{"id": "a"}, {"id": "a"}]}
        )
    with pytest.raises(ValidationError):
        RealmEntry.model_validate({"id": ""})


def test_invalid_ids_and_prompt_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({"version": 1, "id": "  "})
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({"version": 1, "prompt": ["AGENT.md", " "]})
    with pytest.raises(ValidationError):
        AgentProfile.model_validate({"version": 1, "thinking": "max"})


def test_defaults() -> None:
    profile = AgentProfile.model_validate({"version": 1})
    assert profile.thinking == "medium"
    assert profile.temperature == 0.3
    assert profile.model is None
    assert profile.provider_file is None
    assert profile.prompt == []
    assert profile.realms == []
    assert profile.limits.context_window == 0
    assert profile.boot.message is None
