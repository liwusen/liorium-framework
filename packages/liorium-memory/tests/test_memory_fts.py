"""关键词索引（side-car FTS5）与 store 的联动。

守四件事：
1. 中文 2 字 / 单字查询都能命中（FTS5 自带分词器做不到，所以我们是预分词）；
2. 写 / 删 / 改名 / 复制之后索引跟着变（不能残留旧路径）；
3. 索引行数与 ``chunks`` 不一致时 ``is_stale`` 为真，``reindex_keyword`` 能自愈；
4. 向量任务队列与索引在同一个事务里入队（``vector_index`` 打开时）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liorium.memory import SqliteMemoryStore
from liorium.memory.fts import FtsIndex, match_expression, query_terms, tokenize_terms


@pytest.fixture
def store(tmp_path: Path):
    item = SqliteMemoryStore(tmp_path / "memory" / "memory.sqlite")
    yield item
    item.close()


def test_tokenizer_keeps_bigrams_and_unigrams_for_index() -> None:
    # 建索引：二元组 + 单字（否则"猫"这种单字查询永远命中不了）
    assert tokenize_terms("猫咪") == ["猫咪", "猫", "咪"]
    # 查询：只有二元组（单字会带来大量噪声命中）
    assert query_terms("猫咪") == ["猫咪"]
    assert query_terms("猫") == ["猫"]
    assert query_terms("AI Agent") == ["ai", "agent"]
    # 表达式：去重、加引号、OR 连接
    assert match_expression("猫 猫 狗") == '"猫" OR "狗"'
    assert match_expression("   ") == ""


def test_fts_hits_cjk_short_queries(tmp_path: Path) -> None:
    index = FtsIndex(tmp_path / "index" / "fts.sqlite")
    try:
        index.apply(
            items=[
                {
                    "chunk_id": "c1",
                    "node_path": "/notes/cat.md",
                    "scope_prefix": "/notes",
                    "text": "用户喜欢猫，家里的猫叫呼噜。",
                },
                {
                    "chunk_id": "c2",
                    "node_path": "/notes/dog.md",
                    "scope_prefix": "/notes",
                    "text": "狗的名字叫大壮。",
                },
            ]
        )
        assert [hit.node_path for hit in index.query("呼噜")] == ["/notes/cat.md"]
        assert [hit.node_path for hit in index.query("猫")] == ["/notes/cat.md"]  # 单字
        assert {hit.node_path for hit in index.query("猫 狗")} == {
            "/notes/cat.md",
            "/notes/dog.md",
        }
        assert index.query("   ") == []
        assert index.query("猫", scope_prefix="/work") == []
        assert [hit.node_path for hit in index.query("猫", scope_prefix="/notes")] == [
            "/notes/cat.md"
        ]
        assert [hit.rank for hit in index.query("猫 狗")] == [1, 2]
    finally:
        index.close()


def test_store_write_indexes_and_delete_cleans(store: SqliteMemoryStore) -> None:
    store.write("/a/猫.md", "猫会打呼噜。")
    fts = store.fts
    assert fts is not None
    assert [hit.node_path for hit in fts.query("呼噜")] == ["/a/猫.md"]

    store.delete("/a/猫.md")
    assert fts.query("呼噜") == []
    assert fts.count() == 0


def test_store_write_replaces_old_chunks(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "第一版：蓝色。")
    store.write("/a.md", "第二版：红色。")
    fts = store.fts
    assert fts is not None
    assert fts.query("蓝色") == []
    assert [hit.node_path for hit in fts.query("红色")] == ["/a.md"]


def test_rename_and_copy_rewrite_index_paths(store: SqliteMemoryStore) -> None:
    store.write("/old/a.md", "独特的词语：蓝鲸。")
    store.rename("/old", "new")
    fts = store.fts
    assert fts is not None
    assert [hit.node_path for hit in fts.query("蓝鲸")] == ["/new/a.md"]

    store.copy("/new/a.md", "/copy/a.md")
    assert {hit.node_path for hit in fts.query("蓝鲸")} == {"/copy/a.md", "/new/a.md"}


def test_unindexed_write_leaves_no_trace(store: SqliteMemoryStore) -> None:
    store.write("/skip.md", "不该进索引的内容。", index=False)
    fts = store.fts
    assert fts is not None
    assert fts.query("不该") == []
    assert store.chunk_count() == 0


def test_stale_detection_and_reindex(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "内容一。")
    store.write("/b.md", "内容二。")
    status = store.keyword_index_status()
    assert status["stale"] is False
    assert status["indexed"] == status["chunks"] == 2

    fts = store.fts
    assert fts is not None
    fts.rebuild([])  # 模拟索引落后 / 被杀进程
    assert store.keyword_index_status()["stale"] is True
    assert fts.query("内容") == []

    assert store.reindex_keyword() == 2
    assert store.keyword_index_status()["stale"] is False
    assert len({hit.node_path for hit in fts.query("内容")}) == 2


def test_vector_jobs_only_when_enabled(store: SqliteMemoryStore) -> None:
    store.write("/a.md", "没有队列也要能写。")
    fts = store.fts
    assert fts is not None
    assert fts.job_count() == 0

    store.vector_index = True
    store.write("/a.md", "这次应该入队。")
    jobs = fts.pending_jobs(limit=10)
    # 覆盖写：新 chunk 入队 upsert，旧 chunk 入队 delete（向量库里可能已经有旧的了）
    assert {job["op"] for job in jobs} == {"upsert", "delete"}
    upsert = next(job for job in jobs if job["op"] == "upsert")
    assert upsert["payload"]["node_path"] == "/a.md"
    assert upsert["payload"]["text"] == "这次应该入队。"

    store.delete("/a.md")
    # 删掉的 chunk：原来的 upsert 被 delete 覆盖（同一个 chunk_id 只留最新意图）
    assert {job["op"] for job in fts.pending_jobs(limit=10)} == {"delete"}


def test_index_failure_does_not_break_write(store: SqliteMemoryStore, monkeypatch) -> None:
    """索引是派生数据：同步失败只标脏，不能让"正文已写进去"的写操作报错。"""
    fts = store.fts
    assert fts is not None

    def boom(**kwargs):  # noqa: ANN003
        raise __import__("sqlite3").OperationalError("disk I/O error")

    monkeypatch.setattr(fts, "apply", boom)
    store.write("/a.md", "写入仍然要成功。")
    assert store.read("/a.md").text == "写入仍然要成功。"
    assert store.keyword_index_status()["stale"] is True

    monkeypatch.undo()
    store.reindex_keyword()
    assert [hit.node_path for hit in fts.query("成功")] == ["/a.md"]
