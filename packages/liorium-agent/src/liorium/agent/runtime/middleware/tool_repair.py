"""Tool-call 修复：永不允许悬空 ``tool_call`` 到达模型。

计划 §4.1：位于基座链，**永远打开**。规格见 ``docs/design/vfs-output-mw.md`` §6，
唯一实现是纯函数 :func:`liorium.agent.runtime.repair.repair_dangling_tool_calls`。

- 被打断的 AI 消息（悬空 ``tool_calls``）→ 作废（清空 ``tool_calls`` + content
  里的 ``tool_use`` / ``function_call`` / ``tool_call`` 块），**不注入占位**；
- 序列结尾的悬空 ``tool_calls`` → 注入占位 ``ToolMessage``（冻结文本在
  ``repair.PLACEHOLDER_CONTENT``）；
- 孤立 ``ToolMessage`` → 丢弃；``server_tool_use`` 等 provider 托管块永不触碰。

入口是模型调用级（``wrap_model_call`` / ``awrap_model_call``）：用
``request.override(messages=...)`` 只改本次请求，**不写回 checkpoint**——
checkpoint 允许存在悬空调用，修复是请求期的净化。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from liorium.core.log import get_logger

from ..repair import PLACEHOLDER_CONTENT, count_violations, repair_dangling_tool_calls

log = get_logger("liorium.agent.tool_repair")

_REPORT_LIMIT = 5
"""日志里最多列几处问题（其余只说总数，避免一行日志塞满上下文）。"""


def _repair_with_report(messages: list[Any]) -> list[Any]:
    """修复前先审计：**结果凭空消失这件事必须留痕**。

    2026-11-08：宿主看到 ``[工具调用结果缺失，已由 Liorium 自动补全]`` 却查不到
    为什么 —— 修复是请求期的静默净化（不写回 checkpoint），所以这里补一条 WARNING，
    至少让 session 日志能对上"哪次调用丢了结果"。
    """
    problems = count_violations(messages)
    if problems:
        head = "；".join(problems[:_REPORT_LIMIT])
        if len(problems) > _REPORT_LIMIT:
            head += f"（共 {len(problems)} 处）"
        log.warning(
            "悬空 tool_call 已被占位补齐（%d 处）：%s。"
            "常见原因：回合被中断 / 工具调用被取消 / 进程崩在工具执行中。",
            len(problems),
            head,
        )
    return repair_dangling_tool_calls(messages)


class ToolCallRepairMiddleware(AgentMiddleware):
    """在每次模型调用前修复请求消息里悬空的 assistant tool_calls。"""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """同步入口：修复消息后调用模型。"""
        if not request.messages:
            return handler(request)
        return handler(request.override(messages=_repair_with_report(request.messages)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """异步入口：修复消息后调用模型。"""
        if not request.messages:
            return await handler(request)
        return await handler(
            request.override(messages=_repair_with_report(request.messages))
        )


__all__ = ["PLACEHOLDER_CONTENT", "ToolCallRepairMiddleware"]
