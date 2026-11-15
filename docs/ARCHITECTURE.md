# Liorium 架构（实现视角）

> 决策记录在 `docs/plans/2026-11-04-liorium-agent-core.md`（唯一真相）。
> 本文只描述**当前代码怎么组织的**，方便第二个宿主接进来。

## 1. 包与依赖方向

```mermaid
graph TD
    core["liorium-core<br/>types / uri / canonical / log / ports / errors"]
    vfs["liorium-vfs<br/>VfsNode / VirtualFileSystem / mount"]
    memory["liorium-memory<br/>SqliteMemoryStore / MemoryRealm"]
    skills["liorium-skills<br/>SKILL.md / SkillsRealm"]
    mcp["liorium-mcp<br/>McpClient / McpRealm"]
    todo["liorium-todo-realm<br/>TodoRealm / 状态化待办"]
    coretool["liorium-core-tool<br/>read/write/edit/search/find/execute"]
    coretoolrealm["liorium-core-tool-realm<br/>CoreToolRealm（core）"]
    realmbase["liorium-realm-base<br/>Realm / BaseRealm / RealmContext / @realm_tool"]
    provider["liorium-provider-manager<br/>schema / formats / thinking / models.dev / ChatModelPool"]
    profile["liorium-profile<br/>AgentProfile / load / build / ProfileBuild"]
    agent["liorium-agent<br/>LioriumAgent / runtime / realm / scheduler"]
    cli["liorium-cli<br/>REPL / 斜杠命令 / 子代理事件"]

    vfs --> core
    memory --> core
    memory --> vfs
    skills --> core
    skills --> vfs
    mcp --> core
    todo --> core
    todo --> realmbase
    coretool --> core
    coretool --> vfs
    coretoolrealm --> coretool
    coretoolrealm --> realmbase
    coretoolrealm --> vfs
    provider --> core
    agent --> core
    agent --> vfs
    realmbase --> core
    realmbase --> vfs
    agent --> realmbase
    agent -.-> coretoolrealm
    agent -.-> memory
    agent -.-> skills
    agent -.-> mcp
    agent -.-> todo
    profile --> agent
    profile --> provider
    cli --> agent
    cli --> provider
    cli --> profile
    cli -.-> memory
    cli -.-> skills
    cli -.-> todo
```

`liorium-cli` 是官方宿主：装配 Agent、渲染事件、订阅 `SubagentRealm` 的 ``events()``
做 `/subagent`。宿主的并发/展示策略留在 CLI 里，不回流到库。

`liorium-provider-manager` 是宿主侧的 Provider 管理库：配置 schema、四种 API 格式、
思考能力/参数、`/models` 自动发现、models.dev 目录与模型构建、`ChatModelPool`（按
`(label, intensity, purpose)` 懒建并负责关闭）。它只依赖 `liorium-core`，
不 import `liorium.agent`/`liorium.cli`；默认配置路径与交互向导由宿主提供。

`liorium-profile`（2026-11-14，第 13 个包）是**装配层**：读 `profile.json`（`version: 1`，
未知字段报错），展开 `${VAR}`/`~`，按 `source`（`pkg.mod:Class` 或 `./file.py:Class`）导入
realm 类并校验其 `Config`，然后把一份声明编译成 `AgentSpec` + realm 工厂表 + `ChatModelPool`
（`ProfileBuild`）。它依赖 `agent` + `provider`，不依赖 `cli`；CLI 只是它的第一个宿主
（`--profile` / `.liorium/profile.json`）。旧的 `Profile` / `AgentSpec.with_profile` 语法糖
已删除：`extends` 的合并语义是当年删掉它的原因，复用改为复制一份 profile 文件。

`tests/test_architecture.py` 用 AST 守这条线：core 只依赖标准库；任何包不许 import
旧后端模块或旧插件系统；能力包之间互不依赖；裸 `asyncio.create_task` 只允许在
`liorium/realm_base/base.py`（`RealmTaskScope`）与 `cli/subagents.py`（CLI 宿主自己托管的
事件监控任务）。

> 2026-11-06 决策：能力包（`liorium-core-tool` / `liorium-memory` / `liorium-skills` /
> `liorium-mcp`）**可以** import `liorium.agent` 的 realm 契约（取消反向依赖禁令，
> 当前尚无包实际使用）；能力包之间仍互不依赖。`Realm.mounts()` 的返回类型是 `VfsMount`
> （计划 §3.3），所以它们仍需要 `liorium-vfs`。

`liorium-realm-base`（2026-11-07）是 realm 的**契约与写作 API**：`Realm` / `BaseRealm` /
`RealmContext` / `JsonStateStore` / `TurnInfo` / `@realm_tool`。它只依赖 `core` + `vfs`，
模块级不 import 任何 `langchain*` / `langgraph*`（守卫 `test_realm_base_stays_light`），
所以第三方 realm 作者不必为了写一个 realm 装下整个 Agent 运行时。`liorium-agent` 依赖它并
re-export 旧名字（`from liorium.agent import BaseRealm` 仍可用，且是同一个对象）。

`liorium-todo-realm` 是**只有 realm 的能力包**：只依赖 `core` + `realm_base`（连
`liorium.agent` / `liorium-vfs` 都不 import），提供 `todo` / `todo_list` 两个工具、四个状态
（`pending` / `processing` / `blocked` / `finished`），并用第五拍每 50 回合提醒一次未完成事项。

`liorium-core-tool-realm`（2026-11-08）是六个核心工具的 realm 壳，**`liorium-agent` 不再依赖
`liorium-core-tool`**：工具实现留在 `liorium-core-tool`，装配成 `RealmSpec(id="core")` 才生效。
于是「Agent 自带什么工具」这个问题消失了 —— `Agent._prepare_runtime()` 里只有
`tools = realms.tools()`，零工具也是合法装配。

**realm 实例作用域**（2026-11-08）：`instance_scope="tree"` 的 realm（memory / skills / mcp /
subagents）整棵树一份实例；子 Agent 只借用它的 `tools()`，不重复挂载、不重复消费周期上下文、
不重复生效中间件与提示词；`"agent"` 的 realm（core / todo）每个 Agent 一份、状态在各自 inbox 下。
框架在启动期拒绝「`agent` 级 realm 返回挂载点」，因此同一路径不可能有两套状态
（详见 [design/realm-scope.md](design/realm-scope.md)）。

## 2. 一个回合发生了什么

```mermaid
sequenceDiagram
    participant H as 调用方
    participant A as LioriumAgent
    participant I as Inbox
    participant W as TurnWorker
    participant L as runtime.loop
    participant G as LangGraph(create_agent)
    participant M as 中间件链
    participant E as EventHub

    H->>A: push(text, id=..., bucket=..., flags=...)
    A->>I: 入队（bucket / flags / timeout / 同 id 原位替换）
    A-->>H: input(accepted)
    Note over H,A: 订阅先于 push：stream() 在 async for 开始时注册，不回放
    W->>I: peek_turn_entry()（IMMEDIATE 先于 MAJOR，同档 FIFO）
    W->>W: 占执行位（EXCLUSIVE ⇒ 拒绝被抢）
    W->>L: execute_turn(runtime, request)
    L->>L: 回合开始 Context 并入 user 消息 + 写 state.context
    L-->>E: start(turn_id, origin, inputs)
    W-->>E: input(delivered, turn_id)
    L->>G: astream_events({"messages": [...], "context": [...]})
    G->>M: context_merge → mm_bridge → tool_repair → server_tools → compact → model_retry
    G-->>L: on_chat_model_stream / on_tool_start / on_tool_end
    L-->>E: delta / reasoning_delta / tool_start / tool_result / round_aborted
    L-->>E: done（终态）
    E-->>H: stream() 每条订阅各收一份
```

- 事件共 14 种；`stream()` 是唯一出口，可开多个、**不回放**、发布永不阻塞。
- **终态帧约定**：每个回合恰好一条 `done` / `interrupted` / `error`，且是该回合最后一条事件。
  `done` 不带 `final_text`（文本已经流过 `delta`），`error` 不带 traceback（进日志）。
  `IMMEDIATE` 抢占只切模型轮（补非终态 `round_aborted`），**不结束回合**。
- `inject()` 只入队；`context_merge` 在**下一个模型调用前**把它物化成独立 user 消息（安全点）。
- 第五拍：回合开始时问一次 `realms.context_for_turn(TurnInfo)`；到期且返回非空才排队，
  同样落在安全点，成为独立 `<context>` 消息（不重写历史前缀）。
- 第六拍：realm 可以主动说话 —— `ctx.emit` 推非终态 `realm` 帧（不开回合）、`ctx.push` 复用
  `Agent.push`（默认 `environment` 不开回合）、`ctx.interrupt` 叫停本回合；另有六个只读钩子
  （`on_turn_*` / `on_model_*` / `on_tool_*`，进入声明序、退出反序，**没有改写通道**）。
- `interrupt()` 取消本回合的托管任务与模型流；历史里被打断的半条消息保留，由 `tool_repair` 补占位。

完整语义（bucket / flag / 安全点 / 维护插队 / 生命周期）见
[design/event-stream.md](design/event-stream.md)。

## 3. 模块地图（`liorium-agent`）

| 模块 | 职责 |
|---|---|
| `spec.py` | `AgentSpec` / `RealmSpec`（`config` + `params` 别名）/ `Limits` / `BootSpec`；结构指纹（排除 id/inbox/boot） |
| `agent.py` | `LioriumAgent`：`start/close/push/stream/inject/interrupt/send_to_realm/apply/reset_dialog/status` |
| `context.py` | `ContextChannel`：待物化队列 + 审计记录 |
| `status.py` | `AgentStatus` / `ApplyResult` / `CacheEpoch` |
| `realm/base.py` | **re-export**（实体在 `liorium.realm_base`）：`Realm` 协议、`RealmContext`（`vfs/state/config/inbox/conversation/spawn/log/emit/push/interrupt/turn/model`）、`JsonStateStore`、`RealmTaskScope`、`TurnInfo`、六个只读钩子与其数据对象 |
| `realm/host.py` | `RealmHostPort`：把 `ctx.emit` / `ctx.push` / `ctx.interrupt` / `ctx.turn` 接到 `EventHub` / `Agent.push` / `TurnWorker`（第六拍） |
| `realm/hooks.py` | `RealmHooksMiddleware`：每个覆写了模型/工具钩子的 realm 一个适配中间件，进入发 `*_start`、返回/异常发 `*_end`；钩子异常只记日志（第六拍） |
| `realm/manager.py` | 装配、`requires` 拓扑排序、start/stop、工具名冲突、失败降级、挂载回滚、dirty 刷新、conversation scope、第五拍（`context_for_turn` + 相位落盘）、第七拍（`send` + `send_lock` + `CURRENT_REALM_MESSAGE`） |
| `realm/subagent.py` | `SubagentRealm`：`newSubagent` / `invokeSubagent` / `wait_for_subagent` / `stopSubagent` / `removeSubagent` / `listSubagents`，`subagents://` 投影，名册与正文落盘（[`design/subagent-state.md`](design/subagent-state.md)） |
| `runtime/loop.py` | `execute_turn`：拼 input、跑图、翻译事件、`max_turns` 保护、`IMMEDIATE` 抢占的安全点裁决 |
| `runtime/stream.py` | LangGraph 事件 → Liorium 事件（四种 API 格式；`call_id` = run_id） |
| `runtime/state.py` | `LioriumState`（`messages` + `context` channel） |
| `runtime/inbox.py` | 入站队列：`Bucket` / `Flag` / `Entry` / `BatchWindow`（四旋钮）、同 id 原位替换、`timeout` 惰性过期、`QUEUE_MAX` 丢弃 |
| `runtime/worker.py` | `TurnWorker`：常驻单执行位，取条目 → 跑回合 → 补终态帧；`IMMEDIATE` 抢占信号、维护插队、合批到期开批回合 |
| `runtime/hub.py` | `EventHub` / `Subscription`：`put_nowait` 扇出、每订阅者无界队列、`ids` 过滤、不回放、`close()` |
| `runtime/scheduler.py` | `ExecSlot`：单执行位 + `exclusive` + 维护队列（**没有优先级**） |
| `runtime/prompt.py` | 身份 → 规则 → realm 片段 → 技能清单 → 尾注（顺序固定） |
| `runtime/checkpoint.py` | `AsyncSqliteSaver` 生命周期、system 消息植入、`reset_dialog` |
| `runtime/tools.py` | 工具输出拦截（OutputStore + artifact 引用） |
| `runtime/output_store.py` | `artifact://` 完整内容存储（60000 字符 / 300 行摘要） |
| `runtime/repair.py` | 纯函数：悬空 tool_call 修复（压缩与 tool_repair 共用） |
| `runtime/usage.py` | API 上报 token 归一化（Anthropic/OpenAI/Gemini 口径） |
| `runtime/middleware/` | 基座链（顺序固定）：context_merge → mm_bridge → tool_repair → server_tools → compact → model_retry |

## 4. 扩展点（realm）

一个能力 = **工具 + VFS 挂载 + 状态 + `prompt_fragment()`**，外加一条可选的动态节拍
（第五拍：`context_every` + `context_items()`，见 §4.3）：

```python
from liorium.realm_base import BaseRealm, RealmContext, realm_tool
from liorium.vfs import VfsMount, VirtualFileSystem


class WeatherRealm(BaseRealm):
    id = "weather"
    requires = []

    @realm_tool                          # 工具（schema 变化 ⇒ CacheEpoch +1）
    async def weather_lookup(self, city: str) -> str:
        """查一个城市的当前天气。"""
        return ...

    def prompt_fragment(self) -> str:    # 用法说明随包走（跨工具工作流）
        return "问穿衣/出行建议时，先查天气。"

    def mounts(self):                    # /weather ⇔ weather://
        fs = VirtualFileSystem(owner_id="weather")
        return {"/weather": VfsMount(prefix="/weather", vfs=fs, readonly=True)}

    async def start(self, ctx: RealmContext):
        ctx.spawn(self._prefetch_hourly())   # 托管后台任务，禁止裸 create_task

    async def stop(self, ctx: RealmContext):
        ...


agent = LioriumAgent(
    spec=AgentSpec(id="my-agent", inbox_dir=Path("agents/my-agent"), model="deepseek::chat",
                   prompt_sources=("AGENT.md", "ROLE.md"), realms=[RealmSpec(id="weather")],
                   limits=Limits(context_window=200_000, max_turns=100)),
    model_factory=make_chat_model,
    realm_factories={"weather": lambda spec: WeatherRealm()},
)
```

### 4.0 realm 自组装：`Config` + `ctx.model`（2026-11-14）

realm 的配置不再是"宿主塞进去的 `params` 字典"，而是**realm 自己声明的 `Config` 模型**：
profile 的 `realms[].config` 在 `load()` 时用它校验并填默认值，realm 在 `start()` 里按它
自组装自己需要的服务（向量后端、embedding、抽取器……）。宿主只看见 `spec.realms[].config`。

```python
from pydantic import BaseModel, ConfigDict


class WeatherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 拼错的键要报错，不能静默忽略

    unit: str = "c"
    timeout: float = 10.0


class WeatherRealm(BaseRealm):
    id = "weather"
    Config = WeatherConfig                       # 可选；profile loader 用它校验

    async def start(self, ctx: RealmContext):
        config = WeatherConfig.model_validate(ctx.config)   # 已由 loader 校验/补默认
        self.unit = config.unit
        # ctx.model：宿主的 aux 模型（compact 工厂，不思考、无工具）；没注入时为 None
        self._extractor = LlmExtractor(ctx.model) if ctx.model is not None else None
```

- `ctx.config` 是 canonical；`ctx.params` 保留为只读别名（旧 realm 不用改）。
- `ctx.model` 每次访问都向工厂要一个**新**实例（模型归工厂关，realm 不缓存、不关闭）；
  独立使用 realm（没有 aux 工厂）时返回 `None`，需要模型的能力要自己降级。
- `requires` 里的 realm 一定先 `start()`；`Config` 校验失败属于**配置错**，在 `load()` 就报，
  不进运行期降级。

- `requires` 拓扑排序；工具名冲突直接 `failed`（带冲突详情），不静默跳过。
- realm 起不来 ⇒ 该 realm `failed`、其工具与挂载移除、其它 realm 照常（降级但可用）。
- `apply(spec)` 只重建指纹变化的部件；失败保留旧部件并返回 `ApplyResult.failed`。

### 4.1 运行期刷新（v1.1：realm 自报 dirty）

`RealmSpec.config` 之外的变化（MCP 发现了新工具、调用方装了技能）不进 spec 指纹，
由 realm 自己记账，Agent 在**回合边界**（拿到执行位之后）统一处理：

```mermaid
sequenceDiagram
    participant H as 宿主
    participant A as LioriumAgent
    participant R as RealmManager
    participant X as realm

    H->>H: realm.set_servers(...) / 装技能
    H->>A: push(text, id=...)
    A->>R: refresh()
    R->>X: dirty()
    R->>X: refresh()（可选，async；MCP 重连）
    R->>X: mounts() / tools() 重新取
    A->>A: 重建运行时（工具 schema / prompt 变则 CacheEpoch +1）
```

realm 可选实现三个钩子（`BaseRealm` 有默认空实现）：

| 钩子 | 语义 |
|---|---|
| `dirty() -> bool` | 真相源变了、需要刷新 |
| `mark_dirty()` / `clear_dirty()` | 宿主标记 / 刷新后清账 |
| `async refresh()` | 刷新前重连（MCP）；失败按 §C5 降级 |
| `skills() -> list[str]` | 预载进 prompt「技能清单」段 |

- 刷新失败 ⇒ 该 realm 标 `failed`、撤工具与挂载，其它 realm 照常。
- `state_scope="conversation"` 的 realm 状态按 thread 分桶，`reset_dialog()` 时清空；
  `agent` scope 跨对话保留（计划 §C6 / §3.3）。
- **装配属于 profile，不属于 AgentSpec**：`AgentSpec` 只描述"这一刻的运行时形状"，
  "为什么是这个形状"（模型选择、realm 清单、每个 realm 的 config、`${VAR}` 展开）在
  `profile.json` 里，由 `liorium-profile` 编译。换 profile = 重新 build + `apply()`。

## 4.2 realm 能力包（v1.1）

| 包 | 模块 | 能力面 |
|---|---|---|
| `liorium-memory` | `store.py`（schema 与旧 `memory.sqlite` 一致）、`fts.py`、`vectors.py`、`embeddings.py`、`search.py`、`indexer.py`、`graph.py`、`extract.py`、`migrate.py`、`realm.py`、`tools.py` | `memories://` 懒加载投影 + 混合检索（FTS5/向量/图谱 RRF）+ 实体抽取；写走 `write_handler` / `create_handler`，读走 `provider`（详见 [MEMORY](MEMORY.md)） |
| `liorium-skills` | `catalog.py`（`SKILL.md` 发现 + mtime 指纹）、`frontmatter.py`、`realm.py`、`tools.py` | `skills://` 只读挂载 + `list_skills` + prompt 技能清单 |
| `liorium-mcp` | `client.py`（stdio/sse/streamable-http，SDK 延迟导入）、`schema.py`（JSON Schema→pydantic）、`realm.py`、`tools.py` | 运行时发现工具（`<server>_<tool>`）+ `set_servers`/`mark_dirty` 重连 |
| `liorium-todo-realm` | `model.py`（状态机 + 渲染）、`realm.py` | `todo`（add/update/remove）/ `todo_list` + 每 50 回合的未完成清单提醒（只在有 `pending`/`processing` 时） |

**投影目录的写入**：`VfsNode.create_handler`（v1.1 新增）让"本树里还不存在的路径"交给
目录节点处理（例如 memory 写进 SQLite），而不是在 VFS 里凭空建一个临时的、重启即丢的节点；
handler 返回的字符串走 ACK 通道（`↳ ack`）。

## 4.3 周期上下文（第五拍，2026-11-07）

前面四条描述的是"这个能力提供什么"，只在装配和刷新时读一次。第五拍补的是**运行期**：
realm 按回合节奏说一句"现在有这些事"。

```python
class TodoRealm(BaseRealm):
    context_every = 50      # 每 50 个回合给一次机会；0 = 不参与（默认）
    context_first = True    # 第 1 回合先问一次，再进入 50 的节奏

    def context_items(self, turn: TurnInfo):     # 可 sync 可 async
        if not self._open_items():
            return None      # 返回空 = 不消费周期，下一回合继续问
        return ContextItem(source="realm:todo", text=render(self._open_items()))
```

| 规则 | 行为 |
|---|---|
| 计时 | 每个 origin 匹配的回合 `due -= 1`；归零那一回合才调用 |
| 返回空 | 不消费周期（`due` 停在 0）—— 空的待办清单不会白等 50 轮 |
| 返回非空 | 相位重置为 N 并写进 realm 自己的 state（`__context_due__` 保留键）⇒ 跨重启保留 |
| 异常 | 记 error、只跳过这个 realm、下一回合重试；不改 `record.state` |
| 落点 | `ContextChannel.queue()` → `context_merge` 在下一个安全点物化成独立 `<context>` user 消息 |

为什么不用 `prompt_fragment()`：它属于 `CacheEpoch` 的一部分，改它要重写整段历史
（前缀缓存全失效）；注入消息在历史**末尾**，前面的前缀照常命中缓存。

## 4.4 主动推送与只读钩子（第六拍，2026-11-14）

第五拍仍是"框架问、realm 答"。第六拍给 realm 两个**写**出口和一整圈**只看不改**的观测面：

| 入口 | 作用 | 约束 |
|---|---|---|
| `ctx.emit(name, data=None)` | 往事件流推一帧非终态 `realm` | 不开回合、不落 checkpoint；没有宿主 ⇒ 静默 no-op |
| `ctx.push(text, bucket="environment")` | 复用 `Agent.push` 投条目 | 默认 `environment` 不开回合；显式 `major` 才开；没有宿主 ⇒ `RealmError` |
| `ctx.interrupt(reason="realm")` | 叫停当前回合 | 同步；事件流以 `interrupted(reason="realm")` 收尾；没有宿主 ⇒ `RealmError` |
| `ctx.turn` | 当前 `TurnInfo` 或 `None` | 与 `context_items(turn)` 的入参同源 |

六个只读钩子（`on_turn_start/end`、`on_model_start/end`、`on_tool_start/end`）是 `BaseRealm` 上的
`async` 空实现，进入按声明序、退出反序；它们**拿不到请求对象、没有返回值、没有 `nxt()`**，
所以机制上改不了模型请求（realm 永远不能改写请求，这是硬约束，不是自觉）：

```mermaid
sequenceDiagram
    participant W as TurnWorker
    participant RA as realm A
    participant RB as realm B
    participant M as 模型/工具
    W->>RA: on_turn_start
    W->>RB: on_turn_start
    W->>RA: on_model_start → RB: on_model_start
    RA->>M: （请求由基座拼装，realm 只收到快照）
    M-->>RB: on_model_end
    M-->>RA: on_model_end
    RB-->>W: on_turn_end
    RA-->>W: on_turn_end
    W-->>W: 终态帧 done（恰好一条、在最后）
```

- 钩子异常只记日志，不改变回合；要中止就用 `ctx.interrupt()`（在 `on_turn_end` 里中断会把
  `done` 变成 `interrupted`，别这么写）；
- 没覆写模型/工具钩子的 realm 不装适配中间件（静默 realm 零成本）；
- 需要真改请求是**宿主**的 `Realm.middleware()`，不属于 realm 公共面。

## 4.5 宿主 → realm 消息（第七拍，2026-11-14）

第六拍是 realm 主动说，第七拍反过来：外部主程序把一条消息**直接**交给某个 realm。

```mermaid
sequenceDiagram
    participant H as 宿主（GUI / webhook / cron）
    participant A as LioriumAgent
    participant R as RealmManager
    participant X as realm
    H->>A: await send_to_realm("todo", "todo.add", {"text": ...})
    A->>R: send(realm_id, RealmMessage)
    R->>R: record.send_lock（与 start/stop 互斥）
    R->>X: on_message(message)
    X-->>R: 返回值
    R-->>A: 原样返回
    A-->>H: 结果
```

| 规则 | 不变式 |
|---|---|
| 执行位置 | **宿主 task 里立即执行**，不进维护队列、不等回合结束（handler 必须廉价） |
| 成败 | 状态不对就抛（未装配 / 停用 / 失败 / 无 handler / 超时），handler 异常原样上抛 |
| 互斥 | `RealmRecord.send_lock` 同 realm 串行；`start`/`stop` 持同一把锁 ⇒ 停用会等在手消息 |
| 树级 | 子 Agent 的 record 就是根的那条 ⇒ 消息落到根实例，`ctx.emit`/`ctx.push` 作用在根 Agent |
| 防互锁 | `CURRENT_REALM_MESSAGE` ContextVar：handler 里调维护操作 / `close()` 直接 `TurnError` |
| 事件流 | 不新增帧；想留痕由 handler 自己 `ctx.emit`（落成 `realm` 帧） |
| 模型请求 | 完全不变（消息不进历史、不过模型、不影响指纹） |

## 5. 设计取舍（与既有实现的差异）

| 现状 | Liorium |
|---|---|
| `plugins/system` + 旧插件系统的 hook | 只有 realm 一种扩展机制（插件由宿主翻译） |
| `rebuild_runtime(reset_dialog, no_initial_chat)` 55 处调用 | `start/close/push/stream/apply/reset_dialog` 几个显式操作 |
| `invoke_agent_locked` + 队列 + `_StreamState` | 常驻 `TurnWorker` + `Inbox` + `EventHub`；回合由 worker 拥有，终止 = 一条终态帧 |
| `get_output_store()` 进程单例 | 构造注入 `OutputStore`（每个 Agent 一份） |
| 全局 `state.PROMPT` | `AgentSpec.prompt_sources`（库不读产品配置） |
| `skill://` `sourceCode://` `img_source://` | `skills://` `code://`（`screen://`/`camera://` 由产品侧 realm 提供） |
