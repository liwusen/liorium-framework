"""实体抽取：JSON 解析、去重入库、单飞、失败状态、以及**上下文隔离**。

最后那条是回归：抽取常在被工具调用触发的后台任务里，如果它继承了当前回合的
``contextvars``（LangChain 的 ``var_child_runnable_config`` 就在里面），那次 LLM
调用会成为主 run 的子 run，流式 delta 会冒泡进主 Agent 的事件流。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from liorium.agent.realm.base import JsonStateStore, RealmTaskScope
from liorium.memory import MemoryRealm, SqliteMemoryStore
from liorium.memory.extract import (
    ExtractionError,
    ExtractionResult,
    ExtractedEntity,
    ExtractedRelation,
    LlmExtractor,
    MemoryExtractor,
    parse_extraction_json,
)
from liorium.memory.graph import EntityGraph
from liorium.memory.vectors import NanoVectorBackend

from helpers import FakeEmbedder

SAMPLE = {
    "entities": [
        {"name": "爱丽丝", "type": "person", "description": "住在上海的人", "properties": {}},
        {"name": "上海", "type": "place", "description": "城市"},
    ],
    "relations": [{"source": "爱丽丝", "target": "上海", "type": "located_at"}],
}


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


def test_parse_json_tolerates_fences_and_prose() -> None:
    raw = "好的，结果如下：\n```json\n" + json.dumps(SAMPLE, ensure_ascii=False) + "\n```\n希望有用。"
    result = parse_extraction_json(raw)
    assert [entity.name for entity in result.entities] == ["爱丽丝", "上海"]
    assert result.relations == (ExtractedRelation("爱丽丝", "上海", "located_at"),)


def test_parse_json_rejects_garbage() -> None:
    with pytest.raises(ExtractionError, match="没有 JSON"):
        parse_extraction_json("我觉得这个文本提到了爱丽丝")
    with pytest.raises(ExtractionError, match="不是合法 JSON"):
        parse_extraction_json('{"entities": [}')


def test_parse_json_dedupes_and_filters() -> None:
    payload = {
        "entities": [
            {"name": "爱丽丝", "type": "person"},
            {"name": "爱丽丝", "type": "person"},  # 重名：只留一条
            {"name": "  "},  # 空名：丢掉
            *[{"name": f"实体{i}"} for i in range(30)],
        ],
        "relations": [
            {"source": "爱丽丝", "target": "上海", "type": "located_at"},  # 上海没抽到
            {"source": "爱丽丝", "target": "爱丽丝"},  # 自环
            {"source": "", "target": "爱丽丝"},
        ],
    }
    result = parse_extraction_json(json.dumps(payload, ensure_ascii=False))
    assert len(result.entities) == 15  # MAX_ENTITIES
    assert result.relations == ()


@pytest.mark.asyncio
async def test_llm_extractor_calls_chat_and_parses() -> None:
    calls: list[tuple[str, str]] = []

    async def chat(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps(SAMPLE, ensure_ascii=False)

    extractor = LlmExtractor(chat)
    result = await extractor.extract("爱丽丝住在上海。")
    assert len(result.entities) == 2
    assert calls and "实体与关系抽取" in calls[0][0]
    assert calls[0][1] == "爱丽丝住在上海。"
    assert await extractor.extract("   ") == ExtractionResult()


@pytest.mark.asyncio
async def test_memory_extractor_writes_entities_and_relations(store: SqliteMemoryStore) -> None:
    store.write("/notes/alice.md", "爱丽丝住在上海。")
    graph = EntityGraph(store)

    async def chat(system: str, user: str) -> str:
        return json.dumps(SAMPLE, ensure_ascii=False)

    extractor = MemoryExtractor(store, graph, extractor=LlmExtractor(chat))
    report = await extractor.run_document("/notes/alice.md")
    assert report["status"] == "ok"
    assert report["entities"] == 2 and report["relations"] == 1

    names = {entity.name for entity in graph.iter_entities()}
    assert names == {"爱丽丝", "上海"}
    alice = graph.search("爱丽丝")[0]
    assert {entity.name for entity in graph.entities_of("/notes/alice.md")} == {"爱丽丝", "上海"}
    assert {relation.type for relation in graph.relations_of(alice.id)} == {"located_at", "from"}
    assert extractor.status()["last_success"]["entities"] == 2


@pytest.mark.asyncio
async def test_memory_extractor_dedupes_by_name(store: SqliteMemoryStore) -> None:
    store.write("/notes/1.md", "爱丽丝住在上海。")
    store.write("/notes/2.md", "今天爱丽丝来了。")
    graph = EntityGraph(store)

    async def chat(system: str, user: str) -> str:
        return json.dumps(SAMPLE, ensure_ascii=False)

    extractor = MemoryExtractor(store, graph, extractor=LlmExtractor(chat))
    await extractor.run_document("/notes/1.md")
    first = {entity.name: entity.id for entity in graph.iter_entities()}
    await extractor.run_document("/notes/2.md")
    second = {entity.name: entity.id for entity in graph.iter_entities()}
    assert first == second  # 没有建重复实体
    assert len(graph.entities_of("/notes/2.md")) == 2


@pytest.mark.asyncio
async def test_memory_extractor_dedupes_by_name_vector(
    store: SqliteMemoryStore, tmp_path: Path
) -> None:
    store.write("/notes/1.md", "喵星人喜欢打盹。")
    store.write("/notes/2.md", "猫又来了。")
    embedder = FakeEmbedder(dim=512, aliases={"猫": "cat", "喵": "cat"})
    entity_vectors = NanoVectorBackend(tmp_path / "index" / "entity.vdb", dim=512)
    graph = EntityGraph(store, entity_vectors=entity_vectors)

    async def nyan(system: str, user: str) -> str:
        return json.dumps({"entities": [{"name": "喵星人", "type": "concept"}]}, ensure_ascii=False)

    async def cat(system: str, user: str) -> str:
        return json.dumps({"entities": [{"name": "猫", "type": "concept"}]}, ensure_ascii=False)

    try:
        await MemoryExtractor(
            store, graph, extractor=LlmExtractor(nyan), embedder=embedder,
            dedup_threshold=0.5,
        ).run_document("/notes/1.md")
        await MemoryExtractor(
            store, graph, extractor=LlmExtractor(cat), embedder=embedder,
            dedup_threshold=0.5,
        ).run_document("/notes/2.md")
        entities = graph.iter_entities()
        assert len(entities) == 1  # 语义等价 → 复用同一个实体
        assert entities[0].name == "喵星人"
        assert len(graph.entities_of("/notes/2.md")) == 1
    finally:
        await entity_vectors.aclose()


@pytest.mark.asyncio
async def test_memory_extractor_reports_failures(store: SqliteMemoryStore) -> None:
    store.write("/notes/a.md", "内容。")
    graph = EntityGraph(store)

    class Boom:
        async def extract(self, text: str) -> ExtractionResult:
            raise RuntimeError("模型挂了")

    extractor = MemoryExtractor(store, graph, extractor=Boom())
    report = await extractor.run_document("/notes/a.md")
    assert report["status"] == "error"
    assert "模型挂了" in report["error"]
    assert extractor.status()["last_error"]["path"] == "/notes/a.md"

    missing = await extractor.run_document("/notes/missing.md")
    assert missing["status"] == "error"


@pytest.mark.asyncio
async def test_single_flight_per_document(store: SqliteMemoryStore) -> None:
    store.write("/notes/a.md", "内容。")
    graph = EntityGraph(store)
    gate = asyncio.Event()

    class Slow:
        async def extract(self, text: str) -> ExtractionResult:
            await gate.wait()
            return ExtractionResult()

    extractor = MemoryExtractor(store, graph, extractor=Slow())
    first = asyncio.create_task(extractor.run_document("/notes/a.md"))
    await asyncio.sleep(0.01)
    skipped = await extractor.run_document("/notes/a.md")
    assert skipped["status"] == "skipped"
    gate.set()
    assert (await first)["status"] == "ok"


# ---------------------------------------------------------------------------
# realm 接线 + 上下文隔离
# ---------------------------------------------------------------------------

CURRENT_TURN = contextvars.ContextVar("current_turn", default="")


def make_ctx(tmp_path: Path) -> SimpleNamespace:
    state = JsonStateStore(tmp_path / "realm_state" / "memory.json", realm_id="memory")
    state.load()
    scope = RealmTaskScope()
    return SimpleNamespace(
        state=state,
        inbox_dir=tmp_path,
        params={},
        conversation_id="main",
        agent_id="test",
        tasks=scope,
        spawn=scope.spawn,
    )


@pytest.mark.asyncio
async def test_realm_schedules_extraction_with_blank_context(
    store: SqliteMemoryStore, tmp_path: Path
) -> None:
    """抽取任务不能继承当前回合的 contextvars。"""
    seen: list[str] = []

    class Recorder:
        async def extract(self, text: str) -> ExtractionResult:
            seen.append(CURRENT_TURN.get(""))
            return ExtractionResult(
                entities=(ExtractedEntity("爱丽丝", "person"),),
                relations=(),
            )

    realm = MemoryRealm(store=store, extractor=Recorder())
    ctx = make_ctx(tmp_path)
    await realm.start(ctx)
    try:
        CURRENT_TURN.set("turn-42")
        from liorium.core.types import Text

        root = SimpleNamespace(meta={"path": "/"}, is_directory=True)
        realm._create_child(root, "notes/a.md", Text("爱丽丝来了。"))  # noqa: SLF001
        await realm.drain_extraction(timeout=5.0)
        assert seen == [""]  # 空白上下文：拿不到当前回合的值
        assert [entity.name for entity in realm.graph.iter_entities()] == ["爱丽丝"]
    finally:
        CURRENT_TURN.set("")
        await realm.stop(ctx)


@pytest.mark.asyncio
async def test_memory_graph_tool(store: SqliteMemoryStore, tmp_path: Path) -> None:
    store.write("/notes/alice.md", "爱丽丝住在上海。")
    realm = MemoryRealm(store=store)
    await realm.start(make_ctx(tmp_path))
    try:
        alice = realm.graph.add("爱丽丝", "person")
        shanghai = realm.graph.add("上海", "place")
        realm.graph.add_relation(alice.id, shanghai.id, "located_at")
        realm.graph.add_relation("path:/notes/alice.md", alice.id, "from")
        tool = next(item for item in realm.tools() if item.name == "memory_graph")

        found = json.loads(await tool.ainvoke({"action": "search", "target": "爱丽"}))
        assert [item["name"] for item in found["result"]] == ["爱丽丝"]

        detail = json.loads(
            await tool.ainvoke({"action": "detail", "target": alice.id})
        )
        assert detail["result"]["name"] == "爱丽丝"

        near = json.loads(
            await tool.ainvoke({"action": "neighbors", "target": alice.id, "depth": 2})
        )
        assert {item["name"] for item in near["result"]} == {"上海", "alice.md"}

        linked = json.loads(
            await tool.ainvoke(
                {"action": "entities_of", "target": "/notes/alice.md"}
            )
        )
        assert [item["name"] for item in linked["result"]] == ["爱丽丝"]

        bad = json.loads(await tool.ainvoke({"action": "nope"}))
        assert bad["status"] == "error"
        missing = json.loads(
            await tool.ainvoke({"action": "detail", "target": "ent_missing"})
        )
        assert missing["status"] == "error"
    finally:
        await realm.stop(make_ctx(tmp_path))
