"""``MemoryRealm`` —— 记忆能力包（``[memory]`` extra）。

计划依据：§C1（工具 + 挂载 + 状态 + ``prompt_fragment()``）、§C6（状态存自己的
store，VFS 只出投影节点）、§3.7（memory store 冻结，读写旧库）。

行为：

- **挂载**：``/memories``（``memories://``）是 SQLite store 的**懒加载投影**：
  只投影当前层，子节点由 ``child_provider`` / ``children_provider`` 按需从节点表查
  （记忆库再大，VFS 常驻对象也只有一层）；写已有文件走 ``write_handler``，写新文件
  走目录节点上的 ``create_handler``；
- **工具**：``memory_search``（混合检索）、``memory_list``（分页列目录）、
  ``memory_delete``（目录需 ``recursive``）、``memory_tag``、``memory_score``；
  读/写/编辑用核心 ``read``/``write``/``edit``；
- **检索**：关键词（FTS5 BM25）默认开；向量检索要宿主注入 ``embedder`` + ``vectors``
  （本包不读产品配置，也就不自己造 API key）。两者都有时按 RRF 融合；
- **状态**：``state_scope="agent"``，记库路径、条目数、索引体检结果；
- **dirty**：外部（另一个进程 / 宿主）改了库 ⇒ ``dirty()``，Agent 在回合边界
  调 ``refresh()``；投影是懒加载的，所以"刷新"只是重读 revision（不重建树）。

重命名 / 移动 / 复制 / 多条件检索是 **store API**（与既有实现一致，不给模型当工具）。
"""

from __future__ import annotations

import asyncio
import contextvars
import posixpath
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from liorium.core.errors import VfsError
from liorium.core.log import get_logger
from liorium.core.types import Binary, Image, Text, VfsContent
from liorium.vfs import VirtualFileSystem, VfsMount, VfsNode

from .graph import EntityGraph
from .search import MemorySearch, SearchConfig
from .store import (
    MAX_SCORE_PATCH,
    MIN_SCORE_PATCH,
    MemoryEntry,
    MemoryNode,
    SqliteMemoryStore,
    epoch_to_iso,
    normalize_memory_path,
    path_id,
)

if TYPE_CHECKING:  # pragma: no cover - 只用于类型（避免硬依赖 numpy/httpx）
    from .embeddings import Embeddings
    from .extract import EntityExtractor, MemoryExtractor
    from .indexer import MemoryIndexer
    from .vectors import VectorBackend

log = get_logger("liorium.memory.realm")

MOUNT_PREFIX = "/memories"
SCHEME = "memories"
STORE_DIR_NAME = "memory"
"""默认记忆目录（与既有实现的 ``agents/<name>/memory`` 布局对齐）。"""

DEFAULT_LIST_LIMIT = 200
"""一层最多投影多少个子节点（超过用 ``memory_list`` 分页）。"""

SYNC_REINDEX_CHUNKS = 20_000
"""启动时关键词索引漂移：chunk 数不超过这个值就同步重建，否则丢后台。"""


class MemoryRealm:
    """记忆 realm：SQLite 懒加载投影 + 混合检索/维护工具 + prompt 用法说明。"""

    id = "memory"
    requires: tuple[str, ...] = ()
    state_scope = "agent"
    instance_scope = "tree"
    """整棵 Agent 树一份：sqlite 库与 ``/memories`` 挂载都是全树唯一的资源。"""

    def __init__(
        self,
        spec: Any = None,
        *,
        store: SqliteMemoryStore | None = None,
        embedder: "Embeddings | None" = None,
        vectors: "VectorBackend | None" = None,
        entity_vectors: "VectorBackend | None" = None,
        extractor: "EntityExtractor | None" = None,
        search_config: SearchConfig | None = None,
    ) -> None:
        params = dict(getattr(spec, "params", {}) or {})
        self._db_path_param = params.get("db_path")
        self._content_dir_param = params.get("content_dir")
        self.list_limit = int(params.get("list_limit", DEFAULT_LIST_LIMIT))
        if self.list_limit < 1:
            raise ValueError("list_limit 必须 >= 1")
        self.keyword_index = bool(params.get("keyword_index", True))
        self.vectors_enabled = bool(params.get("vectors", True)) and bool(
            embedder is not None and vectors is not None
        )
        self.extraction_enabled = bool(params.get("extraction", True)) and extractor is not None

        self._store = store
        self._owns_store = store is None
        self._embedder = embedder
        self._vectors = vectors
        self._entity_vectors = entity_vectors
        self._extractor_spec = extractor
        self._extractor: "MemoryExtractor | None" = None
        self._graph: EntityGraph | None = None
        self._search_config = search_config
        self._search: MemorySearch | None = None
        self._indexer: "MemoryIndexer | None" = None
        self._ctx: Any = None
        self._vfs: VirtualFileSystem | None = None
        self._revision: tuple[int, int, float] | None = (
            store.revision() if store is not None else None
        )
        self._tools: list[Any] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, ctx: Any) -> None:
        self._ctx = ctx
        if self._store is None:
            db_path = self._resolve_path(ctx, self._db_path_param, "memory.sqlite")
            content_dir = self._resolve_path(ctx, self._content_dir_param, "content")
            self._store = SqliteMemoryStore(
                db_path, content_dir=content_dir, keyword_index=self.keyword_index
            )
        store = self._store
        store.vector_index = self.vectors_enabled
        self._graph = EntityGraph(store, entity_vectors=self._entity_vectors)
        self._search = self._build_search()
        if self.extraction_enabled and self._extractor_spec is not None:
            from .extract import MemoryExtractor

            self._extractor = MemoryExtractor(
                store,
                self._graph,
                extractor=self._extractor_spec,
                embedder=self._embedder if self.vectors_enabled else None,
            )
        if self.vectors_enabled and self._embedder is not None and self._vectors is not None:
            from .indexer import MemoryIndexer

            self._indexer = MemoryIndexer(
                store, embedder=self._embedder, vectors=self._vectors
            )
            ctx.spawn(self._indexer.run(), name="memory:indexer")
            # 启动时按水位自愈：向量库被删/首次开向量/毒丸被丢弃后，语义检索
            # 不能永久为空（reconcile 只补投，不在前台 embed）。
            await self._reconcile_vectors()

        self._vfs = self._build_vfs()
        self._revision = store.revision()
        await self._prepare_index(ctx)
        await ctx.state.update_many(
            {
                "db": str(store.path),
                "content_dir": str(store.content_dir),
                "entries": store.count_files(),
                "top_level": store.count_children("/"),
                "index": store.keyword_index_status(),
                "entities": self._graph.count() if self._graph is not None else 0,
                "vectors": self._vectors.count()
                if self.vectors_enabled and self._vectors is not None
                else 0,
            }
        )
        log.info(
            "MemoryRealm 启动: db=%s vectors=%s", store.path, "on" if self.vectors_enabled else "off"
        )

    async def stop(self, ctx: Any) -> None:
        self._ctx = None
        if self._indexer is not None:
            await self._indexer.aclose()
            self._indexer = None
        if self.vectors_enabled and self._vectors is not None:
            try:
                await self._vectors.flush()
            except Exception as exc:  # noqa: BLE001 - 退出路径：落盘失败要说出来但别吞掉别的清理
                log.error("记忆向量落盘失败: %s", exc, exc_info=exc)
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None
            self._vfs = None
        self._search = None

    def _build_search(self) -> MemorySearch:
        store = self._store
        if store is None:
            raise RuntimeError("MemoryRealm 没有 store，无法检索")
        return MemorySearch(
            store,
            embedder=self._embedder if self.vectors_enabled else None,
            vectors=self._vectors if self.vectors_enabled else None,
            graph=self.graph,
            config=self._search_config,
        )

    @property
    def graph(self) -> EntityGraph:
        """实体/关系图（惰性构建：没 ``start()`` 也能用注入的 store）。"""
        if self._graph is None:
            if self._store is None:
                raise RuntimeError("MemoryRealm 没有 store，无法访问实体图")
            self._graph = EntityGraph(self._store, entity_vectors=self._entity_vectors)
        return self._graph

    @property
    def search_engine(self) -> MemorySearch:
        """检索器（惰性构建：没 ``start()`` 也能用注入的 store 检索）。"""
        if self._search is None:
            self._search = self._build_search()
        return self._search

    async def _reconcile_vectors(self) -> None:
        """启动时对账 chunks 与向量库；失败只降级，不拖垮 realm 启动。"""
        if self._indexer is None:
            return
        try:
            report = await self._indexer.reconcile()
        except Exception as exc:  # noqa: BLE001 - 自愈失败不该让记忆库起不来
            log.error("向量索引启动对账失败（语义检索可能不全）: %s", exc, exc_info=exc)
            return
        if report.get("missing") or report.get("stale"):
            log.info("向量索引启动自愈: %s", report)

    async def _prepare_index(self, ctx: Any) -> None:
        """启动时体检关键词索引；漂移就重建（小库同步，大库丢后台）。"""
        store = self._store
        assert store is not None
        status = store.keyword_index_status()
        if not status.get("stale"):
            return
        chunks = int(status.get("chunks") or 0)
        if chunks <= SYNC_REINDEX_CHUNKS:
            await asyncio.to_thread(store.reindex_keyword)
            return
        log.warning(
            "关键词索引与 chunks 不一致（%d 条），后台重建（期间检索可能不全）", chunks
        )
        ctx.spawn(self._reindex_background(), name="memory:reindex")

    async def _reindex_background(self) -> None:
        store = self._store
        if store is None:
            return
        started = time.monotonic()
        count = await asyncio.to_thread(store.reindex_keyword)
        log.info("后台关键词索引重建完成: %d chunks，用时 %.1fs", count, time.monotonic() - started)

    def _resolve_path(self, ctx: Any, raw: Any, default: str) -> Path:
        inbox = getattr(ctx, "inbox_dir", None)
        base = Path(inbox) if inbox is not None else Path.cwd()
        if raw is None:
            return base / STORE_DIR_NAME / default
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = base / path
        return path

    # ------------------------------------------------------------------
    # realm 协议
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        if self._tools is None:
            from .tools import build_memory_tools

            self._tools = build_memory_tools(self)
        return list(self._tools)

    def prompt_fragment(self) -> str:
        semantic = "支持语义检索（同义改写也能召回）" if self.vectors_enabled else "按关键词检索"
        return (
            "记忆库（`memories://`，可写）：\n"
            "- `read memories://` 列一层目录（文件带 `MM/DD, N lines, #tag` 元数据），"
            "`read memories://<path>` 读全文；目录很大时分页用 `memory_list`。\n"
            "- 新记忆用 `write memories://<path>` 写入（路径当标题，正文写事实本身）；"
            "改已有内容用 `edit`；`memory_search` 检索全部记忆"
            f"（{semantic}）。\n"
            "- 维护：`memory_tag` 打标签、`memory_score` 调检索权重（-0.15~+0.15）、"
            "`memory_delete` 删除（删目录必须显式 `recursive=True`）。\n"
            "- 记忆里会抽实体与关系（人/地点/事件/概念）：`memory_graph` 可以按实体"
            "看邻居与详情（action=search 先找 id，再 neighbors/detail）。\n"
            "- 记忆是长期事实（偏好、人物、结论、待办），不是本轮临时上下文；"
            "回答涉及过往事实时先检索，不要凭印象编造。"
        )

    def mounts(self) -> dict[str, VfsMount]:
        if self._vfs is None:
            self._vfs = self._build_vfs()
        return {
            MOUNT_PREFIX: VfsMount(
                prefix=MOUNT_PREFIX,
                vfs=self._vfs,
                readonly=False,
                description=f"memory:{self._store.path if self._store else ''}",
            )
        }

    def middleware(self) -> list[Any]:
        """记忆不追加中间件（读写都走 VFS 投影）。"""
        return []

    def skills(self) -> list[str]:
        return []

    def dirty(self) -> bool:
        if self._store is None:
            return False
        return self._store.revision() != self._revision

    def mark_dirty(self, flag: bool = True) -> None:
        # 记忆没有"强制脏"的语义；置 False 等价于清账，True 用 revision 自然体现。
        if flag and self._store is not None:
            self._revision = None

    def clear_dirty(self) -> None:
        if self._store is not None:
            self._revision = self._store.revision()

    async def refresh(self) -> None:
        """外部（别的进程 / 宿主 / 另一个 Agent）改了库 ⇒ 重新对账。

        投影是懒加载的（每次 ``read`` 都问 store），所以这里不需要重建树；只把
        revision 拉齐，免得下一回合又被判脏。既有实现要求重建整棵树才能看到新节点。
        """
        if self._store is None:
            return
        self._revision = self._store.revision()
        log.info("MemoryRealm 对账: db=%s revision=%s", self._store.path, self._revision)

    # ------------------------------------------------------------------
    # 查询 / 检索（工具用）
    # ------------------------------------------------------------------

    @property
    def store(self) -> SqliteMemoryStore | None:
        return self._store

    def search(
        self,
        query: str,
        *,
        scope: str = "/",
        top_k: int = 5,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """同步检索（只走关键词）。异步混合检索请用 :meth:`asearch`。"""
        if self._store is None:
            raise RuntimeError("MemoryRealm 未 start()，没有可用的记忆库")
        return [hit.to_dict() for hit in self._store.search(query, scope=scope, top_k=top_k, tags=tags or ())]

    async def asearch(
        self,
        query: str,
        *,
        scope: str = "/",
        top_k: int = 5,
        tags: list[str] | None = None,
        return_mode: str = "compact",
        mode: str = "auto",
    ) -> list[dict[str, Any]]:
        """混合检索（关键词 + 向量 + 图谱，RRF 融合）。"""
        return await self.search_engine.search(
            query,
            scope=scope,
            tags=tags or (),
            top_k=top_k,
            return_mode=return_mode,
            mode=mode,
        )

    async def drain_index(self, *, max_batches: int = 200) -> int:
        """把待向量化的作业跑完（CLI/测试用；正常流程由后台索引器负责）。"""
        if self._indexer is None:
            return 0
        return await self._indexer.drain(max_batches=max_batches)

    def _notify_indexer(self) -> None:
        if self._indexer is not None:
            self._indexer.notify()

    # ------------------------------------------------------------------
    # 实体抽取（后台，与当前回合解耦）
    # ------------------------------------------------------------------

    def _schedule_extraction(self, path: str) -> None:
        """写/改记忆之后，把实体抽取扔到后台。

        **必须用空白 contextvars 上下文**：否则这次 LLM 调用会成为当前 Agent run
        的子 run，流式 delta 冒泡进主事件流（见 :class:`RealmTaskScope`）。
        """
        extractor = self._extractor
        ctx = self._ctx
        spawn = getattr(ctx, "spawn", None)
        if extractor is None or spawn is None:
            return
        extractor.notify_pending(path)
        spawn(
            extractor.run_document(path),
            name=f"memory:extract:{path}",
            context=contextvars.Context(),
        )

    async def drain_extraction(self, timeout: float = 10.0) -> None:
        """等后台抽取任务跑完（测试 / CLI 用；正常流程不需要等）。"""
        tasks = getattr(getattr(self._ctx, "tasks", None), "_tasks", None)
        if not tasks:
            return
        pending = [task for task in list(tasks) if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def list_dir(self, path: str = "/", *, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0):
        """分页列一层（给 ``memory_list`` 工具）；返回 ``(nodes, total)``。"""
        store = self._require_store()
        total = store.count_children(path)
        nodes = store.children(path, limit=limit, offset=offset)
        return nodes, total

    def delete(self, path: str, *, recursive: bool = False) -> dict[str, Any]:
        store = self._require_store()
        node = store.node(path)
        if node is None:
            raise FileNotFoundError(f"记忆不存在: {path}")
        if node.kind == "dir" and not recursive:
            raise ValueError(f"{node.path} 是目录，删除目录需显式 recursive=True")
        removed = store.delete(node.path)
        self._revision = store.revision()
        self._notify_indexer()
        return {"path": node.path, "deleted": removed, "kind": node.kind}

    def set_tags(self, path: str, tags: list[str]) -> dict[str, Any]:
        store = self._require_store()
        node = store.set_tags(path, tags, managed_by="agent")
        self._revision = store.revision()
        return {"path": node.path, "tags": list(node.tags)}

    def set_score(self, path: str, score_patch: float) -> dict[str, Any]:
        store = self._require_store()
        node = store.set_score_patch(path, score_patch)
        self._revision = store.revision()
        return {"path": node.path, "score_patch": node.score_patch}

    def _require_store(self) -> SqliteMemoryStore:
        if self._store is None:
            raise RuntimeError("MemoryRealm 未 start()，没有可用的记忆库")
        return self._store

    # ------------------------------------------------------------------
    # 内容与运维（**只给宿主 API，不当模型工具**）
    # ------------------------------------------------------------------

    def add_chat_record(
        self,
        user_text: str,
        assistant_text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把一轮对话存成 ``/records/<日期>/<时间>_<rand>.md`` + 一个记录实体。

        与既有实现一致：记录实体带 ``timestamp``/``path`` 属性，前后两条用 ``next``
        边串成链（维护 Agent 靠这条链做增量整理）。
        """
        user_text = str(user_text or "")
        assistant_text = str(assistant_text or "")
        stamp = time.strftime("%Y-%m-%d/%H%M%S", time.localtime())
        path = f"/records/{stamp}_{uuid.uuid4().hex[:6]}.md"
        content = f"## 用户\n\n{user_text}\n\n## 助手\n\n{assistant_text}\n"
        description = f"Chat record: {user_text[:120]}" if user_text else ""
        node = self._require_store().write(
            path, content, description=description, declared_by="chat_record"
        )
        extra: dict[str, Any] = {
            "user_preview": user_text[:200],
            "assistant_preview": assistant_text[:200],
        }
        if attachments:
            extra["attachments"] = attachments[:10]
        self._add_record_entity("chat_record", node.path, extra)
        self._revision = self._require_store().revision()
        self._notify_indexer()
        self._schedule_extraction(node.path)
        return {"path": node.path, "description": description}

    def write_diary(self, content: str) -> dict[str, Any]:
        """写一篇日记（``/diary/<日期>/<时间>_<rand>.md``）+ 一条 diary 记录实体。"""
        text = str(content or "")
        stamp = time.strftime("%Y-%m-%d/%H%M%S", time.localtime())
        path = f"/diary/{stamp}_{uuid.uuid4().hex[:6]}.md"
        node = self._require_store().write(
            path, text, description=text[:200], declared_by="diary"
        )
        self._add_record_entity("diary", node.path, {"preview": text[:200]})
        self._revision = self._require_store().revision()
        self._notify_indexer()
        self._schedule_extraction(node.path)
        return {"path": node.path, "description": text[:200]}

    def declare_file_update(self, file_path: str, kb_path: str | None = None) -> dict[str, Any]:
        """把宿主磁盘上的一个 UTF-8 文本文件导入记忆（``/imports/<名字>``）。

        这是**宿主 API**，不是模型工具：读任意本地文件不该由模型发起。
        """
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"源文件不存在: {source}")
        target = kb_path or f"/imports/{source.name}"
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"源文件不是 UTF-8 文本，不能导入记忆: {source}") from exc
        node = self._require_store().write(
            target, text, description="", declared_by=str(source)
        )
        self._revision = self._require_store().revision()
        self._notify_indexer()
        self._schedule_extraction(node.path)
        return {"path": node.path, "source": str(source), "declared_by": str(source)}

    def write_attachment(
        self,
        path: str,
        data: bytes,        *,
        content_type: str = "image/png",
        description: str = "",
    ) -> dict[str, Any]:
        node = self._require_store().write_attachment(
            path, data, content_type=content_type, description=description
        )
        self._revision = self._require_store().revision()
        self._notify_indexer()
        return {
            "path": node.path,
            "content_type": content_type,
            "description": description,
            "bytes": len(data),
        }

    def _add_record_entity(self, record_type: str, path: str, extra: dict[str, Any]) -> str:
        graph = self.graph
        name = (
            f"{record_type}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        )
        entity = graph.add(
            name,
            record_type,
            properties={"timestamp": epoch_to_iso(time.time()), "path": path, **extra},
            kb_refs=[path],
        )
        graph.add_relation(path_id(path), entity.id, "from")
        previous = self._latest_record_entity(record_type, exclude=entity.id)
        if previous:
            graph.add_relation(previous, entity.id, "next")
        return entity.id

    def _latest_record_entity(self, record_type: str, *, exclude: str | None = None) -> str | None:
        best: str | None = None
        best_ts = ""
        for entity in self.graph.search("", type_filter=record_type, top_k=10_000):
            if exclude and entity.id == exclude:
                continue
            stamp = str(entity.properties.get("timestamp") or "")
            if stamp > best_ts:
                best_ts = stamp
                best = entity.id
        return best

    # ------------------------------------------------------------------
    # 懒加载投影
    # ------------------------------------------------------------------

    def _build_vfs(self) -> VirtualFileSystem:
        """建**空**投影：子节点由 provider 按需查库，不预热整棵树。"""
        fs = VirtualFileSystem(owner_id="memory")
        fs.root.create_handler = self._create_child
        fs.root.child_provider = self._child
        fs.root.children_provider = self._children
        fs.root.meta["path"] = "/"
        return fs

    def _node_for(self, node: MemoryNode) -> VfsNode:
        """把 store 的节点投影成 VFS 节点（目录带 provider/create_handler）。"""
        if node.kind == "dir":
            return VfsNode(
                name=node.name,
                is_directory=True,
                description=node.description,
                create_handler=self._create_child,
                child_provider=self._child,
                children_provider=self._children,
                meta={"path": node.path, "tags": list(node.tags)},
            )
        return VfsNode(
            name=node.name,
            content=None,
            description=node.description,
            provider=_entry_provider(self._store, node.path),
            write_handler=self._write_existing,
            edit_handler=self._edit_existing,
            meta={
                "path": node.path,
                "description": node.description,
                "updated_at": node.updated_at,
                "line_count": _line_count(self._store, node.path),
                "tags": list(node.tags),
                "content_type": node.content_type,
            },
        )

    def _child(self, dir_node: VfsNode, name: str) -> VfsNode | None:
        """``child_provider``：按名字查一层（一次带索引的 DB 查询）。"""
        store = self._store
        if store is None:
            return None
        base = str(dir_node.meta.get("path") or "/")
        target = normalize_memory_path(posixpath.join(base, name))
        node = store.node(target)
        if node is None or node.name != name:
            return None
        return self._node_for(node)

    def _children(self, dir_node: VfsNode) -> list[VfsNode]:
        """``children_provider``：列一层（上限 ``list_limit``，多的用工具分页）。"""
        store = self._store
        if store is None:
            return []
        base = str(dir_node.meta.get("path") or "/")
        return [self._node_for(node) for node in store.children(base, limit=self.list_limit)]

    # -- 写处理器 -------------------------------------------------------

    def _create_child(self, dir_node: VfsNode, relative: str, content: VfsContent) -> str:
        store = self._require_store()
        text = _require_text(content, dir_node)
        base = str(dir_node.meta.get("path") or "/")
        target = normalize_memory_path(posixpath.join(base, relative))
        if target == base:
            raise VfsError(f"目标路径为空: {relative!r}")
        store.write(target, text)
        self._revision = store.revision()
        self._notify_indexer()
        self._schedule_extraction(target)
        return f"已存入记忆 {target}"

    def _write_existing(self, node: VfsNode, content: VfsContent) -> str:
        store = self._require_store()
        text = _require_text(content, node)
        path = str(node.meta.get("path") or "")
        store.write(path, text)
        self._revision = store.revision()
        self._notify_indexer()
        self._schedule_extraction(path)
        self._refresh_meta(node, path)
        return f"已存入记忆 {path}"

    def _edit_existing(self, node: VfsNode, old: str, kwargs: dict[str, Any]) -> str:
        store = self._require_store()
        path = str(node.meta.get("path") or "")
        current = store.read(path)
        text = current.text or ""
        new = str(kwargs.get("new", ""))
        if old not in text:
            raise VfsError(f"{path} 中找不到要替换的片段")
        store.write(path, text.replace(old, new, 1))
        self._revision = store.revision()
        self._notify_indexer()
        self._schedule_extraction(path)
        self._refresh_meta(node, path)
        return f"已更新记忆 {path}"

    def _refresh_meta(self, node: VfsNode, path: str) -> None:
        store = self._store
        if store is None:
            return
        entry = store.read(path)
        node.meta.update(
            {
                "path": path,
                "description": entry.description,
                "updated_at": entry.updated_at,
                "line_count": entry.line_count,
                "tags": list(entry.tags),
                "content_type": entry.content_type,
            }
        )


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _require_text(content: VfsContent, node: VfsNode) -> str:
    if isinstance(content, Text):
        return content.text
    raise VfsError(f"记忆只支持文本写入: {node.meta.get('path') or node.name}")


def _line_count(store: SqliteMemoryStore | None, path: str) -> int | None:
    if store is None:
        return None
    try:
        entry: MemoryEntry = store.read(path)
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    return entry.line_count


def _entry_provider(store: SqliteMemoryStore | None, path: str):
    def load(_node: VfsNode) -> VfsContent:
        if store is None:
            raise VfsError("MemoryRealm 未 start()")
        entry = store.read(path)
        if entry.is_image:
            assert entry.data is not None
            return Image(entry.data, entry.content_type)
        if entry.text is None:
            return Binary(entry.data or b"", entry.content_type or "application/octet-stream")
        return Text(entry.text)

    return load


__all__ = ["DEFAULT_LIST_LIMIT", "MOUNT_PREFIX", "SCHEME", "STORE_DIR_NAME", "MemoryRealm"]
