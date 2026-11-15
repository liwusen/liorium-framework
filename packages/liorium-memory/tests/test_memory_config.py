"""``MemoryRealm.Config``：配置驱动自组装（计划 C1/C5 / §4.2 / §6.4）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from liorium.agent.realm.base import JsonStateStore
from liorium.memory import MemoryConfig, MemoryRealm
from liorium.memory import embeddings as embeddings_module
from liorium.memory import indexer as indexer_module
from liorium.memory import vectors as vectors_module

from helpers import FakeEmbedder, RecordingVectors


def make_ctx(tmp_path: Path, **extra: Any) -> Any:
    state = JsonStateStore(tmp_path / "realm_state" / "memory.json", realm_id="memory")
    state.load()
    ctx = SimpleNamespace(
        state=state,
        inbox_dir=tmp_path,
        conversation_id="main",
        agent_id="test",
        spawn=lambda coro, name=None: asyncio.ensure_future(coro),
    )
    ctx.__dict__.update(extra)
    return ctx


def test_config_defaults_and_validation() -> None:
    config = MemoryConfig()
    assert config.dir is None
    assert config.vectors is True
    assert config.extraction is True
    assert config.list_limit == 200
    assert config.keyword_index is True

    with pytest.raises(ValidationError):
        MemoryConfig(unknown_key=1)
    with pytest.raises(ValidationError, match="dir"):
        MemoryConfig(dir="a", db_path="b")
    with pytest.raises(ValidationError):
        MemoryConfig(list_limit=0)


def test_realm_validates_config_on_construction() -> None:
    with pytest.raises(ValidationError):
        MemoryRealm(SimpleNamespace(config={"unknown_key": 1}))


async def test_dir_layout_and_owned_store(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(SimpleNamespace(config={"dir": "mem"}))
    await realm.start(ctx)
    try:
        store = realm._store
        assert store is not None
        assert store.path == tmp_path / "mem" / "memory.sqlite"
        assert store.content_dir == tmp_path / "mem" / "content"
        assert realm.vectors_enabled is False  # 没配 embed ⇒ 只跑关键词
    finally:
        await realm.stop(ctx)


async def test_embed_config_builds_and_closes_owned_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = FakeEmbedder()
    backends: list[RecordingVectors] = []

    monkeypatch.setattr(
        embeddings_module, "OpenAICompatibleEmbeddings", lambda **kwargs: embedder
    )

    def make_backend(path: Path) -> RecordingVectors:
        backend = RecordingVectors()
        backends.append(backend)
        return backend

    monkeypatch.setattr(vectors_module, "NanoVectorBackend", make_backend)

    class FakeIndexer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False

        async def run(self) -> None:
            await asyncio.sleep(3600)

        async def aclose(self) -> None:
            self.closed = True

        async def reconcile(self) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(indexer_module, "MemoryIndexer", FakeIndexer)

    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(
        SimpleNamespace(config={"embed": {"model": "text-embedding-3-small", "api_key": "k"}})
    )
    await realm.start(ctx)
    try:
        assert realm.vectors_enabled is True
        assert len(backends) == 2  # chunks.vdb + entity.vdb
        assert realm._owns_embedder is True
        assert realm._owns_vectors is True
    finally:
        await realm.stop(ctx)

    assert embedder.closed is True
    assert all(backend.flushes >= 1 for backend in backends)
    assert realm._embedder is None and realm._vectors is None


async def test_missing_embed_degrades_to_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise AssertionError("没有 embed 配置时不该构造 embedder")

    monkeypatch.setattr(embeddings_module, "OpenAICompatibleEmbeddings", boom)

    ctx = make_ctx(tmp_path)
    realm = MemoryRealm(SimpleNamespace(config={"vectors": True}))
    await realm.start(ctx)
    try:
        assert realm.vectors_enabled is False
        assert realm.keyword_index is True
    finally:
        await realm.stop(ctx)


async def test_extractor_wraps_ctx_model(tmp_path: Path) -> None:
    calls: list[Any] = []

    class FakeModel:
        async def ainvoke(self, messages: Any) -> SimpleNamespace:
            calls.append(messages)
            return SimpleNamespace(content="OK")

    model = FakeModel()
    ctx = make_ctx(tmp_path, model_factory=lambda: model)
    realm = MemoryRealm(SimpleNamespace(config={}))
    await realm.start(ctx)
    try:
        assert realm.extraction_enabled is True
        assert realm._extractor is not None
        assert realm._extractor_spec is not None
        assert await realm._extractor_spec.chat("sys", "hello") == "OK"
    finally:
        await realm.stop(ctx)

    assert len(calls) == 1


async def test_standalone_without_ctx_model_disables_extraction(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)  # 没有 model_factory
    realm = MemoryRealm(SimpleNamespace(config={}))
    await realm.start(ctx)
    try:
        assert realm.extraction_enabled is False
        assert realm._extractor is None
    finally:
        await realm.stop(ctx)
