"""Provider schema、配置读写与选择辅助。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.provider.errors import ProviderConfigError
from liorium.provider.schema import (
    DEFAULT_CONTEXT_LENGTH,
    ModelProvider,
    ModelProviders,
    dumps,
    get_context_length,
    get_default_subagent_model,
    get_main_credentials,
    get_main_provider,
    is_model_spec_usable,
    is_subagent_model_allowed,
    loads,
    new_provider,
    parse_spec,
    prune_model_selection,
    remove_model_from_provider,
    remove_provider,
    resolve_provider,
)


def test_provider_defaults_and_helpers():
    provider = ModelProvider(name="P", base_url="https://x", key="sk")
    assert provider.api_key == "sk"
    assert provider.thinking_type == "auto"
    assert provider.api_format == "auto"
    assert provider.models == []
    assert provider.context_window("m", 1234) == 1234
    assert provider.context_window("m", DEFAULT_CONTEXT_LENGTH) == DEFAULT_CONTEXT_LENGTH
    assert provider.max_output_tokens("m") == 8192

    provider.model_context_lengths["m"] = 1000
    provider.model_max_output_tokens["m"] = 100
    assert provider.context_window("m") == 1000
    assert provider.max_output_tokens("m") == 100


def test_loads_dumps_roundtrip_preserves_everything(tmp_path: Path):
    raw = {
        "main_model": "P::m",
        "subagent_models": ["P::m"],
        "providers": [
            {
                "name": "P",
                "base_url": "https://x/v1",
                "key": "sk",
                "models": ["m"],
                "model_context_lengths": {"m": 1000},
                "thinking_type": "deepseek",
                "api_format": "openai_chat",
                "model_api_formats": {"m": "openai_responses"},
                "format_base_urls": {"openai_chat": "https://x/chat/v1"},
                "model_max_output_tokens": {"m": 100},
                "model_api_capabilities": {"m": {"openai_chat": {}}},
                "model_thinking_caps": {"m": {"reasoning": True, "options": []}},
                "model_thinking_modes": {"m": "effort"},
                "model_server_tools": {"m": ["web_search"]},
                "catalog_provider_id": "cat",
                "model_catalog_routes": {"m": {"format": "openai_chat"}},
                "catalog_synced_at": 123.0,
                "opencode_go": True,
                "stream_usage": False,
                "future_field": {"keep": "me"},
            }
        ],
    }
    source = tmp_path / "providers.json"
    source.write_text(json.dumps(raw), encoding="utf-8")

    providers = loads(source)
    assert providers.main_model == "P::m"
    assert providers.subagent_models == ["P::m"]
    provider = providers.providers[0]
    assert provider.future_field == {"keep": "me"}

    target = tmp_path / "saved" / "providers.json"
    dumps(providers, target)
    reloaded = loads(target)

    assert reloaded == providers
    dumped = json.loads(target.read_text(encoding="utf-8"))
    assert dumped["providers"][0]["future_field"] == {"keep": "me"}
    assert dumped["providers"][0]["key"] == "sk"
    # 原子写不残留临时文件
    assert list(target.parent.glob("*.tmp")) == []


def test_loads_errors(tmp_path: Path):
    with pytest.raises(ProviderConfigError) as info:
        loads(tmp_path / "missing.json")
    assert "不存在" in str(info.value)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as info:
        loads(broken)
    assert "JSON" in str(info.value)

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as info:
        loads(array)
    assert "顶层" in str(info.value)


def test_parse_spec():
    assert parse_spec(" P :: m ") == ("P", "m")
    for bad in ("m", "::m", "P::"):
        with pytest.raises(ProviderConfigError):
            parse_spec(bad)


def test_provider_crud():
    providers = ModelProviders()
    provider = new_provider(providers, "P", "https://x", "sk")
    new_provider(providers, "Q", "https://y")
    assert providers.find("P") is provider
    assert resolve_provider(providers, "Q").base_url == "https://y"

    provider.models = ["a", "b"]
    assert remove_model_from_provider(providers, "P", "a") is True
    assert remove_model_from_provider(providers, "P", "nope") is False
    assert remove_provider(providers, "Q") is True
    assert remove_provider(providers, "Q") is False

    with pytest.raises(ProviderConfigError):
        resolve_provider(providers, "missing")


def test_main_provider_and_credentials():
    providers = ModelProviders(
        providers=[ModelProvider(name="P", base_url="https://x", key="sk", models=["m"])],
        main_model="P::m",
    )
    assert get_main_provider(providers).name == "P"
    assert get_main_credentials(providers) == ("m", "sk", "https://x")
    assert get_context_length(providers, "P::m") == DEFAULT_CONTEXT_LENGTH

    providers.main_model = None
    assert get_main_credentials(providers) == ("", "", "")
    with pytest.raises(ProviderConfigError):
        get_main_provider(providers)


def test_model_selection_helpers():
    providers = ModelProviders(
        providers=[
            ModelProvider(name="P", base_url="https://x", models=["a", "b"]),
            ModelProvider(name="Q", base_url="https://y", models=["c"]),
        ],
        main_model="P::a",
        subagent_models=["P::gone", "Q::c", "P::a"],
    )
    assert is_model_spec_usable(providers, "P::a") is True
    assert is_model_spec_usable(providers, "P::gone") is False
    assert is_model_spec_usable(providers, "Nope::a") is False
    assert is_subagent_model_allowed(providers, "Q::c") is True
    assert is_subagent_model_allowed(providers, "P::a") is True
    assert is_subagent_model_allowed(providers, "Q::nope") is False

    dropped = prune_model_selection(providers)
    assert dropped == ["P::gone"]
    assert providers.subagent_models == ["Q::c", "P::a"]
    assert get_default_subagent_model(providers) == "Q::c"

    providers.subagent_models = ["P::gone"]
    assert get_default_subagent_model(providers) == "P::a"

    providers.main_model = None
    providers.subagent_models = None
    with pytest.raises(ProviderConfigError):
        get_default_subagent_model(providers)


def test_empty_models_means_usable():
    """还没拉过模型（models 为空）时不做判定，避免首次配置被误删。"""
    providers = ModelProviders(
        providers=[ModelProvider(name="P", base_url="https://x", models=[])],
        main_model="P::anything",
        subagent_models=["P::anything"],
    )
    assert is_model_spec_usable(providers, "P::anything") is True
