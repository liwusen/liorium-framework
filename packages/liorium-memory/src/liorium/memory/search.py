"""混合检索：关键词（FTS5 BM25）+ 语义（向量）+ 图谱，RRF 融合。

为什么是 RRF（Reciprocal Rank Fusion）而不是既有实现的"归一化后加权平均"：

- 既有实现把 cosine 和 BM25 各自除以本路最大值再 ``0.5*v + 0.5*b`` 相加——两路的
  量纲和分布完全不同（cosine 挤在 0.6~0.9，BM25 是长尾），除最大值等于让某一路的
  "第一名" 恒等于 1，排名信息被压掉了；
- RRF 只用**位次**：``score = Σ weight / (k + rank)``，与分数量纲无关，不用调参就
  稳；再乘 ``k`` 让分数落在"每路 0~weight"的可读区间，``score_patch`` 这类
  ±0.15 的人工微调才不会被淹没。

不迁 rerank（用户明确要求）：融合完就是终值。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from liorium.core.log import get_logger

from .store import SqliteMemoryStore, normalize_memory_path

if TYPE_CHECKING:  # pragma: no cover - 只用于类型，运行时不 import（避免硬依赖 numpy）
    from .embeddings import Embeddings
    from .graph import EntityGraph
    from .vectors import VectorBackend

log = get_logger("liorium.memory.search")

RETURN_MODES = ("paths", "compact", "snippets", "full")
"""四种返回形态：只要路径 / 路径+元数据 / 再带片段 / 再带全文。"""

MODES = ("auto", "hybrid", "keyword", "vector")
"""检索模式；``auto`` = 有向量就 hybrid，没有就 keyword。"""


@dataclass(frozen=True)
class SearchConfig:
    """融合参数（默认值就是推荐值，别乱调）。"""

    rrf_k: int = 60
    """RRF 的平滑常数：越大越"平"，位次差距的影响越小。"""
    per_list: int = 40
    """每一路取多少条候选进融合。"""
    vector_weight: float = 1.0
    keyword_weight: float = 1.0
    graph_weight: float = 0.35
    graph_hops: int = 2
    """图谱扩展走几跳（文档 → 实体 → 别的文档 = 2 跳）。"""
    graph_seeds: int = 5
    """拿融合后的前几条结果去扩展图谱。"""
    recency_weight: float = 0.05
    """新鲜度加成上限（加在 RRF 分之上，量级约等于 3 个位次）。"""
    recency_days: float = 30.0
    tag_bonus: float = 0.1
    """命中查询标签的额外加成（标签同时也是过滤条件）。"""
    min_vector_score: float = 0.0
    """向量路的最低 cosine：``<=`` 这个值的命中丢掉（nano 总会返回 top_k，不设门槛
    等于把"完全不像"的文档也塞进融合）。"""


@dataclass(frozen=True)
class SearchResult:
    """一条检索结果（融合后的最终形态）。"""

    path: str
    score: float
    snippet: str = ""
    description: str = ""
    updated_at: str = ""
    tags: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    """这条结果是被哪几路召回的：``keyword`` / ``vector`` / ``graph``。"""
    matched_chunks: int = 0

    @property
    def uri(self) -> str:
        return "memories://" + self.path.lstrip("/")

    def to_dict(self, *, content: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "uri": self.uri,
            "score": round(self.score, 4),
            "description": self.description,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            "sources": list(self.sources),
            "snippet": self.snippet,
        }
        if content is not None:
            payload["content"] = content
        return payload


class MemorySearch:
    """在 ``SqliteMemoryStore`` 之上做混合检索。

    ``embedder`` / ``vectors`` 都可为 ``None``：

    - 都没有 ⇒ 纯关键词（FTS5；没开索引时退回 ``store.search``）；
    - 有 ``vectors`` 没 ``embedder`` ⇒ 建库可以用，但**查询**没法把 query 变向量
      ⇒ 自动降级成关键词（并在日志里说明），不静默给空结果。
    """

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        embedder: "Embeddings | None" = None,
        vectors: "VectorBackend | None" = None,
        graph: "EntityGraph | None" = None,
        config: SearchConfig | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.vectors = vectors
        self.graph = graph
        self.config = config or SearchConfig()
        self.last_vector_error: str | None = None
        """最近一次语义路失败原因（``None`` = 最近一次检索语义路正常或未启用）。"""

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    @property
    def vector_ready(self) -> bool:
        return self.vectors is not None and self.embedder is not None

    async def search(
        self,
        query: str,
        *,
        scope: str = "/",
        tags: Iterable[str] = (),
        top_k: int = 5,
        return_mode: str = "compact",
        mode: str = "auto",
    ) -> list[dict[str, Any]]:
        results = await self.rank(
            query, scope=scope, tags=tags, top_k=top_k, mode=mode
        )
        return self.render(results, return_mode=return_mode)

    async def rank(
        self,
        query: str,
        *,
        scope: str = "/",
        tags: Iterable[str] = (),
        top_k: int = 5,
        mode: str = "auto",
    ) -> list[SearchResult]:
        text = str(query or "").strip()
        if not text:
            return []
        if mode not in MODES:
            raise ValueError(f"mode 只支持 {MODES}: {mode!r}")
        scope_path = normalize_memory_path(scope)
        wanted = tuple(dict.fromkeys(str(t).strip() for t in tags if str(t).strip()))

        lists: list[tuple[str, list[str], float]] = []
        keyword = self._keyword_paths(text, scope_path)
        if keyword:
            lists.append(("keyword", keyword, self.config.keyword_weight))

        use_vector = mode in ("auto", "hybrid", "vector")
        if use_vector and self.vector_ready:
            # 语义路是增强项：embedding 的 429/500/超时不能把已经算好的关键词
            # 结果一起丢掉（否则一次瞬时故障 = 整次检索失败）。
            try:
                vector = await self._vector_paths(text, scope_path)
                self.last_vector_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_vector_error = f"{type(exc).__name__}: {exc}"
                log.warning("语义检索失败，降级为关键词结果: %s", self.last_vector_error)
                vector = []
            if vector:
                lists.append(("vector", vector, self.config.vector_weight))
        elif mode == "vector" and not self.vector_ready:
            log.warning("向量后端/embedder 未就绪，vector 模式退回关键词检索")
            if not keyword:
                return []

        if not lists:
            return []

        # 图谱扩展要先知道"哪几条最相关"，所以先粗融合一次拿种子，再补第三路
        if self.graph is not None and mode in ("auto", "hybrid"):
            seeds = [
                path
                for path in self._fuse(lists, top_k=max(1, int(self.config.graph_seeds)))
            ]
            if seeds:
                expanded = self.graph.expand_documents(
                    seeds, hops=self.config.graph_hops, limit=max(1, int(self.config.per_list))
                )
                if expanded:
                    lists.append(("graph", expanded, self.config.graph_weight))

        fused = self._fuse(lists)

        meta = self.store.meta_many(fused.keys())
        previews = self.store.preview_many(fused.keys())
        now = time.time()
        results: list[SearchResult] = []
        for path, bucket in fused.items():
            info = meta.get(path)
            if info is None or info.get("kind") != "file":
                continue  # 目录节点不参与检索（检索是"找内容"，目录用 list）
            node_tags = tuple(info.get("tags") or ())
            if wanted and not set(wanted) <= {t.casefold() for t in node_tags}:
                continue
            score = float(bucket["score"])
            score += float(info.get("score_patch") or 0.0)
            age_days = max(0.0, (now - float(info.get("updated_ts") or 0.0)) / 86400.0)
            score += self.config.recency_weight * math.exp(
                -age_days / max(1e-6, self.config.recency_days)
            )
            if wanted and set(wanted) <= {t.casefold() for t in node_tags}:
                score += self.config.tag_bonus
            results.append(
                SearchResult(
                    path=path,
                    score=score,
                    snippet=str(bucket.get("snippet") or previews.get(path, "")),
                    description=str(info.get("description") or ""),
                    updated_at=str(info.get("updated_at") or ""),
                    tags=node_tags,
                    sources=tuple(dict.fromkeys(bucket["sources"])),
                    matched_chunks=int(bucket.get("chunks") or 0),
                )
            )
        results.sort(key=lambda item: (-item.score, item.path))
        return results[: max(1, int(top_k))]

    # ------------------------------------------------------------------
    # 各路召回
    # ------------------------------------------------------------------

    def _fuse(
        self, lists: Sequence[tuple[str, Sequence[str], float]], *, top_k: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """RRF 融合：``Σ weight * k / (k + rank)``（乘 k 后每路贡献落在 0~weight）。"""
        fused: dict[str, dict[str, Any]] = {}
        k = max(1, int(self.config.rrf_k))
        for source, paths, weight in lists:
            for rank, path in enumerate(paths, 1):
                bucket = fused.setdefault(
                    path, {"score": 0.0, "sources": [], "snippet": "", "chunks": 0}
                )
                bucket["score"] += weight * (k / (k + rank))
                bucket["sources"].append(source)
                bucket["chunks"] += 1
        if top_k is None:
            return fused
        ordered = sorted(fused.items(), key=lambda item: (-item[1]["score"], item[0]))
        return dict(ordered[: max(1, int(top_k))])

    def _keyword_paths(self, query: str, scope: str) -> list[str]:
        """关键词路：FTS5 BM25（没开索引时退回 LIKE 打分）。"""
        limit = max(1, int(self.config.per_list))
        fts = self.store.fts
        if fts is not None:
            hits = fts.query(query, top_k=limit, scope_prefix=scope)
            return _paths_in_order(
                (hit.node_path, hit.chunk_id) for hit in hits
            )
        hits = self.store.search(query, scope=scope, top_k=limit)
        return [hit.path for hit in hits]

    async def _vector_paths(self, query: str, scope: str) -> list[str]:
        """语义路：把 query embed 一次，再按 scope 过 ``filter_lambda``。"""
        assert self.embedder is not None and self.vectors is not None
        vectors = await self.embedder.embed([query])
        if not vectors:
            return []
        hits = await self.vectors.query(
            vectors[0], top_k=max(1, int(self.config.per_list)), scope_prefix=scope
        )
        floor = float(self.config.min_vector_score)
        return _paths_in_order(
            (str(hit.metadata.get("node_path") or ""), hit.chunk_id)
            for hit in hits
            if hit.score > floor
        )

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(
        self,
        results: Sequence[SearchResult],
        *,
        return_mode: str = "compact",
    ) -> list[dict[str, Any]]:
        mode = str(return_mode or "compact").strip().lower()
        if mode not in RETURN_MODES:
            raise ValueError(f"return_mode 只支持 {RETURN_MODES}: {return_mode!r}")
        if mode == "paths":
            return [{"path": item.path} for item in results]
        need_content = mode == "full"
        payload: list[dict[str, Any]] = []
        for item in results:
            content: str | None = None
            if need_content:
                try:
                    content = self.store.read(item.path).text or ""
                except (FileNotFoundError, IsADirectoryError, OSError):
                    content = ""
            payload.append(item.to_dict(content=content))
        return payload


def _paths_in_order(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """``(path, chunk_id)`` 序列 → 按最佳命中排序的 path 列表（去重、保序）。"""
    seen: dict[str, None] = {}
    for path, _ in pairs:
        path = str(path or "")
        if not path or path in seen:
            continue
        seen[path] = None
    return list(seen)


__all__ = [
    "MODES",
    "RETURN_MODES",
    "MemorySearch",
    "SearchConfig",
    "SearchResult",
]
