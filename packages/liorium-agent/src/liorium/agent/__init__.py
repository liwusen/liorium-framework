"""Liorium Agent 公共面。

计划依据：``__dev__/Liorium Event 事件流原生 定稿.md`` §3（公共 API）。

```python
class LioriumAgent:
    async def start(self) -> None
    async def close(self) -> None

    def push(self, text, *, id, bucket=Bucket.MAJOR, flags=Flag.NONE,
             origin="user", timeout=None, context=()) -> None
    def stream(self, *, ids=None) -> AsyncIterator[Event]
    def inject(self, items) -> None
    async def interrupt(self, *, reason="user") -> bool

    async def apply(self, spec, *, force=False) -> ApplyResult
    async def refresh_realms(self, *, force=True) -> bool
    async def set_realm_enabled(self, realm_id, enabled) -> bool
    async def reset_dialog(self) -> None
    async def send_to_realm(self, realm_id, name, data=None, *,
                            origin="host", timeout=None) -> Any   # 2026-11-14
    def status(self) -> AgentStatus
```

2026-11-12 起是**事件流原生**：外部只 ``push()`` 投递、``stream()`` 观察、
``inject()`` 走底层通道；回合的创建 / 调度 / 取消全部由 Agent 内部的常驻
worker 裁决（``runtime/worker.py``），``EventHub`` 负责永不阻塞的扇出。

入站语义见 :class:`Bucket`（何时投递：``IMMEDIATE`` / ``MAJOR`` 入队即排回合，
``BATCHED`` 合批窗口到期后开回合，``ENVIRONMENT`` 永不开回合只搭车）与
:class:`Flag`（AUTO / EXCLUSIVE / DROPPABLE）；合批窗口见 :class:`BatchWindow`。

刻意不存在：``run()``（P3 起删除）、``invoke()`` / ``submit()`` / ``events()`` /
``notify()``。
"""

from __future__ import annotations

__version__ = "0.3.0"

from .agent import LioriumAgent
from .context import ContextChannel
from .realm import (
    BaseRealm,
    HookOutcome,
    ModelCall,
    Realm,
    RealmContext,
    RealmHost,
    RealmManager,
    RealmMessage,
    RealmRecord,
    RealmTaskScope,
    ToolCall,
    TurnInfo,
    TurnOutcome,
    realm_tool,
)
from .realm.subagent import SubagentRealm
from .runtime.hub import EventHub, Subscription
from .runtime.inbox import BatchWindow, Bucket, Entry, Flag, Inbox
from .spec import AgentSpec, BootSpec, Limits, RealmSpec
from .status import AgentStatus, ApplyResult, CacheEpoch, RealmStatus

__all__ = [
    "AgentSpec",
    "AgentStatus",
    "ApplyResult",
    "BaseRealm",
    "BatchWindow",
    "BootSpec",
    "Bucket",
    "CacheEpoch",
    "ContextChannel",
    "Entry",
    "EventHub",
    "Flag",
    "HookOutcome",
    "Inbox",
    "Limits",
    "ModelCall",
    "Realm",
    "RealmContext",
    "RealmHost",
    "RealmManager",
    "RealmMessage",
    "RealmSpec",
    "RealmStatus",
    "Subscription",
    "SubagentRealm",
    "ToolCall",
    "TurnInfo",
    "TurnOutcome",
    "LioriumAgent",
    "__version__",
    "realm_tool",
]
