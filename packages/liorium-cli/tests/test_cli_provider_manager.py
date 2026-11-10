"""CLI 接入 Provider 管理：思考参数、/thinking、/models-dev 与启动刷新。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from liorium.cli.app import _prepare_registry, build_parser
from liorium.cli.providers import ProviderRegistry
from liorium.cli.session import CliOptions, CliSession
from liorium_testkit import ScriptedChatModel, write_workspace

CONFIG = {
    "main_model": "stub::demo",
    "providers": [
        {
            "name": "stub",
            "base_url": "https://stub.invalid/v1",
            "key": "sk-stub",
            "models": ["demo", "other"],
            "model_context_lengths": {"demo": 64_000, "other": 32_000},
        }
    ],
}


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def stub_factory(responses: list | None = None):
    model = ScriptedChatModel(responses=responses or ["桩模型回复"])

    def factory(_label: str) -> ScriptedChatModel:
        return model

    factory.model = model  # type: ignore[attr-defined]
    return factory


def make_session(tmp_path: Path, **overrides) -> CliSession:
    registry = ProviderRegistry.load(write_config(tmp_path))
    resolved = registry.resolve("stub::demo")
    options = CliOptions(
        workspace=write_workspace(tmp_path / "ws"),
        inbox=tmp_path / "inbox",
        color=False,
        models_dev_cache=tmp_path / "cache" / "api.json",
        **overrides,
    )
    return CliSession(
        registry,
        resolved,
        options,
        input_fn=lambda _prompt: "",
        model_factory=stub_factory(),
    )


def test_parser_thinking_defaults(monkeypatch):
    for key in ("LIORIUM_THINKING", "LIORIUM_NO_THINKING", "LIORIUM_HIDE_REASONING"):
        monkeypatch.delenv(key, raising=False)

    args = build_parser().parse_args([])
    assert args.thinking == "medium"
    assert args.hide_reasoning is False
    assert args.models_dev == "cache"

    assert build_parser().parse_args(["--thinking", "high"]).thinking == "high"
    assert build_parser().parse_args(["--no-thinking"]).thinking == "off"
    assert build_parser().parse_args(["--hide-reasoning"]).hide_reasoning is True

    monkeypatch.setenv("LIORIUM_THINKING", "low")
    assert build_parser().parse_args([]).thinking == "low"

    monkeypatch.setenv("LIORIUM_NO_THINKING", "1")
    assert build_parser().parse_args([]).thinking == "off"


def test_parser_rejects_invalid_env(monkeypatch):
    monkeypatch.setenv("LIORIUM_THINKING", "turbo")
    with pytest.raises(ValueError):
        build_parser()

    monkeypatch.delenv("LIORIUM_THINKING")
    monkeypatch.setenv("LIORIUM_MODELS_DEV", "sometimes")
    with pytest.raises(ValueError):
        build_parser()


def test_models_dev_parser(monkeypatch):
    for key in ("LIORIUM_MODELS_DEV", "LIORIUM_MODELS_DEV_CACHE"):
        monkeypatch.delenv(key, raising=False)
    args = build_parser().parse_args(["--models-dev", "refresh"])
    assert args.models_dev == "refresh"
    args = build_parser().parse_args(["--models-dev-cache", "x.json"])
    assert args.models_dev_cache == Path("x.json")


def test_session_factory_passes_thinking(monkeypatch, tmp_path):
    session = make_session(tmp_path, thinking="high")
    captured: dict = {}

    def fake_make_chat_model(resolved, **kwargs):
        captured.update(kwargs)
        captured["label"] = resolved.label
        return object()

    monkeypatch.setattr("liorium.cli.session.make_chat_model", fake_make_chat_model)

    session._build_model("stub::demo", purpose="chat")
    assert captured["intensity"] == "high"
    assert captured["purpose"] == "chat"
    assert captured["temperature"] == session.options.temperature

    captured.clear()
    session._build_model("stub::demo", purpose="compact")
    assert captured["intensity"] is None
    assert captured["purpose"] == "compact"

    # 同一 (label, intensity, purpose) 只构建一次
    session._build_model("stub::demo", purpose="chat")
    assert captured["label"] == "stub::demo"


@pytest.mark.asyncio
async def test_thinking_command_switches_and_rolls_back(tmp_path):
    session = make_session(tmp_path)
    await session.start()
    try:
        assert session.options.thinking == "medium"
        before = session.agent.epoch.value
        await session.handle_command("/thinking high")
        assert session.options.thinking == "high"
        assert session.agent.epoch.value == before  # 思考不影响 CacheEpoch

        await session.handle_command("/thinking nope")
        assert session.options.thinking == "high"  # 非法值不改

        await session.handle_command("/thinking off")
        assert session.options.thinking == "off"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_models_dev_status_command(tmp_path):
    session = make_session(tmp_path)
    await session.start()
    try:
        assert await session.handle_command("/models-dev status") is False
        assert await session.handle_command("/models-dev nope") is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_prepare_registry_enriches_in_memory(monkeypatch, tmp_path):
    config_path = write_config(tmp_path)
    registry = ProviderRegistry.load(config_path)
    catalog = {
        "stub": {
            "id": "stub",
            "name": "Stub",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://stub.invalid/v1",
            "models": {
                "demo": {"limit": {"context": 128000, "output": 4096}},
                "other": {},
            },
        }
    }

    async def fake_ensure_catalog(**kwargs):
        assert kwargs["allow_network"] is False
        return catalog

    monkeypatch.setattr("liorium.cli.app.ensure_catalog", fake_ensure_catalog)

    args = argparse.Namespace(
        models_dev="cache",
        models_dev_cache=tmp_path / "cache" / "api.json",
        refresh_models=False,
    )
    await _prepare_registry(registry, args)

    provider = registry.provider("stub")
    assert provider.catalog_provider_id == "stub"
    assert provider.model_catalog_routes["demo"]["format"] == "openai_chat"
    # 用户手填的上下文不能被目录覆盖
    assert registry.resolve("stub::demo").context_window == 64_000
    assert provider.model_max_output_tokens["demo"] == 4096
    # 只改内存：配置文件未被写回
    assert json.loads(config_path.read_text(encoding="utf-8"))["providers"][0].get(
        "model_catalog_routes", {}
    ) == {}


@pytest.mark.asyncio
async def test_prepare_registry_refresh_models_reports_failure(monkeypatch, tmp_path, capsys):
    registry = ProviderRegistry.load(write_config(tmp_path))

    async def fake_ensure_catalog(**kwargs):
        return {}

    async def fake_refresh_models(names=None, force=True):
        return {"stub": {"ok": False, "error": "boom"}}

    monkeypatch.setattr("liorium.cli.app.ensure_catalog", fake_ensure_catalog)
    monkeypatch.setattr(registry, "refresh_models", fake_refresh_models)

    args = argparse.Namespace(
        models_dev="cache",
        models_dev_cache=tmp_path / "cache" / "api.json",
        refresh_models=True,
    )
    await _prepare_registry(registry, args)

    assert "boom" in capsys.readouterr().err
