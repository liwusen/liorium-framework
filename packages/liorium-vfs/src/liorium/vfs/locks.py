"""异步读写锁。

一处实现，VFS 与 realm 状态 store 共用。写者优先（避免写饥饿）。
"""

from __future__ import annotations

import asyncio


class AsyncRWLock:
    """写者优先的异步读写锁。"""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    async def acquire_read(self) -> None:
        async with self._cond:
            while self._writer or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self) -> None:
        async with self._cond:
            self._readers = max(0, self._readers - 1)
            if self._readers == 0:
                self._cond.notify_all()

    async def acquire_write(self) -> None:
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers > 0:
                    await self._cond.wait()
                self._writer = True
            finally:
                self._writers_waiting = max(0, self._writers_waiting - 1)
                # 等待者被取消时不会走到 release_write()；如果它是最后一个排队
                # 写者，必须唤醒被"写者优先"挡住、但已无写者可等的读者，
                # 否则这些读者会永久睡下去。
                if self._writers_waiting == 0 and not self._writer:
                    self._cond.notify_all()

    async def release_write(self) -> None:
        async with self._cond:
            self._writer = False
            self._cond.notify_all()

    def read_lock(self) -> "AsyncRWLock._ReadCtx":
        return AsyncRWLock._ReadCtx(self)

    def write_lock(self) -> "AsyncRWLock._WriteCtx":
        return AsyncRWLock._WriteCtx(self)

    class _ReadCtx:
        def __init__(self, lock: "AsyncRWLock") -> None:
            self._lock = lock

        async def __aenter__(self) -> "AsyncRWLock._ReadCtx":
            await self._lock.acquire_read()
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            await self._lock.release_read()
            return False

    class _WriteCtx:
        def __init__(self, lock: "AsyncRWLock") -> None:
            self._lock = lock

        async def __aenter__(self) -> "AsyncRWLock._WriteCtx":
            await self._lock.acquire_write()
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            await self._lock.release_write()
            return False


__all__ = ["AsyncRWLock"]
