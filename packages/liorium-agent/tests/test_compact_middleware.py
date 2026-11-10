"""CompactMiddleware 单测（离线，不调用真实 LLM）。

逐条移植 ``docs/design/compact.md`` §16.1 的 19 条断言（编号见每个测试的 docstring），
外加 Liorium 特有的四条：失败重试次数、同步入口禁用、未知上下文窗口不触发、
跨压缩保留钩子。

约定：不 import 任何宿主模块；假模型只实现压缩路径真正用到的
``bind_tools`` / ``bind`` / ``with_retry`` / ``ainvoke`` 四件套。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from liorium.agent.runtime.middleware.compact import (
    COMPACT_MARKER_END,
    COMPACT_MARKER_START,
    CompactMiddleware,
    _reported_tokens,
    _strip_summary,
    _wrap_summary,
    build_mock_tools,
)


# ── 测试替身 ──


@tool
def read_file(path: str, limit: int = 100) -> str:
    """读取一个文件。"""
    return f"real:{path}"


class _Retrying:
    """复刻 ``Runnable.with_retry()`` 的语义：同一个调用最多尝试 3 次。"""

    def __init__(self, target: "_FakeStream", attempts: int = 3) -> None:
        self._target = target
        self._attempts = attempts

    async def ainvoke(self, payload, config=None):  # noqa: ANN001
        last: Exception | None = None
        for _ in range(self._attempts):
            try:
                return await self._target.ainvoke(payload, config=config)
            except Exception as exc:  # noqa: BLE001 - 模拟瞬态故障
                last = exc
        assert last is not None
        raise last


class _FakeStream:
    """假模型：``bind_tools`` / ``bind`` / ``with_retry`` / ``ainvoke`` 四件套。

    ``_llm_type`` 与 ``profile`` 是基类 ``SummarizationMiddleware.__init__`` 会读取的
    属性（前者用于挑选近似 token 计数器），必须提供。
    """

    _llm_type = "openai-chat"
    profile: dict = {}

    def __init__(self, *, summary: str = "## 目标与进度\n已完成压缩", fail: bool = False):
        self.summary = summary
        self.fail = fail
        self.calls: list[list] = []
        self.bound_log: list[list] = []
        self.bind_kwargs: list[dict] = []

    def _clone(self) -> "_FakeStream":
        clone = _FakeStream(summary=self.summary, fail=self.fail)
        # 绑定返回克隆体；调用记录必须共享，否则断言看不到真实 payload
        clone.calls = self.calls
        clone.bound_log = self.bound_log
        clone.bind_kwargs = self.bind_kwargs
        return clone

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        self.bound_log.append(list(tools))
        return self._clone()

    def bind(self, **kwargs):  # noqa: ANN003
        self.bind_kwargs.append(dict(kwargs))
        return self._clone()

    def with_retry(self, **_kwargs):
        return _Retrying(self)

    async def ainvoke(self, payload, config=None):  # noqa: ANN001
        self.calls.append(list(payload))
        if self.fail:
            raise RuntimeError("上游 500")
        return AIMessage(content=self.summary)


def _make_middleware(
    *,
    fake: _FakeStream | None = None,
    keep_rounds: int = 1,
    tools=None,
    context_window: int = 1000,
    threshold_ratio: float = 0.8,
    **kwargs,
) -> CompactMiddleware:
    fake = fake or _FakeStream()
    return CompactMiddleware(
        model=fake,
        tools=tools if tools is not None else [read_file],
        context_window=context_window,
        threshold_ratio=threshold_ratio,
        keep_rounds=keep_rounds,
        **kwargs,
    )


def _ai(content: str, *, tokens: int = 0, tool_calls=None) -> AIMessage:
    message = AIMessage(content=content, tool_calls=tool_calls or [])
    if tokens:
        message.usage_metadata = {
            "input_tokens": tokens,
            "output_tokens": 1,
            "total_tokens": tokens,
        }
    return message


def _messages_above_threshold() -> list:
    """两轮真实历史 + 一条超过阈值（500）的上报值。"""
    return [
        SystemMessage(content="你是 Liorium。", id="s1"),
        HumanMessage(content="r1", id="h1"),
        _ai("a1", tokens=900),
        HumanMessage(content="r2", id="h2"),
        _ai("a2", tokens=950),
    ]


# ── 1. API token 提取（#1 / #2） ──


def test_reported_tokens_uses_api_value():
    """#1 多轮历史取**最新**上报值（9000），不是最早或最大值。"""
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        _ai("old", tokens=500),
        HumanMessage(content="new"),
        _ai("latest", tokens=9000),
    ]
    assert _reported_tokens(messages) == 9000


def test_reported_tokens_zero_without_api_data():
    """#2 无 AI 用量 ⇒ 0（绝不本地估算）。"""
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    assert _reported_tokens(messages) == 0


# ── 2. mock 工具 schema 逐字节一致（#3 / #4） ──


def test_mock_tool_schema_is_byte_identical():
    """#3 tools 定义位于请求最前端，差一个字节前缀缓存就整段失效。"""
    mock = build_mock_tools([read_file])[0]
    assert convert_to_openai_tool(mock) == convert_to_openai_tool(read_file)
    assert mock.name == read_file.name
    assert mock.description == read_file.description


def test_mock_tool_does_not_execute_real_impl():
    """#4 压缩请求绝不能真的执行工具。"""
    mock = build_mock_tools([read_file])[0]
    assert mock.invoke({"path": "/etc/passwd"}) == ""


# ── 3. 轮次边界：保留 N 轮且不拆 tool 组（#5 - #8） ──


def test_cutoff_keeps_last_n_rounds():
    """#5 ``keep_rounds=1`` + 3 轮 ⇒ 切在最后一轮起点（索引 5）。"""
    mw = _make_middleware(keep_rounds=1)
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        _ai("a1"),
        HumanMessage(content="r2"),
        _ai("a2"),
        HumanMessage(content="r3"),
        _ai("a3"),
    ]
    assert mw._determine_cutoff_index(messages) == 5


def test_cutoff_never_splits_tool_call_group():
    """#6 保留区与丢弃区**各自**不存在"有 ToolMessage 无 tool_calls"的悬空。"""
    mw = _make_middleware(keep_rounds=1)
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        _ai("a1"),
        HumanMessage(content="r2"),
        _ai("", tool_calls=[{"id": "call_1", "name": "read_file", "args": {"path": "x"}}]),
        ToolMessage(content="tool out", tool_call_id="call_1"),
        HumanMessage(content="r3"),
        _ai("a3"),
    ]
    cutoff = mw._determine_cutoff_index(messages)

    preserved = messages[cutoff:]
    kept_tool_ids = {
        call["id"]
        for message in preserved
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    }
    kept_tool_msgs = {m.tool_call_id for m in preserved if isinstance(m, ToolMessage)}
    assert kept_tool_msgs <= kept_tool_ids

    dropped = messages[:cutoff]
    dropped_tool_ids = {
        call["id"]
        for message in dropped
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    }
    dropped_tool_msgs = {m.tool_call_id for m in dropped if isinstance(m, ToolMessage)}
    assert dropped_tool_msgs <= dropped_tool_ids


def test_cutoff_advances_to_include_ai_when_boundary_is_tool_message():
    """#7 切点落在 tool 组上时，保留区 ToolMessage ⊆ 有 tool_calls 的 AIMessage。

    ``find_safe_cutoff_point`` 的方向是**向前回退**（整组一起进丢弃段），
    所以这里同时断言"回退生效"：切点落在 ToolMessage 上时结果 ≤ 原切点。
    """
    mw = _make_middleware(keep_rounds=1)
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        _ai("", tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "x"}}]),
        ToolMessage(content="out", tool_call_id="c1"),
        HumanMessage(content="r2"),
        _ai("a2"),
    ]
    cutoff = mw._determine_cutoff_index(messages)
    preserved = messages[cutoff:]
    kept_ids = {
        call["id"]
        for message in preserved
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    }
    kept_tool = {m.tool_call_id for m in preserved if isinstance(m, ToolMessage)}
    assert kept_tool <= kept_ids

    # 直接把切点指到 ToolMessage 上：必须回退到发起调用的 AIMessage（索引 2）
    assert mw._find_safe_cutoff_point(messages, 3) == 2


def test_cutoff_zero_when_nothing_to_compress():
    """#8 ``[S, H(only)]`` ⇒ 0（丢弃段里没有真实轮次）。"""
    mw = _make_middleware(keep_rounds=1)
    messages = [SystemMessage(content="sys"), HumanMessage(content="only")]
    assert mw._determine_cutoff_index(messages) == 0


# ── 4. 写回：摘要并入 System，顺序正确（#9 - #12） ──


def test_strip_and_wrap_summary_are_inverse():
    """#9 包装含起止标记；剥掉包装后回到原始 prompt。"""
    base = "你是 Liorium。"
    wrapped = _wrap_summary(base, "## 目标与进度\n做重构")
    assert COMPACT_MARKER_START in wrapped and COMPACT_MARKER_END in wrapped
    assert _strip_summary(wrapped) == base


def test_strip_summary_replaces_previous_summary():
    """#10 二次包装：旧摘要消失、新摘要存在、START 只出现一次（I13）。"""
    first = _wrap_summary("你是 Liorium。", "第一版摘要")
    second = _wrap_summary(_strip_summary(first), "第二版摘要")
    assert "第一版摘要" not in second
    assert "第二版摘要" in second
    assert second.count(COMPACT_MARKER_START) == 1


def test_build_replacement_merges_into_system():
    """#11 长度 1、是 SystemMessage、保留原 id（就地替换）、原文与摘要都在。"""
    mw = _make_middleware()
    messages = [
        SystemMessage(content="你是 Liorium。", id="s1"),
        HumanMessage(content="r1", id="h1"),
        _ai("a1", tokens=100),
        HumanMessage(content="r2", id="h2"),
        _ai("a2", tokens=200),
    ]
    replacement = mw._build_replacement(messages, "摘要正文")
    assert len(replacement) == 1
    only = replacement[0]
    assert isinstance(only, SystemMessage)
    assert only.id == "s1"
    assert "你是 Liorium。" in only.content
    assert "摘要正文" in only.content


def test_build_replacement_falls_back_to_human_without_system():
    """#12 无 System（Subagent）⇒ 摘要退化为首条 HumanMessage 且带幂等标记。"""
    mw = _make_middleware()
    messages = [HumanMessage(content="r1", id="h1"), _ai("a1", tokens=100)]
    replacement = mw._build_replacement(messages, "摘要正文")
    assert len(replacement) == 1
    assert isinstance(replacement[0], HumanMessage)
    assert "摘要正文" in replacement[0].content
    assert replacement[0].additional_kwargs["lc_source"] == "liorium_compact"


# ── 5. abefore_model 端到端（#13 - #17） ──


async def test_abefore_model_skips_without_api_tokens():
    """#13 无用量 ⇒ None（I1：不猜）。"""
    mw = _make_middleware(context_window=1000, threshold_ratio=0.5)
    messages = [SystemMessage(content="sys"), HumanMessage(content="r1"), _ai("a1")]
    assert await mw.abefore_model({"messages": messages}, None) is None


async def test_abefore_model_skips_below_threshold():
    """#14 100 < 8000 ⇒ None。"""
    mw = _make_middleware(context_window=10000, threshold_ratio=0.8)
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="r1"),
        _ai("a1", tokens=100),
        HumanMessage(content="r2"),
        _ai("a2", tokens=100),
    ]
    assert await mw.abefore_model({"messages": messages}, None) is None


async def test_abefore_model_compacts_and_orders_messages():
    """#15 写回顺序：清空哨兵 → 合并 System（同 id）→ 保留轮次。"""
    fake = _FakeStream(summary="## 目标与进度\n完成压缩")
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    out = await mw.abefore_model({"messages": _messages_above_threshold()}, None)
    assert out is not None
    updates = out["messages"]
    assert isinstance(updates[0], RemoveMessage)
    assert updates[0].id == REMOVE_ALL_MESSAGES
    assert isinstance(updates[1], SystemMessage)
    assert updates[1].id == "s1"
    assert "完成压缩" in updates[1].content
    assert [type(m).__name__ for m in updates[2:]] == ["HumanMessage", "AIMessage"]
    assert updates[2].content == "r2"


async def test_compact_call_reuses_full_prefix_with_mock_tools():
    """#16 payload 前缀与主请求逐条同 id，尾部只多一条指令；工具换成同 schema 的 mock。"""
    fake = _FakeStream()
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    messages = _messages_above_threshold()
    await mw.abefore_model({"messages": messages}, None)

    assert fake.calls, "压缩调用没有发生"
    sent = fake.calls[0]
    assert [m.id for m in sent[: len(messages)]] == [m.id for m in messages]
    assert len(sent) == len(messages) + 1
    assert isinstance(sent[-1], HumanMessage)
    assert "调用任何工具" in sent[-1].content
    assert fake.bound_log, "压缩调用没有绑定工具"
    assert convert_to_openai_tool(fake.bound_log[0][0]) == convert_to_openai_tool(read_file)


async def test_abefore_model_degrades_on_failure():
    """#17 压缩调用抛错 ⇒ 返回 None（不改 checkpoint），本轮继续。"""
    fake = _FakeStream(fail=True)
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    assert await mw.abefore_model({"messages": _messages_above_threshold()}, None) is None


# ── 6. 压缩请求自修复悬空 tool_calls（#18 / #19） ──


def _tool_call_violations(messages: list) -> list[str]:
    """OpenAI 约束校验：assistant 的每个 tool_call 必须在遇到下一条非 tool
    消息之前被对应的 ToolMessage 响应。返回违规描述（空列表 = 合法）。"""
    problems: list[str] = []
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            if pending:
                problems.append(f"未响应 {sorted(pending)} 就出现新的 AIMessage")
            pending = {call["id"] for call in (message.tool_calls or [])}
        elif isinstance(message, ToolMessage):
            pending.discard(message.tool_call_id)
        else:
            if pending:
                problems.append(f"未响应 {sorted(pending)} 就出现 {type(message).__name__}")
            pending = set()
    if pending:
        problems.append(f"序列结尾未响应 {sorted(pending)}")
    return problems


async def test_compact_call_repairs_dangling_tool_calls_mid_history():
    """#18 历史中段悬空 ⇒ payload 无违规，且**不注入**占位（清空 tool_calls）。"""
    fake = _FakeStream()
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    messages = [
        SystemMessage(content="sys", id="s1"),
        HumanMessage(content="r1", id="h1"),
        _ai("", tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "x"}}]),
        # ← 缺少 ToolMessage("c1")：工具执行被中断的遗留
        HumanMessage(content="r2", id="h2"),
        _ai("a2", tokens=950),
    ]
    assert _tool_call_violations(messages), "测试前提：入参 state 本身违规"

    await mw.abefore_model({"messages": messages}, None)
    assert fake.calls, "压缩调用没有发生"
    sent = fake.calls[0]
    assert _tool_call_violations(sent) == []
    assert not any(isinstance(m, ToolMessage) for m in sent)


async def test_compact_call_placeholder_for_trailing_dangling_tool_calls():
    """#19 序列结尾悬空 ⇒ 注入占位，且占位必须排在指令**之前**（I9）。"""
    fake = _FakeStream()
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    messages = [
        SystemMessage(content="sys", id="s1"),
        HumanMessage(content="r1", id="h1"),
        _ai("a1", tokens=900),
        HumanMessage(content="r2", id="h2"),
        _ai(
            "",
            tokens=950,
            tool_calls=[{"id": "c9", "name": "read_file", "args": {"path": "x"}}],
        ),
    ]
    assert _tool_call_violations(messages), "测试前提：入参 state 本身违规"

    await mw.abefore_model({"messages": messages}, None)
    assert fake.calls, "压缩调用没有发生"
    sent = fake.calls[0]
    assert _tool_call_violations(sent) == []
    placeholder_ids = {m.tool_call_id for m in sent if isinstance(m, ToolMessage)}
    assert "c9" in placeholder_ids, "结尾悬空必须注入占位 ToolMessage"
    assert isinstance(sent[-1], HumanMessage), "指令必须是最后一条"
    assert isinstance(sent[-2], ToolMessage), "占位必须排在指令之前"


# ── 7. Liorium 特有：重试 / 同步入口 / 未知窗口 / 保留钩子 ──


async def test_summary_failure_retries_three_times_before_degrading():
    """失败重试 3 次（``with_retry``）后才降级为"不压缩"。"""
    fake = _FakeStream(fail=True)
    mw = _make_middleware(
        fake=fake, keep_rounds=1, context_window=1000, threshold_ratio=0.5
    )
    assert await mw.abefore_model({"messages": _messages_above_threshold()}, None) is None
    assert len(fake.calls) == 3, "压缩调用应当在进程内重试 3 次"


def test_sync_before_model_is_not_provided():
    """同步入口显式禁用：基类的同步写回会产出另一种格式的摘要（§2）。"""
    mw = _make_middleware()
    with pytest.raises(NotImplementedError):
        mw.before_model({"messages": []}, None)


async def test_abefore_model_skips_when_context_window_unknown():
    """``context_window=0``（未知）⇒ 永不触发，而不是把阈值算成 1 每回合都压。"""
    fake = _FakeStream()
    mw = _make_middleware(fake=fake, context_window=0)
    assert mw.threshold_tokens == 0
    messages = _messages_above_threshold()
    assert await mw.abefore_model({"messages": messages}, None) is None
    assert fake.calls == []


async def test_preserve_predicate_keeps_extra_messages():
    """跨压缩保留钩子（I17）：落在丢弃段的额外消息被插在头部之后、保留轮次之前。"""
    notice = HumanMessage(
        content="<vng-mode on>", additional_kwargs={"lc_source": "liorium_vng_mode"}
    )
    seen: list[list] = []

    def hook(messages, preserved):  # noqa: ANN001
        seen.append(list(preserved))
        already = any(
            m is notice for m in preserved
        )
        return [] if already else [notice]

    fake = _FakeStream(summary="摘要正文")
    mw = _make_middleware(
        fake=fake,
        keep_rounds=1,
        context_window=1000,
        threshold_ratio=0.5,
        preserve_predicate=hook,
    )
    messages = [
        SystemMessage(content="sys", id="s1"),
        notice,
        HumanMessage(content="r1", id="h1"),
        _ai("a1", tokens=900),
        HumanMessage(content="r2", id="h2"),
        _ai("a2", tokens=950),
    ]
    out = await mw.abefore_model({"messages": messages}, None)
    assert out is not None
    updates = out["messages"]
    assert updates[2] is notice
    assert seen, "钩子没有被调用"


async def test_preserve_predicate_failure_does_not_block_compaction():
    """钩子抛错绝不能影响压缩（I17）。"""

    def hook(messages, preserved):  # noqa: ANN001
        raise RuntimeError("钩子坏了")

    fake = _FakeStream(summary="摘要正文")
    mw = _make_middleware(
        fake=fake,
        keep_rounds=1,
        context_window=1000,
        threshold_ratio=0.5,
        preserve_predicate=hook,
    )
    out = await mw.abefore_model({"messages": _messages_above_threshold()}, None)
    assert out is not None
    assert isinstance(out["messages"][1], SystemMessage)
