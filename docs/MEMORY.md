# 记忆子系统（`liorium-memory`）

> 这一篇是记忆能力包的**实现说明**：数据布局、42 个旧接口的映射、检索流水线、内存预算、
> 以及和既有实现（`GraphStore`）的**全部偏差**。
> 快速上手看 [GUIDE](GUIDE.md)，逐字签名看 [API](API.md) §5。

## 1. 它解决什么

一条事实要能被"以后想得起来"，需要四件事同时成立：

1. **存得住**：文件式记忆（标题即路径，正文写事实），人肉可读、可 diff、可备份；
2. **找得到**：关键词（精确词）与语义（换个说法）两条路都要通，还得按标签/时间/权重筛；
3. **连得上**：记忆里提到的人/地点/事件要成为实体，实体之间有关系——这样"上次说的那个人"能跳过来；
4. **不炸内存**：10 万条记忆不能变成 10 万份常驻对象。

## 2. 磁盘布局

```
<inbox>/memory/
├── memory.sqlite          # 主库：nodes / edges / tags / chunks / tasks（schema v1，与旧库逐字同构）
├── content/               # 正文，路径即标题：content/notes/cat.md
│   └── entities/          # 实体正文（id 是 ent_*，不进目录树）
└── index/                 # 派生索引，删了能重建
    ├── fts.sqlite         # 关键词倒排（FTS5）+ 向量任务队列
    ├── chunks.vdb         # 分块向量（nano-vectordb 格式，与旧库兼容）
    └── entity.vdb         # 实体名向量（去重用）
```

**主库与旧库同 schema、同 `SCHEMA_VERSION=1`**：把 Liorium 的 `memory.sqlite` 丢回既有实现能直接读写，
反过来也是。`index/` 是 Liorium 的私货，既有实现不认识它，删掉也不丢数据。

## 3. 一次写入发生了什么

```
write memories://notes/cat.md
   │
   ├─ store.write()                     主库事务：nodes + content 文件 + chunks（3000/300 切块）
   │      └─ 提交后 _index_change()
   │            ├─ fts.apply()          index/fts.sqlite：删旧 chunk → 插新 chunk（预分词）+ 入队向量任务
   │            └─ （可选）vector_jobs  与 FTS 同一事务，避免"关键词搜得到、向量永远缺一条"
   │
   ├─ indexer.notify()                  后台：把队列里的 chunk embed 进 chunks.vdb（延迟落盘）
   └─ extractor.run_document()          后台：LLM 抽实体/关系 → 去重入库 → 文档-实体 from 边
```

两个后台动作都**不阻塞**写操作，都由 `ctx.spawn` 托管、realm `stop()` 时收尾：

| 后台活 | 失败后果 | 自愈手段 |
|---|---|---|
| 向量化 | 该 chunk 暂时搜不到语义 | 重试；连续失败 5 次丢弃 + ERROR 日志；`reconcile()` 补投 |
| 实体抽取 | 这条记忆没有实体（关键词/向量仍可搜） | 改记忆会重跑；`memories://` 重写一次即可 |

## 4. 检索流水线

```
query ──┬─ 关键词路：FTS5 BM25（index/fts.sqlite，中文二元组+单字，单字查询可用）
        ├─ 语义路：   embed(query) → chunks.vdb cosine（按 scope 过滤）
        └─ 图谱路：   取融合前 5 条 → 文档→实体→别的文档（2 跳）
                     ↓
              RRF 融合 Σ w·k/(k+rank)        ← 只看位次，不混量纲
                     ↓
        + score_patch(±0.15) + 新鲜度(exp(-age/30d)) + 标签命中
                     ↓
        标签(AND) 过滤 → 排序 → top_k → 四种返回形态(paths/compact/snippets/full)
```

为什么是 RRF 而不是既有实现的"归一化后加权平均"：cosine 挤在 0.6~0.9、BM25 是长尾，各自除以
本路最大值等于把"第一名恒等于 1"，位次信息被抹平；RRF 只用名次，不用调参就稳。

**没有 rerank**（按需求删除）：融合完就是终值。

## 5. 42 个旧接口的映射

旧 `GraphStore` 的公开方法逐个对应到 Liorium（`store` = `SqliteMemoryStore`，`graph` = `EntityGraph`，
`realm` = `MemoryRealm`）。**API** = 只给宿主调用，不当模型工具。

| # | 旧接口 | Liorium | 位置 | 备注 |
|---|---|---|---|---|
| 1 | `refresh(agent_name)` | — | — | 既有实现靠换 agent 重建；Liorium 用 `revision()` 对账，没有"切 agent"概念 |
| 2 | `close` | `store.close()` | store | 关连接 + 关 side-car |
| 3 | `tree_list(scope)` | `store.children()` / `realm.list_dir()` | store/realm | 分页（`limit`/`offset`），不再一次返回整层 |
| 4 | `get_entity_children(path)` | `graph.entities_of(path)` | graph | `from` 边 + `kb_refs` 反查 |
| 5 | `file_read` | `store.read()` | store | 文本/图片/其它二进制三选一 |
| 6 | `file_write` | `store.write()` | store | 覆盖写 + 重建分块 |
| 7 | `attachment_write` | `store.write_attachment()` / `realm.write_attachment()` / 工具 `memory_attach` | store/realm | 描述生成可检索 chunk |
| 8 | `attachment_read` | `store.read()` 的 `MemoryEntry.data` | store | VFS `read` 直接出多模态内容 |
| 9 | `file_delete` | `store.delete()` | store | 目录递归删 |
| 10 | `file_delete_tree` | `store.delete()` | store | 与 9 合并（目录本来就递归） |
| 11 | `file_rename` | `store.rename()` | store | 子树重写 + 正文搬移 + chunk 重算 |
| 12 | `file_copy` | `store.copy()` | store | 同上 |
| 13 | `file_move` | `store.move()` | store | 同上 |
| 14 | `mkdir` | `store.mkdir()` | store | 幂等 |
| 15 | `register_extraction` | `MemoryExtractor.notify_pending()` | extract | 只记积压计数 |
| 16 | `complete_extraction` | `MemoryExtractor._ok()` / `_fail()` | extract | 状态机内部 |
| 17 | `get_extraction_status` | `MemoryExtractor.status()` | extract | `pending/running/last_success/last_error` |
| 18 | `set_tags` | `store.set_tags()` | store | 不刷 `updated_at` |
| 19 | `set_score_patch` | `store.set_score_patch()` | store | ±0.15，越界报错 |
| 20 | `advanced_search` | `store.advanced_search()` | store | 只返回文件（见 D6） |
| 21 | `get_entity_detail` | `graph.detail()` | graph | 实体/路径节点都能查 |
| 22 | `chunk_list(lst, size)` | — | — | 旧代码里的批处理小工具，Liorium 在 `embeddings.py` 内联 |
| 23 | `entity_add` | `graph.add()` | graph | 可选 `eid`（迁移/测试） |
| 24 | `entity_delete` | `graph.delete()` | graph | 级联删边 + 删正文 |
| 25 | `entity_merge` | `graph.merge()` | graph | 改指边、去自环/重边、补缺失字段 |
| 26 | `entity_search` | `graph.search()` | graph | 名字子串，前缀优先 |
| 27 | `entity_iter` | `graph.iter_entities()` | graph | |
| 28 | `relation_add` | `graph.add_relation()` | graph | 端点不存在直接 `FileNotFoundError` |
| 29 | `relation_remove` | `graph.remove_relation()` | graph | 返回删除条数 |
| 30 | `relation_iter` | `graph.iter_relations()` | graph | 不含 `has_child` |
| 31 | `get_neighbors` | `graph.neighbors()` | graph | **递归 CTE**，带 `depth` |
| 32 | `entity_find_similar` | `graph.find_similar()` | graph | 名字向量去重；无向量后端退精确匹配 |
| 33 | `search(...)` | `MemorySearch.search()` | search | 四路融合；`mode=auto/hybrid/keyword/vector` |
| 34 | `search_compact` | `MemorySearch.search(return_mode="compact")` | search | 邻居合并由图谱路统一做 |
| 35 | `search_bm25(query_tokens)` | `FtsIndex.query()` | fts | 接受原文（内部预分词）；`store.search()` 保留纯 LIKE 打分 |
| 36 | `get_changed_nodes` | `store.changed_nodes()` | store | **API**（维护 Agent 用） |
| 37 | `get_tasks` | `store.get_tasks()` | store | **API** |
| 38 | `add_task` | `store.add_task()` | store | **API** |
| 39 | `update_task` | `store.update_task()` | store | **API** |
| 40 | `add_chat_record` | `realm.add_chat_record()` | realm | **API**；记录实体 + `from`/`next` 链 |
| 41 | `write_diary` | `realm.write_diary()` | realm | **API** |
| 42 | `declare_file_update` | `realm.declare_file_update()` | realm | **API**（读本地文件不该由模型发起） |

## 6. 偏差登记（与既有实现的每一处不同）

| # | 偏差 | 为什么 |
|---|---|---|
| D1 | **不迁 rerank** | 需求明确删除；融合完即终值 |
| D2 | **不迁 MemoryInjector 插件** | 需求明确删除；记忆注入由宿主/提示词层决定 |
| D3 | **不建 networkx 投影**，邻居/2 跳走递归 CTE | 既有实现在内存里放整张图（≈1KB/节点），10 万节点就是 100MB；SQL 走索引，常驻≈0 |
| D4 | BM25 从 `jieba + rank_bm25` 改为 **FTS5 预分词**（中文二元组+单字） | 既有实现把全量 token 列表放内存；FTS5 倒排落盘，页缓存 20MB 封顶。代价：切词粒度不同（召回见 §9），换来 2 字/1 字查询都能命中（FTS5 自带 trigram 要求 ≥3 字） |
| D5 | 融合从"归一化加权平均"改为 **RRF** | 见 §4：两路量纲不同，归一化会抹平位次 |
| D6 | `advanced_search` **只返回文件** | 既有实现把目录节点也塞进结果，检索语义上没有意义；目录用 `children`/`memory_list` |
| D7 | `memory_search` 的 `tags` 是 **AND 过滤**（与 `advanced_search` 一致） | 旧的 `_hybrid_search` 也是 `required_tags ⊆ tags`；`store.search()` 保留旧的"有交集即命中"语义并在 docstring 里写明 |
| D8 | 向量**格式不变**，落盘策略变：惰性加载 + 延迟落盘 + `unload()` + `params["vectors"]=False` | 全量矩阵常驻是 nano 的固有代价；把"何时加载/何时写盘"变成可控。`vectors=False` ≈ 旧 `BM25_ONLY` |
| D9 | 实体不进分块索引，实体名靠 `memory_graph(action="search")` | 实体节点 `path IS NULL`（与旧库一致），没有路径就无法作为检索结果返回；既有实现把实体塞进 BM25 后又在 `_hybrid_search` 里过滤掉，实际也是走图谱 |
| D10 | 实体类型**不做白名单收敛** | 旧库里有 `chat_record` / `diary` 这类自定义类型，收敛会把记录链断掉 |
| D11 | 记录→实体用 `from` 边（既有实现用 `has_child`） | 本项目不变量：`has_child` 只表示 path↔path；`next` 链保留 |
| D12 | `set_tags` / `set_score_patch` **不刷** `updated_at` | 否则维护 Agent 自己的写操作会淹没增量变更流 |
| D13 | 旧 JSON 迁移：缺分块的文件**从正文重切**；实体名向量只在装了 `[vector]` 时迁移 | 分块是派生数据，重切无损；省一个硬依赖 |
| D14 | 不迁 FastAPI 路由 / 前端 | 库不带服务端 |
| D15 | `refresh(agent_name)` 取消 | Liorium 的 Agent 名不参与记忆路径 |
| D16 | `neighbors` 多返回 `depth`；`relations` 只给与中心**直接相连**的边 | 更远的跳说"相对中心的方向"是假的；给深度更诚实 |
| D17 | 附件写入走 base64（工具）/ `write_attachment`（API），**模型不能读本地任意文件** | 既有实现的 `attachment_write(file_path=...)` 让模型能读磁盘任意文件；Liorium 里读本地文件是宿主 API（`declare_file_update`） |
| D18 | `search_compact` 的 rerank 与"邻居直接塞结果"改为 **图谱路 RRF** | 邻居不该和马厩里的精排结果同权；现在邻居也要过融合与过滤 |

## 7. 并发与生命周期

| 资源 | 归属 | 说明 |
|---|---|---|
| `memory.sqlite` 连接 | `SqliteMemoryStore`（单连接 + `RLock`） | 与旧 `MemoryDB` 同策略；事务可重入；WAL + `busy_timeout=5s` |
| `fts.sqlite` 连接 | `FtsIndex`（单连接 + `RLock`） | 与主库**不同文件**，所以不在同一事务；行数漂移靠 `is_stale()` 发现并重建 |
| 向量矩阵 | `NanoVectorBackend`（`threading.RLock`，nano 操作走 `to_thread`） | 只想关键词检索时可以不开 |
| 索引器 | `MemoryIndexer`，`ctx.spawn` 托管 | 轮询 + `notify()` 唤醒；`stop()` 先 `aclose()`（等收尾 + flush）再由 manager 取消 |
| 抽取器 | `MemoryExtractor`，`ctx.spawn(context=Context())` | 单飞（同一条记忆同时只跑一次）+ `Semaphore(2)` |

**上下文隔离**（踩过的坑）：抽取任务必须用**空白 `contextvars.Context()`** 启动。否则它继承
LangChain 的 `var_child_runnable_config`，那次 LLM 调用会成为主 run 的子 run，
`on_chat_model_stream` 会冒泡进主 Agent 的 astream_events，被当成主回复的 delta 发给前端。

## 8. `RealmSpec.params`

| 键 | 默认 | 语义 |
|---|---|---|
| `db_path` | `<inbox>/memory/memory.sqlite` | 主库路径（相对路径相对 `inbox_dir`） |
| `content_dir` | `<inbox>/memory/content` | 正文目录 |
| `list_limit` | `200` | 一层最多投影多少子节点（超出用 `memory_list` 分页） |
| `keyword_index` | `True` | 是否建 `index/fts.sqlite`（关掉则检索退回 LIKE 打分） |
| `vectors` | `True` | 是否启用向量检索/索引器（还要宿主注入 `embedder`+`vectors`，否则自动关闭） |
| `extraction` | `True` | 是否在写记忆后自动抽实体（还要注入 `extractor`） |

服务（embedding / 向量后端 / 抽取器）**不走 params**：params 要能进指纹，而服务对象不可序列化。
用 realm 工厂闭包注入：

```python
def memory_realm(spec):
    return MemoryRealm(
        spec,
        embedder=OpenAICompatibleEmbeddings(model="text-embedding-3-small",
                                            api_key=..., base_url=...),
        vectors=NanoVectorBackend(inbox / "memory/index/chunks.vdb"),
        entity_vectors=NanoVectorBackend(inbox / "memory/index/entity.vdb"),
        extractor=LlmExtractor(chat=my_chat),
    )
```

## 8.1 配置速查

**零配置**：不注入任何服务就是「关键词检索 + 目录树」，不需要 API key，装 `liorium-memory`
本体即可（`liorium-agent[memory]`，不带 `[vector]`）。

要语义检索/实体抽取，两种装法：

### A0. `.env` 文件（推荐）

所有 CLI 配置都能写进 `.env`，不用每次带一长串参数：

```powershell
Copy-Item .env.example .env    # 模板（已入库，无密钥）
notepad .env                   # 填 provider / 模型 / 记忆目录 / embedding
uv run liorium-cli
```

- **查找顺序**：`$env:LIORIUM_ENV_FILE` → `./.env` → `./.env.local`；
- **优先级**：命令行 > 进程环境变量 > `.env` > 内置默认（`.env` 永远不覆盖已存在的环境变量）；
- `--env-file <path>` 指定文件，`--no-env` 完全不读；`$env:LIORIUM_ENV_FILE=""` 也能关掉自动加载；
- 键名 = `LIORIUM_` + 选项名大写（`--no-color` → `LIORIUM_NO_COLOR=1`，`--memory-dir` → `LIORIUM_MEMORY_DIR=...`）；
- 支持 `# 注释`、`export KEY=...`、单/双引号、`${VAR}` 引用前面定义的键；
- 没有 provider 配置文件时，`.env` 里的 `LIORIUM_BASE_URL` + `LIORIUM_CHAT_KEY` + `LIORIUM_MODEL`
  可以独立拼出一个 OpenAI 兼容 provider（键名见 `.env.example`）。

### A. CLI（`liorium-cli` / `liorium.cli`）

| 开关 / 环境变量 | 作用 |
|---|---|
| `--embed-model` / `LIORIUM_EMBED_MODEL` | **总开关**：给了才建向量索引、才自动抽实体（例：`text-embedding-3-small`、`text-embedding-v4`） |
| `--embed-base-url` / `LIORIUM_EMBED_BASE_URL` | embedding 的 OpenAI 兼容 base_url；空 = 跟随当前 chat provider |
| `--embed-key` / `LIORIUM_EMBED_KEY` | embedding 的 key；空 = 跟随当前 chat provider 的 key |
| `--no-vectors` | 不建向量索引（只跑关键词），同时不抽实体 |
| `--no-extraction` | 只向量化、不抽实体（省每写一次记忆一次模型调用） |
| `--memory-dir` | 记忆库目录，默认 `<inbox>/memory`；指到已有的 `agents/<name>/memory` 就是直接接管旧库 |

```powershell
# 只开语义检索（复用 chat provider 的 key 与 base_url）
uv run liorium-cli --embed-model text-embedding-3-small

# 换个 embedding 端点（比如本地/代理）
uv run liorium-cli `
    --embed-model bge-m3 --embed-base-url http://127.0.0.1:8080/v1 --embed-key local

# 接管旧库 + 关掉抽取
uv run liorium-cli `
    --memory-dir "D:\path\to\agent\memory" --no-extraction
```

chat 模型本身仍走原有 provider 配置：`LIORIUM_PROVIDER_CONFIG` → `~/.liorium/providers.json`
→ `~/.liorium/providers.json`（`--config` 可显式指定）。embedding 默认复用**当前 chat provider**
的 `base_url` 与 `key`，所以大多数 OpenAI 兼容端点不用额外配。

### B. 库内（自己装配 realm）

```python
from liorium.memory import MemoryRealm
from liorium.memory.embeddings import OpenAICompatibleEmbeddings
from liorium.memory.extract import LlmExtractor
from liorium.memory.vectors import NanoVectorBackend

inbox = Path("agents/main")
store_dir = inbox / "memory"

realm_factory = lambda spec: MemoryRealm(
    spec,
    embedder=OpenAICompatibleEmbeddings(          # 语义检索 + 实体名向量
        model="text-embedding-3-small",
        api_key=os.environ["OPENAI_API_KEY"],
        base_url="https://api.openai.com/v1",     # 任意 OpenAI 兼容端点
    ),
    vectors=NanoVectorBackend(store_dir / "index" / "chunks.vdb"),
    entity_vectors=NanoVectorBackend(store_dir / "index" / "entity.vdb"),
    extractor=LlmExtractor(chat=my_chat),         # async (system, user) -> str
)
```

`my_chat` 就是「喂 system + user，回一段（最好只有 JSON 的）文本」的协程，用哪个模型都行；
`LlmExtractor` 会宽容解析 ```json 围栏和前后废话。**只想抽实体不想上向量**：只传
`extractor=`（不给 `embedder`/`vectors`），实体去重会退化成名字精确匹配。

### 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 启动日志说「语义检索不可用」 | 没装 extra：`pip install 'liorium[vector]'`（numpy + nano-vectordb + httpx） |
| `embedding 维度不对: 期望 1536` | 服务端不支持 `dimensions` 参数。`.vdb` 格式要求 1536，换维度 = 断兼容（要么换服务，要么改 `EMBED_DIM` 并清空 `index/`） |
| `向量索引维度不兼容: 文件是 1536，本后端是 …` | 换了 embedding 模型。删掉 `index/chunks.vdb`（或换 `--memory-dir`）重来 |
| 写完记忆立刻 `memory_search` 搜不到语义结果 | 向量是**最终一致**的（后台队列，几百毫秒~几秒）；关键词结果立刻就有。要立刻齐可用 `realm.drain_index()` |
| 每一笔写入都多一次模型调用 | 那是抽取。`--no-extraction` 或 `params={"extraction": False}` |
| 关键词搜得到、语义一直搜不到 | `store.keyword_index_status()` 看 `pending_vectors`；`realm.drain_index()` 手动推完，失败原因看 ERROR 日志 |

## 9. 内存与性能预算（估算）

| 位置 | 既有实现 | Liorium | 常驻 |
|---|---|---|---|
| 图 | 全量 `networkx`（≈1KB/节点+边） | 递归 CTE，无投影 | ~0 |
| BM25 | `rank_bm25` 全量 token 列表 | FTS5 磁盘倒排 | 页缓存 ≤ 20MB |
| 向量 | nano 全量 | 同样全量（兼容代价）：惰性加载 + `unload()` + `vectors=False` | N×1536×4B（10 万 chunk ≈ 600MB） |
| VFS 投影 | 启动建全树 | 子节点懒加载 | 一层 |
| `.vdb` 文件 | — | ≈ 4/3 × N×1536×4B（base64） | 10 万 chunk ≈ 0.8GB |

建议：**≤10 万 chunk 开向量**；10–50 万按 scope 分库或 `vectors=False`；>50 万需要非兼容后端
（`VectorBackend` 协议留好了口子）。

关键词索引（FTS5，中文二元组+单字）在磁盘上大约是同量正文的 1–2 倍：10 万 chunk ≈ 0.3–1GB。
磁盘换 RAM，这是刻意的取舍。

## 10. 验收对照

| 项 | 结果 |
|---|---|
| 双向 `.vdb` 兼容 | `NanoVectorBackend` 写的文件用裸 `NanoVectorDB` 直读，metadata 逐键一致；反向亦然（`tests/test_memory_vectors.py`） |
| 双向库兼容 | 实体节点用 `ent_*` + `NULL path` + `data` JSON，path 节点 `has_child` 走 `parent_id`，与旧库同构（`test_memory_graph.py::test_reads_entity_rows_written_by_legacy_layout`） |
| 旧 JSON 迁移 | 单事务全有或全无，校验失败回滚、旧文件不动（`test_memory_migrate.py`） |
| 上下文隔离 | 抽取任务拿不到当前回合的 contextvar（`test_memory_extract.py`） |
| 全量测试 | workspace 864 passed / 3 skipped；记忆包自身 104 条（11 个测试文件） |

**没有做**的验收项：与既有实现在同一批查询上的 Recall@5 对比、以及 1 万节点 p95 压测——
这两项需要真实 embedding 服务与真实语料，属于部署侧验证，代码里只保证结构与算法一致。
