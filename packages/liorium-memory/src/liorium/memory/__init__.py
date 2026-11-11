"""liorium.memory —— 记忆能力包（``[memory]`` extra）。

计划依据 §Phase 2 / §C6 / §3.7：

- 挂载点 ``/memories``（scheme ``memories://``）
- 能力四件套：工具 + VFS 挂载 + 状态 + ``prompt_fragment()``
- 状态存 SQLite store（``scope="agent"``），VFS 只出投影节点
- **与既有实现 ``memory.sqlite`` 同 schema**：能直接读旧库并继续读写，不写迁移脚本

用法：

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.memory import MemoryRealm

agent = LioriumAgent(
    spec=AgentSpec(..., realms=[RealmSpec(id="memory")]),
    realm_factories={"memory": MemoryRealm},
    ...
)
```

默认库在 ``<inbox_dir>/memory/memory.sqlite``（正文在 ``memory/content/``），
也可以用 ``RealmSpec.params["db_path"]`` / ``["content_dir"]`` 指到别处。
"""

from __future__ import annotations

__version__ = "0.1.1"

# 轻量面：只要关键词检索就不需要 numpy / httpx（它们在 [vector] extra 里，
# 由 ``liorium.memory.vectors`` / ``liorium.memory.embeddings`` 直接 import）。
from .graph import Entity, EntityGraph, Relation
from .realm import MOUNT_PREFIX, SCHEME, STORE_DIR_NAME, MemoryRealm
from .search import MemorySearch, SearchConfig, SearchResult
from .store import (
    MAX_SCORE_PATCH,
    MIN_SCORE_PATCH,
    MemoryEntry,
    MemoryNode,
    SearchHit,
    SqliteMemoryStore,
    chunk_text,
    normalize_memory_path,
    tokenize,
)

__all__ = [
    "MAX_SCORE_PATCH",
    "MIN_SCORE_PATCH",
    "MOUNT_PREFIX",
    "SCHEME",
    "STORE_DIR_NAME",
    "Entity",
    "EntityGraph",
    "MemoryEntry",
    "MemoryNode",
    "MemoryRealm",
    "MemorySearch",
    "Relation",
    "SearchConfig",
    "SearchHit",
    "SearchResult",
    "SqliteMemoryStore",
    "chunk_text",
    "normalize_memory_path",
    "tokenize",
    "__version__",
]
