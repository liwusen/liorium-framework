"""ProviderRegistry：装载、解析、格式覆盖与持久化。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.provider import ProviderRegistry
from liorium.provider.errors import ProviderConfigError

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


def test_load_and_resolve_manual_wins_over_catalog(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("Go::deepseek-v4.1-flash")
    # 既有语义：手动 model_api_formats 优先于目录 route
    assert resolved.api_format == "openai_responses"
    assert resolved.format_source == "manual"
    assert resolved.base_url == "https://opencode.ai/zen/go/v1"
    assert resolved.context_window == 1_048_576
    assert resolved.max_output_tokens == 8192
    assert resolved.label == "Go::deepseek-v4.1-flash"
    assert "1,048,576" in resolved.describe()


def test_catalog_used_when_no_manual_override(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("Go::glm-5.2")
    assert resolved.api_format == "openai_chat"
    assert resolved.format_source == "catalog"


def test_resolve_by_model_name_only(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.resolve("glm-5.2").label == "Go::glm-5.2"
    with pytest.raises(ProviderConfigError):
        registry.resolve("nope-model")


def test_ambiguous_model_requires_provider(tmp_path: Path):
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


def test_format_inference_and_defaults(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.resolve("claude::claude-sonnet-4-5").api_format == "anthropic_messages"
    assert registry.resolve("gemini::gemini-2.5-pro").api_format == "google_gemini"
    # 没有 context 长度时给 fallback
    assert registry.resolve("gemini::gemini-2.5-pro").context_window == 128_000
    assert (
        registry.resolve("gemini::gemini-2.5-pro", fallback_window=4096).context_window
        == 4096
    )


def test_default_label_and_labels(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.default_label() == "Go::deepseek-v4.1-flash"
    assert len(registry.labels()) == 4


def test_default_label_falls_back_when_main_model_invalid(tmp_path: Path):
    payload = dict(AGENT_STYLE)
    payload["main_model"] = "Ghost::none"
    registry = ProviderRegistry.load(write_config(tmp_path, payload))
    assert registry.default_label() == "Go::deepseek-v4.1-flash"


def test_api_format_override_re_resolves_endpoint(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("Go::glm-5.2", api_format="anthropic")
    assert resolved.api_format == "anthropic_messages"
    assert resolved.format_source == "manual"
    # Anthropic SDK 自己追加 /v1/messages，根地址去掉 /v1
    assert resolved.base_url == "https://opencode.ai/zen/go"

    with pytest.raises(ProviderConfigError):
        registry.resolve("Go::glm-5.2", api_format="nope")


def test_load_errors(tmp_path: Path):
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


def test_from_dict_validation_and_api_key_alias():
    with pytest.raises(ProviderConfigError) as info:
        ProviderRegistry.from_dict({"providers": [{"base_url": "https://x"}]})
    assert "name" in str(info.value)

    with pytest.raises(ProviderConfigError) as info:
        ProviderRegistry.from_dict({"providers": [{"name": "P"}]})
    assert "base_url" in str(info.value)

    registry = ProviderRegistry.from_dict(
        {
            "providers": [
                {"name": "P", "base_url": "https://x", "api_key": "sk", "models": ["m"]}
            ]
        }
    )
    assert registry.provider("P").api_key == "sk"


def test_save_roundtrip(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    target = tmp_path / "saved" / "providers.json"
    assert registry.save(target) == target
    reloaded = ProviderRegistry.load(target)
    assert reloaded.resolve("Go::deepseek-v4.1-flash").api_format == "openai_responses"
    dumped = json.loads(target.read_text(encoding="utf-8"))
    assert dumped["providers"][0]["model_catalog_routes"]


def test_make_chat_model_requires_key(tmp_path: Path):
    payload = {
        "providers": [{"name": "p", "base_url": "https://x", "key": "", "models": ["m"]}]
    }
    registry = ProviderRegistry.load(write_config(tmp_path, payload))
    with pytest.raises(ProviderConfigError) as info:
        registry.make_chat_model(registry.resolve("p::m"))
    assert "API key" in str(info.value)


def test_from_env(monkeypatch):
    for key in (
        "LIORIUM_BASE_URL",
        "LIORIUM_CHAT_KEY",
        "LIORIUM_MODEL",
        "LIORIUM_API_FORMAT",
        "LIORIUM_CONTEXT_WINDOW",
        "LIORIUM_THINKING_TYPE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert ProviderRegistry.from_env() is None

    monkeypatch.setenv("LIORIUM_BASE_URL", "https://stub.invalid/v1/")
    monkeypatch.setenv("LIORIUM_CHAT_KEY", "sk-stub")
    monkeypatch.setenv("LIORIUM_MODEL", "ignored::demo")
    monkeypatch.setenv("LIORIUM_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("LIORIUM_THINKING_TYPE", "qwen")
    registry = ProviderRegistry.from_env()
    assert registry is not None
    assert registry.main_model == "env::demo"
    resolved = registry.resolve("env::demo")
    assert resolved.base_url == "https://stub.invalid/v1"
    assert resolved.provider.api_key == "sk-stub"
    assert resolved.context_window == 32_000
    assert resolved.api_format == "openai_chat"
    assert resolved.provider.thinking_type == "qwen"


def test_openai_credentials(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.try_main_openai_credentials() == (
        "deepseek-v4.1-flash",
        "oc_sk_test_key",
        "https://opencode.ai/zen/go/v1",
    )
    assert registry.get_main_openai_credentials()[0] == "deepseek-v4.1-flash"

    registry.main_model = "claude::claude-sonnet-4-5"
    assert registry.try_main_openai_credentials() is None
    with pytest.raises(ProviderConfigError) as info:
        registry.get_main_openai_credentials()
    assert "format_base_urls" in str(info.value)


def test_get_model_api_format(tmp_path: Path):
    registry = ProviderRegistry.load(write_config(tmp_path))
    assert registry.get_model_api_format("claude::claude-sonnet-4-5") == "anthropic_messages"
    assert registry.get_model_api_format("Go::deepseek-v4.1-flash") == "openai_responses"
    assert registry.get_model_api_format("bad-spec") == "openai_chat"
