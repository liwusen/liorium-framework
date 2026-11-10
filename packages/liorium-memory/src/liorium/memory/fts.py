"""关键词索引 side-car：SQLite FTS5 + 预分词。

为什么要 side-car（``<memory>/index/fts.sqlite``）而不是塞进 ``memory.sqlite``：

1. 既有实现用 ``rank_bm25`` 把**全量 token** 放进进程内存（10 万 chunk 就是
   上 GB 的 Python list）；FTS5 的倒排索引在磁盘上，常驻只有页缓存
   （``PRAGMA cache_size`` 钉在 ~20MB）。
2. 主库保持"既有实现能直接读写"的样子：只多一个同级文件，不动 schema。

分词策略：**不引 jieba**。中文按二元组、ASCII 按 ``[a-z0-9_]{2,}``，写入时预分词
（``tokenize_terms``），查询时同样分词再用 ``OR`` 连接。这样 2 字中文查询
（"呼噜"）也能命中——FTS5 自带的 ``trigram`` 分词器要求 ≥3 字，直接用它搜不到。

向量任务队列也放这个文件（``vector_jobs``）：写库时同步入队，后台索引器慢慢
embed。和 ``chunks_fts`` 同一个连接 ⇒ 一次写事务把"关键词可见"和"待向量化"
一起落盘，不会出现关键词搜得到、向量却永远缺一条的情况。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u3400-\u9fff]+")

DEFAULT_CACHE_MB = 20
"""FTS5 页缓存上限（MB）——常驻内存的上界，与库大小无关。"""

MAX_QUERY_TERMS = 32
"""一次查询最多用多少个词（长句的 OR 表达式别无限长）。"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fts_meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  body,
  chunk_id     UNINDEXED,
  node_path    UNINDEXED,
  scope_prefix UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS vector_jobs(
  chunk_id   TEXT PRIMARY KEY,
  op         TEXT NOT NULL,
  node_path  TEXT NOT NULL DEFAULT '',
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at REAL
);
"""


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------


def _terms(text: str, *, unigrams: bool) -> list[str]:
    """切成检索词：ASCII 词 + 中文二元组（可选再补单字），**保序、保留重复**。"""
    lowered = str(text or "").lower()
    terms = _WORD_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            terms.append(run)
            continue
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
        if unigrams:
            # 建索引时补单字：否则「猫」这种单字查询永远命中不了二元组索引
            # （"的猫"/"猫叫"都不是 "猫"）。查询侧不加单字，避免噪声。
            terms.extend(run)
    return terms


def tokenize_terms(text: str) -> list[str]:
    """建索引用：中文二元组 + 单字，**保序、保留重复**（BM25 要词频）。

    和 ``store.tokenize()`` 的区别：那个给 LIKE 检索用，会去重（同一个词出现两次
    对"命中次数"没意义）；BM25 要的是词频，所以这里必须保留重复。
    """
    return _terms(text, unigrams=True)


def query_terms(text: str) -> list[str]:
    """查询用：有二元组就只用二元组（精度），单字串才退化成单字（召回）。"""
    return _terms(text, unigrams=False)


def match_expression(query: str, *, limit: int = MAX_QUERY_TERMS) -> str:
    """把查询串变成 FTS5 的 MATCH 表达式（``"甲" OR "乙"``）；空查询返回空串。"""
    seen: set[str] = set()
    parts: list[str] = []
    for term in query_terms(query):
        if term in seen:
            continue
        seen.add(term)
        parts.append('"' + term.replace('"', '""') + '"')
        if len(parts) >= limit:
            break
    return " OR ".join(parts)


class FtsHit(NamedTuple):
    """一条关键词命中。"""

    chunk_id: str
    node_path: str
    scope_prefix: str
    score: float
    """FTS5 ``bm25()`` 原值：**越小（越负）越相关**，只用来排序，不参与融合。"""
    rank: int
    """1 起的位次（融合用位次，避免和向量的 cosine 混量纲）。"""


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


class FtsIndex:
    """``index/fts.sqlite``：关键词倒排 + 向量任务队列。

    线程安全：单连接 + ``RLock``（与 ``SqliteMemoryStore`` 同策略）。
    """

    def __init__(self, path: Path | str, *, cache_mb: int = DEFAULT_CACHE_MB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # 负值是 KB：bm25 打分要遍历倒排，页缓存太小会疯狂读盘。
        self._conn.execute(f"PRAGMA cache_size=-{max(1, int(cache_mb)) * 1024}")
        self._conn.executescript(SCHEMA_SQL)

    # -- 生命周期 -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "FtsIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 写 -------------------------------------------------------------

    def apply(
        self,
        *,
        old_ids: Iterable[str] = (),
        items: Iterable[Mapping[str, Any]] = (),
        vector_jobs: bool = False,
    ) -> None:
        """一次事务内：删旧关键词行 → 插新行 → （可选）更新向量任务队列。

        ``items`` 的键与 ``chunks`` 行一致：``chunk_id`` / ``node_path`` /
        ``scope_prefix`` / ``text``（可选 ``chunk_index`` / ``text_preview``）。
        """
        removed = [str(i) for i in old_ids]
        rows = list(items)
        with self._lock, self._transaction() as conn:
            for chunk_id in removed:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,))
                if vector_jobs:
                    conn.execute(
                        "INSERT OR REPLACE INTO vector_jobs(chunk_id, op, node_path, payload,"
                        " created_at) VALUES (?, 'delete', '', '{}', ?)",
                        (chunk_id, time.time()),
                    )
            for item in rows:
                chunk_id = str(item["chunk_id"])
                node_path = str(item.get("node_path") or "")
                scope = str(item.get("scope_prefix") or "/")
                body = " ".join(tokenize_terms(str(item.get("text") or "")))
                conn.execute(
                    "INSERT INTO chunks_fts(body, chunk_id, node_path, scope_prefix)"
                    " VALUES (?, ?, ?, ?)",
                    (body, chunk_id, node_path, scope),
                )
                if vector_jobs:
                    payload = {
                        "node_path": node_path,
                        "scope_prefix": scope,
                        "chunk_index": int(item.get("chunk_index") or 0),
                        "text": str(item.get("text") or ""),
                        "text_preview": str(item.get("text_preview") or ""),
                    }
                    conn.execute(
                        "INSERT OR REPLACE INTO vector_jobs(chunk_id, op, node_path, payload,"
                        " created_at) VALUES (?, 'upsert', ?, ?, ?)",
                        (chunk_id, node_path, json.dumps(payload, ensure_ascii=False), time.time()),
                    )
            if rows or removed:
                # 增量维护行数：写库时不该为了一个计数去 COUNT(*) 扫整张 FTS 表。
                current = self._expected(conn)
                conn.execute(
                    "INSERT INTO fts_meta(key, value) VALUES ('rows', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(max(0, current + len(rows) - len(removed))),),
                )

    def rebuild(self, items: Iterable[Mapping[str, Any]]) -> int:
        """清空并从 ``chunks`` 全量重建（漂移自愈 / ``store.reindex_keyword()``）。"""
        with self._lock, self._transaction() as conn:
            conn.execute("DELETE FROM chunks_fts")
            count = 0
            for item in items:
                conn.execute(
                    "INSERT INTO chunks_fts(body, chunk_id, node_path, scope_prefix)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        " ".join(tokenize_terms(str(item.get("text") or ""))),
                        str(item["chunk_id"]),
                        str(item.get("node_path") or ""),
                        str(item.get("scope_prefix") or "/"),
                    ),
                )
                count += 1
            conn.execute(
                "INSERT INTO fts_meta(key, value) VALUES ('rows', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(count),),
            )
            return count

    # -- 读 -------------------------------------------------------------

    def query(
        self,
        text: str,
        *,
        top_k: int = 10,
        scope_prefix: str = "/",
    ) -> list[FtsHit]:
        """关键词检索：返回按相关性排序的 chunk 命中（跨节点，不去重）。"""
        expression = match_expression(text)
        if not expression:
            return []
        sql = (
            "SELECT chunk_id, node_path, scope_prefix, bm25(chunks_fts) AS score"
            " FROM chunks_fts WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [expression]
        scope = str(scope_prefix or "/")
        if scope != "/":
            sql += " AND (node_path = ? OR node_path LIKE ? ESCAPE '\\')"
            params.extend([scope, _like_prefix(scope)])
        sql += " ORDER BY score LIMIT ?"
        params.append(max(1, int(top_k)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            FtsHit(
                str(row["chunk_id"]),
                str(row["node_path"]),
                str(row["scope_prefix"]),
                float(row["score"]),
                index + 1,
            )
            for index, row in enumerate(rows)
        ]

    def count(self) -> int:
        with self._lock:
            return self._count(self._conn)

    def expected_rows(self) -> int | None:
        """上次同步时记下的行数（用来发现漂移；``None`` ⇒ 从没同步过）。"""
        with self._lock:
            return self._expected(self._conn)

    def is_stale(self, chunk_count: int) -> bool:
        """``chunks`` 行数与索引记录不一致 ⇒ 需要 ``rebuild``。"""
        expected = self.expected_rows()
        if expected is None:
            return int(chunk_count) > 0
        return expected != int(chunk_count)

    def mark_stale(self) -> None:
        """把行数标记成 -1：下次 ``is_stale()`` 一定为真（同步失败时用）。"""
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO fts_meta(key, value) VALUES ('rows', '-1')"
                " ON CONFLICT(key) DO UPDATE SET value='-1'"
            )

    # -- 向量任务 -------------------------------------------------------

    def pending_jobs(self, *, limit: int = 32) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_id, op, node_path, payload FROM vector_jobs"
                " ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        jobs: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            jobs.append(
                {
                    "chunk_id": str(row["chunk_id"]),
                    "op": str(row["op"]),
                    "node_path": str(row["node_path"] or ""),
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        return jobs

    def ack_jobs(self, chunk_ids: Sequence[str]) -> int:
        if not chunk_ids:
            return 0
        with self._lock, self._transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                "DELETE FROM vector_jobs WHERE chunk_id=?", [(str(i),) for i in chunk_ids]
            )
            return conn.total_changes - before

    def enqueue_vectors(self, items: Iterable[Mapping[str, Any]]) -> int:
        """把已存在的 chunk 补进向量队列（自愈 / 首次开启向量检索时用）。"""
        now = time.time()
        rows: list[tuple[str, str, str, float]] = []
        for item in items:
            node_path = str(item.get("node_path") or "")
            payload = json.dumps(
                {
                    "node_path": node_path,
                    "scope_prefix": str(item.get("scope_prefix") or "/"),
                    "chunk_index": int(item.get("chunk_index") or 0),
                    "text": str(item.get("text") or ""),
                    "text_preview": str(item.get("text_preview") or ""),
                },
                ensure_ascii=False,
            )
            rows.append((str(item["chunk_id"]), node_path, payload, now))
        if not rows:
            return 0
        with self._lock, self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vector_jobs(chunk_id, op, node_path, payload, created_at)"
                " VALUES (?, 'upsert', ?, ?, ?)",
                rows,
            )
        return len(rows)

    def enqueue_deletes(self, chunk_ids: Iterable[str]) -> int:
        """向量库里还有、但正文已没了的 chunk（删节点/删目录时用）。"""
        now = time.time()
        rows = [(str(cid), now) for cid in chunk_ids]
        if not rows:
            return 0
        with self._lock, self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO vector_jobs(chunk_id, op, node_path, payload, created_at)"
                " VALUES (?, 'delete', '', '{}', ?)",
                rows,
            )
        return len(rows)

    def job_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM vector_jobs").fetchone()
        return int(row["n"] if row else 0)

    def clear_jobs(self) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute("DELETE FROM vector_jobs")

    # -- 内部 -----------------------------------------------------------

    _depth = 0

    def _transaction(self) -> "_Transaction":
        return _Transaction(self)

    def _count(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()
        return int(row["n"] if row else 0)

    def _expected(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM fts_meta WHERE key='rows'").fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0


class _Transaction:
    """可重入事务（外层 BEGIN IMMEDIATE，内层加入同一事务）。"""

    def __init__(self, index: FtsIndex) -> None:
        self._index = index

    def __enter__(self) -> sqlite3.Connection:
        index = self._index
        index._lock.acquire()
        index._depth += 1
        try:
            if index._depth == 1:
                index._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            index._depth -= 1
            index._lock.release()
            raise
        return index._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        index = self._index
        try:
            if index._depth == 1:
                if exc_type is None:
                    index._conn.execute("COMMIT")
                else:
                    index._conn.execute("ROLLBACK")
        finally:
            index._depth -= 1
            index._lock.release()


def _like_prefix(path: str) -> str:
    """``/notes`` → ``/notes/%``（转义 LIKE 元字符）。"""
    escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


__all__ = [
    "DEFAULT_CACHE_MB",
    "FtsHit",
    "FtsIndex",
    "MAX_QUERY_TERMS",
    "match_expression",
    "query_terms",
    "tokenize_terms",
]
