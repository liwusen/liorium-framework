"""单执行位：一个 Agent 同一时刻只跑一个回合。

定稿（``__dev__/Liorium Event 事件流原生 定稿.md`` §5.5 / §12 P2）把旧的
"优先级堆 + 抢占标志"调度器**退化**成只表达三件事：

1. **单执行位**：任意时刻最多一个回合在跑（``occupied``）；
2. **独占**：本回合是否拒绝被 ``IMMEDIATE`` 抢占（``exclusive``）；
3. **维护队列**：``apply`` / ``refresh_realms`` / ``set_realm_enabled`` / ``reset_dialog``
   插在"当前回合结束之后、下一个 Inbox 条目之前"执行（§5.5）。

真正的排队（bucket / FIFO / 合批 / 超时）在 :mod:`liorium.agent.runtime.inbox`，
回合裁决在 :mod:`liorium.agent.runtime.worker`。这里**没有优先级**：``IMMEDIATE``
只是插到队首并可在安全点接管当前回合，不是"高优先级队列"。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class MaintenanceRequest:
    """一条等待插队执行的维护操作。"""

    fn: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]


class ExecSlot:
    """单执行位 + 维护队列（同步状态，由 worker 任务驱动）。"""

    def __init__(self) -> None:
        self._occupied = False
        self._exclusive = False
        self._maintenance: deque[MaintenanceRequest] = deque()

    # -- 执行位 ---------------------------------------------------------

    @property
    def occupied(self) -> bool:
        return self._occupied

    @property
    def exclusive(self) -> bool:
        return self._exclusive if self._occupied else False

    def enter(self, *, exclusive: bool = False) -> None:
        self._occupied = True
        self._exclusive = bool(exclusive)

    def leave(self) -> None:
        self._occupied = False
        self._exclusive = False

    # -- 维护队列 -------------------------------------------------------

    def submit(self, fn: Callable[[], Awaitable[Any]]) -> asyncio.Future[Any]:
        """登记一条维护操作；返回等待结果的 future（调用方 ``await`` 它）。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._maintenance.append(MaintenanceRequest(fn=fn, future=future))
        return future

    def pop_maintenance(self) -> MaintenanceRequest | None:
        while self._maintenance:
            request = self._maintenance.popleft()
            if not request.future.done():
                return request
        return None

    @property
    def pending_maintenance(self) -> int:
        return len(self._maintenance)

    def drain(self, exc: BaseException) -> None:
        """收摊：未执行的维护操作全部以 ``exc`` 失败（不留下永远等不到的 future）。"""
        while self._maintenance:
            request = self._maintenance.popleft()
            if not request.future.done():
                request.future.set_exception(exc)


__all__ = ["ExecSlot", "MaintenanceRequest"]
