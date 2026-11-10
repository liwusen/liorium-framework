"""会话压缩在四种 API 格式下的行为：用量口径、截断判定、工具前缀、写回。

逐条移植 ``docs/design/compact.md`` §16.2 的 18 个测试（其中 ``test_is_truncated``
是 7 组参数化），外加两条：``api_format`` 缺失时的用量口径反推、模块级
``is_truncated`` 与中间件静态方法一致。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_core.tools import tool

from liorium.agent.runtime import usage as usage_mod
from liorium.agent.runtime.middleware.compact import CompactMiddleware, _reported_tokens


@tool
def read_file(path: str, limit: int = 100) -> str:
    """读取一个文件。"""
    return f"real:{path}"


# ── 用量口径 ──


def _ai(
    content: str = "ok",
    *,
    usage: dict | None = None,
    raw_usage: dict | None = None,
) -> AIMessage:
    message = AIMessage(content=content)
    if usage is not None:
        message.usage_metadata = usage
    if raw_usage is not None:
        message.response_metadata = {"usage": raw_usage}
    return message


def test_anthropic_context_includes_cache_read_and_creation():
    """Anthropic：缓存读写必须计入 input（I3）。"""
    message = _ai(
        raw_usage={
            "input_tokens": 100,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "output_tokens": 20,
        }
    )

    snapshot = usage_mod.normalize_usage(message, "anthropic_messages")

    assert snapshot is not None
    assert snapshot.input_tokens == 1050
    assert snapshot.cache_read_tokens == 900
    assert snapshot.cache_creation_tokens == 50
    assert snapshot.context_tokens == 1070


def test_anthropic_does_not_double_count_when_sdk_already_folded_cache():
    """新版 SDK 已把缓存折进 ``usage_metadata.input_tokens`` ⇒ 取 ``max()``，不相加。"""
    message = _ai(
        usage={
            "input_tokens": 1050,
            "output_tokens": 20,
            "total_tokens": 1070,
            "input_token_details": {"cache_read": 900, "cache_creation": 50},
        },
        raw_usage={
            "input_tokens": 100,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "output_tokens": 20,
        },
    )

    snapshot = usage_mod.normalize_usage(message, "anthropic_messages")

    assert snapshot is not None
    assert snapshot.context_tokens == 1070


def test_openai_cached_tokens_are_subset_not_additive():
    """OpenAI：``cached`` 是 input 的子集，重复相加会虚高（1900 是错的）。"""
    message = _ai(
        usage={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 800},
        }
    )

    snapshot = usage_mod.normalize_usage(message, "openai_chat")

    assert snapshot is not None
    assert snapshot.context_tokens == 1100
    assert snapshot.cache_read_tokens == 800


def test_gemini_cached_tokens_are_subset_not_additive():
    """Gemini：同样不相加；reasoning 单独记。"""
    message = _ai(
        usage={
            "input_tokens": 5000,
            "output_tokens": 200,
            "total_tokens": 5200,
            "input_token_details": {"cache_read": 4000},
            "output_token_details": {"reasoning": 120},
        }
    )

    snapshot = usage_mod.normalize_usage(message, "google_gemini")

    assert snapshot is not None
    assert snapshot.context_tokens == 5200
    assert snapshot.reasoning_tokens == 120


def test_usage_without_any_report_returns_none():
    """无上报值 ⇒ ``None``（调用方据此不压缩）。"""
    assert usage_mod.normalize_usage(AIMessage(content="x"), "openai_chat") is None
    assert usage_mod.last_usage([HumanMessage(content="x")]) is None


def test_usage_total_falls_back_to_input_plus_output():
    """``total_tokens == 0`` ⇒ 退化到 ``input + output``。"""
    message = _ai(usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 0})

    snapshot = usage_mod.normalize_usage(message, "openai_chat")

    assert snapshot is not None
    assert snapshot.context_tokens == 15


def test_reported_tokens_uses_latest_reporting_message():
    """跳过末尾无 usage 的本地 AIMessage，取最近一条真实上报值（I2）。"""
    messages = [
        HumanMessage(content="q"),
        _ai("old", usage={"input_tokens": 100, "output_tokens": 1, "total_tokens": 101}),
        HumanMessage(content="q2"),
        AIMessage(content="本地构造，无 usage"),
        _ai(usage={"input_tokens": 10, "output_tokens": 100, "total_tokens": 110}),
    ]

    assert _reported_tokens(messages) == 110


def test_reported_tokens_anthropic_reads_raw_usage():
    """Anthropic 只给了 ``response_metadata["usage"]`` 时也能算对（10+990+100）。"""
    messages = [
        HumanMessage(content="q"),
        _ai(
            raw_usage={
                "input_tokens": 10,
                "cache_read_input_tokens": 990,
                "output_tokens": 100,
            }
        ),
    ]

    assert _reported_tokens(messages, "anthropic_messages") == 1100


def test_normalize_usage_infers_anthropic_without_api_format():
    """``api_format`` 为 None 时按上报字段形态反推 Anthropic 口径。"""
    message = _ai(raw_usage={"input_tokens": 100, "cache_read_input_tokens": 900,
                             "output_tokens": 20})

    snapshot = usage_mod.normalize_usage(message)

    assert snapshot is not None
    assert snapshot.context_tokens == 1020


def test_usage_fields_are_the_frozen_contract():
    """``Usage`` 的字段集是压缩与前端共用的契约，缺一个就会静默丢数据。"""
    import dataclasses

    names = {field.name for field in dataclasses.fields(usage_mod.Usage)}
    assert names == {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "total_tokens",
        "context_tokens",
    }


def test_reported_tokens_aliases_context_tokens():
    """``reported_tokens`` 与 ``context_tokens`` 是同一个判据（§6.1 的两种叫法）。"""
    messages = [
        HumanMessage(content="q"),
        _ai(usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    ]
    assert usage_mod.reported_tokens(messages) == 10
    assert usage_mod.context_tokens(messages) == 10
    assert _reported_tokens(messages) == 10


# ── 截断判定（7 组参数） ──


@pytest.mark.parametrize(
    ("max_out", "reason", "usage", "expected"),
    [
        (100, "length", None, True),
        (100, "max_tokens", None, True),
        (100, "stop", None, False),
        (100, None, {"output_tokens": 100}, True),
        (100, None, {"output_tokens": 99}, False),
        (0, "length", None, False),
        (100, "end_turn", {"output_tokens": 5}, False),
    ],
)
def test_is_truncated(max_out, reason, usage, expected):
    """§8.5 的 7 组断言；模块级函数与中间件静态方法必须一致。"""
    assert usage_mod.is_truncated(max_out, reason, usage) is expected
    assert CompactMiddleware._is_truncated(max_out, reason, usage) is expected


# ── 写回：多条 System 合并 ──


def _middleware(**kwargs) -> CompactMiddleware:
    fake = _FakeModel()
    return CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        **kwargs,
    )


def test_build_replacement_merges_all_system_messages():
    """多条 System 合并为 1 条（Gemini/Anthropic 只认一个 system），标记只出现一次。"""
    mw = _middleware()
    messages = [
        SystemMessage(content="主提示词"),
        SystemMessage(content="角色设定"),
        HumanMessage(content="q"),
        AIMessage(content="a"),
        HumanMessage(content="q2"),
        AIMessage(content="a2"),
    ]

    replacement = mw._build_replacement(messages, "摘要正文")

    assert len(replacement) == 1
    merged = replacement[0]
    assert isinstance(merged, SystemMessage)
    assert "主提示词" in merged.content
    assert "角色设定" in merged.content
    assert "摘要正文" in merged.content
    assert merged.content.count("liorium:compact-summary:start") == 1


def test_build_replacement_without_system_uses_human_message():
    """无 System ⇒ HumanMessage + ``lc_source="liorium_compact"`` 幂等标记。"""
    mw = _middleware()
    replacement = mw._build_replacement([HumanMessage(content="q")], "摘要")

    assert len(replacement) == 1
    assert isinstance(replacement[0], HumanMessage)
    assert replacement[0].additional_kwargs["lc_source"] == "liorium_compact"


# ── 压缩调用：工具前缀与 tool_choice ──


class _FakeModel:
    """记录 ``bind_tools`` / ``bind`` 入参，``ainvoke`` 产出可控的摘要与结束原因。"""

    _llm_type = "fake"
    profile: dict = {}

    def __init__(
        self,
        *,
        summary: str = "摘要正文",
        stop_reason: str | None = None,
        finish_reason: str | None = None,
        accept_tool_choice: bool = True,
        output_tokens: int = 5,
    ) -> None:
        self.summary = summary
        self.stop_reason = stop_reason
        self.finish_reason = finish_reason
        self.accept_tool_choice = accept_tool_choice
        self.output_tokens = output_tokens
        self.bind_tools_calls: list[tuple[list, object]] = []
        self.bind_kwargs: list[dict] = []

    def _clone(self) -> "_FakeModel":
        clone = _FakeModel(
            summary=self.summary,
            stop_reason=self.stop_reason,
            finish_reason=self.finish_reason,
            accept_tool_choice=self.accept_tool_choice,
            output_tokens=self.output_tokens,
        )
        clone.bind_tools_calls = self.bind_tools_calls
        clone.bind_kwargs = self.bind_kwargs
        return clone

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        if kwargs and not self.accept_tool_choice:
            raise TypeError("tool_choice unsupported")
        self.bind_tools_calls.append((list(tools), kwargs.get("tool_choice")))
        return self._clone()

    def bind(self, **kwargs):  # noqa: ANN003
        self.bind_kwargs.append(dict(kwargs))
        return self._clone()

    def with_retry(self, **_kwargs):
        return self

    async def ainvoke(self, payload, config=None):  # noqa: ANN001
        meta: dict = {}
        if self.stop_reason:
            meta["stop_reason"] = self.stop_reason
        if self.finish_reason:
            meta["finish_reason"] = self.finish_reason
        return AIMessage(
            content=self.summary,
            response_metadata=meta,
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": self.output_tokens,
                "total_tokens": 10 + self.output_tokens,
            },
        )


async def test_compact_binds_server_tools_after_mocks_with_anthropic_none():
    """Anthropic 必须用 ``{"type": "none"}``；mock 客户端工具在前、服务端 specs 在后。"""
    fake = _FakeModel()
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        api_format="anthropic_messages",
        server_tool_specs=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    await mw._acompact([HumanMessage(content="q")])

    tools, tool_choice = fake.bind_tools_calls[-1]
    assert tool_choice == {"type": "none"}
    assert tools[0].name == "read_file"
    assert tools[-1]["name"] == "web_search"


async def test_compact_uses_string_none_for_openai():
    """OpenAI 风格用字符串 ``"none"``。"""
    fake = _FakeModel()
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        api_format="openai_chat",
    )

    await mw._acompact([HumanMessage(content="q")])

    _tools, tool_choice = fake.bind_tools_calls[-1]
    assert tool_choice == "none"


async def test_compact_uses_string_none_when_api_format_missing():
    """``api_format=None`` ⇒ 按 OpenAI 风格处理（不依赖任何格式推断器）。"""
    fake = _FakeModel()
    mw = CompactMiddleware(
        model=fake, tools=[read_file], context_window=1000, keep_rounds=1, api_format=None
    )

    await mw._acompact([HumanMessage(content="q")])

    _tools, tool_choice = fake.bind_tools_calls[-1]
    assert tool_choice == "none"


async def test_compact_falls_back_when_tool_choice_unsupported():
    """不支持 ``tool_choice`` ⇒ 降级为无 tool_choice，且只告警一次。"""
    fake = _FakeModel(accept_tool_choice=False)
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        api_format="openai_chat",
    )

    await mw._acompact([HumanMessage(content="q")])

    assert mw._tool_choice_warned is True
    _tools, tool_choice = fake.bind_tools_calls[-1]
    assert tool_choice is None


async def test_compact_raises_on_truncated_summary():
    """模型自身 ``max_tokens`` 截断摘要 ⇒ 抛错且不触发成本上限自愈。"""
    fake = _FakeModel(stop_reason="max_tokens")
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        api_format="anthropic_messages",
        summary_max_tokens=4096,
    )

    with pytest.raises(RuntimeError) as excinfo:
        await mw._acompact([HumanMessage(content="q")])

    assert "4096" in str(excinfo.value)
    assert mw._cost_cap_bypassed is False


async def test_compact_cost_cap_truncation_bypasses_cap():
    """成本上限导致的截断：抛错 + 自愈（``_cost_cap_bypassed=True``）。"""
    fake = _FakeModel(finish_reason="length")
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        keep_rounds=1,
        api_format="openai_chat",
        cost_control=True,
        max_output_tokens=500,
    )

    with pytest.raises(RuntimeError):
        await mw._acompact([HumanMessage(content="q")])

    assert mw._cost_cap_bypassed is True
    assert fake.bind_kwargs[-1]["max_tokens"] == 500


# ── abefore_model：压缩后写回 ──


async def test_abefore_model_writes_merged_state():
    """两条 System 合并成一条写回，保留段跟在头部之后。"""
    fake = _FakeModel(summary="## 目标与进度\n做完了")
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        threshold_ratio=0.5,
        keep_rounds=1,
    )
    messages = [
        SystemMessage(content="sys-a"),
        SystemMessage(content="sys-b"),
        HumanMessage(content="q1"),
        _ai("a1", usage={"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}),
        HumanMessage(content="q2"),
        _ai("a2", usage={"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}),
    ]

    result = await mw.abefore_model({"messages": messages}, None)

    assert result is not None
    updated = result["messages"]
    assert isinstance(updated[0], RemoveMessage)
    replacement = updated[1]
    assert isinstance(replacement, SystemMessage)
    assert "sys-a" in replacement.content and "sys-b" in replacement.content
    assert "做完了" in replacement.content


async def test_abefore_model_skips_without_usage():
    """无 usage ⇒ None（即使已超阈值）。"""
    fake = _FakeModel()
    mw = CompactMiddleware(
        model=fake,
        tools=[read_file],
        context_window=1000,
        threshold_ratio=0.5,
        keep_rounds=1,
    )
    messages = [HumanMessage(content="q"), AIMessage(content="a")]

    assert await mw.abefore_model({"messages": messages}, None) is None
