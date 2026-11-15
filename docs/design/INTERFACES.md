# Liorium 内部接口约定（实现者必读）

> 计划依据：`docs/plans/2026-11-04-liorium-agent-core.md`（唯一决策记录）。
> 本文件只说明**当前代码里的接口与分工**，与计划冲突时以计划为准。

## 1. 本地跑测试

**首选 `.venv`（与 CI 一致，依赖 langchain 1.4.7 / core 1.6.13 / langgraph 1.2.19）：**

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider <路径>
# 或
./scripts/test.ps1 -q packages/liorium-agent/tests
```

- `.venv` 已 `uv sync --offline --all-packages --all-extras` 并 editable 安装了 7 个包；
  网络恢复前不要跑不带 `--offline` 的 `uv sync`。
- 备用解释器：`.venv\Scripts\python.exe`（3.11.9，langchain 1.4.0），
  它自带 `python311._pth`，**会忽略 `PYTHONPATH`** —— 路径注入由仓库根 `conftest.py` 完成。

- Python 3.11；命名空间包：每个包只有 `src/liorium/<sub>/__init__.py`，**绝不**创建 `src/liorium/__init__.py`。
- 不新增第三方依赖（除非先改 `pyproject.toml` 并说明理由）。

## 2. 硬规则（违反即返工）

1. `liorium` 任何包不许 import 旧后端模块；不许读产品配置。
2. 不许裸 `asyncio.create_task`（只有 `liorium-realm-base` 的
   `liorium/realm_base/base.py::RealmTaskScope.spawn` 允许；2026-11-07 从 agent 搬出）。
3. 工具名冲突必须抛 `ToolNameConflictError`，不许静默跳过。
4. 不许新增 LangChain 消息类型；`Context` 只存在于独立 channel。
5. 逐字冻结的文本（`↳ ack`、`[完整输出: ...]`、`artifact://`、压缩标记、占位 ToolResult）
   必须与 `docs/design/*.md` 里引用的一致。
6. 所有 docstring/注释用中文；标识符与冻结字符串保持原样。
7. 异步优先：能 async 就 async。
8. 每个实现都要带 pytest 测试，且测试在本地命令下能跑绿。

## 3. 冻结的公共契约

```python
# liorium.core.types
VfsContent = Text(text) | Image(data: bytes, mime) | Binary(data: bytes, mime)
ContextItem(source: str, text: str, meta: dict, interrupt: bool = False)
Event(type, payload)  # 事件类型见 AGENT_EVENT_TYPES（= ALL_EVENT_TYPES，共 14 种；2026-11-14 追加 realm）

# liorium.core.uri
parse(raw) -> ParsedUri(scheme, path, selector, query, trailing_slash)
ParsedUri.vfs_path() -> "/<scheme>/<path>"

# liorium.core.ports
ArtifactStore.put/put_multimodal/get/summary/exists
ProcessRunner.run(command, *, cwd, timeout, env) -> (rc, out, err)

# liorium.agent.runtime.types
TurnRequest(agent_id, turn_id, text, context, origin)
TurnRuntime(spec, graph, checkpointer, model, tools, middleware, artifacts, vfs,
            emit, interrupt, tasks, thread_id, api_format)

# liorium.agent（入站 / 出站）
Bucket: str Enum  # IMMEDIATE / MAJOR / BATCHED / ENVIRONMENT
Flag: IntFlag     # NONE / EXCLUSIVE / DROPPABLE / AUTO
BatchWindow(quiet_gap=2.5, min_age=0.0, max_age=15.0, max_size=100)
LioriumAgent.push(text, *, id, bucket=Bucket.MAJOR, flags=Flag.NONE,
                  origin="user", timeout=None, context=())
LioriumAgent.stream(*, ids=None) -> AsyncIterator[Event]   # 同步返回订阅对象
LioriumAgent.inject(items) -> None                         # 同步；不发 input 帧
LioriumAgent.interrupt(*, reason="user") -> bool           # async
# 生命周期：每个回合恰好一条终态帧（done / interrupted / error），且是该回合最后一条

# liorium.agent.realm
Realm: id / requires / state_scope / tools() / prompt_fragment() / middleware()
        / mounts() / start(ctx) / stop(ctx) / Config（可选：pydantic BaseModel）
RealmContext: realm_id / vfs / state(JsonStateStore) / config(dict) / params(只读别名) / agent_id
              / spawn(coro) / log
              / context(ContextSink|None) / inject(text) / inject_items(items)
              / host(RealmHost|None) / emit(name, data=None) / push(text, *, bucket, flags)
              / interrupt(reason="realm") / turn(TurnInfo|None)         # 2026-11-14
              / model(Any|None)      # aux 模型；每次访问新建，归工厂关   # 2026-11-14
Realm 只读钩子（`async`、默认空实现、可 raise 但只记日志、无返回值/无 nxt）：
              on_turn_start(turn) / on_turn_end(turn, TurnOutcome)
              / on_model_start(ModelCall) / on_model_end(ModelCall, HookOutcome)
              / on_tool_start(ToolCall) / on_tool_end(ToolCall, HookOutcome)
Realm 宿主消息（2026-11-14）：RealmMessage(realm_id, name, data, origin)
              BaseRealm.on_message(message) -> Any      # 默认抛 RealmError（= 不接受消息）
              LioriumAgent.send_to_realm(realm_id, name, data=None, *,
                                         origin="host", timeout=None) -> Any   # async
              RealmManager.send(realm_id, message, *, timeout=None) -> Any     # async
RealmManager(agent_id, vfs, state_dir, factories, context_channel)
             .assemble(specs) / start_all() / stop_all()
             / tools() / prompt_fragments() / realm_middlewares() / restart(id, spec)
             / status() / attach_specs(specs) / context_for_turn(TurnInfo) -> list[ContextItem]
```

> 2026-11-07：上述 realm 契约的**实体**已搬到 `liorium-realm-base`（模块 `liorium.realm_base`）；
> `liorium.agent.realm` 保留同名 re-export，所以本节契约逐字不变。同批搬过去的还有
> `TurnInfo(index, id, origin)`（第五拍入参）与写作 API `@realm_tool` / `BaseRealm.save()` /
> `on_start` / `on_stop`；**节拍器已接线**（`RealmManager.context_for_turn()`，同日）：
> `context_every` / `context_first` / `context_origins` 声明节奏，返回非空才注入、
> 返回空不消费周期，相位写在 realm 自己的 state 保留键 `__context_due__` 里。
>
> 2026-11-14：`RealmSpec.params` 改名为 `RealmSpec.config`（`params` 保留只读别名），
> realm 可以用类属性 `Config`（pydantic `BaseModel`）声明自己的配置；`AgentProfile.load()`
> 用它校验并补默认值。`ctx.model` 是宿主的 aux 模型（`compact_model_factory`，不思考），
> **每次访问都新建**，所有权在工厂（`ChatModelPool`），realm 不缓存、不关闭；
> 没有工厂时为 `None`，realm 必须降级。`LioriumAgent.close()` 会调两个工厂的 `aclose()`
> （幂等、同一个对象只关一次）。`Profile` / `AgentSpec.with_profile` 已删除，
> 装配改由 `liorium-profile` 的 `profile.json` 承担（见 [plans/2026-11-14-agent-profile.md](../plans/2026-11-14-agent-profile.md)）。
>
> 2026-11-14：`RealmContext` 增加 `host` / `emit` / `push` / `interrupt` / `turn`（主动出口），
> `BaseRealm` 增加六个只读钩子。`emit` 无宿主时静默 no-op（广播没人听），`push` / `interrupt`
> 无宿主时抛 `RealmError`（会改变 Agent 行为，装配错误必须暴露）。钩子只通知、不参与决策：
> **realm 永久不能改写模型请求/响应**（拿不到请求对象、没有 `nxt()`、没有返回通道）。
>
> 2026-11-14（同日，第七拍）：新增**宿主 → realm** 的 `RealmMessage` + `on_message()` +
> `send_to_realm()`。语义是**立即执行、等结果**（不进维护队列）：同 realm 消息由
> `RealmRecord.send_lock` 串行，`start` / `stop` 共用同一把锁；状态不是 `running` 就抛
> `RealmError`（停用/失败不静默），未装配 `RealmNotFoundError`，agent 未 start / 已 close
> `TurnError`，handler 异常原样上抛。handler 执行期间 `CURRENT_REALM_MESSAGE` 让维护操作与
> `close()` 抛 `TurnError`（防互锁）。消息**不进事件流**、不进历史、不改模型请求。

事件协议（`liorium.core.types`，前端零改动）：

| 产出方 | type |
|---|---|
| Agent（`EventHub` 扇出，`stream()` 是唯一出口，可多订阅） | `start(turn_id,origin,inputs)` `delta(content)` `reasoning_delta(content)` `tool_start(tool_name,args,call_id)` `tool_result(tool_name,output,call_id)` `compact_start` `compact_delta(content)` `compact_done(ok)` `done(turn_id)` `round_aborted(turn_id,reason)` `interrupted(turn_id,reason)` `error(turn_id,error)` `input(id,state[,turn_id])` `realm(realm,name,data[,turn_id])` |

**硬规则**：每个回合恰好一条终态帧（`done` / `interrupted` / `error`），且是该回合最后一条事件；
`round_aborted` 是非终态（`IMMEDIATE` 抢占模型轮），`input` 是入站生命周期帧，`realm` 是
realm 主动推的非终态帧（2026-11-14 追加，不开回合、不落 checkpoint）。
`done` 不带 `final_text`，`error` 不带 traceback（只进日志）。
2026-11-08 删掉「宿主事件」（`pending` / `streaming` / `interrupted` / `error`）后，终结不再靠
异常：`TurnInterrupted` / `TurnError` 由 worker 翻译成终态帧；`push()` 的生命周期错误
（未 `start()` / 已 `close()`）仍抛 `TurnError`。

## 4. 分工（文件所有权）

| 模块 | 路径 | 负责人 |
|---|---|---|
| 基座中间件：OutputStore / wrap_tools / mm_bridge / tool_repair / server_tools | `runtime/output_store.py`、`runtime/tools.py`、`runtime/middleware/{mm_bridge,tool_repair,server_tools}.py` | S-MW |
| 压缩 | `runtime/middleware/compact.py` + `runtime/usage.py` | S-COMPACT |
| 六个核心工具 | `packages/liorium-core-tool/src/liorium/core_tool/**` | S-READ / S-TOOLS |
| 核心工具的 realm 壳（`RealmSpec(id="core")` 才装） | `packages/liorium-core-tool-realm/src/liorium/core_tool_realm/**` | S-TOOLS |
| realm 实例作用域（`instance_scope` / 树级借用 / 挂载不变式） | `packages/liorium-realm-base/src/liorium/realm_base/base.py`、`packages/liorium-agent/src/liorium/agent/realm/manager.py` | [design/realm-scope.md](realm-scope.md) |
| 回合循环 / 事件翻译 | `runtime/loop.py`、`runtime/stream.py` | S-LOOP |
| 事件流原生（入站队列 / 扇出 / 常驻 worker / 单执行位） | `runtime/{inbox,hub,worker,scheduler}.py` | [design/event-stream.md](event-stream.md) |
| 其余 | `agent.py`、`runtime/{prompt,checkpoint,context}.py`、`realm/manager.py` | 主 Agent |

## 5. 测试与验收

- 每个模块自带 `packages/<pkg>/tests/test_*.py`；跨包集成在 `tests/`。
- 关键回归必须覆盖（计划 §F6）：`test_compact_*`、`test_tool_call_repair`、
  `test_event_hub`、`test_inbox`、`test_worker`、`test_buckets`、`test_mm_bridge`、
  `test_subagent_manager`、`test_vfs_handler_ack`。
- 真模型用例打 `@pytest.mark.live`，默认跳过；环境变量：
  `OPENCODE_GO_BASE_URL=https://opencode.ai/zen/go/v1`、
  `OPENCODE_GO_KEY=...`（在 `~/.liorium/providers.json` 的 `Go` provider 里）、
  `OPENCODE_GO_MODEL=deepseek-v4.1-flash`。
