"""Liorium Agent 公共面。

计划 §3.2：

```python
class LioriumAgent:
    async def start(self) -> None
    async def close(self) -> None
    def run(self, text, *, context=(), origin="user", priority=None, flags=FLAG.NONE) -> AsyncIterator[Event]
    async def inject(self, items) -> None
    async def interrupt(self, *, reason="") -> bool
    async def apply(self, spec: AgentSpec) -> ApplyResult
    async def refresh_realms(self, *, force: bool = True) -> bool
    def status(self) -> AgentStatus
```

2026-11-08：删除宿主端口（``HostPort`` / ``SpecProvider``），``run()`` 是唯一事件出口；
调度标志见 :class:`FLAG`，可丢弃回合抛 ``TurnDropped``（``liorium.core.errors``）。

刻意不存在：``invoke()`` / ``submit()`` / ``events()`` / ``notify()``。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .agent import LioriumAgent
from .context import ContextChannel
from .realm import BaseRealm, Realm, RealmContext, RealmManager, TurnInfo, realm_tool
from .realm.subagent import SubagentRealm
from .runtime.scheduler import DEFAULT_FLAGS, FLAG
from .spec import AgentSpec, BootSpec, Limits, Profile, RealmSpec
from .status import AgentStatus, ApplyResult, CacheEpoch, RealmStatus

__all__ = [
    "AgentSpec",
    "AgentStatus",
    "ApplyResult",
    "BaseRealm",
    "BootSpec",
    "CacheEpoch",
    "ContextChannel",
    "DEFAULT_FLAGS",
    "FLAG",
    "Limits",
    "Profile",
    "Realm",
    "RealmContext",
    "RealmManager",
    "RealmSpec",
    "RealmStatus",
    "SubagentRealm",
    "TurnInfo",
    "LioriumAgent",
    "__version__",
    "realm_tool",
]
