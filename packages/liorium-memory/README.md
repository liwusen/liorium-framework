# liorium-memory

Liorium 记忆能力包。挂载点 `/memories` ⇔ `memories://`。

与 v1 `GraphStore` 的对照已**全部落地**（42 个公开方法逐个对应，18 条偏差逐条登记）：
实现说明、映射表、内存预算见 [`docs/MEMORY.md`](../../docs/MEMORY.md)。

## 组成

| 模块 | 职责 |
|---|---|
| `store.py` | `SqliteMemoryStore`：**与旧 `memory.sqlite` 同 schema v1**（nodes/edges/tags/chunks/tasks + `content/<path>`），可直接读旧库继续读写 |
| `fts.py` | `FtsIndex`：side-car FTS5 关键词索引（预分词）+ 向量任务队列 |
| `vectors.py` | `NanoVectorBackend`：`index/chunks.vdb`，与既有实现**同文件格式**（dim 1536 / 同 metadata 键 / cosine） |
| `embeddings.py` | `OpenAICompatibleEmbeddings`：OpenAI 兼容 `/embeddings`（批量 + 并发 + 维度校验） |
| `search.py` | `MemorySearch`：关键词 + 向量 + 图谱 **RRF 融合**，四种返回形态 |
| `indexer.py` | `MemoryIndexer`：后台把队列里的 chunk embed 进向量库（重试/丢弃/对账） |
| `graph.py` | `EntityGraph`：实体/关系 CRUD、merge、邻居（**递归 CTE**，不引 networkx） |
| `extract.py` | `MemoryExtractor` / `LlmExtractor`：抽实体与关系（单飞 + 空白 contextvars） |
| `migrate.py` | 旧 JSON 库（`graph.json` + `meta/`）→ SQLite，单事务全有或全无 |
| `realm.py` | `MemoryRealm`：懒加载投影 + 混合检索 + 维护工具 + 记录/日记 API |
| `tools.py` | 7 个模型工具：`memory_search` / `memory_list` / `memory_delete` / `memory_tag` / `memory_score` / `memory_graph` / `memory_attach` |

能力四件套：**工具 + VFS 挂载 + 状态（`state_scope="agent"`）+ `prompt_fragment()`**。
状态文件在 `<inbox>/realm_state/memory.json`；库默认在 `<inbox>/memory/memory.sqlite`
（`RealmSpec.params` 的 `db_path` / `content_dir` 可覆盖）。

## 用法

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.memory import MemoryRealm

agent = LioriumAgent(
    spec=AgentSpec(
        id="main",
        inbox_dir=Path("agents/main"),
        model="deepseek::chat",
        prompt_sources=("AGENT.md",),
        realms=[RealmSpec(id="memory")],
    ),
    model_factory=make_chat_model,
    realm_factories={"memory": MemoryRealm},
)
```

要语义检索 / 实体抽取，就在工厂里注入服务（**服务对象不进 `params`**，params 要参与指纹）：

```python
from liorium.memory import MemoryRealm
from liorium.memory.embeddings import OpenAICompatibleEmbeddings
from liorium.memory.extract import LlmExtractor
from liorium.memory.vectors import NanoVectorBackend

def memory_realm(spec):
    root = Path("agents/main/memory")
    return MemoryRealm(
        spec,
        embedder=OpenAICompatibleEmbeddings(model="text-embedding-3-small", api_key=...),
        vectors=NanoVectorBackend(root / "index/chunks.vdb"),
        entity_vectors=NanoVectorBackend(root / "index/entity.vdb"),
        extractor=LlmExtractor(chat=my_chat),   # async (system, user) -> str
    )
```

模型侧的行为：

- `read memories://` → 列一层目录（文件带 `MM/DD, N lines, #tag` 元数据）
- `read memories://人物/爱丽丝` / `write` / `edit` → 读、写、局部替换（走核心工具）
- `memory_search(query="…")` → 混合检索（`compact` / `snippets` / `paths` / `full`）
- `memory_graph(action="search"|"neighbors"|"detail"|"entities_of")` → 实体与关系
- `memory_attach` → 存图片附件（描述会被索引）

## 检索

- **关键词**：FTS5 倒排在 `index/fts.sqlite`（中文二元组 + 单字，2 字/1 字查询都能命中），
  常驻只有页缓存（默认 20MB）；
- **语义**：`index/chunks.vdb`（nano-vectordb 格式，与既有实现逐字节兼容），全量矩阵在内存是它的固有代价
  → 惰性加载 + 延迟落盘 + `unload()` + `params["vectors"]=False` 只跑关键词；
- **融合**：RRF（只看位次），再叠 `score_patch`、新鲜度、标签；**没有 rerank**。

## 已知边界

- 旧库若 `schema_version` 不是 1，打开时直接报错（不静默迁移）。
- 图片附件只能通过 `write_attachment` / `memory_attach` 写入（正文写接口只收文本）。
- 实体不进分块索引：按名字找实体用 `memory_graph(action="search")`（原因见 MEMORY §6 D9）。
- 外部进程改了库 ⇒ `dirty()` 在**下一回合边界**触发对账；投影是懒加载的，不重建树。
