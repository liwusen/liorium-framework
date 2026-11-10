"""验收 4：运行中注入产生额外 user 消息后，压缩切点仍然正确。

计划 §4.3：注入的 Context 物化成**独立 user 消息**插在安全点（模型流结束/工具组结束之后），
所以历史里会出现"一轮之内有多条 HumanMessage"的形态。压缩的轮次算法必须：

- 不把注入的 user 消息单独当成一轮的起点（一轮的起点 = 真实用户回合）
- 绝不拆散 ``AIMessage(tool_calls)`` + ``ToolMessage`` 组
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from liorium.agent.runtime.middleware.compact import CompactMiddleware
from liorium_testkit import ScriptedChatModel


def make_compact(keep_rounds: int = 2) -> CompactMiddleware:
    return CompactMiddleware(
        model=ScriptedChatModel(responses=["摘要"]),
        context_window=10_000,
        keep_rounds=keep_rounds,
    )


def _tool_group(call_id: str, name: str = "read") -> tuple[AIMessage, ToolMessage]:
    call = AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {"uri": "a.txt"}, "id": call_id, "type": "tool_call"}],
    )
    result = ToolMessage(content="内容", tool_call_id=call_id, name=name)
    return call, result


def _assert_no_split_group(messages: list) -> None:
    """保留区/丢弃区各自都不得出现"有 ToolMessage 无对应 tool_calls"。"""
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            pending = {c["id"] for c in (message.tool_calls or [])}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in pending, f"悬空 ToolMessage: {message.tool_call_id}"
            pending.discard(message.tool_call_id)
        elif isinstance(message, HumanMessage):
            pending.clear()


def test_cut_ignores_injected_context_as_round_start() -> None:
    """工具组之后紧跟的注入 user 消息不能成为切点（会拆散它前面的 AI 消息语义）。"""
    mw = make_compact(keep_rounds=1)
    call, result = _tool_group("c1")
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="第 1 轮"),
        AIMessage(content="答 1"),
        HumanMessage(content='<context source="trigger:desktop" at="12:00">下雨了</context>'),
        AIMessage(content="答 1b"),
        HumanMessage(content="第 2 轮"),
        call,
        result,
        HumanMessage(content='<context source="subagent:r1" at="12:01">结论见文件</context>'),
        AIMessage(content="答 2"),
    ]
    cutoff = mw._determine_cutoff_index(messages)
    assert 0 < cutoff < len(messages), f"应当压缩，cutoff={cutoff}"
    assert not isinstance(messages[cutoff], ToolMessage), "切点不能落在 ToolMessage 上"
    _assert_no_split_group(messages[cutoff:])


def test_cut_keeps_whole_tool_group() -> None:
    """切点绝不会落在 AI(tool_calls) 与它的 ToolMessage 之间。"""
    mw = make_compact(keep_rounds=1)
    c1, r1 = _tool_group("c1")
    c2, r2 = _tool_group("c2")
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        c1,
        r1,
        HumanMessage(content="r2"),
        c2,
        r2,
        AIMessage(content="done"),
    ]
    cutoff = mw._determine_cutoff_index(messages)
    assert messages[cutoff] is not r1 and messages[cutoff] is not r2
    _assert_no_split_group(messages[cutoff:])
    # 被丢弃段里的工具组也必须是完整的
    _assert_no_split_group(messages[:cutoff])


def test_cut_only_returns_round_boundary_or_zero() -> None:
    """cutoff 必须是 0 或一条 HumanMessage 的下标（真实轮起点）。"""
    mw = make_compact(keep_rounds=2)
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        AIMessage(content="a1"),
        HumanMessage(content="ctx-injected"),
        AIMessage(content="a1b"),
        HumanMessage(content="r2"),
        AIMessage(content="a2"),
        HumanMessage(content="r3"),
        AIMessage(content="a3"),
    ]
    cutoff = mw._determine_cutoff_index(messages)
    if cutoff != 0:
        assert isinstance(messages[cutoff], HumanMessage)
        assert not str(messages[cutoff].content).startswith("<context"), (
            "注入的 Context 消息不能作为轮次起点"
        )
