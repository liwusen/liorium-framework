"""``ChatModelPool``：缓存键、用途 / 思考强度、幂等关闭（计划 §5.2 / §8.2）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from liorium.provider import ChatModelPool, ModelFactoryPort
from liorium.provider.errors import ProviderConfigError


class FakeModel:
    def __init__(self, label: str, purpose: str) -> None:
        self.label = label
        self.purpose = purpose


def make_registry(*, api_key: str = "sk-test") -> Any:
    registry = SimpleNamespace(calls=[], api_key=api_key)

    def resolve(label: str, *, api_format: str | None = None) -> Any:
        registry.calls.append((label, api_format))
        return SimpleNamespace(
            provider=SimpleNamespace(name="P", api_key=registry.api_key),
            model=label.split("::")[-1],
        )

    registry.resolve = resolve
    return registry


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def fake_build(provider: Any, model: str, **kwargs: Any) -> FakeModel:
        records.append({"provider": provider.name, "model": model, **kwargs})
        return FakeModel(model, kwargs["purpose"])

    monkeypatch.setattr("liorium.provider.pool.build_chat_model", fake_build)
    return records


@pytest.fixture()
def closed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []

    async def fake_close(model: Any) -> None:
        seen.append(model)

    monkeypatch.setattr("liorium.provider.pool.close_chat_model", fake_close)
    return seen


def test_factories_are_model_factory_ports() -> None:
    pool = ChatModelPool(make_registry())
    assert isinstance(pool.chat, ModelFactoryPort)
    assert isinstance(pool.compact, ModelFactoryPort)


def test_chat_and_compact_differ_by_purpose_and_intensity(built: list[dict[str, Any]]) -> None:
    pool = ChatModelPool(make_registry(), thinking="high", temperature=0.2)

    chat_model = pool.chat("P::m")
    compact_model = pool.compact("P::m")

    assert chat_model is not compact_model
    assert built[0]["purpose"] == "chat"
    assert built[0]["intensity"] == "high"
    assert built[0]["temperature"] == 0.2
    assert built[1]["purpose"] == "compact"
    assert built[1]["intensity"] is None


def test_cache_key_includes_label_thinking_and_purpose(built: list[dict[str, Any]]) -> None:
    registry = make_registry()
    pool = ChatModelPool(registry)

    first = pool.chat("P::m")
    assert pool.chat("P::m") is first
    assert len(built) == 1

    pool.thinking = "low"
    second = pool.chat("P::m")
    assert second is not first
    assert len(built) == 2
    assert built[1]["intensity"] == "low"

    assert registry.calls == [("P::m", None), ("P::m", None)]


def test_api_format_is_forwarded_to_registry(built: list[dict[str, Any]]) -> None:
    registry = make_registry()
    pool = ChatModelPool(registry, api_format="anthropic_messages")

    pool.chat("P::m")

    assert registry.calls == [("P::m", "anthropic_messages")]


def test_missing_api_key_raises(built: list[dict[str, Any]]) -> None:
    pool = ChatModelPool(make_registry(api_key=""))
    with pytest.raises(ProviderConfigError, match="API key"):
        pool.chat("P::m")
    assert built == []


@pytest.mark.asyncio
async def test_aclose_closes_each_model_once_and_is_idempotent(
    built: list[dict[str, Any]], closed: list[Any]
) -> None:
    pool = ChatModelPool(make_registry())
    pool.chat("P::m")
    pool.compact("P::m")

    await pool.aclose()
    await pool.aclose()  # 幂等
    await pool.chat.aclose()  # 绑定视图转发的也是同一个池

    assert len(closed) == 2
    assert len({id(model) for model in closed}) == 2  # 每个恰好一次

    with pytest.raises(RuntimeError, match="已关闭"):
        pool.chat("P::m")


@pytest.mark.asyncio
async def test_close_failure_does_not_break_others(
    built: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def flaky_close(model: Any) -> None:
        seen.append(model)
        if len(seen) == 1:
            raise OSError("boom")

    monkeypatch.setattr("liorium.provider.pool.close_chat_model", flaky_close)
    pool = ChatModelPool(make_registry())
    pool.chat("P::a")
    pool.compact("P::b")

    await pool.aclose()

    assert len(seen) == 2
