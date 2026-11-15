"""``TurnWorker``：常驻单执行位，从 Inbox 取条目、跑回合、发终态帧。

定稿 ``__dev__/Liorium Event 事件流原生 定稿.md`` §5 / §8 / §12 P2 的落点：

- ``start()`` 拉起一个常驻协程（``_loop``）；``close()`` 收掉它。调用方不再"驱动回合"；
- 队列纪律：``IMMEDIATE`` 先于 ``MAJOR``（同档 FIFO）；``BATCHED`` 合批窗口到期后
  **自己开一个回合**（批次在跑就留在队列里，等那个回合结束）；``ENVIRONMENT`` 永不
  开回合：回合开始时作为 context 搭车，回合进行中则在安全点按 ``env_interval``
  节流投递（``pull_env_context()``，形态②独立 ``<context>`` 消息）；
- 每回合**恰好一条**终态帧（``done`` / ``interrupted`` / ``error``），且是该回合最后
  一条事件：``done`` 由 ``execute_turn`` 产出，``interrupted`` / ``error`` 由本模块补齐；
- ``IMMEDIATE`` 抢占：worker 把"有待接管条目"的信号交给 ``execute_turn``，由它在本轮
  安全点中止并补 ``round_aborted``，``turn_id`` 不变（§5.3）；
- 维护操作（``apply`` / ``refresh_realms`` / ``set_realm_enabled`` / ``reset_dialog``）
  插在"当前回合结束之后、下一个 Inbox 条目之前"（§5.5）。

线程模型：worker 是**唯一**驱动回合的任务；``push()`` 只在同一事件循环里同步入队 +
置唤醒信号，永不 ``await``。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import time
from typing import TYPE_CHECKING, Any, Sequence

from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.log import get_logger
from liorium.core.types import ContextItem, Event, ev_error, ev_input, ev_interrupted

from ..realm.base import RealmTaskScope, TurnInfo, TurnOutcome
from .inbox import Bucket, Entry, Flag, Inbox
from .loop import execute_turn
from .scheduler import ExecSlot, MaintenanceRequest
from .state import current_turn
from .types import TurnRequest, TurnRuntime

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查
    from ..agent import LioriumAgent

log = get_logger("liorium.agent.worker")


class TurnWorker:
    """一个 Agent 的入站队列消费者 + 回合裁决者。"""

    def __init__(
        self,
        agent: "LioriumAgent",
        *,
        inbox: Inbox,
        hub: Any,
        clock: Any = time.monotonic,
    ) -> None:
        self._agent = agent
        self._inbox = inbox
        self._hub = hub
        self._clock = clock
        self._slot = ExecSlot()
        self._seq = itertools.count(1)
        self._loop_task: asyncio.Task[Any] | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._runtime: TurnRuntime | None = None
        self._turn_id: str | None = None
        self._turn_info: TurnInfo | None = None
        """当前回合身份（``ctx.turn`` / 钩子入参）；回合结束后清空。"""
        self._interrupt = asyncio.Event()
        self._interrupt_reason = ""
        self._preempt = asyncio.Event()
        self._wake = asyncio.Event()
        self._closing = False
        self._delivered: set[str] = set()
        """已经发过 ``input.delivered`` 的入站 id（回合开始与安全点两条路径去重）。"""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def turn_running(self) -> bool:
        """是否有回合正在执行（维护操作期间为 False）。"""
        task = self._turn_task
        return task is not None and not task.done()

    @property
    def current_turn_id(self) -> str | None:
        return self._turn_id if self.turn_running else None

    @property
    def current_turn_info(self) -> TurnInfo | None:
        """当前回合身份；没有回合在跑时为 ``None``。"""
        return self._turn_info if self.turn_running else None

    def in_turn_task(self) -> bool:
        """当前协程是否就是回合任务（维护操作禁止从回合内部重入）。"""
        task = self._turn_task
        return task is not None and asyncio.current_task() is task

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """拉起常驻循环（幂等）。由 ``LioriumAgent.start()`` 调用。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._closing = False
        self._loop_task = asyncio.create_task(
            self._loop(), name=f"worker:{self._agent.spec.id}"
        )

    async def close(self) -> None:
        """收掉当前回合 / 丢弃 pending / 关流 / 停 worker（幂等）。

        丢弃 pending **不发帧**（流马上关闭，发出去也没人消费）。
        """
        if self._closing:
            return
        self._closing = True
        self._interrupt_reason = "close"
        turn_task = self._turn_task
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        loop_task = self._loop_task
        if loop_task is not None:
            loop_task.cancel()
        for task in (turn_task, loop_task):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task
        self._turn_task = None
        self._loop_task = None
        self._inbox.clear()
        self._slot.drain(TurnError("agent 已 close"))
        self._hub.close()

    def wake(self) -> None:
        """叫醒 idle 的 worker（push / 维护操作 / 网关信号用）。"""
        self._wake.set()

    def notify_immediate(self) -> None:
        """有 ``IMMEDIATE`` 条目待接管：让在跑的回合在安全点来取。"""
        self._preempt.set()
        self._wake.set()

    def forget_delivered(self, input_id: str) -> None:
        """同 id 再次投递：允许它在新的生命周期里再发一次 ``input.delivered``。"""
        if input_id:
            self._delivered.discard(input_id)

    def interrupt(self, reason: str = "user") -> bool:
        """取消当前回合（队列保留）；返回是否真的有回合被打断。"""
        task = self._turn_task
        if task is None or task.done():
            return False
        self._interrupt_reason = reason
        self._interrupt.set()
        if self._runtime is not None:
            # execute_turn 读的是运行时对象上的快照字段：先写进去再取消。
            self._runtime.interrupt_reason = reason
            self._runtime.interrupt.set()
        task.cancel()
        return True

    async def run_maintenance(self, fn: Any, *, name: str = "maintenance") -> Any:
        """把一条维护操作插到"当前回合之后、下一个条目之前"执行并等结果。"""
        if self._closing:
            raise TurnError("agent 已 close，不能执行维护操作")
        if self._loop_task is None or self._loop_task.done():
            raise TurnError("agent 未 start()，不能执行维护操作")
        if self.in_turn_task():
            raise TurnError(f"不能在当前回合内部执行维护操作: {name}")
        future = self._slot.submit(fn)
        self.wake()
        return await future

    # ------------------------------------------------------------------
    # 常驻循环
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                self._publish_expired()
                request = self._slot.pop_maintenance()
                if request is not None:
                    await self._run_maintenance(request)
                    continue
                entry = self._inbox.pop_turn_entry()
                if entry is None:
                    entry = self._take_batch_turn()
                if entry is not None:
                    await self._execute_entry(entry)
                    continue
                await self._idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单个条目出错不许炸掉常驻循环
                log.error("worker 循环异常，继续常驻: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(0.05)

    async def _idle(self) -> None:
        """没有可跑的东西：等 push / 维护请求，或最近的 deadline / 合批到期。"""
        timeout = self._inbox.next_wakeup()
        if timeout is None:
            await self._wake.wait()
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), max(0.0, float(timeout)))

    def _publish_expired(self) -> None:
        """惰性过期扫描（不额外起定时器 task）：取条目前 / 被唤醒后各扫一遍。"""
        for entry in self._inbox.expire():
            self._hub.publish(ev_input(entry.id, "expired"))

    def _take_batch_turn(self) -> Entry | None:
        """合批窗口到期：把整批合成一个开回合条目（不 ``await``，在循环顶部裁决）。

        - 正文 = 各条 ``BATCHED`` 文本按队列序换行拼接（= 一条普通 user 消息）；
        - 各条 ``BATCHED`` 自带的 ``context`` 与搭车的 ``ENVIRONMENT`` 一起并入本回合；
        - ``origin`` 取第一条 ``BATCHED``；任一 ``BATCHED`` 带 ``EXCLUSIVE`` ⇒ 批回合也独占。

        没有可跑的开回合条目（``MAJOR`` / ``IMMEDIATE``）时才会走到这里，所以批次
        要么自己开回合，要么在忙碌期间排队 —— **不存在"攒了批却永远不说话"的档位**。
        """
        entries = self._inbox.take_due_batch()
        if not entries:
            return None
        batched = [entry for entry in entries if entry.bucket is Bucket.BATCHED]
        environment = [entry for entry in entries if entry.bucket is not Bucket.BATCHED]
        if not batched:  # 不变量：``batch_due()`` 为真 ⇒ 至少一条 BATCHED
            return None
        text = "\n".join(entry.text for entry in batched if entry.text)
        items = [item for entry in batched for item in entry.context]
        items.extend(self._items_from_entries(environment))
        if not text and not items:
            # 退化输入（既无正文也无 context）：不开回合，条目已消费，只留一条日志。
            log.debug("合批到期但无可投递内容，丢弃整批: %s", [entry.id for entry in entries])
            return None
        return Entry(
            id=batched[0].id,
            text=text,
            bucket=Bucket.BATCHED,
            origin=batched[0].origin,
            flags=(
                Flag.EXCLUSIVE
                if any(entry.flags & Flag.EXCLUSIVE for entry in batched)
                else Flag.NONE
            ),
            context=tuple(items),
            input_ids=tuple(entry.id for entry in entries),
        )

    # ------------------------------------------------------------------
    # 维护操作
    # ------------------------------------------------------------------

    async def _run_maintenance(self, request: MaintenanceRequest) -> None:
        try:
            result = await request.fn()
        except asyncio.CancelledError:
            if not request.future.done():
                request.future.set_exception(TurnError("agent 已 close"))
            raise
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方
            if not request.future.done():
                request.future.set_exception(exc)
        else:
            if not request.future.done():
                request.future.set_result(result)

    # ------------------------------------------------------------------
    # 回合
    # ------------------------------------------------------------------

    async def _execute_entry(self, entry: Entry) -> None:
        """跑一个回合条目；等回合任务结束（中断 / 抢占不杀 worker）。"""
        task = asyncio.create_task(self._run_turn(entry), name=f"turn:{entry.id}")
        self._turn_task = task
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            if self._closing:
                raise
        finally:
            if self._turn_task is task:
                self._turn_task = None

    async def _run_turn(self, entry: Entry) -> None:
        agent = self._agent
        index = next(self._seq)
        turn_id = f"{agent.spec.id}#{index}"
        turn = TurnInfo(index=index, id=turn_id, origin=entry.origin)
        self._turn_id = turn_id
        self._turn_info = turn
        started = time.monotonic()
        self._interrupt.clear()
        self._interrupt_reason = ""
        self._preempt.clear()
        self._slot.enter(exclusive=bool(entry.flags & Flag.EXCLUSIVE))
        scope = RealmTaskScope(log_name=f"turn.{turn_id}")
        emit = self._hub.publish
        delivered: list[ContextItem] = []
        runtime: TurnRuntime | None = None
        try:
            request, runtime, delivered = await self._prepare_turn(index, turn_id, entry, scope)
            self._runtime = runtime
            agent._emitter.bind(emit)
            pending_frames = self._id_frames(request.inputs, turn_id)
            pending_frames.extend(self._delivery_frames(delivered, turn_id))
            async for event in execute_turn(runtime, request):
                if event.type == "done":
                    # 终态帧是回合的最后一条：on_turn_end 必须在它**之前**跑完，
                    # 这样钩子里的 emit 才排在 done 前面（且永远不可能是终态帧）。
                    await self._finish_turn(turn, "done", None, started, event)
                    continue
                emit(event)
                if pending_frames and event.type == "start":
                    for frame in pending_frames:
                        emit(frame)
                    pending_frames = []
                    await self._fire_turn_hooks("on_turn_start", turn, forward=True)
        except TurnInterrupted as exc:
            await self._finish_turn(
                turn,
                "interrupted",
                None,
                started,
                ev_interrupted(turn_id, exc.reason or self._interrupt_reason or "interrupt"),
            )
        except TurnError as exc:
            text = _error_text(exc)
            await self._finish_turn(turn, "error", text, started, ev_error(turn_id, text))
        except asyncio.CancelledError:
            # 任务已被取消：不能再 await 钩子（会立刻再被取消），直接补终态帧。
            emit(ev_interrupted(turn_id, self._interrupt_reason or "interrupt"))
            raise
        except Exception as exc:  # noqa: BLE001 - 回合失败必须变成 error 终态帧，不许静默
            log.error("回合 %s 失败: %s: %s", turn_id, type(exc).__name__, exc)
            text = _error_text(exc)
            await self._finish_turn(turn, "error", text, started, ev_error(turn_id, text))
        finally:
            agent._emitter.unbind(emit)
            self._runtime = None
            self._turn_info = None
            with contextlib.suppress(BaseException):
                await scope.cancel_all(timeout=5.0)
            self._slot.leave()
            self._turn_id = None
            await self._sync_message_count()

    async def _finish_turn(
        self,
        turn: TurnInfo,
        kind: str,
        error: str | None,
        started: float,
        frame: Event,
    ) -> None:
        """终态帧之前跑 ``on_turn_end``（失败也不阻止终态帧）。"""
        try:
            await self._fire_turn_hooks("on_turn_end", turn, forward=False, kind=kind, error=error, started=started)
        except asyncio.CancelledError:
            # 钩子里调了 ctx.interrupt()：done 不能发，交给外层补 interrupted。
            raise
        except Exception as exc:  # noqa: BLE001 - 观测者出 bug 不许吃掉终态帧
            log.error("on_turn_end 派发失败（已忽略）: %s: %s", type(exc).__name__, exc)
        self._hub.publish(frame)

    async def _fire_turn_hooks(
        self,
        name: str,
        turn: TurnInfo,
        *,
        forward: bool,
        kind: str | None = None,
        error: str | None = None,
        started: float = 0.0,
    ) -> None:
        """按声明序（或逆序）调用覆盖了该钩子的 realm；单个失败只记日志。"""
        instances = self._agent.realms.hook_instances(name)
        if not instances:
            return
        if not forward:
            instances = list(reversed(instances))
        args: tuple[Any, ...]
        if name == "on_turn_start":
            args = (turn,)
        else:
            args = (
                turn,
                TurnOutcome(
                    kind=kind or "",
                    error=error,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                ),
            )
        for instance in instances:
            hook = getattr(instance, name, None)
            if hook is None:
                continue
            try:
                # 钩子里 ctx.emit 出来的帧要带上本回合 id（contextvar 只在回合任务内有效）。
                with current_turn(turn.id):
                    result = hook(*args)
                    if inspect.isawaitable(result):
                        await result
            except Exception as exc:  # noqa: BLE001 - 观测者不许拖垮回合
                log.error(
                    "realm %s 的 %s 失败（已忽略）: %s: %s",
                    getattr(instance, "id", "?"),
                    name,
                    type(exc).__name__,
                    exc,
                )

    async def _prepare_turn(
        self,
        index: int,
        turn_id: str,
        entry: Entry,
        scope: RealmTaskScope,
    ) -> tuple[TurnRequest, TurnRuntime, list[ContextItem]]:
        """组装本回合的请求与运行时；返回待发 ``input.delivered`` 的 context 项。"""
        agent = self._agent
        if agent.parent is None:
            # 子代理的 spec 由父固定，不做回合边界 realm 刷新（它的 realm 集合是空的，
            # 而且重建会把父的 spec 套到子代理身份上）。
            await agent._reconcile_realms()
        if self._closing:
            raise TurnError("agent 已 close，丢弃待执行回合")

        items: list[ContextItem] = list(entry.context)
        delivered: list[ContextItem] = []
        input_ids: list[str] = list(entry.input_ids) or [entry.id]
        for context_entry in self._inbox.pop_context_entries():
            input_ids.append(context_entry.id)
            built = self._items_from_entries([context_entry])
            items.extend(built)
            delivered.extend(built)
        # 通道里可能已有：inject() 的条目 + 忙碌 ``AUTO`` 注入的条目（回合开始即物化）
        pending = agent.context_channel.take_pending()
        items.extend(pending)
        delivered.extend(pending)
        # 第五拍（realm 周期上下文）：到期的才问；结果**排到下一个安全点**物化成
        # 独立的 <context> user 消息（形态②），不并入本回合的 user 消息。
        realm_items = await agent._context_for_turn(index, turn_id, entry.origin)
        if realm_items:
            agent.context_channel.queue(realm_items)

        request = TurnRequest(
            agent_id=agent.spec.id,
            turn_id=turn_id,
            text=entry.text,
            context=tuple(items),
            origin=entry.origin,
            inputs=tuple(input_ids),
        )
        runtime = TurnRuntime(
            spec=agent.spec,
            graph=agent.graph,
            checkpointer=agent.checkpoint.saver,
            model=agent._model,
            tools=agent._tools,
            middleware=agent._middleware,
            artifacts=agent.artifacts,
            vfs=agent.vfs,
            emit=self._hub.publish,
            interrupt=self._interrupt,
            tasks=scope,
            thread_id=agent.thread_id,
            api_format=agent.api_format,
            exclusive=bool(entry.flags & Flag.EXCLUSIVE),
            preempt=self._take_preempt_entry,
            preempt_event=self._preempt,
            interrupt_reason=self._interrupt_reason,
            extra={"context_channel": agent.context_channel},
        )
        return request, runtime, delivered

    # ------------------------------------------------------------------
    # 入站条目 → Context
    # ------------------------------------------------------------------

    @staticmethod
    def _items_from_entries(entries: Sequence[Entry]) -> list[ContextItem]:
        items: list[ContextItem] = []
        for entry in entries:
            if entry.text:
                items.append(
                    ContextItem(
                        source=entry.origin,
                        text=entry.text,
                        meta={"input_id": entry.id, "bucket": entry.bucket.value},
                    )
                )
            items.extend(entry.context)
        return items

    def _id_frames(self, input_ids: Sequence[str], turn_id: str | None) -> list[Any]:
        """给「本回合代表的入站 id」逐个生成 ``input.delivered``（已通知过的跳过）。

        批回合一次代表整批：``start.inputs`` 有几个 id，这里就补几条 ``delivered``，
        全部带本回合的 ``turn_id``。
        """
        frames: list[Any] = []
        for input_id in input_ids:
            if not input_id or input_id in self._delivered:
                continue
            self._delivered.add(input_id)
            frames.append(ev_input(input_id, "delivered", turn_id))
        return frames

    def _delivery_frames(self, items: Sequence[ContextItem], turn_id: str | None) -> list[Any]:
        """给带 ``input_id`` 且尚未通知过的条目生成 ``input.delivered`` 帧。"""
        frames: list[Any] = []
        for item in items:
            input_id = str(item.meta.get("input_id") or "")
            if not input_id or input_id in self._delivered:
                continue
            self._delivered.add(input_id)
            frames.append(ev_input(input_id, "delivered", turn_id))
        return frames

    def _publish_delivered(self, items: Sequence[ContextItem], turn_id: str | None) -> None:
        for frame in self._delivery_frames(items, turn_id):
            self._hub.publish(frame)

    def on_context_materialized(self, items: Sequence[ContextItem]) -> None:
        """安全点物化回调（``ContextMergeMiddleware`` 用）：补 ``input.delivered``。"""
        turn_id = self.current_turn_id
        self._publish_delivered(items, turn_id)

    def pull_env_context(self) -> list[ContextItem]:
        """安全点取用环境条目（``ContextMergeMiddleware`` 的 ``env_context``）。

        只在回合任务内部被同步调用，此刻 worker 正 ``await`` 本回合，是 Inbox 的
        唯一使用者，所以摘除条目是安全的。先扫一遍过期（``input.expired`` 照常发），
        再按 ``Inbox.env_interval`` 节流取用，避免把已经过期的快照当新鲜数据注入。
        """
        self._publish_expired()
        return self._items_from_entries(self._inbox.take_environment())

    def _take_preempt_entry(self) -> Entry | None:
        """``IMMEDIATE`` 抢占：取一条可接管的条目（过期条目顺手丢弃并发帧）。"""
        if self._closing:
            return None
        while True:
            entry = self._inbox.pop_immediate()
            if entry is None:
                return None
            if entry.deadline is not None and entry.deadline <= self._clock():
                self._hub.publish(ev_input(entry.id, "expired"))
                continue
            return entry

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def _sync_message_count(self) -> None:
        agent = self._agent
        graph = agent.graph
        if graph is None:
            return
        try:
            snapshot = await graph.aget_state(agent._thread_config())
            agent._message_count = len(
                (getattr(snapshot, "values", None) or {}).get("messages") or []
            )
        except Exception:  # noqa: BLE001 - 统计失败不影响回合结果
            pass


def _error_text(exc: BaseException) -> str:
    """``error`` 终态帧的 ``error`` 字段：``类名: 消息``（traceback 只进日志）。"""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


__all__ = ["TurnWorker"]
