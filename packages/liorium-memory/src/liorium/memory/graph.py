"""实体 / 关系图：与旧库同一张 ``nodes`` / ``edges`` 表，不建 networkx 投影。

既有实现把全库灌进 ``networkx.MultiDiGraph``（每个节点 ≈ 1KB 的 dict），10 万节点
就是 100MB 常驻；这里一律走 SQL：

- 邻居 / N 跳用**递归 CTE**（``edges(src,type)`` / ``edges(dst,type)`` 都有索引）；
- 实体详情、关系计数是单条聚合查询；
- 实体名向量（去重用）在 ``index/entity.vdb``，与既有实现的 ``entity.vdb`` 同格式
  （``__id__`` = 实体 id，只存向量，没有 metadata）。

**实体节点的 id 是 ``ent_<uuid>``、``path`` 为 NULL**（与旧库一致）：实体不是路径
节点，正文放 ``content/entities/<id>.md``，靠 ``kb_refs`` / ``from`` 边与记忆文件
关联。所以实体不会出现在 ``memories://`` 目录树里，也不会进分块索引——检索里靠
图谱扩展（文档 → 实体 → 其它文档）把它们带上。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from liorium.core.log import get_logger

from .store import SqliteMemoryStore, epoch_to_iso, normalize_memory_path, path_id

if TYPE_CHECKING:  # pragma: no cover - 只用于类型
    from .vectors import VectorBackend

log = get_logger("liorium.memory.graph")

ENTITY_TYPES = ("person", "place", "event", "concept", "object", "document", "custom")
"""推荐类型（不是白名单：``chat_record`` / ``diary`` 这类自定义类型原样保留）。"""
RELATION_TYPES = (
    "relates_to",
    "part_of",
    "located_at",
    "created_by",
    "mentions",
    "references",
)
DEFAULT_RELATION = "relates_to"
FROM_EDGE = "from"
"""文档 → 实体 的边：表示"这条记忆提到了这个实体"（既有实现同名字）。"""

TREE_EDGE = "has_child"
ENTITY_DIR = "entities"
DEFAULT_DEDUP_THRESHOLD = 0.85
"""实体去重阈值（与既有实现 ``ENTITY_DEDUP_THRESHOLD`` 同值）。"""


def new_entity_id() -> str:
    """实体 id：``ent_<uuid>``（与既有实现同前缀，旧库里的 ``ent_*`` 直接可用）。"""
    return "ent_" + uuid.uuid4().hex


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    entity_type: str = "custom"
    description: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    kb_refs: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "properties": dict(self.properties),
            "kb_refs": list(self.kb_refs),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    type: str
    key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target, "type": self.type}


class EntityGraph:
    """实体与关系（``nodes.type='entity'`` + ``edges``）。

    ``entity_vectors`` 可选：给了才能做"同名合并"的语义去重；没给就退化成按名字
    精确匹配（并在日志里说明），不假装做过语义去重。
    """

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        entity_vectors: "VectorBackend | None" = None,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> None:
        self.store = store
        self.entity_vectors = entity_vectors
        self.dedup_threshold = float(dedup_threshold)

    # ------------------------------------------------------------------
    # 实体 CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        entity_type: str = "custom",
        *,
        description: str = "",
        properties: Mapping[str, Any] | None = None,
        kb_refs: Iterable[str] = (),
        eid: str | None = None,
        now: float | None = None,
    ) -> Entity:
        """新建实体（正文写 ``content/entities/<id>.md``，与既有实现同布局）。"""
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("实体名不能为空")
        ident = str(eid or new_entity_id())
        if not ident.startswith("ent_"):
            ident = "ent_" + ident
        if self.store._one("SELECT id FROM nodes WHERE id=?", (ident,)) is not None:  # noqa: SLF001
            raise FileExistsError(f"实体已存在: {ident}")
        kind = str(entity_type or "custom")
        # 不认识的类型原样保留：旧库里还有 chat_record / diary 这类自定义类型，
        # 强行归成 custom 会把记录链断掉（ENTITY_TYPES 只是推荐值，不是白名单）。
        desc = str(description or "")
        refs = [str(r) for r in kb_refs if str(r).strip()]
        body_ref = self._body_ref(ident)
        if body_ref not in refs:
            # 既有实现把实体自己的正文路径也塞进 kb_refs（兼容旧库读回来的样子）
            refs.append(body_ref)
        stamp = time.time() if now is None else float(now)
        data = {"properties": dict(properties or {}), "kb_refs": refs}

        self._write_body(ident, clean, desc)
        with self.store._transaction() as conn:  # noqa: SLF001 - 同包内部
            conn.execute(
                "INSERT INTO nodes(id, type, name, description, entity_type, data,"
                " created_at, updated_at, chunk_count, indexed)"
                " VALUES (?, 'entity', ?, ?, ?, ?, ?, ?, 0, 0)",
                (ident, clean, desc, kind, json.dumps(data, ensure_ascii=False), stamp, stamp),
            )
        log.debug("entity_add name=%s type=%s id=%s", clean, kind, ident[:12])
        return Entity(
            id=ident,
            name=clean,
            entity_type=kind,
            description=desc,
            properties=dict(properties or {}),
            kb_refs=tuple(refs),
            created_at=epoch_to_iso(stamp),
            updated_at=epoch_to_iso(stamp),
        )

    def get(self, eid: str) -> Entity | None:
        row = self.store._one(  # noqa: SLF001
            "SELECT id, name, entity_type, description, data, created_at, updated_at"
            " FROM nodes WHERE id=? AND type='entity'",
            (str(eid),),
        )
        return _entity_from_row(row) if row is not None else None

    def delete(self, eid: str) -> bool:
        ident = str(eid)
        if self.get(ident) is None:
            return False
        with self.store._transaction() as conn:  # noqa: SLF001
            conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (ident, ident))
            conn.execute("DELETE FROM nodes WHERE id=?", (ident,))
        self._body_path(ident).unlink(missing_ok=True)
        return True

    def merge(self, keep_id: str, absorb_id: str) -> dict[str, Any]:
        """把 ``absorb`` 并入 ``keep``：补字段、改指边、去自环/重边，然后删掉 absorb。"""
        keep, absorb = self.get(keep_id), self.get(absorb_id)
        if keep is None or absorb is None:
            raise FileNotFoundError("要合并的两个实体必须都存在")
        if keep.id == absorb.id:
            raise ValueError("keep 与 absorb 不能是同一个实体")

        properties = {**absorb.properties, **keep.properties}
        kb_refs = list(keep.kb_refs)
        absorb_body = self._body_ref(absorb.id)
        for ref in absorb.kb_refs:
            if ref != absorb_body and ref not in kb_refs:
                kb_refs.append(ref)
        entity_type = keep.entity_type
        if entity_type == "custom":
            entity_type = absorb.entity_type or entity_type
        description = keep.description or absorb.description

        incident = self.store._all(  # noqa: SLF001
            "SELECT src, dst, type FROM edges WHERE (src=? OR dst=?) AND type<>?",
            (absorb.id, absorb.id, TREE_EDGE),
        )
        existing = {
            (str(row["src"]), str(row["dst"]), str(row["type"]))
            for row in self.store._all(  # noqa: SLF001
                "SELECT src, dst, type FROM edges WHERE src<>? AND dst<>? AND type<>?",
                (absorb.id, absorb.id, TREE_EDGE),
            )
        }
        rewired: list[tuple[str, str, str, str]] = []
        loops = duplicates = 0
        for row in incident:
            src = keep.id if str(row["src"]) == absorb.id else str(row["src"])
            dst = keep.id if str(row["dst"]) == absorb.id else str(row["dst"])
            rel_type = str(row["type"] or DEFAULT_RELATION)
            if src == dst:
                loops += 1
                continue
            if (src, dst, rel_type) in existing:
                duplicates += 1
                continue
            existing.add((src, dst, rel_type))
            rewired.append((src, dst, rel_type, uuid.uuid4().hex))

        stamp = time.time()
        data = {"properties": properties, "kb_refs": kb_refs}
        with self.store._transaction() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE nodes SET name=?, entity_type=?, description=?, data=?, updated_at=?"
                " WHERE id=?",
                (keep.name, entity_type, description, json.dumps(data, ensure_ascii=False), stamp, keep.id),
            )
            conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (absorb.id, absorb.id))
            conn.executemany(
                "INSERT OR REPLACE INTO edges(src, dst, type, key) VALUES (?, ?, ?, ?)",
                rewired,
            )
            conn.execute("DELETE FROM nodes WHERE id=?", (absorb.id,))
        self._body_path(absorb.id).unlink(missing_ok=True)
        log.info(
            "entity_merge keep=%s absorb=%s rewired=%d dup=%d loops=%d",
            keep.id[:12],
            absorb.id[:12],
            len(rewired),
            duplicates,
            loops,
        )
        return {
            "ok": True,
            "keep_id": keep.id,
            "absorb_id": absorb.id,
            "keep_name": keep.name,
            "absorb_name": absorb.name,
            "rewired_edges": len(rewired),
            "dropped_duplicate_edges": duplicates,
            "dropped_self_loops": loops,
        }

    def search(
        self, query: str = "", *, type_filter: str | None = None, top_k: int = 20
    ) -> list[Entity]:
        """按名字子串搜实体（前缀命中优先）。"""
        rows = self.store._all(  # noqa: SLF001
            "SELECT id, name, entity_type, description, data, created_at, updated_at"
            " FROM nodes WHERE type='entity'",
        )
        needle = str(query or "").strip().lower()
        found: list[Entity] = []
        for row in rows:
            if type_filter and str(row["entity_type"] or "custom") != type_filter:
                continue
            if needle and needle not in str(row["name"] or "").lower():
                continue
            found.append(_entity_from_row(row))
        found.sort(key=lambda item: (0 if item.name.lower().startswith(needle) else 1, item.name))
        return found[: max(1, int(top_k))]

    def iter_entities(self) -> list[Entity]:
        return self.search("", top_k=1_000_000)

    def count(self) -> int:
        row = self.store._one("SELECT COUNT(*) AS n FROM nodes WHERE type='entity'")  # noqa: SLF001
        return int(row["n"] if row else 0)

    # ------------------------------------------------------------------
    # 关系
    # ------------------------------------------------------------------

    def add_relation(
        self, source_id: str, target_id: str, rel_type: str = DEFAULT_RELATION
    ) -> str:
        """加一条边；节点不存在直接报错（既有实现只会抛看不懂的外键错误）。"""
        src, dst = str(source_id or ""), str(target_id or "")
        for nid in (src, dst):
            if not nid:
                raise ValueError("source_id / target_id 不能为空")
            if self.store._one("SELECT id FROM nodes WHERE id=?", (nid,)) is None:  # noqa: SLF001
                raise FileNotFoundError(f"节点不存在: {nid}")
        key = uuid.uuid4().hex
        with self.store._transaction() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT OR IGNORE INTO edges(src, dst, type, key) VALUES (?, ?, ?, ?)",
                (src, dst, str(rel_type or DEFAULT_RELATION), key),
            )
        return key

    def remove_relation(
        self, source_id: str, target_id: str, rel_type: str | None = None
    ) -> int:
        sql = "DELETE FROM edges WHERE src=? AND dst=? AND type<>?"
        params: list[Any] = [str(source_id), str(target_id), TREE_EDGE]
        if rel_type:
            sql += " AND type=?"
            params.append(str(rel_type))
        with self.store._transaction() as conn:  # noqa: SLF001
            before = conn.total_changes
            conn.execute(sql, params)
            return conn.total_changes - before

    def relations_of(self, eid: str) -> list[Relation]:
        rows = self.store._all(  # noqa: SLF001
            "SELECT src, dst, type, key FROM edges WHERE (src=? OR dst=?) AND type<>?"
            " ORDER BY type",
            (str(eid), str(eid), TREE_EDGE),
        )
        return [
            Relation(str(row["src"]), str(row["dst"]), str(row["type"]), str(row["key"]))
            for row in rows
        ]

    def iter_relations(self) -> list[Relation]:
        rows = self.store._all(  # noqa: SLF001
            "SELECT src, dst, type, key FROM edges WHERE type<>?", (TREE_EDGE,)
        )
        return [
            Relation(str(row["src"]), str(row["dst"]), str(row["type"]), str(row["key"]))
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 图查询（全部 SQL，无投影）
    # ------------------------------------------------------------------

    def neighbors(self, eid: str, depth: int = 1) -> list[dict[str, Any]]:
        """``eid`` 的 ``depth`` 跳邻居（含深度与相对中心的边类型/方向）。"""
        nid = str(eid)
        if self.store._one("SELECT id FROM nodes WHERE id=?", (nid,)) is None:  # noqa: SLF001
            return []
        rows = self.store._all(  # noqa: SLF001
            """
            WITH RECURSIVE walk(id, depth) AS (
                SELECT dst, 1 FROM edges WHERE src=? AND type<>?
                UNION
                SELECT src, 1 FROM edges WHERE dst=? AND type<>?
                UNION
                SELECT CASE WHEN e.src = walk.id THEN e.dst ELSE e.src END, walk.depth + 1
                FROM edges e JOIN walk ON (e.src = walk.id OR e.dst = walk.id)
                WHERE walk.depth < ? AND e.type<>?
            )
            SELECT id, MIN(depth) AS depth FROM walk WHERE id<>? GROUP BY id
            """,
            (nid, TREE_EDGE, nid, TREE_EDGE, max(1, int(depth)), TREE_EDGE, nid),
        )
        if not rows:
            return []
        depths = {str(row["id"]): int(row["depth"]) for row in rows}
        info = self._nodes_info(list(depths))
        direct = self._center_edges(nid, list(depths))
        results: list[dict[str, Any]] = []
        for other, level in depths.items():
            node = info.get(other)
            if node is None:
                continue
            item: dict[str, Any] = {
                "id": other,
                "name": node["name"],
                "depth": level,
                "relations": direct.get(other, []),
            }
            if node["type"] == "entity":
                item["entity_type"] = node["entity_type"] or "custom"
                item["description"] = node["description"]
                item["path_ref"] = None
            elif node["type"] in ("file", "dir"):
                item["path_ref"] = node["path"]
            else:
                continue
            results.append(item)
        results.sort(key=lambda item: (item["depth"], item["name"]))
        return results

    def detail(self, eid: str) -> dict[str, Any] | None:
        """实体（或路径节点）详情：字段 + 关系计数 + 邻居预览。"""
        nid = str(eid)
        row = self.store._one(  # noqa: SLF001
            "SELECT id, type, path, name, entity_type, description, data, created_at,"
            " updated_at FROM nodes WHERE id=?",
            (nid,),
        )
        if row is None:
            return None
        kind = str(row["type"] or "")
        data = _load_data(row)
        payload: dict[str, Any] = {
            "id": nid,
            "name": str(row["name"] or ""),
            "entity_type": str(row["entity_type"] or kind or "custom"),
            "description": str(row["description"] or ""),
            "properties": dict(data.get("properties") or {}),
            "kb_refs": list(data.get("kb_refs") or ([row["path"]] if row["path"] else [])),
            "created_at": epoch_to_iso(row["created_at"]),
            "updated_at": epoch_to_iso(row["updated_at"]),
        }
        if row["path"]:
            payload["path"] = str(row["path"])
        relations = self.relations_of(nid)
        payload["relations_count"] = len(relations)
        payload["neighbors"] = [
            {"id": item["id"], "name": item["name"], "depth": item["depth"],
             "relations": item["relations"]}
            for item in self.neighbors(nid, depth=1)[:20]
        ]
        return payload

    def entities_of(self, path: str) -> list[Entity]:
        """某条记忆里抽出来的实体（``from`` 边 + ``kb_refs`` 反查）。"""
        norm = normalize_memory_path(path)
        nid = path_id(norm)
        rows = self.store._all(  # noqa: SLF001
            "SELECT DISTINCT n.id, n.name, n.entity_type, n.description, n.data,"
            " n.created_at, n.updated_at FROM nodes n"
            " JOIN edges e ON e.dst = n.id AND e.type=?"
            " WHERE e.src=? AND n.type='entity'",
            (FROM_EDGE, nid),
        )
        found = {str(row["id"]): _entity_from_row(row) for row in rows}
        for entity in self.iter_entities():
            if norm in entity.kb_refs:
                found.setdefault(entity.id, entity)
        return sorted(found.values(), key=lambda item: item.name)

    def linked_paths(self, eid: str) -> list[str]:
        """实体挂着的记忆文件（``from`` 反向 + ``kb_refs``）。"""
        entity = self.get(eid)
        if entity is None:
            return []
        paths: list[str] = []
        rows = self.store._all(  # noqa: SLF001
            "SELECT n.path AS path FROM nodes n JOIN edges e ON e.src = n.id AND e.type=?"
            " WHERE e.dst=? AND n.path IS NOT NULL",
            (FROM_EDGE, str(eid)),
        )
        for row in rows:
            path = str(row["path"] or "")
            if path and path not in paths:
                paths.append(path)
        for ref in entity.kb_refs:
            if ref not in paths and ref.startswith("/") and not ref.startswith(f"/{ENTITY_DIR}/"):
                paths.append(ref)
        return paths

    def expand_documents(
        self, paths: Sequence[str], *, hops: int = 2, limit: int = 10
    ) -> list[str]:
        """从命中的记忆文件出发，经实体走到**别的**记忆文件（图谱召回）。

        既有实现的 ``search_compact`` 就是把 2 跳邻居直接塞进结果；这里只返回文件路径
        （实体本身不是可展开的记忆条目），排序按"经过几次跳"。
        """
        seeds = [path_id(p) for p in paths]
        if not seeds:
            return []
        found: dict[str, int] = {}
        frontier = list(seeds)
        for level in range(1, max(1, int(hops)) + 1):
            next_frontier: list[str] = []
            for start in range(0, len(frontier), 400):
                page = frontier[start : start + 400]
                marks = ",".join("?" * len(page))
                rows = self.store._all(  # noqa: SLF001
                    "SELECT e.src AS src, e.dst AS dst FROM edges e"
                    f" WHERE (e.src IN ({marks}) OR e.dst IN ({marks})) AND e.type IN (?, ?)",
                    [*page, *page, FROM_EDGE, DEFAULT_RELATION],
                )
                for row in rows:
                    for a, b in ((str(row["src"]), str(row["dst"])), (str(row["dst"]), str(row["src"]))):
                        if a in page and b not in found and b not in seeds:
                            found[b] = level
                            next_frontier.append(b)
            frontier = next_frontier
        if not found:
            return []
        rows = self.store._all(  # noqa: SLF001
            "SELECT id, path FROM nodes WHERE id IN ("
            + ",".join("?" * len(found))
            + ") AND type='file'",
            list(found),
        )
        ranked = sorted(
            ((str(row["path"]), found[str(row["id"])]) for row in rows if row["path"]),
            key=lambda item: item[1],
        )
        return [path for path, _ in ranked][: max(1, int(limit))]

    # ------------------------------------------------------------------
    # 语义去重（实体名向量）
    # ------------------------------------------------------------------

    async def find_similar(
        self,
        names: Sequence[str],
        vectors: Sequence[Sequence[float]] | None = None,
        *,
        threshold: float | None = None,
    ) -> list[str | None]:
        """给每个实体名找最像的已有实体 id（不达标返回 ``None``）。

        没有向量后端/向量时退回**名字精确匹配**（并记日志，不假装做过语义去重）。
        """
        if not names:
            return []
        if vectors is None or self.entity_vectors is None:
            if self.entity_vectors is None:
                log.debug("没有实体向量后端，实体去重退化为名字精确匹配")
            return [self._exact_name_id(str(name)) for name in names]
        floor = self.dedup_threshold if threshold is None else float(threshold)
        results: list[str | None] = []
        for vector in vectors:
            hits = await self.entity_vectors.query(vector, top_k=1)
            if not hits or hits[0].score < floor:
                results.append(None)
                continue
            candidate = hits[0].chunk_id
            # 向量库里可能有已删除实体的残留（删除是同步 SQL，向量删除是异步的）：
            # 命中后必须回表确认，否则会把新实体并到一个不存在的 id 上。
            results.append(candidate if self.get(candidate) is not None else None)
        return results

    async def prune_entity_vectors(self) -> int:
        """清掉向量库里已经没有对应实体的行（实体删除后的清道夫）。"""
        if self.entity_vectors is None:
            return 0
        known = {entity.id for entity in self.iter_entities()}
        stale = [vid for vid in self.entity_vectors.ids() if vid not in known]
        if not stale:
            return 0
        removed = await self.entity_vectors.delete(stale)
        await self.entity_vectors.flush()
        log.info("清理无主实体向量: %d 条", removed)
        return removed

    async def store_name_vector(self, eid: str, vector: Sequence[float]) -> None:
        """记一个实体名向量（去重用；与既有实现一样只存 ``__id__`` + 向量）。"""
        if self.entity_vectors is None:
            return
        from .vectors import VectorRecord

        await self.entity_vectors.upsert([VectorRecord(chunk_id=str(eid), vector=vector)])

    def _exact_name_id(self, name: str) -> str | None:
        found = self.search(name, top_k=5)
        for entity in found:
            if entity.name == name:
                return entity.id
        return None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _body_path(self, eid: str) -> Path:
        return self.store.content_dir / ENTITY_DIR / f"{str(eid).split(':', 1)[-1]}.md"

    @staticmethod
    def _body_ref(eid: str) -> str:
        return f"/{ENTITY_DIR}/{str(eid).split(':', 1)[-1]}.md"

    def _write_body(self, eid: str, name: str, description: str) -> None:
        path = self._body_path(eid)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
        path.write_text(body, encoding="utf-8", newline="")

    def _nodes_info(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        info: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), 500):
            page = [str(i) for i in ids[start : start + 500]]
            marks = ",".join("?" * len(page))
            for row in self.store._all(  # noqa: SLF001
                "SELECT id, type, path, name, entity_type, description FROM nodes"
                f" WHERE id IN ({marks})",
                page,
            ):
                info[str(row["id"])] = {
                    "type": str(row["type"] or ""),
                    "path": str(row["path"] or ""),
                    "name": str(row["name"] or ""),
                    "entity_type": str(row["entity_type"] or ""),
                    "description": str(row["description"] or ""),
                }
        return info

    def _center_edges(
        self, nid: str, others: Sequence[str]
    ) -> dict[str, list[dict[str, str]]]:
        """中心节点与邻居之间**直接**相连的边（更远的跳只给 depth，不给方向）。"""
        wanted = set(others)
        result: dict[str, list[dict[str, str]]] = {}
        for row in self.store._all(  # noqa: SLF001
            "SELECT src, dst, type FROM edges WHERE (src=? OR dst=?) AND type<>?",
            (nid, nid, TREE_EDGE),
        ):
            src, dst, rel_type = str(row["src"]), str(row["dst"]), str(row["type"])
            other = dst if src == nid else src
            if other not in wanted:
                continue
            direction = "out" if src == nid else "in"
            result.setdefault(other, []).append({"type": rel_type, "direction": direction})
        for items in result.values():
            items.sort(key=lambda item: (item["type"], item["direction"]))
        return result


def _load_data(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entity_from_row(row: Any) -> Entity:
    data = _load_data(row)
    return Entity(
        id=str(row["id"]),
        name=str(row["name"] or ""),
        entity_type=str(row["entity_type"] or "custom"),
        description=str(row["description"] or ""),
        properties=dict(data.get("properties") or {}),
        kb_refs=tuple(str(r) for r in (data.get("kb_refs") or ())),
        created_at=epoch_to_iso(row["created_at"]),
        updated_at=epoch_to_iso(row["updated_at"]),
    )


__all__ = [
    "DEFAULT_DEDUP_THRESHOLD",
    "ENTITY_DIR",
    "ENTITY_TYPES",
    "FROM_EDGE",
    "RELATION_TYPES",
    "Entity",
    "EntityGraph",
    "Relation",
    "new_entity_id",
]
