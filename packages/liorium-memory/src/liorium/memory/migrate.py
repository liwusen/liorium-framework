"""旧 JSON 记忆库（``graph.json`` + ``meta/``）→ SQLite 的一次性迁移。

只在**很老的库**上需要跑（那种库还没有 ``memory.sqlite``）：新版库本来就是这个
schema，Liorium 直接读。触发条件是 ``needs_migration()`` 为真——
``<memory>/graph.json`` 存在而库里只有根节点。

迁移是**全有或全无**的：所有写入在同一个事务里，校验不过就整体回滚，不会留下半成品。
迁移完把旧文件挪进 ``_legacy_json/``（不删，出问题还能回查）。

旧格式（networkx 存出来的）::

    graph.json  = {"nodes": {"path:/a.md": {...attrs...}, ...},
                   "edges": [{"source": ..., "target": ..., "type": ..., "key": ...}]}
    meta/x.meta.json, meta/chunks_index.json, meta/**/*.chunks.json
    index/entity_vecs.jsonl      # {"id": ..., "vector": [...]}

**偏差**：分块优先用旧索引里的；旧索引缺了就从正文重切（chunk_id 会变，但分块是
派生数据，重切无损）。实体名向量只在装了 ``[vector]`` 时迁移，否则留给后续重算。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from liorium.core.log import get_logger

from .store import SqliteMemoryStore, iso_to_epoch, normalize_memory_path, path_id

log = get_logger("liorium.memory.migrate")

LEGACY_GRAPH = "graph.json"
LEGACY_META_DIR = "meta"
LEGACY_ENTITY_VECS = Path("index") / "entity_vecs.jsonl"
ARCHIVE_DIRNAME = "_legacy_json"
TREE_EDGE = "has_child"


class MigrationError(RuntimeError):
    """迁移失败：源文件损坏或校验不一致（不静默降级）。"""


@dataclass(frozen=True)
class MigrationReport:
    migrated: bool
    counts: dict[str, int] = field(default_factory=dict)
    archived_to: str | None = None
    warnings: tuple[str, ...] = ()


def needs_migration(store: SqliteMemoryStore) -> bool:
    """有旧 ``graph.json`` 且库里还没有业务节点 ⇒ 需要迁移。"""
    if not (store.path.parent / LEGACY_GRAPH).exists():
        return False
    row = store._one("SELECT COUNT(*) AS n FROM nodes WHERE id <> 'path:/'")  # noqa: SLF001
    return int(row["n"] if row else 0) == 0


def migrate_legacy_json(
    store: SqliteMemoryStore,
    *,
    force: bool = False,
    migrate_entity_vectors: bool = True,
) -> MigrationReport:
    """执行迁移；``force=True`` 时即使库非空也迁移（覆盖同 id 节点）。"""
    root = store.path.parent
    graph_file = root / LEGACY_GRAPH
    if not graph_file.exists():
        if force:
            raise MigrationError(f"没有旧格式记忆库: {graph_file}")
        return MigrationReport(migrated=False, counts={})
    if not force and not needs_migration(store):
        return MigrationReport(migrated=False, counts={})

    try:
        graph = json.loads(graph_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"{LEGACY_GRAPH} 解析失败，迁移中止: {exc}") from exc
    nodes: dict[str, dict[str, Any]] = dict(graph.get("nodes") or {})
    edges: list[dict[str, Any]] = list(graph.get("edges") or [])
    if not nodes:
        raise MigrationError(f"{graph_file} 里没有节点")

    meta_items = list(_iter_meta(root / LEGACY_META_DIR))
    legacy_chunks = _load_legacy_chunks(root / LEGACY_META_DIR)
    tasks = _read_json(root / LEGACY_META_DIR / "tasks.json", []) or []
    entity_vecs = _load_entity_vecs(root)

    counts = {
        "nodes": len(nodes),
        "edges": 0,
        "parent_edges": 0,
        "tags": 0,
        "chunks": 0,
        "rechunked": 0,
        "tasks": 0,
        "entity_vecs": len(entity_vecs),
    }
    warnings: list[str] = []

    with store._transaction() as conn:  # noqa: SLF001 - 迁移只碰库，不碰派生索引
        for nid, attrs in nodes.items():
            conn.execute(_NODE_INSERT, _node_columns(dict(attrs or {}), str(nid)))

        for edge in edges:
            src = str(edge.get("source") or "")
            dst = str(edge.get("target") or "")
            rel_type = str(edge.get("type") or "relates_to")
            if not src or not dst:
                continue
            if rel_type == TREE_EDGE and src.startswith("path:") and dst.startswith("path:"):
                conn.execute("UPDATE nodes SET parent_id=? WHERE id=?", (src, dst))
                counts["parent_edges"] += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO edges(src, dst, type, key) VALUES (?, ?, ?, ?)",
                (src, dst, rel_type, str(edge.get("key") or "")),
            )
            counts["edges"] += 1

        for raw_path, meta in meta_items:
            norm = normalize_memory_path(raw_path)
            nid = path_id(norm)
            # graph.json 是树形状的真源；meta 比节点多说明是孤儿，不入库
            if conn.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone() is None:
                warnings.append(f"跳过孤儿元数据: {norm}")
                continue
            conn.execute(
                "UPDATE nodes SET declared_by=COALESCE(?, declared_by), description=?,"
                " updated_at=COALESCE(?, updated_at), score_patch=?,"
                " score_patch_updated_at=COALESCE(?, score_patch_updated_at),"
                " managed_by=COALESCE(?, managed_by), content_type=COALESCE(?, content_type)"
                " WHERE id=?",
                (
                    meta.get("declared_by"),
                    str(meta.get("description") or ""),
                    iso_to_epoch(meta.get("updated_at")),
                    float(meta.get("score_patch") or 0.0),
                    iso_to_epoch(meta.get("score_patch_updated_at")),
                    meta.get("managed_by"),
                    meta.get("content_type"),
                    nid,
                ),
            )
            for tag in [
                str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()
            ]:
                conn.execute(
                    "INSERT OR IGNORE INTO tags(node_id, tag) VALUES (?, ?)", (nid, tag)
                )
                counts["tags"] += 1

        for chunk_id, item in legacy_chunks.items():
            norm = normalize_memory_path(str(item.get("node_path") or ""))
            nid = path_id(norm)
            if conn.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone() is None:
                warnings.append(f"跳过分块（节点不存在）: {chunk_id}")
                continue
            conn.execute(
                "INSERT OR REPLACE INTO chunks(chunk_id, node_id, chunk_index, text,"
                " text_preview, scope_prefix, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    nid,
                    int(item.get("chunk_index") or 0),
                    str(item.get("text") or ""),
                    str(item.get("text_preview") or ""),
                    str(item.get("scope_prefix") or "/"),
                    iso_to_epoch(item.get("updated_at")),
                ),
            )
            counts["chunks"] += 1

        for task in tasks:
            conn.execute(
                "INSERT OR REPLACE INTO tasks(task_id, type, status, payload, created_at,"
                " updated_at, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task.get("task_id") or ""),
                    str(task.get("type") or ""),
                    str(task.get("status") or "pending"),
                    json.dumps(task.get("payload") or {}, ensure_ascii=False),
                    iso_to_epoch(task.get("created_at")),
                    iso_to_epoch(task.get("updated_at")),
                    str(task.get("error") or ""),
                ),
            )
            counts["tasks"] += 1

        _fixup_parents(store, conn)
        _verify(conn, counts)

    # 事务外：旧索引缺块的文件从正文重切（派生数据，重切无损）
    counts["rechunked"] = _rechunk_missing(store)
    store.reindex_keyword()

    archived: Path | None = None
    if migrate_entity_vectors and entity_vecs:
        archived = _write_entity_vectors(store, entity_vecs, warnings=warnings)
    if archived is None:
        archived = _archive_legacy(root)
    log.info("旧格式记忆迁移完成: %s", counts)
    return MigrationReport(
        migrated=True, counts=counts, archived_to=str(archived), warnings=tuple(warnings)
    )


# ---------------------------------------------------------------------------
# 行映射
# ---------------------------------------------------------------------------

_KNOWN_ATTRS = frozenset(
    {
        "type",
        "name",
        "description",
        "entity_type",
        "content_type",
        "declared_by",
        "updated_at",
        "created_at",
        "score_patch",
        "score_patch_updated_at",
        "managed_by",
        "chunk_count",
        "indexed",
        "path",
        "tags",
    }
)

_NODE_INSERT = (
    "INSERT OR REPLACE INTO nodes(id, type, name, description, entity_type, content_type,"
    " parent_id, path, declared_by, updated_at, created_at, score_patch,"
    " score_patch_updated_at, managed_by, chunk_count, indexed, data)"
    " VALUES (:id, :type, :name, :description, :entity_type, :content_type, :parent_id,"
    " :path, :declared_by, :updated_at, :created_at, :score_patch,"
    " :score_patch_updated_at, :managed_by, :chunk_count, :indexed, :data)"
)


def _id_to_path(nid: str) -> str:
    """``path:/a/b`` → ``/a/b``。"""
    return "/" + nid.split(":", 1)[1].lstrip("/") if ":" in nid else nid


def _node_columns(attrs: dict[str, Any], nid: str) -> dict[str, Any]:
    """把 graph.json 的节点属性拆成 SQL 列 + ``data`` JSON（未知键进 data）。"""
    extra = {
        key: value
        for key, value in attrs.items()
        if key not in _KNOWN_ATTRS and not str(key).startswith("_")
    }
    known = {
        key: attrs[key]
        for key, value in attrs.items()
        if key in ("properties", "kb_refs")
    }
    extra.update(known)
    return {
        "id": nid,
        "type": str(attrs.get("type") or "file"),
        "name": str(attrs.get("name") or ""),
        "description": str(attrs.get("description") or ""),
        "entity_type": attrs.get("entity_type"),
        "content_type": attrs.get("content_type"),
        "parent_id": None,
        "path": _id_to_path(nid) if nid.startswith("path:") else None,
        "declared_by": attrs.get("declared_by"),
        "updated_at": iso_to_epoch(attrs.get("updated_at")),
        "created_at": iso_to_epoch(attrs.get("created_at")),
        "score_patch": float(attrs.get("score_patch") or 0.0),
        "score_patch_updated_at": iso_to_epoch(attrs.get("score_patch_updated_at")),
        "managed_by": attrs.get("managed_by"),
        "chunk_count": int(attrs.get("chunk_count") or 0),
        "indexed": 1 if attrs.get("indexed") else 0,
        "data": json.dumps(extra, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# 读取旧文件
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"无法读取 {path}: {exc}") from exc


def _iter_meta(meta_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    if not meta_dir.exists():
        return iter(())
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(meta_dir.rglob("*.meta.json")):
        meta = _read_json(path, {}) or {}
        target = str(meta.get("path") or "").strip()
        if target:
            found.append((target, meta))
    return iter(found)


def _load_legacy_chunks(meta_dir: Path) -> dict[str, dict[str, Any]]:
    """``chunks_index.json`` 为准，``*.chunks.json`` 补缺。"""
    items: dict[str, dict[str, Any]] = {}
    index = _read_json(meta_dir / "chunks_index.json", {}) or {}
    for _, item in index.items():
        chunk_id = str((item or {}).get("chunk_id") or "")
        if chunk_id:
            items[chunk_id] = item
    for path in sorted(meta_dir.rglob("*.chunks.json")):
        for item in _read_json(path, []) or []:
            chunk_id = str((item or {}).get("chunk_id") or "")
            if chunk_id and chunk_id not in items:
                items[chunk_id] = item
    return items


def _load_entity_vecs(root: Path) -> dict[str, list[float]]:
    path = root / LEGACY_ENTITY_VECS
    if not path.exists():
        return {}
    vectors: dict[str, list[float]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        eid = str(record.get("id") or "")
        vector = record.get("vector") or record.get("__vector__")
        if eid and isinstance(vector, list) and vector:
            vectors[eid] = [float(value) for value in vector]
    return vectors


# ---------------------------------------------------------------------------
# 收尾
# ---------------------------------------------------------------------------


def _fixup_parents(store: SqliteMemoryStore, conn: Any) -> None:
    """按 path 推导 ``parent_id``（不能只信 has_child 边：旧库可能缺边）。"""
    rows = conn.execute(
        "SELECT id, path FROM nodes WHERE path IS NOT NULL AND path <> '/'"
    ).fetchall()
    for row in rows:
        path = str(row["path"])
        parent = path.rsplit("/", 1)[0] or "/"
        conn.execute(
            "UPDATE nodes SET parent_id=? WHERE id=?", (path_id(parent), row["id"])
        )


def _verify(conn: Any, counts: dict[str, int]) -> None:
    """一致性校验；不一致直接抛错，由外层事务整体回滚。"""
    total = int(conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"])
    if total < counts["nodes"]:
        raise MigrationError(f"节点数对不上: 源 {counts['nodes']} 库 {total}")
    for row in conn.execute(
        "SELECT id, path, parent_id FROM nodes WHERE path IS NOT NULL AND path <> '/'"
    ).fetchall():
        expected = path_id(str(row["path"]).rsplit("/", 1)[0] or "/")
        if row["parent_id"] != expected:
            raise MigrationError(
                f"父节点推导不一致: {row['id']} 实得 {row['parent_id']} 期望 {expected}"
            )
    for row in conn.execute("SELECT chunk_id, text FROM chunks LIMIT 5").fetchall():
        if not row["text"]:
            raise MigrationError(f"分块正文为空: {row['chunk_id']}")


def _rechunk_missing(store: SqliteMemoryStore) -> int:
    """旧索引里没有分块的文件，从正文重切（``chunk_count`` 为 0 的才算）。"""
    rows = store._all(  # noqa: SLF001
        "SELECT id, path, description FROM nodes WHERE type='file' AND path IS NOT NULL"
        " AND indexed=1 AND (chunk_count IS NULL OR chunk_count=0)"
    )
    rechunked = 0
    for row in rows:
        norm = str(row["path"])
        text = store._read_text_safe(store._content_path(norm))  # noqa: SLF001
        if text is None:
            continue
        with store._transaction() as conn:  # noqa: SLF001
            batch = store._replace_chunks_locked(  # noqa: SLF001
                conn, norm, text, description=str(row["description"] or ""), now=time.time()
            )
            conn.execute(
                "UPDATE nodes SET chunk_count=? WHERE id=?", (len(batch.items), row["id"])
            )
        rechunked += 1
    return rechunked


def _write_entity_vectors(
    store: SqliteMemoryStore, vectors: dict[str, list[float]], *, warnings: list[str]
) -> Path | None:
    """把旧实体名向量写成 ``index/entity.vdb``（与既有实现同格式）。"""
    try:
        from .vectors import EMBED_DIM, NanoVectorBackend, VectorRecord
    except ImportError:  # 没装 [vector]：留给后续重算
        warnings.append("未安装向量依赖，实体名向量未迁移（启用向量检索后会重算）")
        return None
    dim = len(next(iter(vectors.values())))
    if dim != EMBED_DIM:
        warnings.append(f"实体向量维度 {dim} != {EMBED_DIM}，跳过迁移（会在启用向量后重算）")
        return None
    if _in_loop():
        # 迁移是运维动作（CLI/启动期），不该在事件循环里被调用
        warnings.append("在事件循环里调用迁移，实体向量未迁移（请改用同步入口）")
        return None
    backend = NanoVectorBackend(store.index_dir / "entity.vdb", dim=dim)

    async def run() -> None:
        await backend.upsert(
            [VectorRecord(chunk_id=eid, vector=vec) for eid, vec in vectors.items()]
        )
        await backend.aclose()

    asyncio.run(run())
    return _archive_legacy(store.path.parent)


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _archive_legacy(root: Path) -> Path:
    """旧文件挪进 ``_legacy_json/``（不删）。"""
    archive = root / ARCHIVE_DIRNAME
    archive.mkdir(parents=True, exist_ok=True)
    graph_file = root / LEGACY_GRAPH
    if graph_file.exists():
        shutil.move(str(graph_file), str(archive / LEGACY_GRAPH))
    for name in (LEGACY_META_DIR,):
        source = root / name
        if source.exists():
            target = archive / name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(source), str(target))
    sidecar = root / LEGACY_ENTITY_VECS
    if sidecar.exists():
        target = archive / LEGACY_ENTITY_VECS
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sidecar), str(target))
    return archive


__all__ = [
    "ARCHIVE_DIRNAME",
    "LEGACY_GRAPH",
    "MigrationError",
    "MigrationReport",
    "migrate_legacy_json",
    "needs_migration",
]
