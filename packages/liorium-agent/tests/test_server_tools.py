"""ServerToolsMiddleware：`vfs-output-mw.md` §8 的最小可用面。

spec 已由宿主解析好；本层只负责「空 specs = 严格 no-op；非空 = 追加到本地
工具之后」。
"""

from __future__ import annotations

import asyncio

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from liorium.agent.runtime.middleware.server_tools import ServerToolsMiddleware


@tool
def local_tool(path: str) -> str:
    """一个本地工具。"""
    return f"real:{path}"


def _request(tools=None) -> ModelRequest:
    return ModelRequest(
        model=None,  # type: ignore[arg-type]
        messages=[HumanMessage(content="hi")],
        tools=list(tools or []),
    )


def _response() -> ModelResponse:
    return ModelResponse(result=[AIMessage(content="ok")])


# ── 空 specs = no-op ────────────────────────────────────────────────────


def test_empty_specs_is_passthrough():
    middleware = ServerToolsMiddleware()
    requests: list[ModelRequest] = []

    def handler(request):
        requests.append(request)
        return _response()

    original = _request([local_tool])
    middleware.wrap_model_call(original, handler)

    assert requests[0] is original


def test_empty_specs_async_is_passthrough():
    middleware = ServerToolsMiddleware([])
    requests: list[ModelRequest] = []

    async def handler(request):
        requests.append(request)
        return _response()

    original = _request([local_tool])
    asyncio.run(middleware.awrap_model_call(original, handler))

    assert requests[0] is original


# ── 非空 specs = 追加到本地工具之后 ─────────────────────────────────────


def test_sync_wrap_appends_specs_after_local_tools():
    middleware = ServerToolsMiddleware(
        [{"type": "web_search_20250305", "name": "web_search"}]
    )
    seen: list[list] = []

    def handler(request):
        seen.append(list(request.tools))
        return _response()

    original = _request([local_tool])
    middleware.wrap_model_call(original, handler)

    assert seen
    tools = seen[0]
    assert tools[0] is local_tool
    assert tools[-1]["name"] == "web_search"
    # 原 request 不被就地修改
    assert original.tools == [local_tool]


def test_async_wrap_appends_specs():
    middleware = ServerToolsMiddleware([{"google_search": {}}])
    seen: list[list] = []

    async def handler(request):
        seen.append(list(request.tools))
        return _response()

    asyncio.run(middleware.awrap_model_call(_request([local_tool]), handler))

    assert seen[0][0] is local_tool
    assert seen[0][-1] == {"google_search": {}}


def test_specs_are_copied_per_instance():
    specs = [{"type": "web_search"}]
    middleware = ServerToolsMiddleware(specs)
    specs[0]["type"] = "mutated"

    assert middleware.specs == [{"type": "web_search"}]


def test_positional_and_keyword_construction_are_equivalent():
    positional = ServerToolsMiddleware([{"type": "web_search"}])
    keyword = ServerToolsMiddleware(specs=({"type": "web_search"},))

    assert positional.specs == keyword.specs == [{"type": "web_search"}]


def test_specs_default_is_empty():
    assert ServerToolsMiddleware().specs == []
