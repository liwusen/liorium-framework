"""Context 物化的安全点：把"运行中到达"的 Context 变成独立 user 消息。

计划依据 §4.3 形态② / §D5 / §D7：``inject()`` **永不打断**，只排队到下一个
安全点；物化形态是一条**独立**的 user 消息（``<context source="..." at="HH:MM">``
块，格式冻结）。

两条来源在同一个安全点合流：

- ``ContextChannel`` 里 ``inject()`` / realm 周期上下文排进来的项；
- ``env_context`` 回调（worker 提供）：按 ``Inbox.env_interval`` 节流的
  ``ENVIRONMENT`` 条目，回合进行中每 N 秒至多取到一次。

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
from typing import Any, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from liorium.core.log import get_logger
from liorium.core.types import ContextItem, render_context_block

from ...context import ContextChannel
from ..state import CURRENT_TURN_ID

log = get_logger("liorium.agent.context_merge")

MaterializedHook = Callable[[Sequence[ContextItem]], None]


class ContextMergeMiddleware(AgentMiddleware):
    """把 ``ContextChannel`` 里的待物化项在模型调用前追加成独立 user 消息。

    ``on_materialized`` 是给 worker 的旁路通知：条目带着 ``meta["input_id"]`` 时，
    worker 用它补一条 ``input.delivered`` 生命周期帧（`push`/`inject` 的关联键）。

    ``env_context`` 是同步回调（worker 的 ``pull_env_context``）：按 ``env_interval``
    节流地取走 ``ENVIRONMENT`` 条目，取到的内容与 ``ContextChannel`` 的 pending
    合流成**一条**消息。为 ``None`` 时（没有 Inbox / 不需要环境注入）纯空转。
    """

    def __init__(
        self,
        channel: ContextChannel,
        on_materialized: MaterializedHook | None = None,
        env_context: Callable[[], Sequence[ContextItem]] | None = None,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.on_materialized = on_materialized
        self.env_context = env_context

    # -- 内部 -----------------------------------------------------------

    def _materialize(self, state: Any) -> dict[str, Any] | None:
        """取走 pending（含节流到期的环境条目）并构造状态更新；都空则 ``None``。"""
        items = self.channel.take_pending()
        if self.env_context is not None:
            try:
                items.extend(self.env_context())
            except Exception as exc:  # noqa: BLE001 - 环境取用失败不许拖垮模型调用
                log.error("环境 Context 取用失败: %s: %s", type(exc).__name__, exc)
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
        if self.on_materialized is not None:
            try:
                self.on_materialized(items)
            except Exception as exc:  # noqa: BLE001 - 通知失败不许影响模型调用
                log.error("Context 物化回调失败: %s: %s", type(exc).__name__, exc)
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
