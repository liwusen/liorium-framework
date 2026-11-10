"""Context 物化的安全点：把"运行中到达"的 Context 变成独立 user 消息。

计划依据 §4.3 形态② / §D5 / §D7：``inject()`` **永不打断**，只排队到下一个
安全点；物化形态是一条**独立**的 user 消息（``<context source="..." at="HH:MM">``
块，格式冻结）。

为什么只在 ``before_model`` 追加（这是本中间件唯一的正确挂点）：

- 此刻上一轮的工具组**必然已经完整**（``AIMessage(tool_calls)`` 后面跟齐了它的
  全部 ``ToolMessage``），所以追加不会把一个工具组拆成两半；
- 反过来，模型流中途插入会改写"正在生成的 assistant 消息"的前缀，且让
  ``AIMessage(tool_calls)`` 与 ``ToolMessage`` 之间夹一条 Human 消息，
  上游 provider 立刻 400。

无 pending 时返回 ``None``（不产生任何状态写入，也不碰历史）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from liorium.core.types import render_context_block

from ...context import ContextChannel
from ..state import CURRENT_TURN_ID


class ContextMergeMiddleware(AgentMiddleware):
    """把 ``ContextChannel`` 里的待物化项在模型调用前追加成独立 user 消息。"""

    def __init__(self, channel: ContextChannel) -> None:
        super().__init__()
        self.channel = channel

    # -- 内部 -----------------------------------------------------------

    def _materialize(self, state: Any) -> dict[str, Any] | None:
        """取走 pending 并构造状态更新；无 pending 返回 ``None``。"""
        items = self.channel.take_pending()
        if not items:
            return None
        at = time.strftime("%H:%M")
        message = HumanMessage(
            content=render_context_block(items, at),
            id=uuid.uuid4().hex,
        )
        self.channel.record_materialized(
            items,
            message_id=message.id,
            turn_id=CURRENT_TURN_ID.get(),
            at=at,
        )
        messages = list(state.get("messages") or [])
        return {"messages": [*messages, message]}

    # -- 钩子 -----------------------------------------------------------

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        """同步路径（``graph.invoke``）的安全点。"""
        return self._materialize(state)

    async def abefore_model(
        self, state: Any, runtime: Any = None
    ) -> dict[str, Any] | None:
        """异步路径（``graph.astream_events``）的安全点。"""
        return self._materialize(state)


__all__ = ["ContextMergeMiddleware"]
