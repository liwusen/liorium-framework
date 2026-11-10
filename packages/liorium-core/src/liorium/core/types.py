"""Liorium 核心类型：VFS 内容 / Context / 事件。

计划依据：`docs/plans/2026-11-04-liorium-agent-core.md`
- §4.5 VFS 内容是封闭三元联合：``Text | Image | Binary``
- §4.3 Context 是入站流里的一等消息，格式一旦上线不可改
- §3.5 run() 事件协议（前端零改动）

本模块是叶子：只依赖标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# ---------------------------------------------------------------------------
# VFS 内容（封闭三元联合）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Text:
    """文本节点内容。

    ``read`` 对它走文本工具结果路径（可选行选择器 / ``:raw`` / 结构摘要）。
    """

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError(f"Text.text 必须是 str，得到 {type(self.text).__name__}")


@dataclass(frozen=True, slots=True)
class Image:
    """图片节点内容。

    ``read`` 对它走多模态路径：图片写入 OutputStore，文本里只留
    ``artifacts://<id>`` 引用，由 mm_bridge 在请求期还原成 image block。
    **绝不内联 base64**（一张截图 ≈ 250 万 token）。
    """

    data: bytes
    mime: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise TypeError("Image.data 必须是 bytes")
        if not self.mime:
            raise ValueError("Image.mime 不能为空")
        object.__setattr__(self, "data", bytes(self.data))


@dataclass(frozen=True, slots=True)
class Binary:
    """二进制节点内容（非图片）。

    ``read`` 只产出文本说明（类型 + 大小 + ``artifacts://`` 引用），不内联。
    """

    data: bytes
    mime: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise TypeError("Binary.data 必须是 bytes")
        if not self.mime:
            raise ValueError("Binary.mime 不能为空")
        object.__setattr__(self, "data", bytes(self.data))


VfsContent = Union[Text, Image, Binary]
"""VFS 节点内容的封闭联合。新增成员即为破坏性变更（计划 §4.5）。"""


def content_to_text(content: VfsContent) -> str | None:
    """把内容投影成可搜索文本；非 Text 返回 ``None``（search/find 跳过）。"""
    if isinstance(content, Text):
        return content.text
    return None


def content_size(content: VfsContent) -> int:
    """内容的字节体积（Text 按 UTF-8 编码计）。"""
    if isinstance(content, Text):
        return len(content.text.encode("utf-8"))
    return len(content.data)


def content_kind(content: VfsContent) -> Literal["text", "image", "binary"]:
    """内容类型的稳定标签（日志/审计用，勿改）。"""
    if isinstance(content, Text):
        return "text"
    if isinstance(content, Image):
        return "image"
    return "binary"


# ---------------------------------------------------------------------------
# Context（入站流里的一等消息）
# ---------------------------------------------------------------------------

CONTEXT_OPEN = "<context"
CONTEXT_CLOSE = "</context>"
"""物化消息的标签。格式一旦上线不可改（改格式 = 全历史前缀失效）。"""


@dataclass(frozen=True, slots=True)
class ContextItem:
    """一条注入上下文。

    - 只放结论 + 指针，过程留在 VFS（计划 §D6）
    - ``source`` 形如 ``memory:recall`` / ``subagent:research-1`` / ``trigger:desktop``
    - ``interrupt`` 预留给 v1.1：是否像用户插话那样叫停当前工作。
      v1 的 ``inject()`` 永不打断（计划 §D7），因此该字段当前只被记录、不生效。
    """

    source: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    interrupt: bool = False

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("ContextItem.source 不能为空")
        if not isinstance(self.text, str):
            raise TypeError("ContextItem.text 必须是 str")


def context_tag(item: ContextItem, at: str) -> str:
    """单条 Context 的物化标签（不含正文）。

    ``<context source="memory:recall" at="12:03">`` —— ``at`` 是宿主时钟的本地时间
    ``HH:MM``，由调用方传入，核心不做时区推断。
    """
    return f'{CONTEXT_OPEN} source="{item.source}" at="{at}">'


def render_context_block(items: list[ContextItem], at: str) -> str:
    """把若干 ContextItem 渲染成一条可并入 user 消息的文本块。

    多条之间用空行分隔。调用方负责决定它落在回合开头（并入 user 消息）
    还是安全点（独立 user 消息）。
    """
    parts = []
    for item in items:
        parts.append(f"{context_tag(item, at)}{item.text}{CONTEXT_CLOSE}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 事件（run() 协议）
# ---------------------------------------------------------------------------

AGENT_EVENT_TYPES = (
    "start",
    "delta",
    "reasoning_delta",
    "tool_start",
    "tool_result",
    "compact_start",
    "compact_delta",
    "compact_done",
    "done",
)
"""``run()`` 产出的事件类型（计划 §3.5 表第一段）。

2026-11-08 起不再有「宿主事件」：``run()`` 返回的生成器是唯一事件出口，
等待 / 中断 / 失败分别用「不产出事件」「``TurnInterrupted``」「``TurnError``」表达。
"""

ALL_EVENT_TYPES = AGENT_EVENT_TYPES


@dataclass(frozen=True, slots=True)
class Event:
    """一条事件。

    ``to_dict()`` 是前端消费的扁平形态：``{"type": ..., **payload}``。
    ``agent_id`` 由调用方自己拼（一次 ``run()`` 只有一个 Agent，库不打标）。
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in ALL_EVENT_TYPES:
            raise ValueError(f"未知事件类型: {self.type!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        out.update(self.payload)
        return out

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def ev_start(turn_id: str) -> Event:
    return Event("start", {"turn_id": turn_id})


def ev_delta(content: str) -> Event:
    return Event("delta", {"content": content})


def ev_reasoning_delta(content: str) -> Event:
    return Event("reasoning_delta", {"content": content})


def ev_tool_start(tool_name: str, args: Any, call_id: str) -> Event:
    return Event("tool_start", {"tool_name": tool_name, "args": args, "call_id": call_id})


def ev_tool_result(tool_name: str, output: str, call_id: str) -> Event:
    return Event("tool_result", {"tool_name": tool_name, "output": output, "call_id": call_id})


def ev_compact_start() -> Event:
    return Event("compact_start", {})


def ev_compact_delta(content: str) -> Event:
    return Event("compact_delta", {"content": content})


def ev_compact_done(ok: bool) -> Event:
    return Event("compact_done", {"ok": ok})


def ev_done(turn_id: str) -> Event:
    return Event("done", {"turn_id": turn_id})


__all__ = [
    "AGENT_EVENT_TYPES",
    "ALL_EVENT_TYPES",
    "Binary",
    "CONTEXT_CLOSE",
    "CONTEXT_OPEN",
    "ContextItem",
    "Event",
    "Image",
    "Text",
    "VfsContent",
    "content_kind",
    "content_size",
    "content_to_text",
    "context_tag",
    "ev_compact_delta",
    "ev_compact_done",
    "ev_compact_start",
    "ev_delta",
    "ev_done",
    "ev_reasoning_delta",
    "ev_start",
    "ev_tool_result",
    "ev_tool_start",
    "render_context_block",
]
