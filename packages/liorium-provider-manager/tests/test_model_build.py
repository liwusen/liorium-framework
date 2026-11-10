"""按模型构建四种 API 格式的 Chat 模型（参数、头部、思考、关闭）。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from liorium.provider import ModelProvider
from liorium.provider.formats import build_chat_model, close_chat_model
from liorium.provider.thinking import ReasoningChatOpenAI

MODELS = ("chat-model", "claude-model", "gemini-model", "gpt-model")


def _provider(**overrides) -> ModelProvider:
    provider = ModelProvider(
        name="GW",
        base_url="https://gw.example.com/v1",
        key="sk-test",
        models=list(MODELS),
    )
    provider.model_catalog_routes = {
        "chat-model": {
            "format": "openai_chat",
            "base_url": "https://gw.example.com/v1",
            "host_matched": True,
        },
        "claude-model": {
            "format": "anthropic_messages",
            "base_url": "https://gw.example.com/anthropic",
            "host_matched": True,
        },
        "gemini-model": {
            "format": "google_gemini",
            "base_url": "https://gw.example.com/google",
            "host_matched": True,
        },
        "gpt-model": {
            "format": "openai_responses",
            "base_url": "https://gw.example.com/v1",
            "host_matched": True,
        },
    }
    for key, value in overrides.items():
        setattr(provider, key, value)
    return provider


def _build(provider: ModelProvider, model: str, intensity=None, purpose="chat", **kwargs):
    return build_chat_model(
        provider, model, intensity=intensity, purpose=purpose, **kwargs
    )


# ── 四种格式的类与关键参数 ──


def test_builds_openai_chat():
    model = _build(_provider(), "chat-model")

    assert isinstance(model, ChatOpenAI)
    assert not isinstance(model, ReasoningChatOpenAI)
    assert str(model.openai_api_base) == "https://gw.example.com/v1"
    assert model.stream_usage is True


def test_builds_openai_responses_stateless():
    model = _build(_provider(), "gpt-model")

    assert isinstance(model, ChatOpenAI)
    assert model.use_responses_api is True
    assert model.output_version == "responses/v1"
    assert model.store is False
    assert model.use_previous_response_id is False


def test_builds_anthropic_with_explicit_max_tokens():
    model = _build(_provider(), "claude-model")

    assert isinstance(model, ChatAnthropic)
    assert model.max_tokens == 8192  # 未配置上限时的安全默认
    assert str(model._client.base_url).rstrip("/") == "https://gw.example.com/anthropic"
    assert model.stream_usage is True


def test_builds_gemini_with_output_cap():
    model = _build(_provider(), "gemini-model")

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.max_output_tokens == 8192
    assert str(model.base_url).rstrip("/") == "https://gw.example.com/google"


# ── Anthropic max_tokens 规则 ──


def test_anthropic_max_tokens_uses_model_cap():
    provider = _provider()
    provider.model_max_output_tokens = {"claude-model": 32000}

    assert _build(provider, "claude-model").max_tokens == 32000


def test_anthropic_max_tokens_must_exceed_thinking_budget():
    provider = _provider()
    provider.model_max_output_tokens = {"claude-model": 32000}
    provider.thinking_type = "openai"

    model = _build(provider, "claude-model", intensity="high")

    assert model.thinking == {"type": "enabled", "budget_tokens": 8192}
    assert model.max_tokens == 8192 + 1024


def test_anthropic_compact_uses_bounded_summary_cap():
    provider = _provider()
    provider.model_max_output_tokens = {"claude-model": 32000}

    model = _build(provider, "claude-model", purpose="compact")
    assert model.max_tokens == 8192

    provider.model_max_output_tokens = {"claude-model": 2000}
    model = _build(provider, "claude-model", purpose="compact")
    # 摘要至少要有 4096，否则会被截断成半句话
    assert model.max_tokens == 4096


# ── 思考参数按格式翻译 ──


def test_chat_thinking_returns_reasoning_subclass():
    provider = _provider()
    provider.thinking_type = "qwen"

    model = _build(provider, "chat-model", intensity="medium")

    assert isinstance(model, ReasoningChatOpenAI)


def test_responses_thinking_uses_reasoning_effort():
    provider = _provider()
    provider.thinking_type = "openai"

    model = _build(provider, "gpt-model", intensity="low")

    assert model.reasoning == {"effort": "low"}


def test_gemini_thinking_uses_budget_and_thoughts():
    provider = _provider()
    provider.thinking_type = "openai"

    model = _build(provider, "gemini-model", intensity="medium")

    assert model.thinking_budget == 4096
    assert model.include_thoughts is True


def test_gemini_effort_capability_uses_reasoning_effort():
    """models.dev 标注 effort 时改发 thinking level（Gemini 3/4 不接受 budget）。"""
    provider = _provider()
    provider.thinking_type = "openai"
    provider.model_catalog_routes["gemini-model"]["thinking"] = {
        "reasoning": True,
        "options": [{"type": "effort", "values": ["low", "medium", "high"]}],
    }

    model = _build(provider, "gemini-model", intensity="medium")

    assert model.reasoning_effort == "medium"
    assert model.thinking_budget is None
    assert model.include_thoughts is True


def test_manual_thinking_mode_overrides_catalog():
    provider = _provider()
    provider.thinking_type = "openai"
    provider.model_catalog_routes["gemini-model"]["thinking"] = {
        "reasoning": True,
        "options": [{"type": "effort", "values": ["low", "medium", "high"]}],
    }
    provider.model_thinking_modes = {"gemini-model": "budget"}

    model = _build(provider, "gemini-model", intensity="medium")

    assert model.thinking_budget == 4096
    assert model.reasoning_effort is None


def test_models_api_reasoning_false_disables_thinking():
    provider = _provider()
    provider.thinking_type = "openai"
    provider.model_thinking_caps = {"gemini-model": {"reasoning": False, "options": []}}

    model = _build(provider, "gemini-model", intensity="high")

    assert model.thinking_budget is None
    assert model.reasoning_effort is None
    assert model.include_thoughts is None


def test_thinking_disabled_by_provider_preset_none():
    provider = _provider()
    provider.thinking_type = "none"

    chat = _build(provider, "chat-model", intensity="high")
    anthropic = _build(provider, "claude-model", intensity="high")

    assert not isinstance(chat, ReasoningChatOpenAI)
    assert anthropic.thinking is None


def test_thinking_disabled_when_catalog_says_no_reasoning():
    provider = _provider()
    provider.thinking_type = "openai"
    provider.model_catalog_routes["claude-model"]["reasoning"] = False

    model = _build(provider, "claude-model", intensity="high")

    assert model.thinking is None


def test_purpose_compact_disables_thinking():
    provider = _provider()
    provider.thinking_type = "openai"

    model = _build(provider, "gpt-model", intensity="high", purpose="compact")

    assert model.reasoning is None


def test_purpose_aux_disables_thinking():
    provider = _provider()
    provider.thinking_type = "qwen"

    model = _build(provider, "chat-model", intensity="high", purpose="aux")

    assert not isinstance(model, ReasoningChatOpenAI)


def test_temperature_omitted_when_thinking_enabled():
    provider = _provider()
    provider.thinking_type = "openai"

    chat = _build(provider, "chat-model", intensity="medium", temperature=0.3)
    responses = _build(provider, "gpt-model", intensity="medium", temperature=0.3)
    anthropic = _build(provider, "claude-model", intensity="medium", temperature=0.3)
    gemini = _build(provider, "gemini-model", intensity="medium", temperature=0.3)

    assert chat.temperature is None
    assert responses.temperature is None
    assert anthropic.temperature is None
    # Gemini SDK 的默认 temperature 是 0.7；这里只保证我们没有把 0.3 传进去
    assert gemini.temperature != 0.3


def test_temperature_kept_when_thinking_off():
    provider = _provider()

    model = _build(provider, "chat-model", intensity=None, temperature=0.3)
    assert model.temperature == 0.3


# ── 头部注入 ──


def test_user_agent_injected_for_all_formats():
    provider = _provider(opencode_go=False)

    chat = _build(provider, "chat-model")
    responses = _build(provider, "gpt-model")
    anthropic = _build(provider, "claude-model")
    gemini = _build(provider, "gemini-model")

    expected = "liorium-provider-manager/0.1 (langchain)"
    assert chat.default_headers["User-Agent"] == expected
    assert responses.default_headers["User-Agent"] == expected
    assert anthropic.default_headers["User-Agent"] == expected
    assert gemini.additional_headers["User-Agent"] == expected


def test_opencode_session_header_injected_for_all_formats():
    provider = _provider(opencode_go=True)

    chat = _build(provider, "chat-model")
    responses = _build(provider, "gpt-model")
    anthropic = _build(provider, "claude-model")
    gemini = _build(provider, "gemini-model")

    assert chat.default_headers.get("x-opencode-session")
    assert responses.default_headers.get("x-opencode-session")
    assert anthropic.default_headers.get("x-opencode-session")
    assert gemini.additional_headers.get("x-opencode-session")


# ── stream_usage 三态 ──


def test_stream_usage_can_be_disabled():
    provider = _provider(stream_usage=False)

    assert _build(provider, "chat-model").stream_usage is False
    assert _build(provider, "claude-model").stream_usage is False


def test_stream_usage_can_be_forced_on():
    provider = _provider(stream_usage=True)

    assert _build(provider, "chat-model").stream_usage is True


# ── 错误路径 ──


def test_invalid_format_base_url_raises():
    provider = _provider(format_base_urls={"anthropic_messages": "nope"})

    with pytest.raises(ValueError):
        _build(provider, "claude-model")


# ── 关闭客户端 ──


def test_close_chat_model_does_not_close_shared_default_client():
    """关一个模型不得关掉 langchain 的进程级共享客户端。"""
    provider = _provider()

    async def _run():
        first = build_chat_model(provider, "chat-model")
        second = build_chat_model(provider, "chat-model")
        # 共用同一个底层客户端是这件事成立的前提（访问私有属性：测的就是这层共享）
        assert first.root_async_client._client is second.root_async_client._client

        await close_chat_model(first)
        assert second.root_async_client.is_closed() is False

        for model in ("gpt-model", "claude-model", "gemini-model"):
            await close_chat_model(build_chat_model(provider, model))
        await close_chat_model(None)

    asyncio.run(_run())
