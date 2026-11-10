"""``TodoRealm`` —— 状态化待办能力包。

一个能力 = 四件套 + 第五拍：

- **工具**：``todo(action=add|update|remove)`` 写、``todo_list`` 读（两个工具，参数窄）；
- **状态**：``state_scope="agent"`` ⇒ ``<inbox>/realm_state/todo.json``，每次写操作立刻落盘；
- **prompt 片段**：告诉模型四个状态与"什么时候该改状态"；
- **挂载**：无（读走 ``todo_list``，不额外投影 `todo://`）；
- **周期上下文**：``context_every=50``、``context_first=True`` —— 每 50 个回合给一次机会，
  只有存在 ``pending`` / ``processing`` 项时才真正注入；清单空着**不消费周期**，
  所以"一有事情马上会被提醒"，而"一直没事就保持安静"。

框架写的保留键 ``__context_due__`` 也在同一个状态文件里（``RealmManager`` 管），
本 realm 只读写 ``items`` / ``next_id`` 两个键。
"""

from __future__ import annotations

from typing import Any

from liorium.core.types import ContextItem
from liorium.realm_base import BaseRealm, TurnInfo, realm_tool, utc_stamp

from .model import (
    ACTIVE_STATES,
    STATES,
    Todo,
    counts_line,
    open_items,
    parse_state,
    render_context,
    render_item,
    render_list,
    sort_items,
)


class TodoRealm(BaseRealm):
    """待办清单：两个工具 + 周期提醒。"""

    id = "todo"
    state_scope = "agent"

    # 第五拍：每 50 个 run() 给一次机会；第 1 回合先提醒一次（针对从上次启动遗留的待办）。
    # RealmSpec.params["context_every"] / ["context_first"] 可覆盖（框架认这两个键）。
    context_every = 50
    context_first = True

    def __init__(self, spec: Any = None) -> None:
        super().__init__(spec)
        self._items: list[Todo] = []
        self._next = 1

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        raw = await self.state.get("items", [])
        if not isinstance(raw, list):
            raise ValueError(f"todo 状态里的 items 不是列表: {type(raw).__name__}")
        self._items = [Todo.from_dict(item) for item in raw]
        stored = await self.state.get("next_id", 0)
        highest = max((_id_index(todo.id) for todo in self._items), default=0)
        self._next = max(int(stored or 0), highest + 1, 1)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @realm_tool(name="todo")
    async def todo(
        self,
        action: str,
        text: str = "",
        id: str = "",
        status: str = "",
    ) -> str:
        """增删改待办。成功返回改动后的一行，失败返回「错误：…」（不会中断回合）。

        Args:
            action: add 新建 / update 改正文或状态 / remove 删除。
            text: 待办内容；add 必填，update 时可用来改正文。
            id: 待办 id（如 t1）；update / remove 必填。
            status: 目标状态：pending / processing / blocked / finished（大小写都收）。
        """
        verb = str(action or "").strip().lower()
        if verb == "add":
            return await self._add(text, status)
        if verb == "update":
            return await self._update(id, text, status)
        if verb == "remove":
            return await self._remove(id)
        return f"错误：未知 action {action!r}（允许 add / update / remove）"

    @realm_tool(name="todo_list")
    def todo_list(self, include_finished: bool = False) -> str:
        """列出待办（默认只看未完成；按「在做 → 待做 → 卡住 → 已完成」排序）。

        Args:
            include_finished: 是否连已完成的也列出来。
        """
        return render_list(self._items, include_finished=include_finished)

    # ------------------------------------------------------------------
    # 宿主 / CLI 用
    # ------------------------------------------------------------------

    def todos(self, *, include_finished: bool = False) -> list[Todo]:
        """当前待办快照（排序稳定；默认不含已完成）。"""
        if include_finished:
            return sort_items(self._items)
        return open_items(self._items)

    def counts(self) -> dict[str, int]:
        return {state: sum(1 for todo in self._items if todo.state == state) for state in STATES}

    # ------------------------------------------------------------------
    # 四件套
    # ------------------------------------------------------------------

    def prompt_fragment(self) -> str:
        return (
            "待办（状态 pending / processing / blocked / finished）：\n"
            "- `todo_list` 查看；`todo(action=\"add|update|remove\", id, text, status)` 增删改。\n"
            "- 开始做某条时置 processing，卡在外部条件上置 blocked，做完置 finished —— "
            "别让状态停在 pending 骗自己。\n"
            "- 未完成的待办会**定期**出现在上下文的 `<context source=\"realm:todo\">` 里；"
            "那只是提醒，不是新任务，也不需要回复它。"
        )

    # ------------------------------------------------------------------
    # 第五拍：周期上下文
    # ------------------------------------------------------------------

    def context_items(self, turn: TurnInfo) -> ContextItem | None:
        """到期时被框架调用（``context_every=50``）。

        - 只有 ``pending`` / ``processing`` 存在才返回内容；全空 / 全完成 / 只剩 ``blocked``
          时返回 ``None`` ⇒ 框架**不消费周期**，下一回合继续问（一有事情立刻提醒）。
        - 纯内存读，不 await、不 IO：本方法在持有输出许可时执行。
        """
        if not any(todo.state in ACTIVE_STATES for todo in self._items):
            return None
        visible = open_items(self._items)
        return ContextItem(
            source="realm:todo",
            text=render_context(self._items),
            meta={"open": len(visible), "ids": [todo.id for todo in visible]},
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _add(self, text: str, status: str) -> str:
        body = str(text or "").strip()
        if not body:
            return "错误：add 需要非空 text"
        try:
            state = parse_state(status) if status else "pending"
        except ValueError as exc:
            return f"错误：{exc}"
        stamp = utc_stamp()
        todo = Todo(id=f"t{self._next}", text=body, state=state, created_at=stamp, updated_at=stamp)
        self._items.append(todo)
        self._next += 1
        await self._save()
        return f"已添加 {render_item(todo)}"

    async def _update(self, todo_id: str, text: str, status: str) -> str:
        todo = self._find(todo_id)
        if todo is None:
            return self._missing(todo_id, "update")
        body = str(text or "").strip()
        if not body and not status:
            return "错误：update 至少要给 text 或 status"
        if body:
            todo.text = body
        if status:
            try:
                todo.state = parse_state(status)
            except ValueError as exc:
                return f"错误：{exc}"
        todo.updated_at = utc_stamp()
        await self._save()
        return f"已更新 {render_item(todo)}"

    async def _remove(self, todo_id: str) -> str:
        todo = self._find(todo_id)
        if todo is None:
            return self._missing(todo_id, "remove")
        self._items.remove(todo)
        await self._save()
        return f"已删除 {todo.id}：{todo.text}"

    def _find(self, todo_id: str) -> Todo | None:
        needle = str(todo_id or "").strip().lower()
        for todo in self._items:
            if todo.id.lower() == needle:
                return todo
        return None

    @staticmethod
    def _missing(todo_id: str, verb: str) -> str:
        target = str(todo_id or "").strip()
        return f"错误：没有 id 为 {target!r} 的待办，{verb} 没做" if target else f"错误：{verb} 需要 id"

    async def _save(self) -> None:
        await self.save(items=[todo.to_dict() for todo in self._items], next_id=self._next)

    def summary_line(self) -> str:
        """给宿主 ``/status`` 的一行摘要。"""
        return counts_line(self._items)


def _id_index(todo_id: str) -> int:
    digits = "".join(ch for ch in str(todo_id) if ch.isdigit())
    return int(digits) if digits else 0


__all__ = ["TodoRealm"]
