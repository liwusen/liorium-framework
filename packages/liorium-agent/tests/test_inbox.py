"""Inbox：桶映射 / 同 id 原位替换 / timeout / DROPPABLE / 合批 / 唤醒。

全部用例注入假时钟（``clock=lambda: fake.now``），不依赖真实 ``time.monotonic``。
"""

from __future__ import annotations

import asyncio

import pytest

from liorium.agent.runtime.inbox import (
    QUEUE_MAX,
    BatchWindow,
    Bucket,
    Entry,
    Flag,
    Inbox,
)
from liorium.core.types import ContextItem


class FakeClock:
    """可推进的单调假时钟（秒）。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 1. 桶映射 / 深度
# ---------------------------------------------------------------------------


def test_bucket_opens_turn_and_depth() -> None:
    assert Bucket.IMMEDIATE.opens_turn
    assert Bucket.MAJOR.opens_turn
    assert Bucket.BATCHED.opens_turn  # 窗口到期后自己开回合
    assert not Bucket.ENVIRONMENT.opens_turn
    assert QUEUE_MAX == 50

    clock = FakeClock()
    inbox = Inbox(clock=clock)
    assert inbox.batch_window == BatchWindow()
    assert inbox.batch_window == BatchWindow(quiet_gap=2.5, min_age=0.0, max_age=15.0, max_size=100)
    assert inbox.depth() == 0
    assert inbox.depth_by_bucket() == {
        Bucket.IMMEDIATE: 0,
        Bucket.MAJOR: 0,
        Bucket.BATCHED: 0,
        Bucket.ENVIRONMENT: 0,
    }

    inbox.push(Entry(id="i", text="I", bucket=Bucket.IMMEDIATE))
    inbox.push(Entry(id="m", text="M"))  # 默认 MAJOR
    inbox.push(Entry(id="b", text="B", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="e", text="E", bucket=Bucket.ENVIRONMENT))

    assert inbox.depth() == 4
    assert inbox.depth_by_bucket() == {
        Bucket.IMMEDIATE: 1,
        Bucket.MAJOR: 1,
        Bucket.BATCHED: 1,
        Bucket.ENVIRONMENT: 1,
    }


def test_turn_entry_immediate_before_major_fifo() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="m1", text="M1"))
    clock.advance(1)
    inbox.push(Entry(id="m2", text="M2"))
    clock.advance(1)
    inbox.push(Entry(id="i1", text="I1", bucket=Bucket.IMMEDIATE))
    clock.advance(1)
    inbox.push(Entry(id="i2", text="I2", bucket=Bucket.IMMEDIATE))

    assert inbox.peek_turn_entry().id == "i1"  # IMMEDIATE 全体先于 MAJOR
    assert inbox.pop_turn_entry().id == "i1"
    assert inbox.pop_turn_entry().id == "i2"
    assert inbox.pop_turn_entry().id == "m1"  # 同档 FIFO
    assert inbox.pop_turn_entry().id == "m2"
    assert inbox.pop_turn_entry() is None


def test_pop_immediate_only_takes_immediate() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="m", text="M"))
    inbox.push(Entry(id="i1", text="I1", bucket=Bucket.IMMEDIATE))
    inbox.push(Entry(id="i2", text="I2", bucket=Bucket.IMMEDIATE))

    assert inbox.has_immediate()
    assert inbox.pop_immediate().id == "i1"
    assert inbox.pop_immediate().id == "i2"
    assert inbox.pop_immediate() is None
    assert not inbox.has_immediate()
    assert inbox.peek_turn_entry().id == "m"


# ---------------------------------------------------------------------------
# 2. 同 id 原位替换 / 锚点继承
# ---------------------------------------------------------------------------


def test_same_id_replaces_in_place_and_inherits_anchors() -> None:
    clock = FakeClock(100.0)
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="a", text="A"))
    clock.advance(1)  # 101
    inbox.push(Entry(id="b", text="B", timeout=10))
    clock.advance(1)  # 102
    inbox.push(Entry(id="c", text="C"))
    clock.advance(3)  # 105

    assert inbox.push(Entry(id="b", text="B2", timeout=4)) == "accepted"

    # 位置不挪：仍是 A, B', C
    assert inbox.pop_turn_entry().id == "a"
    replaced = inbox.pop_turn_entry()
    assert replaced is not None
    assert replaced.id == "b"
    assert replaced.text == "B2"
    assert replaced.first_at == 101.0  # 继承旧 first_at
    assert replaced.last_at == 101.0  # 不刷新旧 last_at
    assert replaced.pushed_at == 105.0  # 本次 push 时刻
    assert replaced.deadline == 109.0  # timeout 从本次 push 起算
    assert inbox.pop_turn_entry().id == "c"
    assert inbox.pop_turn_entry() is None


def test_same_id_replacement_uses_new_bucket_and_flags() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="x", text="1"))  # MAJOR
    clock.advance(1)
    inbox.push(Entry(id="x", text="2", bucket=Bucket.BATCHED, flags=Flag.EXCLUSIVE))

    entry = inbox.context_entries()[0]
    assert entry.id == "x"
    assert entry.bucket is Bucket.BATCHED
    assert entry.flags & Flag.EXCLUSIVE
    assert inbox.peek_turn_entry() is None  # 不再开回合
    assert inbox.depth() == 1  # 替换不增加深度


def test_same_id_replacement_does_not_refresh_batch_anchor() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="x", text="1", bucket=Bucket.BATCHED))
    clock.advance(2.5)
    inbox.push(Entry(id="x", text="2", bucket=Bucket.BATCHED))

    entry = inbox.context_entries()[0]
    assert entry.first_at == 0.0
    assert entry.last_at == 0.0  # 高频状态更新不饿死批次
    assert inbox.batch_due()  # 沿用原锚点，2.5s 安静期已到


# ---------------------------------------------------------------------------
# 3. timeout 过期
# ---------------------------------------------------------------------------


def test_expire_returns_only_expired_without_collateral() -> None:
    clock = FakeClock(10.0)
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="a", text="A", timeout=5))  # deadline 15
    inbox.push(Entry(id="b", text="B"))  # 永不过期
    inbox.push(Entry(id="c", text="C", timeout=20))  # deadline 30
    inbox.push(Entry(id="d", text="D", bucket=Bucket.BATCHED, timeout=10))  # deadline 20

    clock.advance(5)  # now=15：a 到期（<= 边界）
    assert [e.id for e in inbox.expire()] == ["a"]
    assert inbox.depth() == 3  # 不连坐

    clock.advance(16)  # now=31：c、d 到期，按队列序返回
    assert [e.id for e in inbox.expire()] == ["c", "d"]
    assert inbox.depth() == 1
    assert inbox.peek_turn_entry().id == "b"  # 未到期条目保留

    assert inbox.expire() == []


# ---------------------------------------------------------------------------
# 4. DROPPABLE
# ---------------------------------------------------------------------------


def test_droppable_dropped_only_when_full() -> None:
    clock = FakeClock()
    inbox = Inbox(queue_max=1, clock=clock)
    assert inbox.push(Entry(id="a", text="A")) == "accepted"
    # 非 DROPPABLE 不受 queue_max 限制
    assert inbox.push(Entry(id="b", text="B")) == "accepted"
    assert inbox.depth() == 2

    assert inbox.push(Entry(id="c", text="C", flags=Flag.DROPPABLE)) == "dropped"
    assert inbox.depth() == 2  # 未入队

    assert [inbox.pop_turn_entry().id, inbox.pop_turn_entry().id] == ["a", "b"]
    assert inbox.pop_turn_entry() is None


def test_droppable_at_just_below_limit_is_accepted() -> None:
    clock = FakeClock()
    inbox = Inbox(queue_max=2, clock=clock)
    inbox.push(Entry(id="a", text="A"))
    assert inbox.push(Entry(id="b", text="B", flags=Flag.DROPPABLE)) == "accepted"
    assert inbox.depth() == 2


# ---------------------------------------------------------------------------
# 5. id 复用
# ---------------------------------------------------------------------------


def test_id_reuse_after_pop_is_new_entry() -> None:
    clock = FakeClock(5.0)
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="x", text="first"))
    popped = inbox.pop_turn_entry()
    assert popped is not None and popped.id == "x" and popped.first_at == 5.0

    clock.advance(7)  # 12
    assert inbox.push(Entry(id="x", text="second")) == "accepted"
    entry = inbox.peek_turn_entry()
    assert entry is not None
    assert entry.text == "second"
    assert entry.first_at == 12.0  # 当作全新条目
    assert entry.last_at == 12.0
    assert inbox.depth() == 1


# ---------------------------------------------------------------------------
# 6. 合批窗口
# ---------------------------------------------------------------------------


def test_batch_quiet_gap_default() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    assert not inbox.batch_due()  # BATCHED 为空
    inbox.push(Entry(id="a", text="A", bucket=Bucket.BATCHED))
    clock.advance(2.4)
    assert not inbox.batch_due()
    clock.advance(0.1)  # 2.5
    assert inbox.batch_due()


def test_batch_min_age_holds_back_early_flush() -> None:
    clock = FakeClock()
    inbox = Inbox(
        batch_window=BatchWindow(quiet_gap=1.0, min_age=5.0, max_age=100.0),
        clock=clock,
    )
    inbox.push(Entry(id="a", text="A", bucket=Bucket.BATCHED))
    clock.advance(4.9)
    assert not inbox.batch_due()  # 至少攒 5s
    clock.advance(0.1)
    assert inbox.batch_due()


def test_batch_max_age_caps_quiet_gap() -> None:
    clock = FakeClock()
    inbox = Inbox(
        batch_window=BatchWindow(quiet_gap=100.0, min_age=0.0, max_age=10.0),
        clock=clock,
    )
    inbox.push(Entry(id="a", text="A", bucket=Bucket.BATCHED))
    clock.advance(9.9)
    assert not inbox.batch_due()
    clock.advance(0.1)
    assert inbox.batch_due()


def test_batch_uses_first_and_last_anchors() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)  # quiet_gap=2.5 / max_age=15
    inbox.push(Entry(id="a", text="A", bucket=Bucket.BATCHED))
    clock.advance(2)
    inbox.push(Entry(id="b", text="B", bucket=Bucket.BATCHED))
    # first=0, last=2 ⇒ deliver_at = min(15, max(0, 4.5)) = 4.5
    clock.advance(2.4)  # 4.4
    assert not inbox.batch_due()
    clock.advance(0.1)  # 4.5
    assert inbox.batch_due()


def test_batch_max_size_flushes_immediately() -> None:
    clock = FakeClock()
    inbox = Inbox(batch_window=BatchWindow(max_size=3), clock=clock)
    inbox.push(Entry(id="a", text="A", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="b", text="B", bucket=Bucket.BATCHED))
    assert not inbox.batch_due()
    inbox.push(Entry(id="c", text="C", bucket=Bucket.BATCHED))
    assert inbox.batch_due()  # 到量立即冲刷，不等安静期


# ---------------------------------------------------------------------------
# 7. ENVIRONMENT 不参与合批
# ---------------------------------------------------------------------------


def test_environment_never_counts_toward_batch() -> None:
    clock = FakeClock()
    inbox = Inbox(batch_window=BatchWindow(max_size=1), clock=clock)
    for index in range(5):
        inbox.push(Entry(id=f"e{index}", text="E", bucket=Bucket.ENVIRONMENT))

    assert not inbox.batch_due()  # 再多的 ENVIRONMENT 也不触发合批
    assert inbox.next_wakeup() is None  # 且没有 timeout 时没有定时

    inbox.push(Entry(id="t", text="T", bucket=Bucket.ENVIRONMENT, timeout=5))
    assert inbox.next_wakeup() == 5  # 只有它自己的 deadline 参与唤醒


def test_context_entries_are_batched_plus_environment_in_queue_order() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="b1", text="B1", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="m", text="M"))
    inbox.push(Entry(id="b2", text="B2", bucket=Bucket.BATCHED))

    assert [e.id for e in inbox.context_entries()] == ["b1", "e1", "b2"]
    assert [e.id for e in inbox.pop_context_entries()] == ["b1", "e1", "b2"]
    assert inbox.depth() == 1
    assert inbox.pop_turn_entry().id == "m"


# ---------------------------------------------------------------------------
# 7b. take_due_batch（合批到期 ⇒ 整批开回合）
# ---------------------------------------------------------------------------


def test_take_due_batch_waits_for_window() -> None:
    clock = FakeClock()
    inbox = Inbox(batch_window=BatchWindow(quiet_gap=1.0), clock=clock)
    inbox.push(Entry(id="b1", text="B1", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="b2", text="B2", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="m1", text="M1"))

    clock.advance(0.5)
    assert inbox.take_due_batch() == [], "窗口没到期：队列不动"
    assert inbox.depth() == 4

    clock.advance(0.5)  # 安静期到了
    entries = inbox.take_due_batch()
    assert [entry.id for entry in entries] == ["b1", "e1", "b2"], "整批 + 搭车项，队列序"
    assert inbox.depth() == 1, "MAJOR 不受影响"
    assert inbox.pop_turn_entry().id == "m1"
    assert inbox.take_due_batch() == []  # 已经取空


def test_take_due_batch_needs_a_batched_entry() -> None:
    clock = FakeClock()
    inbox = Inbox(batch_window=BatchWindow(max_size=1), clock=clock)
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="m1", text="M1"))
    clock.advance(100)
    assert inbox.take_due_batch() == [], "只有 ENVIRONMENT 时永不开回合"
    assert inbox.depth() == 2


# ---------------------------------------------------------------------------
# 7c. take_environment（回合中途按 env_interval 节流投递）
# ---------------------------------------------------------------------------


def test_take_environment_waits_for_interval() -> None:
    clock = FakeClock()
    inbox = Inbox(env_interval=30.0, clock=clock)
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="b1", text="B1", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="m1", text="M1"))

    assert [e.id for e in inbox.take_environment()] == ["e1"], "从未投递过 ⇒ 立即可投"
    assert inbox.depth() == 2, "只取 ENVIRONMENT，BATCHED / MAJOR 不动"
    assert inbox.env_interval_remaining() == 30.0

    inbox.push(Entry(id="e2", text="E2", bucket=Bucket.ENVIRONMENT))
    clock.advance(29.9)
    assert not inbox.env_due()
    assert inbox.take_environment() == [], "未到期：队列一格不动"
    assert inbox.depth() == 3

    clock.advance(0.1)
    assert inbox.env_due()
    assert [e.id for e in inbox.take_environment()] == ["e2"]
    assert inbox.depth() == 2


def test_environment_timer_is_reset_by_turn_start_and_batch() -> None:
    clock = FakeClock()
    inbox = Inbox(batch_window=BatchWindow(max_size=1), env_interval=30.0, clock=clock)
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))

    assert [e.id for e in inbox.pop_context_entries()] == ["e1"], "回合开始搭车"
    assert inbox.env_due() is False, "回合开始投递过环境 ⇒ 计时器重置"

    clock.advance(30)
    inbox.push(Entry(id="e2", text="E2", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="b1", text="B1", bucket=Bucket.BATCHED))
    assert inbox.batch_due()
    assert [e.id for e in inbox.take_due_batch()] == ["e2", "b1"], "批回合整批取走"
    assert inbox.env_due() is False, "批回合投递过环境 ⇒ 计时器同样重置"


def test_take_environment_with_zero_interval_always_takes() -> None:
    clock = FakeClock()
    inbox = Inbox(env_interval=0.0, clock=clock)
    inbox.push(Entry(id="e1", text="E1", bucket=Bucket.ENVIRONMENT))
    inbox.push(Entry(id="e2", text="E2", bucket=Bucket.ENVIRONMENT))
    assert [e.id for e in inbox.take_environment()] == ["e1", "e2"]

    assert inbox.take_environment() == [], "空队列"
    inbox.push(Entry(id="e3", text="E3", bucket=Bucket.ENVIRONMENT))
    assert [e.id for e in inbox.take_environment()] == ["e3"], "间隔 0 ⇒ 每次都能投"


# ---------------------------------------------------------------------------
# 8. next_wakeup
# ---------------------------------------------------------------------------


def test_next_wakeup_is_min_of_deadline_and_batch() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    assert inbox.next_wakeup() is None

    inbox.push(Entry(id="m", text="M", timeout=10))
    assert inbox.next_wakeup() == 10

    inbox.push(Entry(id="b", text="B", bucket=Bucket.BATCHED))
    assert inbox.next_wakeup() == 2.5  # 合批比 deadline 更早

    clock.advance(3)
    assert inbox.next_wakeup() == 0.0  # 已到期按 0 处理

    inbox.pop_context_entries()
    assert inbox.next_wakeup() == 7.0

    clock.advance(7)
    assert inbox.next_wakeup() == 0.0  # deadline 恰好到期


# ---------------------------------------------------------------------------
# 9. wait()
# ---------------------------------------------------------------------------


async def test_wait_returns_when_pushed() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    waiter = asyncio.create_task(inbox.wait())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    inbox.push(Entry(id="x", text="X"))
    await asyncio.wait_for(waiter, timeout=1.0)

    # 信号已被清掉：没有新 push 时再次 wait 继续阻塞
    again = asyncio.create_task(inbox.wait())
    await asyncio.sleep(0.01)
    assert not again.done()
    again.cancel()
    with pytest.raises(asyncio.CancelledError):
        await again


async def test_wait_timeout_returns_silently() -> None:
    inbox = Inbox(clock=FakeClock())
    await asyncio.wait_for(inbox.wait(0.02), timeout=1.0)


async def test_wait_wakes_on_dropped_push() -> None:
    clock = FakeClock()
    inbox = Inbox(queue_max=0, clock=clock)
    waiter = asyncio.create_task(inbox.wait())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    assert inbox.push(Entry(id="x", text="X", flags=Flag.DROPPABLE)) == "dropped"
    await asyncio.wait_for(waiter, timeout=1.0)  # 丢弃同样唤醒


# ---------------------------------------------------------------------------
# 10. clear()
# ---------------------------------------------------------------------------


def test_clear_returns_all_in_queue_order() -> None:
    clock = FakeClock()
    inbox = Inbox(clock=clock)
    inbox.push(Entry(id="a", text="A"))
    inbox.push(Entry(id="b", text="B", bucket=Bucket.BATCHED))
    inbox.push(Entry(id="c", text="C", bucket=Bucket.IMMEDIATE))

    entries = inbox.clear()
    assert [e.id for e in entries] == ["a", "b", "c"]  # 队列序
    assert inbox.depth() == 0
    assert inbox.peek_turn_entry() is None
    assert inbox.context_entries() == []

    # 清空后可以继续使用，同 id 视为新条目
    assert inbox.push(Entry(id="a", text="A2", timeout=3)) == "accepted"
    entry = inbox.peek_turn_entry()
    assert entry is not None and entry.first_at == 0.0 and entry.deadline == 3.0


# ---------------------------------------------------------------------------
# 附加：入参校验 / context 字段
# ---------------------------------------------------------------------------


def test_push_rejects_empty_id() -> None:
    inbox = Inbox(clock=FakeClock())
    with pytest.raises(ValueError):
        inbox.push(Entry(id="", text="X"))
    assert inbox.depth() == 0


def test_auto_flag_is_mutually_exclusive_with_immediate() -> None:
    inbox = Inbox(clock=FakeClock())
    with pytest.raises(ValueError):
        inbox.push(Entry(id="x", text="X", bucket=Bucket.IMMEDIATE, flags=Flag.AUTO))
    assert inbox.depth() == 0

    # AUTO 对 MAJOR / BATCHED 合法
    assert inbox.push(Entry(id="m", text="M", flags=Flag.AUTO)) == "accepted"


def test_push_normalizes_context_and_sets_timestamps() -> None:
    clock = FakeClock(42.0)
    inbox = Inbox(clock=clock)
    item = ContextItem(source="test", text="ctx")
    entry = Entry(id="x", text="X", timeout=5, context=[item])
    assert inbox.push(entry) == "accepted"

    assert entry.context == (item,)
    assert isinstance(entry.context, tuple)
    assert entry.first_at == entry.last_at == entry.pushed_at == 42.0
    assert entry.deadline == 47.0
