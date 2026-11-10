"""``MemoryRealm`` 的工具。

读 / 写 / 编辑走 VFS（``read|write|edit memories://...``）；这里补 VFS 给不了的四件事：

- ``memory_search``：跨目录关键词检索（VFS 的 ``search`` 只扫已展开的投影）；
- ``memory_list``：分页列一层（目录很大时 VFS 列表会截断在 ``list_limit``）；
- ``memory_delete``：删除文件/目录（核心工具集里没有 delete）；
- ``memory_tag`` / ``memory_score``：元数据维护（标签影响检索加权，权重直接影响排序）。

重命名 / 移动 / 复制 / 多条件检索是 store API（与既有实现一致，不作为模型工具）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from .realm import MemoryRealm

MAX_TOOLS = 8
"""工具数量上限：超了就把相近的合并成 action 参数（避免工具 schema 膨胀）。"""


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(exc: Exception) -> str:
    return _dump({"status": "error", "error": str(exc)})


def build_memory_tools(realm: "MemoryRealm") -> list[Any]:
    """构造记忆 realm 的全部工具（顺序稳定，便于测试与提示词对齐）。"""
    from langchain_core.tools import tool

    @tool("memory_search")
    async def memory_search(
        query: str,
        scope: str = "/",
        top_k: int = 5,
        tags: str = "",
        return_mode: str = "compact",
    ) -> str:
        """在记忆库（memories://）里检索：关键词 + 语义，按相关性融合排序。

        Args:
            query: 检索内容（自然语言即可；中文按二元组切分）。
            scope: 只搜这个记忆路径下的内容，默认整个库。
            top_k: 返回条数上限。
            tags: 必须同时具备的标签，逗号分隔（可空；是过滤不是加权）。
            return_mode: compact（路径+说明+分数）/ snippets（带片段）/
                paths（只给路径）/ full（带全文，慎用，很占上下文）。
        """
        tag_list = [part.strip() for part in str(tags or "").split(",") if part.strip()]
        mode = str(return_mode or "compact").strip().lower()
        if mode not in ("compact", "snippets", "paths", "full"):
            return _error(
                ValueError(f"return_mode 只支持 compact/snippets/paths/full: {return_mode!r}")
            )
        try:
            hits = await realm.asearch(
                query,
                scope=scope or "/",
                top_k=max(1, int(top_k)),
                tags=tag_list,
                return_mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 - 工具错误以文本回给模型，不炸回合
            return _error(exc)
        if mode == "paths":
            return _dump([hit["path"] for hit in hits])
        if mode == "full":
            return _dump(hits)
        compact = []
        for hit in hits:
            path = str(hit.get("path") or "")
            line_count = await asyncio.to_thread(_line_count_of, realm, path)
            item = {
                "path": path,
                "uri": hit.get("uri"),
                "line_count": line_count,
                "description": hit.get("description", ""),
                "tags": hit.get("tags", []),
                "score": hit.get("score", 0.0),
                "sources": hit.get("sources", []),
            }
            if mode == "snippets":
                item["snippet"] = hit.get("snippet", "")
            compact.append(item)
        return _dump(compact)

    @tool("memory_list")
    async def memory_list(path: str = "/", limit: int = 50, offset: int = 0) -> str:
        """分页列出记忆库某个目录（目录项在前，文件带行数/标签/说明）。

        Args:
            path: 目录路径（默认根）。
            limit: 本页条数（1-500）。
            offset: 跳过多少条。
        """
        page = max(1, min(500, int(limit)))
        try:
            nodes, total = await asyncio.to_thread(
                realm.list_dir, path or "/", limit=page, offset=max(0, int(offset))
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump(
            {
                "path": path or "/",
                "total": total,
                "offset": max(0, int(offset)),
                "returned": len(nodes),
                "items": [
                    {
                        "name": node.name,
                        "path": node.path,
                        "uri": "memories://" + node.path.lstrip("/"),
                        "kind": node.kind,
                        "description": node.description,
                        "updated_at": node.updated_at,
                        "tags": list(node.tags),
                        "line_count": await asyncio.to_thread(_line_count_of, realm, node.path)
                        if node.kind == "file"
                        else None,
                    }
                    for node in nodes
                ],
            }
        )

    @tool("memory_delete")
    async def memory_delete(path: str, recursive: bool = False) -> str:
        """删除记忆库中的文件或目录。

        Args:
            path: 记忆路径（``/notes/a.md`` 或 ``notes/a.md``）。
            recursive: 删除**目录**时必须显式置 True（防止误删整棵树）。
        """
        try:
            result = await asyncio.to_thread(realm.delete, path, recursive=bool(recursive))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump({"status": "ok", **result})

    @tool("memory_tag")
    async def memory_tag(path: str, tags: str) -> str:
        """整组替换某条记忆的标签（空字符串表示清空）。

        Args:
            path: 记忆路径。
            tags: 逗号分隔的标签，例如 ``人物,上海``。
        """
        tag_list = [part.strip() for part in str(tags or "").split(",") if part.strip()]
        try:
            result = await asyncio.to_thread(realm.set_tags, path, tag_list)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump({"status": "ok", **result})

    @tool("memory_score")
    async def memory_score(path: str, score_patch: float) -> str:
        """调整某条记忆的检索权重（-0.15 到 +0.15；正数更容易被检索到）。

        Args:
            path: 记忆路径。
            score_patch: 权重补丁，越界会被拒绝。
        """
        try:
            result = await asyncio.to_thread(realm.set_score, path, float(score_patch))
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump({"status": "ok", **result})

    @tool("memory_graph")
    async def memory_graph(action: str, target: str = "", depth: int = 1) -> str:
        """查记忆里的实体/关系图（实体是从记忆里抽出来的：人、地点、事件、概念）。

        Args:
            action: neighbors（target=实体 id，看邻居）/ detail（target=实体 id，
                看详情）/ search（target=名字片段，找实体 id）/ entities_of
                （target=记忆路径，看这条记忆提到了哪些实体）。
            target: 实体 id（``ent_...``）、搜索词或记忆路径，取决于 action。
            depth: neighbors 走几跳（1-3）。
        """
        kind = str(action or "").strip().lower()
        arg = str(target or "").strip()
        graph = realm.graph
        try:
            if kind == "neighbors":
                if not arg:
                    return _error(ValueError("neighbors 需要 target=实体 id"))
                payload = await asyncio.to_thread(
                    graph.neighbors, arg, max(1, min(3, int(depth)))
                )
            elif kind == "detail":
                if not arg:
                    return _error(ValueError("detail 需要 target=实体 id"))
                payload = await asyncio.to_thread(graph.detail, arg)
                if payload is None:
                    return _error(FileNotFoundError(f"实体不存在: {arg}"))
            elif kind == "search":
                entities = await asyncio.to_thread(graph.search, arg, top_k=20)
                payload = [entity.to_dict() for entity in entities]
            elif kind == "entities_of":
                if not arg:
                    return _error(ValueError("entities_of 需要 target=记忆路径"))
                entities = await asyncio.to_thread(graph.entities_of, arg)
                payload = [entity.to_dict() for entity in entities]
            else:
                return _error(
                    ValueError(
                        "action 只支持 neighbors/detail/search/entities_of: "
                        f"{action!r}"
                    )
                )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump({"action": kind, "target": arg, "result": payload})

    @tool("memory_attach")
    async def memory_attach(
        path: str,
        content_base64: str,
        content_type: str = "image/png",
        description: str = "",
    ) -> str:
        """把一张图片（或其它二进制）存进记忆库，描述会被检索到。

        Args:
            path: 记忆路径，例如 ``/images/cat.png``。
            content_base64: 文件内容的 base64（不要带 data: 前缀）。
            content_type: MIME 类型，默认 image/png。
            description: 一句话说明图里是什么（会被索引，搜图靠它）。
        """
        import base64
        import binascii

        try:
            raw = base64.b64decode(str(content_base64 or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            return _error(ValueError(f"content_base64 不是合法 base64: {exc}"))
        if not raw:
            return _error(ValueError("content_base64 为空"))
        try:
            result = await asyncio.to_thread(
                realm.write_attachment,
                path,
                raw,
                content_type=str(content_type or "image/png"),
                description=str(description or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return _dump({"status": "ok", **result})

    tools = [
        memory_search,
        memory_list,
        memory_delete,
        memory_tag,
        memory_score,
        memory_graph,
        memory_attach,
    ]
    assert len(tools) <= MAX_TOOLS, "记忆工具超过上限，先合并 action 再加"
    return tools


def _line_count_of(realm: "MemoryRealm", path: str) -> int | None:
    store = realm.store
    if store is None:
        return None
    try:
        return store.read(path).line_count
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


__all__ = ["MAX_TOOLS", "build_memory_tools"]
