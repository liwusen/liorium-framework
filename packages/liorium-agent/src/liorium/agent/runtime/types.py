"""一个回合的输入与运行时上下文。

``TurnWorker`` 负责从 Inbox 取条目、组装 ``TurnRequest`` / ``TurnRuntime``，再迭代
``execute_turn`` 把这一回合的事件按产生顺序转出去；真正的模型/工具循环在
``runtime/loop.py``。
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

PreemptSource = Callable[[], Any]
"""``IMMEDIATE`` 抢占时的"接管条目"取货点（worker 提供，同步、可能返回 ``None``）。

返回 ``None`` 表示"信号在，但没有可接管的条目"（例如条目刚好过期被丢弃）；
此时回合内继续等待下一个事件。
"""


@dataclass
class TurnRequest:
    """一条回合的输入。"""

    agent_id: str
    turn_id: str
    text: str
    context: Sequence[ContextItem] = ()
    origin: str = "user"
    inputs: tuple[str, ...] = ()
    """本回合开始时会话消费的入站条目 id（开回合条目 + 并入的 context 条目）。"""

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

    exclusive: bool = False
    """本回合是否拒绝被 ``IMMEDIATE`` 抢占（``Flag.EXCLUSIVE``）。"""
    preempt: PreemptSource | None = None
    """``IMMEDIATE`` 抢占时取"接管条目"；``None`` = 不可抢占（无 worker 的裸驱动）。"""
    preempt_event: asyncio.Event | None = None
    """有 ``IMMEDIATE`` 条目等待抢占时置位；``execute_turn`` 与它赛跑。"""
    interrupt_reason: str = ""
    """``interrupt`` 置位的原因（worker 写，终态帧用）。"""

    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["EventEmitter", "TurnRequest", "TurnRuntime"]
