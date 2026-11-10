"""Checkpoint：播种幂等 / 重置只留 system / 旧库兼容。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from liorium.agent.runtime.checkpoint import CheckpointStore, thread_config


class _FakeGraph:
    """最小 StateGraph 替身：只实现 aget_state / aupdate_state。"""

    def __init__(self) -> None:
        self.messages: list = []

    async def aget_state(self, config):
        return type("Snapshot", (), {"values": {"messages": list(self.messages)}})()

    async def aupdate_state(self, config, values, as_node=None):
        assert as_node == "model"
        updates = values.get("messages") or []
        if any(isinstance(m, RemoveMessage) and m.id == REMOVE_ALL_MESSAGES for m in updates):
            kept = [m for m in updates if not isinstance(m, RemoveMessage)]
            self.messages = kept
        else:
            self.messages.extend(updates)


@pytest.mark.asyncio
async def test_thread_config() -> None:
    cfg = thread_config("main", recursion_limit=42)
    assert cfg == {"configurable": {"thread_id": "main"}, "recursion_limit": 42}


@pytest.mark.asyncio
async def test_seed_system_prompt_is_idempotent() -> None:
    graph = _FakeGraph()
    store = CheckpointStore(Path("unused.db"))
    config = thread_config("main")
    assert await store.seed_system_prompt(graph, config, "你是 A") is True
    assert isinstance(graph.messages[0], SystemMessage)
    assert await store.seed_system_prompt(graph, config, "你是 A") is False
    assert len(graph.messages) == 1


@pytest.mark.asyncio
async def test_seed_preserves_history() -> None:
    graph = _FakeGraph()
    graph.messages = [SystemMessage(content="旧"), HumanMessage(content="历史")]
    store = CheckpointStore(Path("unused.db"))
    await store.seed_system_prompt(graph, thread_config("main"), "新")
    assert graph.messages[0].content == "新"
    assert any(isinstance(m, HumanMessage) for m in graph.messages)
    assert sum(isinstance(m, SystemMessage) for m in graph.messages) == 1


@pytest.mark.asyncio
async def test_reset_dialog_drops_history() -> None:
    graph = _FakeGraph()
    graph.messages = [SystemMessage(content="旧"), HumanMessage(content="历史")]
    store = CheckpointStore(Path("unused.db"))
    await store.reset_dialog(graph, thread_config("main"), "新")
    assert len(graph.messages) == 1
    assert isinstance(graph.messages[0], SystemMessage)


@pytest.mark.asyncio
async def test_sqlite_roundtrip(tmp_path: Path) -> None:
    """真实 AsyncSqliteSaver：打开 / 关闭 / 重开。"""
    db = tmp_path / "cp.db"
    store = CheckpointStore(db)
    saver = await store.open()
    assert isinstance(saver, AsyncSqliteSaver)
    assert db.exists()
    await store.close()
    store2 = CheckpointStore(db)
    await store2.open()
    await store2.close()
