"""实体图：CRUD / 合并 / 邻居（递归 CTE）/ 图谱扩展 / 旧库直读。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from liorium.memory import SqliteMemoryStore
from liorium.memory.graph import EntityGraph, new_entity_id


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


@pytest.fixture
def graph(store: SqliteMemoryStore):
    return EntityGraph(store)


def test_add_entity_writes_node_and_body(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    entity = graph.add("爱丽丝", "person", description="住上海的人", properties={"年龄": 20})
    assert entity.id.startswith("ent_")
    assert entity.entity_type == "person"
    assert entity.kb_refs == (f"/entities/{entity.id}.md",)  # 与旧库一致：正文路径进 kb_refs

    row = store._one(  # noqa: SLF001 - 测试直接看表
        "SELECT id, type, path, name, entity_type, description, data FROM nodes WHERE id=?",
        (entity.id,),
    )
    assert row is not None
    assert row["type"] == "entity"
    assert row["path"] is None  # 实体不是路径节点
    data = json.loads(row["data"])
    assert data["properties"] == {"年龄": 20}
    assert data["kb_refs"] == [f"/entities/{entity.id}.md"]

    body = store.content_dir / "entities" / f"{entity.id}.md"
    assert body.read_text(encoding="utf-8").startswith("# 爱丽丝")

    with pytest.raises(ValueError):
        graph.add("   ")
    with pytest.raises(FileExistsError):
        graph.add("重复 id", eid=entity.id)


def test_get_search_and_iter(graph: EntityGraph) -> None:
    alice = graph.add("爱丽丝", "person")
    graph.add("爱丽丝的妈妈", "person")
    graph.add("上海", "place")
    assert graph.count() == 3

    got = graph.get(alice.id)
    assert got is not None and got.name == "爱丽丝"
    assert graph.get("ent_nope") is None

    assert [item.name for item in graph.search("爱丽丝")] == ["爱丽丝", "爱丽丝的妈妈"]
    assert [item.name for item in graph.search("", type_filter="place")] == ["上海"]
    assert [item.name for item in graph.search("不存在")] == []
    assert len(graph.iter_entities()) == 3


def test_relations_require_existing_nodes(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    store.write("/a.md", "内容")
    alice = graph.add("爱丽丝", "person")
    shanghai = graph.add("上海", "place")

    with pytest.raises(FileNotFoundError):
        graph.add_relation(alice.id, "ent_missing", "located_at")
    with pytest.raises(ValueError):
        graph.add_relation("", shanghai.id)

    graph.add_relation(alice.id, shanghai.id, "located_at")
    graph.add_relation("path:/a.md", alice.id, "from")
    assert len(graph.relations_of(alice.id)) == 2
    assert len(graph.iter_relations()) == 2  # has_child 边不算

    removed = graph.remove_relation(alice.id, shanghai.id)
    assert removed == 1
    assert graph.remove_relation(alice.id, shanghai.id) == 0


def test_neighbors_depth_and_cycles(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    store.write("/notes/alice.md", "爱丽丝喜欢猫。")
    alice = graph.add("爱丽丝", "person")
    shanghai = graph.add("上海", "place")
    cat = graph.add("小明的猫", "object")
    graph.add_relation(alice.id, shanghai.id, "located_at")
    graph.add_relation(alice.id, cat.id, "relates_to")
    graph.add_relation(shanghai.id, cat.id, "relates_to")  # 三角形：不能无限递归
    graph.add_relation("path:/notes/alice.md", alice.id, "from")

    one = graph.neighbors(alice.id, depth=1)
    names = {item["name"] for item in one}
    assert names == {"上海", "小明的猫", "alice.md"}
    doc = next(item for item in one if item["path_ref"])
    assert doc["path_ref"] == "/notes/alice.md"
    assert doc["relations"] == [{"type": "from", "direction": "in"}]

    two = graph.neighbors(alice.id, depth=2)
    assert {item["name"] for item in two} == names  # 三角形不产生新节点
    assert graph.neighbors("ent_missing") == []


def test_detail_and_entities_of(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    store.write("/notes/alice.md", "爱丽丝住在上海。")
    alice = graph.add("爱丽丝", "person", description="主角")
    shanghai = graph.add("上海", "place")
    graph.add_relation(alice.id, shanghai.id, "located_at")
    graph.add_relation("path:/notes/alice.md", alice.id, "from")

    detail = graph.detail(alice.id)
    assert detail is not None
    assert detail["name"] == "爱丽丝"
    assert detail["relations_count"] == 2
    assert {item["name"] for item in detail["neighbors"]} == {"上海", "alice.md"}

    doc_detail = graph.detail("path:/notes/alice.md")
    assert doc_detail is not None and doc_detail["path"] == "/notes/alice.md"

    assert [entity.name for entity in graph.entities_of("/notes/alice.md")] == ["爱丽丝"]
    assert graph.linked_paths(alice.id) == ["/notes/alice.md"]


def test_merge_rewires_edges_and_drops_duplicates(
    store: SqliteMemoryStore, graph: EntityGraph
) -> None:
    store.write("/a.md", "内容")
    keep = graph.add("爱丽丝", "person", description="主要人物")
    absorb = graph.add("爱丽", "person", description="别名", properties={"来源": "日记"})
    other = graph.add("上海", "place")
    graph.add_relation(keep.id, other.id, "located_at")
    graph.add_relation(absorb.id, other.id, "located_at")  # 合并后与上面重复
    graph.add_relation(absorb.id, keep.id, "relates_to")  # 合并后变自环
    graph.add_relation("path:/a.md", absorb.id, "from")

    report = graph.merge(keep.id, absorb.id)
    assert report["ok"] is True
    assert report["dropped_self_loops"] == 1
    assert report["dropped_duplicate_edges"] == 1

    assert graph.get(absorb.id) is None
    merged = graph.get(keep.id)
    assert merged is not None
    assert merged.properties == {"来源": "日记"}  # 缺失键从 absorb 补
    assert merged.description == "主要人物"
    relations = graph.iter_relations()
    assert {(r.src if hasattr(r, "src") else r.source, r.type) for r in relations} == {
        (keep.id, "located_at"),
        ("path:/a.md", "from"),
    }

    with pytest.raises(ValueError):
        graph.merge(keep.id, keep.id)
    with pytest.raises(FileNotFoundError):
        graph.merge(keep.id, "ent_missing")


def test_delete_removes_edges_and_body(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    store.write("/a.md", "内容")
    alice = graph.add("爱丽丝", "person")
    graph.add_relation("path:/a.md", alice.id, "from")
    body = store.content_dir / "entities" / f"{alice.id}.md"
    assert body.exists()

    assert graph.delete(alice.id) is True
    assert graph.delete(alice.id) is False
    assert graph.get(alice.id) is None
    assert graph.iter_relations() == []
    assert not body.exists()


def test_expand_documents_walks_two_hops(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    store.write("/notes/a.md", "爱丽丝住在上海。")
    store.write("/notes/b.md", "上海今天下雨。")
    store.write("/notes/d.md", "又一条提到爱丽丝的记忆。")
    store.write("/notes/c.md", "无关的一条。")
    alice = graph.add("爱丽丝", "person")
    shanghai = graph.add("上海", "place")
    graph.add_relation("path:/notes/a.md", alice.id, "from")
    graph.add_relation("path:/notes/b.md", shanghai.id, "from")
    graph.add_relation("path:/notes/d.md", alice.id, "from")
    graph.add_relation(alice.id, shanghai.id, "relates_to")

    # 文档 → 同一个实体 → 另一篇文档，正好 2 跳
    assert graph.expand_documents(["/notes/a.md"]) == ["/notes/d.md"]
    # 经过实体间关系要多一跳：a.md → 爱丽丝 → 上海 → b.md
    assert graph.expand_documents(["/notes/a.md"], hops=3) == ["/notes/d.md", "/notes/b.md"]
    # 种子本身不会被当成"扩展结果"
    assert graph.expand_documents(["/notes/a.md", "/notes/d.md"]) == []
    assert graph.expand_documents(["/notes/c.md"]) == []


def test_reads_entity_rows_written_by_legacy_layout(store: SqliteMemoryStore) -> None:
    """旧库直接搬过来：``ent_*`` 节点 + data JSON，没有 path 列。"""
    eid = new_entity_id()
    payload = json.dumps(
        {"properties": {"city": "上海"}, "kb_refs": ["/notes/alice.md"]}, ensure_ascii=False
    )
    with store._transaction() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO nodes(id, type, name, description, entity_type, data, created_at,"
            " updated_at, chunk_count, indexed) VALUES (?, 'entity', ?, ?, ?, ?, ?, ?, 0, 0)",
            (eid, "爱丽丝", "旧库里的实体", "person", payload, 1.0, 1.0),
        )
    graph = EntityGraph(store)
    entity = graph.get(eid)
    assert entity is not None
    assert entity.properties == {"city": "上海"}
    assert entity.kb_refs == ("/notes/alice.md",)
    assert [item.name for item in graph.entities_of("/notes/alice.md")] == ["爱丽丝"]


def test_foreign_key_errors_are_preempted(store: SqliteMemoryStore, graph: EntityGraph) -> None:
    """给不存在的关系端点，应该报"节点不存在"而不是裸的外键错误。"""
    alice = graph.add("爱丽丝", "person")
    with pytest.raises(FileNotFoundError, match="节点不存在"):
        graph.add_relation(alice.id, "ent_missing")
    with pytest.raises(sqlite3.IntegrityError):
        # 绕过 graph 直接插：证明外键真的在生效（不是静默丢边）
        with store._transaction() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO edges(src, dst, type, key) VALUES (?, 'ent_missing', 'x', 'k')",
                (alice.id,),
            )
