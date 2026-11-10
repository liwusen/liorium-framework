"""会话压缩回归测试（离线，不调用真实 LLM）。

覆盖 ``docs/design/compact.md`` §16.3 的三组已确认缺陷，以及 Liorium 侧的
``emit`` 事件序列契约：

1. ``_reported_tokens`` 遇到第一条没有 ``usage_metadata`` 的 AIMessage 就返回 0 ——
   本地构造 / 插件注入的消息会命中这一分支，自动压缩**静默失效**，会话一路涨到
   必须手动 ``/compact``（最贵的那条路径）。见 I2。
2. 压缩写回的摘要被当成"一轮对话" —— 没有 SystemMessage 时摘要写成首条
   HumanMessage，于是"摘要 + 保留 N 轮"永远比 ``keep_rounds`` 多一轮，切点恒 > 0，
   **压缩后每回合都会再压一次**。见 I5。
3. 压缩调用失败 / 空摘要 / 截断都**不得**写回 checkpoint，且卡片事件必须收尾
   （``compact_done`` 带 ``ok`` 与 ``error``）。见 I14 / I15。

``/compact`` 命令本身的前端事件（``compact_start`` → ``compact_delta`` →
``compact_done`` → ``done``/``error``）由宿主负责，这里只断言中间件经构造参数
``emit`` 推出去的那一段序列。
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

from liorium.agent.runtime.middleware.compact import (
    COMPACT_MARKER_START,
    CompactMiddleware,
    _is_compact_summary,
    _reported_tokens,
)


# ── 测试替身 ──


class _FakeStream:
    """假模型：``bind_tools`` / ``with_retry`` / ``ainvoke``（压缩路径只用这三个）。"""

    _llm_type = "openai-chat"
    profile: dict = {}

    def __init__(self, *, summary: str = "## 目标与进度\n已压缩", fail: bool = False):
        self.summary = summary
        self.fail = fail
        self.calls: list[list] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def with_retry(self, **_kwargs):
        return self

    async def ainvoke(self, payload, config=None):  # noqa: ANN001
        self.calls.append(list(payload))
        if self.fail:
            raise RuntimeError("上游 500")
        return AIMessage(content=self.summary)


class _Emit:
    """事件记录器（``emit`` 的真实形态是 ``async def emit(event)``）。"""

    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, event) -> None:  # noqa: ANN001
        self.events.append(event)

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def dicts(self) -> list[dict]:
        return [event.to_dict() for event in self.events]

    def delta_text(self) -> str:
        return "".join(
            payload.get("content", "")
            for payload in self.dicts()
            if payload.get("type") == "compact_delta"
        )


def _make_middleware(
    *,
    fake: _FakeStream | None = None,
    emit: _Emit | None = None,
    keep_rounds: int = 1,
    context_window: int = 1000,
    threshold_ratio: float = 0.5,
) -> CompactMiddleware:
    fake = fake or _FakeStream()
    return CompactMiddleware(
        model=fake,
        emit=emit,
        tools=[],
        context_window=context_window,
        threshold_ratio=threshold_ratio,
        keep_rounds=keep_rounds,
    )


def _ai(content: str, *, tokens: int = 0) -> AIMessage:
    message = AIMessage(content=content)
    if tokens:
        message.usage_metadata = {
            "input_tokens": tokens,
            "output_tokens": 1,
            "total_tokens": tokens + 1,
        }
    return message


def _summary(content: str = "【摘要】") -> HumanMessage:
    return HumanMessage(content=content, additional_kwargs={"lc_source": "liorium_compact"})


def _compacted_state(out: dict) -> list:
    """把 ``abefore_model`` 的返回值应用成新的 state（丢掉清空哨兵）。"""
    return [message for message in out["messages"] if not isinstance(message, RemoveMessage)]


# ── 1. API 用量提取：不能因为末尾消息没有 usage 就放弃（I2） ──


def test_reported_tokens_skips_ai_without_usage():
    """末尾 AIMessage 没有 usage 时，应继续往前找真实上报值（而不是返回 0）。"""
    messages = [
        HumanMessage(content="早先请求"),
        _ai("早先回复", tokens=900_000),
        HumanMessage(content="最近请求"),
        _ai("本地构造的回复"),  # 无 usage_metadata
    ]
    assert _reported_tokens(messages) == 900_001


def test_reported_tokens_zero_when_no_usage_anywhere():
    """全都没有真实上报值时才返回 0（绝不本地估算）。"""
    assert _reported_tokens([HumanMessage(content="q"), _ai("a")]) == 0


def test_reported_tokens_ignores_non_ai_messages():
    """只看 AIMessage：Human/Tool 上的字段不算用量。"""
    messages = [_ai("a", tokens=500), HumanMessage(content="q")]
    assert _reported_tokens(messages) == 501


def test_reported_tokens_zero_on_empty():
    assert _reported_tokens([]) == 0


# ── 2. 轮次边界：压缩摘要不算一轮（I5） ──


def test_is_compact_summary_marker():
    """只认 ``lc_source == "liorium_compact"``；另一种 ``lc_source`` 不算。"""
    assert _is_compact_summary(_summary()) is True
    assert _is_compact_summary(HumanMessage(content="普通轮次")) is False
    assert _is_compact_summary(AIMessage(content="x")) is False
    assert _is_compact_summary(
        HumanMessage(content="别的来源", additional_kwargs={"lc_source": "summarization"})
    ) is False


def test_cutoff_skips_when_only_old_summary_is_compressible():
    """只有「摘要 + 1 轮」时不应压缩：否则只是把摘要重新摘要一遍，白花一次调用。"""
    messages = [
        _summary(),
        HumanMessage(content="r1"),
        _ai("a1", tokens=100),
    ]
    mw = _make_middleware(keep_rounds=3)
    assert mw._determine_cutoff_index(messages) == 0


def test_cutoff_compacts_real_rounds_not_the_summary():
    """「摘要 + 3 轮」时切点必须落在真实轮次上，而不是把摘要本身当一轮。"""
    messages = [
        _summary(),
        HumanMessage(content="r1"),
        _ai("a1", tokens=100),
        HumanMessage(content="r2"),
        _ai("a2", tokens=100),
        HumanMessage(content="r3"),
        _ai("a3", tokens=100),
    ]
    mw = _make_middleware(keep_rounds=3)
    cutoff = mw._determine_cutoff_index(messages)
    # 修复前：摘要被算作第 1 轮 → len(round_starts)=4 > 3 → cutoff=1（只摘要摘要）
    assert cutoff != 1, "把摘要当成了可压缩轮次"
    # 真实轮次 3 个 = keep_rounds → 退化为只保留最后一轮，丢弃摘要与 r1/r2
    assert cutoff == messages.index(messages[5])
    assert not _is_compact_summary(messages[cutoff])


def test_cutoff_requires_a_real_discarded_round():
    """丢弃段里没有真实轮次时返回 0（System 与摘要都不算轮次）。"""
    messages = [
        SystemMessage(content="sys"),
        _summary(),
        HumanMessage(content="r1"),
        _ai("a1", tokens=100),
    ]
    mw = _make_middleware(keep_rounds=1)
    # round_starts = [2]，len <= keep_rounds → cutoff=2；丢弃段只有 sys+摘要 → 0
    assert mw._determine_cutoff_index(messages) == 0


def _tool_loop(count: int) -> list:
    """单轮里连续跑 ``count`` 次工具调用。"""
    messages: list = [SystemMessage(content="sys", id="s")]
    messages.append(HumanMessage(content="跑很多工具", id="h"))
    for index in range(count):
        messages.append(
            AIMessage(
                content="",
                id=f"a{index}",
                tool_calls=[{"id": f"t{index}", "name": "read", "args": {}}],
            )
        )
        messages.append(
            ToolMessage(content=f"r{index}", tool_call_id=f"t{index}", id=f"tr{index}")
        )
    return messages


def test_single_turn_tool_loop_is_compressible():
    """单轮 agentic 长循环必须能切在完整 tool 组边界，而不是永远返回 0。"""
    messages = _tool_loop(10)
    mw = _make_middleware(keep_rounds=3)
    cutoff = mw._determine_cutoff_index(messages)
    assert cutoff > 0
    assert isinstance(messages[cutoff], AIMessage)
    # 切点是新 tool 组的起点：前缀以 Tool 结果结尾、后缀以 AIMessage 开头。
    assert messages[cutoff].type == "ai"

    _dropped, preserved = mw._partition_messages(messages, cutoff)
    preserved = mw._with_turn_anchor(messages, cutoff, preserved)
    assert isinstance(preserved[0], HumanMessage)
    assert "跑很多工具" in preserved[0].content


def test_single_turn_without_enough_tool_groups_still_skips():
    """只有一两组工具时压缩收益为负，应继续返回 0。"""
    mw = _make_middleware(keep_rounds=3)
    assert mw._determine_cutoff_index(_tool_loop(2)) == 0


# ── 3. emit 事件序列（前端按这个顺序渲染卡片） ──


def _threshold_messages() -> list:
    return [
        SystemMessage(content="sys", id="s1"),
        HumanMessage(content="r1", id="h1"),
        _ai("a1", tokens=900),
        HumanMessage(content="r2", id="h2"),
        _ai("a2", tokens=950),
    ]


async def test_compact_success_emits_start_delta_done():
    """成功：``compact_start`` → ``compact_delta``(×N) → ``compact_done``。

    摘要文本**只能**走 ``compact_delta``，绝不出现在正文 ``delta`` 里（I15）。
    """
    emit = _Emit()
    fake = _FakeStream(summary="## 目标与进度\n摘要正文")
    mw = _make_middleware(fake=fake, emit=emit)

    out = await mw.abefore_model({"messages": _threshold_messages()}, None)

    assert out is not None
    assert emit.types() == ["compact_start", "compact_delta", "compact_done"]
    assert emit.delta_text() == "## 目标与进度\n摘要正文"
    done = emit.dicts()[-1]
    assert done.get("ok") is not False
    assert "error" not in done


async def test_compact_success_carries_no_failure_flag():
    """成功路径的 ``compact_done`` 不带 ``ok=False``、不带 ``error``。"""
    emit = _Emit()
    mw = _make_middleware(emit=emit)

    await mw.abefore_model({"messages": _threshold_messages()}, None)

    done = [payload for payload in emit.dicts() if payload.get("type") == "compact_done"][-1]
    assert done.get("ok") is not False
    assert "error" not in done


async def test_compact_never_emits_plain_delta_events():
    """事件类型必须全部落在 ``compact_*`` 里（摘要不得泄进正文气泡）。"""
    emit = _Emit()
    mw = _make_middleware(emit=emit)

    await mw.abefore_model({"messages": _threshold_messages()}, None)

    assert all(event_type.startswith("compact_") for event_type in emit.types())
    assert "delta" not in emit.types()


async def test_compact_failure_emits_single_failed_done():
    """失败：``compact_start`` → ``compact_done(ok=False, error=…)``，且返回 None。"""
    emit = _Emit()
    fake = _FakeStream(fail=True)
    mw = _make_middleware(fake=fake, emit=emit)

    assert await mw.abefore_model({"messages": _threshold_messages()}, None) is None

    assert emit.types() == ["compact_start", "compact_done"]
    done = emit.dicts()[-1]
    assert done.get("ok") is False
    assert done.get("error")


async def test_compact_failure_without_emit_does_not_raise():
    """没有 emit（测试/无界面宿主）时失败路径也不能抛。"""
    fake = _FakeStream(fail=True)
    mw = _make_middleware(fake=fake, emit=None)

    assert await mw.abefore_model({"messages": _threshold_messages()}, None) is None


async def test_force_compact_emits_start_delta_done_and_returns_update():
    """手动路径：同样推 ``compact_start`` → ``compact_delta`` → ``compact_done``，返回写回字典。"""
    emit = _Emit()
    fake = _FakeStream(summary="摘要正文")
    mw = _make_middleware(fake=fake, emit=emit)

    update = await mw.force_compact(_threshold_messages())

    assert update is not None
    assert [type(message).__name__ for message in update["messages"]] == [
        "RemoveMessage",
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
    ]
    assert emit.types() == ["compact_start", "compact_delta", "compact_done"]


async def test_force_compact_uses_on_delta_instead_of_emitting_delta():
    """手动路径传了 ``on_delta`` ⇒ 增量走回调，不再重复发 ``compact_delta``。"""
    emit = _Emit()
    received: list[str] = []

    async def on_delta(text: str) -> None:
        received.append(text)

    mw = _make_middleware(emit=emit)

    await mw.force_compact(_threshold_messages(), on_delta=on_delta)

    assert received == ["## 目标与进度\n已压缩"]
    assert emit.types() == ["compact_start", "compact_done"]


async def test_force_compact_without_rounds_returns_none_without_events():
    """没有可压缩轮次 ⇒ 返回 None，中间件不发事件（卡片文案由宿主给）。"""
    emit = _Emit()
    mw = _make_middleware(emit=emit)

    assert await mw.force_compact([SystemMessage(content="sys"), HumanMessage(content="only")]) is None
    assert emit.types() == []


async def test_force_compact_without_messages_returns_none():
    """空会话 ⇒ None（宿主据此给"没有可压缩的上下文"卡片）。"""
    mw = _make_middleware()
    assert await mw.force_compact([]) is None


async def test_force_compact_failure_marks_card_failed_and_raises():
    """手动路径失败：先收尾卡片（``ok=False`` + ``error``），再上抛给命令处理器。"""
    emit = _Emit()
    fake = _FakeStream(fail=True)
    mw = _make_middleware(fake=fake, emit=emit)

    with pytest.raises(RuntimeError):
        await mw.force_compact(_threshold_messages())

    assert emit.types() == ["compact_start", "compact_done"]
    done = emit.dicts()[-1]
    assert done.get("ok") is False
    assert done.get("error")


async def test_force_compact_empty_summary_raises():
    """空摘要视为失败：抛错且不写回（否则写回就是"把历史换成空白"）。"""
    emit = _Emit()
    fake = _FakeStream(summary="   ")
    mw = _make_middleware(fake=fake, emit=emit)

    with pytest.raises(RuntimeError, match="为空"):
        await mw.force_compact(_threshold_messages())

    done = emit.dicts()[-1]
    assert done.get("ok") is False


# ── 4. 压缩一次之后不得立刻再压（I5 / I13 / §18 验收） ──


async def test_second_compaction_does_not_repeat_without_new_rounds():
    """压缩后的 state 再进 ``abefore_model``：切点为 0，不再发第二次摘要调用。"""
    fake = _FakeStream(summary="摘要正文")
    mw = _make_middleware(fake=fake)

    first = await mw.abefore_model({"messages": _threshold_messages()}, None)
    assert first is not None
    assert len(fake.calls) == 1

    compacted = _compacted_state(first)
    assert await mw.abefore_model({"messages": compacted}, None) is None
    assert len(fake.calls) == 1, "没有新轮次却再次压缩 = 每回合白烧一次全量调用"


async def test_system_keeps_only_one_summary_after_two_compactions():
    """压缩两次后 System 只剩一段摘要（先剥旧再写新，I13）。"""
    fake = _FakeStream(summary="第一版摘要")
    mw = _make_middleware(fake=fake)

    first = await mw.abefore_model({"messages": _threshold_messages()}, None)
    assert first is not None
    state = _compacted_state(first)
    # 模拟又过了一轮：用户发新请求，模型带着压缩前的上报值回复
    state = [*state, HumanMessage(content="r3", id="h3"), _ai("a3", tokens=950)]
    fake.summary = "第二版摘要"

    second = await mw.abefore_model({"messages": state}, None)
    assert second is not None
    merged = second["messages"][1]
    assert isinstance(merged, SystemMessage)
    assert merged.content.count(COMPACT_MARKER_START) == 1
    assert "第一版摘要" not in merged.content
    assert "第二版摘要" in merged.content
