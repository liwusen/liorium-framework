"""事件翻译器单测：``astream_events`` 事件 → Liorium 事件。

覆盖 ``docs/design/turn-loop.md`` §1.3（映射）、§1.4（四种格式的内容提取）、
§6（事件 type 总表）里与止血相关的断言：

- 正文 / 思考分流正确，空 delta 不发；
- 工具事件的 ``call_id`` 成对一致、重复事件不发；
- 压缩 tag 的事件一律忽略（压缩事件由中间件自己 emit）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from liorium.core.types import Event, ev_delta, ev_reasoning_delta, ev_tool_result, ev_tool_start
from liorium.agent.runtime.stream import (
    COMPACT_TAG,
    EventTranslator,
    extract_reasoning_text,
    is_ai_message_chunk,
    message_content_to_text,
    normalize_tool_args,
    tool_value_to_text,
)

RUN_ID = "01a1a621-0259-7021-a8b1-79454c72e887"


def _event(name: str, *, data: dict[str, Any] | None = None, tags: Any = None, run_id: str = RUN_ID, tool_name: str = "") -> dict[str, Any]:
    """构造一条 ``astream_events`` 事件 dict。"""
    event: dict[str, Any] = {
        "event": name,
        "data": dict(data or {}),
        "tags": list(tags or []),
        "run_id": run_id,
    }
    if tool_name:
        event["name"] = tool_name
    return event


def _chunk_event(chunk: Any, *, tags: Any = None) -> dict[str, Any]:
    return _event("on_chat_model_stream", data={"chunk": chunk}, tags=tags)


# ---------------------------------------------------------------------------
# 正文
# ---------------------------------------------------------------------------


def test_chat_model_stream_emits_delta() -> None:
    """AI 分片 → 一条 ``delta``。"""
    events = EventTranslator().translate(_chunk_event(AIMessageChunk(content="你好")))
    assert events == [Event("delta", {"content": "你好"})]


def test_empty_content_does_not_emit_delta() -> None:
    """空字符串不发 delta（末尾的空 chunk 很常见）。"""
    translator = EventTranslator()
    assert translator.translate(_chunk_event(AIMessageChunk(content=""))) == []
    assert message_content_to_text(None) == ""


def test_non_ai_chunk_is_ignored() -> None:
    """非 AI 分片（例如 Human 消息）不产生任何事件。"""
    translator = EventTranslator()
    assert translator.translate(_chunk_event(HumanMessage(content="hi"))) == []
    assert is_ai_message_chunk(HumanMessage(content="hi")) is False
    assert is_ai_message_chunk(AIMessageChunk(content="hi")) is True


def test_content_blocks_only_text_goes_to_delta() -> None:
    """content block 列表里只有 ``type == "text"`` 的块进正文。"""
    chunk = AIMessageChunk(
        content=[
            {"type": "text", "text": "答案"},
            {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
        ]
    )
    events = EventTranslator().translate(_chunk_event(chunk))
    assert events == [Event("delta", {"content": "答案"})]
    assert message_content_to_text([{"type": "tool_use", "id": "x"}]) == ""


# ---------------------------------------------------------------------------
# 思考（四种 API 格式）
# ---------------------------------------------------------------------------


def test_reasoning_from_additional_kwargs_openai_chat() -> None:
    """OpenAI Chat 兼容格式：思考在 ``additional_kwargs["reasoning_content"]``。"""
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先算一下"})
    events = EventTranslator().translate(_chunk_event(chunk))
    assert events == [Event("reasoning_delta", {"content": "先算一下"})]


def test_reasoning_then_delta_order() -> None:
    """同一个分片里既有思考又有正文时，先 reasoning_delta 再 delta。"""
    chunk = AIMessageChunk(content="5", additional_kwargs={"reasoning": "2+3"})
    events = EventTranslator().translate(_chunk_event(chunk))
    assert events == [
        Event("reasoning_delta", {"content": "2+3"}),
        Event("delta", {"content": "5"}),
    ]


def test_reasoning_from_anthropic_thinking_block() -> None:
    """Anthropic / Gemini 格式：``{"type": "thinking", "thinking": ...}``。"""
    chunk = AIMessageChunk(
        content=[
            {"type": "thinking", "thinking": "推理过程", "signature": "sig"},
            {"type": "text", "text": "结论"},
        ]
    )
    events = EventTranslator().translate(_chunk_event(chunk))
    assert events == [
        Event("reasoning_delta", {"content": "推理过程"}),
        Event("delta", {"content": "结论"}),
    ]


def test_reasoning_from_openai_responses_summary() -> None:
    """OpenAI Responses 格式：``{"type": "reasoning", "summary": [...]}``。"""
    chunk = AIMessageChunk(
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "想清楚"},
                    {"type": "summary_text", "text": "了"},
                ],
            },
            {"type": "text", "text": "答案"},
        ]
    )
    events = EventTranslator().translate(_chunk_event(chunk))
    assert events == [
        Event("reasoning_delta", {"content": "想清楚了"}),
        Event("delta", {"content": "答案"}),
    ]


def test_extract_reasoning_text_prefers_additional_kwargs() -> None:
    """``additional_kwargs`` 命中即返回，不再去 content 里找。"""
    chunk = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "块里的"}],
        additional_kwargs={"think": "kwargs 里的"},
    )
    assert extract_reasoning_text(chunk) == "kwargs 里的"
    assert extract_reasoning_text(AIMessageChunk(content="纯正文")) == ""


# ---------------------------------------------------------------------------
# 压缩分流
# ---------------------------------------------------------------------------


def test_compact_tagged_events_are_ignored() -> None:
    """带 ``COMPACT_TAG`` 的模型分片绝不产生正文 delta（摘要不进 TTS）。"""
    translator = EventTranslator()
    chunk = AIMessageChunk(content="这是摘要正文")
    assert translator.translate(_chunk_event(chunk, tags=[COMPACT_TAG])) == []
    assert translator.translate(_event("on_tool_start", tags=[COMPACT_TAG], tool_name="t")) == []
    assert COMPACT_TAG == "liorium_compact_summarizer"


def test_compact_metadata_also_splits_events() -> None:
    """``metadata["liorium_event"] == "compact"`` 同样算压缩调用（tags 丢信息时兜底）。"""
    translator = EventTranslator()
    event = _chunk_event(AIMessageChunk(content="摘要"))
    event["metadata"] = {"liorium_event": "compact"}
    assert translator.translate(event) == []


def test_unknown_events_are_ignored() -> None:
    """未列出的事件名（chain / llm 等）一律忽略。"""
    translator = EventTranslator()
    for name in ("on_chain_start", "on_chain_end", "on_llm_start", "on_llm_end", ""):
        assert translator.translate(_event(name)) == []
    assert translator.translate({}) == []
    assert translator.translate("不是 dict") == []


def test_model_end_fills_in_when_model_did_not_stream() -> None:
    """完全不流式的模型：``on_chat_model_end`` 兜底发一次 delta（否则正文消失）。"""
    translator = EventTranslator()
    end = _event("on_chat_model_end", data={"output": AIMessageChunk(content="非流式正文")})
    assert translator.translate(end) == [Event("delta", {"content": "非流式正文"})]
    # 同一 run 只发一次
    assert translator.translate(end) == []


def test_model_end_does_not_duplicate_streamed_content() -> None:
    """已经出过分片的 run：``on_chat_model_end`` 不再补发（绝不重复）。"""
    translator = EventTranslator()
    chunk_event = _chunk_event(AIMessageChunk(content="流式正文"))
    assert translator.translate(chunk_event) == [Event("delta", {"content": "流式正文"})]
    end = _event("on_chat_model_end", data={"output": AIMessageChunk(content="流式正文")})
    assert translator.translate(end) == []


# ---------------------------------------------------------------------------
# 工具事件
# ---------------------------------------------------------------------------


def test_tool_pair_has_same_call_id_and_is_idempotent() -> None:
    """同一对 start/end 的 ``call_id`` 一致；重复投递不重复产出。"""
    translator = EventTranslator()
    start = _event("on_tool_start", data={"input": {"a": 2, "b": 3}}, tool_name="add")
    end = _event(
        "on_tool_end",
        data={"output": ToolMessage(content="5", name="add", tool_call_id="call_abc")},
        tool_name="add",
    )

    started = translator.translate(start)
    assert started == [ev_tool_start("add", {"a": 2, "b": 3}, RUN_ID)]

    finished = translator.translate(end)
    assert finished == [ev_tool_result("add", "5", RUN_ID)]

    assert started[0].payload["call_id"] == finished[0].payload["call_id"] == RUN_ID
    # 幂等：重放同一对事件不再产出
    assert translator.translate(start) == []
    assert translator.translate(end) == []


def test_tool_args_normalization() -> None:
    """``input`` 四种形态的规范化。"""
    assert normalize_tool_args({"a": 1}) == {"a": 1}
    assert normalize_tool_args(None) == {}
    assert normalize_tool_args("") == {}
    assert normalize_tool_args('{"a": 1}') == {"a": 1}
    assert normalize_tool_args("[1, 2]") == {"input": [1, 2]}
    assert normalize_tool_args("不是 json") == {"input": "不是 json"}
    assert normalize_tool_args(42) == {"input": 42}


def test_tool_output_normalization() -> None:
    """``output`` 规范成 str 或 dict。"""
    assert tool_value_to_text(None) == ""
    assert tool_value_to_text("原样") == "原样"
    assert tool_value_to_text({"ok": True}) == {"ok": True}
    assert tool_value_to_text(ToolMessage(content="结果", tool_call_id="c1")) == "结果"
    assert tool_value_to_text([1, 2]) == "[\n  1,\n  2\n]"


def test_tool_name_falls_back_to_tool() -> None:
    """没有名字时兜底 ``"tool"``。"""
    events = EventTranslator().translate(_event("on_tool_start", data={"input": {}}))
    assert events[0].payload["tool_name"] == "tool"


def test_tool_event_without_run_id_pairs_by_tool_name() -> None:
    """极端情况：``run_id`` 缺失时，end 复用同名 start 的 call_id（仍成对一致）。"""
    translator = EventTranslator()
    start = _event("on_tool_start", data={"input": {}}, run_id="", tool_name="read")
    end = _event(
        "on_tool_end",
        data={"output": ToolMessage(content="ok", tool_call_id="call_1")},
        run_id="",
        tool_name="read",
    )
    started = translator.translate(start)
    finished = translator.translate(end)
    assert started[0].payload["call_id"] == finished[0].payload["call_id"]
    assert started[0].payload["call_id"] != ""


def test_translator_reset_clears_dedup_state() -> None:
    """``reset()`` 之后同一个 translator 可以复用于新回合。"""
    translator = EventTranslator()
    start = _event("on_tool_start", data={"input": {}}, tool_name="add")
    assert translator.translate(start) != []
    assert translator.translate(start) == []
    translator.reset()
    assert translator.translate(start) != []


def test_reasoning_helper_is_exported_for_reuse() -> None:
    """``ev_*`` 工厂与提取器都可直接复用（避免各处重复实现）。"""
    assert message_content_to_text("x") == "x"
    assert ev_delta("x").type == "delta"
    assert ev_reasoning_delta("x").type == "reasoning_delta"


def test_unfinished_tool_call_drains_as_interrupted() -> None:
    """2026-11-08：回合被打断时 ``on_tool_end`` 不会来，前端会留一张死卡片。"""
    translator = EventTranslator()
    assert translator.translate(_event("on_tool_start", tool_name="execute")) != []

    drained = translator.drain_unfinished()
    assert [event.type for event in drained] == ["tool_result"]
    assert drained[0].get("tool_name") == "execute"
    assert drained[0].get("output") == "[已中断]"
    assert drained[0].get("call_id") == RUN_ID

    # 幂等：补过的不再补
    assert translator.drain_unfinished() == []


def test_finished_tool_call_is_not_drained() -> None:
    translator = EventTranslator()
    translator.translate(_event("on_tool_start", tool_name="read"))
    translator.translate(_event("on_tool_end", tool_name="read", data={"output": "ok"}))
    assert translator.drain_unfinished() == []


def test_drain_is_cleared_by_reset() -> None:
    translator = EventTranslator()
    translator.translate(_event("on_tool_start", tool_name="read"))
    assert translator.drain_unfinished() != []
    translator.reset()
    assert translator.drain_unfinished() == []
