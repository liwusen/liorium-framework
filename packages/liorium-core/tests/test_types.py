"""``liorium.core.types`` 的回归测试（计划 §4.5 内容联合 / §4.3 Context / §3.5 事件）。

三类契约：

1. ``VfsContent`` 是**封闭三元联合**：``Text`` / ``Image`` / ``Binary``，构造即校验；
2. ``Event`` 只接受 9 种 Agent 事件类型，``to_dict()`` 是前端消费的扁平形态；
3. ``render_context_block`` 的文本格式**逐字冻结**（改格式 = 全历史前缀失效）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from liorium.core.types import (
    AGENT_EVENT_TYPES,
    ALL_EVENT_TYPES,
    CONTEXT_CLOSE,
    CONTEXT_OPEN,
    Binary,
    ContextItem,
    Event,
    Image,
    Text,
    content_kind,
    content_size,
    content_to_text,
    context_tag,
    ev_compact_delta,
    ev_compact_done,
    ev_compact_start,
    ev_delta,
    ev_done,
    ev_reasoning_delta,
    ev_start,
    ev_tool_result,
    ev_tool_start,
    render_context_block,
)

# ---------------------------------------------------------------------------
# VfsContent：构造即校验
# ---------------------------------------------------------------------------


def test_text_accepts_str() -> None:
    assert Text("你好").text == "你好"
    assert Text("").text == ""


@pytest.mark.parametrize("bad", [b"bytes", 1, None, ["a"]])
def test_text_rejects_non_str(bad: object) -> None:
    with pytest.raises(TypeError):
        Text(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("cls", [Image, Binary])
def test_media_requires_non_empty_mime(cls: type) -> None:
    with pytest.raises(ValueError):
        cls(data=b"\x89PNG", mime="")


@pytest.mark.parametrize("cls", [Image, Binary])
def test_media_requires_bytes(cls: type) -> None:
    with pytest.raises(TypeError):
        cls(data="not-bytes", mime="image/png")  # type: ignore[arg-type]


@pytest.mark.parametrize("cls", [Image, Binary])
@pytest.mark.parametrize("data", [bytearray(b"abc"), memoryview(b"abc")])
def test_media_coerces_bytes_like_to_bytes(cls: type, data: object) -> None:
    """``bytearray`` / ``memoryview`` 归一成 ``bytes``（frozen dataclass 也要可哈希）。"""
    content = cls(data=data, mime="application/octet-stream")
    assert content.data == b"abc"
    assert type(content.data) is bytes


@pytest.mark.parametrize("cls", [Image, Binary])
def test_media_accepts_empty_data(cls: type) -> None:
    """空内容合法（0 字节文件不该在构造期报错）。"""
    assert cls(data=b"", mime="application/octet-stream").data == b""


def test_content_is_hashable_and_frozen() -> None:
    """三元联合成员是 frozen dataclass：可作为 dict 键，且字段不可改。"""
    text = Text("a")
    assert {text: 1}[Text("a")] == 1
    with pytest.raises(FrozenInstanceError):
        text.text = "b"  # type: ignore[misc]


def test_content_to_text_projects_only_text() -> None:
    """``search`` / ``find`` 靠它跳过非文本内容（计划 §4.5）。"""
    assert content_to_text(Text("hello")) == "hello"
    assert content_to_text(Image(data=b"x", mime="image/png")) is None
    assert content_to_text(Binary(data=b"x", mime="application/zip")) is None


def test_content_kind_labels() -> None:
    assert content_kind(Text("a")) == "text"
    assert content_kind(Image(data=b"x", mime="image/png")) == "image"
    assert content_kind(Binary(data=b"x", mime="application/zip")) == "binary"


def test_content_size() -> None:
    assert content_size(Text("中文")) == len("中文".encode("utf-8")) == 6
    assert content_size(Image(data=b"0123", mime="image/png")) == 4
    assert content_size(Binary(data=b"", mime="application/zip")) == 0


# ---------------------------------------------------------------------------
# Event：9 种类型 / 扁平 to_dict
# ---------------------------------------------------------------------------


def test_event_type_table_is_frozen() -> None:
    """9 种 Agent 事件；增删即破坏前端兼容（计划 §3.5）。

    2026-11-08 删掉宿主事件（``pending`` / ``streaming`` / ``interrupted`` /
    ``error``）：``run()`` 是唯一事件出口，等待不产出事件、中断与失败用异常。
    """
    assert len(AGENT_EVENT_TYPES) == 9
    assert len(ALL_EVENT_TYPES) == 9
    assert set(AGENT_EVENT_TYPES) == {
        "start",
        "delta",
        "reasoning_delta",
        "tool_start",
        "tool_result",
        "compact_start",
        "compact_delta",
        "compact_done",
        "done",
    }


@pytest.mark.parametrize("event_type", ALL_EVENT_TYPES)
def test_event_accepts_every_declared_type(event_type: str) -> None:
    assert Event(event_type).type == event_type


@pytest.mark.parametrize(
    "bad", ["unknown", "", "DELTA", "delta ", "interrupt", "tool_delta"]
)
def test_event_rejects_unknown_type(bad: str) -> None:
    with pytest.raises(ValueError):
        Event(bad)


def test_event_to_dict_is_flat() -> None:
    """``{"type": ..., **payload}`` —— 前端只认这一种形态。"""
    event = Event("tool_start", {"tool_name": "read", "args": {"path": "a"}, "call_id": "c1"})
    assert event.to_dict() == {
        "type": "tool_start",
        "tool_name": "read",
        "args": {"path": "a"},
        "call_id": "c1",
    }


def test_event_to_dict_without_payload() -> None:
    assert Event("compact_start").to_dict() == {"type": "compact_start"}


def test_event_payload_is_not_shared_between_instances() -> None:
    first = Event("done")
    second = Event("done")
    first.payload["turn_id"] = "t1"
    assert second.payload == {}


def test_event_get_and_getitem() -> None:
    event = ev_done("turn-1")
    assert event["turn_id"] == "turn-1"
    assert event.get("turn_id") == "turn-1"
    assert event.get("missing", "fallback") == "fallback"
    assert event.get("missing") is None
    with pytest.raises(KeyError):
        event["missing"]


def test_event_helpers_payloads() -> None:
    """事件构造器的字段名是前端契约，逐字断言。"""
    assert ev_start("t1").to_dict() == {"type": "start", "turn_id": "t1"}
    assert ev_delta("hi").to_dict() == {"type": "delta", "content": "hi"}
    assert ev_reasoning_delta("想").to_dict() == {"type": "reasoning_delta", "content": "想"}
    assert ev_tool_start("read", {"path": "a"}, "c1").to_dict() == {
        "type": "tool_start",
        "tool_name": "read",
        "args": {"path": "a"},
        "call_id": "c1",
    }
    assert ev_tool_result("read", "ok", "c1").to_dict() == {
        "type": "tool_result",
        "tool_name": "read",
        "output": "ok",
        "call_id": "c1",
    }
    assert ev_compact_start().to_dict() == {"type": "compact_start"}
    assert ev_compact_delta("摘要").to_dict() == {"type": "compact_delta", "content": "摘要"}
    assert ev_compact_done(True).to_dict() == {"type": "compact_done", "ok": True}
    assert ev_compact_done(False).to_dict() == {"type": "compact_done", "ok": False}
    assert ev_done("t1").to_dict() == {"type": "done", "turn_id": "t1"}


# ---------------------------------------------------------------------------
# ContextItem / render_context_block（格式冻结）
# ---------------------------------------------------------------------------


def test_context_item_defaults() -> None:
    item = ContextItem(source="memory:recall", text="结论")
    assert item.meta == {}
    assert item.interrupt is False


def test_context_item_meta_is_not_shared() -> None:
    first = ContextItem(source="a", text="x")
    second = ContextItem(source="b", text="y")
    first.meta["k"] = "v"
    assert second.meta == {}


@pytest.mark.parametrize("source", ["", None])
def test_context_item_rejects_empty_source(source: object) -> None:
    with pytest.raises(ValueError):
        ContextItem(source=source, text="x")  # type: ignore[arg-type]


@pytest.mark.parametrize("text", [b"bytes", 1, None])
def test_context_item_rejects_non_str_text(text: object) -> None:
    with pytest.raises(TypeError):
        ContextItem(source="memory:recall", text=text)  # type: ignore[arg-type]


def test_context_tag_is_verbatim() -> None:
    item = ContextItem(source="memory:recall", text="a")
    assert context_tag(item, "12:03") == '<context source="memory:recall" at="12:03">'
    assert CONTEXT_OPEN == "<context"
    assert CONTEXT_CLOSE == "</context>"


def test_render_single_item_is_verbatim() -> None:
    """逐字冻结：``<context source="..." at="12:03">...</context>``。"""
    items = [ContextItem(source="memory:recall", text="a")]
    assert render_context_block(items, "12:03") == '<context source="memory:recall" at="12:03">a</context>'


def test_render_multiple_items_joined_by_blank_line() -> None:
    items = [
        ContextItem(source="memory:recall", text="a"),
        ContextItem(
            source="subagent:research-1",
            text="已完成，结论见 agent://subagents/research-1/finalResult.md",
        ),
    ]
    assert render_context_block(items, "12:05") == (
        '<context source="memory:recall" at="12:05">a</context>\n\n'
        '<context source="subagent:research-1" at="12:05">'
        "已完成，结论见 agent://subagents/research-1/finalResult.md</context>"
    )


def test_render_empty_block_is_empty_string() -> None:
    assert render_context_block([], "12:03") == ""


def test_render_keeps_text_payload_untouched() -> None:
    """正文原样（多行、引号、标签都不转义）。"""
    body = 'line1\n<context source="fake">line2'
    assert render_context_block([ContextItem(source="x", text=body)], "00:00") == (
        f'<context source="x" at="00:00">{body}{CONTEXT_CLOSE}'
    )
