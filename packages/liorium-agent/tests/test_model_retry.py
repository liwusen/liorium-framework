"""模型重试：失败重放模型调用（最内层），且不吞错误。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
from langchain.agents.middleware import ModelRetryMiddleware
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from liorium.agent import AgentSpec, Limits, LioriumAgent
from liorium.agent.runtime.middleware import build_chain
from liorium.agent.runtime.output_store import OutputStore


def is_model_retry_middleware(middleware: Any) -> bool:
    return isinstance(middleware, ModelRetryMiddleware)


class FlakyChatModel(BaseChatModel):
    """前 ``fail_times`` 次调用抛错，之后返回固定文本。"""

    fail_times: int = 1
    answer: str = "恢复成功"
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "liorium-flaky"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FlakyChatModel":
        """create_agent 会 bind_tools；假模型只需接受（schema 不影响重试）。"""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("429 Too Many Requests")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.answer))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("429 Too Many Requests")
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.answer))


def test_chain_ends_with_model_retry(tmp_path: Path) -> None:
    chain = build_chain(artifacts=OutputStore(root=tmp_path / "artifacts"))
    assert chain, "基座链不能为空"
    assert is_model_retry_middleware(chain[-1]), "model_retry 必须是最内层（末尾）"
    assert not any(is_model_retry_middleware(m) for m in chain[:-1]), "不能有双重重试"


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_error(agent_workspace: Path) -> None:
    model = FlakyChatModel(fail_times=1)
    spec = AgentSpec(
        id="retry-agent",
        inbox_dir=agent_workspace,
        model="flaky::x",
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(spec=spec, model_factory=lambda _l: model)
    await agent.start()
    try:
        events = [e async for e in agent.run("你好")]
        assert events[-1].type == "done"
        assert any(e.type == "delta" for e in events)
        assert model.calls == 2, f"应重试一次，实际调用 {model.calls} 次"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts(agent_workspace: Path) -> None:
    from liorium.core.errors import TurnError

    model = FlakyChatModel(fail_times=99)
    spec = AgentSpec(
        id="retry-fail",
        inbox_dir=agent_workspace,
        model="flaky::x",
        prompt_sources=("AGENT.md",),
        limits=Limits(context_window=8000, max_turns=5),
        workspace_root=agent_workspace,
    )
    agent = LioriumAgent(spec=spec, model_factory=lambda _l: model)
    await agent.start()
    try:
        with pytest.raises(TurnError) as info:
            _ = [e async for e in agent.run("你好")]
        assert "429" in str(info.value) or "ConnectionError" in str(info.value)
        assert model.calls >= 2
    finally:
        await agent.close()
