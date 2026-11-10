"""多 Agent 示例：调用方 semaphore 限并发 + 两个 Agent 并发跑。"""

from __future__ import annotations

from pathlib import Path

import pytest

from examples.multi_agent import ROLES, run_agents
from liorium_testkit import ScriptedChatModel


@pytest.mark.asyncio
async def test_run_agents_returns_two_outputs(tmp_path: Path) -> None:
    models: dict[str, ScriptedChatModel] = {}

    def factory(label: str) -> ScriptedChatModel:
        if label not in models:
            models[label] = ScriptedChatModel(responses=["好的，这是输出。"])
        return models[label]

    outputs = await run_agents(
        prompt="介绍一下你自己",
        workspace=tmp_path,
        model_factory=factory,
        model_label="fake::chat",
        concurrency=1,
    )

    assert set(outputs) == set(ROLES)
    assert all(text.strip() for text in outputs.values())
    for name in ROLES:
        assert (tmp_path / "agents" / name / "AGENT.md").is_file()


@pytest.mark.asyncio
async def test_run_agents_collects_events_and_uses_own_inbox(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def factory(label: str) -> ScriptedChatModel:
        return ScriptedChatModel(responses=["一", "二"])

    outputs = await run_agents(
        prompt="干活",
        workspace=tmp_path,
        model_factory=factory,
        model_label="fake::chat",
        concurrency=2,
        emit=lambda agent_id, event: seen.append((agent_id, event.type)),
    )
    assert set(outputs) == set(ROLES)
    assert {agent_id for agent_id, _ in seen} == set(ROLES)
    assert ("planner", "delta") in seen
    assert ("writer", "done") in seen
    for name in ROLES:
        inbox = tmp_path / "agents" / name
        assert (inbox / "checkpoint.db").is_file()
