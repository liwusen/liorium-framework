"""OpenAI 兼容 embedding 客户端：批量、并发、顺序、维度校验。

用 ``httpx.MockTransport`` 跑，不碰网络。
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from liorium.memory.embeddings import EmbeddingError, OpenAICompatibleEmbeddings
from liorium.memory.vectors import EMBED_DIM


def make_client(handler, *, base_url: str = "https://stub.invalid/v1") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


def vector_for(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性假向量：文本长度决定第一个分量，其余为 0。"""
    values = [0.0] * dim
    values[0] = float(len(text) or 1)
    return values


@pytest.mark.asyncio
async def test_batches_preserve_order() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        data = [
            {"index": index, "embedding": vector_for(text)}
            for index, text in enumerate(payload["input"])
        ]
        # 故意打乱顺序：客户端必须按 index 排回来
        data.reverse()
        return httpx.Response(200, json={"data": data})

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed",
        api_key="sk-test",
        batch_size=2,
        concurrency=3,
        client=make_client(handler),
    )
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    vectors = await embedder.embed(texts)
    await embedder.aclose()

    assert len(vectors) == 5
    assert [round(vector[0]) for vector in vectors] == [1, 2, 3, 4, 5]
    assert len(calls) == 3  # 5 条按 2 一批
    assert [len(call["input"]) for call in calls] == [2, 2, 1]
    assert calls[0]["model"] == "stub-embed"
    assert calls[0]["dimensions"] == EMBED_DIM
    assert calls[0]["encoding_format"] == "float"


@pytest.mark.asyncio
async def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("空输入不该发请求")

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed", api_key="sk-test", client=make_client(handler)
    )
    assert await embedder.embed([]) == []
    await embedder.aclose()


@pytest.mark.asyncio
async def test_wrong_dimension_is_loud() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed", api_key="sk-test", client=make_client(handler)
    )
    with pytest.raises(EmbeddingError, match="维度不对"):
        await embedder.embed(["x"])
    await embedder.aclose()


@pytest.mark.asyncio
async def test_http_error_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":"bad model"}')

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed", api_key="sk-test", client=make_client(handler)
    )
    with pytest.raises(EmbeddingError, match="400"):
        await embedder.embed(["x"])
    await embedder.aclose()


@pytest.mark.asyncio
async def test_malformed_body_is_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed", api_key="sk-test", client=make_client(handler)
    )
    with pytest.raises(EmbeddingError, match="不合法"):
        await embedder.embed(["x"])
    await embedder.aclose()


def test_constructor_requires_model_and_key() -> None:
    with pytest.raises(EmbeddingError, match="model"):
        OpenAICompatibleEmbeddings(model="", api_key="sk-test")
    with pytest.raises(EmbeddingError, match="api_key"):
        OpenAICompatibleEmbeddings(model="m", api_key="")


@pytest.mark.asyncio
async def test_vectors_are_finite() -> None:
    """返回的每个分量都必须是有限数（NaN/inf 会把 cosine 打分毁掉）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        values = [1.0] * EMBED_DIM
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": values}]})

    embedder = OpenAICompatibleEmbeddings(
        model="stub-embed", api_key="sk-test", client=make_client(handler)
    )
    vectors = await embedder.embed(["x"])
    assert all(math.isfinite(value) for value in vectors[0])
    await embedder.aclose()
