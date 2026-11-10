"""CLI 的 provider 加载 / 解析 / 模型工厂（不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.cli.providers import (
    ProviderConfigError,
    ProviderRegistry,
    make_chat_model,
    normalize_format,
    prompt_for_registry,
    save_registry,
)

AGENT_STYLE = {
    "main_model": "Go::deepseek-v4.1-flash",
    "providers": [
        {
            "name": "Go",
            "base_url": "https://opencode.ai/zen/go/v1",
            "key": "oc_sk_test_key",
            "models": ["deepseek-v4.1-flash", "glm-5.2"],
            "model_context_lengths": {"deepseek-v4.1-flash": 1_048_576, "glm-5.2": 1_000_000},
            "model_api_formats": {"deepseek-v4.1-flash": "openai_responses"},
            "model_catalog_routes": {
                "deepseek-v4.1-flash": {
                    "format": "openai_chat",
                    "base_url": "https://opencode.ai/inference/go/openai/v1",
                },
                "glm-5.2": {"format": "openai_chat"},
            },
            "opencode_go": True,
        },
        {
            "name": "claude",
            "base_url": "https://api.anthropic.com",
            "key": "sk-ant-test",
            "models": ["claude-sonnet-4-5"],
            "model_context_lengths": {"claude-sonnet-4-5": 200_000},
        },
        {
            "name": "gemini",
            "base_url": "https://generativelanguage.googleapis.com",
            "key": "AIza-test",
            "models": ["gemini-2.5-pro"],
        },
    ],
}


def write_config(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(payload or AGENT_STYLE), encoding="utf-8")
    return path


def test_normalize_format_aliases() -> None:
    assert normalize_format("openai") == "openai_chat"
    assert normalize_format("OpenAI-Chat") == "openai_chat"
    assert normalize_format("anthropic") == "anthropic_messages"
    assert normalize_format("gemini") == "google_gemini"
    assert normalize_format(None) is None
    assert normalize_format("nope") is None


def test_load_and_resolve_manual_format_wins(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("Go::deepseek-v4.1-flash")
    # 既有语义：model_api_formats 手动覆盖优先于目录 route
    assert resolved.api_format == "openai_responses"
    assert resolved.format_source == "manual"
    assert resolved.base_url == "https://opencode.ai/zen/go/v1"
    assert resolved.context_window == 1_048_576
    assert resolved.label == "Go::deepseek-v4.1-flash"
    assert "1,048,576" in resolved.describe()


def test_resolve_by_model_name_only(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.resolve("glm-5.2").label == "Go::glm-5.2"
    with pytest.raises(ProviderConfigError):
        registry.resolve("nope-model")


def test_ambiguous_model_requires_provider(tmp_path: Path) -> None:
    payload = {
        "providers": [
            {"name": "a", "base_url": "https://a", "key": "k", "models": ["m"]},
            {"name": "b", "base_url": "https://b", "key": "k", "models": ["m"]},
        ]
    }
    registry = ProviderRegistry.load(write_config(tmp_path, payload))
    with pytest.raises(ProviderConfigError) as info:
        registry.resolve("m")
    assert "Provider::model" in str(info.value)
    assert registry.resolve("b::m").provider.name == "b"


def test_format_inference_and_defaults(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.resolve("claude::claude-sonnet-4-5").api_format == "anthropic_messages"
    assert registry.resolve("gemini::gemini-2.5-pro").api_format == "google_gemini"
    # 没有 context 长度时给 fallback
    assert registry.resolve("gemini::gemini-2.5-pro").context_window == 128_000
    assert registry.resolve("gemini::gemini-2.5-pro", fallback_window=4096).context_window == 4096


def test_default_label_and_labels(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.default_label() == "Go::deepseek-v4.1-flash"
    assert len(registry.labels()) == 4


def test_default_label_falls_back_when_main_model_invalid(tmp_path: Path) -> None:
    payload = dict(AGENT_STYLE)
    payload["main_model"] = "Ghost::none"
    registry = ProviderRegistry.load(write_config(tmp_path, payload))
    assert registry.default_label() == "Go::deepseek-v4.1-flash"


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError) as info:
        ProviderRegistry.load(tmp_path / "missing.json")
    assert "不存在" in str(info.value)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as info:
        ProviderRegistry.load(broken)
    assert "JSON" in str(info.value)

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(ProviderConfigError):
        ProviderRegistry.load(empty)


def test_make_chat_model_per_format(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(write_config(tmp_path))

    # deepseek 手动覆盖成 Responses；端点用用户 base_url（目录 route 不是 host 匹配）
    responses_model = make_chat_model(registry.resolve("Go::deepseek-v4.1-flash"))
    from langchain_openai import ChatOpenAI

    assert isinstance(responses_model, ChatOpenAI)
    assert responses_model.use_responses_api is True
    assert str(responses_model.openai_api_base).startswith("https://opencode.ai/zen/go/v1")
    assert responses_model.default_headers.get("x-opencode-session")

    anthropic_model = make_chat_model(registry.resolve("claude::claude-sonnet-4-5"))
    from langchain_anthropic import ChatAnthropic

    assert isinstance(anthropic_model, ChatAnthropic)

    google_model = make_chat_model(registry.resolve("gemini::gemini-2.5-pro"))
    from langchain_google_genai import ChatGoogleGenerativeAI

    assert isinstance(google_model, ChatGoogleGenerativeAI)
    assert str(google_model.base_url).startswith("https://generativelanguage")


def test_make_chat_model_requires_key(tmp_path: Path) -> None:
    payload = {
        "providers": [{"name": "p", "base_url": "https://x", "key": "", "models": ["m"]}]
    }
    registry = ProviderRegistry.load(write_config(tmp_path, payload))
    with pytest.raises(ProviderConfigError) as info:
        make_chat_model(registry.resolve("p::m"))
    assert "API key" in str(info.value)


def test_wizard_and_save_roundtrip(tmp_path: Path) -> None:
    answers = iter(
        [
            "https://api.example.com",  # base_url
            "sk-test",                  # key
            "demo-model",               # model
            "2",                        # 格式 = openai_responses
            "32000",                    # context window
            "deepseek",                 # 思考方言
        ]
    )
    registry = prompt_for_registry(input_fn=lambda _prompt: next(answers), print_fn=lambda _m: None)
    assert registry.main_model == "custom::demo-model"
    resolved = registry.resolve("custom::demo-model")
    assert resolved.api_format == "openai_responses"
    assert resolved.context_window == 32_000
    assert resolved.base_url == "https://api.example.com"
    assert resolved.provider.thinking_type == "deepseek"

    target = tmp_path / "saved" / "providers.json"
    saved = save_registry(registry, target)
    assert saved == target
    reloaded = ProviderRegistry.load(target)
    assert reloaded.resolve("custom::demo-model").api_format == "openai_responses"


def test_wizard_accepts_defaults() -> None:
    answers = iter(["", "sk-test", "demo", "", "", ""])
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    registry = prompt_for_registry(input_fn=fake_input, print_fn=lambda _m: None)
    resolved = registry.resolve("custom::demo")
    assert resolved.base_url == "https://api.deepseek.com"
    assert resolved.api_format == "openai_chat"
    assert resolved.context_window == 128_000
    assert resolved.provider.thinking_type == "openai"
    assert any("base_url" in prompt for prompt in prompts)


def test_load_registry_finds_first_candidate(tmp_path: Path) -> None:
    from liorium.cli.providers import load_registry

    good = write_config(tmp_path)
    registry, tried = load_registry(None, candidates=[tmp_path / "missing.json", good])
    assert registry is not None
    assert registry.source == good
    assert len(tried) == 2

    registry, tried = load_registry(None, candidates=[tmp_path / "a.json"])
    assert registry is None
    assert tried == [tmp_path / "a.json"]
