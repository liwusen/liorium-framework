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
Event(type, payload)  # 事件类型见 AGENT_EVENT_TYPES（= ALL_EVENT_TYPES，只有 9 种）

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

# liorium.agent（调度）
FLAG: IntFlag  # NONE / INTERRUPTING / EXCLUSIVE / DROPPABLE
LioriumAgent.run(text, *, context=(), origin="user", priority=None, flags=FLAG.NONE)

# liorium.agent.realm
Realm: id / requires / state_scope / tools() / prompt_fragment() / middleware()
        / mounts() / start(ctx) / stop(ctx)
RealmContext: realm_id / vfs / state(JsonStateStore) / params / agent_id / spawn(coro) / log
              / context(ContextSink|None) / inject(text) / inject_items(items)
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

事件协议（`liorium.core.types`，前端零改动）：

| 产出方 | type |
|---|---|
| Agent（`run()` 的生成器，唯一出口） | `start(turn_id)` `delta(content)` `reasoning_delta(content)` `tool_start(tool_name,args,call_id)` `tool_result(tool_name,output,call_id)` `compact_start` `compact_delta(content)` `compact_done(ok)` `done(turn_id)` |

终止用异常：`TurnInterrupted` / `TurnDropped` / `TurnError`，**不是**事件。
2026-11-08 删掉「宿主事件」（`pending` / `streaming` / `interrupted` / `error`）：
等待期间不产出事件，中断/丢弃/失败用上面三个异常表达。

## 4. 分工（文件所有权）

| 模块 | 路径 | 负责人 |
|---|---|---|
| 基座中间件：OutputStore / wrap_tools / mm_bridge / tool_repair / server_tools | `runtime/output_store.py`、`runtime/tools.py`、`runtime/middleware/{mm_bridge,tool_repair,server_tools}.py` | S-MW |
| 压缩 | `runtime/middleware/compact.py` + `runtime/usage.py` | S-COMPACT |
| 六个核心工具 | `packages/liorium-core-tool/src/liorium/core_tool/**` | S-READ / S-TOOLS |
| 核心工具的 realm 壳（`RealmSpec(id="core")` 才装） | `packages/liorium-core-tool-realm/src/liorium/core_tool_realm/**` | S-TOOLS |
| realm 实例作用域（`instance_scope` / 树级借用 / 挂载不变式） | `packages/liorium-realm-base/src/liorium/realm_base/base.py`、`packages/liorium-agent/src/liorium/agent/realm/manager.py` | [design/realm-scope.md](realm-scope.md) |
| 回合循环 / 事件翻译 | `runtime/loop.py`、`runtime/stream.py` | S-LOOP |
| 其余 | `agent.py`、`runtime/{scheduler,prompt,checkpoint,context}.py`、`realm/manager.py` | 主 Agent |

## 5. 测试与验收

- 每个模块自带 `packages/<pkg>/tests/test_*.py`；跨包集成在 `tests/`。
- 关键回归必须覆盖（计划 §F6）：`test_compact_*`、`test_tool_call_repair`、
  `test_agent_scheduler`、`test_mm_bridge`、`test_subagent_manager`、`test_vfs_handler_ack`。
- 真模型用例打 `@pytest.mark.live`，默认跳过；环境变量：
  `OPENCODE_GO_BASE_URL=https://opencode.ai/zen/go/v1`、
  `OPENCODE_GO_KEY=...`（在 `~/.liorium/providers.json` 的 `Go` provider 里）、
  `OPENCODE_GO_MODEL=deepseek-v4.1-flash`。
