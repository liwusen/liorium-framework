"""事件扇出中心：一条事件流，多个彼此独立的订阅者。

设计依据：``__dev__/Liorium Event 事件流原生 定稿.md`` §6 / §7。

契约：

- 每个订阅者一条**无界** ``asyncio.Queue``；``publish`` 只做 ``put_nowait``，
  永不等待、永不抛异常，慢订阅者 / 抛错订阅者只影响它自己；
- **不回放**：订阅者只收到注册之后发布的事件；
- **注册发生在 ``__aiter__``**（``async for`` 开始的那一刻），``subscribe()``
  只构造对象、不建队列、不注册，避免 ``s = agent.stream()`` 之后不迭代的泄漏；
  注册幂等，``aclose()`` 幂等；
- ``close()`` 让所有订阅立即结束（``StopAsyncIteration``），之后 ``publish``
  静默忽略，新的订阅迭代也立即结束；
- ``ids`` 过滤：只放行 ``start.inputs`` 与集合有交集的回合（包含其终态帧），
  以及 ``id`` 命中集合的 ``input`` 生命周期帧；不带 ``turn_id`` 的帧
  （如 ``compact_*``）归属当前回合。

本模块只依赖 ``liorium.core.types`` 与标准库。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from liorium.core.types import Event

_TERMINAL_TYPES = frozenset({"done", "interrupted", "error"})
"""回合终态帧类型：每个回合恰好一条，且是该回合最后一条事件。"""


class EventHub:
    """同步发布、异步订阅的事件扇出中心。

    ``publish`` 是同步方法，因此可以在任意事件循环 / 回调里调用；订阅者
    通过 ``subscribe()`` 拿到一个异步迭代器。
    """

    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        """是否已经关闭（关闭后发布会静默丢弃，订阅立即结束）。"""
        return self._closed

    def publish(self, event: Event) -> None:
        """把事件投递给每个订阅者（不等待、不抛异常）。

        遍历的是订阅者快照：投递过程中有人 ``aclose`` / 被 GC 不影响遍历；
        某个订阅者抛错只把它自己摘掉，其余订阅者照常收到事件。
        """
        if self._closed:
            return
        for subscription in tuple(self._subscribers):
            try:
                subscription._offer(event)
            except Exception:
                self._subscribers.discard(subscription)

    def subscribe(self, *, ids: Sequence[str] | None = None) -> Subscription:
        """构造一个订阅对象；真正的注册发生在开始迭代（``__aiter__``）时。

        ``ids=None`` 表示全量；``ids=[...]`` 表示只订阅这些入站 id 相关的事件。
        """
        return Subscription(self, ids)

    def close(self) -> None:
        """关闭扇出中心：结束所有订阅、清空订阅表，之后发布静默忽略。"""
        if self._closed:
            return
        self._closed = True
        for subscription in tuple(self._subscribers):
            try:
                subscription._shutdown()
            except Exception:
                pass
        self._subscribers.clear()

    def subscriber_count(self) -> int:
        """当前已注册的订阅者数量（未开始迭代的订阅对象不计）。"""
        return len(self._subscribers)

    def _register(self, subscription: Subscription) -> None:
        """登记订阅者；若此刻已关闭则直接把它标记为结束。"""
        if self._closed:
            subscription._shutdown()
            return
        self._subscribers.add(subscription)

    def _unregister(self, subscription: Subscription) -> None:
        """注销订阅者（幂等，未注册时无副作用）。"""
        self._subscribers.discard(subscription)


class Subscription:
    """单个订阅者的异步迭代器。

    不要直接构造：用 ``EventHub.subscribe()``。对象在 ``__aiter__`` 之前
    没有任何资源；``aclose()`` 之后再次迭代会立即结束。
    """

    def __init__(self, hub: EventHub, ids: Sequence[str] | None = None) -> None:
        self._hub = hub
        self._ids = frozenset(ids) if ids is not None else None
        self._queue: asyncio.Queue[Event | None] | None = None
        self._registered = False
        self._done = False
        # ids 过滤的回合状态：当前回合 id 与是否已放行。
        self._turn_id: str | None = None
        self._turn_allowed = False

    def __aiter__(self) -> AsyncIterator[Event]:
        """开始迭代即注册（幂等）；``subscribe()`` 本身不注册。"""
        self._ensure_registered()
        return self

    async def __anext__(self) -> Event:
        """取下一条事件；订阅结束或已被关闭时抛 ``StopAsyncIteration``。

        hub 关闭时队列里已经排队的帧会先被读空，再遇到结束信号 —— 这样
        ``close()`` 之前发出的终态帧（如 ``interrupted(close)``）不会丢。
        """
        self._ensure_registered()
        if self._done:
            raise StopAsyncIteration
        queue = self._queue
        if queue is None:  # pragma: no cover - _ensure_registered 保证已建队列
            raise StopAsyncIteration
        item = await queue.get()
        if item is None:
            self._done = True
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        """关闭本订阅（幂等）：之后 ``__anext__`` 立即抛 ``StopAsyncIteration``。"""
        if self._done:
            return
        self._done = True
        self._hub._unregister(self)
        if self._queue is not None:
            # 唤醒可能正阻塞在 queue.get() 上的读取者。
            self._queue.put_nowait(None)

    async def __aenter__(self) -> Subscription:
        self._ensure_registered()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def __del__(self) -> None:
        """被 GC 时尽力注销；绝不让析构抛异常。"""
        try:
            self._hub._unregister(self)
        except BaseException:
            pass

    # ------------------------------------------------------------------
    # 内部：注册 / 投递 / 过滤
    # ------------------------------------------------------------------

    def _ensure_registered(self) -> None:
        """惰性建立队列并登记（幂等）。"""
        if self._registered or self._done:
            return
        self._registered = True
        if self._queue is None:
            self._queue = asyncio.Queue()
        self._hub._register(self)

    def _shutdown(self) -> None:
        """由 ``EventHub.close()`` 调用：投递结束信号（已排队的帧先被消费）。"""
        if self._done:
            return
        if self._queue is None:
            self._done = True
            return
        self._queue.put_nowait(None)

    def _offer(self, event: Event) -> None:
        """把事件放入本订阅者的队列（不等待）。过滤失败由 hub 隔离。"""
        if self._done:
            return
        queue = self._queue
        if queue is None:
            return
        if self._ids is not None and not self._accepts(event):
            return
        queue.put_nowait(event)

    def _accepts(self, event: Event) -> bool:
        """``ids`` 过滤：判断该事件是否属于本订阅者关心的回合 / 入站条目。"""
        ids = self._ids
        if ids is None:  # pragma: no cover - 调用方只在有过滤时进来
            return True

        event_type = event.type
        if event_type == "input":
            # 入站生命周期帧只看自己的 id，不依赖回合状态。
            return event.get("id") in ids

        turn_id = event.get("turn_id")
        if event_type == "start":
            inputs = event.get("inputs") or ()
            allowed = any(input_id in ids for input_id in inputs)
            self._turn_id = None if turn_id is None else str(turn_id)
            self._turn_allowed = allowed
            return allowed

        if turn_id is not None:
            # 明确带回合 id 的帧：只认当前回合，陌生回合（含已结束的）一律丢弃。
            if self._turn_id is None or str(turn_id) != self._turn_id:
                return False
            allowed = self._turn_allowed
            if event_type in _TERMINAL_TYPES:
                self._clear_turn()
            return allowed

        # 不带 turn_id 的帧（compact_* 等）归属当前回合。
        allowed = self._turn_allowed
        if event_type in _TERMINAL_TYPES:
            self._clear_turn()
        return allowed

    def _clear_turn(self) -> None:
        """清掉当前回合的放行状态（终态帧之后不再放行该回合的帧）。"""
        self._turn_id = None
        self._turn_allowed = False


__all__ = ["EventHub", "Subscription"]
