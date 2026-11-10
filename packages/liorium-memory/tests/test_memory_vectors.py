"""向量后端（nano-vectordb 兼容）与后台索引器。

兼容性断言是这组测试的重点：写出来的 ``.vdb`` 必须能被既有实现那一侧
（直接用 ``nano_vectordb.NanoVectorDB``）读出来，反之亦然；metadata 键逐字对齐。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from nano_vectordb import NanoVectorDB

from liorium.memory import MemorySearch, SqliteMemoryStore
from liorium.memory.indexer import MemoryIndexer
from liorium.memory.vectors import (
    EMBED_DIM,
    METADATA_KEYS,
    NanoVectorBackend,
    VectorBackendError,
    VectorRecord,
)

from helpers import FakeEmbedder, RecordingVectors


def unit(index: int, dim: int = 64) -> list[float]:
    vector = [0.0] * dim
    vector[index % dim] = 1.0
    return vector


def record(path: str, index: int, *, text: str = "x", dim: int = 64) -> VectorRecord:
    return VectorRecord(
        chunk_id=f"{path}::chunk::1::{index}",
        vector=unit(index, dim),
        metadata={
            "node_path": path,
            "scope_prefix": str(Path(path).parent.as_posix()),
            "chunk_index": 1,
            "text": text,
            "text_preview": text[:20],
        },
    )


@pytest.mark.asyncio
async def test_upsert_query_scope_and_delete(tmp_path: Path) -> None:
    backend = NanoVectorBackend(tmp_path / "index" / "chunks.vdb", dim=64)
    try:
        await backend.upsert([record("/a/1.md", 1), record("/b/2.md", 2)])
        assert backend.count() == 2
        assert backend.ids() == {"/a/1.md::chunk::1::1", "/b/2.md::chunk::1::2"}

        hits = await backend.query(unit(1), top_k=5)
        assert hits[0].chunk_id == "/a/1.md::chunk::1::1"
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)
        assert hits[0].metadata["node_path"] == "/a/1.md"

        only_b = await backend.query(unit(2), top_k=5, scope_prefix="/b")
        assert [hit.chunk_id for hit in only_b] == ["/b/2.md::chunk::1::2"]
        # scope 一条都不命中：nano 0.0.4.3 在这里会 IndexError，后端要兜住
        assert await backend.query(unit(1), top_k=5, scope_prefix="/nope") == []

        assert await backend.delete(["/a/1.md::chunk::1::1"]) == 1
        assert backend.count() == 1
        assert await backend.delete(["/missing"]) == 0
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_vdb_is_readable_by_legacy_client(tmp_path: Path) -> None:
    """Liorium 写 → 裸 NanoVectorDB 读，metadata 逐键一致。"""
    path = tmp_path / "index" / "chunks.vdb"
    backend = NanoVectorBackend(path)
    vector = [0.0] * EMBED_DIM
    vector[7] = 1.0
    await backend.upsert(
        [
            VectorRecord(
                chunk_id="/mem/猫.md::chunk::1::abcd",
                vector=vector,
                metadata={
                    "node_path": "/mem/猫.md",
                    "scope_prefix": "/mem",
                    "chunk_index": 1,
                    "text": "猫会打呼噜。",
                    "text_preview": "猫会打呼噜。",
                },
            )
        ]
    )
    await backend.aclose()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["embedding_dim"] == EMBED_DIM
    assert len(raw["data"]) == 1

    legacy = NanoVectorDB(EMBED_DIM, storage_file=str(path))
    assert len(legacy) == 1
    hit = legacy.query(np.asarray(vector, dtype=np.float32), top_k=1)[0]
    assert hit["__id__"] == "/mem/猫.md::chunk::1::abcd"
    assert set(METADATA_KEYS) <= set(hit)
    assert hit["node_path"] == "/mem/猫.md"
    assert hit["text"] == "猫会打呼噜。"
    assert float(hit["__metrics__"]) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.asyncio
async def test_reads_vdb_written_by_legacy_client(tmp_path: Path) -> None:
    """既有实现写 → Liorium 读并检索。"""
    path = tmp_path / "index" / "chunks.vdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = NanoVectorDB(EMBED_DIM, storage_file=str(path))
    vector = [0.0] * EMBED_DIM
    vector[3] = 1.0
    legacy.upsert(
        [
            {
                "__id__": "old::chunk::1::zz",
                "__vector__": np.asarray(vector, dtype=np.float32),
                "node_path": "/old/x.md",
                "scope_prefix": "/old",
                "chunk_index": 1,
                "text": "旧库里的记忆。",
                "text_preview": "旧库里的记忆。",
            }
        ]
    )
    legacy.save()

    backend = NanoVectorBackend(path)
    try:
        hits = await backend.query(vector, top_k=1)
        assert [hit.chunk_id for hit in hits] == ["old::chunk::1::zz"]
        assert hits[0].metadata["node_path"] == "/old/x.md"
    finally:
        await backend.flush()


@pytest.mark.asyncio
async def test_flush_writes_file_and_unload_frees(tmp_path: Path) -> None:
    path = tmp_path / "chunks.vdb"
    backend = NanoVectorBackend(path, dim=64)
    await backend.upsert([record("/a.md", 1)])
    assert not path.exists()  # 惰性：不 flush 就不落盘
    await backend.flush()
    assert path.exists()
    assert backend.dirty is False

    backend.unload()
    assert backend.loaded is False
    assert backend.count() == 1  # 重新加载
    await backend.upsert([record("/b.md", 2)])
    backend.unload()  # 未落盘的改动随内存一起丢（调用方负责 flush）
    assert backend.count() == 1


@pytest.mark.asyncio
async def test_dimension_mismatch_is_loud(tmp_path: Path) -> None:
    path = tmp_path / "chunks.vdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    NanoVectorDB(64, storage_file=str(path)).save()  # 落一个 64 维的空库
    backend = NanoVectorBackend(path, dim=128)
    with pytest.raises(VectorBackendError, match="维度不兼容"):
        backend.count()
    with pytest.raises(VectorBackendError):
        await backend.upsert([record("/a.md", 1, dim=64)])


@pytest.mark.asyncio
async def test_corrupt_file_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "chunks.vdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    backend = NanoVectorBackend(path, dim=64)
    assert backend.count() == 0
    backups = list((tmp_path).glob("chunks.vdb.corrupt-*"))
    assert len(backups) == 1
    await backend.upsert([record("/a.md", 1)])
    await backend.flush()
    assert path.exists()


# ---------------------------------------------------------------------------
# 后台索引器
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    item.vector_index = True
    yield item
    item.close()


@pytest.mark.asyncio
async def test_indexer_drains_queue(store: SqliteMemoryStore, tmp_path: Path) -> None:
    store.write("/a/猫.md", "猫会打呼噜。")
    embedder = FakeEmbedder(dim=64)
    vectors = NanoVectorBackend(tmp_path / "index" / "chunks.vdb", dim=64)
    indexer = MemoryIndexer(store, embedder=embedder, vectors=vectors)
    try:
        assert store.fts is not None and store.fts.job_count() == 1
        assert await indexer.drain() == 1
        assert store.fts.job_count() == 0
        assert vectors.count() == 1
        query_vector = (await embedder.embed(["猫会打呼噜。"]))[0]
        hits = await vectors.query(query_vector)
        assert hits[0].metadata["node_path"] == "/a/猫.md"
        assert embedder.calls[-1] == ["猫会打呼噜。"]
    finally:
        await vectors.aclose()


@pytest.mark.asyncio
async def test_indexer_skips_deleted_before_processing(
    store: SqliteMemoryStore, tmp_path: Path
) -> None:
    store.write("/a.md", "马上删掉。")
    vectors = RecordingVectors()
    indexer = MemoryIndexer(store, embedder=FakeEmbedder(), vectors=vectors)
    store.delete("/a.md")  # 队列里现在是 delete

    assert await indexer.run_once() == 1
    assert [name for name, _ in vectors.calls] == ["delete"]
    assert vectors.count() == 0


@pytest.mark.asyncio
async def test_search_degrades_to_keyword_when_embedding_fails(
    store: SqliteMemoryStore,
) -> None:
    """一次 embedding 429 不能把已经算好的关键词结果一起丢掉。"""
    store.write("/notes/cat.md", "猫喜欢晒太阳。")

    class BoomEmbedder:
        async def embed(self, texts):
            raise RuntimeError("429 too many requests")

    class EmptyVectors:
        async def query(self, *args, **kwargs):
            return []

    search = MemorySearch(store, embedder=BoomEmbedder(), vectors=EmptyVectors())
    results = await search.search("猫")

    assert results, "语义路失败时关键词结果仍应返回"
    assert all("keyword" in hit["sources"] for hit in results)
    assert search.last_vector_error is not None
    assert "429" in search.last_vector_error


@pytest.mark.asyncio
async def test_indexer_retries_then_drops(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "会一直失败。")
    vectors = RecordingVectors()
    indexer = MemoryIndexer(store, embedder=FakeEmbedder(), vectors=vectors, max_attempts=2)
    store.fts.clear_jobs()  # 只留一条确定的 upsert 作业
    store.fts.enqueue_vectors(store.chunk_rows_by_ids([row["chunk_id"] for row in store.chunk_rows()]))
    assert store.fts.job_count() == 1

    vectors.fail_times = 2
    assert await indexer.run_once() == -1  # 第一轮失败，作业留在队列
    assert store.fts.job_count() == 1
    assert await indexer.run_once() == -1  # 第二轮失败 → 到达上限，丢弃
    assert store.fts.job_count() == 0
    assert vectors.count() == 0


@pytest.mark.asyncio
async def test_indexer_reconcile_fills_gaps(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容一。")
    store.write("/b.md", "内容二。")
    vectors = RecordingVectors()
    indexer = MemoryIndexer(store, embedder=FakeEmbedder(), vectors=vectors)
    indexer.store.fts.clear_jobs()  # 模拟作业丢失（写库成功但索引没记）

    report = await indexer.reconcile()
    assert report == {"missing": 2, "stale": 0}
    assert store.fts.job_count() == 2

    await indexer.drain()
    assert store.fts.job_count() == 0
    assert vectors.count() == 2

    # 反向：向量库里有一条正文已经没了
    vectors.rows["/gone.md::chunk::1::x"] = record("/gone.md", 1)
    report = await indexer.reconcile()
    assert report == {"missing": 0, "stale": 1}
    await indexer.drain()
    assert vectors.count() == 2
