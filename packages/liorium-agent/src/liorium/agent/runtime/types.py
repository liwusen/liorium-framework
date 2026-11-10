"""一个回合的输入与运行时上下文。

``run()`` 的生成器只做三件事：排队（调度位）、跑 ``execute_turn``、把这一回合的事件
按产生顺序转出去；真正的模型/工具循环在 ``runtime/loop.py``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from liorium.core.ports import ArtifactStore
from liorium.core.types import ContextItem, Event
from liorium.vfs.fs import VirtualFileSystem

from ..realm.base import RealmTaskScope
from ..spec import AgentSpec

EventEmitter = Callable[[Event], Awaitable[None]]
"""把一条事件推入当前回合的事件队列（顺序即产生顺序）。"""


@dataclass
class TurnRequest:
    """一次 ``run()`` 的输入。"""

    agent_id: str
    turn_id: str
    text: str
    context: Sequence[ContextItem] = ()
    origin: str = "user"

    @property
    def depth(self) -> int:
        return self.text.count("")


@dataclass
class TurnRuntime:
    """``execute_turn`` 需要的全部端口。"""

    spec: AgentSpec
    graph: Any
    """``langchain.agents.create_agent`` 编译出的图。"""
    checkpointer: Any
    model: Any
    tools: list[Any]
    middleware: list[Any]
    artifacts: ArtifactStore
    vfs: VirtualFileSystem
    emit: EventEmitter
    interrupt: asyncio.Event
    tasks: RealmTaskScope
    thread_id: str = "default"
    api_format: str | None = None
    """``openai_chat`` / ``openai_responses`` / ``anthropic`` / ``google`` / None。"""

    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["EventEmitter", "TurnRequest", "TurnRuntime"]
