"""Provider 托管工具（server tools）规格注入。

计划 §4.1：可选层，位于 repair 与 compact 之间。规格见
``docs/design/vfs-output-mw.md`` §8。

服务端工具由**服务商自己执行**（OpenAI Responses / Anthropic Messages /
Google Gemini），它们在请求里是 ``tools`` 数组的成员，但不产生普通
``tool_calls``，也不进本地 ``ToolNode``：

- 本层只做「已解析好的 spec dict → 模型请求」的搬运，spec 的解析（按 provider
  与 api_format 决定形态）在宿主侧完成，库不读产品配置；
- spec 永远**追加在本地工具之后**（``tools[0]`` 是本地工具、``tools[-1]`` 是
  spec），与压缩请求的前缀保持逐字节一致以命中前缀缓存；
- ``specs`` 为空时是严格 no-op（不 ``override``，转发原 request 对象）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse


class ServerToolsMiddleware(AgentMiddleware):
    """把 provider 托管工具规格追加到每次模型请求的 ``tools`` 列表。"""

    def __init__(self, specs: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__()
        #: 复制一份，避免不同 Agent / 请求之间共享同一个 dict 被就地修改。
        self.specs: list[dict[str, Any]] = [dict(spec) for spec in (specs or [])]

    def _merged_tools(self, request: ModelRequest[Any]) -> list[Any]:
        return [*(request.tools or []), *self.specs]

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """同步入口：空 specs 原样透传，否则追加 spec 后调用模型。"""
        if not self.specs:
            return handler(request)
        return handler(request.override(tools=self._merged_tools(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """异步入口：空 specs 原样透传，否则追加 spec 后调用模型。"""
        if not self.specs:
            return await handler(request)
        return await handler(request.override(tools=self._merged_tools(request)))


__all__ = ["ServerToolsMiddleware"]
