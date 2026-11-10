"""P3 内容与运维面：附件、变更流、任务队列、聊天记录/日记、文件导入。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from liorium.memory import MemoryRealm, SqliteMemoryStore


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


@pytest.fixture
def realm(store: SqliteMemoryStore):
    return MemoryRealm(store=store)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_write_attachment_indexes_description(store: SqliteMemoryStore) -> None:
    node = store.write_attachment(
        "/images/cat.png", PNG, content_type="image/png", description="一只橘猫在窗台上"
    )
    assert node.kind == "file"
    assert node.content_type == "image/png"
    assert node.chunk_count == 1  # 描述生成了一个可检索 chunk

    entry = store.read("/images/cat.png")
    assert entry.is_image
    assert entry.data == PNG
    assert entry.description == "一只橘猫在窗台上"

    fts = store.fts
    assert fts is not None
    assert [hit.node_path for hit in fts.query("橘猫")] == ["/images/cat.png"]

    # 没描述就不生成 chunk（但节点还在）
    store.write_attachment("/images/dog.png", PNG, description="")
    assert store.node("/images/dog.png").chunk_count == 0


def test_changed_nodes_filters(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容 A", tags=["重要"])
    store.write("/b.md", "内容 B")
    store.mkdir("/dir", description="目录")
    graph_entity = store.changed_nodes(0, include_entities=True)

    everything = store.changed_nodes(0)
    assert {item["path"] for item in everything} == {"/a.md", "/b.md", "/dir"}

    tagged = store.changed_nodes(0, tags=["重要"])
    assert [item["path"] for item in tagged] == ["/a.md"]

    scoped = store.changed_nodes(0, scope="/dir")
    assert [item["path"] for item in scoped] == ["/dir"]

    assert store.changed_nodes(0, limit=1)[0]["updated_at"] >= store.changed_nodes(0)[1]["updated_at"]
    assert graph_entity  # include_entities 不影响文件节点
    # 未来时间点：什么都没有
    assert store.changed_nodes(4_000_000_000) == []


def test_task_queue(store: SqliteMemoryStore) -> None:
    task = store.add_task("dedupe_entities", {"threshold": 0.9})
    assert task["status"] == "pending"
    assert task["payload"] == {"threshold": 0.9}

    listed = store.get_tasks()
    assert [item["task_id"] for item in listed] == [task["task_id"]]
    assert listed[0]["payload"] == {"threshold": 0.9}

    store.update_task(task["task_id"], "done")
    assert store.get_tasks()[0]["status"] == "done"
    store.update_task(task["task_id"], "failed", error="boom")
    assert store.get_tasks()[0]["error"] == "boom"
    store.update_task("tsk_missing", "done")  # 不存在的任务：静默无操作


def test_realm_chat_record_chains_records(store: SqliteMemoryStore, realm: MemoryRealm) -> None:
    first = realm.add_chat_record("你好", "你好呀")
    second = realm.add_chat_record("再说一次", "好的")

    assert first["path"].startswith("/records/")
    assert store.read(first["path"]).text == "## 用户\n\n你好\n\n## 助手\n\n你好呀\n"

    records = realm.graph.search("", type_filter="chat_record")
    assert len(records) == 2
    assert {entity.properties["path"] for entity in records} == {
        first["path"],
        second["path"],
    }
    # 前后两条用 next 边串起来
    previous = next(entity for entity in records if entity.properties["path"] == first["path"])
    following = next(entity for entity in records if entity.properties["path"] == second["path"])
    assert any(
        relation.source == previous.id
        and relation.target == following.id
        and relation.type == "next"
        for relation in realm.graph.iter_relations()
    )
    # 记录 → 记忆文件也有 from 边
    assert [entity.name for entity in realm.graph.entities_of(first["path"])] == [
        previous.name
    ]


def test_realm_write_diary(store: SqliteMemoryStore, realm: MemoryRealm) -> None:
    result = realm.write_diary("今天下雨，读了点书。")
    assert result["path"].startswith("/diary/")
    assert store.read(result["path"]).text == "今天下雨，读了点书。"
    assert [entity.name for entity in realm.graph.search("", type_filter="diary")][0].startswith(
        "diary_"
    )


def test_realm_declare_file_update(store: SqliteMemoryStore, realm: MemoryRealm, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("外部文件的内容。", encoding="utf-8")
    result = realm.declare_file_update(str(source))
    assert result["path"] == "/imports/notes.txt"
    assert store.read("/imports/notes.txt").text == "外部文件的内容。"

    custom = realm.declare_file_update(str(source), "/docs/笔记.md")
    assert custom["path"] == "/docs/笔记.md"

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ValueError, match="UTF-8"):
        realm.declare_file_update(str(binary))
    with pytest.raises(FileNotFoundError):
        realm.declare_file_update(str(tmp_path / "missing.txt"))


def test_realm_write_attachment_and_tool(store: SqliteMemoryStore, realm: MemoryRealm) -> None:
    result = realm.write_attachment(
        "/images/x.png", PNG, content_type="image/png", description="一张测试图"
    )
    assert result["bytes"] == len(PNG)
    assert store.read("/images/x.png").data == PNG

    tool = next(item for item in realm.tools() if item.name == "memory_attach")
    import asyncio

    raw = asyncio.run(
        tool.ainvoke(
            {
                "path": "/images/y.png",
                "content_base64": base64.b64encode(PNG).decode("ascii"),
                "description": "另一张图",
            }
        )
    )
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert store.read("/images/y.png").data == PNG

    bad = asyncio.run(
        tool.ainvoke({"path": "/images/z.png", "content_base64": "@@not-base64@@"})
    )
    assert json.loads(bad)["status"] == "error"
