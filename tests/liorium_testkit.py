"""测试工具箱：可脚本化假模型等（跨包测试共用）。

单独成模块（而不是塞进 ``conftest.py``）的原因：仓库里有多个 ``conftest.py``，
pytest 在 prepend 模式下会把它们都当同名顶层模块 ``conftest`` 导入 → 互相覆盖
（``ImportError: cannot import name ... from 'conftest'``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class ScriptedChatModel(BaseChatModel):
    """按脚本返回响应的假模型（永不联网）。

    - ``responses``：字符串（分块流式输出）或 ``AIMessage``（可带 tool_calls）
    - 用完后重复最后一条（避免测试因多余调用而崩）
    - ``bind_tools`` 只记录 schema，不改行为
    """

    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []
    bound_tools: list[Any] = []
    stream_delay: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "liorium-scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        self.bound_tools = list(tools)
        return self

    def _next(self) -> Any:
        index = len(self.calls) - 1
        if index < len(self.responses):
            return self.responses[index]
        if self.responses:
            return self.responses[-1]
        return ""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        item = self._next()
        message = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.calls.append(list(messages))
        item = self._next()
        if isinstance(item, AIMessage):
            if item.content:
                yield ChatGenerationChunk(message=AIMessageChunk(content=item.content))
            if item.tool_calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="", tool_calls=item.tool_calls)
                )
            return
        text = str(item)
        if len(text) > 1:
            half = len(text) // 2
            chunks = [text[:half], text[half:]]
        else:
            chunks = [text]
        for chunk in chunks:
            if chunk:
                yield ChatGenerationChunk(message=AIMessageChunk(content=chunk))


def make_scripted_factory(responses: list[Any], *, stream_delay: float = 0.0):
    """返回一个 ``model_factory``：同一个模型实例被复用（调用记录可断言）。"""
    model = ScriptedChatModel(responses=responses, stream_delay=stream_delay)

    def factory(_label: str) -> ScriptedChatModel:
        return model

    factory.model = model  # type: ignore[attr-defined]
    return factory


def write_workspace(root: Path) -> Path:
    """最小 Agent 工作区：AGENT.md / ROLE.md + 一个可读文件。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENT.md").write_text("你是 Liorium 的测试角色。", encoding="utf-8")
    (root / "ROLE.md").write_text("回答要短。", encoding="utf-8")
    (root / "notes.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    return root


def events_json(events: list[Any]) -> str:
    return json.dumps([e.to_dict() for e in events], ensure_ascii=False)


__all__ = ["ScriptedChatModel", "events_json", "make_scripted_factory", "write_workspace"]
