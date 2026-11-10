"""system prompt 拼装：分段顺序 / 缺失文件报错 / 指纹。"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.agent.runtime.prompt import (
    BASE_RULES,
    TAIL_NOTE,
    build_system_prompt,
    load_prompt_sources,
)
from liorium.agent.spec import AgentSpec, Limits
from liorium.core.errors import SpecError


def spec_for(tmp_path: Path, sources=("AGENT.md",)) -> AgentSpec:
    (tmp_path / "AGENT.md").write_text("身份：测试角色", encoding="utf-8")
    (tmp_path / "ROLE.md").write_text("规则：简短", encoding="utf-8")
    return AgentSpec(id="a", inbox_dir=tmp_path, model="m", prompt_sources=sources)


def test_section_order(tmp_path: Path) -> None:
    spec = spec_for(tmp_path, ("AGENT.md", "ROLE.md"))
    prompt = build_system_prompt(
        spec,
        realm_fragments=[("weather", "先查天气。")],
        skills=["pdf"],
    )
    names = [name for name, _text in prompt.sections]
    assert names == ["identity", "rules", "realms", "skills", "tail"]
    assert prompt.text.index("测试角色") < prompt.text.index(BASE_RULES)
    assert prompt.text.index(BASE_RULES) < prompt.text.index("先查天气")
    assert prompt.text.index("先查天气") < prompt.text.index("pdf")
    assert prompt.text.endswith(TAIL_NOTE)


def test_missing_prompt_file_raises(tmp_path: Path) -> None:
    spec = AgentSpec(id="a", inbox_dir=tmp_path, model="m", prompt_sources=("NOPE.md",))
    with pytest.raises(SpecError):
        load_prompt_sources(spec)


def test_fingerprint_changes_with_fragment(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    a = build_system_prompt(spec, realm_fragments=[("w", "x")])
    b = build_system_prompt(spec, realm_fragments=[("w", "y")])
    assert a.fingerprint() != b.fingerprint()
    assert (
        build_system_prompt(spec, realm_fragments=[("w", "x")]).fingerprint()
        == a.fingerprint()
    )


def test_empty_fragments_are_skipped(tmp_path: Path) -> None:
    spec = spec_for(tmp_path)
    prompt = build_system_prompt(spec, realm_fragments=[("w", "   ")], skills=[])
    names = [name for name, _ in prompt.sections]
    assert "realms" not in names and "skills" not in names


def test_limits_defaults() -> None:
    limits = Limits()
    assert limits.max_turns > 0
    assert 0 < limits.compact_threshold_ratio <= 1
