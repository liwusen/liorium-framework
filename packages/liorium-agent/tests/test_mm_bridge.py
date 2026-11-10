"""MultimodalBridgeMiddleware：`vfs-output-mw.md` §9.1 的 8 条断言 + 多图/块形状。

桥接只做三件事：从 ``ToolMessage`` 里认出 artifact 引用、从 ``OutputStore``
取回图片、追加一条带 ``image_url`` 块的 ``HumanMessage``。它**永不删除消息**，
且注入幂等（``_mm_bridge_source``）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage

from liorium.agent.runtime.middleware.mm_bridge import (
    GENERATED_FIELD,
    SOURCE_KEY_FIELD,
    MultimodalBridgeMiddleware,
)
from liorium.agent.runtime.output_store import OutputStore

# 1×1 PNG（无需 Pillow 即可构造合法图片字节）
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
TINY_PNG = base64.b64decode(TINY_PNG_B64)


def _ref_message(
    artifact_id: str, *, tool_call_id: str = "c1", message_id: str = "t1"
) -> ToolMessage:
    ref = json.dumps(
        {
            "kind": "multimodal_tool_result",
            "text": f"截图\n[图片: artifact://{artifact_id}]",
            "artifact": artifact_id,
        },
        ensure_ascii=False,
    )
    return ToolMessage(content=ref, tool_call_id=tool_call_id, id=message_id)


def _state(artifact_id: str) -> dict:
    return {"messages": [HumanMessage(content="看图", id="u1"), _ref_message(artifact_id)]}


def _image_urls(messages: list) -> list[str]:
    urls: list[str] = []
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        if not (message.additional_kwargs or {}).get(GENERATED_FIELD):
            continue
        content = message.content
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                urls.append(str(block["image_url"]["url"]))
    return urls


def _put_image(store: OutputStore, *, text: str = "截图", data: bytes = TINY_PNG) -> str:
    return store.put_multimodal(
        {
            "kind": "multimodal_tool_result",
            "text": text,
            "images": [{"data": data, "mime": "image/png"}],
        },
        tool_name="read",
    )


def _png_bytes(width: int, height: int) -> bytes:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (width, height), (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ── §9.1：从 artifact 引用还原 ───────────────────────────────────────────


def test_bridge_rehydrates_image_from_artifact_ref():
    """只有引用时，桥接从 OutputStore 取回图片，并按 max_pixels 缩放。"""
    image_module = pytest.importorskip("PIL.Image")
    store = OutputStore()
    artifact_id = _put_image(store, data=_png_bytes(1200, 1200))

    out = MultimodalBridgeMiddleware(store, max_pixels=640000).before_model(
        _state(artifact_id), None
    )
    urls = _image_urls(out["messages"])

    assert len(urls) == 1
    assert urls[0].startswith("data:image/png;base64,")
    with image_module.open(io.BytesIO(base64.b64decode(urls[0].split(",", 1)[1]))) as sent:
        assert sent.width * sent.height <= 640000
        assert sent.width < 1200


def test_bridge_skips_ref_without_artifact():
    """artifact 已被清理（新对话/重启）时不注入任何消息，上下文保持干净。"""
    store = OutputStore()

    assert MultimodalBridgeMiddleware(store).before_model(_state("gone_1"), None) is None


def test_bridge_never_removes_messages():
    """回归：桥接只新增消息，绝不删除源 ToolMessage（否则 tool_calls 悬空）。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    out = MultimodalBridgeMiddleware(store).before_model(_state(artifact_id), None)

    assert out is not None
    assert not any(isinstance(message, RemoveMessage) for message in out["messages"])
    assert all(isinstance(message, HumanMessage) for message in out["messages"])


def test_bridge_is_idempotent_across_restart():
    """进程重启（内存状态丢失）后不得重复注入同一张图。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    state = _state(artifact_id)

    first = MultimodalBridgeMiddleware(store).before_model(state, None)
    assert first is not None and len(first["messages"]) == 1

    merged = {**state, "messages": [*state["messages"], *first["messages"]]}
    assert MultimodalBridgeMiddleware(store).before_model(merged, None) is None


def test_bridge_injects_only_unbridged_sources():
    """已有桥接消息的源跳过，只注入缺失的那一条。"""
    store = OutputStore()
    first_id = _put_image(store, text="第一张")
    second_id = _put_image(store, text="第二张")

    state = _state(first_id)
    first = MultimodalBridgeMiddleware(store).before_model(state, None)
    assert first is not None

    state_with_second = {
        "messages": [
            *state["messages"],
            *first["messages"],
            _ref_message(second_id, tool_call_id="c2", message_id="t2"),
        ]
    }
    out = MultimodalBridgeMiddleware(store).before_model(state_with_second, None)

    assert out is not None
    assert len(out["messages"]) == 1
    assert _image_urls(out["messages"])


def test_bridge_scans_all_messages_without_scan_limit():
    """回归：早期图片（超出旧 MAX_SCAN 窗口）也必须被桥接。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    messages: list = [
        HumanMessage(content="看图", id="u1"),
        _ref_message(artifact_id, tool_call_id="c0", message_id="t0"),
    ]
    for index in range(1, 7):
        messages.append(
            ToolMessage(content="普通工具结果", tool_call_id=f"c{index}", id=f"t{index}")
        )

    out = MultimodalBridgeMiddleware(store).before_model({"messages": messages}, None)

    assert out is not None
    assert len(out["messages"]) == 1
    assert _image_urls(out["messages"])


# ── 多图 / 块形状 ────────────────────────────────────────────────────────


def test_bridge_injects_all_images_of_one_artifact():
    store = OutputStore()
    artifact_id = store.put_multimodal(
        {
            "kind": "multimodal_tool_result",
            "text": "两张图",
            "images": [
                {"data": TINY_PNG, "mime": "image/png"},
                {"data": TINY_PNG, "mime": "image/jpeg"},
            ],
        },
        tool_name="read",
    )

    out = MultimodalBridgeMiddleware(store).before_model(_state(artifact_id), None)
    urls = _image_urls(out["messages"])

    assert len(urls) == 2
    assert urls[0].startswith("data:image/png;base64,")
    assert urls[1].startswith("data:image/jpeg;base64,")


def test_bridge_block_shape_matches_spec():
    """逐字形状：text 块在前、``image_url`` 块在后、两个标记齐全。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    out = MultimodalBridgeMiddleware(store).before_model(_state(artifact_id), None)

    message = out["messages"][0]
    assert isinstance(message, HumanMessage)
    assert message.content[0] == {"type": "text", "text": "截图\n[图片: artifact://read_1]"}
    assert message.content[1]["type"] == "image_url"
    assert list(message.content[1]["image_url"]) == ["url"]
    assert message.additional_kwargs[GENERATED_FIELD] is True
    assert message.additional_kwargs[SOURCE_KEY_FIELD] == "t1"


def test_bridge_accepts_both_artifact_reference_spellings():
    """``artifact://<id>``（冻结文本）与 ``artifacts://<id>``（挂载点）等价。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    for spelling in (f"artifact://{artifact_id}", f"artifacts://{artifact_id}", artifact_id):
        message = ToolMessage(
            content=json.dumps(
                {
                    "kind": "multimodal_tool_result",
                    "text": "截图",
                    "artifact": spelling,
                },
                ensure_ascii=False,
            ),
            tool_call_id=f"c-{spelling}",
            id=f"t-{spelling}",
        )

        out = MultimodalBridgeMiddleware(store).before_model({"messages": [message]}, None)

        assert out is not None, spelling
        assert len(_image_urls(out["messages"])) == 1, spelling


def test_bridge_without_pillow_passes_url_through(monkeypatch):
    """Pillow 不可用时不得报错：超过阈值的图片原样透传。"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    store = OutputStore()
    artifact_id = _put_image(store)

    out = MultimodalBridgeMiddleware(store, max_pixels=1).before_model(
        _state(artifact_id), None
    )

    assert _image_urls(out["messages"]) == [f"data:image/png;base64,{TINY_PNG_B64}"]


def test_bridge_passes_through_http_urls_without_resizing():
    store = OutputStore()

    tool_message = ToolMessage(
        content=json.dumps(
            {
                "kind": "multimodal_tool_result",
                "text": "远程图",
                "images": [{"url": "https://example.com/a.png"}],
            }
        ),
        tool_call_id="c1",
        id="t1",
    )
    out = MultimodalBridgeMiddleware(store, max_pixels=1000).before_model(
        {"messages": [tool_message]}, None
    )

    assert _image_urls(out["messages"]) == ["https://example.com/a.png"]


def test_bridge_ignores_non_tool_messages():
    """其余类型即使内容像 payload 也不碰（只处理 ToolMessage）。"""
    store = OutputStore()
    artifact_id = _put_image(store)
    payload = json.dumps(
        {
            "kind": "multimodal_tool_result",
            "text": "伪装成工具结果",
            "artifact": artifact_id,
        }
    )
    state = {"messages": [HumanMessage(content=payload, id="u1")]}

    assert MultimodalBridgeMiddleware(store).before_model(state, None) is None


def test_bridge_handles_empty_state():
    store = OutputStore()

    assert MultimodalBridgeMiddleware(store).before_model({"messages": []}, None) is None
    assert MultimodalBridgeMiddleware(store).before_model({}, None) is None


def test_bridge_async_entry_delegates():
    store = OutputStore()
    artifact_id = _put_image(store)

    out = asyncio.run(
        MultimodalBridgeMiddleware(store).abefore_model(_state(artifact_id), None)
    )

    assert out is not None
    assert _image_urls(out["messages"])


# ── 跨格式兼容：image_url 块必须能被 Anthropic / Gemini 接受 ──────────────


def test_bridge_message_converts_to_anthropic_image_block():
    pytest.importorskip("langchain_anthropic")
    from langchain_anthropic import ChatAnthropic

    store = OutputStore()
    artifact_id = _put_image(store)
    messages = MultimodalBridgeMiddleware(store).before_model(_state(artifact_id), None)[
        "messages"
    ]

    model = ChatAnthropic(model="claude-x", api_key="k", max_tokens=64)
    payload = model._get_request_payload(messages)

    content = payload["messages"][0]["content"]
    image_blocks = [block for block in content if block.get("type") == "image"]
    assert image_blocks, content
    assert image_blocks[0]["source"]["type"] == "base64"
    assert image_blocks[0]["source"]["media_type"] == "image/png"


def test_bridge_message_converts_to_gemini_inline_data():
    pytest.importorskip("langchain_google_genai")
    import langchain_google_genai.chat_models as google_chat_models

    store = OutputStore()
    artifact_id = _put_image(store)
    messages = MultimodalBridgeMiddleware(store).before_model(_state(artifact_id), None)[
        "messages"
    ]

    _system, history = google_chat_models._parse_chat_history(messages, model="gemini-2.5-pro")
    parts = [
        part
        for content in history
        if content is not None
        for part in (content.parts or [])
    ]
    assert any(getattr(part, "inline_data", None) for part in parts), parts
