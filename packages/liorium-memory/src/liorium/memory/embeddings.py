"""Embedding 服务：OpenAI 兼容 ``POST {base_url}/embeddings`` + 协议定义。

**库不读任何产品配置**（计划 §8.2）：``base_url`` / ``api_key`` / ``model`` 全部由
宿主注入（realm 工厂闭包或 CLI 参数），本模块不认识"配置文件"这种东西。

维度固定 1536（与旧库的 ``.vdb`` 一致）：请求里显式带 ``dimensions``，返回后逐条
校验——维度不对必须**立刻报错**，不能把不兼容的向量写进索引（nano 加载时会
assert，等于把库里已有的向量全废掉）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, Sequence, runtime_checkable

import httpx

from liorium.core.log import get_logger

from .vectors import EMBED_DIM

log = get_logger("liorium.memory.embeddings")


class EmbeddingError(RuntimeError):
    """Embedding 调用失败或返回不符合约定（维度/条数）。"""


@runtime_checkable
class Embeddings(Protocol):
    """Embedding 服务协议：批量把文本变成向量（与 ``texts`` **同序**）。"""

    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class OpenAICompatibleEmbeddings:
    """OpenAI 兼容的 ``/embeddings`` 客户端（httpx，批量 + 并发上限）。

    ``api_key`` 为空时直接报错（不静默退化成"没有向量"——那样检索会悄悄变差）。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        dim: int = EMBED_DIM,
        batch_size: int = 8,
        concurrency: int = 2,
        timeout: float = 30.0,
        max_chars: int = 8000,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not str(model or "").strip():
            raise EmbeddingError("embedding 必须给 model")
        if not str(api_key or "").strip():
            raise EmbeddingError("embedding 缺少 api_key")
        self.model = str(model)
        self.base_url = str(base_url or "").rstrip("/")
        self.dim = int(dim)
        self.batch_size = max(1, int(batch_size))
        self.concurrency = max(1, int(concurrency))
        self.timeout = float(timeout)
        self.max_chars = max(1, int(max_chars))
        self._headers = {"Authorization": f"Bearer {api_key}", **(headers or {})}
        self._client = client
        self._owns_client = client is None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量 embed：按 ``batch_size`` 切批、``concurrency`` 并发，结果按输入顺序拼回。"""
        items = [str(text or "") for text in texts]
        if not items:
            return []
        batches = [
            items[start : start + self.batch_size]
            for start in range(0, len(items), self.batch_size)
        ]
        gate = asyncio.Semaphore(self.concurrency)

        async def run(batch: list[str]) -> list[list[float]]:
            async with gate:
                return await self._embed_batch(batch)

        results = await asyncio.gather(*(run(batch) for batch in batches))
        vectors: list[list[float]] = [vector for chunk in results for vector in chunk]
        if len(vectors) != len(items):
            raise EmbeddingError(
                f"embedding 条数不匹配: 输入 {len(items)} 条，返回 {len(vectors)} 条"
            )
        return vectors

    async def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        # OpenAI 拒绝空串：用单个空格占位（这类 chunk 本来也不该存在）。
        payload = {
            "model": self.model,
            "input": [text[: self.max_chars] or " " for text in batch],
            "dimensions": self.dim,
            "encoding_format": "float",
        }
        client = self._get_client()
        try:
            response = await client.post("/embeddings", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding 请求失败: {exc}") from exc
        if response.status_code >= 400:
            detail = (response.text or "")[:300]
            raise EmbeddingError(f"embedding 返回 {response.status_code}: {detail}")
        try:
            body: Any = response.json()
            data = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"embedding 返回体不合法: {exc}") from exc

        ordered: list[tuple[int, list[float]]] = []
        for item in data:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                raise EmbeddingError("embedding 返回项缺少 embedding 数组")
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"embedding 维度不对: 期望 {self.dim}，返回 {len(vector)}"
                    f"（服务端可能不支持 dimensions 参数）"
                )
            index = int(item.get("index", len(ordered)))
            ordered.append((index, [float(value) for value in vector]))
        if len(ordered) != len(batch):
            raise EmbeddingError(
                f"embedding 条数不匹配: 本批 {len(batch)} 条，返回 {len(ordered)} 条"
            )
        ordered.sort(key=lambda pair: pair[0])
        indices = [index for index, _ in ordered]
        if indices != list(range(len(batch))):
            raise EmbeddingError(f"embedding 返回的 index 不连续: {indices}")
        return [vector for _, vector in ordered]

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


__all__ = ["EmbeddingError", "Embeddings", "OpenAICompatibleEmbeddings"]
