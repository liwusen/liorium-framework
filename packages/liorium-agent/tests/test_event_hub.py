"""``runtime/hub.py`` 的扇出契约测试（设计稿 §6 / §7 的 P0 部分）。

覆盖：多订阅者各收全量、不回放、慢订阅者不阻塞发布、``ids`` 回合过滤、
``aclose`` 幂等、关闭后的结束语义、单订阅者异常隔离。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from liorium.agent.runtime.hub import EventHub, Subscription
from liorium.core.types import (
    Event,
    ev_compact_delta,
    ev_compact_start,
    ev_delta,
    ev_done,
    ev_error,
    ev_input,
    ev_start,
)

_TIMEOUT = 1.0


async def _take(subscription: Subscription, count: int) -> list[Event]:
    """按顺序取 ``count`` 条事件；超时即失败（防止实现卡死）。"""
    return [await asyncio.wait_for(subscription.__anext__(), _TIMEOUT) for _ in range(count)]


async def _assert_exhausted(subscription: Subscription) -> None:
    """断言此刻没有新事件（用于验证过滤 / 不回放）。"""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscription.__anext__(), 0.05)


@pytest.fixture
def hub() -> Iterator[EventHub]:
    hub = EventHub()
    try:
        yield hub
    finally:
        hub.close()


def test_subscribe_does_not_register_until_iterated(hub: EventHub) -> None:
    """``subscribe()`` 只构造对象：不建队列、不登记（避免 stream() 后不迭代的泄漏）。"""
    sub = hub.subscribe()
    assert hub.subscriber_count() == 0
    sub.__aiter__()
    assert hub.subscriber_count() == 1


async def test_two_subscribers_each_receive_everything(hub: EventHub) -> None:
    first = hub.subscribe()
    second = hub.subscribe()
    first.__aiter__()
    second.__aiter__()

    events = [
        ev_start("t1", inputs=["a"]),
        ev_delta("你"),
        ev_delta("好"),
        ev_done("t1"),
    ]
    for event in events:
        hub.publish(event)

    assert [event.type for event in await _take(first, len(events))] == [
        "start",
        "delta",
        "delta",
        "done",
    ]
    assert [event.type for event in await _take(second, len(events))] == [
        "start",
        "delta",
        "delta",
        "done",
    ]


async def test_no_replay_from_before_subscription(hub: EventHub) -> None:
    """订阅只从注册时刻起收事件：先 publish 的旧帧永远收不到。"""
    hub.publish(ev_delta("old"))
    sub = hub.subscribe()
    sub.__aiter__()
    hub.publish(ev_delta("new"))

    assert (await _take(sub, 1))[0].get("content") == "new"
    await _assert_exhausted(sub)


async def test_slow_subscriber_never_blocks_publish(hub: EventHub) -> None:
    """不消费的订阅者只堆积自己的无界队列，发布方永不等待。"""
    slow = hub.subscribe()
    slow.__aiter__()  # 注册后完全不消费
    fast = hub.subscribe()
    fast.__aiter__()

    count = 300

    async def flood() -> None:
        for index in range(count):
            hub.publish(ev_delta(str(index)))

    await asyncio.wait_for(flood(), _TIMEOUT)

    received = await _take(fast, count)
    assert [event.get("content") for event in received] == [str(i) for i in range(count)]
    assert hub.subscriber_count() == 2


async def test_ids_filters_whole_turns_and_input_frames(hub: EventHub) -> None:
    """``ids`` 只放行 inputs 命中的回合（含终态）与自己的 ``input`` 帧。"""
    sub = hub.subscribe(ids=["a"])
    sub.__aiter__()

    # 还没有回合时发布的 input 帧也按 id 放行（不依赖 start）。
    hub.publish(ev_input("a", "accepted"))
    hub.publish(ev_input("b", "accepted"))

    # 命中回合：start / 无 turn_id 的 compact 帧 / body / 终态全部放行。
    hub.publish(ev_start("t1", inputs=["a"]))
    hub.publish(ev_compact_start())
    hub.publish(ev_delta("正文"))
    hub.publish(ev_done("t1"))

    # 终态之后同一回合的残留帧（无 turn_id）不得再放行。
    hub.publish(ev_delta("泄漏"))

    # 未命中回合：start / compact / body / 终态一条都不收。
    hub.publish(ev_start("t2", inputs=["b"]))
    hub.publish(ev_compact_delta("噪音"))
    hub.publish(ev_delta("噪音"))
    hub.publish(ev_error("t2", "Boom: 失败"))

    events = await _take(sub, 5)
    assert [(event.type, event.get("turn_id")) for event in events] == [
        ("input", None),
        ("start", "t1"),
        ("compact_start", None),
        ("delta", None),
        ("done", "t1"),
    ]
    await _assert_exhausted(sub)

    hub.publish(ev_input("a", "delivered"))
    hub.publish(ev_input("b", "delivered"))
    delivered = await _take(sub, 1)
    assert delivered[0].to_dict() == {"type": "input", "id": "a", "state": "delivered"}
    await _assert_exhausted(sub)


async def test_aclose_is_idempotent_and_unregisters(hub: EventHub) -> None:
    sub = hub.subscribe()
    sub.__aiter__()
    assert hub.subscriber_count() == 1

    await sub.aclose()
    await sub.aclose()
    assert hub.subscriber_count() == 0
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_aclose_before_iteration_ends_immediately(hub: EventHub) -> None:
    sub = hub.subscribe()
    await sub.aclose()
    assert hub.subscriber_count() == 0
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_async_context_manager_closes_on_exit(hub: EventHub) -> None:
    sub = hub.subscribe()
    async with sub as entered:
        assert entered is sub
        assert hub.subscriber_count() == 1
    assert hub.subscriber_count() == 0
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_close_drains_queued_frames_before_stopping(hub: EventHub) -> None:
    """close 前已入队的帧（如 ``interrupted(close)``）先送达，再结束迭代。"""
    sub = hub.subscribe()
    sub.__aiter__()
    hub.publish(ev_done("t1"))

    hub.close()
    assert (await _take(sub, 1))[0].type == "done"
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


async def test_close_ends_all_subscriptions_and_ignores_publish(hub: EventHub) -> None:
    active = hub.subscribe()
    active.__aiter__()
    assert hub.subscriber_count() == 1

    hub.close()
    assert hub.closed is True
    assert hub.subscriber_count() == 0
    with pytest.raises(StopAsyncIteration):
        await active.__anext__()

    # close 之后 publish 静默忽略，不抛异常。
    hub.publish(ev_delta("after-close"))

    # close 之后的新订阅迭代立即结束。
    late = hub.subscribe()
    late.__aiter__()
    with pytest.raises(StopAsyncIteration):
        await late.__anext__()


async def test_subscriber_exception_only_drops_that_subscriber(
    hub: EventHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个订阅者投递时抛错：摘掉它自己，其他订阅者照常收到。"""
    bad = hub.subscribe(ids=["x"])
    bad.__aiter__()
    good = hub.subscribe()
    good.__aiter__()

    def boom(_event: Event) -> None:
        raise RuntimeError("filter boom")

    monkeypatch.setattr(bad, "_offer", boom)

    hub.publish(ev_delta("一"))
    assert hub.subscriber_count() == 1
    assert (await _take(good, 1))[0].get("content") == "一"

    hub.publish(ev_delta("二"))
    assert (await _take(good, 1))[0].get("content") == "二"
