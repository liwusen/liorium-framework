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
| 6 | profile 语法糖 | ✅ | `packages/liorium-agent/tests/test_profile.py`（9：合并顺序/显式声明优先/指纹变化/空 profile/重复报错） |
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
