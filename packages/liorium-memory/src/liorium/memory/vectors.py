"""向量索引后端：``nano-vectordb`` 兼容实现 + 后端协议。

**兼容性是这个模块的唯一硬约束**（计划 §关键决策 2）：写出的
``<memory>/index/chunks.vdb`` 必须能被既有实现直接用
``nano_vectordb.NanoVectorDB(1536, storage_file=...)`` 打开并检索，反之亦然。
所以：

- 文件格式交给 nano 自己（``{embedding_dim, data, matrix}``，``matrix`` 是
  base64 的 float32），**不自己造格式**；
- 维度钉死 :data:`EMBED_DIM`（1536，与旧库一致）；
- metadata 键与既有实现逐字相同：``node_path`` / ``scope_prefix`` /
  ``chunk_index`` / ``text`` / ``text_preview``，``__id__`` 就是 ``chunk_id``；
- cosine 归一化由 nano 负责（加载时统一归一化，查询时归一化 query）。

与旧的差别只在**落盘策略**：全量向量常驻内存是 nano 的固有代价
（``N×1536×4B``），这里做三件事把它变成"可控"而不是"不可控"：

1. **惰性加载**：第一次 ``query``/``upsert`` 才读文件；
2. **延迟落盘**：``flush()`` 由调用方（``indexer``）在空闲时批量调用，
   不要每次写 chunk 都重写整个文件；
3. **``unload()``**：不检索时能把矩阵从内存里放掉（RAM 立刻回落）。
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np
from nano_vectordb import NanoVectorDB

from liorium.core.log import get_logger

log = get_logger("liorium.memory.vectors")

EMBED_DIM = 1536
"""与既有实现一致：换维度 = 断兼容（nano 加载时会 assert）。"""

VECTOR_FILE_NAME = "chunks.vdb"
"""关键字面文件名必须与既有实现相同（``<memory>/index/chunks.vdb``）。"""

ENTITY_VECTOR_FILE_NAME = "entity.vdb"

METADATA_KEYS = ("node_path", "scope_prefix", "chunk_index", "text", "text_preview")
"""既有实现写进 ``.vdb`` 的 metadata 键（逐字相同，少一个都算不兼容）。"""


class VectorBackendError(RuntimeError):
    """向量后端不可用（文件损坏且无法重建、维度不匹配等）。"""


@dataclass(frozen=True)
class VectorRecord:
    """一条待写入的向量：``chunk_id`` + 向量 + 既有实现同款 metadata。"""

    chunk_id: str
    vector: Sequence[float]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    score: float
    """cosine 相似度（``[-1, 1]``，越大越像）。"""
    metadata: dict[str, Any]
    rank: int = 0
    """1 起的位次（融合用位次，不混量纲）。"""


@runtime_checkable
class VectorBackend(Protocol):
    """向量后端协议：``NanoVectorBackend`` 是本仓唯一实现，接口留给别的后端。"""

    dim: int

    def count(self) -> int: ...

    def ids(self) -> set[str]: ...

    async def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    async def delete(self, chunk_ids: Sequence[str]) -> int: ...

    async def query(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 10,
        scope_prefix: str = "/",
    ) -> list[VectorHit]: ...

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


class NanoVectorBackend:
    """``nano-vectordb`` 后端（全量矩阵在内存，落盘整文件重写）。

    ``path`` 指向 ``<memory>/index/chunks.vdb``；文件不存在时是空库。
    """

    def __init__(self, path: Path | str, *, dim: int = EMBED_DIM) -> None:
        self.path = Path(path)
        self.dim = int(dim)
        self._db: NanoVectorDB | None = None
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0

    # -- 状态 -----------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._db is not None

    @property
    def dirty(self) -> bool:
        return self._dirty

    def count(self) -> int:
        with self._lock:
            return len(self._ensure())

    def ids(self) -> set[str]:
        with self._lock:
            return {str(row["__id__"]) for row in _rows(self._ensure())}

    # -- 写 -------------------------------------------------------------

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        payload = list(records)
        if not payload:
            return 0
        for record in payload:
            if len(record.vector) != self.dim:
                raise VectorBackendError(
                    f"向量维度不对: {record.chunk_id} 期望 {self.dim} 实际 {len(record.vector)}"
                )
        await asyncio.to_thread(self._upsert_sync, payload)
        return len(payload)

    def _upsert_sync(self, records: Sequence[VectorRecord]) -> None:
        with self._lock:
            db = self._ensure()
            rows = [
                {
                    "__id__": record.chunk_id,
                    "__vector__": np.asarray(record.vector, dtype=np.float32),
                    **{key: record.metadata.get(key, "") for key in METADATA_KEYS},
                }
                for record in records
            ]
            db.upsert(rows)  # nano 会就地删掉 __vector__ 并回填 __id__
            self._dirty = True

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        ids = [str(i) for i in chunk_ids]
        if not ids:
            return 0
        return await asyncio.to_thread(self._delete_sync, ids)

    def _delete_sync(self, ids: Sequence[str]) -> int:
        with self._lock:
            db = self._ensure()
            before = len(db)
            db.delete(list(ids))
            removed = before - len(db)
            if removed:
                self._dirty = True
            return removed

    async def flush(self) -> None:
        """落盘（整文件重写）。无改动时是空操作。"""
        if not self._dirty:
            return
        await asyncio.to_thread(self._flush_sync)

    def _flush_sync(self) -> None:
        with self._lock:
            if self._db is None or not self._dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            # 先写 tmp 再替换：nano 的 save() 只接受文件路径，直接覆盖写一半会毁库。
            self._db.storage_file = str(tmp)
            try:
                self._db.save()
            finally:
                self._db.storage_file = str(self.path)
            tmp.replace(self.path)
            self._dirty = False
            self._last_flush = time.time()

    def unload(self) -> None:
        """把矩阵从内存里放掉（下次查询会重新读文件）。"""
        with self._lock:
            self._db = None

    async def aclose(self) -> None:
        await self.flush()
        self.unload()

    # -- 读 -------------------------------------------------------------

    async def query(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 10,
        scope_prefix: str = "/",
    ) -> list[VectorHit]:
        if len(vector) != self.dim:
            raise VectorBackendError(
                f"查询向量维度不对: 期望 {self.dim} 实际 {len(vector)}"
            )
        return await asyncio.to_thread(self._query_sync, list(vector), int(top_k), str(scope_prefix or "/"))

    def _query_sync(self, vector: Sequence[float], top_k: int, scope_prefix: str) -> list[VectorHit]:
        with self._lock:
            db = self._ensure()
            rows = _rows(db)
            if not rows:
                return []
            scope = scope_prefix.rstrip("/")
            filter_lambda = None
            if scope not in ("", "/"):
                prefix = scope + "/"

                def matches(row: Mapping[str, Any]) -> bool:
                    path = str(row.get("node_path") or "")
                    return path == scope or path.startswith(prefix)

                matched = sum(1 for row in rows if matches(row))
                # nano 的 filter_lambda 在全不命中时会用空的 float 数组取下标 → IndexError
                # （0.0.4.3 的 bug）。先自己数一遍，全不命中直接返回。
                if matched == 0:
                    return []
                if matched != len(rows):
                    filter_lambda = matches

            raw = db.query(
                np.asarray(vector, dtype=np.float32),
                top_k=max(1, int(top_k)),
                filter_lambda=filter_lambda,
            )
        hits: list[VectorHit] = []
        for index, item in enumerate(raw or [], 1):
            score = float(item.get("__metrics__", 0.0))
            if not math.isfinite(score):
                score = 0.0
            metadata = {key: item.get(key, "") for key in METADATA_KEYS}
            hits.append(
                VectorHit(
                    chunk_id=str(item.get("__id__", "")),
                    score=score,
                    metadata=metadata,
                    rank=index,
                )
            )
        return hits

    # -- 内部 -----------------------------------------------------------

    def _ensure(self) -> NanoVectorDB:
        if self._db is not None:
            return self._db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_compatible()
        try:
            self._db = NanoVectorDB(self.dim, storage_file=str(self.path))
        except Exception as exc:  # noqa: BLE001 - JSON 坏了 / matrix 长度和维度对不上
            backup = self.path.with_suffix(self.path.suffix + f".corrupt-{int(time.time())}")
            if self.path.exists():
                self.path.replace(backup)
                log.error(
                    "向量索引无法加载，已备份为 %s 并重建（原错误: %s）", backup.name, exc
                )
            else:
                raise VectorBackendError(f"向量索引创建失败: {exc}") from exc
            self._db = NanoVectorDB(self.dim, storage_file=str(self.path))
        return self._db

    def _check_compatible(self) -> None:
        """维度不一致**直接报错**：不能备份重建（那等于把已有向量全丢了）。"""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # 坏文件走"备份重建"路径
        dim = raw.get("embedding_dim") if isinstance(raw, dict) else None
        if dim is None:
            return
        if int(dim) != self.dim:
            raise VectorBackendError(
                f"向量索引维度不兼容: 文件 {self.path} 是 {dim}，本后端是 {self.dim}"
                "（换 embedding 模型必须换库或清空 index/chunks.vdb）"
            )


def _rows(db: NanoVectorDB) -> list[dict[str, Any]]:
    """取 nano 的全部 metadata 行（它没有公开的"遍历全部 id"接口）。

    nano 用 ``self.__storage``（名字改写成 ``_NanoVectorDB__storage``）存
    ``{embedding_dim, data, matrix}``；版本 pin 在 0.0.4.3，升级前必须重跑兼容测试。
    """
    storage = getattr(db, "_NanoVectorDB__storage", None)
    if not isinstance(storage, dict):
        raise VectorBackendError("nano-vectordb 内部结构变了（pin 的 0.0.4.3 才有 __storage）")
    return storage["data"]


__all__ = [
    "EMBED_DIM",
    "ENTITY_VECTOR_FILE_NAME",
    "METADATA_KEYS",
    "NanoVectorBackend",
    "VECTOR_FILE_NAME",
    "VectorBackend",
    "VectorBackendError",
    "VectorHit",
    "VectorRecord",
]
