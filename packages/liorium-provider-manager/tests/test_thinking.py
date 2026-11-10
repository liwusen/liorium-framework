"""思考预设、按格式参数与 ReasoningChatOpenAI。"""

from __future__ import annotations

import pytest
from langchain_core.messages.ai import AIMessageChunk

from liorium.provider import ModelProvider
from liorium.provider.thinking import (
    THINKING_PRESETS,
    ReasoningChatOpenAI,
    get_format_thinking_params,
    get_thinking_params,
    normalize_intensity,
    resolve_thinking_dialect,
)
from liorium.provider.thinking_caps import (
    MODE_BUDGET,
    MODE_EFFORT,
    MODE_TOGGLE,
    ThinkingOption,
)


# ── 强度 / 方言 ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("off", None),
        ("none", None),
        ("low", "low"),
        ("MEDIUM", "medium"),
        ("high", "high"),
    ],
)
def test_normalize_intensity(raw, expected):
    assert normalize_intensity(raw) == expected


def test_normalize_intensity_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_intensity("turbo")


@pytest.mark.parametrize(
    ("name", "model", "thinking_type", "expected"),
    [
        ("Go", "deepseek-v4.1-flash", "auto", "deepseek"),
        ("Qwen", "qwen-max", "auto", "qwen"),
        ("GW", "glm-5", "auto", "glm"),
        ("GW", "mimo-7b", "auto", "mimo"),
        ("GW", "abab-6", "auto", "minimax"),
        ("GW", "gpt-4o", "auto", "openai"),
        ("GW", "claude-sonnet", "none", "none"),
        ("GW", "anything", "glm", "glm"),
    ],
)
def test_resolve_thinking_dialect(name, model, thinking_type, expected):
    provider = ModelProvider(name=name, base_url="https://x", thinking_type=thinking_type)
    assert resolve_thinking_dialect(provider, model) == expected


# ── 预设 ──


def test_preset_lookup_and_fallback():
    assert get_thinking_params("openai", "high") == {"reasoning_effort": "high"}
    assert get_thinking_params("unknown", "high") == {}
    assert "extra_body" in get_thinking_params("deepseek", "medium")


def test_format_params_openai_chat_uses_preset():
    assert get_format_thinking_params("openai_chat", "openai", "low") == {
        "reasoning_effort": "low"
    }
    assert get_format_thinking_params("openai_chat", "none", "high") == {}


def test_responses_maps_effort_to_supported_level():
    option = ThinkingOption(MODE_EFFORT, levels=("low", "high", "max"))
    params = get_format_thinking_params(
        "openai_responses", "openai", "medium", option=option
    )
    assert params == {"reasoning": {"effort": "high"}}


def test_anthropic_budget_is_clamped_to_minimum():
    option = ThinkingOption(MODE_BUDGET, max_budget=512)
    params = get_format_thinking_params(
        "anthropic_messages", "openai", "low", option=option
    )
    assert params == {"thinking": {"type": "enabled", "budget_tokens": 1024}}


def test_gemini_effort_option_replaces_budget():
    option = ThinkingOption(MODE_EFFORT, levels=("low", "medium", "high"))
    params = get_format_thinking_params(
        "google_gemini", "openai", "medium", option=option
    )
    assert params == {"reasoning_effort": "medium", "include_thoughts": True}


def test_gemini_budget_option_is_clamped():
    option = ThinkingOption(MODE_BUDGET, max_budget=2048)
    params = get_format_thinking_params(
        "google_gemini", "openai", "high", option=option
    )
    assert params == {"thinking_budget": 2048, "include_thoughts": True}


def test_gemini_toggle_option_sends_thoughts_only():
    option = ThinkingOption(MODE_TOGGLE)
    params = get_format_thinking_params(
        "google_gemini", "openai", "medium", option=option
    )
    assert params == {"include_thoughts": True}


def test_presets_are_independent_copies():
    params = get_thinking_params("openai", "low")
    params["reasoning_effort"] = "mutated"
    assert THINKING_PRESETS["openai"]["low"]["reasoning_effort"] == "low"


# ── ReasoningChatOpenAI ──


class TestReasoningChatOpenAI:
    default_chunk_class = AIMessageChunk

    def _llm(self) -> ReasoningChatOpenAI:
        return ReasoningChatOpenAI(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://test.api/v1",
        )

    def test_reasoning_content_in_additional_kwargs(self):
        raw_chunk = {
            "choices": [
                {"delta": {"content": None, "reasoning_content": "I think step by step..."}}
            ],
            "object": "chat.completion.chunk",
        }
        gc = self._llm()._convert_chunk_to_generation_chunk(
            raw_chunk, self.default_chunk_class, None
        )
        assert gc is not None
        message = gc.message
        assert isinstance(message, AIMessageChunk)
        assert message.additional_kwargs.get("reasoning_content") == "I think step by step..."

    def test_fallback_reasoning_keys(self):
        for key in ("reasoning", "think"):
            raw_chunk = {
                "choices": [{"delta": {"content": None, key: f"via {key}"}}],
            }
            gc = self._llm()._convert_chunk_to_generation_chunk(
                raw_chunk, self.default_chunk_class, None
            )
            assert gc is not None
            assert gc.message.additional_kwargs.get("reasoning_content") == f"via {key}"

    def test_no_reasoning_content(self):
        raw_chunk = {
            "choices": [{"delta": {"content": "hello"}}],
            "object": "chat.completion.chunk",
        }
        gc = self._llm()._convert_chunk_to_generation_chunk(
            raw_chunk, self.default_chunk_class, None
        )
        assert gc is not None
        assert "reasoning_content" not in gc.message.additional_kwargs

    def test_choices_empty_does_not_crash(self):
        gc = self._llm()._convert_chunk_to_generation_chunk(
            {"choices": []}, self.default_chunk_class, None
        )
        assert gc is not None

    def test_delta_none_does_not_crash(self):
        gc = self._llm()._convert_chunk_to_generation_chunk(
            {"choices": [{"delta": None}], "object": "chat.completion.chunk"},
            self.default_chunk_class,
            None,
        )
        assert gc is None
