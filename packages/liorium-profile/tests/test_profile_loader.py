"""loader：``~`` / ``${VAR}``、id 缺省、错误消息里的 JSON 路径（计划 D2/D3）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.profile import AgentProfile, ProfileSchemaError
from liorium.profile.loader import expand_environment, from_dict


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_env_expansion_is_recursive_and_errors_on_undefined() -> None:
    expanded = expand_environment(
        {"a": "${X}", "b": ["${X}", 1], "c": {"d": "${Y}"}},
        env={"X": "x", "Y": "y"},
    )
    assert expanded == {"a": "x", "b": ["x", 1], "c": {"d": "y"}}

    with pytest.raises(ProfileSchemaError, match=r"c\.d.*HOME"):
        expand_environment({"c": {"d": "${HOME}"}}, env={})


def test_dollar_escape() -> None:
    assert expand_environment("$${HOME}", env={}) == "${HOME}"
    assert expand_environment("a$$b", env={}) == "a$b"
    # 转义之后的 ${...} 不再被二次展开
    assert expand_environment("$$${X}", env={"X": "1"}) == "$1"


def test_load_uses_file_stem_for_id_and_expands_env(tmp_path: Path) -> None:
    path = write(
        tmp_path / "myagent.json",
        {
            "version": 1,
            "model": "${MODEL}",
            "realms": [{"id": "subagents"}],
        },
    )
    profile = AgentProfile.load(path, env={"MODEL": "P::m"})
    assert profile.id == "myagent"
    assert profile.model == "P::m"
    assert profile.base_dir == tmp_path.resolve()


def test_from_dict_requires_id_or_default_id() -> None:
    with pytest.raises(ProfileSchemaError, match="id"):
        from_dict({"version": 1})
    profile = from_dict({"version": 1}, default_id="x")
    assert profile.id == "x"


def test_missing_file_and_bad_json_raise(tmp_path: Path) -> None:
    with pytest.raises(ProfileSchemaError, match="不存在"):
        AgentProfile.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileSchemaError, match="JSON"):
        AgentProfile.load(bad)


def test_validation_error_carries_json_path(tmp_path: Path) -> None:
    path = write(
        tmp_path / "p.json",
        {"version": 1, "limits": {"max_turns": "many"}},
    )
    with pytest.raises(ProfileSchemaError, match=r"limits\.max_turns"):
        AgentProfile.load(path)


def test_top_level_must_be_object() -> None:
    with pytest.raises(ProfileSchemaError, match="顶层"):
        from_dict([], default_id="x")  # type: ignore[arg-type]
