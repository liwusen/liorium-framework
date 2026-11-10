"""混合检索：RRF 融合、过滤、返回形态。

用"假 embedder + 别名表"造出**关键词搜不到但向量搜得到**的文档，才能真正验证
"两路都在起作用"，而不是让向量路只是关键词路的影子。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.memory import MemorySearch, SearchConfig, SqliteMemoryStore
from liorium.memory.graph import EntityGraph
from liorium.memory.vectors import METADATA_KEYS, NanoVectorBackend, VectorRecord

from helpers import FakeEmbedder

ALIASES = {"猫": "cat", "喵": "cat"}  # 语义近义：关键词完全不相交


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


async def build_backend(store: SqliteMemoryStore, embedder: FakeEmbedder) -> NanoVectorBackend:
    backend = NanoVectorBackend(store.index_dir / "chunks.vdb", dim=embedder.dim)
    records = []
    for row in store.chunk_rows():
        vector = (await embedder.embed([row["text"]]))[0]
        records.append(
            VectorRecord(
                chunk_id=row["chunk_id"],
                vector=vector,
                metadata={key: row.get(key, "") for key in METADATA_KEYS},
            )
        )
    await backend.upsert(records)
    await backend.flush()
    return backend


@pytest.mark.asyncio
async def test_keyword_only_rank(store: SqliteMemoryStore) -> None:
    store.write("/notes/cat.md", "家里的猫叫呼噜，今天又喂了一次。", description="宠物")
    store.write("/notes/dog.md", "狗叫大壮，很能吃。")
    store.set_tags("/notes/cat.md", ["动物"])

    search = MemorySearch(store)
    results = await search.rank("呼噜")
    assert [item.path for item in results] == ["/notes/cat.md"]
    assert results[0].sources == ("keyword",)
    assert results[0].description == "宠物"
    assert results[0].tags == ("动物",)
    assert "家里的猫" in results[0].snippet
    assert results[0].snippet.startswith("宠物")  # 描述也进了索引（与既有实现一致）

    assert await search.rank("") == []
    assert await search.rank("查无此词") == []


@pytest.mark.asyncio
async def test_scope_and_tag_filters(store: SqliteMemoryStore) -> None:
    store.write("/a/x.md", "同样的关键词：蓝鲸。")
    store.write("/b/y.md", "同样的关键词：蓝鲸。", tags=["海"])
    search = MemorySearch(store)

    assert {item.path for item in await search.rank("蓝鲸")} == {"/a/x.md", "/b/y.md"}
    assert {item.path for item in await search.rank("蓝鲸", scope="/b")} == {"/b/y.md"}
    # tags 是 AND 过滤：没有这个标签的文档直接被排除
    assert {item.path for item in await search.rank("蓝鲸", tags=["海"])} == {"/b/y.md"}
    assert await search.rank("蓝鲸", tags=["不存在"]) == []


@pytest.mark.asyncio
async def test_vector_recall_finds_synonym(store: SqliteMemoryStore) -> None:
    store.write("/a/nyan.md", "喵星人喜欢打盹。")
    store.write("/a/dog.md", "狗的日常。")
    embedder = FakeEmbedder(dim=512, aliases=ALIASES)
    backend = await build_backend(store, embedder)
    search = MemorySearch(store, embedder=embedder, vectors=backend)
    try:
        # 关键词路：没有任何 "猫" 字，召不回
        assert await search.rank("猫", mode="keyword") == []
        # 向量路：喵 ≈ 猫（假 embedder 的别名），能召回来
        hits = await search.rank("猫", mode="vector")
        assert [item.path for item in hits] == ["/a/nyan.md"]
        assert hits[0].sources == ("vector",)
        # auto：有向量就用混合
        assert [item.path for item in await search.rank("猫")] == ["/a/nyan.md"]
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_rrf_prefers_documents_both_routes_agree_on(store: SqliteMemoryStore) -> None:
    store.write("/a/both.md", "猫和喵都在这里。")
    store.write("/a/vector_only.md", "喵在那里。")
    embedder = FakeEmbedder(dim=512, aliases=ALIASES)
    backend = await build_backend(store, embedder)
    search = MemorySearch(store, embedder=embedder, vectors=backend)
    try:
        results = await search.rank("猫")
        # 两路都命中的排第一；只被向量命中的在后面
        assert results[0].path == "/a/both.md"
        assert set(results[0].sources) == {"keyword", "vector"}
        by_path = {item.path: item.sources for item in results}
        assert by_path["/a/vector_only.md"] == ("vector",)
        keyword_only = await search.rank("猫", mode="keyword")
        assert [item.path for item in keyword_only] == ["/a/both.md"]
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_score_patch_changes_order(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "同样的词：蓝鲸。")
    store.write("/b.md", "同样的词：蓝鲸。")
    search = MemorySearch(store)
    baseline = await search.rank("蓝鲸")
    assert {item.path for item in baseline} == {"/a.md", "/b.md"}

    store.set_score_patch("/b.md", 0.15)
    boosted = await search.rank("蓝鲸")
    assert boosted[0].path == "/b.md"
    assert boosted[0].score > next(
        item.score for item in boosted if item.path == "/a.md"
    )


@pytest.mark.asyncio
async def test_older_documents_score_lower(store: SqliteMemoryStore) -> None:
    store.write("/new.md", "同样的词：蓝鲸。")
    store.write("/old.md", "同样的词：蓝鲸。")
    with store._transaction() as conn:  # noqa: SLF001 - 测试直接改时间戳
        conn.execute("UPDATE nodes SET updated_at=? WHERE path=?", (0.0, "/old.md"))

    config = SearchConfig(recency_weight=0.05, recency_days=30.0)
    results = await MemorySearch(store, config=config).rank("蓝鲸")
    by_path = {item.path: item.score for item in results}
    assert by_path["/new.md"] > by_path["/old.md"]


@pytest.mark.asyncio
async def test_return_modes(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "第一段。\n\n第二段有蓝鲸。", description="说明")
    search = MemorySearch(store)

    paths = await search.search("蓝鲸", return_mode="paths")
    assert paths == [{"path": "/a.md"}]

    compact = (await search.search("蓝鲸", return_mode="compact"))[0]
    assert set(compact) == {
        "path",
        "uri",
        "score",
        "description",
        "updated_at",
        "tags",
        "sources",
        "snippet",
    }

    snippets = (await search.search("蓝鲸", return_mode="snippets"))[0]
    assert "蓝鲸" in snippets["snippet"]

    full = (await search.search("蓝鲸", return_mode="full"))[0]
    assert "第二段有蓝鲸" in full["content"]

    with pytest.raises(ValueError, match="return_mode"):
        await search.search("蓝鲸", return_mode="nope")
    with pytest.raises(ValueError, match="mode"):
        await search.rank("蓝鲸", mode="nope")


@pytest.mark.asyncio
async def test_vector_mode_without_backend_degrades_to_keyword(
    store: SqliteMemoryStore,
) -> None:
    store.write("/a.md", "蓝鲸。")
    search = MemorySearch(store)  # 没有 embedder/vectors
    assert MemorySearch(store).vector_ready is False
    assert [item.path for item in await search.rank("蓝鲸", mode="vector")] == ["/a.md"]


@pytest.mark.asyncio
async def test_graph_expansion_pulls_in_related_documents(store: SqliteMemoryStore) -> None:
    """直接命中一篇，图谱把"提到同一个实体"的另一篇也带回来。"""
    store.write("/notes/a.md", "蓝鲸住在海里。")  # 命中
    store.write("/notes/b.md", "又一条关于蓝鲸的记录。")  # 也命中关键词
    store.write("/notes/c.md", "完全无关的一篇。")  # 只靠图谱进来

    graph = EntityGraph(store)
    entity = graph.add("蓝鲸", "concept")
    graph.add_relation("path:/notes/a.md", entity.id, "from")
    graph.add_relation("path:/notes/c.md", entity.id, "from")

    without = MemorySearch(store)
    assert "/notes/c.md" not in {item.path for item in await without.rank("蓝鲸")}

    with_graph = MemorySearch(store, graph=graph)
    results = await with_graph.rank("蓝鲸")
    by_path = {item.path: item.sources for item in results}
    assert by_path["/notes/c.md"] == ("graph",)
    assert set(by_path["/notes/a.md"]) == {"keyword"}


@pytest.mark.asyncio
async def test_top_k_and_stable_ordering(store: SqliteMemoryStore) -> None:
    for index in range(6):
        store.write(f"/doc{index}.md", f"共同词 蓝鲸 第 {index} 篇。")
    search = MemorySearch(store)
    assert len(await search.rank("蓝鲸", top_k=3)) == 3
    first = [item.path for item in await search.rank("蓝鲸", top_k=5)]
    second = [item.path for item in await search.rank("蓝鲸", top_k=5)]
    assert first == second  # 同分按路径稳定排序
    assert first == sorted(first)
