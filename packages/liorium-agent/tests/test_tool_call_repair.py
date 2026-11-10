"""Tool-call 修复：`vfs-output-mw.md` §9.2 的 15 条断言 + §6.5 边界。

被测对象是纯函数 ``repair_dangling_tool_calls`` 与
``ToolCallRepairMiddleware``（模型调用级、只改本次请求）。
"""

from __future__ import annotations

import asyncio

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from liorium.agent.runtime.middleware.tool_repair import (
    PLACEHOLDER_CONTENT,
    ToolCallRepairMiddleware,
)
from liorium.agent.runtime.repair import repair_dangling_tool_calls


def _ai(content: str = "", tool_calls: list[dict] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


def _ai_invalid(content: str = "", invalid: list[dict] | None = None) -> AIMessage:
    return AIMessage(content=content, invalid_tool_calls=invalid or [])


def _tool(tool_call_id: str, content: str = "ok") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def _tool_ids(messages: list) -> set[str]:
    return {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}


def _invalid(tool_call_id: str, name: str = "read_file") -> dict:
    return {
        "id": tool_call_id,
        "name": name,
        "args": "{not-json",
        "error": "Failed to parse tool call arguments as JSON",
    }


# ── §9.2：content block 清理 ────────────────────────────────────────────


def test_dropped_call_also_removes_content_block():
    """被打断的调用必须同时删掉 content 里的 tool_use/function_call 块。

    只清 `tool_calls` 时，Anthropic 的 tool_use / Responses 的 function_call
    仍会作为 content block 序列化上去，上游照样报
    "tool_use ids were found without tool_result blocks"。
    """
    ai = AIMessage(
        content=[
            {"type": "text", "text": "我来读一下"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "/tmp"}},
        ],
        tool_calls=[{"id": "t1", "name": "read", "args": {"path": "/tmp"}}],
    )

    repaired = repair_dangling_tool_calls(
        [HumanMessage(content="q"), ai, HumanMessage(content="停")]
    )

    repaired_ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert repaired_ai.tool_calls == []
    assert [block["type"] for block in repaired_ai.content] == ["text"]


def test_dropped_function_call_block_for_responses():
    ai = AIMessage(
        content=[
            {"type": "function_call", "id": "call_1", "name": "read", "arguments": "{}"},
        ],
        tool_calls=[{"id": "call_1", "name": "read", "args": {}}],
    )

    repaired = repair_dangling_tool_calls(
        [HumanMessage(content="q"), ai, HumanMessage(content="停")]
    )

    repaired_ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert repaired_ai.content == []


def test_server_tool_blocks_are_never_removed():
    ai = AIMessage(
        content=[
            {"type": "server_tool_use", "id": "srv1", "name": "web_search"},
            {"type": "web_search_call", "id": "srv1", "status": "completed"},
        ]
    )

    repaired = repair_dangling_tool_calls([HumanMessage(content="q"), ai])

    # 服务端工具没有 tool_call_id、也不需要客户端响应 → 原样保留且不注入占位
    assert repaired[1].content == ai.content
    assert len(repaired) == 2


# ── §9.2：完整序列 / 打断 / 悬空 / 孤立 ─────────────────────────────────


def test_complete_sequence_is_unchanged():
    messages = [
        HumanMessage(content="hi"),
        _ai("thinking", [{"id": "t1", "name": "read", "args": {}}]),
        _tool("t1", "file content"),
        _ai("done"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert repaired == messages


def test_interrupted_tool_call_is_voided_not_placeholder():
    """工具调用被新用户消息打断 → 作废（清空 tool_calls），不注入占位。"""
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
        HumanMessage(content="hello?"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    tool_ids = _tool_ids(repaired)
    assert "t1" not in tool_ids, "被打断的调用不应注入占位"
    assert not any(isinstance(message, ToolMessage) for message in repaired)
    # 原 AIMessage 的 tool_calls 被清空
    ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert ai.tool_calls == []


def test_partial_response_then_interrupt_voids_remaining():
    """t1 已响应，t2 未执行就遇到新消息 → t2 作废，t1 保持。"""
    messages = [
        HumanMessage(content="hi"),
        _ai(
            "",
            [
                {"id": "t1", "name": "read", "args": {}},
                {"id": "t2", "name": "search", "args": {}},
            ],
        ),
        _tool("t1"),
        HumanMessage(content="continue"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    tool_ids = _tool_ids(repaired)
    assert "t1" in tool_ids
    assert "t2" not in tool_ids, "未执行的 t2 应作废而不是注入占位"
    ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert [tool_call["id"] for tool_call in ai.tool_calls] == ["t1"]


def test_dangling_tool_calls_at_end_get_placeholder():
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert "t1" in _tool_ids(repaired)
    placeholder = next(message for message in repaired if isinstance(message, ToolMessage))
    assert placeholder.content == PLACEHOLDER_CONTENT
    assert placeholder.name == "read"


def test_orphan_tool_message_is_dropped():
    messages = [
        HumanMessage(content="hi"),
        _tool("ghost"),
        _ai("done"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert "ghost" not in _tool_ids(repaired)
    assert any(isinstance(message, AIMessage) for message in repaired)


def test_tool_message_with_unknown_id_after_pending_is_dropped():
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
        _tool("t2", "unexpected"),
        HumanMessage(content="continue"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    tool_ids = _tool_ids(repaired)
    assert "t2" not in tool_ids
    # t1 被后面的 HumanMessage 打断 → 作废，不再注入占位
    assert "t1" not in tool_ids


def test_original_list_not_mutated():
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
        HumanMessage(content="hello?"),
    ]
    snapshot = list(messages)
    repair_dangling_tool_calls(messages)
    assert messages == snapshot


# ── §9.2：invalid_tool_calls（参数解析失败）──────────────────────────────


def test_trailing_invalid_tool_call_gets_placeholder():
    """回归：只带 invalid_tool_calls 的悬空调用必须被响应，否则上游 400。"""
    messages = [
        HumanMessage(content="hi"),
        _ai_invalid("", [_invalid("bad1")]),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert "bad1" in _tool_ids(repaired)
    placeholder = next(message for message in repaired if isinstance(message, ToolMessage))
    assert placeholder.name == "read_file"


def test_interrupted_invalid_tool_call_is_voided():
    messages = [
        HumanMessage(content="hi"),
        _ai_invalid("", [_invalid("bad1")]),
        HumanMessage(content="继续"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert not any(isinstance(message, ToolMessage) for message in repaired)
    ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert ai.invalid_tool_calls == []


def test_valid_and_invalid_tool_calls_in_same_message():
    """同一条 AIMessage 同时带 tool_calls 与 invalid_tool_calls 时两条都要配平。"""
    message = AIMessage(
        content="",
        tool_calls=[{"id": "t1", "name": "read", "args": {}}],
        invalid_tool_calls=[_invalid("bad1")],
    )
    messages = [HumanMessage(content="hi"), message, _tool("t1")]
    repaired = repair_dangling_tool_calls(messages)
    assert _tool_ids(repaired) == {"t1", "bad1"}
    # 已响应的 t1 保持，未响应的 bad1 由占位补齐
    ai = next(message for message in repaired if isinstance(message, AIMessage))
    assert [tool_call["id"] for tool_call in ai.tool_calls] == ["t1"]
    assert [tool_call["id"] for tool_call in ai.invalid_tool_calls] == ["bad1"]


# ── §6.5 边界：空 id / 重复 id / 幂等 ───────────────────────────────────


def test_empty_tool_call_id_is_ignored():
    """空 tool_call_id：既不会被占位，也不会被清空。"""
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "", "name": "read", "args": {}}]),
    ]
    repaired = repair_dangling_tool_calls(messages)

    assert repaired == messages
    assert not any(isinstance(message, ToolMessage) for message in repaired)
    assert [tool_call["id"] for tool_call in repaired[1].tool_calls] == [""]


def test_duplicate_tool_call_id_gets_single_placeholder():
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
    ]
    repaired = repair_dangling_tool_calls(messages)

    placeholders = [message for message in repaired if isinstance(message, ToolMessage)]
    assert len(placeholders) == 1
    assert placeholders[0].tool_call_id == "t1"


def test_repair_is_idempotent():
    messages = [
        HumanMessage(content="hi"),
        _ai("", [{"id": "t1", "name": "read", "args": {}}]),
    ]

    once = repair_dangling_tool_calls(messages)
    twice = repair_dangling_tool_calls(once)

    assert twice == once


def test_repair_keeps_ai_message_reference_when_untouched():
    ai = _ai("done")
    repaired = repair_dangling_tool_calls([HumanMessage(content="hi"), ai])

    assert repaired[1] is ai


# ── §9.2：中间件入口 ─────────────────────────────────────────────────────


def test_wrap_model_call_forwards_repaired_messages():
    middleware = ToolCallRepairMiddleware()
    calls: list[list] = []

    def handler(request):
        calls.append(request.messages)
        return ModelResponse(result=[_ai("final")])

    request = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[
            HumanMessage(content="hi"),
            _ai("", [{"id": "t1", "name": "read", "args": {}}]),
            HumanMessage(content="hello?"),
        ],
    )
    middleware.wrap_model_call(request, handler)
    assert calls, "handler 必须被调用"
    # 被打断的 t1 被作废：转发给模型的消息里不再有该占位/调用
    assert "t1" not in _tool_ids(calls[0])
    ai = next(message for message in calls[0] if isinstance(message, AIMessage))
    assert ai.tool_calls == []
    # 只改本次请求，原 request 不受影响
    assert [tool_call["id"] for tool_call in request.messages[1].tool_calls] == ["t1"]


def test_awrap_model_call_forwards_repaired_messages():
    middleware = ToolCallRepairMiddleware()
    calls: list[list] = []

    async def handler(request):
        calls.append(request.messages)
        return ModelResponse(result=[_ai("final")])

    request = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[
            HumanMessage(content="hi"),
            _ai("", [{"id": "t1", "name": "read", "args": {}}]),
        ],
    )
    asyncio.run(middleware.awrap_model_call(request, handler))

    assert "t1" in _tool_ids(calls[0])


def test_wrap_model_call_with_empty_messages_is_passthrough():
    middleware = ToolCallRepairMiddleware()
    seen: list[ModelRequest] = []

    def handler(request):
        seen.append(request)
        return ModelResponse(result=[_ai("final")])

    request = ModelRequest(model=None, messages=[])  # type: ignore[arg-type]
    middleware.wrap_model_call(request, handler)

    assert seen[0] is request


def test_placeholder_injection_is_logged(caplog) -> None:
    """2026-11-08：结果凭空消失必须留痕 —— 注入占位前打 WARNING。"""
    import logging

    middleware = ToolCallRepairMiddleware()

    async def handler(request):
        return ModelResponse(result=[_ai("final")])

    request = ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[
            HumanMessage(content="hi"),
            _ai("", [{"id": "t1", "name": "execute", "args": {}}]),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="liorium.agent.tool_repair"):
        asyncio.run(middleware.awrap_model_call(request, handler))

    messages = [record.getMessage() for record in caplog.records]
    assert any("悬空 tool_call" in text for text in messages), messages
    assert any("t1" in text for text in messages), messages
