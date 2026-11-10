"""真模型端到端（``@pytest.mark.live``，默认跳过）。

计划 Phase 1 验收 1：真模型单轮对话 + 工具循环。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from live_support import make_live_factory
from liorium.agent import AgentSpec, Limits, LioriumAgent

pytestmark = pytest.mark.live


def _spec(workspace: Path, model_label: str) -> AgentSpec:
    return AgentSpec(
        id="live-agent",
        inbox_dir=workspace,
        model=model_label,
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=200_000, max_turns=8),
        workspace_root=workspace,
    )


@pytest.mark.asyncio
async def test_live_single_turn(agent_workspace: Path) -> None:
    cfg = {"base_url": os.environ.get("OPENCODE_GO_BASE_URL"), "model": os.environ.get("OPENCODE_GO_MODEL", "deepseek-v4.1-flash")}
    agent = LioriumAgent(
        spec=_spec(agent_workspace, f"opencode-go::{cfg['model']}"),
        model_factory=make_live_factory(),
        api_format="openai_chat",
    )
    await agent.start()
    try:
        text = ""
        async for event in agent.run("用一句话回答：你是谁？"):
            if event.type == "delta":
                text += str(event.get("content") or "")
        assert text.strip(), "真模型没有返回任何内容"
        print("\n[live] 单轮回复:", text[:200])
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_live_tool_cycle(agent_workspace: Path) -> None:
    cfg = {"model": os.environ.get("OPENCODE_GO_MODEL", "deepseek-v4.1-flash")}
    agent = LioriumAgent(
        spec=_spec(agent_workspace, f"opencode-go::{cfg['model']}"),
        model_factory=make_live_factory(),
        api_format="openai_chat",
    )
    await agent.start()
    try:
        events = []
        async for event in agent.run(
            "读一下 notes.txt 这个文件，然后用一句话告诉我第一行是什么。"
        ):
            events.append(event)
        types = [e.type for e in events]
        assert types[-1] == "done"
        assert "tool_start" in types, f"模型没有调用工具：{types}"
        output = "".join(
            str(e.get("content") or "") for e in events if e.type == "delta"
        )
        assert "第一行" in output or "第一行" in "".join(
            str(e.get("output") or "") for e in events if e.type == "tool_result"
        )
        print("\n[live] 工具循环回复:", output[:300])
    finally:
        await agent.close()
