"""Provider /models 自动发现：解析、补全、裁剪与错误。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from liorium.provider import ModelProvider
from liorium.provider import discovery
from liorium.provider.errors import ProviderConfigError


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, url, headers=None, **kwargs):  # noqa: ANN001
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def _patch(monkeypatch, payload=None, error=None) -> _FakeClient:
    client = _FakeClient(payload=payload, error=error)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    return client


def _provider(**overrides) -> ModelProvider:
    defaults = dict(name="P", base_url="https://api.example.com/v1", key="sk-test")
    defaults.update(overrides)
    return ModelProvider(**defaults)


def test_fetch_parses_openai_shape_and_metadata(monkeypatch):
    payload = {
        "data": [
            {
                "id": "deepseek-v4",
                "context_window": 1_000_000,
                "max_output_tokens": 32_000,
                "api_capabilities": {
                    "openai_chat": {"streaming": True},
                    "nope": {"x": 1},
                },
                "effort": {"supported_levels": ["low", "high"]},
            },
            {
                "id": "claude",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 8192},
            },
        ]
    }
    client = _patch(monkeypatch, payload=payload)

    entries = asyncio.run(discovery.fetch_provider_model_entries(_provider()))

    assert [e["id"] for e in entries] == ["deepseek-v4", "claude"]
    assert entries[0]["context_length"] == 1_000_000
    assert entries[0]["max_output_tokens"] == 32_000
    assert entries[0]["api_capabilities"] == {"openai_chat": {"streaming": True}}
    assert entries[0]["thinking"] == {
        "reasoning": None,
        "options": [{"type": "effort", "values": ["low", "high"]}],
    }
    assert entries[1]["context_length"] == 200_000
    assert entries[1]["max_output_tokens"] == 8192
    assert client.calls == ["https://api.example.com/v1/models"]


def test_fetch_keeps_path_prefix(monkeypatch):
    client = _patch(monkeypatch, payload={"data": [{"id": "m"}]})

    asyncio.run(
        discovery.fetch_provider_model_entries(
            _provider(base_url="https://gateway.example.com/zen/go/v1")
        )
    )

    assert client.calls == ["https://gateway.example.com/zen/go/v1/models"]


def test_fetch_bare_list_has_no_metadata(monkeypatch):
    _patch(monkeypatch, payload=["chat-model", "claude-model"])

    entries = asyncio.run(discovery.fetch_provider_model_entries(_provider()))

    assert [e["id"] for e in entries] == ["chat-model", "claude-model"]
    assert entries[0]["context_length"] is None
    assert entries[0]["thinking"] is None


def test_fetch_http_error_is_config_error(monkeypatch):
    _patch(monkeypatch, error=httpx.ConnectError("boom"))

    with pytest.raises(ProviderConfigError) as info:
        asyncio.run(discovery.fetch_provider_model_entries(_provider()))

    assert "请求" in str(info.value)


def test_fetch_empty_response_is_error(monkeypatch):
    _patch(monkeypatch, payload={"data": []})

    with pytest.raises(ProviderConfigError) as info:
        asyncio.run(discovery.fetch_provider_model_entries(_provider()))

    assert "无法" in str(info.value)


def test_auto_load_fills_missing_and_keeps_user_values(monkeypatch):
    provider = _provider(models=[], model_context_lengths={"keep": 123})
    _patch(
        monkeypatch,
        payload={
            "data": [
                {"id": "m", "context_length": 1000, "max_output_tokens": 100},
                {"id": "keep", "context_length": 2000},
            ]
        },
    )

    models = asyncio.run(discovery.auto_load_model_for_provider(provider, force=True))

    assert models == ["m", "keep"]
    assert provider.model_context_lengths == {"keep": 123, "m": 1000}
    assert provider.model_max_output_tokens == {"m": 100}


def test_auto_load_is_idempotent_when_models_present(monkeypatch):
    provider = _provider(models=["m"])
    client = _patch(monkeypatch, payload={"data": [{"id": "m"}]})

    models = asyncio.run(discovery.auto_load_model_for_provider(provider))

    assert models == ["m"]
    assert client.calls == []


def test_auto_load_force_prunes_stale_metadata(monkeypatch):
    provider = _provider(
        models=["old", "m"],
        model_context_lengths={"old": 1, "m": 1},
        model_max_output_tokens={"old": 1, "m": 1},
        model_api_capabilities={"old": {"openai_chat": {}}, "m": {"openai_chat": {}}},
        model_thinking_caps={"old": {"reasoning": True}, "m": {"reasoning": True}},
    )
    _patch(monkeypatch, payload={"data": [{"id": "m"}]})

    asyncio.run(discovery.auto_load_model_for_provider(provider, force=True))

    assert provider.models == ["m"]
    assert "old" not in provider.model_context_lengths
    assert "old" not in provider.model_max_output_tokens
    assert "old" not in provider.model_api_capabilities
    assert "old" not in provider.model_thinking_caps


def test_auto_load_failure_keeps_old_models(monkeypatch):
    provider = _provider(models=["m"])
    _patch(monkeypatch, error=httpx.ConnectError("boom"))

    with pytest.raises(ProviderConfigError):
        asyncio.run(discovery.auto_load_model_for_provider(provider, force=True))

    assert provider.models == ["m"]


def test_auto_load_fills_thinking_caps_from_models(monkeypatch):
    provider = _provider(models=[])
    _patch(
        monkeypatch,
        payload={
            "data": [
                {
                    "id": "m",
                    "reasoning": True,
                    "reasoning_options": [
                        {"type": "effort", "values": ["low", "high"]}
                    ],
                }
            ]
        },
    )

    asyncio.run(discovery.auto_load_model_for_provider(provider, force=True))

    assert provider.model_thinking_caps["m"] == {
        "reasoning": True,
        "options": [{"type": "effort", "values": ["low", "high"]}],
    }
