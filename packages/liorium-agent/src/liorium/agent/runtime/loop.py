"""一个回合的模型/工具循环：``execute_turn``。

计划依据 §3.2 / §4.3 / §E1；逐条语义见 ``docs/design/turn-loop.md`` §1.1、§1.3、
§1.5、§3。

``execute_turn`` 是**单消费者 async 生成器**：

- 迭代结束 = 回合结束；正常结束前发一条 ``done``；
- 中断以 ``TurnInterrupted`` 抛出、失败以 ``TurnError`` 抛出，**不发**终结事件
  （2026-11-08 起没有宿主事件：调用方拿到的异常就是终态）。

只有 ``run()`` 的生成器会转发这里的事件；库不向任何别的消费者扇出。

回合开始（形态①，§4.3）：``request.context`` 并入该回合的 user 消息，并写进
``state.context`` 审计通道（原始 ContextItem + 物化消息 id）。运行中到达的
Context 由 ``ContextMergeMiddleware`` 在安全点物化成**独立** user 消息（形态②）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from langchain_core.messages import HumanMessage

from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.types import ContextItem, Event, ev_done, ev_start, render_context_block

from ..context import ContextChannel, ContextRecord
from .state import current_turn
from .stream import EventTranslator, is_compact_event, is_model_call_start
from .types import TurnRequest, TurnRuntime


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
    """把回合开始并入的 Context 登记成纯 JSON 审计记录。

    有 channel 时走 channel（历史记录集中在一处）；没有 channel 时（宿主没装配
    Context 通道，例如子代理的极简运行）就地构造同样的记录，保证
    ``state.context`` 的形状不随部署方式变化。
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
    """形态①：context 块并入 user 消息（``text`` 在前，context 逐条在后）。"""
    if not items:
        return text
    return f"{text}\n\n{render_context_block(items, at)}"


def _turn_config(runtime: TurnRuntime) -> dict[str, Any]:
    """每次调用现造 config（与既有实现一致：不缓存）。"""
    return {
        "configurable": {"thread_id": runtime.thread_id},
        "recursion_limit": runtime.spec.limits.recursion_limit,
    }


async def _close_stream(stream: Any) -> None:
    """提前关掉图的事件流，别把挂起的生成器留给 GC。"""
    if isinstance(stream, AsyncGenerator):
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


async def execute_turn(
    runtime: TurnRuntime, request: TurnRequest
) -> AsyncIterator[Event]:
    """驱动一个回合，按序产出 Agent 事件。

    事件顺序：``start`` → （``reasoning_delta`` / ``delta`` / ``tool_start`` /
    ``tool_result`` 按实际发生顺序）→ ``done``。

    异常语义：

    - ``runtime.interrupt`` 置位（每收到一个事件检查一次）→ ``TurnInterrupted``；
      不 ``yield`` 任何终结事件，终结就是异常本身；
    - 图/模型/工具抛异常 → ``TurnError``（原异常挂在 ``cause`` 上，绝不静默吞）；
    - 单回合模型调用次数超过 ``spec.limits.max_turns`` → ``TurnError``
      （防死循环的最后一层保护；LangGraph 的 ``recursion_limit`` 是另一层）。
    """
    translator = EventTranslator()
    at = time.strftime("%H:%M")
    items = list(request.context)
    message_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "messages": [
            HumanMessage(
                content=_user_content(request.text, items, at),
                id=message_id,
            )
        ]
    }
    records = _audit_records(
        _context_channel(runtime),
        items,
        message_id=message_id,
        turn_id=request.turn_id,
        at=at,
    )
    if records:
        payload["context"] = records

    max_calls = runtime.spec.limits.max_turns
    model_calls = 0

    yield ev_start(request.turn_id)

    stream = runtime.graph.astream_events(
        payload, config=_turn_config(runtime), version="v2"
    )
    with current_turn(request.turn_id):
        try:
            async for raw in stream:
                if runtime.interrupt.is_set():
                    raise TurnInterrupted(reason="interrupt")
                if is_model_call_start(raw) and not is_compact_event(raw):
                    model_calls += 1
                    if model_calls > max_calls:
                        raise TurnError(
                            f"单回合模型调用超过上限（{model_calls} > {max_calls}）"
                        )
                for event in translator.translate(raw):
                    yield event
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
            raise TurnError(f"回合执行失败: {_error_detail(exc)}", cause=exc) from exc
        finally:
            await _close_stream(stream)

    # 正常结束也可能留下未闭合的调用（例如工具节点提前收尾）：同样补齐
    for event in translator.drain_unfinished():
        yield event

    if runtime.interrupt.is_set():
        raise TurnInterrupted(reason="interrupt")

    yield ev_done(request.turn_id)


__all__ = ["execute_turn"]
