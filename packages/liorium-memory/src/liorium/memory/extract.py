"""实体抽取流水线：把一条记忆的正文交给 LLM，产出实体/关系并入库。

三个刻意的设计：

1. **抽取服务是可注入的函数**（``chat(system, user) -> str``），库不认模型客户端、
   不读产品配置；宿主用自家 provider 调模型，返回 JSON 文本即可。
2. **必须用空白 contextvars 上下文跑**：抽取常在被工具调用触发的后台任务里，
   若继承 LangChain 的 ``var_child_runnable_config``，这次 LLM 调用会成为主 run 的
   子 run，流式 delta 会冒泡进主 Agent 的事件流（既有实现踩过）。调度由 realm 用
   ``ctx.spawn(..., context=contextvars.Context())`` 完成，见 ``realm.py``。
3. **实体去重靠名字向量**（``index/entity.vdb``）：抽到"爱丽丝"时如果库里已有
   语义等价的实体，就复用它的 id 并补一条 ``from`` 边，而不是又建一个。

单飞 + 并发上限：同一条记忆同时只跑一次抽取（``_running``），整体并发
``Semaphore(2)``——抽取是后台活，不能把主对话的 API 配额吃光。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence, runtime_checkable

from liorium.core.log import get_logger

from .graph import DEFAULT_DEDUP_THRESHOLD, EntityGraph
from .store import SqliteMemoryStore, normalize_memory_path, path_id

if TYPE_CHECKING:  # pragma: no cover - 只用于类型
    from .embeddings import Embeddings

log = get_logger("liorium.memory.extract")

EXTRACTION_PROMPT = """# Task: 实体与关系抽取

你的任务: 严格遵循如下指示,从给定文本中抽取实体和关系。

你抽取的实体有如下要求:

1. 准确，不得偏离文本

2. 精炼，应该是文本中的核心内容，如果只是简单在文本中提到，不应该输出为一个实体

## 实体类型
- person: 人物
- place: 地点
- event: 事件
- concept: 抽象概念/主题/知识
- object: 具体物体/工具
- document: 文档/文件

## 关系类型
- relates_to: 通用关联
- part_of: A是B的一部分
- located_at: A位于B
- created_by: A由B创建
- mentions: A提到了B
- references: A引用了B

## 实体命名规则

- 具体,避免歧义
  比如 文档中提到 小明有一只猫，那么应该把实体命名为 小明的猫 而不是 猫

## 输出格式

必须严格按照JSON格式输出
```json
{"entities": [{"name": "...", "type": "...", "description": "...", "properties": {}}], "relations": [{"source": "...", "target": "...", "type": "..."}]}
```
- description 字段为实体的一到两句自然语言描述，概括其核心属性或与上下文的关系。必须为中文。

## 实体数量

控制在每100字3~4个实体
无论文本长度为多少,最多输出15个实体
"""

MAX_ENTITIES = 15
"""一次抽取最多接受多少个实体（与提示词里的上限一致）。"""

DEFAULT_MAX_CHARS = 6000
"""送进模型的正文上限；超了截断（抽取是后台增益，不值得为大文档多花几十倍 token）。"""


class ExtractionError(RuntimeError):
    """抽取结果不可用（模型没返回 JSON / 结构不对）。"""


@dataclass(frozen=True)
class ExtractedEntity:
    name: str
    entity_type: str = "custom"
    description: str = ""
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedRelation:
    source: str
    target: str
    relation_type: str = "relates_to"


@dataclass(frozen=True)
class ExtractionResult:
    entities: tuple[ExtractedEntity, ...] = ()
    relations: tuple[ExtractedRelation, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entities or self.relations)


@runtime_checkable
class EntityExtractor(Protocol):
    """抽取器协议：给正文，返回结构化结果。"""

    async def extract(self, text: str) -> ExtractionResult: ...


ChatCallable = Callable[[str, str], Awaitable[str]]
"""``(system_prompt, user_text) -> 模型返回的文本``。"""


class LlmExtractor:
    """用宿主提供的 ``chat`` 函数跑抽取，并把 JSON 文本解析成结构化结果。"""

    def __init__(
        self,
        chat: ChatCallable,
        *,
        prompt: str = EXTRACTION_PROMPT,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.chat = chat
        self.prompt = prompt
        self.max_chars = max(200, int(max_chars))

    async def extract(self, text: str) -> ExtractionResult:
        body = str(text or "").strip()
        if not body:
            return ExtractionResult()
        raw = await self.chat(self.prompt, body[: self.max_chars])
        return parse_extraction_json(raw)


def parse_extraction_json(raw: str) -> ExtractionResult:
    """宽容地解析模型输出：容忍 ```json 围栏、前后废话、单个对象。"""
    text = str(raw or "").strip()
    if not text:
        return ExtractionResult()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ExtractionError(f"抽取结果里没有 JSON 对象: {text[:120]!r}")
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ExtractionError(f"抽取结果不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("抽取结果必须是 JSON 对象")

    entities: list[ExtractedEntity] = []
    seen: set[str] = set()
    for item in payload.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entities.append(
            ExtractedEntity(
                name=name,
                entity_type=str(item.get("type") or "custom"),
                description=str(item.get("description") or ""),
                properties=dict(item.get("properties") or {})
                if isinstance(item.get("properties"), dict)
                else {},
            )
        )
        if len(entities) >= MAX_ENTITIES:
            break

    relations: list[ExtractedRelation] = []
    known = {entity.name for entity in entities}
    for item in payload.get("relations") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        if not source or not target or source == target:
            continue
        if source not in known or target not in known:
            # 同一批里没抽到的名字不建边（既有实现也是 skip + warning）
            continue
        relations.append(
            ExtractedRelation(
                source=source,
                target=target,
                relation_type=str(item.get("type") or "relates_to"),
            )
        )
    return ExtractionResult(tuple(entities), tuple(relations))


class MemoryExtractor:
    """一条记忆的抽取流水线：读正文 → 抽取 → 实体去重入库 → 关系入库。"""

    def __init__(
        self,
        store: SqliteMemoryStore,
        graph: EntityGraph,
        *,
        extractor: EntityExtractor,
        embedder: "Embeddings | None" = None,
        concurrency: int = 2,
        dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.store = store
        self.graph = graph
        self.extractor = extractor
        self.embedder = embedder
        self.dedup_threshold = float(dedup_threshold)
        self.max_chars = max(200, int(max_chars))
        self._gate = asyncio.Semaphore(max(1, int(concurrency)))
        self._running: set[str] = set()
        self._status: dict[str, Any] = {
            "pending": 0,
            "running": 0,
            "last_path": None,
            "last_success": None,
            "last_error": None,
        }

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run_document(self, path: str) -> dict[str, Any]:
        """抽取一条记忆；``path`` 正在抽就跳过（单飞）。"""
        norm = normalize_memory_path(path)
        if norm in self._running:
            return {"status": "skipped", "path": norm, "reason": "already running"}
        self._running.add(norm)
        self._status["pending"] = max(0, int(self._status["pending"]) - 1)
        self._status["running"] = int(self._status["running"]) + 1
        started = time.monotonic()
        try:
            async with self._gate:
                return await self._run(norm, started)
        finally:
            self._running.discard(norm)
            self._status["running"] = max(0, int(self._status["running"]) - 1)

    def notify_pending(self, path: str) -> None:
        """记一笔"这条记忆等着抽"（调度前调，便于观察积压）。"""
        self._status["pending"] = int(self._status["pending"]) + 1
        self._status["last_path"] = normalize_memory_path(path)

    async def _run(self, norm: str, started: float) -> dict[str, Any]:
        try:
            entry = self.store.read(norm)
        except (FileNotFoundError, IsADirectoryError, OSError) as exc:
            return self._fail(norm, f"读不到记忆正文: {exc}")
        text = (entry.text or "").strip()
        if not text:
            return self._ok(norm, 0, 0, started)
        try:
            result = await self.extractor.extract(text[: self.max_chars])
        except Exception as exc:  # noqa: BLE001 - 后台任务：失败要记状态，不能炸掉
            return self._fail(norm, f"抽取失败: {type(exc).__name__}: {exc}")
        if not result:
            return self._ok(norm, 0, 0, started)

        try:
            ids = await self._upsert_entities(norm, result.entities)
        except Exception as exc:  # noqa: BLE001
            return self._fail(norm, f"实体入库失败: {type(exc).__name__}: {exc}")

        relations = 0
        for relation in result.relations:
            source = ids.get(relation.source)
            target = ids.get(relation.target)
            if not source or not target or source == target:
                continue
            try:
                self.graph.add_relation(source, target, relation.relation_type)
                relations += 1
            except (FileNotFoundError, ValueError) as exc:
                log.warning("关系入库跳过 %s -> %s: %s", relation.source, relation.target, exc)
        return self._ok(norm, len(ids), relations, started)

    async def _upsert_entities(
        self, norm: str, entities: Sequence[ExtractedEntity]
    ) -> dict[str, str]:
        """实体去重入库，并把"这条记忆提到这个实体"记成 ``from`` 边。"""
        names = [entity.name for entity in entities]
        vectors: list[list[float]] | None = None
        if self.embedder is not None and names:
            try:
                vectors = await self.embedder.embed(names)
            except Exception as exc:  # noqa: BLE001 - 没有向量就退化精确匹配
                log.warning("实体名向量失败，退化为名字精确匹配: %s", exc)
                vectors = None
        similar = await self.graph.find_similar(
            names, vectors, threshold=self.dedup_threshold
        )
        doc_id = path_id(norm)
        mapping: dict[str, str] = {}
        for entity, existing in zip(entities, similar):
            if existing:
                eid = existing
            else:
                created = self.graph.add(
                    entity.name,
                    entity.entity_type,
                    description=entity.description,
                    properties=entity.properties,
                )
                eid = created.id
                if vectors is not None:
                    await self.graph.store_name_vector(
                        eid, vectors[names.index(entity.name)]
                    )
            mapping[entity.name] = eid
            self.graph.add_relation(doc_id, eid, "from")
        return mapping

    # ------------------------------------------------------------------
    # 状态维护
    # ------------------------------------------------------------------

    def _ok(self, norm: str, entities: int, relations: int, started: float) -> dict[str, Any]:
        elapsed = time.monotonic() - started
        self._status["last_success"] = {
            "path": norm,
            "entities": entities,
            "relations": relations,
            "elapsed": round(elapsed, 3),
        }
        self._status["last_error"] = None
        log.info(
            "记忆抽取完成 path=%s entities=%d relations=%d %.2fs",
            norm,
            entities,
            relations,
            elapsed,
        )
        return {"status": "ok", "path": norm, "entities": entities, "relations": relations}

    def _fail(self, norm: str, message: str) -> dict[str, Any]:
        self._status["last_error"] = {"path": norm, "error": message}
        log.error("记忆抽取失败 path=%s: %s", norm, message)
        return {"status": "error", "path": norm, "error": message}


__all__ = [
    "DEFAULT_MAX_CHARS",
    "EXTRACTION_PROMPT",
    "MAX_ENTITIES",
    "EntityExtractor",
    "ExtractedEntity",
    "ExtractedRelation",
    "ExtractionError",
    "ExtractionResult",
    "LlmExtractor",
    "MemoryExtractor",
    "parse_extraction_json",
]
