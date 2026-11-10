"""Checkpoint：打开 / 播种 System 消息 / 重置对话。

计划 §3.7：``checkpoint.db`` 表结构**冻结**（LangGraph ``AsyncSqliteSaver``），
必须能加载旧库继续对话。
计划 §3.9 / §F7：``start()`` 绝不调模型；``reset_dialog()`` 是纯状态写入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import RemoveMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from liorium.core.errors import CheckpointError
from liorium.core.log import get_logger

log = get_logger("liorium.checkpoint")


def thread_config(thread_id: str, *, recursion_limit: int = 100) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": int(recursion_limit),
    }


class CheckpointStore:
    """``AsyncSqliteSaver`` 的生命周期包装。

    ``AsyncSqliteSaver.from_conn_string`` 是 async context manager，连接必须显式
    打开/关闭；``close()`` 幂等。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.saver: AsyncSqliteSaver | None = None
        self._cm: Any = None

    async def open(self) -> AsyncSqliteSaver:
        if self.saver is not None:
            return self.saver
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._cm = AsyncSqliteSaver.from_conn_string(str(self.db_path))
            self.saver = await self._cm.__aenter__()
        except Exception as exc:  # noqa: BLE001 - 包成 CheckpointError，带路径
            raise CheckpointError(f"打开 checkpoint 失败: {self.db_path}: {exc}") from exc
        return self.saver

    async def close(self) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            finally:
                self._cm = None
                self.saver = None

    # ------------------------------------------------------------------
    # 消息读写
    # ------------------------------------------------------------------

    async def messages(self, graph: Any, config: dict[str, Any]) -> list[Any]:
        snapshot = await graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        return list(values.get("messages") or [])

    async def seed_system_prompt(
        self, graph: Any, config: dict[str, Any], prompt: str
    ) -> bool:
        """把 system prompt 写进 checkpoint 的 System 消息（不调模型）。

        幂等：system 已是最新且在最前面 ⇒ 返回 False。
        保留历史（非 System 消息）。
        """
        if not prompt.strip():
            log.warning("system prompt 为空，跳过植入")
            return False
        existing = await self.messages(graph, config)
        systems = [m for m in existing if isinstance(m, SystemMessage)]
        kept = [m for m in existing if not isinstance(m, SystemMessage)]
        if (
            existing
            and len(systems) == 1
            and existing[0] is systems[0]
            and systems[0].content == prompt
        ):
            return False
        await graph.aupdate_state(
            config,
            {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    SystemMessage(content=prompt),
                    *kept,
                ]
            },
            as_node="model",
        )
        log.info("已植入 system prompt（%d 字符，保留 %d 条历史）", len(prompt), len(kept))
        return True

    async def reset_dialog(
        self, graph: Any, config: dict[str, Any], prompt: str
    ) -> None:
        """清空对话 + 重写 system 段（纯状态写入，不调模型）。"""
        messages: list[Any] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
        if prompt.strip():
            messages.append(SystemMessage(content=prompt))
        await graph.aupdate_state(config, {"messages": messages}, as_node="model")
        log.info("已清空对话并重写 system 段（%d 字符）", len(prompt))

    async def delete_thread(self, thread_id: str) -> None:
        if self.saver is None:
            return
        await self.saver.adelete_thread(thread_id)


__all__ = ["CheckpointStore", "thread_config"]
