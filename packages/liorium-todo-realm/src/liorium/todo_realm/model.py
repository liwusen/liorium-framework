"""Todo 的数据模型与渲染（纯函数：不碰 Agent / VFS / langchain）。

四个状态（JSON 里一律小写，工具入参大小写都收）：

``pending`` → 还没开始；``processing`` → 在做；``blocked`` → 卡住等外部条件；
``finished`` → 完成。只有 ``pending`` / ``processing`` 会触发周期注入（``context_items``），
``blocked`` 会跟着一起显示但单独存在时不算"有事要做"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

TodoState = Literal["pending", "processing", "blocked", "finished"]

STATES: tuple[TodoState, ...] = ("pending", "processing", "blocked", "finished")
"""全部合法状态（顺序即文档里的介绍顺序）。"""

ACTIVE_STATES: tuple[TodoState, ...] = ("pending", "processing")
"""会触发周期上下文的状态：清单里还有"要做/在做"的事。"""

_DISPLAY_ORDER = {"processing": 0, "pending": 1, "blocked": 2, "finished": 3}

CONTEXT_REMINDER = (
    "请按实际进度用 `todo` 工具更新状态：开始做 → processing；"
    "卡住 → blocked；做完 → finished。"
)
"""注入正文末尾的提醒句（固定的行为约束，别让模型只看不动）。"""


def parse_state(value: Any) -> TodoState:
    """把外部输入（大小写不敏感）归一成合法状态；非法值抛 ``ValueError``。"""
    text = str(value or "").strip().lower()
    if text not in STATES:
        raise ValueError(f"未知状态 {value!r}（允许 {'/'.join(STATES)}）")
    return text  # type: ignore[return-value]


def _id_index(todo_id: str) -> int:
    digits = "".join(ch for ch in str(todo_id) if ch.isdigit())
    return int(digits) if digits else 0


@dataclass(slots=True)
class Todo:
    """一条待办。``id`` 形如 ``t1``；删除后**不复用**。"""

    id: str
    text: str
    state: TodoState = "pending"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Todo":
        """从状态文件读回；缺字段或状态非法时**报错**（让损坏在 status 里可见）。"""
        todo_id = str(raw.get("id") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not todo_id or not text:
            raise ValueError(f"待办记录缺 id/text: {dict(raw)!r}")
        return cls(
            id=todo_id,
            text=text,
            state=parse_state(raw.get("state", "pending")),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


def sort_items(items: Sequence[Todo]) -> list[Todo]:
    """按「在做 → 待做 → 卡住 → 已完成」排，同状态按 id 数字升序。"""
    return sorted(items, key=lambda todo: (_DISPLAY_ORDER.get(todo.state, 9), _id_index(todo.id)))


def open_items(items: Sequence[Todo]) -> list[Todo]:
    """未完成（除 ``finished`` 之外，含 ``blocked``）。"""
    return sort_items([todo for todo in items if todo.state != "finished"])


def active_items(items: Sequence[Todo]) -> list[Todo]:
    """``pending`` / ``processing``：决定"这回合要不要注入"。"""
    return sort_items([todo for todo in items if todo.state in ACTIVE_STATES])


def render_item(todo: Todo) -> str:
    return f"{todo.id} [{todo.state}] {todo.text}"


def counts_line(items: Sequence[Todo]) -> str:
    counts = {state: sum(1 for todo in items if todo.state == state) for state in STATES}
    return (
        f"未完成 {sum(counts[s] for s in ACTIVE_STATES + ('blocked',))} 条"
        f"（pending {counts['pending']} / processing {counts['processing']}"
        f" / blocked {counts['blocked']}）"
        f"，已完成 {counts['finished']} 条"
    )


def render_list(items: Sequence[Todo], *, include_finished: bool = False) -> str:
    """工具 ``todo_list`` 的输出。"""
    visible = sort_items(items) if include_finished else open_items(items)
    if not visible:
        return "（没有待办）"
    lines = [f"- {render_item(todo)}" for todo in visible]
    lines.append(counts_line(items))
    return "\n".join(lines)


def render_context(items: Sequence[Todo]) -> str:
    """周期注入的正文：只列未完成项 + 一句"按状态更新"的提醒。"""
    visible = open_items(items)
    lines = [f"待办提醒：{len(visible)} 条未完成"]
    lines.extend(f"- [{todo.state}] {todo.id} {todo.text}" for todo in visible)
    lines.append("")
    lines.append(CONTEXT_REMINDER)
    return "\n".join(lines)


__all__ = [
    "ACTIVE_STATES",
    "CONTEXT_REMINDER",
    "STATES",
    "Todo",
    "TodoState",
    "active_items",
    "counts_line",
    "open_items",
    "parse_state",
    "render_context",
    "render_item",
    "render_list",
    "sort_items",
]
