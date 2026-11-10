"""事件翻译器：LangGraph ``astream_events`` 事件 → Liorium 事件。

计划依据与逐条语义见 ``docs/design/turn-loop.md`` §1.3（事件映射）、§1.4（四种
API 格式的内容提取）、§6（事件 type 总表）。这里只产出 **Agent 事件**
（``delta`` / ``reasoning_delta`` / ``tool_start`` / ``tool_result``）：

- ``start`` / ``done`` 由 ``runtime/loop.py`` 在回合边界发；
- ``compact_*`` 由压缩中间件自己 emit（它知道压缩调用的生命周期），
  本模块**忽略**一切带压缩 tag 的事件，摘要文本绝不进正文 ``delta``；
- 2026-11-08 起没有宿主事件：``run()`` 的生成器是唯一出口，中断/失败用异常。

硬规则（违反即前端错乱）：

1. **空 delta 不发**：``content`` 为空字符串时不上行，否则前端白渲染一帧；
2. **工具事件幂等**：同一 ``call_id`` 的 ``tool_start`` / ``tool_result`` 各只发一次
   （重复的 ``on_tool_end`` 会出现在重试 / 缓存命中路径上）；
3. **同一对 start/end 的 ``call_id`` 必须一致**：见 ``EventTranslator`` 的注释。
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from liorium.core.types import (
    Event,
    ev_delta,
    ev_reasoning_delta,
    ev_tool_result,
    ev_tool_start,
)

COMPACT_TAG = "liorium_compact_summarizer"
"""压缩调用的 tag（冻结字符串，见 ``docs/design/compact.md`` §3）。

压缩中间件以 ``config={"tags": [COMPACT_TAG]}`` 发起摘要调用；带该 tag 的事件
在本模块一律丢弃 —— 压缩事件由中间件自己产。
"""

REASONING_KWARG_KEYS = ("reasoning_content", "reasoning", "think")
"""思考文本在 ``additional_kwargs`` 里的落点（OpenAI Chat 兼容格式）。"""

REASONING_BLOCK_TYPES = frozenset({"reasoning", "thinking", "reasoning_content", "thought"})
"""思考块在 ``content`` 列表里的 ``type``（OpenAI Responses / Anthropic / Gemini）。"""


# ---------------------------------------------------------------------------
# 事件外观
# ---------------------------------------------------------------------------


def event_name(event: Any) -> str:
    """事件名（``on_chat_model_stream`` 等），非 dict 或缺失时为空串。"""
    if not isinstance(event, dict):
        return ""
    return str(event.get("event") or "").strip().lower()


def event_tags(event: Any) -> tuple[str, ...]:
    """事件 tag 元组（压缩分流与非压缩分流的依据）。"""
    if not isinstance(event, dict):
        return ()
    tags = event.get("tags") or ()
    return tuple(str(tag) for tag in tags)


def is_compact_event(event: Any) -> bool:
    """该事件是否属于压缩调用。

    判据有两份，任命中即算压缩（两份都是 ``docs/design/compact.md`` §4 冻结的
    发起约定）：

    - ``tags`` 含 ``COMPACT_TAG``（主判据，实测会传播到子 run 的模型事件上）；
    - ``metadata["liorium_event"] == "compact"``（同一 config 里一起打的标记，
      防止某个 provider 在 tags 上丢信息）。
    """
    if COMPACT_TAG in event_tags(event):
        return True
    if not isinstance(event, dict):
        return False
    metadata = event.get("metadata") or {}
    return str(metadata.get("liorium_event") or "") == "compact"


def is_model_call_start(event: Any) -> bool:
    """是否一次模型调用的开始（单回合调用次数上限的计量点）。"""
    return event_name(event) == "on_chat_model_start"


def is_ai_message_chunk(chunk: Any) -> bool:
    """是否是 AI 分片。

    ``AIMessageChunk`` 的 ``type`` 是 ``"ai"``；这里同时容忍类名含 ``aimessage``
    的实现（部分 provider 的分片类型不走标准 ``type``）。
    """
    if not isinstance(chunk, BaseMessage):
        return False
    if str(chunk.type or "").strip().lower() == "ai":
        return True
    return "aimessage" in type(chunk).__name__.lower()


# ---------------------------------------------------------------------------
# 内容提取（§1.4 的四种 API 格式）
# ---------------------------------------------------------------------------


def message_content_to_text(content: Any) -> str:
    """把消息 ``content`` 投影成正文文本。

    - ``None`` → ``""``
    - ``str`` → 原样
    - ``list`` → 只拼 dict 且 ``type == "text"`` 的块的 ``text``（str 项直接拼）；
      其余块（``image`` / ``thinking`` / ``tool_use`` …）不进正文
    - 其它 → ``str(content)``
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").strip().lower() != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return str(content)


def _reasoning_block_text(block: dict[str, Any]) -> str:
    """单个思考块的文本：先 ``reasoning`` / ``thinking`` / ``text``，再看 ``summary``。"""
    for key in ("reasoning", "thinking", "text"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value
    summary = block.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, (list, tuple)):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def extract_reasoning_text(message: BaseMessage) -> str:
    """提取思考文本，落点按四种 API 格式分流。

    ① ``additional_kwargs`` 依次取 ``reasoning_content`` / ``reasoning`` /
    ``think``（非空 str 命中即返回）—— OpenAI Chat 兼容（DeepSeek R1 / Qwen）；
    ② ``content`` 为 list 时，拼接 ``type`` ∈ ``REASONING_BLOCK_TYPES`` 的块
    （块内取 ``reasoning`` / ``thinking`` / ``text``，否则取 ``summary``）——
    OpenAI Responses / Anthropic / Gemini；都不命中返回 ``""``。
    """
    additional = message.additional_kwargs or {}
    for key in REASONING_KWARG_KEYS:
        value = additional.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = message.content
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").strip().lower() not in REASONING_BLOCK_TYPES:
            continue
        text = _reasoning_block_text(block)
        if text:
            parts.append(text)
    return "".join(parts)


def normalize_tool_args(value: Any) -> dict[str, Any]:
    """把 ``on_tool_start`` 的 ``input`` 规范成 dict（前端按 dict 渲染参数）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return {"input": value}
        if isinstance(decoded, dict):
            return decoded
        return {"input": decoded}
    if value is None:
        return {}
    return {"input": value}


def tool_value_to_text(value: Any) -> str | dict[str, Any]:
    """把 ``on_tool_end`` 的 ``output`` 规范成 ``str`` 或 ``dict``。

    LangGraph 的工具节点把输出包成 ``ToolMessage``（``data["output"]`` 实测就是
    它），所以这里先取消息正文；dict 原样交给外层只序列化一次；其余落 ``json``。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, BaseMessage):
        return message_content_to_text(value.content)
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# 翻译器
# ---------------------------------------------------------------------------


def _content_events(message: BaseMessage) -> list[Event]:
    """一条 AI 消息 → ``reasoning_delta`` / ``delta``（空内容不发）。"""
    events: list[Event] = []
    reasoning = extract_reasoning_text(message)
    if reasoning:
        events.append(ev_reasoning_delta(reasoning))
    text = message_content_to_text(message.content)
    if text:
        events.append(ev_delta(text))
    return events


def _tool_name(event: dict[str, Any], data: dict[str, Any]) -> str:
    """工具名：先看事件名，再看 data，最后兜底 ``"tool"``。"""
    name = event.get("name") or data.get("name") or "tool"
    return str(name).strip() or "tool"


def _tool_call_id_from_output(value: Any) -> str:
    """工具结束时消息里带的模型侧 ``tool_call_id``（可能为空）。"""
    if isinstance(value, ToolMessage):
        return str(value.tool_call_id or "").strip()
    return ""


INTERRUPTED_RESULT = "[已中断]"
"""回合收尾时给"只发过 start、没等到 end"的工具调用补的结果文案（2026-11-08）。

工具被取消 / 回合被打断时 ``on_tool_end`` 永远不会来：前端会留一张永远转圈的
工具卡，模型侧则要靠悬空修复补占位（结果内容是空的）。这里让事件流至少成对。
"""


class EventTranslator:
    """有状态的事件翻译器（一个回合一个实例）。

    ``call_id`` 的取法（与既有实现一致，见 turn-loop §1.3）：
    **用 LangGraph 的 ``run_id``**，不是模型的 ``tool_call_id``。

    原因：``on_tool_start`` 的 ``data`` 里只有 ``input``，拿不到 ``tool_call_id``；
    而 ``on_tool_end`` 的 ``data["output"]`` 是 ``ToolMessage``，它才带
    ``tool_call_id``。若 start 用 ``run_id``、end 用 ``tool_call_id``，前端就永远
    配不上对。``run_id`` 在 start / end 上天然相同，因此成对一致。

    兜底顺序（仅在 ``run_id`` 缺失时才会走到，正常路径不会触发）：
    同工具名最早未配对的那次 start → ``output.tool_call_id`` → 合成 id。
    """

    def __init__(self) -> None:
        self._started: set[str] = set()
        self._finished: set[str] = set()
        self._unmatched: dict[str, deque[str]] = {}
        self._names: dict[str, str] = {}
        self._synthetic = 0
        self._streamed: set[str] = set()
        self._ended: set[str] = set()

    # -- 对外 -----------------------------------------------------------

    def translate(self, event: Any) -> list[Event]:
        """一条 ``astream_events`` 事件 → 零到多条 Liorium 事件。"""
        name = event_name(event)
        if not name:
            return []
        if is_compact_event(event):
            return []
        if name == "on_chat_model_stream":
            return self._translate_chunk(event)
        if name == "on_chat_model_end":
            return self._translate_model_end(event)
        if name == "on_tool_start":
            return self._translate_tool_start(event)
        if name == "on_tool_end":
            return self._translate_tool_end(event)
        return []

    def reset(self) -> None:
        """清空去重与配对状态（同一个 translator 复用于新回合时调用）。"""
        self._started.clear()
        self._finished.clear()
        self._unmatched.clear()
        self._names.clear()
        self._synthetic = 0
        self._streamed.clear()
        self._ended.clear()

    def drain_unfinished(self) -> list[Event]:
        """给"只发过 ``tool_start``、没有对应 ``tool_result``"的调用补结果。

        在回合收尾（正常结束 / 被打断 / 出错）时调用一次：已经 ``start`` 过的
        ``call_id`` 若没进过 ``_finished``，就补一条 ``[已中断]``。补完的 id 记进
        ``_finished``，所以调用两次也只补一次。
        """
        events: list[Event] = []
        for call_id in [cid for cid in self._started if cid not in self._finished]:
            self._finished.add(call_id)
            events.append(
                ev_tool_result(self._names.get(call_id, "tool"), INTERRUPTED_RESULT, call_id)
            )
        self._unmatched.clear()
        return events

    # -- 内容 -----------------------------------------------------------

    def _translate_chunk(self, event: dict[str, Any]) -> list[Event]:
        """``on_chat_model_stream``：先思考、后正文，空内容不发。"""
        data = event.get("data") or {}
        chunk = data.get("chunk")
        if not is_ai_message_chunk(chunk):
            return []
        self._streamed.add(self._run_id(event) or "*")
        return _content_events(chunk)

    def _translate_model_end(self, event: dict[str, Any]) -> list[Event]:
        """``on_chat_model_end``：**只**给"完全没流式分片"的调用补一次正文。

        LangChain 的模型节点走 ``ainvoke``，是否流式由 ``astream_events`` 挂上的
        callback 隐式决定（``_should_stream``）。实现了 ``_stream`` / ``_astream``
        的 provider 会流式，于是全程由 ``on_chat_model_stream`` 产出 delta；
        但**不实现流式**的模型一条分片都没有，若完全忽略本事件，模型正文就会
        静默消失（只剩 ``start`` + ``done``）。

        因此这里的兜底是"无分片才发"：出过分片的 run 一律跳过，绝不重复。
        """
        run_key = self._run_id(event) or "*"
        if run_key in self._streamed or run_key in self._ended:
            return []
        self._ended.add(run_key)
        data = event.get("data") or {}
        output = data.get("output")
        if not is_ai_message_chunk(output):
            return []
        return _content_events(output)

    # -- 工具 -----------------------------------------------------------

    def _translate_tool_start(self, event: dict[str, Any]) -> list[Event]:
        data = event.get("data") or {}
        tool_name = _tool_name(event, data)
        call_id = self._start_call_id(event, data, tool_name)
        if call_id in self._started:
            return []
        self._started.add(call_id)
        self._names[call_id] = tool_name
        self._unmatched.setdefault(tool_name, deque()).append(call_id)
        return [ev_tool_start(tool_name, normalize_tool_args(data.get("input")), call_id)]

    def _translate_tool_end(self, event: dict[str, Any]) -> list[Event]:
        data = event.get("data") or {}
        tool_name = _tool_name(event, data)
        call_id = self._finish_call_id(event, data, tool_name)
        if call_id in self._finished:
            return []
        self._finished.add(call_id)
        self._forget_unmatched(tool_name, call_id)
        output = tool_value_to_text(data.get("output"))
        return [ev_tool_result(tool_name, output, call_id)]

    # -- call_id --------------------------------------------------------

    def _run_id(self, event: dict[str, Any]) -> str:
        return str(event.get("run_id") or "").strip()

    def _start_call_id(
        self, event: dict[str, Any], data: dict[str, Any], tool_name: str
    ) -> str:
        run_id = self._run_id(event)
        if run_id:
            return run_id
        tool_call_id = _tool_call_id_from_output(data.get("output"))
        if tool_call_id:
            return tool_call_id
        return self._synthetic_id(tool_name)

    def _finish_call_id(
        self, event: dict[str, Any], data: dict[str, Any], tool_name: str
    ) -> str:
        run_id = self._run_id(event)
        if run_id:
            return run_id
        pending = self._unmatched.get(tool_name)
        if pending:
            return pending[0]
        tool_call_id = _tool_call_id_from_output(data.get("output"))
        if tool_call_id:
            return tool_call_id
        return self._synthetic_id(tool_name)

    def _synthetic_id(self, tool_name: str) -> str:
        """``run_id`` 与 ``tool_call_id`` 都拿不到时的最后兜底（稳定自增）。"""
        self._synthetic += 1
        return f"{tool_name}:{self._synthetic}"

    def _forget_unmatched(self, tool_name: str, call_id: str) -> None:
        pending = self._unmatched.get(tool_name)
        if not pending:
            return
        try:
            pending.remove(call_id)
        except ValueError:
            return
        if not pending:
            self._unmatched.pop(tool_name, None)


__all__ = [
    "COMPACT_TAG",
    "EventTranslator",
    "REASONING_BLOCK_TYPES",
    "REASONING_KWARG_KEYS",
    "event_name",
    "event_tags",
    "extract_reasoning_text",
    "is_ai_message_chunk",
    "is_compact_event",
    "is_model_call_start",
    "message_content_to_text",
    "normalize_tool_args",
    "tool_value_to_text",
]
