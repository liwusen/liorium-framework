"""后台索引器：把 ``vector_jobs`` 队列里的 chunk embed 进向量库。

为什么是队列 + 后台任务，而不是"写完同步 embed"：

- VFS 的写处理器是**同步**的（``create_handler`` 返回 ACK 字符串），没法在写路径上
  await 网络；embedding 又是几百毫秒级的网络调用；
- 记忆检索是"最终一致"就够的：新写的记忆立刻能被**关键词**检索到（FTS 在写事务里
  同步更新），向量最多晚几秒。

失败策略：embedding 失败**不 ack**（下轮重试），但同一批连续失败到
``max_attempts`` 次就丢掉并打 ERROR——否则一个毒丸（比如服务端永久 400）会让整个
队列永远堵住。丢掉的 chunk 可以用 ``reconcile()`` 重新补投（它会比对
``chunks`` 表与向量库里的 id），所以丢弃是可恢复的。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from liorium.core.log import get_logger

from .store import SqliteMemoryStore
from .vectors import METADATA_KEYS, VectorBackend, VectorRecord

if TYPE_CHECKING:  # pragma: no cover - 只用于类型
    from .embeddings import Embeddings

log = get_logger("liorium.memory.indexer")


class MemoryIndexer:
    """向量队列的消费者（由宿主用 ``ctx.spawn`` 托管）。"""

    def __init__(
        self,
        store: SqliteMemoryStore,
        *,
        embedder: "Embeddings",
        vectors: VectorBackend,
        batch_size: int = 8,
        poll_interval: float = 0.5,
        flush_delay: float = 5.0,
        max_attempts: int = 5,
        error_backoff: float = 10.0,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.vectors = vectors
        self.batch_size = max(1, int(batch_size))
        self.poll_interval = max(0.05, float(poll_interval))
        self.flush_delay = max(0.0, float(flush_delay))
        self.max_attempts = max(1, int(max_attempts))
        self.error_backoff = max(0.0, float(error_backoff))
        self.processed = 0
        self.failed = 0
        self.last_error: str | None = None
        self._closing = False
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attempts: dict[str, int] = {}
        self._last_flush = time.monotonic()
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """主循环（宿主 ``ctx.spawn(self._indexer.run())``）。"""
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = asyncio.current_task()
        try:
            while not self._closing:
                processed = await self.run_once()
                if processed > 0:
                    continue
                await self.flush()
                timeout = self.poll_interval if processed == 0 else self.error_backoff
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                else:
                    self._wake.clear()
        finally:
            await self.flush()
            log.info(
                "记忆索引器退出: 处理 %d 条，失败 %d 条", self.processed, self.failed
            )

    async def aclose(self, timeout: float = 5.0) -> None:
        """请求退出并等主循环收尾（幂等；宿主 stop 时先调它再取消任务）。"""
        self._closing = True
        self.notify()
        task = getattr(self, "_task", None)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("记忆索引器退出超时，强制取消")
                task.cancel()
        await self.flush()

    def notify(self) -> None:
        """有新作业时叫醒主循环（可从**任意线程**调用）。"""
        event, loop = self._wake, self._loop
        if event is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # 事件循环已经关了（退出中）
            pass

    # ------------------------------------------------------------------
    # 处理
    # ------------------------------------------------------------------

    async def run_once(self) -> int:
        """处理一批作业；返回处理条数（0 = 队列空，-1 = 本轮失败）。"""
        fts = self.store.fts
        if fts is None:
            return 0
        jobs = fts.pending_jobs(limit=self.batch_size * 4)
        if not jobs:
            return 0
        try:
            await self._process(jobs)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能因为一轮失败就死掉
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("记忆索引失败（%d 条作业留在队列里重试）: %s", len(jobs), exc, exc_info=exc)
            self._record_attempts(jobs)
            return -1
        fts.ack_jobs([job["chunk_id"] for job in jobs])
        for job in jobs:
            self._attempts.pop(job["chunk_id"], None)
        self.processed += len(jobs)
        self.last_error = None
        self.notify()  # 还有剩的作业就继续跑
        return len(jobs)

    async def _process(self, jobs: list[dict]) -> None:
        deletes = [job for job in jobs if job["op"] == "delete" or not job["payload"].get("text")]
        if deletes:
            await self.vectors.delete([str(job["chunk_id"]) for job in deletes])

        upserts = [job for job in jobs if job not in deletes]
        if not upserts:
            return
        alive = self.store.existing_chunk_ids([str(job["chunk_id"]) for job in upserts])
        dead = [job for job in upserts if str(job["chunk_id"]) not in alive]
        if dead:
            # 入队后又被删掉的 chunk：别浪费 embedding 调用，直接确保向量库里没有。
            await self.vectors.delete([str(job["chunk_id"]) for job in dead])
        live = [job for job in upserts if str(job["chunk_id"]) in alive]
        if not live:
            return
        vectors = await self.embedder.embed([str(job["payload"].get("text") or "") for job in live])
        records = [
            VectorRecord(
                chunk_id=str(job["chunk_id"]),
                vector=vector,
                metadata={key: job["payload"].get(key, "") for key in METADATA_KEYS},
            )
            for job, vector in zip(live, vectors)
        ]
        await self.vectors.upsert(records)

    def _record_attempts(self, jobs: list[dict]) -> None:
        dropped: list[str] = []
        for job in jobs:
            chunk_id = str(job["chunk_id"])
            count = self._attempts.get(chunk_id, 0) + 1
            self._attempts[chunk_id] = count
            if count >= self.max_attempts:
                dropped.append(chunk_id)
        if not dropped:
            return
        fts = self.store.fts
        if fts is not None:
            fts.ack_jobs(dropped)
        for chunk_id in dropped:
            self._attempts.pop(chunk_id, None)
        log.error(
            "这些 chunk 连续 %d 次 embed 失败，已移出队列（可 reconcile() 补投）: %s",
            self.max_attempts,
            dropped[:5],
        )

    async def drain(self, *, max_batches: int = 200) -> int:
        """把队列跑到空（测试/CLI 显式索引用）。"""
        total = 0
        for _ in range(max(1, int(max_batches))):
            count = await self.run_once()
            if count <= 0:
                break
            total += count
        await self.flush(force=True)
        return total

    async def flush(self, *, force: bool = False) -> None:
        """向量落盘（整文件重写，所以按 ``flush_delay`` 攒批）。"""
        dirty = getattr(self.vectors, "dirty", False)
        if not dirty:
            return
        if not force and (time.monotonic() - self._last_flush) < self.flush_delay:
            return
        await self.vectors.flush()
        self._last_flush = time.monotonic()

    # ------------------------------------------------------------------
    # 自愈
    # ------------------------------------------------------------------

    async def reconcile(self, *, batch_size: int = 500) -> dict[str, int]:
        """比对 ``chunks`` 与向量库：补投缺失的、删除多余的。

        启动时按水位自愈（删了 ``chunks.vdb`` / 首次给旧库开向量 / 毒丸被丢弃后
        语义检索会永久为空）。补投分批发，避免一次把全库 chunk 行读进内存。
        """
        fts = self.store.fts
        if fts is None:
            return {"missing": 0, "stale": 0}
        chunk_ids = set(self.store.iter_chunk_ids())
        vector_ids = set(await asyncio.to_thread(self.vectors.ids))
        missing = chunk_ids - vector_ids
        stale = vector_ids - chunk_ids
        if missing:
            rows = self.store.chunk_rows_by_ids(missing)
            step = max(1, int(batch_size))
            for start in range(0, len(rows), step):
                fts.enqueue_vectors(rows[start : start + step])
        if stale:
            fts.enqueue_deletes(stale)
        if missing or stale:
            log.info("向量索引对账: 补投 %d 条，清理 %d 条", len(missing), len(stale))
        return {"missing": len(missing), "stale": len(stale)}

    def status(self) -> dict[str, object]:
        fts = self.store.fts
        return {
            "pending": fts.job_count() if fts is not None else 0,
            "processed": self.processed,
            "failed": self.failed,
            "last_error": self.last_error,
            "vectors": self.vectors.count() if getattr(self.vectors, "loaded", False) else None,
        }


__all__ = ["MemoryIndexer"]
