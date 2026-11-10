"""``SqliteMemoryStore`` 的元数据 / 目录树 / 多条件检索（P0 迁移）。

覆盖：

- ``set_tags`` / ``set_score_patch``：整组替换、``managed_by``、范围校验、不刷 ``updated_at``；
- ``rename`` / ``move`` / ``copy``：正文、节点行、标签、chunk_id（重新分块）全部跟着走；
- ``advanced_search``：标签 AND/OR、日期、``declared_by``、``content_type``、排序、``line_count``。

注意：chunk_id 里带路径，所以任何改路径的操作都必须重新分块 —— 这里专门断言
旧 chunk 消失、新 chunk 出现（否则向量索引会指向不存在的 chunk）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.memory import MAX_SCORE_PATCH, MIN_SCORE_PATCH, SqliteMemoryStore
from liorium.memory.store import path_id


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


def chunk_ids(store: SqliteMemoryStore, path: str) -> list[str]:
    return [
        str(row["chunk_id"])
        for row in store._all(  # noqa: SLF001 - 断言索引状态
            "SELECT chunk_id FROM chunks WHERE node_id=?", (path_id(path),)
        )
    ]


# ---------------------------------------------------------------------------
# 标签 / 权重
# ---------------------------------------------------------------------------


def test_set_tags_replaces_and_keeps_updated_at(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容", tags=["旧"])
    before = store.node("/a.md").updated_at
    node = store.set_tags("/a.md", ["人物", " 人物 ", "", "上海"], managed_by="araya")

    assert node.tags == ("人物", "上海")  # 去重、去空白、保序
    assert store.node("/a.md").updated_at == before  # 元数据不刷时间（增量维护依赖这点）
    assert store._one("SELECT managed_by FROM nodes WHERE path='/a.md'")["managed_by"] == "araya"

    store.set_tags("/a.md", [])
    assert store.tags_of("/a.md") == ()

    with pytest.raises(FileNotFoundError):
        store.set_tags("/nope.md", ["x"])


def test_set_score_patch_validates_range(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容")
    node = store.set_score_patch("/a.md", 0.1)
    assert node.score_patch == pytest.approx(0.1)

    for bad in (MIN_SCORE_PATCH - 0.01, MAX_SCORE_PATCH + 0.01, float("nan")):
        with pytest.raises(ValueError):
            store.set_score_patch("/a.md", bad)


def test_score_patch_lifts_search_ranking(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "同一段正文 关键词")
    store.write("/b.md", "同一段正文 关键词")
    before = {hit.path: hit.score for hit in store.search("关键词", top_k=2)}

    store.set_score_patch("/a.md", MAX_SCORE_PATCH)
    store.set_score_patch("/b.md", MIN_SCORE_PATCH)
    after = {hit.path: hit.score for hit in store.search("关键词", top_k=2)}

    # 权重直接进打分：两者差 = MAX - MIN
    assert after["/a.md"] - after["/b.md"] == pytest.approx(
        MAX_SCORE_PATCH - MIN_SCORE_PATCH, abs=1e-6
    )
    assert after["/a.md"] > before["/a.md"]
    assert store.search("关键词", top_k=1)[0].path == "/a.md"


# ---------------------------------------------------------------------------
# 重命名 / 移动 / 复制
# ---------------------------------------------------------------------------


def test_rename_file_moves_content_and_rechunks(store: SqliteMemoryStore) -> None:
    store.write("/notes/a.md", "猫会打呼噜。", tags=["动物"])
    old_chunks = chunk_ids(store, "/notes/a.md")

    new_path = store.rename("/notes/a.md", "b.md")

    assert new_path == "/notes/b.md"
    assert store.node("/notes/a.md") is None
    node = store.node("/notes/b.md")
    assert node is not None and node.tags == ("动物",)
    assert (store.content_dir / "notes" / "b.md").is_file()
    assert not (store.content_dir / "notes" / "a.md").exists()
    new_chunks = chunk_ids(store, "/notes/b.md")
    assert new_chunks and set(new_chunks).isdisjoint(old_chunks)
    assert [hit.path for hit in store.search("打呼噜")] == ["/notes/b.md"]


def test_rename_directory_rewrites_subtree(store: SqliteMemoryStore) -> None:
    store.write("/diary/2026/01.md", "第一篇", tags=["日记"])
    store.write("/diary/2026/02.md", "第二篇")

    new_path = store.rename("/diary", "journal")

    assert new_path == "/journal"
    assert store.node("/journal") is not None
    assert store.node("/journal/2026/01.md") is not None
    assert store.node("/diary") is None
    assert store.node("/diary/2026/01.md") is None
    assert store.tags_of("/journal/2026/01.md") == ("日记",)
    assert (store.content_dir / "journal" / "2026" / "01.md").read_text(encoding="utf-8") == "第一篇"
    # 子树的 chunk 全部重算（旧路径的 chunk 不能残留）
    assert chunk_ids(store, "/diary/2026/01.md") == []
    assert chunk_ids(store, "/journal/2026/01.md")
    assert [hit.path for hit in store.search("第二篇")] == ["/journal/2026/02.md"]


def test_move_into_new_directory(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容 A")
    new_path = store.move("/a.md", "/archive/2026")
    assert new_path == "/archive/2026/a.md"
    assert store.node("/archive/2026") is not None  # 中间目录自动补齐
    assert store.node("/a.md") is None
    assert store.search("内容")[0].path == "/archive/2026/a.md"


def test_copy_file_and_tree(store: SqliteMemoryStore) -> None:
    store.write("/src/a.md", "原件", tags=["t"], description="说明")
    store.write("/src/sub/b.md", "子件")

    assert store.copy("/src/a.md", "/dst/a.md") == "/dst/a.md"
    copied = store.read("/dst/a.md")
    assert copied.text == "原件"
    assert copied.tags == ("t",)
    assert copied.description == "说明"
    assert [hit.path for hit in store.search("原件")] == ["/dst/a.md", "/src/a.md"]

    store.copy("/src", "/src2")
    assert store.read("/src2/sub/b.md").text == "子件"
    assert store.read("/src/a.md").text == "原件"  # 原件不动


def test_tree_operation_guards(store: SqliteMemoryStore) -> None:
    store.write("/a/x.md", "x")
    store.write("/b.md", "b")
    with pytest.raises(ValueError):
        store.rename("/", "root2")
    with pytest.raises(ValueError):
        store.move("/", "/tmp")
    with pytest.raises(ValueError):
        store.rename("/b.md", "x/y.md")
    with pytest.raises(FileExistsError):
        store.rename("/b.md", "a")  # 目标是已存在目录
    with pytest.raises(ValueError):
        store.move("/a", "/a/sub")  # 移进自己的子树
    with pytest.raises(FileExistsError):
        store.copy("/b.md", "/a")  # 已存在
    with pytest.raises(FileNotFoundError):
        store.rename("/ghost", "x.md")


def test_move_keeps_parent_links(store: SqliteMemoryStore) -> None:
    store.write("/d1/a.md", "甲")
    store.move("/d1/a.md", "/d2")
    assert store.children("/d1") == []
    names = [node.name for node in store.children("/d2")]
    assert names == ["a.md"]


# ---------------------------------------------------------------------------
# advanced_search
# ---------------------------------------------------------------------------


def test_advanced_search_filters_by_tags_logic(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "甲", tags=["人物", "上海"])
    store.write("/b.md", "乙", tags=["人物"])
    store.write("/c.md", "丙", tags=["地点"])

    and_hits = store.advanced_search(tags=["人物", "上海"], tag_logic="AND")
    assert [item["path"] for item in and_hits] == ["/a.md"]

    or_hits = store.advanced_search(tags=["上海", "地点"], tag_logic="OR")
    assert {item["path"] for item in or_hits} == {"/a.md", "/c.md"}


def test_advanced_search_filters_by_scope_declared_by_type(store: SqliteMemoryStore) -> None:
    store.write("/notes/a.md", "文本", declared_by="agent")
    store.write("/notes/b.md", "文本", declared_by="user")
    store.write("/other/c.md", "文本")

    assert {i["path"] for i in store.advanced_search(scope="/notes")} == {
        "/notes/a.md",
        "/notes/b.md",
    }
    assert [i["path"] for i in store.advanced_search(declared_by="user")] == ["/notes/b.md"]
    # 没有 content_type 的节点按旧语义视为"不排除"
    assert len(store.advanced_search(content_type="text")) == 3


def test_advanced_search_text_scoring_and_sorting(store: SqliteMemoryStore) -> None:
    store.write("/关键词.md", "正文无关")
    store.write("/其它.md", "描述里有 关键词", description="关键词")
    store.write("/无.md", "什么都没有")

    hits = store.advanced_search("关键词")
    assert hits[0]["path"] == "/关键词.md"
    assert hits[0]["score"] == pytest.approx(1.0)
    assert hits[1]["score"] == pytest.approx(0.8)
    assert all(item["path"] != "/无.md" for item in hits)

    line_counts = store.advanced_search(sort_by="updated_at", sort_order="asc")
    assert all("line_count" in item for item in line_counts)
    assert line_counts[0]["line_count"] == 1


def test_advanced_search_date_window(store: SqliteMemoryStore) -> None:
    store.write("/old.md", "旧")
    with store._transaction() as conn:  # noqa: SLF001 - 造一条 2020-01-01 的时间戳
        conn.execute("UPDATE nodes SET updated_at=1577836800 WHERE path='/old.md'")
    store.write("/new.md", "新")

    assert [i["path"] for i in store.advanced_search(date_from="2021-01-01")] == ["/new.md"]
    assert [i["path"] for i in store.advanced_search(date_to="2020-12-31")] == ["/old.md"]
