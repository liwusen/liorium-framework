"""回合状态：``LioriumState`` + "当前回合 id" 的上下文变量。

计划依据 §4.3：

- ``state.messages`` 只装 system / user / ai / tool（含物化后的 context 消息）；
- ``state.context`` 是**纯 JSON** 的 Context 审计通道：原始 ``ContextItem`` 的
  来源、正文、时刻，以及它物化到哪条消息（``message_id``）。它不进模型请求，
  只供审计与前端读取。

``context`` 用 ``operator.add`` 归并（追加语义）：回合开始的钩子与运行中的
``ContextMergeMiddleware`` 都只**追加**，不重写历史前缀，因此对前缀缓存安全。

本模块不做任何 I/O，也不 import 具体模型；它只声明状态形状。
"""

from __future__ import annotations

import operator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Iterator, NotRequired

from langchain.agents.middleware.types import AgentState


class LioriumState(AgentState[Any]):
    """``create_agent`` 的状态形状：``AgentState`` + ``context`` 审计通道。

    ``messages`` / ``jump_to`` / ``structured_response`` 全部继承自
    ``AgentState``（不做任何改动）；新增的只有 ``context``。

    - ``NotRequired``：回合输入可以不带它（宿主只发 user 消息时）；
    - ``list[dict]``：只放 JSON 可序列化内容（``ContextRecord.to_dict()``）；
    - ``operator.add``：任何写入方都只能追加，历史只增长。
    """

    context: NotRequired[Annotated[list[dict[str, Any]], operator.add]]


CURRENT_TURN_ID: ContextVar[str | None] = ContextVar(
    "liorium_current_turn_id", default=None
)
"""当前回合 id。

只用于**审计记录**：``runtime/loop.py`` 在驱动图之前设置它，
``ContextMergeMiddleware`` 在图内读它，好让运行中物化的 Context 也带上回合 id。

图内的节点可能跑在子任务里，ContextVar 会随上下文复制传播；万一某一层丢了
上下文，读到的就是 ``None``（审计记录的该字段留空），不影响消息物化本身。
"""


@contextmanager
def current_turn(turn_id: str) -> Iterator[None]:
    """在 ``with`` 块内声明"这是哪个回合"。"""
    token = CURRENT_TURN_ID.set(turn_id)
    try:
        yield
    finally:
        CURRENT_TURN_ID.reset(token)


__all__ = ["CURRENT_TURN_ID", "LioriumState", "current_turn"]
