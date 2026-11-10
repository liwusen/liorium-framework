"""单执行位调度：调用级优先级 + 三个正交标志。

计划基线：既有实现的 ``agent/runtime/scheduler.py``「4 档 + 4 个正交标志」（§1.1）。
本项目只迁其中真正用得上的部分：

- **优先级**：每个 ``run()`` 自己给一个整数，**越大越优先**（与 Liorium 既有语义一致；
  既有实现是越小越优先的 4 档枚举，这里不照抄档位）。同优先级 FIFO，不饿死。
- **三个标志**（``FLAG``）：``INTERRUPTING``（可抢占）、``EXCLUSIVE``（拒绝被抢占）、
  ``DROPPABLE``（拥塞可丢）。``sink`` 与 ``gate`` 不迁 —— 本项目没有播报通道与
  播放闸门，事件出口就是 ``run()`` 返回的生成器。

不迁的部分（见 ``docs/design/scheduler.md``「非目标」）：同 ``merge_key`` 合并、
trigger 超限降级进 batch、播放闸门准入、惰性僵死判定、``snapshot()``、owner 撤单。
"""

from __future__ import annotations

import asyncio
import heapq
from enum import IntFlag
from typing import Any, Callable

from liorium.core.errors import TurnDropped
from liorium.core.log import get_logger

log = get_logger("liorium.agent.scheduler")

QUEUE_MAX = 50
"""等待者上限。**只有**带 ``FLAG.DROPPABLE`` 的回合会因此被丢弃（``TurnDropped``）。"""


class FLAG(IntFlag):
    """一次 ``run()`` 的调度标志：正交、可组合，默认 ``NONE``。

    - ``INTERRUPTING``：可以抢占正在运行、优先级**严格更高**、且没打
      ``EXCLUSIVE`` 的回合。被抢占方收到 ``TurnInterrupted(reason="preempted")``。
    - ``EXCLUSIVE``：本回合拒绝被抢占；更高优先级的 ``INTERRUPTING`` 只能排队。
      （等价既有实现的 ``preemptible=False``；默认没有它 = 可以被抢占。）
    - ``DROPPABLE``：排队已满（``queue_depth() >= queue_max``）时立即丢弃，
      抛 ``TurnDropped`` —— 适合"晚了就没意义"的后台回合。
    """

    NONE = 0
    INTERRUPTING = 1
    EXCLUSIVE = 2
    DROPPABLE = 4


DEFAULT_FLAGS = FLAG.NONE
"""默认：可被抢占（等价既有实现 ``preemptible=True``）、不抢别人、拥塞时不丢。"""

PreemptHook = Callable[[str], bool]
"""抢占回调：**同步**调用，返回是否真的打断了运行中的回合。

返回 ``False`` 的典型场景：执行位被维护操作（``apply`` / ``reset_dialog`` 等）占着，
没有"正在跑的回合"可打断 —— 此时抢占者只是排队，不算抢占已发生。
"""


class TurnScheduler:
    """一个 Agent 的执行位。

    ``acquire(priority=..., flags=...)`` 拿位；``release()`` 把位交给等待者里优先级
    最高的那个（同优先级按到达顺序）。取消等待者的 ``acquire`` 不会泄漏执行位。

    不变量：

    - ``locked=False`` ⟹ 没有活等待者（``release()`` 只在无人可唤醒时才解锁）；
    - 同一个时刻只有一把执行位：抢占只是"取消运行中的回合并排队"，**不会**让两个
      回合并排跑（被抢占方释放之后，抢占者才被唤醒）。
    """

    def __init__(self, *, queue_max: int = QUEUE_MAX) -> None:
        self._queue_max = int(queue_max)
        self._locked = False
        self._active_priority = 0
        self._active_flags = FLAG.NONE
        self._active_tag: Any = None
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = 0
        self._preempt_signalled = False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def active_priority(self) -> int | None:
        return self._active_priority if self._locked else None

    @property
    def active_flags(self) -> FLAG:
        return self._active_flags if self._locked else FLAG.NONE

    def queue_depth(self) -> int:
        """还在等待的回合数（已被唤醒 / 已取消的不算）。"""
        return len([w for w in self._waiters if not w[2].done()])

    # ------------------------------------------------------------------
    # 拿位 / 还位
    # ------------------------------------------------------------------

    async def acquire(
        self,
        *,
        priority: int = 0,
        flags: FLAG = FLAG.NONE,
        tag: Any = None,
        on_preempt: PreemptHook | None = None,
    ) -> None:
        flags = FLAG(flags)
        if not self._locked:
            self._activate(priority, flags, tag)
            return

        depth = self.queue_depth()
        if flags & FLAG.DROPPABLE and depth >= self._queue_max:
            log.warning(
                "调度器拥塞（depth=%d >= %d），丢弃可丢弃回合: %s", depth, self._queue_max, tag
            )
            raise TurnDropped("congested")

        if self._should_preempt(priority, flags):
            signalled = on_preempt("preempted") if on_preempt is not None else False
            if signalled:
                self._preempt_signalled = True
                log.info(
                    "抢占执行位: 新回合 %s priority=%d > 运行中 %s priority=%d",
                    tag,
                    int(priority),
                    self._active_tag,
                    self._active_priority,
                )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (-int(priority), self._seq, fut))
        try:
            await fut
        except BaseException:
            # 已被唤醒但没接住（取消恰好发生在 set_result 之后）：把位还回去
            fut.cancel()
            if fut.done() and not fut.cancelled():
                self.release()
            raise
        self._activate(priority, flags, tag)

    def release(self) -> None:
        while self._waiters:
            _prio, _seq, fut = heapq.heappop(self._waiters)
            if fut.done():
                continue
            fut.set_result(None)
            # 所有权直接转移，_locked 保持 True
            return
        self._locked = False
        self._active_priority = 0
        self._active_flags = FLAG.NONE
        self._active_tag = None
        self._preempt_signalled = False

    def reset(self) -> None:
        """强制释放（close() 时兜底）。"""
        for _p, _s, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        self._locked = False
        self._active_priority = 0
        self._active_flags = FLAG.NONE
        self._active_tag = None
        self._preempt_signalled = False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _should_preempt(self, priority: int, flags: FLAG) -> bool:
        """新回合是否应该打断运行中的那个。

        - 必须显式 ``INTERRUPTING``（默认不抢）；
        - 运行中的回合不能是 ``EXCLUSIVE``；
        - 优先级必须**严格更高**（同档 FIFO：先来先跑，后来的排队）；
        - 一次抢占只发一次信号（在旧回合释放之前，后续抢占者只排队）。
        """
        if not (flags & FLAG.INTERRUPTING):
            return False
        if self._preempt_signalled:
            return False
        if self._active_flags & FLAG.EXCLUSIVE:
            return False
        return int(priority) > self._active_priority

    def _activate(self, priority: int, flags: FLAG, tag: Any) -> None:
        self._locked = True
        self._active_priority = int(priority)
        self._active_flags = FLAG(flags)
        self._active_tag = tag
        self._preempt_signalled = False


__all__ = ["DEFAULT_FLAGS", "FLAG", "QUEUE_MAX", "PreemptHook", "TurnScheduler"]
