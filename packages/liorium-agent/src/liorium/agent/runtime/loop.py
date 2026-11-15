"""一个回合的模型/工具循环：``execute_turn``。

计划依据：``__dev__/Liorium Event 事件流原生 定稿.md`` §5（回合调度）、§7（事件类型）、
§11（实现细节）。

``execute_turn`` 是**单消费者 async 生成器**，由 ``TurnWorker`` 唯一驱动：

- 迭代结束 = 回合结束；正常结束前发一条 ``done``（终态帧）；
- 中断以 ``TurnInterrupted`` 抛出、失败以 ``TurnError`` 抛出，**不发**终态帧 ——
  终态帧（``interrupted`` / ``error``）由 worker 补齐；
- ``IMMEDIATE`` 抢占（§5.3）：模型轮流式输出中收到抢占 ⇒ 取消本轮、补一条非终态
  ``round_aborted``，然后**同一回合**用新消息重新发起模型请求（``turn_id`` 不变）；
  工具执行中不打断（等整组工具跑完，下一个安全点再换消息）；
- ``EXCLUSIVE`` 回合拒绝被抢占（由 worker 传 ``runtime.exclusive`` 进来）。

回合开始（形态①，§5.1）：``request.context`` 并入该回合的 user 消息，并写进
``state.context`` 审计通道（原始 ContextItem + 物化消息 id）。运行中到达的
Context 由 ``ContextMergeMiddleware`` 在安全点物化成**独立** user 消息（形态②）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage

from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.log import get_logger
from liorium.core.types import (
    ContextItem,
    Event,
    ev_done,
    ev_input,
    ev_round_aborted,
    ev_start,
    render_context_block,
)

from ..context import ContextChannel, ContextRecord
from .state import current_turn
from .stream import EventTranslator, is_compact_event, is_model_call_start
from .types import TurnRequest, TurnRuntime

log = get_logger("liorium.agent.loop")


class _RoundAborted(Exception):
    """本轮被 ``IMMEDIATE`` 抢占：带着"接管这条回合的入站条目"跳出当前轮。"""

    def __init__(self, entry: Any) -> None:
        self.entry = entry
        super().__init__("round_aborted")


def _context_channel(runtime: TurnRuntime) -> ContextChannel | None:
    """取本 Agent 的 Context 通道（``LioriumAgent`` 放在 ``extra`` 里传进来）。"""
    channel = runtime.extra.get("context_channel")
    if isinstance(channel, ContextChannel):
        return channel
    return None


def _audit_records(
    channel: ContextChannel | None,
    items: list[ContextItem],
    *,
    message_id: str,
    turn_id: str,
    at: str,
) -> list[dict[str, Any]]:
    """把物化进 user 消息的 Context 登记成纯 JSON 审计记录。

    有 channel 时走 channel（历史记录集中在一处）；没有 channel 时（子代理的极简
    运行）就地构造同样的记录，保证 ``state.context`` 的形状不随部署方式变化。
    """
    if not items:
        return []
    if channel is not None:
        records = channel.record_materialized(
            items, message_id=message_id, turn_id=turn_id, at=at
        )
        return [record.to_dict() for record in records]
    return [
        ContextRecord(
            source=item.source,
            text=item.text,
            at=at,
            message_id=message_id,
            turn_id=turn_id,
            meta=dict(item.meta),
        ).to_dict()
        for item in items
    ]


def _user_content(text: str, items: list[ContextItem], at: str) -> str:
    """形态①/安全点：context 块并入 user 消息（``text`` 在前，context 逐条在后）。"""
    if not items:
        return text
    return f"{text}\n\n{render_context_block(items, at)}"


def _turn_config(runtime: TurnRuntime) -> dict[str, Any]:
    """每次调用现造 config（与既有实现一致：不缓存）。"""
    return {
        "configurable": {"thread_id": runtime.thread_id},
        "recursion_limit": runtime.spec.limits.recursion_limit,
    }


def _message_payload(
    runtime: TurnRuntime,
    text: str,
    items: list[ContextItem],
    *,
    turn_id: str,
) -> dict[str, Any]:
    """一条 user 消息 + 它的审计记录；首次请求与抢占后的续跑共用这一条路径。"""
    at = time.strftime("%H:%M")
    message_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "messages": [HumanMessage(content=_user_content(text, items, at), id=message_id)]
    }
    records = _audit_records(
        _context_channel(runtime),
        items,
        message_id=message_id,
        turn_id=turn_id,
        at=at,
    )
    if records:
        payload["context"] = records
    return payload


async def _close_stream(stream: Any) -> None:
    """提前关掉图的事件流，别把挂起的生成器留给 GC。"""
    if isinstance(stream, AsyncGenerator):
        with contextlib.suppress(BaseException):
            await stream.aclose()


def _error_detail(exc: BaseException) -> str:
    """异常的可读描述：**始终带类名**。

    有些异常（``NotImplementedError()``、``TimeoutError()`` 之类）的 ``str()`` 是
    空串，只写 ``f"{exc}"`` 会得到一条"回合执行失败: "的空消息，宿主日志里查不到
    任何线索。类名是最后一道可诊断性保险。
    """
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


async def _next_raw(
    iterator: AsyncIterator[Any],
    *,
    runtime: TurnRuntime,
    tool_depth: int,
) -> Any:
    """取下一条图事件；等待期间收到 ``IMMEDIATE`` 抢占信号就在安全点中止本轮。

    **为什么不用"每个事件后轮询"**：模型还没吐第一个 token 时（以及工具里 await
    时）下游没有任何事件可轮询，轮询式检查会让"现在就说"退化成"等模型下一个字"。
    这里把 ``stream.__anext__()`` 与抢占信号赛跑，信号一到立刻可裁决；工具执行中
    （``tool_depth > 0``）只清信号、不打断 —— 等整组工具跑完，下一个事件到达后再
    走"安全点"路径（§5.4）。
    """
    signal = runtime.preempt_event
    if signal is None or runtime.preempt is None or runtime.exclusive:
        return await iterator.__anext__()

    next_task = asyncio.ensure_future(iterator.__anext__())
    signal_task = asyncio.ensure_future(signal.wait())
    try:
        done, _pending = await asyncio.wait(
            {next_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if next_task in done:
            return next_task.result()
        # 抢占信号先到：安全点判定。
        # 工具执行中**不清信号**：条目仍在 Inbox 里，等这组工具跑完、下一个事件到达时
        # 会再次走到这里（此时 tool_depth 已归零）并在安全点接管。
        if tool_depth == 0:
            entry = runtime.preempt()
            if entry is not None:
                raise _RoundAborted(entry)
            signal.clear()
        return await next_task
    finally:
        signal_task.cancel()
        if not next_task.done():
            next_task.cancel()
            with contextlib.suppress(BaseException):
                await next_task


async def execute_turn(
    runtime: TurnRuntime, request: TurnRequest
) -> AsyncIterator[Event]:
    """驱动一个回合，按序产出 Agent 事件。

    事件顺序：``start`` → （``reasoning_delta`` / ``delta`` / ``tool_start`` /
    ``tool_result`` / ``round_aborted`` 按实际发生顺序）→ ``done``。

    异常语义：

    - ``runtime.interrupt`` 置位（每收到一个事件检查一次）→ ``TurnInterrupted``；
      不 ``yield`` 任何终态事件，终态由 worker 发；
    - 图/模型/工具抛异常 → ``TurnError``（原异常挂在 ``cause`` 上，绝不静默吞）；
    - 单回合模型调用次数超过 ``spec.limits.max_turns`` → ``TurnError``
      （防死循环的最后一层保护；LangGraph 的 ``recursion_limit`` 是另一层）；
    - 被 ``IMMEDIATE`` 抢占 ⇒ 补齐 ``round_aborted`` + ``input(delivered)`` 后在同一
      回合内换消息续跑，``turn_id`` 不变。
    """
    translator = EventTranslator()
    items = list(request.context)
    max_calls = runtime.spec.limits.max_turns
    model_calls = 0
    externalized = False
    """是否已经"说出去过"（正文 delta 或工具调用）；只用于日志与语义记录。"""

    yield ev_start(request.turn_id, origin=request.origin, inputs=request.inputs)

    payload = _message_payload(runtime, request.text, items, turn_id=request.turn_id)

    try:
        while True:
            stream = runtime.graph.astream_events(
                payload, config=_turn_config(runtime), version="v2"
            )
            iterator = stream.__aiter__()
            tool_depth = 0
            aborted: Any = None
            try:
                with current_turn(request.turn_id):
                    try:
                        while True:
                            try:
                                raw = await _next_raw(
                                    iterator, runtime=runtime, tool_depth=tool_depth
                                )
                            except StopAsyncIteration:
                                break
                            if runtime.interrupt.is_set():
                                raise TurnInterrupted(
                                    reason=runtime.interrupt_reason or "interrupt"
                                )
                            if is_model_call_start(raw) and not is_compact_event(raw):
                                model_calls += 1
                                if model_calls > max_calls:
                                    raise TurnError(
                                        f"单回合模型调用超过上限（{model_calls} > {max_calls}）"
                                    )
                            for event in translator.translate(raw):
                                if event.type == "tool_start":
                                    tool_depth += 1
                                    externalized = True
                                elif event.type == "tool_result":
                                    tool_depth = max(0, tool_depth - 1)
                                elif event.type == "delta" and event.get("content"):
                                    externalized = True
                                yield event
                    except _RoundAborted as exc:
                        aborted = exc.entry
            finally:
                await _close_stream(stream)

            if aborted is None:
                break

            log.info(
                "回合 %s 被 IMMEDIATE 抢占（externalized=%s），同一回合换消息续跑",
                request.turn_id,
                externalized,
            )
            yield ev_round_aborted(request.turn_id, "preempted")
            aborted_id = str(getattr(aborted, "id", "") or "")
            if aborted_id:
                yield ev_input(aborted_id, "delivered", request.turn_id)
            entry_items = list(getattr(aborted, "context", ()) or ())
            payload = _message_payload(
                runtime,
                str(getattr(aborted, "text", "")),
                entry_items,
                turn_id=request.turn_id,
            )
    except (TurnError, TurnInterrupted):
        # 收尾前把"只发过 start、没等到 end"的工具调用补齐（[已中断]）：
        # 被打断时 on_tool_end 永远不会来，前端会留一张永远转圈的工具卡。
        for event in translator.drain_unfinished():
            yield event
        raise
    except asyncio.CancelledError:
        # 取消路径不再 yield（消费者已经走了）；配对交给 repair 的占位。
        raise
    except Exception as exc:  # noqa: BLE001 - 一律保留原异常，不静默吞
        for event in translator.drain_unfinished():
            yield event
        raise TurnError(f"回合执行失败: {_error_detail(exc)}", cause=exc) from exc

    # 正常结束也可能留下未闭合的调用（例如工具节点提前收尾）：同样补齐
    for event in translator.drain_unfinished():
        yield event

    if runtime.interrupt.is_set():
        raise TurnInterrupted(reason=runtime.interrupt_reason or "interrupt")

    yield ev_done(request.turn_id)


__all__ = ["execute_turn"]
