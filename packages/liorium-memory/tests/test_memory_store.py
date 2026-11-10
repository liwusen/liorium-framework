"""``SqliteMemoryStore``：schema 兼容、读写、检索、旧库读取。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from liorium.memory import SqliteMemoryStore, chunk_text, normalize_memory_path, tokenize


@pytest.fixture
def store(tmp_path: Path) -> SqliteMemoryStore:
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


def test_write_read_roundtrip(store: SqliteMemoryStore) -> None:
    node = store.write(
        "/人物/爱丽丝", "喜欢猫，讨厌下雨。\n住在上海。", description="角色设定", tags=["人物", "设定"]
    )
    assert node.path == "/人物/爱丽丝"
    assert node.tags == ("人物", "设定")

    entry = store.read("/人物/爱丽丝")
    assert entry.text == "喜欢猫，讨厌下雨。\n住在上海。"
    assert entry.description == "角色设定"
    assert entry.line_count == 2
    assert entry.tags == ("人物", "设定")
    assert (store.content_dir / "人物" / "爱丽丝").read_text(encoding="utf-8") == entry.text


def test_children_sorted_dirs_first(store: SqliteMemoryStore) -> None:
    store.write("/b.md", "b")
    store.write("/a/x.md", "x")
    store.mkdir("/z-dir")
    children = store.children("/")
    assert [item.name for item in children] == ["a", "z-dir", "b.md"]
    assert children[0].kind == "dir"


def test_write_is_upsert_and_keeps_tags(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "一", tags=["t1"])
    store.write("/a.md", "二")
    entry = store.read("/a.md")
    assert entry.text == "二"
    assert entry.tags == ("t1",)
    assert len(store.all_nodes()) == 2  # 根 + a.md


def test_read_image_entry(store: SqliteMemoryStore) -> None:
    store.write("/img/a.png", "", index=False)
    path = store.content_dir / "img" / "a.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n")
    with store._transaction() as conn:  # noqa: SLF001 - 测试直接改 content_type
        conn.execute(
            "UPDATE nodes SET content_type='image/png' WHERE id='path:/img/a.png'"
        )
    entry = store.read("/img/a.png")
    assert entry.is_image is True
    assert entry.data == b"\x89PNG\r\n"


def test_read_errors(store: SqliteMemoryStore) -> None:
    store.mkdir("/dir")
    with pytest.raises(IsADirectoryError):
        store.read("/dir")
    with pytest.raises(FileNotFoundError):
        store.read("/ghost")


def test_delete_file_and_tree(store: SqliteMemoryStore) -> None:
    store.write("/a/b/c.md", "c")
    assert store.delete("/a") is True
    assert store.node("/a") is None
    assert store.node("/a/b/c.md") is None
    assert not (store.content_dir / "a").exists()
    assert store.delete("/nothing") is False


def test_delete_tree_with_like_metacharacters(store: SqliteMemoryStore) -> None:
    """目录名含 ``_`` / ``%`` 时也必须删掉整棵子树（LIKE 要带 ESCAPE）。"""
    store.write("/a_b/c.md", "c")
    store.write("/a_b/sibling.md", "s")
    store.write("/a%b/c.md", "percent")
    doomed = {chunk for chunk in store.iter_chunk_ids() if chunk.startswith("/a_b/")}
    assert doomed

    assert store.delete("/a_b") is True
    assert store.node("/a_b") is None
    assert store.node("/a_b/c.md") is None
    assert store.node("/a_b/sibling.md") is None
    assert not (store.content_dir / "a_b").exists()
    assert not any(chunk in doomed for chunk in store.iter_chunk_ids())

    # 相邻的 /a%b 不能被误删
    assert store.node("/a%b/c.md") is not None
    assert store.read("/a%b/c.md").text == "percent"


def test_concurrent_read_write_never_returns_partial(store: SqliteMemoryStore) -> None:
    """多线程写正文时，读方只能看到完整的旧值或新值。"""
    import concurrent.futures as futures

    payloads = [f"值-{index}-" + ("x" * 4000) for index in range(12)]
    store.write("/race.md", payloads[0])

    def worker(index: int):
        if index % 3 == 0:
            store.write("/race.md", payloads[index % len(payloads)])
            return None
        try:
            return store.read("/race.md").text
        except FileNotFoundError:
            return None

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(120)))
    assert all(item is None or item in payloads for item in results)


def test_long_text_is_chunked(store: SqliteMemoryStore) -> None:
    text = "段落。" * 2000
    store.write("/long.md", text)
    node = store.node("/long.md")
    assert node is not None and node.chunk_count > 1


def test_search_scores_and_scope(store: SqliteMemoryStore) -> None:
    store.write("/notes/cat.md", "猫喜欢晒太阳。", tags=["动物"])
    store.write("/notes/dog.md", "狗喜欢跑步。")
    store.write("/other/cat.md", "猫在别的目录。")

    hits = store.search("猫")
    assert len(hits) == 2
    assert all("猫" in hit.snippet for hit in hits)

    scoped = store.search("猫", scope="/notes")
    assert [hit.path for hit in scoped] == ["/notes/cat.md"]

    tagged = store.search("喜欢", tags=["动物"])
    assert [hit.path for hit in tagged] == ["/notes/cat.md"]

    assert store.search("   ") == []
    assert store.search("不存在的词") == []


def test_revision_changes_on_write(store: SqliteMemoryStore) -> None:
    before = store.revision()
    store.write("/a.md", "x")
    assert store.revision() != before


def test_reopen_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "memory" / "memory.sqlite"
    first = SqliteMemoryStore(db)
    first.write("/a.md", "持久化")
    first.close()

    second = SqliteMemoryStore(db)
    try:
        assert second.read("/a.md").text == "持久化"
    finally:
        second.close()


def test_rejects_wrong_schema_version(tmp_path: Path) -> None:
    db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE schema_version(version INTEGER NOT NULL);")
    conn.execute("INSERT INTO schema_version(version) VALUES (99)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema 版本不支持"):
        SqliteMemoryStore(db)


def test_reads_legacy_db_written_by_plain_sql(tmp_path: Path) -> None:
    """模拟既有实现库：不经过本 store 直接写 schema 行，仍能读。"""
    db = tmp_path / "legacy" / "memory.sqlite"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    # 用本模块的 DDL 建表（与旧库一致），再手写行，模拟"旧数据"
    from liorium.memory.store import SCHEMA_SQL

    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.execute(
        "INSERT INTO nodes(id, type, name, description, parent_id, path, updated_at,"
        " chunk_count, indexed) VALUES ('path:/', 'dir', '/', '', NULL, '/', 0, 0, 0)"
    )
    conn.execute(
        "INSERT INTO nodes(id, type, name, description, parent_id, path, updated_at,"
        " chunk_count, indexed) VALUES ('path:/old.md', 'file', 'old.md', '旧记录', 'path:/',"
        " '/old.md', 1730000000, 0, 0)"
    )
    conn.execute(
        "INSERT INTO tags(node_id, tag) VALUES ('path:/old.md', '历史')"
    )
    conn.commit()
    conn.close()
    (db.parent / "content").mkdir()
    (db.parent / "content" / "old.md").write_text("旧正文", encoding="utf-8")

    store = SqliteMemoryStore(db)
    try:
        entry = store.read("/old.md")
        assert entry.text == "旧正文"
        assert entry.description == "旧记录"
        assert entry.tags == ("历史",)
    finally:
        store.close()


def test_path_normalization_and_chunking_helpers() -> None:
    assert normalize_memory_path("a/b") == "/a/b"
    assert normalize_memory_path("/") == "/"
    with pytest.raises(ValueError):
        normalize_memory_path("/a/../b")

    assert tokenize("猫咪 AI Agent") == ["ai", "agent", "猫咪"]
    chunks = chunk_text("x" * 7000)
    assert len(chunks) == 3
    assert chunks[1].startswith("x" * 100)
