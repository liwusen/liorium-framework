"""``LioriumAgent.close()`` 与模型工厂的 ``aclose`` 契约（计划 E3 / §5.2 / §8.2）。

「谁造谁关」：Agent 只调工厂自带的 ``aclose``，绝不对模型实例做鸭子类型关闭。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from liorium.agent import LioriumAgent
from liorium.agent.spec import AgentSpec
from liorium_testkit import ScriptedChatModel


def make_spec(tmp_path: Path) -> AgentSpec:
    return AgentSpec(id="close-test", inbox_dir=tmp_path / "agent", model="P::m")


class RecordingFactory:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.closed = 0

    def __call__(self, label: str) -> Any:
        self.labels.append(label)
        return ScriptedChatModel(responses=["ok"])

    async def aclose(self) -> None:
        self.closed += 1


async def test_close_calls_aclose_once_for_shared_factory(tmp_path: Path) -> None:
    factory = RecordingFactory()
    agent = LioriumAgent(spec=make_spec(tmp_path), model_factory=factory, compact_model_factory=factory)

    await agent.start()
    await agent.close()
    await agent.close()  # 幂等

    assert factory.closed == 1


async def test_close_calls_aclose_for_each_distinct_factory(tmp_path: Path) -> None:
    chat = RecordingFactory()
    compact = RecordingFactory()
    agent = LioriumAgent(spec=make_spec(tmp_path), model_factory=chat, compact_model_factory=compact)

    await agent.start()
    await agent.close()

    assert chat.closed == 1
    assert compact.closed == 1


async def test_bare_function_factory_still_closes(tmp_path: Path) -> None:
    """没有 ``aclose`` 的裸函数工厂：行为不变，不能因此报错。"""

    def factory(label: str) -> Any:
        return ScriptedChatModel(responses=["ok"])

    agent = LioriumAgent(spec=make_spec(tmp_path), model_factory=factory)
    await agent.start()
    await agent.close()


async def test_aclose_failure_does_not_block_close(tmp_path: Path) -> None:
    class Broken(RecordingFactory):
        async def aclose(self) -> None:
            raise OSError("boom")

    agent = LioriumAgent(spec=make_spec(tmp_path), model_factory=Broken())
    await agent.start()
    await agent.close()  # 不抛
    assert agent._closed is True
