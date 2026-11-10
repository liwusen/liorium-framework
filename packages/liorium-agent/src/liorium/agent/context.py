"""Context 通道：入站队列 + 物化记录。

计划 §4.3：

- ``Context`` 是 Agent **入站流里的一等消息**，按到达时机物化成两种形态：
  ① 回合开始时到达 → 并入该回合的 user 消息；② 回合进行中到达 → 在安全点
  作为独立 user 消息插入（绝不拆散 ``AIMessage(tool_calls)`` + ``ToolMessage`` 组）。
- 原文另存独立 ``context`` channel（纯 JSON），供审计与前端。

本模块只管**队列与登记**；物化由 ``runtime/loop.py``（回合开始）与
``runtime/middleware/context_merge.py``（安全点）完成。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Sequence

from liorium.core.types import ContextItem


@dataclass
class ContextRecord:
    """一条已物化 Context 的审计记录（转成纯 JSON 存进 ``state.context``）。"""

    source: str
    text: str
    at: str
    message_id: str | None = None
    turn_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "text": self.text,
            "at": self.at,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "meta": dict(self.meta),
        }


class ContextChannel:
    """待物化队列 + 历史记录。

    线程安全由调用方保证（同一事件循环内单任务驱动）。
    """

    def __init__(self) -> None:
        self._pending: deque[ContextItem] = deque()
        self._records: list[ContextRecord] = []

    # -- 入站 -----------------------------------------------------------

    def queue(self, items: Sequence[ContextItem]) -> None:
        for item in items:
            self._pending.append(item)

    def has_pending(self) -> bool:
        return bool(self._pending)

    def pending_count(self) -> int:
        return len(self._pending)

    def take_pending(self) -> list[ContextItem]:
        """取走全部待物化项（调用方负责真正写进消息）。"""
        items = list(self._pending)
        self._pending.clear()
        return items

    def peek_pending(self) -> list[ContextItem]:
        return list(self._pending)

    # -- 登记 -----------------------------------------------------------

    def record_materialized(
        self,
        items: Sequence[ContextItem],
        *,
        message_id: str | None,
        turn_id: str | None,
        at: str | None = None,
    ) -> list[ContextRecord]:
        stamp = at or time.strftime("%H:%M")
        records = []
        for item in items:
            rec = ContextRecord(
                source=item.source,
                text=item.text,
                at=stamp,
                message_id=message_id,
                turn_id=turn_id,
                meta=dict(item.meta),
            )
            self._records.append(rec)
            records.append(rec)
        return records

    def history(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._records]

    def clear(self) -> None:
        self._pending.clear()
        self._records.clear()


__all__ = ["ContextChannel", "ContextRecord"]
