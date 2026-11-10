"""悬空 tool_call 修复（纯函数）。

规格来源：``docs/design/vfs-output-mw.md`` §6（逐字冻结文本在 §6.1）。

三条规则：

1. 一批 tool_calls 还没被响应就被新的 AI 消息 / Human 消息打断 ⇒ **作废**
   （从 ``AIMessage`` 里删掉这些 tool_calls，含 content 里的 ``tool_use`` 块）；
2. 序列**结尾**仍未响应 ⇒ **注入占位 ToolMessage**（模型必须看到每个调用都有结果）；
3. 孤立 ToolMessage（找不到发起调用的 AI）⇒ 丢弃。

纯函数：返回新列表，入参与元素不被修改。压缩与 mm_bridge 都依赖它，
所以放在独立模块，避免中间件互相导入。
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.messages import AIMessage, ToolMessage

PLACEHOLDER_CONTENT = "[工具调用结果缺失，已由 Liorium 自动补全]"
"""冻结文本（§6.1）。改它 = 全历史前缀失效。"""

_TOOL_CALL_BLOCK_TYPES = {"tool_use", "function_call", "tool_call"}


def _calls_of(message: Any) -> list[dict[str, Any]]:
    calls = list(getattr(message, "tool_calls", None) or [])
    calls.extend(getattr(message, "invalid_tool_calls", None) or [])
    return calls


def _without_tool_call(message: AIMessage, tool_call_id: str) -> AIMessage:
    """删掉一个 tool_call 的三重痕迹：``tool_calls`` / ``invalid_tool_calls`` / content 块。"""
    clone = message.model_copy(deep=True)
    clone.tool_calls = [c for c in (clone.tool_calls or []) if c.get("id") != tool_call_id]
    if getattr(clone, "invalid_tool_calls", None):
        clone.invalid_tool_calls = [
            c for c in clone.invalid_tool_calls if c.get("id") != tool_call_id
        ]
    if isinstance(clone.content, list):
        clone.content = [
            block
            for block in clone.content
            if not (
                isinstance(block, dict)
                and block.get("type") in _TOOL_CALL_BLOCK_TYPES
                and block.get("id") == tool_call_id
            )
        ]
    return clone


def repair_dangling_tool_calls(
    messages: Sequence[Any],
    *,
    placeholder: str = PLACEHOLDER_CONTENT,
) -> list[Any]:
    """修复悬空 tool_call。返回新列表；无问题时返回等长的浅拷贝列表。"""
    repaired: list[Any] = []
    pending: dict[str, tuple[str | None, int]] = {}
    injected = 0
    dropped = 0

    def drop_pending() -> None:
        nonlocal dropped
        for cid in list(pending):
            _name, ai_index = pending[cid]
            ai = repaired[ai_index]
            if isinstance(ai, AIMessage):
                still = any(c.get("id") == cid for c in _calls_of(ai))
                if still:
                    repaired[ai_index] = _without_tool_call(ai, cid)
                    dropped += 1
        pending.clear()

    def flush_pending() -> None:
        nonlocal injected
        for cid, (name, _ai_index) in pending.items():
            repaired.append(
                ToolMessage(
                    content=placeholder,
                    tool_call_id=cid,
                    name=name or None,
                )
            )
            injected += 1
        pending.clear()

    for message in messages:
        if isinstance(message, AIMessage):
            calls = _calls_of(message)
            if calls:
                if pending:
                    drop_pending()
                ai_index = len(repaired)
                repaired.append(message)
                for call in calls:
                    cid = call.get("id")
                    if cid:
                        pending[cid] = (call.get("name"), ai_index)
            else:
                if pending:
                    drop_pending()
                repaired.append(message)
        elif isinstance(message, ToolMessage):
            cid = getattr(message, "tool_call_id", None)
            if cid and cid in pending:
                pending.pop(cid)
                repaired.append(message)
            else:
                dropped += 1
        else:
            if pending:
                drop_pending()
            repaired.append(message)

    if pending:
        flush_pending()

    return repaired


def count_violations(messages: Sequence[Any]) -> list[str]:
    """审计用：返回违反 OpenAI tool_call 约束的描述列表（测试/诊断）。"""
    problems: list[str] = []
    pending: dict[str, str] = {}
    for idx, message in enumerate(messages):
        if isinstance(message, AIMessage):
            if pending:
                problems.append(f"msg[{idx}] 打断了未响应的调用: {sorted(pending)}")
                pending.clear()
            for call in _calls_of(message):
                cid = call.get("id")
                if cid:
                    pending[cid] = call.get("name") or ""
        elif isinstance(message, ToolMessage):
            cid = getattr(message, "tool_call_id", None)
            if cid in pending:
                pending.pop(cid)
            else:
                problems.append(f"msg[{idx}] 孤立 ToolMessage: {cid}")
        elif pending:
            problems.append(f"msg[{idx}] 非 tool 消息打断了未响应的调用: {sorted(pending)}")
            pending.clear()
    if pending:
        problems.append(f"序列结尾仍有未响应调用: {sorted(pending)}")
    return problems


__all__ = ["PLACEHOLDER_CONTENT", "count_violations", "repair_dangling_tool_calls"]
