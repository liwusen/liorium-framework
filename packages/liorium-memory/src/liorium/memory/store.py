"""记忆库存储：**与既有实现 memory.sqlite 同 schema** 的 SQLite 读写面。

计划依据 §3.7（memory store 冻结：必须能读旧库并继续读写）与 §F5（不写迁移脚本）。

表结构（与既有实现 v1 一致）：

- ``nodes``：``path:/a/b`` 这样的 path 节点是目录/文件的真源；``ent_*`` 是实体；
- ``edges``：``has_child``（树）+ 语义边；
- ``tags`` / ``chunks``：标签与检索分块；
- 文件正文在 ``<store_dir>/content/<path>``（与旧库同布局）。

本模块只依赖标准库 + ``liorium.core`` + 自己的 ``fts.py``；**不做向量检索**（那需要
embedding 服务）：``search()`` 是关键词打分（tf-idf + 标签/路径/新鲜度加权），
``search.py`` 的混合检索在这里之上再叠 FTS5 BM25 / 向量 / 图谱。

关键词倒排索引是 side-car（``<store_dir>/index/fts.sqlite``，见 ``fts.py``）：
写库时同步更新，**派生数据**——索引坏了/落后了能 ``reindex_keyword()`` 全量重建，
不会阻断写入。
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from liorium.core.log import get_logger

from .fts import FtsIndex, query_terms

log = get_logger("liorium.memory.store")

SCHEMA_VERSION = 1
MAX_CHUNK_CHARS = 3000
CHUNK_OVERLAP_CHARS = 300
TREE_EDGE = "has_child"
MIN_SCORE_PATCH = -0.15
MAX_SCORE_PATCH = 0.15
"""``score_patch`` 的取值范围（与旧库一致；越界直接报错，不静默截断）。"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS nodes(
  id            TEXT PRIMARY KEY,
  type          TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  description   TEXT NOT NULL DEFAULT '',
  entity_type   TEXT,
  content_type  TEXT,
  parent_id     TEXT REFERENCES nodes(id) ON UPDATE CASCADE ON DELETE SET NULL,
  path          TEXT,
  declared_by   TEXT,
  updated_at    REAL,
  created_at    REAL,
  score_patch   REAL NOT NULL DEFAULT 0.0,
  score_patch_updated_at REAL,
  managed_by    TEXT,
  chunk_count   INTEGER NOT NULL DEFAULT 0,
  indexed       INTEGER NOT NULL DEFAULT 0,
  data          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id, type);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_path ON nodes(path) WHERE path IS NOT NULL;
CREATE INDEX IF NOT EXISTS nodes_type_updated ON nodes(type, updated_at DESC);

CREATE TABLE IF NOT EXISTS edges(
  src  TEXT NOT NULL REFERENCES nodes(id) ON UPDATE CASCADE ON DELETE CASCADE,
  dst  TEXT NOT NULL REFERENCES nodes(id) ON UPDATE CASCADE ON DELETE CASCADE,
  type TEXT NOT NULL,
  key  TEXT NOT NULL,
  PRIMARY KEY(src, dst, type, key)
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src, type);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, type);

CREATE TABLE IF NOT EXISTS tags(
  node_id TEXT NOT NULL REFERENCES nodes(id) ON UPDATE CASCADE ON DELETE CASCADE,
  tag     TEXT NOT NULL,
  PRIMARY KEY(node_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS chunks(
  chunk_id     TEXT PRIMARY KEY,
  node_id      TEXT NOT NULL REFERENCES nodes(id) ON UPDATE CASCADE ON DELETE CASCADE,
  chunk_index  INTEGER NOT NULL,
  text         TEXT NOT NULL,
  text_preview TEXT NOT NULL DEFAULT '',
  scope_prefix TEXT NOT NULL DEFAULT '/',
  updated_at   REAL
);
CREATE INDEX IF NOT EXISTS chunks_node ON chunks(node_id);
CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(scope_prefix);

CREATE TABLE IF NOT EXISTS tasks(
  task_id    TEXT PRIMARY KEY,
  type       TEXT NOT NULL,
  status     TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at REAL,
  updated_at REAL,
  error      TEXT NOT NULL DEFAULT ''
);
"""

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"})


# ---------------------------------------------------------------------------
# 路径 / 时间 helper
# ---------------------------------------------------------------------------


def normalize_memory_path(path: str) -> str:
    """记忆路径归一：``a/b`` → ``/a/b``；含 ``..`` 直接报错（不静默裁剪）。"""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw or raw == "/":
        return "/"
    parts = [p for p in raw.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"记忆路径不允许 .. : {path!r}")
    return "/" + "/".join(parts)


def path_id(path: str) -> str:
    """路径 → 节点 id（``/a/b`` → ``path:/a/b``）。"""
    norm = normalize_memory_path(path)
    return "path:/" if norm == "/" else "path:" + norm


def epoch_to_iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        import calendar

        return float(calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


def chunk_text(text: str, *, max_chars: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """把正文切成带重叠的分块（与既有实现同参数：3000 / 300）。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]
    chunks: list[str] = []
    step = max(1, max_chars - overlap)
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        start += step
    return chunks


def tokenize(text: str) -> list[str]:
    """检索用分词：ASCII 词 + 中文二元组（单字串退化成单字），去重、不引分词库。

    用 ``query_terms`` 而不是 ``tokenize_terms``：这是 **查** 侧，单字不进查询
    （"原件" 拆成 原/件 会让只含 "子件" 的文档也命中）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in query_terms(text):
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryNode:
    """一个 path 节点（目录或文件）的只读投影。"""

    path: str
    kind: str  # "dir" / "file"
    name: str
    description: str = ""
    updated_at: str = ""
    content_type: str = ""
    chunk_count: int = 0
    score_patch: float = 0.0
    tags: tuple[str, ...] = ()

    @property
    def uri(self) -> str:
        return "memories://" + self.path.lstrip("/")


@dataclass(frozen=True)
class MemoryEntry:
    """一次读取的结果：文本 / 图片 / 其它二进制三选一。"""

    path: str
    name: str
    description: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    content_type: str = ""
    text: str | None = None
    data: bytes | None = None
    line_count: int | None = None

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/") and self.data is not None


@dataclass(frozen=True)
class SearchHit:
    path: str
    score: float
    snippet: str
    description: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    chunk_index: int = 0

    @property
    def uri(self) -> str:
        return "memories://" + self.path.lstrip("/")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "uri": self.uri,
            "score": round(self.score, 4),
            "snippet": self.snippet,
            "description": self.description,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
        }


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkBatch:
    """一次分块改动的结果：删掉了哪些 chunk、新增了哪些（供派生索引同步）。"""

    old_ids: tuple[str, ...] = ()
    items: tuple[dict[str, Any], ...] = ()


class SqliteMemoryStore:
    """与旧库同 schema 的记忆存储。

    线程安全：单连接 + ``RLock`` 串行化（与旧 ``MemoryDB`` 同策略）。

    ``index_dir`` 下放派生索引（``fts.sqlite``：关键词倒排 + 向量任务队列）。
    ``keyword_index=False`` 可以完全关掉它（代价：``fts.query`` 不可用，
    混合检索退回 ``search()`` 的 LIKE 打分）。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        content_dir: Path | str | None = None,
        index_dir: Path | str | None = None,
        keyword_index: bool = True,
        fts_cache_mb: int = 20,
    ) -> None:
        self.path = Path(db_path)
        self.content_dir = Path(content_dir) if content_dir is not None else self.path.parent / "content"
        self.index_dir = Path(index_dir) if index_dir is not None else self.path.parent / "index"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.keyword_index = bool(keyword_index)
        self.fts_cache_mb = max(1, int(fts_cache_mb))
        self.vector_index = False
        """是否把 chunk 变更记进向量任务队列（由启用向量检索的一方打开）。"""
        self._fts: FtsIndex | None = None
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()
        self._ensure_root()

    # -- 生命周期 -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._fts is not None:
                self._fts.close()
                self._fts = None
            self._conn.close()

    def __enter__(self) -> "SqliteMemoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"memory schema 版本不支持: 库内={row['version']} 代码={SCHEMA_VERSION}"
                )

    def _ensure_root(self) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO nodes(id, type, name, path) VALUES ('path:/', 'dir', '/', '/') "
                "ON CONFLICT(id) DO NOTHING"
            )

    # -- 事务 / 查询 helper ---------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """可重入事务：外层 ``BEGIN IMMEDIATE``，内层加入同一事务。"""
        with self._lock:
            self._depth += 1
            try:
                if self._depth == 1:
                    self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                if self._depth == 1:
                    self._conn.execute("COMMIT")
            except BaseException:
                if self._depth == 1:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                self._depth -= 1

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- 查询 -----------------------------------------------------------

    def revision(self) -> tuple[int, int, float]:
        """结构 + 内容指纹（realm 用它判断"值得重挂载"）。"""
        node = self._one("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS ts FROM nodes")
        chunk = self._one("SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS ts FROM chunks")
        return (
            int(node["n"] if node else 0),
            int(chunk["n"] if chunk else 0),
            float(max(node["ts"] if node else 0.0, chunk["ts"] if chunk else 0.0)),
        )

    def tags_of(self, path: str) -> tuple[str, ...]:
        rows = self._all(
            "SELECT tag FROM tags WHERE node_id=? ORDER BY rowid", (path_id(path),)
        )
        return tuple(str(row["tag"]) for row in rows)

    def node(self, path: str) -> MemoryNode | None:
        norm = normalize_memory_path(path)
        row = self._one(
            "SELECT id, type, name, description, content_type, updated_at, chunk_count,"
            " score_patch FROM nodes WHERE id=?",
            (path_id(norm),),
        )
        if row is None:
            return None
        return MemoryNode(
            path=norm,
            kind="dir" if str(row["type"]) == "dir" else "file",
            name=str(row["name"] or Path(norm).name or "/"),
            description=str(row["description"] or ""),
            updated_at=epoch_to_iso(row["updated_at"]),
            content_type=str(row["content_type"] or ""),
            chunk_count=int(row["chunk_count"] or 0),
            score_patch=float(row["score_patch"] or 0.0),
            tags=self.tags_of(norm),
        )

    def children(
        self, path: str = "/", *, limit: int | None = None, offset: int = 0
    ) -> list[MemoryNode]:
        """列一层子节点（目录在前，按名字排序）；``limit`` 用于投影分页。"""
        norm = normalize_memory_path(path)
        if self.node(norm) is None:
            return []
        rows = self._all(
            "SELECT id, type, name, description, content_type, updated_at, chunk_count,"
            " score_patch, path FROM nodes WHERE parent_id=? AND type IN ('dir', 'file')"
            " ORDER BY type='dir' DESC, name COLLATE NOCASE",
            (path_id(norm),),
        )
        out: list[MemoryNode] = []
        for row in rows:
            child_path = str(row["path"] or "")
            if not child_path:
                continue
            out.append(
                MemoryNode(
                    path=normalize_memory_path(child_path),
                    kind="dir" if str(row["type"]) == "dir" else "file",
                    name=str(row["name"] or Path(child_path).name),
                    description=str(row["description"] or ""),
                    updated_at=epoch_to_iso(row["updated_at"]),
                    content_type=str(row["content_type"] or ""),
                    chunk_count=int(row["chunk_count"] or 0),
                    score_patch=float(row["score_patch"] or 0.0),
                    tags=self.tags_of(child_path),
                )
            )
        out.sort(key=lambda n: (0 if n.kind == "dir" else 1, n.name.lower()))
        start = max(0, int(offset))
        if limit is None:
            return out[start:]
        return out[start : start + max(0, int(limit))]

    def count_children(self, path: str = "/") -> int:
        """一层的子节点数量（投影分页要告诉模型"还有多少条"）。"""
        row = self._one(
            "SELECT COUNT(*) AS n FROM nodes WHERE parent_id=? AND type IN ('dir', 'file')",
            (path_id(path),),
        )
        return int(row["n"] if row else 0)

    def count_files(self) -> int:
        """全部文件节点数（``COUNT(*)``，不读节点行，不随库大小占内存）。"""
        row = self._one("SELECT COUNT(*) AS n FROM nodes WHERE type='file' AND path IS NOT NULL")
        return int(row["n"] if row else 0)

    def all_nodes(self) -> list[MemoryNode]:
        rows = self._all(
            "SELECT path FROM nodes WHERE type IN ('dir', 'file') AND path IS NOT NULL"
        )
        nodes: list[MemoryNode] = []
        for row in rows:
            item = self.node(str(row["path"]))
            if item is not None:
                nodes.append(item)
        nodes.sort(key=lambda n: (n.path.count("/"), n.path.lower()))
        return nodes

    # -- 读 -------------------------------------------------------------

    def read(self, path: str) -> MemoryEntry:
        # 正文文件与 nodes 行必须读自同一个临界区：并发 write()/delete() 会在
        # 释放行锁之后替换/删除文件，不持锁读会拿到半截 UTF-8 或 FileNotFoundError。
        with self._lock:
            return self._read_locked(path)

    def _read_locked(self, path: str) -> MemoryEntry:
        norm = normalize_memory_path(path)
        row = self._one(
            "SELECT type, name, description, content_type, updated_at FROM nodes WHERE id=?",
            (path_id(norm),),
        )
        if row is None:
            raise FileNotFoundError(f"记忆节点不存在: {norm}")
        if str(row["type"]) == "dir":
            raise IsADirectoryError(f"是目录不是文件: {norm}")

        content_type = str(row["content_type"] or "")
        cp = self._content_path(norm)
        raw = b""
        if cp.is_file():
            raw = cp.read_bytes()
        if not content_type:
            content_type = "image/png" if cp.suffix.lower() in _IMAGE_SUFFIXES else "text/plain"

        text: str | None = None
        data: bytes | None = None
        line_count: int | None = None
        if content_type.startswith("image/"):
            data = raw
        else:
            try:
                text = raw.decode("utf-8")
                line_count = len(text.splitlines())
            except UnicodeDecodeError:
                data = raw
                content_type = content_type or "application/octet-stream"
        return MemoryEntry(
            path=norm,
            name=str(row["name"] or Path(norm).name),
            description=str(row["description"] or ""),
            updated_at=epoch_to_iso(row["updated_at"]),
            tags=self.tags_of(norm),
            content_type=content_type,
            text=text,
            data=data,
            line_count=line_count,
        )

    # -- 写 -------------------------------------------------------------

    def write(
        self,
        path: str,
        text: str,
        *,
        description: str | None = None,
        declared_by: str = "agent",
        tags: Iterable[str] | None = None,
        index: bool = True,
    ) -> MemoryNode:
        norm = normalize_memory_path(path)
        if norm == "/":
            raise ValueError("不能写记忆根目录")
        content = str(text or "")
        with self._transaction() as conn:
            parent_id = self._ensure_ancestors(conn, norm)
            now = time.time()
            cp = self._content_path(norm)
            cp.parent.mkdir(parents=True, exist_ok=True)
            # 同目录临时文件 + 原子替换：读方看到的是旧文件或新文件，不会是半截。
            tmp = cp.with_name(f".{cp.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(content, encoding="utf-8", newline="")
                os.replace(tmp, cp)
            finally:
                tmp.unlink(missing_ok=True)

            existing = self._one("SELECT type FROM nodes WHERE id=?", (path_id(norm),))
            if existing is None:
                conn.execute(
                    "INSERT INTO nodes(id, type, name, description, parent_id, path, declared_by,"
                    " updated_at, created_at, chunk_count, indexed) VALUES (?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        path_id(norm),
                        Path(norm).name,
                        str(description or ""),
                        parent_id,
                        norm,
                        declared_by,
                        now,
                        now,
                        0,
                        1 if index else 0,
                    ),
                )
            else:
                if str(existing["type"]) == "dir":
                    raise IsADirectoryError(f"是目录不是文件: {norm}")
                conn.execute(
                    "UPDATE nodes SET parent_id=?, declared_by=?, updated_at=?,"
                    " description=COALESCE(?, description), indexed=? WHERE id=?",
                    (
                        parent_id,
                        declared_by,
                        now,
                        description,
                        1 if index else 0,
                        path_id(norm),
                    ),
                )

            if tags is not None:
                conn.execute("DELETE FROM tags WHERE node_id=?", (path_id(norm),))
                for tag in [str(t).strip() for t in tags if str(t).strip()]:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags(node_id, tag) VALUES (?, ?)",
                        (path_id(norm), tag),
                    )

            if index:
                batch = self._replace_chunks_locked(
                    conn, norm, content, description=description, now=now
                )
            else:
                old_ids = tuple(
                    str(row["chunk_id"])
                    for row in conn.execute(
                        "SELECT chunk_id FROM chunks WHERE node_id=?", (path_id(norm),)
                    )
                )
                conn.execute("DELETE FROM chunks WHERE node_id=?", (path_id(norm),))
                batch = ChunkBatch(old_ids, ())
            conn.execute(
                "UPDATE nodes SET chunk_count=? WHERE id=?", (len(batch.items), path_id(norm))
            )
        self._index_change(batch)
        log.debug("memory write: %s (%d chars)", norm, len(content))
        node = self.node(norm)
        assert node is not None
        return node

    def mkdir(self, path: str, *, description: str = "") -> MemoryNode:
        norm = normalize_memory_path(path)
        if norm == "/":
            node = self.node("/")
            assert node is not None
            return node
        with self._transaction() as conn:
            parent_id = self._ensure_ancestors(conn, norm)
            now = time.time()
            conn.execute(
                "INSERT INTO nodes(id, type, name, description, parent_id, path, updated_at, created_at)"
                " VALUES (?, 'dir', ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET description=excluded.description, updated_at=excluded.updated_at",
                (path_id(norm), Path(norm).name, description, parent_id, norm, now, now),
            )
        node = self.node(norm)
        assert node is not None
        return node

    def delete(self, path: str) -> bool:
        norm = normalize_memory_path(path)
        if norm == "/":
            raise ValueError("不能删除记忆根目录")
        batch = ChunkBatch()
        with self._transaction() as conn:
            row = self._one("SELECT type FROM nodes WHERE id=?", (path_id(norm),))
            if row is None:
                return False
            if str(row["type"]) == "dir":
                prefix = norm + "/"
                pattern = escape_like(prefix) + "%"
                batch = self._subtree_chunk_batch(conn, norm, pattern)
                rows = conn.execute(
                    "SELECT id, path FROM nodes WHERE path=? OR path LIKE ? ESCAPE '\\'",
                    (norm, pattern),
                ).fetchall()
                for item in rows:
                    if item["id"] == "path:/":
                        continue
                    conn.execute("DELETE FROM nodes WHERE id=?", (item["id"],))
                    item_path = str(item["path"] or "")
                    if item_path:
                        cp = self._content_path(item_path)
                        if cp.is_file():
                            cp.unlink(missing_ok=True)
                target = self._content_path(norm)
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            else:
                batch = self._subtree_chunk_batch(conn, norm, "")
                conn.execute("DELETE FROM nodes WHERE id=?", (path_id(norm),))
                cp = self._content_path(norm)
                if cp.is_file():
                    cp.unlink(missing_ok=True)
        self._index_change(batch)
        return True

    def _subtree_chunk_batch(
        self, conn: sqlite3.Connection, norm: str, pattern: str
    ) -> ChunkBatch:
        """删节点前先抄下它的 chunk id（FK 级联会一起删掉，索引要跟着清）。"""
        if pattern:
            rows = conn.execute(
                "SELECT c.chunk_id FROM chunks c JOIN nodes n ON n.id = c.node_id"
                " WHERE n.path = ? OR n.path LIKE ? ESCAPE '\\'",
                (norm, pattern),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT chunk_id FROM chunks WHERE node_id=?", (path_id(norm),)
            ).fetchall()
        return ChunkBatch(tuple(str(row["chunk_id"]) for row in rows), ())

    # -- 元数据（标签 / 权重）--------------------------------------------

    def set_tags(
        self, path: str, tags: Iterable[str], *, managed_by: str | None = None
    ) -> MemoryNode:
        """整组替换标签（与既有实现一致：**不刷** ``updated_at``，避免污染增量维护）。"""
        norm = self._require_path(path)
        clean = [str(t).strip() for t in tags or () if str(t).strip()]
        with self._transaction() as conn:
            node_id = path_id(norm)
            conn.execute("DELETE FROM tags WHERE node_id=?", (node_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO tags(node_id, tag) VALUES (?, ?)",
                [(node_id, tag) for tag in clean],
            )
            if managed_by is not None:
                conn.execute(
                    "UPDATE nodes SET managed_by=? WHERE id=?", (str(managed_by), node_id)
                )
        node = self.node(norm)
        assert node is not None
        return node

    def set_score_patch(self, path: str, score_patch: float) -> MemoryNode:
        """设置检索权重补丁（``[-0.15, +0.15]``，越界报错；同样不刷 ``updated_at``）。"""
        norm = self._require_path(path)
        patch = float(score_patch)
        if not math.isfinite(patch):
            raise ValueError("score_patch 必须是有限数值")
        if patch < MIN_SCORE_PATCH or patch > MAX_SCORE_PATCH:
            raise ValueError(
                f"score_patch 超出范围 [{MIN_SCORE_PATCH}, {MAX_SCORE_PATCH}]: {patch}"
            )
        with self._transaction() as conn:
            conn.execute(
                "UPDATE nodes SET score_patch=?, score_patch_updated_at=? WHERE id=?",
                (patch, time.time(), path_id(norm)),
            )
        node = self.node(norm)
        assert node is not None
        return node

    # -- 附件（图片等二进制）-----------------------------------------------

    def write_attachment(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "image/png",
        description: str = "",
        declared_by: str = "agent",
    ) -> MemoryNode:
        """写一个二进制附件；描述会被当作可检索内容（与既有实现一致）。

        正文是字节，所以不进分块；但 ``[image:<type>] <description>`` 会生成一个
        chunk，让"搜图"能落到这条附件上。
        """
        norm = normalize_memory_path(path)
        if norm == "/":
            raise ValueError("不能写记忆根目录")
        cp = self._content_path(norm)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(bytes(data))
        now = time.time()
        desc = str(description or "")
        kind = str(content_type or "application/octet-stream")
        with self._transaction() as conn:
            parent_id = self._ensure_ancestors(conn, norm)
            existing = self._one("SELECT type FROM nodes WHERE id=?", (path_id(norm),))
            if existing is None:
                conn.execute(
                    "INSERT INTO nodes(id, type, name, description, content_type, parent_id,"
                    " path, declared_by, updated_at, created_at, chunk_count, indexed)"
                    " VALUES (?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)",
                    (
                        path_id(norm),
                        Path(norm).name,
                        desc,
                        kind,
                        parent_id,
                        norm,
                        declared_by,
                        now,
                        now,
                    ),
                )
            elif str(existing["type"]) == "dir":
                raise IsADirectoryError(f"是目录不是文件: {norm}")
            else:
                conn.execute(
                    "UPDATE nodes SET parent_id=?, declared_by=?, updated_at=?,"
                    " description=?, content_type=?, indexed=1 WHERE id=?",
                    (parent_id, declared_by, now, desc, kind, path_id(norm)),
                )
            if desc:
                batch = self._replace_chunks_locked(
                    conn, norm, f"[image:{kind}] {desc}", description=None, now=now
                )
            else:
                old_ids = tuple(
                    str(row["chunk_id"])
                    for row in conn.execute(
                        "SELECT chunk_id FROM chunks WHERE node_id=?", (path_id(norm),)
                    )
                )
                conn.execute("DELETE FROM chunks WHERE node_id=?", (path_id(norm),))
                batch = ChunkBatch(old_ids, ())
            conn.execute(
                "UPDATE nodes SET chunk_count=? WHERE id=?", (len(batch.items), path_id(norm))
            )
        self._index_change(batch)
        log.debug("memory attachment: %s (%d bytes, %s)", norm, len(data), kind)
        node = self.node(norm)
        assert node is not None
        return node

    # -- 变更流 -------------------------------------------------------------

    def changed_nodes(
        self,
        since_ts: float,
        *,
        scope: str = "/",
        tags: Iterable[str] = (),
        include_entities: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``since_ts`` 之后被写过的节点（维护 Agent / 增量同步用，**不是模型工具**）。

        注意 ``updated_at`` 也会被标签/权重刷新，所以结果可能包含"只改了标签"的节点。
        """
        scope_path = normalize_memory_path(scope)
        wanted = {str(t).strip().casefold() for t in tags if str(t).strip()}
        kinds = ("file", "dir", "entity") if include_entities else ("file", "dir")
        marks = ",".join("?" * len(kinds))
        rows = self._all(
            "SELECT id, type, path, name, updated_at, score_patch FROM nodes"
            f" WHERE type IN ({marks}) AND updated_at IS NOT NULL AND updated_at >= ?"
            " ORDER BY updated_at DESC",
            (*kinds, float(since_ts)),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            kind = str(row["type"])
            path = str(row["path"] or "")
            if kind in ("file", "dir"):
                if not path or not self._in_scope(path, scope_path):
                    continue
            node_tags = self.tags_of(path) if kind != "entity" else ()
            if wanted and not wanted <= {t.casefold() for t in node_tags}:
                continue
            item: dict[str, Any] = {
                "updated_at": epoch_to_iso(row["updated_at"]),
                "tags": list(node_tags),
                "score_patch": float(row["score_patch"] or 0.0),
                "type": kind,
            }
            if kind == "entity":
                item["id"] = str(row["id"])
                item["name"] = str(row["name"] or "")
            else:
                item["path"] = path
            results.append(item)
        if limit is not None and int(limit) > 0:
            results = results[: int(limit)]
        return results

    # -- 任务队列（长期维护用；表与旧库同名同结构）---------------------------

    def add_task(self, task_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        task_id = f"tsk_{uuid.uuid4().hex}"
        body = dict(payload or {})
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, type, status, payload, created_at, updated_at, error)"
                " VALUES (?, ?, 'pending', ?, ?, ?, '')",
                (task_id, str(task_type), json.dumps(body, ensure_ascii=False), now, now),
            )
        return {
            "task_id": task_id,
            "type": str(task_type),
            "status": "pending",
            "payload": body,
            "created_at": epoch_to_iso(now),
            "updated_at": epoch_to_iso(now),
            "error": "",
        }

    def update_task(self, task_id: str, status: str, error: str = "") -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (str(status), str(error or ""), time.time(), str(task_id)),
            )

    def get_tasks(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT task_id, type, status, payload, created_at, updated_at, error FROM tasks"
            " ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {
                "task_id": str(row["task_id"]),
                "type": str(row["type"]),
                "status": str(row["status"]),
                "payload": json.loads(row["payload"] or "{}"),
                "created_at": epoch_to_iso(row["created_at"]),
                "updated_at": epoch_to_iso(row["updated_at"]),
                "error": str(row["error"] or ""),
            }
            for row in rows
        ]

    # -- 目录树：重命名 / 移动 / 复制 -------------------------------------

    def rename(self, path: str, new_name: str) -> str:
        """同目录内改名（文件或整个子树）；返回新路径。"""
        norm = self._require_path(path)
        if norm == "/":
            raise ValueError("不能重命名记忆根目录")
        parent = posixpath.dirname(norm) or "/"
        new_path = normalize_memory_path(posixpath.join(parent, self._clean_name(new_name)))
        if new_path == norm:
            return norm
        self._relocate(norm, new_path)
        return new_path

    def move(self, path: str, dest_dir: str) -> str:
        """把文件/目录移进 ``dest_dir``（保留原名）；返回新路径。"""
        norm = self._require_path(path)
        if norm == "/":
            raise ValueError("不能移动记忆根目录")
        dest = normalize_memory_path(posixpath.join(dest_dir, posixpath.basename(norm)))
        if dest == norm:
            return norm
        self._relocate(norm, dest)
        return dest

    def copy(self, src: str, dest: str) -> str:
        """复制文件或整个子树到 ``dest``（``dest`` 必须不存在）；返回 ``dest``。"""
        source = self._require_path(src)
        target = normalize_memory_path(dest)
        if source == target:
            raise ValueError("源路径与目标路径相同")
        if self.node(target) is not None:
            raise FileExistsError(f"目标路径已存在: {target}")
        if target.startswith(source + "/"):
            raise ValueError(f"不能把 {source} 复制进它自己的子树: {target}")
        rows = self._subtree_rows(source)
        batches: list[ChunkBatch] = []
        with self._transaction() as conn:
            self._ensure_ancestors(conn, target)
            for row in rows:
                old_path = str(row["path"])
                new_path = target + old_path[len(source) :]
                if str(row["type"]) == "dir":
                    conn.execute(
                        "INSERT INTO nodes(id, type, name, description, parent_id, path,"
                        " declared_by, updated_at, created_at)"
                        " VALUES (?, 'dir', ?, ?, ?, ?, ?, ?, ?)",
                        (
                            path_id(new_path),
                            posixpath.basename(new_path),
                            str(row["description"] or ""),
                            self._parent_id_for(conn, new_path),
                            new_path,
                            str(row["declared_by"] or ""),
                            time.time(),
                            time.time(),
                        ),
                    )
                    continue
                batch = self._copy_file_row(conn, row, new_path)
                if batch is not None:
                    batches.append(batch)
        self._index_change(*batches)
        return target

    def _clean_name(self, value: str) -> str:
        name = str(value or "").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"非法文件名: {value!r}")
        return name

    def _require_path(self, path: str) -> str:
        norm = normalize_memory_path(path)
        if self.node(norm) is None:
            raise FileNotFoundError(f"记忆节点不存在: {norm}")
        return norm

    def _parent_id_for(self, conn: sqlite3.Connection, norm: str) -> str:
        parent = posixpath.dirname(norm) or "/"
        return path_id(parent)

    def _subtree_rows(self, norm: str) -> list[sqlite3.Row]:
        """``norm`` 自身 + 全部后代行（按路径升序，父在前）。"""
        return self._all(
            "SELECT id, type, path, name, description, declared_by, content_type, indexed,"
            " chunk_count, score_patch, managed_by"
            " FROM nodes WHERE path=? OR path LIKE ? ESCAPE '\\' ORDER BY path",
            (norm, escape_like(norm + "/") + "%"),
        )

    def _copy_file_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row, dest: str
    ) -> ChunkBatch | None:
        """复制单个文件行：正文文件搬字节、节点行照抄、标签跟着复制、重新分块。"""
        src_path = str(row["path"])
        src_content = self._content_path(src_path)
        dst_content = self._content_path(dest)
        dst_content.parent.mkdir(parents=True, exist_ok=True)
        if src_content.is_file():
            shutil.copy2(src_content, dst_content)
        now = time.time()
        conn.execute(
            "INSERT INTO nodes(id, type, name, description, content_type, parent_id, path,"
            " declared_by, updated_at, created_at, score_patch, managed_by, indexed)"
            " VALUES (?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                path_id(dest),
                posixpath.basename(dest),
                str(row["description"] or ""),
                row["content_type"],
                self._parent_id_for(conn, dest),
                dest,
                str(row["declared_by"] or ""),
                now,
                now,
                float(row["score_patch"] or 0.0),
                row["managed_by"],
            ),
        )
        for tag in self.tags_of(src_path):
            conn.execute(
                "INSERT OR IGNORE INTO tags(node_id, tag) VALUES (?, ?)", (path_id(dest), tag)
            )
        text = self._read_text_safe(dst_content)
        # ``indexed``/``chunk_count`` 任一为真 ⇒ 源文件本来是分块索引的，复制也要分块
        # （只看 ``indexed`` 会漏掉"rename 之后再 copy"的文件）。
        if text is not None and (int(row["indexed"] or 0) or int(row["chunk_count"] or 0)):
            batch = self._replace_chunks_locked(
                conn, dest, text, description=str(row["description"] or ""), now=now
            )
            conn.execute(
                "UPDATE nodes SET chunk_count=?, indexed=1 WHERE id=?", (len(batch.items), path_id(dest))
            )
            return batch
        return None

    @staticmethod
    def _read_text_safe(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _relocate(self, old: str, new: str) -> None:
        """把 ``old``（文件或子树）整体挪到 ``new``：正文文件 + 节点行 + 重新分块。"""
        if new.startswith(old + "/"):
            raise ValueError(f"不能把 {old} 移动进它自己的子树: {new}")
        if self.node(new) is not None:
            raise FileExistsError(f"目标路径已存在: {new}")
        rows = self._subtree_rows(old)
        old_content = self._content_path(old)
        new_content = self._content_path(new)
        new_content.parent.mkdir(parents=True, exist_ok=True)
        batches: list[ChunkBatch] = []
        with self._transaction() as conn:
            self._ensure_ancestors(conn, new)
            if old_content.exists():
                shutil.move(str(old_content), str(new_content))
            now = time.time()
            moved_files: list[tuple[str, str]] = []
            for row in rows:
                old_path = str(row["path"])
                new_path = new + old_path[len(old) :]
                conn.execute(
                    "UPDATE nodes SET id=?, name=?, path=?, parent_id=?, updated_at=?"
                    " WHERE id=?",
                    (
                        path_id(new_path),
                        posixpath.basename(new_path),
                        new_path,
                        self._parent_id_for(conn, new_path),
                        now,
                        path_id(old_path),
                    ),
                )
                if str(row["type"]) != "dir":
                    moved_files.append((new_path, str(row["description"] or "")))
            # chunk_id 里带路径，改名后必须重算（真源是正文文件，重算幂等）
            for new_path, description in moved_files:
                text = self._read_text_safe(self._content_path(new_path))
                if text is None:
                    continue
                batch = self._replace_chunks_locked(
                    conn, new_path, text, description=description, now=now
                )
                conn.execute(
                    "UPDATE nodes SET chunk_count=?, indexed=? WHERE id=?",
                    (len(batch.items), 1 if batch.items else 0, path_id(new_path)),
                )
                batches.append(batch)
        self._index_change(*batches)
        log.info("memory relocate: %s -> %s（%d 行）", old, new, len(rows))

    def _ensure_ancestors(self, conn: sqlite3.Connection, norm: str) -> str:
        parts = [p for p in norm.split("/") if p]
        current = "/"
        node_id = "path:/"
        for part in parts[:-1]:
            current = current.rstrip("/") + "/" + part
            child_id = path_id(current)
            if self._one("SELECT id FROM nodes WHERE id=?", (child_id,)) is None:
                now = time.time()
                conn.execute(
                    "INSERT INTO nodes(id, type, name, parent_id, path, updated_at, created_at)"
                    " VALUES (?, 'dir', ?, ?, ?, ?, ?)",
                    (child_id, part, node_id, current, now, now),
                )
            node_id = child_id
        return node_id

    def _replace_chunks_locked(
        self,
        conn: sqlite3.Connection,
        norm: str,
        content: str,
        *,
        description: str | None,
        now: float,
    ) -> ChunkBatch:
        node_id = path_id(norm)
        old_ids = tuple(
            str(row["chunk_id"])
            for row in conn.execute("SELECT chunk_id FROM chunks WHERE node_id=?", (node_id,))
        )
        conn.execute("DELETE FROM chunks WHERE node_id=?", (node_id,))
        index_text = content
        if description:
            index_text = f"{description}\n\n{content}"
        scope = str(Path(norm).parent.as_posix())
        scope = "/" if scope in (".", "") else scope
        items: list[dict[str, Any]] = []
        for index, piece in enumerate(chunk_text(index_text), 1):
            chunk_id = f"{norm}::chunk::{index}::{uuid.uuid4().hex[:8]}"
            preview = piece[:120]
            conn.execute(
                "INSERT INTO chunks(chunk_id, node_id, chunk_index, text, text_preview,"
                " scope_prefix, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, node_id, index, piece, preview, scope, now),
            )
            items.append(
                {
                    "chunk_id": chunk_id,
                    "node_path": norm,
                    "scope_prefix": scope,
                    "chunk_index": index,
                    "text": piece,
                    "text_preview": preview,
                }
            )
        return ChunkBatch(old_ids, tuple(items))

    # -- 派生索引（关键词 FTS5 / 向量任务队列）----------------------------

    @property
    def fts(self) -> FtsIndex | None:
        """关键词倒排索引（惰性创建；``keyword_index=False`` 时为 ``None``）。"""
        return self._ensure_fts()

    def _ensure_fts(self) -> FtsIndex | None:
        if not self.keyword_index:
            return None
        if self._fts is None:
            self._fts = FtsIndex(self.index_dir / "fts.sqlite", cache_mb=self.fts_cache_mb)
        return self._fts

    def _index_change(self, *batches: ChunkBatch) -> None:
        """把分块改动同步进派生索引（在**主库事务提交之后**调用）。

        索引是派生数据：这里失败只记日志并把索引标脏（下次 ``reindex_keyword``
        全量重建），不能让"正文已经写进去了"的写操作反过来报失败。
        """
        fts = self._ensure_fts()
        if fts is None:
            return
        old_ids: list[str] = []
        items: list[dict[str, Any]] = []
        for batch in batches:
            old_ids.extend(batch.old_ids)
            items.extend(batch.items)
        if not old_ids and not items:
            return
        try:
            fts.apply(old_ids=old_ids, items=items, vector_jobs=self.vector_index)
        except sqlite3.Error:
            log.error("记忆关键词索引更新失败（下次 reindex_keyword 会重建）", exc_info=True)
            fts.mark_stale()

    def chunk_rows(self) -> Iterator[dict[str, Any]]:
        """逐页流式吐 ``chunks`` 行（重建索引 / 向量自愈用，不整表进内存）。"""
        after = 0
        while True:
            rows = self._all(
                "SELECT c.rowid AS rid, c.chunk_id, c.chunk_index, c.text, c.text_preview,"
                " c.scope_prefix, n.path AS node_path"
                " FROM chunks c JOIN nodes n ON n.id = c.node_id"
                " WHERE c.rowid > ? ORDER BY c.rowid LIMIT 500",
                (after,),
            )
            if not rows:
                return
            for row in rows:
                after = int(row["rid"])
                path = str(row["node_path"] or "")
                if not path:
                    continue
                yield {
                    "chunk_id": str(row["chunk_id"]),
                    "node_path": path,
                    "scope_prefix": str(row["scope_prefix"] or "/"),
                    "chunk_index": int(row["chunk_index"] or 0),
                    "text": str(row["text"] or ""),
                    "text_preview": str(row["text_preview"] or ""),
                }

    def iter_chunk_ids(self) -> Iterator[str]:
        """流式吐全部 chunk id（向量索引对账用，不整表进内存）。"""
        after = 0
        while True:
            rows = self._all(
                "SELECT rowid AS rid, chunk_id FROM chunks WHERE rowid > ?"
                " ORDER BY rowid LIMIT 1000",
                (after,),
            )
            if not rows:
                return
            for row in rows:
                after = int(row["rid"])
                yield str(row["chunk_id"])

    def existing_chunk_ids(self, chunk_ids: Iterable[str]) -> set[str]:
        """这些 chunk id 里哪些还在（向量队列里可能有已经删掉的 chunk）。"""
        wanted = [str(i) for i in chunk_ids]
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            page = wanted[start : start + 500]
            marks = ",".join("?" * len(page))
            rows = self._all(f"SELECT chunk_id FROM chunks WHERE chunk_id IN ({marks})", page)
            found.update(str(row["chunk_id"]) for row in rows)
        return found

    def chunk_rows_by_ids(self, chunk_ids: Iterable[str]) -> list[dict[str, Any]]:
        """按 id 取 chunk 行（补投向量任务用）。"""
        wanted = [str(i) for i in chunk_ids]
        result: list[dict[str, Any]] = []
        for start in range(0, len(wanted), 500):
            page = wanted[start : start + 500]
            marks = ",".join("?" * len(page))
            rows = self._all(
                "SELECT c.chunk_id, c.chunk_index, c.text, c.text_preview, c.scope_prefix,"
                " n.path AS node_path FROM chunks c JOIN nodes n ON n.id = c.node_id"
                f" WHERE c.chunk_id IN ({marks})",
                page,
            )
            for row in rows:
                path = str(row["node_path"] or "")
                if not path:
                    continue
                result.append(
                    {
                        "chunk_id": str(row["chunk_id"]),
                        "node_path": path,
                        "scope_prefix": str(row["scope_prefix"] or "/"),
                        "chunk_index": int(row["chunk_index"] or 0),
                        "text": str(row["text"] or ""),
                        "text_preview": str(row["text_preview"] or ""),
                    }
                )
        return result

    def reindex_keyword(self) -> int:
        """从 ``chunks`` 全量重建关键词索引，返回索引到的 chunk 数。"""
        fts = self._ensure_fts()
        if fts is None:
            return 0
        count = fts.rebuild(self.chunk_rows())
        log.info("记忆关键词索引重建完成: %d chunks", count)
        return count

    def keyword_index_status(self) -> dict[str, Any]:
        """索引体检：``stale`` 为真说明索引落后/损坏，该重建。"""
        fts = self._ensure_fts()
        if fts is None:
            return {"enabled": False, "stale": False, "indexed": 0, "chunks": 0, "pending_vectors": 0}
        chunks = self.chunk_count()
        return {
            "enabled": True,
            "stale": fts.is_stale(chunks),
            "indexed": fts.count(),
            "chunks": chunks,
            "pending_vectors": fts.job_count(),
        }

    def chunk_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM chunks")
        return int(row["n"] if row else 0)

    def preview_many(self, paths: Iterable[str]) -> dict[str, str]:
        """批量取每个节点第一块的分块预览（检索结果的 ``snippet``）。

        取第一块而不是"命中的那一块"：FTS5 里只存分词后的正文（省磁盘），
        没有原文可以回显；文档记忆普遍很短，第一块就是开头，够用。
        """
        norms: list[str] = []
        seen: set[str] = set()
        for raw in paths:
            norm = normalize_memory_path(raw)
            if norm in seen:
                continue
            seen.add(norm)
            norms.append(norm)
        result: dict[str, str] = {}
        for start in range(0, len(norms), 500):
            page = norms[start : start + 500]
            marks = ",".join("?" * len(page))
            rows = self._all(
                "SELECT n.path AS path, c.text_preview AS preview"
                " FROM chunks c JOIN nodes n ON n.id = c.node_id"
                f" WHERE n.path IN ({marks}) AND c.chunk_index = 1",
                page,
            )
            for row in rows:
                result[str(row["path"])] = str(row["preview"] or "")
        return result

    def meta_many(self, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批量取检索后要用的元数据（一次 ``IN`` 查询，别按路径逐条查）。

        返回 ``{path: {name, kind, description, updated_at, updated_ts, tags,
        score_patch, content_type, chunk_count}}``；不存在的路径不出现在结果里。
        """
        norms: list[str] = []
        seen: set[str] = set()
        for raw in paths:
            norm = normalize_memory_path(raw)
            if norm in seen:
                continue
            seen.add(norm)
            norms.append(norm)
        if not norms:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(norms), 500):  # 避开 SQLite 变量数上限
            page = norms[start : start + 500]
            ids = [path_id(p) for p in page]
            marks = ",".join("?" * len(ids))
            rows = self._all(
                "SELECT id, path, type, name, description, updated_at, score_patch,"
                f" content_type, chunk_count FROM nodes WHERE id IN ({marks})",
                ids,
            )
            tags: dict[str, list[str]] = {}
            for tag_row in self._all(
                f"SELECT node_id, tag FROM tags WHERE node_id IN ({marks}) ORDER BY tag", ids
            ):
                tags.setdefault(str(tag_row["node_id"]), []).append(str(tag_row["tag"]))
            for row in rows:
                path = str(row["path"] or "")
                if not path:
                    continue
                result[path] = {
                    "path": path,
                    "name": str(row["name"] or ""),
                    "kind": "dir" if str(row["type"]) == "dir" else "file",
                    "description": str(row["description"] or ""),
                    "updated_at": epoch_to_iso(row["updated_at"]),
                    "updated_ts": float(row["updated_at"] or 0.0),
                    "score_patch": float(row["score_patch"] or 0.0),
                    "content_type": str(row["content_type"] or ""),
                    "chunk_count": int(row["chunk_count"] or 0),
                    "tags": tuple(tags.get(str(row["id"]), ())),
                }
        return result

    def _content_path(self, norm: str) -> Path:
        return self.content_dir / norm.strip("/")

    # -- 检索 -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        scope: str = "/",
        top_k: int = 5,
        tags: Iterable[str] = (),
    ) -> list[SearchHit]:
        tokens = tokenize(query)
        if not tokens:
            return []
        scope_norm = normalize_memory_path(scope)
        wanted_tags = {str(t).strip().lower() for t in tags if str(t).strip()}

        candidates: dict[str, dict[str, Any]] = {}
        total_nodes = max(1, int((self._one("SELECT COUNT(*) AS n FROM nodes WHERE type='file'") or {"n": 1})["n"]))
        for token in tokens:
            pattern = "%" + escape_like(token) + "%"
            rows = self._all(
                "SELECT c.chunk_id, c.node_id, c.text, c.chunk_index, c.updated_at,"
                " n.path, n.name, n.description, n.score_patch"
                " FROM chunks c JOIN nodes n ON n.id = c.node_id"
                " WHERE c.text LIKE ? ESCAPE '\\' LIMIT 200",
                (pattern,),
            )
            for row in rows:
                path = str(row["path"] or "")
                if not path or not self._in_scope(path, scope_norm):
                    continue
                bucket = candidates.setdefault(
                    path,
                    {
                        "chunk": str(row["text"] or ""),
                        "chunk_index": int(row["chunk_index"] or 0),
                        "name": str(row["name"] or ""),
                        "description": str(row["description"] or ""),
                        "updated_at": row["updated_at"],
                        "score_patch": float(row["score_patch"] or 0.0),
                        "hits": 0.0,
                    },
                )
                bucket["hits"] += 1.0

        if not candidates:
            return []

        if wanted_tags:  # 标签是过滤条件，不是加权项（与既有实现的 tags 参数语义一致）
            candidates = {
                path: bucket
                for path, bucket in candidates.items()
                if wanted_tags & {t.lower() for t in self.tags_of(path)}
            }
            if not candidates:
                return []

        idf = math.log((total_nodes + 1) / (len(candidates) + 1)) + 1.0
        hits: list[SearchHit] = []
        for path, bucket in candidates.items():
            chunk_lower = str(bucket["chunk"]).lower()
            meta_lower = f"{bucket['name']} {bucket['description']}".lower()
            tf = sum(chunk_lower.count(t) for t in tokens) + 0.5 * sum(
                meta_lower.count(t) for t in tokens
            )
            score = (tf / max(1.0, len(chunk_lower) / 400.0)) * idf
            node_tags = self.tags_of(path)
            if wanted_tags and wanted_tags & {t.lower() for t in node_tags}:
                score += 1.5
            if any(token in path.lower() for token in tokens):
                score += 1.0
            age_days = max(0.0, (time.time() - float(bucket["updated_at"] or 0.0)) / 86400.0)
            score += 0.2 * math.exp(-age_days / 30.0)
            score += float(bucket["score_patch"])
            hits.append(
                SearchHit(
                    path=path,
                    score=score,
                    snippet=str(bucket["chunk"])[:300],
                    description=str(bucket["description"]),
                    updated_at=epoch_to_iso(bucket["updated_at"]),
                    tags=node_tags,
                    chunk_index=int(bucket["chunk_index"]),
                )
            )
        hits.sort(key=lambda h: (-h.score, h.path))
        return hits[: max(1, int(top_k))]

    @staticmethod
    def _in_scope(path: str, scope: str) -> bool:
        if scope == "/":
            return True
        return path == scope or path.startswith(scope.rstrip("/") + "/")

    # -- 多条件检索（advanced_search，与既有实现同语义）--------------------

    def advanced_search(
        self,
        query: str | None = None,
        *,
        scope: str = "/",
        tags: Iterable[str] = (),
        tag_logic: str = "AND",
        date_from: str | None = None,
        date_to: str | None = None,
        declared_by: str | None = None,
        content_type: str | None = None,
        top_k: int = 20,
        sort_by: str = "relevance",
        sort_order: str = "desc",
    ) -> list[dict[str, Any]]:
        """多条件组合检索：``query`` 可为空（纯条件筛选）。

        条件：``scope`` / ``tags``（``tag_logic=AND|OR``）/ 更新日期区间
        （``YYYY-MM-DD``，按 UTC 日期比较）/ ``declared_by`` / ``content_type``
        （``text`` / ``image`` 做前缀判断，其它值忽略，与既有实现一致）。

        排序：``relevance``（仅在给了 ``query`` 时有效）/ ``updated_at`` /
        ``created_at``（旧库没记创建时间，退回 ``updated_at``）。

        与既有实现的唯一偏差：**只返回文件**（既有实现把目录节点也塞进结果，检索语义上
        没有意义；目录请用 ``children`` / VFS 列表）。

        文本匹配分（无向量，纯字符串）：路径命中 1.0 > 描述命中 0.8 > 正文前 5000
        字符命中 0.6；给了 ``query`` 却都没命中则跳过该条。
        """
        scope_norm = normalize_memory_path(scope)
        required = {str(t).strip().casefold() for t in tags or () if str(t).strip()}
        logic = str(tag_logic or "AND").upper()
        if logic not in ("AND", "OR"):
            raise ValueError(f"tag_logic 只支持 AND / OR: {tag_logic!r}")
        text = str(query or "").strip().lower()

        rows = self._all(
            "SELECT path, type, description, declared_by, updated_at, score_patch, content_type"
            " FROM nodes WHERE type='file' AND path IS NOT NULL"
            " ORDER BY updated_at DESC"
        )
        candidates: list[dict[str, Any]] = []
        for row in rows:
            path = normalize_memory_path(str(row["path"]))
            if path == "/":
                continue  # 根目录不是文档
            if not self._in_scope(path, scope_norm):
                continue
            node_tags = [t.casefold() for t in self.tags_of(path)]
            if required:
                if logic == "AND" and not required.issubset(node_tags):
                    continue
                if logic == "OR" and not required.intersection(node_tags):
                    continue
            updated = epoch_to_iso(row["updated_at"])
            updated_date = updated[:10]
            if date_from and updated_date and updated_date < str(date_from):
                continue
            if date_to and updated_date and updated_date > str(date_to):
                continue
            if declared_by and str(row["declared_by"] or "") != str(declared_by):
                continue
            ctype = str(row["content_type"] or "")
            want = str(content_type or "").strip().lower()
            if want == "text" and ctype and not ctype.startswith("text/"):
                continue
            if want == "image" and ctype and not ctype.startswith("image/"):
                continue

            candidate: dict[str, Any] = {
                "path": path,
                "uri": "memories://" + path.lstrip("/"),
                "description": str(row["description"] or ""),
                "tags": self.tags_of(path),
                "updated_at": updated,
                "declared_by": str(row["declared_by"] or ""),
                "score_patch": float(row["score_patch"] or 0.0),
                "score": 0.0,
                "line_count": None,
            }
            if text:
                score = 0.0
                if text in posixpath.basename(path).lower():
                    score = 1.0
                if text in candidate["description"].lower():
                    score = max(score, 0.8)
                body = self._read_text_safe(self._content_path(path))
                if score < 0.5 and body is not None and text in body[:5000].lower():
                    score = max(score, 0.6)
                if score == 0.0:
                    continue
                candidate["score"] = score
            body = self._read_text_safe(self._content_path(path))
            candidate["line_count"] = len(body.splitlines()) if body is not None else None
            candidates.append(candidate)

        if sort_by == "relevance" and text:
            candidates.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        elif sort_by in ("updated_at", "created_at"):
            candidates.sort(
                key=lambda item: item.get("updated_at") or "",
                reverse=(str(sort_order).lower() != "asc"),
            )
        return candidates[: max(1, int(top_k))]


__all__ = [
    "CHUNK_OVERLAP_CHARS",
    "MAX_CHUNK_CHARS",
    "MAX_SCORE_PATCH",
    "MIN_SCORE_PATCH",
    "SCHEMA_VERSION",
    "MemoryEntry",
    "MemoryNode",
    "SearchHit",
    "SqliteMemoryStore",
    "chunk_text",
    "epoch_to_iso",
    "escape_like",
    "iso_to_epoch",
    "normalize_memory_path",
    "path_id",
    "tokenize",
]
