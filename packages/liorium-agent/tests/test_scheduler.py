"""调度器：单执行位 / 调用级优先级 / FIFO / 抢占 / 丢弃 / 取消不泄漏。"""

from __future__ import annotations

import asyncio

import pytest

from liorium.agent.runtime.scheduler import FLAG, TurnScheduler
from liorium.core.errors import TurnDropped


@pytest.mark.asyncio
async def test_single_slot() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0, tag="a")
    assert s.locked
    s.release()
    assert not s.locked


@pytest.mark.asyncio
async def test_fifo_same_priority() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0)
    order: list[str] = []

    async def waiter(name: str) -> None:
        await s.acquire(priority=0, tag=name)
        order.append(name)
        s.release()

    tasks = [asyncio.create_task(waiter(n)) for n in ("a", "b", "c")]
    await asyncio.sleep(0.02)
    s.release()
    await asyncio.gather(*tasks)
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_priority_order() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0)
    order: list[str] = []

    async def waiter(name: str, prio: int) -> None:
        await s.acquire(priority=prio, tag=name)
        order.append(name)
        await asyncio.sleep(0)
        s.release()

    low = asyncio.create_task(waiter("low", 0))
    await asyncio.sleep(0.01)
    high = asyncio.create_task(waiter("high", 5))
    mid = asyncio.create_task(waiter("mid", 1))
    await asyncio.sleep(0.02)
    s.release()
    await asyncio.gather(low, high, mid)
    assert order == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_slot() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0)

    async def waiter() -> None:
        await s.acquire(priority=0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert s.queue_depth() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s.release()
    assert not s.locked

    await s.acquire(priority=0)  # 还能正常拿到
    s.release()


# ---------------------------------------------------------------------------
# 三个正交标志（§1.1 的子集）
# ---------------------------------------------------------------------------


def test_flag_values_are_frozen() -> None:
    """标志是公开 API：位值一旦上线不改（调用方会把它拼进配置）。"""
    assert int(FLAG.NONE) == 0
    assert int(FLAG.INTERRUPTING) == 1
    assert int(FLAG.EXCLUSIVE) == 2
    assert int(FLAG.DROPPABLE) == 4
    assert FLAG.INTERRUPTING | FLAG.DROPPABLE == 5


async def test_preempting_waiter_signals_holder() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0, tag="holder")
    calls: list[str] = []

    def on_preempt(reason: str) -> bool:
        calls.append(reason)
        return True

    urgent = asyncio.create_task(
        s.acquire(priority=5, flags=FLAG.INTERRUPTING, tag="urgent", on_preempt=on_preempt)
    )
    await asyncio.sleep(0.01)
    assert calls == ["preempted"]
    assert s.active_priority == 0, "旧回合没释放之前，执行位不得转移"

    # 第二个抢占者：旧回合释放前不重复回调，只排队
    urgent2 = asyncio.create_task(
        s.acquire(priority=9, flags=FLAG.INTERRUPTING, tag="urgent2", on_preempt=on_preempt)
    )
    await asyncio.sleep(0.01)
    assert calls == ["preempted"]

    s.release()  # 优先级高的先拿
    await asyncio.wait_for(urgent2, timeout=1.0)
    assert s.active_priority == 9
    s.release()
    await asyncio.wait_for(urgent, timeout=1.0)
    assert s.active_priority == 5
    s.release()
    assert not s.locked


async def test_preempt_requires_higher_priority() -> None:
    """同档或更低档：即使带 INTERRUPTING 也只排队（同档 FIFO，不互相打断）。"""
    s = TurnScheduler()
    await s.acquire(priority=5, tag="holder")
    calls: list[str] = []

    def on_preempt(reason: str) -> bool:
        calls.append(reason)
        return True

    equal = asyncio.create_task(
        s.acquire(priority=5, flags=FLAG.INTERRUPTING, tag="equal", on_preempt=on_preempt)
    )
    lower = asyncio.create_task(
        s.acquire(priority=1, flags=FLAG.INTERRUPTING, tag="lower", on_preempt=on_preempt)
    )
    await asyncio.sleep(0.01)
    assert calls == []
    assert s.active_priority == 5

    s.release()
    await asyncio.wait_for(equal, timeout=1.0)
    assert s.active_priority == 5
    s.release()
    await asyncio.wait_for(lower, timeout=1.0)
    assert s.active_priority == 1
    s.release()


async def test_exclusive_holder_is_not_preempted() -> None:
    s = TurnScheduler()
    await s.acquire(priority=0, flags=FLAG.EXCLUSIVE, tag="holder")
    calls: list[str] = []

    def on_preempt(reason: str) -> bool:
        calls.append(reason)
        return True

    urgent = asyncio.create_task(
        s.acquire(priority=99, flags=FLAG.INTERRUPTING, tag="urgent", on_preempt=on_preempt)
    )
    await asyncio.sleep(0.01)
    assert calls == []
    assert s.active_priority == 0 and s.active_flags & FLAG.EXCLUSIVE

    s.release()
    await asyncio.wait_for(urgent, timeout=1.0)
    assert s.active_priority == 99
    s.release()


async def test_droppable_is_dropped_when_queue_full() -> None:
    s = TurnScheduler(queue_max=1)
    await s.acquire(priority=0, tag="holder")
    queued = asyncio.create_task(s.acquire(priority=0, tag="queued"))
    await asyncio.sleep(0.01)
    assert s.queue_depth() == 1

    with pytest.raises(TurnDropped) as excinfo:
        await s.acquire(priority=99, flags=FLAG.DROPPABLE, tag="shed")
    assert excinfo.value.reason == "congested"
    assert s.queue_depth() == 1, "被丢弃的回合不得留在队列里"

    # 不带 DROPPABLE 的请求不受拥塞上限约束（只排队）
    extra = asyncio.create_task(s.acquire(priority=1, tag="extra"))
    await asyncio.sleep(0.01)
    assert s.queue_depth() == 2

    s.release()
    await asyncio.wait_for(extra, timeout=1.0)
    s.release()
    await asyncio.wait_for(queued, timeout=1.0)
    s.release()
    assert not s.locked


async def test_droppable_takes_free_slot() -> None:
    """执行位空着就别丢：DROPPABLE 只表达"拥塞时可丢"，不是"永远不跑"。"""
    s = TurnScheduler(queue_max=1)
    await s.acquire(priority=0, flags=FLAG.DROPPABLE, tag="only")
    assert s.locked
    assert s.active_flags & FLAG.DROPPABLE
    s.release()
