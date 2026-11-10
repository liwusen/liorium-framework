"""记忆包测试的公共替身（假 embedder / 假后端）。"""

from __future__ import annotations

import math
import zlib
from typing import Any, Sequence


def _tokens(text: str) -> list[str]:
    """假 embedder 用的切词：CJK 单字 + ASCII 单词（够造出可控的"近义"向量）。"""
    out: list[str] = []
    word = ""
    for char in str(text or "").lower():
        if char.isascii() and char.isalnum():
            word += char
            continue
        if word:
            out.append(word)
            word = ""
        if not char.isascii() and char.strip():
            out.append(char)
    if word:
        out.append(word)
    return out


class FakeEmbedder:
    """确定性假 embedder：token → 固定桶的 bag-of-words。

    ``aliases`` 可以把不同 token 映射到同一个桶（例如 ``{"猫": "cat", "喵": "cat"}``），
    用来构造"关键词搜不到、向量搜得到"的用例。
    """

    def __init__(self, *, dim: int = 64, aliases: dict[str, str] | None = None) -> None:
        self.dim = int(dim)
        self.aliases = dict(aliases or {})
        self.calls: list[list[str]] = []
        self.closed = False

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = [str(t) for t in texts]
        self.calls.append(batch)
        return [self._vector(text) for text in batch]

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _tokens(text):
            key = self.aliases.get(token, token)
            vector[zlib.crc32(key.encode("utf-8")) % self.dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    async def aclose(self) -> None:
        self.closed = True


class RecordingVectors:
    """假向量后端：什么都不算，只记录调用（测索引器用）。"""

    dim = 64

    def __init__(self, *, fail_times: int = 0) -> None:
        self.rows: dict[str, Any] = {}
        self.fail_times = int(fail_times)
        self.calls: list[tuple[str, Any]] = []
        self.dirty = False
        self.flushes = 0

    def count(self) -> int:
        return len(self.rows)

    def ids(self) -> set[str]:
        return set(self.rows)

    async def upsert(self, records: Sequence[Any]) -> int:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("假装 embedding 服务挂了")
        self.calls.append(("upsert", len(records)))
        for record in records:
            self.rows[record.chunk_id] = record
        self.dirty = True
        return len(records)

    async def delete(self, chunk_ids: Sequence[str]) -> int:
        self.calls.append(("delete", list(chunk_ids)))
        removed = 0
        for chunk_id in chunk_ids:
            if self.rows.pop(str(chunk_id), None) is not None:
                removed += 1
        self.dirty = removed > 0
        return removed

    async def query(self, vector, *, top_k: int = 10, scope_prefix: str = "/"):  # noqa: ANN001
        raise AssertionError("RecordingVectors 不支持查询")

    async def flush(self) -> None:
        self.flushes += 1
        self.dirty = False

    async def aclose(self) -> None:
        await self.flush()


class FailingEmbedder(FakeEmbedder):
    """总是失败（测索引器的重试与丢弃）。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding 服务 500")
