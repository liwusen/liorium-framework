"""会话压缩成本控制测试（离线，不调用真实 LLM）。

逐条移植 ``docs/design/compact.md`` §16.4 的 6 条，其中第 6 条按 Liorium 形态改写：

1. 成本控制**默认关闭**：不设摘要输出上限、不裁剪输入，payload 与主请求前缀
   逐字节一致（完整命中上游前缀缓存）。
2. 只有显式打开后，``max_output_tokens`` / ``max_input_tokens`` 才生效。
3. 摘要被输出上限截断时按**失败**处理：不写回 checkpoint（否则摘要没覆盖到的
   历史会被永久删掉），并在本进程内自动放弃该上限，避免每回合重复付费。
4. 压缩调用用量必须可审计（开关开或不开都记录），否则无法判断前缀缓存是否命中。
5. 原型第 6 条断言"五个配置键出现在公共配置默认值里"。Liorium 没有产品配置面，
   等价契约是 ``AgentSpec.Limits`` 的默认值与冻结表一致、且能直接映射到构造参数。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from liorium.agent.runtime.middleware.compact import CompactMiddleware
from liorium.agent.spec import Limits


# ── 测试替身 ──


class _FakeCostStream:
    """假压缩模型：记录 ``bind()`` 入参，可模拟"摘要被截断"。

    ``bind_kwargs`` 是"成本控制是否真的把 max_tokens 施加到压缩调用上"的唯一证据。
    """

    _llm_type = "openai-chat"
    profile: dict = {}

    def __init__(
        self,
        *,
        summary: str = "## 目标与进度\n已压缩",
        finish_reason: str | None = None,
        output_tokens: int = 0,
    ) -> None:
        self.summary = summary
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.calls: list[list] = []
        self.bind_kwargs: list[dict] = []

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def bind(self, **kwargs):  # noqa: ANN003
        self.bind_kwargs.append(dict(kwargs))
        return self

    def with_retry(self, **_kwargs):
        return self

    async def ainvoke(self, payload, config=None):  # noqa: ANN001
        self.calls.append(list(payload))
        message = AIMessage(content=self.summary)
        if self.finish_reason:
            message.response_metadata = {"finish_reason": self.finish_reason}
        if self.output_tokens:
            message.usage_metadata = {
                "input_tokens": 10,
                "output_tokens": self.output_tokens,
                "total_tokens": 10 + self.output_tokens,
            }
        return message


def _mw_with(fake: _FakeCostStream, **kwargs) -> CompactMiddleware:
    return CompactMiddleware(
        model=fake,
        tools=[],
        context_window=1000,
        threshold_ratio=0.5,
        keep_rounds=1,
        **kwargs,
    )


def _long_messages(count: int = 6, size: int = 200) -> list[HumanMessage]:
    return [HumanMessage(content="x" * size) for _ in range(count)]


# ── 开关：默认关闭 / 打开后才施加两个上限 ──


async def test_cost_control_off_by_default_leaves_payload_intact():
    """默认关闭：不 bind max_tokens、不裁剪输入 ⇒ payload 与主请求前缀逐字节一致。"""
    fake = _FakeCostStream()
    messages = _long_messages()

    summary = await _mw_with(fake, max_output_tokens=123, max_input_tokens=5)._acompact(messages)

    assert summary == fake.summary
    assert fake.bind_kwargs == [], "默认关闭时不应设置摘要输出上限"
    # 完整 payload = 全部消息 + 尾端指令
    assert len(fake.calls[0]) == len(messages) + 1


async def test_cost_control_on_applies_both_caps():
    """打开开关后两个上限才生效：输出 max_tokens + 输入裁剪。"""
    fake = _FakeCostStream()

    await _mw_with(
        fake, cost_control=True, max_output_tokens=123, max_input_tokens=5
    )._acompact(_long_messages())

    assert fake.bind_kwargs == [{"max_tokens": 123}]
    # 输入上限 5 token ≈ 12 字符 → 只留下最后一条消息 + 指令
    assert len(fake.calls[0]) == 2


# ── 截断保护 ──


async def test_truncated_summary_raises_and_cap_self_heals():
    """摘要被截断：不返回（调用方据此不写回），并在本进程内放弃该上限。"""
    fake = _FakeCostStream(finish_reason="length")
    mw = _mw_with(fake, cost_control=True, max_output_tokens=50)

    with pytest.raises(RuntimeError, match="截断"):
        await mw._acompact([HumanMessage(content="hi")])

    # 自愈：下一轮不再 bind 上限，避免每回合都用过小的上限白烧一次调用
    await mw._acompact([HumanMessage(content="hi")])
    assert fake.bind_kwargs == [{"max_tokens": 50}]
    assert mw._cost_cap_bypassed is True


async def test_truncation_detected_by_output_tokens_without_finish_reason():
    """部分 provider 不返回 finish_reason：用上报的 output_tokens 兜住截断判定。"""
    fake = _FakeCostStream(output_tokens=50)
    mw = _mw_with(fake, cost_control=True, max_output_tokens=50)

    with pytest.raises(RuntimeError, match="截断"):
        await mw._acompact([HumanMessage(content="hi")])


async def test_usage_is_logged_for_cost_audit(caplog):
    """压缩调用用量必须可审计（开关开或不开都记录）。"""
    mw = _mw_with(_FakeCostStream(output_tokens=7), cost_control=False)

    with caplog.at_level("INFO", logger="liorium.compact"):
        await mw._acompact([HumanMessage(content="hi")])

    messages = [record.getMessage() for record in caplog.records]
    assert any("压缩调用用量" in message for message in messages), messages


async def test_usage_is_logged_even_when_provider_reports_nothing(caplog):
    """提供商没返回 usage 时也要留一行账（"无法核算成本"）。"""
    mw = _mw_with(_FakeCostStream(output_tokens=0), cost_control=False)

    with caplog.at_level("INFO", logger="liorium.compact"):
        await mw._acompact([HumanMessage(content="hi")])

    messages = [record.getMessage() for record in caplog.records]
    assert any("无法核算成本" in message for message in messages), messages


# ── 配置默认值必须与冻结表一致（原型第 6 条的 Liorium 等价物） ──


def test_cost_control_defaults_match_the_frozen_table():
    """五个键的默认值逐字照抄 §9.1 表；且能直接映射到构造参数。"""
    limits = Limits()
    assert limits.compact_cost_control is False
    assert limits.compact_max_input_tokens == 0
    assert limits.compact_max_output_tokens == 4000
    assert limits.compact_threshold_ratio == 0.8
    assert limits.compact_keep_rounds == 3

    fake = _FakeCostStream()
    mw = CompactMiddleware(
        model=fake,
        tools=[],
        context_window=limits.context_window,
        threshold_ratio=limits.compact_threshold_ratio,
        keep_rounds=limits.compact_keep_rounds,
        max_output_tokens=limits.compact_max_output_tokens,
        cost_control=limits.compact_cost_control,
        max_input_tokens=limits.compact_max_input_tokens,
    )
    assert mw.cost_control is False
    assert mw.max_input_tokens == 0
    assert mw.max_output_tokens == 4000
    assert mw.threshold_ratio == 0.8
    assert mw.keep_rounds == 3
    # 与默认值类型一致（bool / int / float），避免宿主侧把字符串直接塞进来
    assert isinstance(mw.cost_control, bool)
    assert isinstance(mw.max_input_tokens, int)
    assert isinstance(mw.max_output_tokens, int)
    assert isinstance(mw.threshold_ratio, float)
