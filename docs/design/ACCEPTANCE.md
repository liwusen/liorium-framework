# Phase 1 验收对照（计划 §5）

> 计划：`docs/plans/2026-11-04-liorium-agent-core.md` §5 Phase 1「验收（全部满足才算完）」。
> 本文件是逐条证据，命令统一为：

```powershell
git clone https://github.com/liwusen/liorium-framework.git
cd liorium-framework
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider        # 全量
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m live # 真模型
```

v1.1 完成时的全量结果：**755 passed, 3 skipped, 0 failed**（v1.0 时为 658；最新数字见文末）。
3 个 skip = 2 个真模型用例（未设 `OPENCODE_GO_KEY`）+ 1 个 Pillow 缩放用例（`.venv` 未装 Pillow）。

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 真模型单轮对话；工具循环；压缩触发并正确写回 | ✅ | `tests/test_live_agent.py::test_live_single_turn` / `::test_live_tool_cycle`（OpenCode Go `deepseek-v4.1-flash`，2026-11-04 实跑通过）；压缩写回 `packages/liorium-agent/tests/test_compact_middleware.py`（24）+ `test_compact_formats.py`（28） |
| 2 | 429 重试、用户中断、悬空 tool_call 修复 | ✅ | `test_model_retry.py`（3：重试恢复 / 耗尽即抛 / 重试在最内层）；`tests/test_event_contract.py::test_interrupt_raises_and_never_emits_done` + `packages/liorium-agent/tests/test_turn_loop.py`（流中途中断）；`test_tool_call_repair.py`（20） |
| 3 | Context 两种物化形态：回合开始合并、运行中作为独立 user 消息追加在安全点 | ✅ | `tests/test_agent.py::test_turn_start_context_merged`、`test_inject_queues_without_interrupt`；`test_turn_loop.py::test_pending_context_becomes_separate_user_message_before_next_model_call`（断言插在 `ToolMessage` 之后） |
| 4 | 压缩切点在"运行中注入产生额外 user 消息"后仍然正确 | ✅ | `tests/test_compact_cut_with_injected_context.py`（3：注入消息不当轮起点 / 不拆 tool 组 / cutoff 必在轮起点） |
| 5 | realm 热重载：改实现不升 epoch；改 schema 升 epoch 且工具立即生效 | ✅ | `tests/test_agent.py::test_apply_realm_impl_change_keeps_epoch`（实现变、schema 不变 ⇒ epoch 不变、新实现立即生效）；`::test_apply_realm_tool_change_bumps_epoch`（加工具 ⇒ epoch +1） |
| 6 | 假模型 + 真实旧数据夹具：加载旧 `checkpoint.db` 继续对话 | ✅ | `tests/test_agent.py::test_loads_legacy_checkpoint`（复制 `~/.liorium/agents/ishmael/checkpoint.db`，保留旧历史并继续对话）；`::test_reads_large_legacy_checkpoint_blob`（只读解码既有实现库 311 条消息） |
| 7 | F6 列出的旧测试在新实现上跑绿 | ✅（有替换，见下） | 见下表 |
| 8 | `run()` 事件序列与前端消费序列等价 | ✅ | `tests/test_event_contract.py`（金样：9 种 Agent 事件、无宿主事件、`done.turn_id`、tool 事件字段） |

补充（计划 §B6/§B7，不在 8 条验收里但已实现）：回合边界 reconcile ——
`tests/test_reconcile.py` 覆盖「provider 返回新 spec ⇒ 自动 apply」「provider 抛错不炸回合」
「同指纹 no-op」。

## 最近一次全量（v1.1 之后；v1.0 时为 658 passed）

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
755 passed, 3 skipped
```

## 验收 7 的 F6 逐项对照

| 旧测试（F6） | 新实现上的等价资产 | 用例数 |
|---|---|---|
| `test_compact_*`（4 个文件） | `test_compact_middleware/formats/regressions/cost_control.py` | 24+28+21+7 |
| `test_tool_call_repair` | `test_tool_call_repair.py` | 20 |
| `test_agent_scheduler` | `test_scheduler.py` + `test_turn_flags.py` + `test_agent.py` 的并调用例 | 10 + 6 + 1 |
| `test_mm_bridge` | `test_mm_bridge.py` | 16 |
| `test_subagent_manager` | `test_subagent_realm.py`（子代理是 `LioriumAgent`，不是 Manager） | 6 |
| `test_vfs_handler_ack` | `packages/liorium-core-tool/tests/test_runtime.py` 的 ACK 用例（`↳ ack`） | 3 |

**偏差（有意）**：旧 `agent/scheduler.py` 的"4 档优先级 + TurnKind/播放闸门"是产品语义，计划 §3.2
只要求"排队、不抛忙、优先级由 scheduler 决定"。Liorium 的 `TurnScheduler` 实现"单执行位 + 调用级
优先级 + FIFO + 取消不泄漏"；2026-11-08 起还迁了三个正交标志
（`INTERRUPTING` / `EXCLUSIVE` / `DROPPABLE`），`sink` / `gate` 因为本项目没有播报通道与播放闸门
而不迁（见 [`scheduler.md`](scheduler.md)）。因此 `test_agent_scheduler` 的 15 条产品用例
没有逐条移植，而是移植了其中与库语义有关的部分。

## 最近一次真模型实跑记录

```
tests/test_live_agent.py::test_live_single_turn   PASSED  [0.5s]
tests/test_live_agent.py::test_live_tool_cycle    PASSED  [9.8s]
```

- 端点：`https://opencode.ai/zen/go/v1`（OpenAI 兼容，需 `x-opencode-session` 头，见 `tests/live_support.py`）
- 模型：`deepseek-v4.1-flash`
- 工具循环确实由模型自主发起 `read` 并读到 `第一行`

## 尚未覆盖（计划明说不做 / 后续）

- 权限 / 人工审批（计划 §F3 明确 v1 不做）
- Pillow 缩放路径：`.venv` 未装 Pillow 时该用例 skip（装了则覆盖）

---

# v1.1（Phase 2）验收

> 计划 §5 Phase 2：「`liorium-skills` / `liorium-memory` / `liorium-mcp` 三个包；MCP 的
> "宿主 mark dirty"接法；`conversation` scope；profile 语法糖；多 Agent 宿主示例
> （2 个 `LioriumAgent` + semaphore）」。

| # | 交付项 | 状态 | 证据 |
|---|---|---|---|
| 1 | `liorium-memory`：与旧库同 schema 的存储 + `MemoryRealm` | ✅ | `packages/liorium-memory/tests/test_memory_store.py`（12：读写/覆盖/分块/检索/旧库直读/版本不兼容报错）；`test_memory_realm.py`（8：挂载投影/新建/覆盖/编辑/图片/dirty/状态） |
| 2 | `liorium-skills`：`SKILL.md` 发现 + `SkillsRealm` + 技能清单进 prompt | ✅ | `packages/liorium-skills/tests/test_skills.py`（14：frontmatter/meta/发现/挂载树/只读/list_skills/清单截断/dirty） |
| 3 | `liorium-mcp`：客户端 + 运行时发现工具 | ✅ | `packages/liorium-mcp/tests/test_mcp_schema.py`（6：JSON Schema→pydantic）；`test_mcp_realm.py`（8：前缀命名/调用/单 server 降级/全失败 strict/禁用） |
| 4 | MCP「宿主 mark dirty」接法（回合边界重连换工具） | ✅ | `test_mcp_realm.py::test_mark_dirty_reconnects_new_servers`（新增 server → `refresh()` 后工具出现、旧 client 被关）；`packages/liorium-agent/tests/test_realm_v11.py::test_mcp_like_refresh_hook_is_awaited`（`async refresh()` 先于取工具执行、epoch +1） |
| 5 | `conversation` scope | ✅ | `test_realm_v11.py::test_conversation_scope_state_resets_on_reset_dialog`（`reset_dialog()` 清空 conversation scope、保留 agent scope）；`test_realm_manager.py` 的路径/非法 scope/重置用例 |
| 6 | profile 语法糖 | ⚠️ 已删除 | 2026-11-14 起由 Agent Profile（`liorium-profile`）取代，见文末「变更（十二）」；旧 `Profile` / `AgentSpec.with_profile` 与 `packages/liorium-agent/tests/test_profile.py` 一并删除 |
| 7 | 多 Agent 示例（2 个 `LioriumAgent` + 调用方 semaphore） | ✅ | `examples/multi_agent.py`（`run_agents` 用 `asyncio.Semaphore` 包住整回合的 `async for`）；`tests/test_multi_agent_example.py`（2：两个输出、各自 inbox/checkpoint + 事件） |
| 8 | 端到端：能力包装到 Agent 上（假模型） | ✅ | `tests/test_v11_integration.py`（2：写记忆 → 读 → 列目录带元数据 → 检索 → 列技能 → 装技能后 dirty 刷新并 epoch +1；`edit` 走 VFS handler 改 SQLite 正文） |

## 与计划的一处细化（v1.1）

计划 A4 的包图把能力包画成"只依赖 core"。实现时发现 `Realm.mounts()` 的返回类型是
`VfsMount`（计划 §3.3 明确要求），所以 `liorium-memory` / `liorium-skills` 依赖
`liorium-vfs`。**能力包之间仍然互不依赖**；架构守卫守这条边界
（`tests/test_architecture.py`）。

## 2026-11-06 变更：允许能力包 import `liorium.agent`

原守卫禁止能力包 import `liorium.agent`（理由是 realm 是结构化协议、能力包要能独立
发布）。该反向依赖禁令已取消：`liorium-core-tool` / `liorium-memory` / `liorium-skills` /
`liorium-mcp` 都可以 import `liorium.agent` 的 realm 契约；能力包之间仍然互不依赖。

## 2026-11-07 变更：新增 `liorium-realm-base`（realm 契约 + 写作 API）

`Realm` / `BaseRealm` / `RealmContext` / `JsonStateStore` / `RealmTaskScope` / `RealmRecord`
从 `liorium-agent` 搬到新包 `liorium-realm-base`（模块 `liorium.realm_base`）；`liorium-agent`
依赖它并保留同名 re-export（`from liorium.agent import BaseRealm` 仍是同一个对象）。
同批落地写作 API：`@realm_tool`、`self.state`、`save()`、`on_start/on_stop`、`TurnInfo`，
以及第五拍 `context_items()` 的**声明**（节拍器在当天的下一批改动里接线，见下节
「接线第五拍 + `liorium-todo-realm`」；设计见 [realm-context.md](realm-context.md)）。

| 证据 | 说明 |
|---|---|
| `packages/liorium-realm-base/tests/test_realm_base.py`（14，当次；另有 2 个 `inject()` 用例见下节） | 契约默认值 / `on_start`-`on_stop` / 老写法自己实现 `start` / 工具收集顺序与子类覆盖 / docstring 描述 / `save()` 立刻落盘 / `state` 未 `start()` 报错 |
| `packages/liorium-agent/tests/test_realm_kit.py`（3） | re-export 是同一对象；只用糖的 realm 能装配、工具可调用、状态落盘；`prompt_fragment()` 进 system prompt |
| `tests/test_architecture.py::test_realm_base_stays_light` | 新包模块级不许 import `langchain*` / `langgraph*` |
| `tests/test_architecture.py::test_liorium_dependency_direction` | 新包不许 import `liorium.agent`（防 `agent → 它 → agent` 成环） |

实测 `import liorium.realm_base` 不加载任何 `langchain*` / `langgraph*` 模块。

## 2026-11-07 变更（二）：接线第五拍 + 新增 `liorium-todo-realm`

第五拍从"只在 `BaseRealm` 里声明"变成真正运行：`RealmManager.context_for_turn(TurnInfo)`
按 `context_every` / `context_first` / `context_origins` 问运行中的 realm，非空结果排进
`ContextChannel`，在安全点物化成独立 `<context>` user 消息。`RealmContext` 同时获得推送侧
`inject()` / `inject_items()`（用 `ContextSink` 结构协议，避免 `realm_base → agent` 成环）。

同批新增第 11 个包 `liorium-todo-realm`（模块 `liorium.todo_realm`）：`todo` / `todo_list`
两个工具、四个状态、`context_every=50` + `context_first=True`、只在有 `pending` / `processing`
时注入、返回空不消费周期。CLI 默认装载它（`--no-todo` 可关），新增 `/todo` 命令。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_realm_context.py`（15） | 第 1/4/7 回合注入、空不消费周期、`context_first=False` 每 N 回合、`params` 覆盖、`context_origins` 过滤、`0` 一次不问、sync/async + 三种返回类型归一化、独立消息 + 审计 `turn_id`、`ctx.inject` 与拉取同批物化、`restart` 后相位保留、单 realm 异常降级、`status` 的 `context_due`/`context_injections`、非法节奏按 0 |
| `packages/liorium-todo-realm/tests/test_todo_realm.py`（20） | 两个工具、id 递增不复用、状态与正文改动、默认隐藏 finished、排序、六种非法输入只回错误文本、重启保留 items/next_id、状态文件损坏在 `start()` 报错、注入只在有 pending/processing 时发生（含 blocked-only / 全 finished）、提醒句、prompt 片段、`counts`/`summary_line` |
| `packages/liorium-realm-base/tests/test_realm_base.py`（+2） | `ctx.inject()` 无通道抛 `RealmError`；有 sink 时 `source` / `text` / `meta` 正确，空文本拒绝 |
| `tests/test_v11_integration.py::test_agent_with_todo_realm_injects_context` | 真装配端到端：第 1 回合建待办 → 第 2 回合立刻收到提醒；`<context>` 是独立 user 消息；`realm_state/todo.json` 有 `__context_due__=50`；`status` 计数正确 |
| `packages/liorium-cli/tests/test_cli_agent.py`（+2） | 默认装载 todo（工具 + realm），`/todo` 列出条目、`/status` 显示待办摘要与下次提醒；`--no-todo` 时降级提示 |
| `tests/test_architecture.py` | `liorium-todo-realm` 的允许面被钉死为 `{liorium.core, liorium.realm_base}`（不 import agent / vfs）；包发现列表含新包 |

## 2026-11-07 变更（三）：`/realm` 与运行期开关 realm

新增 `LioriumAgent.set_realm_enabled(realm_id, enabled)`（薄封装 `RealmManager.set_enabled()`）：
只停 / 起目标 realm，然后重建运行时 —— 工具、挂载、prompt 段随状态出现或消失，
`CacheEpoch` 相应 +1；装配集合（`AgentSpec.realms`）不变，`status()` 里显示 `stopped`。
启动失败按 §C5 降级为 `failed`（不抛），`_build_runtime` 失败时回滚 realm 状态再抛。
`RealmManager.context_cadence()` 让宿主 / CLI 不必碰私有 `_cadence`，未 `start()` 时从
登记的 spec 读参数（启动前后给同一个答案）。

CLI 新增 `/realm`：列表（状态 / 工具数 / 挂载 / 第五拍节奏）、`/realm <id>` 详情、
`/realm disable|enable <id>`；`/status` 的 realm 行改显示中文状态并提示 `/realm`；
`/memory` `/skills` `/todo` `/subagent` 在停用态给「`/realm enable` 恢复」而不是「未启用」。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_realm_toggle.py`（4） | 停用后工具 / prompt 消失、启用后恢复（复用实例、`RealmContext` 重建、epoch +1/+2）；幂等（已在运行 / 已关着都不重建）；未装配抛 `RealmNotFoundError`；启动失败降级为 `failed` 且不抛 |
| `packages/liorium-agent/tests/test_realm_manager.py`（+3） | `set_enabled()` 只动目标 realm（装配顺序不变、工具只留运行中的）；`context_cadence()` 读 `params` 覆盖，启动前后一致 |
| `packages/liorium-cli/tests/test_cli_agent.py`（+4） | `/realm` 列表与 `/realm todo` 详情；`disable` 后工具真的被撤下、`/todo` 提示恢复、`enable` 后恢复；停用 `subagents` 时事件监控断开、启用后重新订阅；`--no-todo` 给出「被 --no-todo 关掉」、未知 id 与用法错误；全部关闭时给「没有装配任何 realm」 |

## 2026-11-08 变更（四）：删除 Host 机制 + `run(priority, flags)`

**删除**：`liorium/agent/host.py`（`HostPort` / `NullHost` / `SimpleHost` / `SharedHost` /
`RecordingHost` / `SpecProvider` / `EventSink`）、`LioriumAgent(host=...)`、`CliHost`、
`HOST_EVENT_TYPES`（`pending` / `streaming` / `interrupted` / `error`）、`TurnRequest.priority`
（死字段）、`apply(internal=...)`（唯一调用方 `spec_provider` 已删）、`spec_provider()` 拉模式
reconcile（`_reconcile_from_host` → `_reconcile_realms`）。

**新增**：`liorium.core.errors.TurnDropped`；`liorium.agent.FLAG`（`INTERRUPTING` / `EXCLUSIVE` /
`DROPPABLE`，位值冻结）+ `DEFAULT_FLAGS`；`run(text, *, ..., priority: int | None = None,
flags: FLAG = FLAG.NONE)`；`TurnScheduler.acquire(priority, flags, on_preempt)` 与
`QUEUE_MAX = 50`；`_TurnEmitter.unbind()` 条件解绑；`status().queue_depth` 改为实时读调度器。

**不变量**：`run()` 的生成器是唯一事件出口、只产出这一条消息的事件；抢占只取消旧回合并排队，
执行位在旧回合 `release()` 之后才转移（不并发）；同优先级不抢占（严格大于）。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_scheduler.py`（+6） | 标志位值冻结；抢占回调只发一次且不抢 `EXCLUSIVE`；同档/更低档不抢；`DROPPABLE` 拥塞抛 `TurnDropped` 且不入队；执行位空闲时不丢 |
| `packages/liorium-agent/tests/test_turn_flags.py`（6，新建） | 并发回合事件不串（`turn_id` + 正文各自独立）；排队顺序按 priority；`INTERRUPTING` 抢占后旧回合抛 `TurnInterrupted(reason="preempted")` 且工具真被取消；`EXCLUSIVE` 不被抢；同档不抢；`DROPPABLE` 抛 `TurnDropped` |
| `tests/test_event_contract.py`（1 增改） | 9 种 Agent 事件冻结；`pending` / `streaming` / `interrupted` / `error` 现在构造即 `ValueError` |
| 删除的用例 | `test_host.py`（6）、`test_reconcile.py`（3，`spec_provider`）、`test_subagent_realm.py` 的宿主执行位用例（1）、`test_multi_agent_example.py` 的 `SemaphoreHost` 用例（1）；`test_types.py` 的 `ALL_EVENT_TYPES` 参数化从 13 → 9 |

## 2026-11-08 变更（五）：核心工具收进 `core` realm + realm 实例作用域

**新增包** `liorium-core-tool-realm`（模块 `liorium.core_tool_realm`，第 12 个包）：
`CoreToolRealm` 把 `liorium-core-tool` 的六个工具装成 `RealmSpec(id="core")`。
两个包都保留：工具实现不动，只加 realm 壳。

**破坏性变更**：`LioriumAgent` 不再自动装配六个核心工具（删 `_build_core_tools`，
`liorium-agent` 去掉对 `liorium-core-tool` 的依赖）；`tools` 只来自 `realms.tools()`，
零工具合法（只记 INFO）。`AgentSpec.toolsets["default"]` 保留，改由 `CoreToolRealm` 读取
（`RealmContext` 新增 `agent_spec` / `workspace_root` / `artifacts`）。

**realm 实例作用域**：`BaseRealm.instance_scope`（`"agent"` / `"tree"`）。树级
（memory / skills / mcp / subagents）整棵树一份实例，子 Agent 只借 `tools()`；
子代理继承父的 realm 声明（`subagents` 除外）。不变式：`agent` 级 realm 返回挂载点 ⇒
启动期 `RealmError`。见 [realm-scope.md](realm-scope.md)。

| 证据 | 说明 |
|---|---|
| `packages/liorium-core-tool-realm/tests/test_core_tool_realm.py`（18，新建） | 默认六工具；`params["tools"]` / `toolsets["default"]` 子集与优先级；空子集；未知名字 `ValueError`；非法类型 / `max_tool_output_chars` 报错；`stop` 撤工具；`read` 走 `workspace_root`；缺 `artifacts` 报错；构造器注入；prompt 段只提已装工具 |
| `packages/liorium-agent/tests/test_realm_scope.py`（8，新建） | `agent` 级 + 挂载 ⇒ 启动 `failed` 且错误含 `instance_scope`；非法取值同；子 Agent 借用树级实例（同一实例 / 只 start 一次 / 工具可见 / 挂载一次 / `status().shared` / 不重复 prompt 段 / 不消费周期上下文）；借用 realm 拒绝 `stop`/`restart`/`set_enabled`；关闭子 Agent 不停树级实例；`agent` 级状态父子隔离；子 Agent `core` 用自己的 `toolsets`；真实子代理链路继承 `tree-x/core/todo`、排除 `subagents` |
| `packages/liorium-agent/tests/test_subagent_realm.py` | 子代理仍能 `read` / `find`（`newSubagent(toolsets=...)` 生效） |
| `packages/liorium-cli/tests/test_cli_agent.py`（+3） | 默认装 `core`（工具不变）；`--no-core-tools` ⇒ 零工具；`--tools read,find` ⇒ 子集；`--tools` + `--no-core-tools` 报配置错误 |
| `tests/test_architecture.py` | 新包允许面 `{core, vfs, core_tool, realm_base}`；`liorium-agent` 允许面去掉 `liorium.core_tool`（依赖真断了） |
| 改写的用例 | `tests/test_agent.py` / `test_event_contract.py` / `test_v11_integration.py` / `test_multi_agent_example.py` 的测试 Agent 显式装配 `core`；`test_realm_manager.py` 的 `FakeRealm` 加 `scope`（有挂载即 `tree`） |

## 最近一次全量

```
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1201 passed, 3 skipped
```

3 个 skip 与 v1.0 相同：2 个真模型用例（未设 `OPENCODE_GO_KEY`）+ 1 个 Pillow 缩放用例。

## 2026-11-12 变更（六）：事件流原生

按定稿 `__dev__/Liorium Event 事件流原生 定稿.md` 把"调用方驱动的回合生成器"换成
"自持 worker + 入站队列 + 只读事件流"。上面 §「2026-11-08 变更（四）」与「（五）」是历史记录，
本文不动它们；本节的删除项正是那两节里新增的调度面。

**删除**：`LioriumAgent.run()`（调用方 `async for` 驱动回合的入口）、`priority`（构造参数 /
`run(priority=...)` / 子代理的 `parent.priority - 1`）、`FLAG.INTERRUPTING`、
`FLAG.DROPPABLE`、`DEFAULT_FLAGS`、`TurnDropped`、`TurnScheduler` 的优先级堆与抢占判定
（`acquire(priority, flags, on_preempt)` / `on_preempt` 回调）。**不留兼容层。**

**新增**：`LioriumAgent.push()`（同步、`id` 必填、`bucket` / `flags` / `timeout` / `context`）、
`stream()`（同步返回订阅对象、可多个、不回放、`ids` 过滤、每订阅者无界队列）、`inject()` 改为
同步底层通道；`runtime/inbox.py`（`Bucket` / `Flag` / `Entry` / `Inbox` / `BatchWindow` /
`QUEUE_MAX=50` / 同 id 原位替换 / 惰性 `timeout`）、`runtime/hub.py`（`EventHub` /
`Subscription` / `put_nowait` 扇出 / `close()`）、`runtime/worker.py`（`TurnWorker`：常驻单执行
位、终态帧补齐、`IMMEDIATE` 抢占、维护插队、合批冲刷）、`runtime/scheduler.py` 退化为
`ExecSlot`（单执行位 + `exclusive` + 维护队列）。事件类型从 9 → **13**：新增
`round_aborted`（非终态）、`interrupted`、`error`、`input`；`start` 增 `origin` / `inputs`。
`LioriumAgent(batch_window=...)` 不进 `AgentSpec`（不参与指纹）；
`status()` 的 `queue_depth` 改为 Inbox 深度，新增 `pending_by_bucket`。

**不变量**：每个回合恰好一条终态帧（`done` / `interrupted` / `error`）且是该回合最后一条；
`IMMEDIATE` 永不结束回合（只切模型轮 + 补 `round_aborted`，`turn_id` 不变）；工具执行中不打断
（等整组工具跑完，在安全点注入）；`close()` 丢弃 pending 不发帧、`interrupt()` 保留队列；
发布者永不等待消费者（慢订阅者只影响它自己）；没人订阅时回合照常跑。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_event_hub.py` | 多订阅者各收全量；不回放；慢订阅者不阻塞；异常隔离；`close()` 结束订阅 |
| `packages/liorium-agent/tests/test_inbox.py` | 桶映射与 `opens_turn`；同 id 原位替换与合批锚点继承；`timeout` 过期（patch monotonic）不连坐；`DROPPABLE` 阈值；`AUTO` 与 `IMMEDIATE` 互斥；四旋钮合批 |
| `packages/liorium-agent/tests/test_worker.py` | FIFO；`IMMEDIATE` 四相位（含"工具中不打断"与"已发 delta 保留前缀"）；`EXCLUSIVE` 不被抢；维护插队；`reset_dialog` 清队 + 逐条 `dropped`；终态帧恰好一次；`close()` 发 `interrupted(close)` |
| `packages/liorium-agent/tests/test_buckets.py` | P3 起取代 `test_scheduler.py` / `test_turn_flags.py`：Agent 级 bucket / flags 行为与终态帧 |
| `tests/test_event_contract.py` | 14 种事件类型金样；`round_aborted` / `interrupted` / `error` / `input` / `realm` 字段冻结 |

文档：新增 [event-stream.md](event-stream.md)（权威）；[scheduler.md](scheduler.md) 改为
"单执行位 + bucket 队列"；`API.md` / `ARCHITECTURE.md` / `GUIDE.md` / `EXTENDING.md` /
`README.md` / `INTERFACES.md` 的 `run()` / `priority` / `FLAG` 段落同步改写。

## 2026-11-12 变更（七）：工具异常也发 `tool_result`

`EventTranslator` 只翻译了 `on_tool_end`，`on_tool_error` 完全没接：工具抛异常（参数校验失败、
工具节点自身报错）时 `tool_start` 一直悬着，直到回合收尾被 `drain_unfinished()` 补成 `[已中断]`
—— 文案误导（看起来像用户取消了），位置也错（排在后续 delta 之后）。

现在 `on_tool_error` 与 `on_tool_end` 走同一条配对逻辑（`run_id` 优先，`_finished` 去重、
先到先算）：正文 `[工具执行出错] <类名>: <消息>`（`tool_error_to_text`），与 `error` 事件的
"类名: 消息" 形态一致。工具**体内**的异常本来就由 `wrap_tool_output()` 变成可读结果（走
`on_tool_end`），这条补的是"根本没跑进工具体"的那一类。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_stream_translation.py`（+4） | `on_tool_error` 补对 `tool_result` 且收尾不再补 `[已中断]`；error 事件缺 `name` 时回落 start 的工具名；`end` 先到则忽略后到的 `error`；`tool_error_to_text` 四种入参 |
| `tests/test_agent.py`（+1） | 真回合里给 `read` 传错参数：`tool_result` 是 `[工具执行出错] ValidationError…`，且排在 delta 之前 |

## 2026-11-13 变更（八）：`BATCHED` 到期开回合

变更（六）里 `BATCHED` 被定义成"只做 context、永不开回合"：窗口到期后把内容注入
`ContextChannel`，等下一个回合开始时搭车。这把参照实现（Cortico）的两个档位杂交了 ——
它的 `debounce` 是"到期即投递、投递即开回合"，"攒着但永不主动说话"是 `piggyback`，而
`piggyback` 根本不参与合批。结果是：只要没有别的条目来开回合，攒下来的批次**永远等不到**
（`_flush_batch()` 发的那条 `input.delivered` 还带 `turn_id=None`，没有归属回合）。

现在窗口到期后按 `AUTO` 处理（**只加开回合，不加中途注入**）：

| 时刻 | 行为 |
|---|---|
| 窗口到期、没有回合在跑 | **开一个新回合**：整批各条 `text` 按队列序换行拼接成一条普通 user 消息，各自的 `context` 与搭车的 `ENVIRONMENT` 并入同一条 |
| 窗口到期、有回合在跑 | 留在 Inbox 排队，当前回合结束、且没有别的开回合条目时再开 |
| 窗口到期、已有 `MAJOR` / `IMMEDIATE` 在排队 | 不单独开回合；整批按形态①并入那一条开启的回合（一个批次不产生两个回合） |
| 整批只有 `ENVIRONMENT` | 永不开回合（`batch_due()` 要求至少一条 `BATCHED`） |

**代码**：`Bucket.opens_turn` 改为 `self is not Bucket.ENVIRONMENT`；`Entry` 增 `input_ids`
（批回合一次代表整批）；`Inbox.take_due_batch()` 取整批；`TurnWorker._take_batch_turn()` 合成
批回合条目，`_id_frames()` 按 `start.inputs` 逐条补 `input.delivered`；**删除** `_flush_batch()`
（职责被 `_take_batch_turn()` 完全取代）。**行为变化**：`input.delivered` 现在恒带本回合
`turn_id`（不再有冲刷时的 `turn_id=None`）；`status().pending_by_bucket["BATCHED"]` 是排队期
唯一可见性（没有"自动开口次数"计数器）。

**不做**（与参照实现一致）：回合中途注入（`WakeBus` 的批次只在主循环顶部被取走，回合内能插
进来的只有 `preempt` / `interrupt`，那是"整批投递 + 取消当前轮"）；`ENVIRONMENT` 开回合；
"自动开口"的速率上限 / 闸门原语（Cortico 的 `DeliveryGate` 只记录在案）。已知风险：把
`quiet_gap` 调得很小等于"每次窗口到期一次模型调用"（默认 2.5 s 下最坏约 24 回合/分钟），
旋钮仍在调用方手里。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_inbox.py`（+2，改 1） | `BATCHED.opens_turn is True`；`take_due_batch()` 未到期返回 `[]` 且队列不动、到期返回整批（`BATCHED` + `ENVIRONMENT`，队列序、`MAJOR` 不受影响）；只有 `ENVIRONMENT` 时恒 `[]` |
| `packages/liorium-agent/tests/test_worker.py`（+2，改 1） | `test_batched_opens_turn_when_idle`：一批恰好一个回合、`start.inputs == [b1, b2, e1]`、正文换行拼接、`ENVIRONMENT` 进同一条 user 消息、`delivered` 帧带本回合 `turn_id`；`test_batched_waits_for_running_turn`：回合在跑时窗口到期不插进去，`done` 之后才出现第二个 `start`；原"永远不开回合"用例改为"窗口内不开 + 并入 `MAJOR` 回合" |
| `packages/liorium-agent/tests/test_buckets.py`（+2，改 2） | `test_all_but_environment_open_turns`；`ENVIRONMENT` 永不开回合与 `BATCHED` 窗口未到期不开回合拆成两条；`test_batched_turn_inputs_cover_whole_batch`：只订阅整批里第二条也能收到整个回合（`start.inputs` 契约） |

文档：`event-stream.md` §4.1 / §4.3 / §5.1 / §7 / 附录 A / 附录 B，`scheduler.md` 开头，
`API.md` 的 `Bucket` 代码块与语义表，`GUIDE.md` / `README.md` / `EXTENDING.md` /
`packages/liorium-agent/README.md` 中"`BATCHED` 不开回合"的段落。

全量：`.\.venv\Scripts\python.exe -m pytest -q -m "not live"` → **1332 passed, 3 skipped,
3 deselected**（变更（六）基线 1326 → +6）。

## 2026-11-13 变更（九）：`ENVIRONMENT` 回合中途注入

变更（六）/（八）之后，`ENVIRONMENT` 只在回合开始时搭车：回合进行中 push 的环境快照（观众数、
当前页面）要等本回合结束、下一个回合开始才被模型看见 —— 一个长回合（长工具链）里可能迟到
几十秒。现在给它一条**节流的中途通道**，取值逻辑一条不动（永不开回合、不刷新合批计时器、
不影响 `next_wakeup()`）：

| 时刻 | 行为 |
|---|---|
| 回合开始 | 无条件取走全部 `ENVIRONMENT`，并入 user 消息（形态①）；**不受节流**，但会重置计时器 |
| 批回合到期 | 随整批一起取走（现有行为，同样重置计时器） |
| 回合进行中（安全点） | 距上次投递满 `env_interval`（默认 30 s）⇒ 取走全部 `ENVIRONMENT`，物化成**独立** `<context>` user 消息（形态②） |
| 未满间隔 | 返回 `[]`，队列一格不动，等下一次安全点或回合结束兜底 |
| 空闲（没有回合在跑） | 不投递、不唤醒：没有定时器，`next_wakeup()` 与 `_idle()` 都不变 |

**实现**：`Inbox(env_interval=30.0)` + `_env_at`（按"上一次**投递**"计时，不是"上一次 push"；
首次投递立即可投；`<= 0` ⇒ 每次安全点都投）；`take_environment()` 未到期返回 `[]`；
三条投递路径统一走 `_take_context()`，只要真的投递过 `ENVIRONMENT` 就重置计时器。
`ContextMergeMiddleware` 增加 `env_context` 回调，在 `before_model` 里与 `inject()` 的 pending
合流成**一条**消息；`TurnWorker.pull_env_context()` 先扫过期（`input.expired` 照常发）再取用，
避免把过期快照当新鲜数据注入。`LioriumAgent(env_interval=...)` 透传，**不进 `AgentSpec`**。

不引入 provider / 快照函数 / 后台任务：内容仍只由 `push(bucket=ENVIRONMENT)` 提供；每个 Agent
自带 `Inbox` ⇒ 子代理自动同样生效、各自独立计时，构造点零改动。

**行为变化**：回合进行中 push 的 `ENVIRONMENT` 不再一律留给下一个回合（默认 30 s 内至多一次，
首次立即）；`input.expired` 可能落在回合内（环境取用前会先扫过期），不再固定排在回合之后。

**已知取舍**：注入不增加模型调用次数，但每条都是一条新 user 消息（token 成本，长回合里可达
数条）；真实延迟上界是"`env_interval` + 到下一个安全点的距离"（模型正在出长文本或慢工具时
要等它结束），不是 30 秒硬实时。

| 证据 | 说明 |
|---|---|
| `packages/liorium-agent/tests/test_inbox.py`（+3） | `take_environment()` 未到期返回 `[]` 且队列不动、到期取走全部 `ENVIRONMENT`（不碰 `BATCHED` / `MAJOR`）；`pop_context_entries()` / `take_due_batch()` 投递过环境后计时器重置（`env_due()` 变假）；`env_interval=0` 每次都能投 |
| `packages/liorium-agent/tests/test_worker.py`（+2，改 1） | `test_environment_injected_mid_turn`：回合中途 push ⇒ 仍只有一个 `start`（`inputs` 不含环境 id）、下一秒模型调用前多一条独立 `<context>` 消息、`input.delivered` 带本回合 `turn_id`、`done` 恰好一条；`test_environment_throttled_mid_turn`：`env_interval=100` 时中途不注入、留队列，下一回合开始时按形态①搭车（`start.inputs == ["m1","e1"]`）；`test_expired_entry_is_never_materialized` 改为在回合帧流里找 `input.expired` |
| `packages/liorium-agent/tests/test_buckets.py`（+1，改 2） | `test_environment_injected_mid_turn_keeps_single_turn`（不新开回合、`pending_by_bucket` 清零）；过期用例同上改法；`test_status_queue_depth_and_bucket_breakdown` 的 `start.inputs` 由 `["m","b","e"]` 改为 `["m","b"]`（环境已在 `A` 回合注入） |

文档：`event-stream.md` §4.1（新增一段投递时机）/ §4.3 / §5.1 / 附录 A，`API.md` 的
`LioriumAgent` 参数表与 `Bucket` / `BatchWindow` 代码块，`GUIDE.md`、`README.md`、
`EXTENDING.md`、`scheduler.md`、`packages/liorium-agent/README.md`，以及
`__dev__/Liorium Event 事件流原生 定稿.md` §4.1 的「2026-11-13 修订（二）」标注。

全量：`.\.venv\Scripts\python.exe -m pytest -q -m "not live"` → **1338 passed, 3 skipped,
3 deselected**（变更（八）基线 1332 → +6）。

## 2026-11-14 变更（十）：realm 主动能力（`realm` 帧 / 推送 / 中断 / 只读钩子）

变更（九）为止，realm 只能「被框架拉」：装配时读四件套、按 `context_every` 节奏被问
`context_items()`、用 `ctx.inject()` 往下一个安全点排一条 Context。宿主 UI 想实时知道
"用户刚改了一条待办"，只能等 50 回合的周期上下文，或者干脆等 `done`。

这次给 realm 两个**写**出口 + 一圈**只看不改**的观测面（需求原文：
「给予 realm 推送事件等更多能力」「还需要一些更加丰富的，onion 类型的 hook」
「realm 不允许改写模型请求」「只读为什么要 `nxt()`，去掉」）：

| 入口 | 语义 | 没有宿主时 |
|---|---|---|
| `ctx.emit(name, data=None)` | 往事件流推一帧**非终态** `realm`；不开回合、不落 checkpoint、不回放 | 静默 no-op（广播没人听） |
| `ctx.push(text, *, bucket="environment", flags=None)` | 复用 `Agent.push`，自动 id `realm:<id>:<n>`；默认 `environment` 永不开回合，显式 `major` 才开 | `RealmError` |
| `ctx.interrupt(reason="realm") -> bool` | 同步叫停当前回合，事件流以 `interrupted(reason="realm")` 收尾 | `RealmError` |
| `ctx.turn` | 当前 `TurnInfo` 或 `None` | `None` |

六个只读钩子（`BaseRealm` 上的 `async` 空实现，`overrides_hook()` 发现覆盖）：
`on_turn_start(turn)` / `on_turn_end(turn, TurnOutcome)` / `on_model_start(ModelCall)` /
`on_model_end(ModelCall, HookOutcome)` / `on_tool_start(ToolCall)` / `on_tool_end(ToolCall, HookOutcome)`。
进入按声明序、退出**反序**（`on_turn_end` 同样反序）；`on_turn_end` 在**终态帧之前**跑，
取消路径（`interrupt` / `close`）跳过它；钩子抛异常只记日志，**不改变回合**。

**为什么钩子没有 `nxt()`**：realm 永久不能改写模型请求/响应。这不是约定而是机制 ——
`ModelCall.messages` 是不可变 `tuple`、`ToolCall.args` 是 `MappingProxyType`，拿不到
`ModelRequest`、没有返回值通道、没有短路/拒绝工具的入口。原型实测过四种 `awrap_model_call`
改法（尾部追加 Human、脱敏最后一条、追加 system、删历史 ToolMessage）：模型都收到了新内容，
但 **checkpoint 里仍是原文**，历史与事实分叉；拆工具组还会被 `tool_repair` 补占位变成
"结果缺失"让模型自己猜。要影响未来就走正道：`ctx.inject()` / `ctx.push()` / 工具里 `apply()` /
宿主 `reset_dialog()`；要真改请求是宿主自己的 `Realm.middleware()`（只能挂外层）。

**已知时序**：`realm` 帧只保证**非终态 + 在终态帧之前**；它与 `tool_start` / `tool_result`
的相对顺序**不保证** —— `ctx.emit` 直连 `hub.put_nowait`，工具帧来自事件翻译器，两条独立
投递路径。手测里工具内 emit 的 `todo.changed` 排在 `tool_start` **之前**。宿主不要用帧顺序
推断业务落库，事件自带的 `counts` / `ids` 才是事实。

**实现**：`core` 的 `AGENT_EVENT_TYPES` 尾部追加 `"realm"`（13 → 14）+ `ev_realm()`；
`realm_base` 的 `RealmHost` Protocol + `RealmContext` 四个成员 + 四个数据类 + 六个空钩子；
`agent/realm/host.py::RealmHostPort`（emit → `hub.publish`、push → `Agent.push`、
interrupt → `worker.interrupt`、turn → `worker.current_turn_info`）；
`agent/realm/hooks.py::RealmHooksMiddleware`（每 realm 一个适配中间件，`name` 唯一）；
`worker.py` 在 `start` 帧后 fire `on_turn_start`、终态帧前 fire `on_turn_end`，整段包在
`current_turn(turn_id)` 里；`RealmManager(host_factory=...)` 注入 host；
`build_chain(realm_middlewares=...)` 只给覆写了模型/工具钩子的 realm 追加适配器。
`todo_realm` 每次改动后推 `todo.changed`（`counts` + `ids`），驱动宿主 UI 实时刷新。

| 证据 | 说明 |
|---|---|
| `packages/liorium-core/tests/test_types.py`（改 2） | 金样 13 → 14（`"realm"` 在尾部）；`test_ev_realm_shape` |
| `tests/test_event_contract.py`（改 1） | 冻结集合追加 `"realm"` |
| `packages/liorium-agent/tests/test_realm_capabilities.py`（新增 14） | 回合内 emit 带 `turn_id` 且在 `done` 之前；空闲 emit 无 `turn_id`、无 `start`；无宿主时 emit 静默 / push 与 interrupt 抛 `RealmError`；push `environment` 不开回合（下一回合 `start.inputs` 带上自动 id）；push `major` 开新回合；非法 bucket 抛错；工具里 `ctx.interrupt()` ⇒ `interrupted(reason="realm")`；`ctx.turn` 回合内/外；两 realm 钩子顺序（进入 A→B、退出 B→A）；工具失败 `on_tool_end(ok=False)`；钩子抛异常非致命（回合仍 `done`）；只读快照（`tuple` / `MappingProxyType`，改则 `TypeError`）；未覆写钩子的 realm 不装中间件；`on_turn_end` 的 `realm` 帧排在 `done` 前 |
| `packages/liorium-todo-realm/tests/test_todo_realm.py`（+1） | 每次 add/update/remove 都发一帧 `todo.changed`，字段与 `counts()` 一致 |
| `packages/liorium-agent/tests/test_realm_base.py` / `test_realm_context.py` | 未改一行，仍全绿（新增成员全是可选路径） |

文档：`event-stream.md`（§6 `ids` 说明、§7 → 14 种 + `realm` 行、边界段落、测试表）、
`API.md`（事件表 + `ev_realm` + `RealmContext` 成员 + 六个钩子 + `RealmHost`）、
`EXTENDING.md` §1.5、`GUIDE.md`、`ARCHITECTURE.md` §2/§3/§4.4、`INTERFACES.md`、
`README.md`、`scheduler.md`、`docs/README.md`、`packages/liorium-agent/README.md`，
以及 `__dev__/Liorium Event 事件流原生 定稿.md` §7 的「2026-11-14 修订（三）」。

全量：`.\.venv\Scripts\python.exe -m pytest -q -m "not live"` → **1356 passed, 3 skipped,
3 deselected**（变更（九）基线 1339 → +17）。

## 2026-11-14 变更（十一）：宿主 → realm 消息（`send_to_realm` / `on_message`）

变更（十）解决的是「realm → 宿主」。这一条补反向：外部主程序（GUI / webhook / cron）把一条
**业务命令或领域事件**直接交给某个 realm，不经过模型、不等回合，立刻拿结果。需求原文：
*"外部主程序通过公共API向Realm发送消息"*。

```python
reply = await agent.send_to_realm("todo", "todo.add", {"text": "写 demo"})
# → "已添加 t1 [pending] 写 demo"
```

| 面 | 形状 |
|---|---|
| `RealmMessage`（`liorium.realm_base`） | `realm_id` / `name` / `data`（**副本**）/ `origin`；冻结 dataclass，空 `name` 抛 `RealmError` |
| `BaseRealm.on_message(message)` | 落点；可 sync 可 async；返回值原样回给宿主；**默认实现抛 `RealmError`**（= 不接受消息，不静默丢） |
| `LioriumAgent.send_to_realm(...)` | 唯一宿主入口：`realm_id, name, data=None, *, origin="host", timeout=None` |
| `RealmManager.send(...)` | 实现层：状态检查 → `record.send_lock` → `CURRENT_REALM_MESSAGE` → `on_message()` |

**语义**：立即执行（在调用方 task 里，与回合并发；不进维护队列）、等返回值、同 realm 串行
（`RealmRecord.send_lock`，`start`/`stop` 共用同一把锁 ⇒ 停用会等在手消息；树级 record 就是根的
那条 ⇒ 全树共用）、状态不对就抛（未装配 `RealmNotFoundError`；停用/失败 `RealmError`；
agent 未 start / 已 close `TurnError`；handler 异常**原样上抛**；`timeout` 到期 `RealmError`）、
树级 realm 的消息由根实例处理（`ctx.emit`/`ctx.push` 也作用在根 Agent）、**不进事件流**（想留痕
就在 handler 里 `ctx.emit`）、完全不影响模型请求。

**防互锁**：handler 执行期间 `CURRENT_REALM_MESSAGE` 有值，`_assert_maintenance_allowed()` 与
`close()` 直接抛 `TurnError` —— 否则 handler 持着 realm 锁去等维护队列，维护队列又等这把锁。
同 realm 递归 `send_to_realm` 仍会自锁（文档禁止）。

**示例实现**：`TodoRealm.on_message` 认 `todo.add` / `todo.update` / `todo.remove` /
`todo.list`（按名字最后一段分发，复用既有方法；未知名字抛 `RealmError`）。

| 证据 | 说明 |
|---|---|
| `packages/liorium-realm-base/tests/test_realm_base.py`（+4） | `RealmMessage` 冻结 + `data` 副本 + `name` 去空白；空 `name` 抛错；默认 `on_message` 抛"不接受消息"；子类实现后返回值 |
| `packages/liorium-agent/tests/test_realm_messages.py`（新增 17） | 返回值（async / sync）；字段与 `data` 副本；空 name；未装配 `RealmNotFoundError`；无 handler / 停用 / 失败 `RealmError`；未 start / 已 close `TurnError`；并发三条串行（`max_active == 1`）；**回合进行中发送立即返回**（工具卡着、无新 `start`、`done` 仍恰好一条）；`stop` 等在手消息；handler 异常原样上抛；`timeout` ⇒ `RealmError`；handler 里调维护操作 ⇒ `TurnError`；handler 里 `ctx.emit` ⇒ 事件流出现 `realm` 帧（`turn_id is None`）；树级 realm 命中根实例、agent 级 realm 命中子 Agent 自己的实例 |
| `packages/liorium-todo-realm/tests/test_todo_realm.py`（+3） | `todo.add/update/remove` 经 `on_message` 各发一帧 `todo.changed` 且返回与工具一致的文本；`todo.list` 与未知名字；业务错误仍是 `错误：…` 文本（与工具一致） |
| `packages/liorium-agent/tests/{test_realm_scope,test_realm_toggle,test_realm_manager,test_worker}.py` | 未改一行仍全绿（`start`/`stop` 加锁不改变原有语义） |

文档：`API.md`（`send_to_realm` 成员行、`RealmMessage` + `on_message` 小节、`RealmManager.send`、
§10.2）、`EXTENDING.md` §1.6（示例 + 规则 + 选型表）、`ARCHITECTURE.md` §4.5 + 模块地图、
`INTERFACES.md`（第七拍契约追加）、`GUIDE.md`、`docs/README.md`（索引 + 术语表）、
根 `README.md`、`packages/{liorium-agent,liorium-realm-base,liorium-todo-realm}/README.md`，
以及 `__dev__/Liorium Realm 接收宿主消息 实施计划.md`。

全量：`.\.venv\Scripts\python.exe -m pytest -q -m "not live"` → **1380 passed, 3 skipped,
3 deselected**（变更（十）基线 1356 → +24）。

---

## 2026-11-14 变更（十二）：Agent Profile（第 13 个包 + realm 自组装）

计划：[`docs/plans/2026-11-14-agent-profile.md`](../plans/2026-11-14-agent-profile.md)（唯一依据）。
这一条把"Agent 怎么装配"从命令行 flag / `config.json` 搬进一份可分享的 `profile.json`，
并顺手把两个生命周期问题收干净：realm 的配置由 realm 自己声明（`Config`），模型由造它的人关
（`ChatModelPool`）。

### 交付面

| # | 交付项 | 状态 | 证据 |
|---|---|---|---|
| 1 | `RealmSpec.config` + `RealmContext.config` / `ctx.model`；`AgentSpec` 删 `Profile` / `with_profile` | ✅ | `packages/liorium-agent/tests/test_ctx_model.py`（4：独立 ctx 无 aux 模型 / 每次访问都新建 / manager 注入 config+工厂 / Agent 用 compact 工厂且 apply 后刷新）、`test_model_close.py`（4：共享工厂只关一次 / 不同工厂各关一次 / 裸函数工厂也能关 / `aclose` 异常不阻塞 `close()`）；旧 `test_profile.py` 删除 |
| 2 | `ChatModelPool`（`(label, intensity, purpose)` 缓存、`chat`/`compact` 绑定视图、幂等 `aclose`） | ✅ | `packages/liorium-provider-manager/tests/test_pool.py`（7：purpose/intensity、缓存键、api_format、缺 key、幂等 aclose、关闭失败只记日志） |
| 3 | `MemoryRealm.Config = MemoryConfig` 自组装（embed / vectors / extraction / `ctx.model`） | ✅ | `packages/liorium-memory/tests/test_memory_config.py`（7：默认值、`dir` 布局、embed 构建与关闭、缺 `[vector]` 降级、抽取器用 `ctx.model`、无对象时关闭抽取、`dir` 与 `db_path` 冲突） |
| 4 | 新包 `liorium-profile`：`AgentProfile.load/from_dict/build`、`ProfileBuild` | ✅ | `packages/liorium-profile/tests/`（35：schema / loader（`${VAR}`、`~`、`$$`、source 四种写法、mtime 缓存、`sys.path` 不被污染、Config 校验）/ build（spec 逐项、指纹、registry 覆盖、禁覆盖端口）/ lifecycle（pool 关闭恰好一次、`create_agent` TypeError）） |
| 5 | CLI：profile 取代 `config.json` 装配；旧配置一次性迁移（`.bak`）；`setup profile` 子命令 | ✅ | `packages/liorium-cli/tests/test_cli_profile.py`（6：模板 / 路径优先级 / 迁移映射 / UI-only / realm 读写）、`test_cli_setup.py`（35：`setup profile show|path|init|validate|edit|migrate`、自动迁移、冷启动）、`test_cli_agent.py`（36：profile 驱动装配、flag 只覆盖本次、`/reload` 与身份变化报错、`/model`、`/thinking`、realm 缺失提示） |
| 6 | 发布形态：13 个模块 | ✅ | `tests/test_distribution.py`（13）、`tests/test_architecture.py`（依赖白名单 + 发现集合）、`scripts/check_wheel.py`（13） |
| 7 | 文档同步 | ✅ | `ARCHITECTURE.md`（模块地图 + §4.0 realm 自组装）、`API.md`（§4.1 `RealmSpec.config`、§4.6 `ctx.config`/`ctx.model`、§9.5 `ChatModelPool`、§12 `liorium.profile`）、`EXTENDING.md` §1.7、`MEMORY.md` §8 / §A、`GUIDE.md`、`API.md`/`README.md` 的 12→13、各包 README |

### 验收用例对照（计划 §8）

- schema / loader：`version` 非 1、未知字段、realm id 重复、四种 `source`、`Config` 校验、
  `${VAR}` 未定义、`$$` 转义、内置 `subagents` 可省 source、`requires` 越界、`id` 缺省取文件名
  —— `test_profile_schema.py` / `test_profile_loader.py` / `test_profile_sources.py`；
- build / 生命周期：spec 逐项等价、指纹稳定、`model` 优先级、`context_window: 0` 不被覆盖、
  `create_agent` 禁覆盖、迁移得到的 profile 能直接 `load()`+`build()` —— `test_profile_build.py`、
  `test_cli_setup.py::test_run_cli_migrates_old_config`；
- CLI：无配置启动生成 `~/.liorium/profile.json` + UI-only `config.json`、旧配置迁移、
  `--profile` > cwd > home、flag 不回写 profile、`/reload` 改 realms 生效、改 `id`/`inbox` 要求重启
  —— `test_cli_setup.py`、`test_cli_agent.py`、`test_cli_profile.py`；
- 架构与发布：`test_architecture.py`、`test_distribution.py`、`scripts/check_wheel.py`。

### 全量

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 1441 passed, 3 skipped, 3 deselected
```

（变更（十一）基线 1380 → +61：新增 57 —— `liorium-profile` 35、`ChatModelPool` 7、
`MemoryConfig` 7、agent `ctx.model` / 工厂关闭 8；CLI 侧净增 13（`setup profile` 5、
自动建 profile / 坏 profile / 迁移 3，其余为改写后的等价用例）；同时删除旧的
`packages/liorium-agent/tests/test_profile.py`（9）与只测旧 flag 的用例。）

### 已知取舍（计划 §9）

明文 key 可以写进 profile（0600 + `${VAR}`；轮换 key 会因指纹变化重建 realm）、`load()` 会执行
本地代码（`--schema-only` 提供"只查结构"模式）、`ctx.model` 的花费不设上限、embedding 凭据
需要在 profile 里重复写一次（realm 看不到 provider registry）。
