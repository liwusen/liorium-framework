"""旧 JSON 记忆库迁移：``graph.json`` + ``meta/`` → SQLite。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.memory import SqliteMemoryStore
from liorium.memory.migrate import (
    MigrationError,
    migrate_legacy_json,
    needs_migration,
)


def build_legacy(root: Path) -> Path:
    """造一个"旧格式"记忆目录（networkx 存出来的样子）。"""
    root.mkdir(parents=True, exist_ok=True)
    graph = {
        "nodes": {
            "path:/": {"type": "dir", "name": "/", "created_at": "2020-01-01T00:00:00Z"},
            "path:/notes": {"type": "dir", "name": "notes"},
            "path:/notes/cat.md": {
                "type": "file",
                "name": "cat.md",
                "description": "关于猫",
                "declared_by": "agent",
                "updated_at": "2021-02-03T04:05:06Z",
                "chunk_count": 1,
                "indexed": True,
            },
            "path:/notes/noindex.md": {
                "type": "file",
                "name": "noindex.md",
                "indexed": True,  # 旧索引里缺了它的分块 → 迁移时从正文重切
            },
            "ent_abc123": {
                "type": "entity",
                "name": "爱丽丝",
                "entity_type": "person",
                "description": "旧库实体",
                "kb_refs": ["/notes/cat.md"],
                "properties": {"city": "上海"},
            },
        },
        "edges": [
            {"source": "path:/", "target": "path:/notes", "type": "has_child", "key": "k0"},
            {
                "source": "path:/notes",
                "target": "path:/notes/cat.md",
                "type": "has_child",
                "key": "k1",
            },
            {
                "source": "path:/notes/cat.md",
                "target": "ent_abc123",
                "type": "from",
                "key": "k2",
            },
        ],
    }
    (root / "graph.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    content = root / "content" / "notes"
    content.mkdir(parents=True, exist_ok=True)
    (content / "cat.md").write_text("猫会打呼噜。", encoding="utf-8")
    (content / "noindex.md").write_text("这篇还没分块。", encoding="utf-8")

    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "cat.meta.json").write_text(
        json.dumps(
            {
                "path": "/notes/cat.md",
                "description": "关于猫",
                "updated_at": "2021-02-03T04:05:06Z",
                "tags": ["动物", "宠物"],
                "score_patch": 0.1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (meta / "chunks_index.json").write_text(
        json.dumps(
            {
                "chunk_1": {
                    "chunk_id": "chunk_1",
                    "node_path": "/notes/cat.md",
                    "chunk_index": 1,
                    "text": "猫会打呼噜。",
                    "text_preview": "猫会打呼噜。",
                    "scope_prefix": "/notes",
                    "updated_at": "2021-02-03T04:05:06Z",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (meta / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "task_id": "tsk_1",
                    "type": "dedupe",
                    "status": "done",
                    "payload": {"n": 1},
                    "created_at": "2021-02-03T04:05:06Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    return root


def test_migration_moves_everything(tmp_path: Path) -> None:
    root = build_legacy(tmp_path / "memory")
    store = SqliteMemoryStore(root / "memory.sqlite")
    try:
        assert needs_migration(store) is True
        report = migrate_legacy_json(store)
        assert report.migrated is True
        assert report.counts["nodes"] == 5
        assert report.counts["tags"] == 2
        assert report.counts["chunks"] == 1
        assert report.counts["tasks"] == 1
        assert report.counts["rechunked"] == 1  # noindex.md 从正文重切

        assert store.count_files() == 2
        cat = store.read("/notes/cat.md")
        assert cat.text == "猫会打呼噜。"
        assert cat.description == "关于猫"
        assert cat.tags == ("动物", "宠物")
        assert cat.updated_at == "2021-02-03T04:05:06Z"

        # 树形关系由 path 推导，不依赖 has_child 边
        assert [node.name for node in store.children("/notes")] == [
            "cat.md",
            "noindex.md",
        ]
        # 关键词索引重建过了
        fts = store.fts
        assert fts is not None
        assert {hit.node_path for hit in fts.query("呼噜")} == {"/notes/cat.md"}
        assert {hit.node_path for hit in fts.query("还没分块")} == {"/notes/noindex.md"}

        # 实体原样保留（id / 类型 / properties / kb_refs）
        from liorium.memory.graph import EntityGraph

        graph = EntityGraph(store)
        entity = graph.get("ent_abc123")
        assert entity is not None
        assert entity.name == "爱丽丝"
        assert entity.properties == {"city": "上海"}
        assert [item.name for item in graph.entities_of("/notes/cat.md")] == ["爱丽丝"]

        tasks = store.get_tasks()
        assert [task["task_id"] for task in tasks] == ["tsk_1"]
        assert tasks[0]["status"] == "done"

        # 旧文件归档，不再触发二次迁移
        assert (root / "_legacy_json" / "graph.json").exists()
        assert not (root / "graph.json").exists()
        assert needs_migration(store) is False
        assert migrate_legacy_json(store).migrated is False
    finally:
        store.close()


def test_migration_is_all_or_nothing(tmp_path: Path) -> None:
    root = build_legacy(tmp_path / "memory")
    # 制造一条坏分块（正文为空）→ 校验失败 → 整体回滚
    (root / "meta" / "chunks_index.json").write_text(
        json.dumps({"bad": {"chunk_id": "bad", "node_path": "/notes/cat.md", "text": ""}}),
        encoding="utf-8",
    )
    store = SqliteMemoryStore(root / "memory.sqlite")
    try:
        with pytest.raises(MigrationError, match="分块正文为空"):
            migrate_legacy_json(store)
        assert store.count_files() == 0  # 回滚了
        assert store.node("/notes/cat.md") is None
        assert (root / "graph.json").exists()  # 没归档
    finally:
        store.close()


def test_migration_requires_source(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.sqlite")
    try:
        assert needs_migration(store) is False
        # 幂等：没有旧文件就什么都不做（可以直接放进启动流程）
        assert migrate_legacy_json(store).migrated is False
        with pytest.raises(MigrationError, match="没有旧格式"):
            migrate_legacy_json(store, force=True)
    finally:
        store.close()
