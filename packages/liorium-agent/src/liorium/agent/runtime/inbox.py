"""Inbox：入站事件队列（bucket / flags / timeout / 同 id 原位替换）。

定稿 §4（入站语义）与 §5.1（队列纪律）的落点：

- **``Bucket`` 只表达「何时投递」**：``IMMEDIATE`` / ``MAJOR`` 立刻排回合，
  ``BATCHED`` 攒够窗口后**开自己的回合**（回合没跑就在窗口到期时开，跑着就排队等它结束），
  ``ENVIRONMENT`` 永不开回合：回合开始时作为 context 搭车，回合进行中则在安全点
  按 ``env_interval`` 节流投递（见 :meth:`Inbox.take_environment`）；
- **``Flag``**：``AUTO``（运行时决定形态，仅对 ``MAJOR`` 有意义）、
  ``EXCLUSIVE``（拒绝被 ``IMMEDIATE`` 抢占，由 worker 裁决）、
  ``DROPPABLE``（深度达 ``QUEUE_MAX`` 时立即丢弃）；
- **同 id 原位替换**：新条目占据旧条目的队列位置，继承合批锚点
  ``first_at`` / ``last_at``（高频状态更新不会把批次无限推迟），
  ``timeout`` 从本次 push 重新起算；
- **timeout = 消费截止时间**：``deadline`` 由 ``push`` 时刻的单调时钟算出，
  过期扫描是**惰性**的（``expire()`` 由 worker 取条目 / 唤醒时调用），
  本类不额外起定时器 task；
- **合批四旋钮**：``deliver_at = min(first + max_age,
  max(first + min_age, last + quiet_gap))``，到 ``max_size`` 立即冲刷。

线程模型：同一事件循环内单任务使用即可，不加锁。``wait()`` 只是把
"有新条目"这件事变成可等待的信号，真正的唤醒时机（deadline / 合批到期）
由 ``next_wakeup()`` 告诉 worker。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, IntFlag
from typing import Callable

from liorium.core.types import ContextItem

QUEUE_MAX = 50
"""Inbox 深度上限。只有带 ``Flag.DROPPABLE`` 的条目会因此被立即丢弃。"""


class Bucket(str, Enum):
    """条目「何时投递」，不表达业务优先级。

    - ``IMMEDIATE``：立刻投递（可取消在途且未外化的模型轮）；
    - ``MAJOR``：排队，等当前回合结束；
    - ``BATCHED``：参与合批窗口，**窗口到期后开回合**（整批合成一条 user 消息）；
    - ``ENVIRONMENT``：不触发投递、不刷新合批计时器、永不开回合；回合开始时搭车，
      回合进行中每 ``env_interval`` 秒至多投递一次。

    桶之间没有优先级阶梯：除了 ``IMMEDIATE`` 插队，其余按 FIFO。
    """

    IMMEDIATE = "immediate"
    MAJOR = "major"
    BATCHED = "batched"
    ENVIRONMENT = "environment"

    @property
    def opens_turn(self) -> bool:
        """是否开启回合：除 ``ENVIRONMENT`` 外都为 ``True``。

        ``BATCHED`` 是「**窗口到期后**」才开（自己的合批窗口），不是入队即开。
        """
        return self is not Bucket.ENVIRONMENT


class Flag(IntFlag):
    """入站条目的正交标志，默认 ``NONE``。

    - ``EXCLUSIVE``：本条开启的回合拒绝被 ``IMMEDIATE`` 抢占（不挡显式
      ``interrupt()`` / ``close()``）；
    - ``DROPPABLE``：Inbox 深度 ``>= queue_max`` 时立即丢弃；
    - ``AUTO``：由运行时决定形态（空闲开回合、忙碌注入当前回合）。
      只对 ``MAJOR`` 有意义，与 ``IMMEDIATE`` 互斥。
    """

    NONE = 0
    EXCLUSIVE = 1
    DROPPABLE = 2
    AUTO = 4


@dataclass(frozen=True)
class BatchWindow:
    """``BATCHED`` 的合批窗口（四旋钮）。

    ``deliver_at = min(first + max_age, max(first + min_age, last + quiet_gap))``：
    最后一条之后安静 ``quiet_gap`` 秒就到期；最早一条至少攒 ``min_age`` 秒；
    无论如何不超过 ``max_age`` 秒；条目数达 ``max_size`` 立即到期。
    """

    quiet_gap: float = 2.5
    min_age: float = 0.0
    max_age: float = 15.0
    max_size: int = 100


@dataclass
class Entry:
    """一条入站条目。

    前七个字段由调用方填写；``input_ids`` 由批回合填写；后四个由 :meth:`Inbox.push`
    填（入队时刻、合批锚点、消费截止时间），调用方不必关心。
    """

    id: str
    text: str
    bucket: Bucket = Bucket.MAJOR
    flags: Flag = Flag.NONE
    origin: str = "user"
    timeout: float | None = None
    context: tuple[ContextItem, ...] = ()
    input_ids: tuple[str, ...] = ()
    """本条目在事件流上代表的一组入站 id（→ ``start.inputs``）；空 = 只用 ``id``。

    批回合一次代表整批，所以它填的是整批 id（队列序）。
    """
    # 以下由 Inbox.push 填
    first_at: float = 0.0
    last_at: float = 0.0
    pushed_at: float = 0.0
    deadline: float | None = None


class Inbox:
    """入站队列：全局一条 FIFO，按 bucket 提供不同的取用视图。

    不变量：

    - ``_entries`` 是唯一的队列序（入队顺序），``_by_id`` 指向当前生效的
      条目对象；
    - 同 id 替换只换对象、不动位置；
    - 过期 / 丢弃的条目从两个结构里同时摘除，不连坐其它条目。
    """

    def __init__(
        self,
        *,
        batch_window: BatchWindow | None = None,
        env_interval: float = 30.0,
        queue_max: int = QUEUE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._batch_window = batch_window or BatchWindow()
        self._env_interval = float(env_interval)
        self._queue_max = int(queue_max)
        self._clock = clock
        self._entries: deque[Entry] = deque()
        self._by_id: dict[str, Entry] = {}
        # 上一次**投递**环境条目的时刻（回合开始 / 批回合 / 安全点三条路径都算），
        # 不是"上一次 push"：节流按投递算。
        self._env_at: float | None = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def batch_window(self) -> BatchWindow:
        """当前合批窗口配置。"""
        return self._batch_window

    @property
    def env_interval(self) -> float:
        """``ENVIRONMENT`` 中途投递的最小间隔（秒）；``<= 0`` ⇒ 每次安全点都投。"""
        return self._env_interval

    def env_interval_remaining(self) -> float:
        """距下一次可投递环境还剩几秒（未投递过 ⇒ ``0.0``）。"""
        if self._env_at is None:
            return 0.0
        return max(0.0, self._env_interval - (self._clock() - self._env_at))

    @property
    def queue_max(self) -> int:
        """``Flag.DROPPABLE`` 的丢弃阈值（深度 ``>= queue_max`` 即丢）。"""
        return self._queue_max

    def depth(self) -> int:
        """待处理条目总数（所有 bucket）。"""
        return len(self._entries)

    def depth_by_bucket(self) -> dict[Bucket, int]:
        """每个 bucket 的待处理条数；四个桶都会出现（没有则为 0）。"""
        counts = {bucket: 0 for bucket in Bucket}
        for entry in self._entries:
            counts[entry.bucket] += 1
        return counts

    # ------------------------------------------------------------------
    # 入站
    # ------------------------------------------------------------------

    def push(self, entry: Entry) -> str:
        """投递一条条目，返回 ``"accepted"`` 或 ``"dropped"``。

        - ``id`` 为空 ⇒ ``ValueError``；``AUTO`` 与 ``IMMEDIATE`` 互斥 ⇒
          ``ValueError``；
        - ``DROPPABLE`` 且当前深度 ``>= queue_max`` ⇒ 丢弃（不入队）；
        - 同 id 已 pending ⇒ **原位替换**：位置不变，继承 ``first_at`` /
          ``last_at``，``timeout`` 从本次 push 时刻重新起算；
        - 无论接受还是丢弃，都唤醒 ``wait()`` 的等待者。
        """
        if not entry.id:
            raise ValueError("Entry.id 不能为空")

        entry.bucket = Bucket(entry.bucket)
        entry.flags = Flag(entry.flags)
        if entry.flags & Flag.AUTO and entry.bucket is Bucket.IMMEDIATE:
            raise ValueError("Flag.AUTO 与 Bucket.IMMEDIATE 互斥")
        entry.context = tuple(entry.context)

        now = self._clock()
        if entry.flags & Flag.DROPPABLE and self.depth() >= self._queue_max:
            self._wake.set()
            return "dropped"

        old = self._by_id.get(entry.id)
        if old is not None:
            entry.first_at = old.first_at
            entry.last_at = old.last_at
            entry.pushed_at = now
            entry.deadline = now + entry.timeout if entry.timeout is not None else None
            self._entries[self._index(old)] = entry
            self._by_id[entry.id] = entry
        else:
            entry.first_at = entry.last_at = entry.pushed_at = now
            entry.deadline = now + entry.timeout if entry.timeout is not None else None
            self._entries.append(entry)
            self._by_id[entry.id] = entry

        self._wake.set()
        return "accepted"

    # ------------------------------------------------------------------
    # 过期
    # ------------------------------------------------------------------

    def expire(self) -> list[Entry]:
        """移除并返回已过期条目（按队列序），其它条目不受影响。

        判据：``deadline is not None and deadline <= now``。惰性调用：
        worker 取条目前、或被定时唤醒后扫一遍。
        """
        now = self._clock()
        expired: list[Entry] = []
        kept: deque[Entry] = deque()
        for entry in self._entries:
            if entry.deadline is not None and entry.deadline <= now:
                self._by_id.pop(entry.id, None)
                expired.append(entry)
            else:
                kept.append(entry)
        self._entries = kept
        return expired

    # ------------------------------------------------------------------
    # 开回合条目
    # ------------------------------------------------------------------

    def peek_turn_entry(self) -> Entry | None:
        """最该开回合的条目（不弹出）：``IMMEDIATE`` 全体先于 ``MAJOR``，同档 FIFO。"""
        fallback: Entry | None = None
        for entry in self._entries:
            if entry.bucket is Bucket.IMMEDIATE:
                return entry
            if entry.bucket is Bucket.MAJOR and fallback is None:
                fallback = entry
        return fallback

    def pop_turn_entry(self) -> Entry | None:
        """取出并移除 :meth:`peek_turn_entry` 选中的条目。"""
        entry = self.peek_turn_entry()
        if entry is not None:
            self._remove(entry)
        return entry

    def has_immediate(self) -> bool:
        """队列里是否有 ``IMMEDIATE`` 条目。"""
        return any(entry.bucket is Bucket.IMMEDIATE for entry in self._entries)

    def pop_immediate(self) -> Entry | None:
        """取出并移除最前面的 ``IMMEDIATE``；没有则返回 ``None``。"""
        for entry in self._entries:
            if entry.bucket is Bucket.IMMEDIATE:
                self._remove(entry)
                return entry
        return None

    def take_due_batch(self) -> list[Entry]:
        """合批窗口到期：取出**整批**（``BATCHED`` + ``ENVIRONMENT``，队列序）。

        :meth:`batch_due` 为假时返回 ``[]`` 且队列不动。``BATCHED`` 共用一条窗口，
        所以"到期"对整批生效：worker 拿它直接开一个批回合。
        """
        if not self.batch_due():
            return []
        return self._take_context(self.context_entries())

    # ------------------------------------------------------------------
    # context 类条目
    # ------------------------------------------------------------------

    def context_entries(self) -> list[Entry]:
        """``BATCHED`` + ``ENVIRONMENT``（队列序，不弹出）。

        三条路径共用：形态①（任一回合开始时整批并入该回合）、
        :meth:`take_due_batch`（窗口到期 ⇒ 整批自己开一个回合）、
        :meth:`take_environment`（回合进行中按节流投递环境）。
        """
        return [
            entry
            for entry in self._entries
            if entry.bucket in (Bucket.BATCHED, Bucket.ENVIRONMENT)
        ]

    def pop_context_entries(self) -> list[Entry]:
        """取出并移除全部 context 类条目（队列序）。

        回合开始时无条件调用：这里**不受** ``env_interval`` 节流（回合开头是快照点），
        但会重置环境计时器。
        """
        return self._take_context(self.context_entries())

    def env_due(self) -> bool:
        """``ENVIRONMENT`` 是否可以中途投递了（距上次投递满 ``env_interval``）。

        从未投递过 ⇒ ``True``（首条环境立刻给）；``env_interval <= 0`` ⇒ 恒 ``True``。
        """
        if self._env_at is None:
            return True
        return self._clock() - self._env_at >= self._env_interval

    def take_environment(self) -> list[Entry]:
        """回合进行中（安全点）取用环境条目：未到期 ``[]`` 且队列不动。

        到期时取走**全部** ``ENVIRONMENT``（队列序），不碰 ``BATCHED`` / ``MAJOR``；
        真的取到东西才重置计时器。
        """
        if not self.env_due():
            return []
        return self._take_context(
            [entry for entry in self._entries if entry.bucket is Bucket.ENVIRONMENT]
        )

    # ------------------------------------------------------------------
    # 合批与唤醒
    # ------------------------------------------------------------------

    def batch_due(self) -> bool:
        """``BATCHED`` 是否该开批回合了（按条数或四旋钮窗口）。"""
        batched = self._batched()
        if not batched:
            return False
        if len(batched) >= self._batch_window.max_size:
            return True
        deliver_at = self._batch_deliver_at(batched)
        return self._clock() >= deliver_at

    def next_wakeup(self) -> float | None:
        """距下一个「该醒来的时刻」的秒数；``None`` = 无定时。

        候选：所有条目的 ``deadline``，以及有 ``BATCHED`` 时的合批
        ``deliver_at``。负值按 0 处理（已经到期，应当立刻醒来）。
        """
        now = self._clock()
        candidates: list[float] = [
            entry.deadline - now for entry in self._entries if entry.deadline is not None
        ]
        batched = self._batched()
        if batched:
            candidates.append(self._batch_deliver_at(batched) - now)
        if not candidates:
            return None
        return max(0.0, min(candidates))

    # ------------------------------------------------------------------
    # 清空与等待
    # ------------------------------------------------------------------

    def clear(self) -> list[Entry]:
        """清空队列并返回全部条目（队列序），供 ``reset_dialog`` 逐条发 dropped。"""
        entries = list(self._entries)
        self._entries.clear()
        self._by_id.clear()
        return entries

    async def wait(self, timeout: float | None = None) -> None:
        """等「有新条目 / 有条目被丢弃」的信号，或等 ``timeout`` 秒后返回。

        ``timeout=None`` 表示无限等。被唤醒后清掉信号，保证下一次 ``wait()``
        从新的信号开始等；超时静默返回（由调用方用 ``expire()`` /
        ``next_wakeup()`` 决定后续）。
        """
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _batched(self) -> list[Entry]:
        return [entry for entry in self._entries if entry.bucket is Bucket.BATCHED]

    def _batch_deliver_at(self, batched: list[Entry]) -> float:
        window = self._batch_window
        first = min(entry.first_at for entry in batched)
        last = max(entry.last_at for entry in batched)
        return min(
            first + window.max_age,
            max(first + window.min_age, last + window.quiet_gap),
        )

    def _take_context(self, entries: list[Entry]) -> list[Entry]:
        """摘除一组 context 类条目；只要里面有 ``ENVIRONMENT`` 就重置环境计时器。"""
        for entry in entries:
            self._remove(entry)
        if any(entry.bucket is Bucket.ENVIRONMENT for entry in entries):
            self._env_at = self._clock()
        return entries

    def _index(self, entry: Entry) -> int:
        for index, item in enumerate(self._entries):
            if item is entry:
                return index
        raise LookupError(f"条目不在队列中: {entry.id}")

    def _remove(self, entry: Entry) -> None:
        del self._entries[self._index(entry)]
        self._by_id.pop(entry.id, None)


__all__ = ["QUEUE_MAX", "BatchWindow", "Bucket", "Entry", "Flag", "Inbox"]
