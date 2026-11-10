# Liorium

**Liorium** 是一个可独立发布的 Agent 框架：**一个发行版 `liorium`，内含 12 个模块**、
realm 扩展机制、可热重载的运行时、流式事件出口、混合记忆、技能库、MCP 与子代理，
全部只依赖标准库与公开的 LangChain / LangGraph。

- 模块：`liorium.core` / `liorium.vfs` / `liorium.realm_base` / `liorium.core_tool` /
  `liorium.core_tool_realm` / `liorium.agent` / `liorium.memory` / `liorium.skills` /
  `liorium.mcp` / `liorium.todo_realm` / `liorium.provider` / `liorium.cli`
- 安装：`pip install "liorium[all]"`（全家桶：MCP + 向量记忆 + 三家 provider 适配 + 图片）；
  只要框架运行时用 `pip install liorium`；单独选装见下
- 仓库：<https://github.com/liwusen/liorium-framework>

架构决策记录：[`docs/plans/2026-11-04-liorium-agent-core.md`](docs/plans/2026-11-04-liorium-agent-core.md)。
实现与决策冲突时以决策记录为准；要改结论先改决策记录。

当前状态：**Phase 1（v1.0）骨架 + Phase 2（v1.1）能力包完成** —— 12 个模块的 workspace、
6 个核心工具、Agent 运行时（`run/inject/interrupt/apply`）、基座中间件链、realm 系统、
子代理、checkpoint 兼容旧库；`liorium-memory` / `liorium-skills` / `liorium-mcp` / `liorium-todo-realm`
四个 realm 能力包（挂载 + 工具 + 状态 + prompt 片段），以及 `conversation` scope、profile 语法糖、
MCP mark-dirty reconcile、realm 的**第五拍**（`context_items()` 按回合节奏注入）、多 Agent 宿主示例。`liorium-memory` 已**完整迁移**旧
`GraphStore`：FTS5 关键词 + nano-vectordb 兼容向量 + RRF 混合检索 + 实体图谱（递归 CTE）
+ LLM 抽取 + 附件/变更流/记录 + 旧 JSON 迁移，7 个模型工具（详见
[`docs/MEMORY.md`](docs/MEMORY.md)）。
逐条验收证据见 [`docs/design/ACCEPTANCE.md`](docs/design/ACCEPTANCE.md)，模块地图见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 模块（`liorium.*`，12 个，同一个发行版）

> **发布形态**：PyPI 上只有一个 `liorium`（12 个模块的源码由根 `pyproject.toml` 的
> `force-include` 打进同一个 wheel），安装时的分档只有 **extras**：
> `liorium[all]` / `[mcp]` / `[vector]` / `[openai]` / `[anthropic]` / `[google]` / `[images]`。
> 下表描述的是 workspace 里的模块边界（开发期 `uv sync --all-packages` 逐个装成可编辑包，
> 架构守卫按这些边界检查依赖方向）；它们**不再**各自发到 PyPI。

| 包 | 职责 | 依赖 |
|---|---|---|
| `liorium-core` | 类型 / URI / 指纹 / 日志 / 端口 | 零第三方依赖 |
| `liorium-vfs` | `VirtualFileSystem` / `VfsNode` / mount + 环检测 | 零第三方依赖 |
| `liorium-memory` | 与旧 `memory.sqlite` 同 schema 的存储 + 检索/图谱/抽取 + `MemoryRealm`（`memories://`） | core, vfs, langchain-core（`[vector]` extra：numpy/nano-vectordb/httpx） |
| `liorium-skills` | `SKILL.md` 目录 + `SkillsRealm`（`skills://` + 技能清单） | core, vfs, langchain-core |
| `liorium-mcp` | MCP 客户端（stdio/sse/http）+ `McpRealm`（运行时发现工具） | core, langchain-core, mcp |
| `liorium-todo-realm` | 状态化待办 + `TodoRealm`（`todo` / `todo_list`，每 50 回合提醒） | core, realm-base, langchain-core |
| `liorium-core-tool` | 六个核心工具的实现 + `ToolRuntime` | core, vfs, langchain-core |
| `liorium-core-tool-realm` | 核心工具的 **realm 外壳**：写 `RealmSpec(id="core")` 才装（Agent 不再自带任何工具） | core-tool, realm-base, vfs |
| `liorium-realm-base` | realm 契约与写作 API：`Realm` / `BaseRealm` / `RealmContext` / `JsonStateStore` / `@realm_tool` | core, vfs（`[tools]` extra：langchain-core） |
| `liorium-provider-manager` | Provider schema / 四种 API 格式 / 思考 / models.dev 目录 / 模型构建 | core, pydantic, httpx, langchain-core（`[openai/anthropic/google/all]`） |
| `liorium-agent` | LioriumAgent / runtime / middlewares / scheduler / realms | core, vfs, realm-base, langchain, langgraph |
| `liorium-cli` | 官方命令行 Agent：REPL / 斜杠命令 / 子代理事件查看 | liorium-agent[core,memory-vector,skills,todo], liorium-provider-manager[all] |

依赖方向：`core` 是叶子；开发期 `liorium-agent` 对 realm 模块走 extra（`[core]` `[memory]`
`[skills]` `[mcp]` `[todo]`），发布版这些模块都已在 wheel 里、由上面的 extras 只控制第三方库。
**Agent 不依赖 `liorium-core-tool`**：六个核心工具由 `liorium-core-tool-realm` 提供，装不装、装哪几个
由用户在 `AgentSpec.realms` / `toolsets` 里决定。
**realm 契约与写作 API 在 `liorium-realm-base`**（2026-11-07 从 agent 搬出）：写一个 realm
只需要 `core + vfs`，不背 langchain / langgraph；`liorium-agent` 依赖它并 re-export 旧名字。
能力包（`liorium-core-tool` / `liorium-core-tool-realm` / `liorium-memory` / `liorium-skills` / `liorium-mcp`）**允许**
import `liorium.agent` 或更轻的 `liorium.realm_base`（2026-11-06 起）；`liorium-todo-realm`
只依赖 `core + realm_base`，是"能力包不必背 Agent"的示范。能力包之间仍互不依赖。

**realm 实例作用域**（2026-11-08）：每个 realm 声明 `instance_scope`。
`"tree"`（memory / skills / mcp / subagents）整棵 Agent 树共享**一份实例**，子代理只借它的工具 ——
状态文件、VFS 挂载、外部连接都只有一份；`"agent"`（core / todo）每个 Agent 各一份，状态落在
自己的 `inbox/realm_state/` 下。框架强制约束：`"agent"` 级 realm 返回挂载点会在启动期直接报错，
所以"父子两套状态抢同一路径"在结构上不可能发生。


**第五拍**（2026-11-07 接线）：realm 用 `context_every` / `context_first` 声明节奏、
用 `context_items(turn)` 返回要注入的内容；`RealmManager.context_for_turn()` 在拿到执行位后
问一次，内容在下一个安全点物化成独立的 `<context>` user 消息（不改历史前缀、不吃缓存）。
返回空不消费周期 ⇒ 待办清单空着不浪费额度，一有 `pending` / `processing` 立刻提醒。

## 文档

完整文档入口：[`docs/README.md`](docs/README.md)（按使用者 / 扩展者 / 维护者分三条线，
附术语表）。

| 文档 | 内容 |
|---|---|
| [`docs/GUIDE.md`](docs/GUIDE.md) | 从零到跑起来：安装、最小 Agent、事件表、中断/注入、压缩与成本、多 Agent 并发（每段都能跑） |
| [`docs/API.md`](docs/API.md) | 12 个模块全部公开名字的签名、默认值、异常与不变量 |
| [`docs/EXTENDING.md`](docs/EXTENDING.md) | 写自定义 Realm / Middleware / 调度参数（`priority` / `FLAG`）/ model_factory / 工具，以及常见坑 |
| [`docs/MEMORY.md`](docs/MEMORY.md) | 记忆子系统：磁盘布局、检索流水线、内存预算、设计取舍 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 实现视角的模块地图与一个回合的时序 |
| [`docs/PUBLISHING.md`](docs/PUBLISHING.md) | 发布流程：Trusted Publishing（OIDC）到 PyPI |

## 快速开始

### 配置：`.env` 或命令行

CLI 的每项配置都能放进当前目录的 `.env`（在仓库里就是仓库根；已被 gitignore，模板见 `.env.example`）：

```powershell
Copy-Item .env.example .env      # 填 provider / 模型 / 记忆目录 / embedding
uv run liorium-cli                # 或 python -m liorium.cli
```

优先级 **命令行 > 环境变量 > `.env` > 默认**；`--env-file` 指定文件、`--no-env` 不读。
键名规则：`LIORIUM_` + 选项名大写（`LIORIUM_MODEL`、`LIORIUM_MEMORY_DIR`、`LIORIUM_EMBED_MODEL` …），
完整清单见 [`.env.example`](.env.example) 与 [MEMORY](docs/MEMORY.md)。

### 最小 Agent


```python
import asyncio
from pathlib import Path

from liorium.agent import AgentSpec, Limits, LioriumAgent


def make_chat_model(label: str):
    from langchain_openai import ChatOpenAI
    provider, model = label.split("::", 1)
    return ChatOpenAI(model=model, base_url="https://api.deepseek.com", api_key="...")


async def main() -> None:
    agent = LioriumAgent(
        spec=AgentSpec(
            id="main",
            inbox_dir=Path("agents/main"),
            model="deepseek::chat",
            prompt_sources=("AGENT.md", "ROLE.md", "COREMEMORY.md"),
            limits=Limits(context_window=128_000, max_turns=200),
        ),
        model_factory=make_chat_model,
    )
    await agent.start()                       # 不调模型（计划 §F7）
    try:
        async for event in agent.run("今天出门要带伞吗？"):
            print(event.to_dict())            # start / delta / reasoning_delta / tool_* / done
    finally:
        await agent.close()


asyncio.run(main())
```

- 叫停 = `await agent.interrupt(reason="user_stop")` + 一个新的 `run()`（两个回合）。
- 运行中补充信息 = `await agent.inject([ContextItem(source="trigger:desktop", text="开始下雨了")])`，
  **永不打断**，在下一个安全点物化成独立 user 消息。
- 热重载 = `await agent.apply(new_spec)`：只重建指纹变化的部件；失败保留旧部件。
- 清空对话 = `await agent.reset_dialog()`（纯状态写入，不调模型；同时清空
  `state_scope="conversation"` 的 realm 状态）。

## CLI Agent（`liorium.cli`）

`packages/liorium-cli` 是官方命令行 Agent（不是示例）：realm 能力包（**核心工具**
`read/write/edit/search/find/execute`、**长期记忆（`memories://`）**、**技能库（`skills://`）**、
**待办**、**子代理**）、流式渲染、`/model` 热切换、`inject`、`clear`、Ctrl+C 中断。模块路径
`liorium.cli`，入口 `liorium-cli`（等价 `python -m liorium.cli`）。发布版里 CLI 就在 `liorium`
同一个 wheel 中，装 `pip install "liorium[all]"` 即可，不需要额外的 `liorium-cli` 包。

六个核心工具现在是 `core` realm（`liorium.core_tool_realm`）：CLI 默认装上，`--no-core-tools`
则不装（Agent 就一个工具都没有），`--tools read,find` 只装子集。

```powershell
# 装好工作区后直接跑（默认用 provider 配置里的 main_model）
uv sync --all-packages
uv run liorium-cli

# 指定 provider / 模型
uv run liorium-cli --provider Go --model deepseek-v4.1-flash

# 思考强度（默认 medium；off 关闭）
uv run liorium-cli --thinking high
uv run liorium-cli --thinking off --temperature 0.3

# models.dev：默认只读本地缓存；refresh 才联网（结果只改内存）
uv run liorium-cli --models-dev refresh --list-models

# 挂上示例技能（skills://session-log，演示"读技能 → 写记忆"）
uv run liorium-cli --skills-dir examples/skills

# 只要核心工具，不要记忆 / 技能 / 待办 / 子代理
uv run liorium-cli --no-memory --no-skills --no-todo --no-subagents

# 核心工具只装一部分（读 + 查找 + 执行）
uv run liorium-cli --tools read,find,execute

# 一个工具都不要（零工具装配是合法的）
uv run liorium-cli --no-core-tools --no-memory --no-skills --no-todo --no-subagents

# 把记忆库指到别处（默认 <inbox>/memory，可直接读写既有 memory.sqlite）
uv run liorium-cli --memory-dir ~/liorium-memory

# 单轮 / 管道 / JSON 事件（便于脚本与测试）
uv run liorium-cli -p "统计仓库里有多少个 .py 文件"
echo "总结 README" | uv run liorium-cli --json

# 列出配置里的全部模型（带格式与上下文长度）
uv run liorium-cli --list-models
```

配置查找顺序：`--config` → `$LIORIUM_PROVIDER_CONFIG` → `~/.liorium/providers.json`
→ 当前目录 `providers.json`。全都找不到时**逐项询问**（base_url / key / 模型 / API 格式 /
上下文长度 / 思考方言），并可选保存到 `~/.liorium/providers.json`。
Provider 管理（四种 API 格式、思考参数、models.dev 识别、模型构建）由
[`liorium-provider-manager`](packages/liorium-provider-manager/README.md) 提供。

交互命令：`/help` `/model [Provider::model]` `/provider <name>` `/thinking [off|low|medium|high]`
`/models-dev [status|refresh]` `/clear` `/status` `/tools`
`/memory [query]` `/skills` `/todo [all]` `/realm [enable|disable] [id]` `/subagent`
`/reload` `/inject <text>` `/context` `/history [n]` `/exit`。

- 记忆：`read memories://` 看目录、`read memories://<path>` 读全文、`/memory <词>` 检索；
  Agent 用 `write memories://<path>` 自己存长期事实（路径当标题）。
- 技能：`<skills_dir>/<slug>/SKILL.md`，清单自动预载进 system prompt；
  装/卸/改完打 `/reload` 立刻生效（重挂载 + 重取工具 + 重建 prompt，`CacheEpoch` +1）。
- 子代理：默认启用，模型用 `newSubagent` / `invokeSubagent` / `wait_for_subagents` 驱动
  （并行数量不设上限）。父回合进行中只实时打印关键事件（创建 / 工具 / 完成 / 失败）；
  `/subagent` 列状态，`/subagent <name> [n]` 看最近 n 条事件（默认 20），
  `/subagent stop|rm <name>` 停止 / 删除（名字也接受 `subagent-<name>` 或 `agent_id`）。
- realm 开关：`/realm` 列出每个 realm 的状态 / 工具数 / 挂载 / 上下文节奏，`/realm <id>` 看详情；
  `/realm disable <id>` 与 `/realm enable <id>` 运行期开或关（工具、挂载、prompt 段一起撤下或恢复，
  `epoch` +1，不需要重启 CLI）。装配集合仍由启动参数决定：`--no-memory` 这类要重启才会变。
- 记忆 / 技能 / 待办在发布版里都在同一个 `liorium` wheel 中，CLI 默认全部装配；
  只有第三方库需要按需选装（`liorium[vector]` / `[mcp]` / `[openai]` …），
  缺这些库时 CLI 打印一条提示并降级，核心工具（`core` realm）照常可用。

工作区默认是当前目录（`read/write/edit/search/find/execute` 的相对路径基准），
Agent 根目录（prompt + checkpoint + 记忆库 + 技能目录 + 子代理）默认是
`<workspace>/.liorium-cli/agent`，可用
`--workspace/--inbox/--system/--memory-dir/--skills-dir` 改。Ctrl+C 在生成中是
**中断当回合**（不退出），在提示符下是退出。

## realm 能力模块（v1.1）

四个 realm 能力模块都是"工具 + 状态 + `prompt_fragment()`"（前三个还有 VFS 挂载）：

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.memory import MemoryRealm
from liorium.skills import SkillsRealm
from liorium.mcp import McpRealm
from liorium.todo_realm import TodoRealm

agent = LioriumAgent(
    spec=AgentSpec(
        id="main",
        inbox_dir=Path("agents/main"),
        model="deepseek::chat",
        prompt_sources=("AGENT.md",),
        realms=[
            RealmSpec(id="memory"),          # <inbox>/memory/memory.sqlite
            RealmSpec(id="skills"),          # <inbox>/skill.d/*/SKILL.md
            RealmSpec(id="mcp", params={"servers": {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}}),
            RealmSpec(id="todo"),            # <inbox>/realm_state/todo.json；每 50 回合提醒
        ],
    ),
    model_factory=make_chat_model,
    realm_factories={
        "memory": MemoryRealm,
        "skills": SkillsRealm,
        "mcp": McpRealm,
        "todo": TodoRealm,
    },
)
```

| 包 | 挂载 / 工具 | 说明 |
|---|---|---|
| `liorium-memory` | `memories://`（懒加载投影）、`memory_search` / `memory_list` / `memory_delete` / `memory_tag` / `memory_score` / `memory_graph` / `memory_attach` | SQLite schema 为 v1，可直接读写既有 `memory.sqlite`；读/写/编辑走核心 `read`/`write`/`edit` |
| `liorium-skills` | `skills://`（只读）、`list_skills` | `<inbox>/skill.d/<slug>/SKILL.md`；技能清单预载进 system prompt，装/卸技能 ⇒ `CacheEpoch +1` |
| `liorium-mcp` | 无挂载、动态工具 | 工具名 `<server>_<tool>`；`realm.set_servers(...)` 或 `realm.mark_dirty()` 后，**下一个回合边界**自动重连换工具 |
| `liorium-todo-realm` | 无挂载、`todo` / `todo_list` | 状态化待办（pending/processing/blocked/finished）；每 50 回合把未完成清单注入上下文（只在有 pending/processing 时） |

> 已知缺口：`McpRealm` 目前没有实现 `Realm.mounts()`，经 `AgentSpec` 装配时会被
> `RealmManager` 标 `failed`（降级，不影响其它 realm）。`docs/API.md`
> 的 MCP 一节也标注了同一件事。

realm 的 v1.1 可选钩子（能力包实现，Agent 在回合边界 / 拿到执行位后调用）：

- `dirty()` / `mark_dirty()`：真相源变了就刷新工具/挂载/提示词；
  `clear_dirty()` 由能力包自己实现（`BaseRealm` 没有默认实现，缺省时管理器用
  `mark_dirty(False)` 兜底）；
- `async refresh()`：刷新前重连（MCP 用）或重新对账（memory 用）；
- `skills()`：该 realm 想预载进 prompt「技能清单」段的条目；
- `context_every` + `context_items(turn)`（第五拍）：每 N 回合给一次"要不要说点什么"的机会，
  返回非空才注入、返回空不消费周期；`ctx.inject()` 是同一管道的推送侧。

宿主**自己知道**真相源变了（刚装了技能、另一个进程写了记忆库）时，可以不等下一个回合：

```python
await agent.refresh_realms()          # force=True：先全部置脏，再刷新 + 重建运行时
await agent.refresh_realms(force=False)  # 只刷新自报 dirty() 的 realm
```

`refresh_realms()` 返回是否真的刷新过；工具 schema / system prompt 变了 `CacheEpoch` 才 +1。

运行期开关某个**已装配**的 realm（工具 / 挂载 / prompt 段 / 后台任务一起撤下或恢复）：

```python
await agent.set_realm_enabled("todo", False)   # 返回调用后是否在运行
await agent.set_realm_enabled("todo", True)
```

装配集合仍由 `AgentSpec.realms` 决定（未装配的抛 `RealmNotFoundError`）；CLI 的
`/realm` 就是它的薄前端。

`conversation` scope：`state_scope="conversation"` 的 realm 状态按 thread 分桶
（`realm_state/<id>/conversation-<thread>.json`），`await agent.reset_dialog()` 时清空；
`state_scope="agent"` 的状态跨对话保留。

profile 语法糖：

```python
from liorium.agent import Profile

profile = Profile(id="chat", realms=(RealmSpec(id="memory"), RealmSpec(id="skills")))
spec = spec.with_profile(profile)     # 显式声明优先；指纹覆盖合并结果
```

## 示例：多 Agent（2 个 Agent + 调用方 semaphore）

`examples/multi_agent.py` 演示"并发由调用方控制"：两个身份独立的 Agent 并发跑同一条
任务，用 `asyncio.Semaphore` 包住每个回合的 `async for`（库只保证同一个 Agent 内部排队）。

```powershell
python examples/multi_agent.py -p "用一句话介绍 Liorium" --concurrency 1
```

## 开发

```powershell
uv sync --all-packages --all-extras     # 建 .venv 并装好 12 个模块（可编辑包）
uv run pytest -q                        # 全量测试（含架构守卫）

# 本机 PyPI 慢时用离线模式
uv sync --all-packages --all-extras --offline

# 直接跑测试（用 .venv 里的解释器）
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\scripts\test.ps1 -q

# 真模型端到端（OpenCode Go）
$env:OPENCODE_GO_KEY = "<key>"
$env:OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
$env:OPENCODE_GO_MODEL = "deepseek-v4.1-flash"
.\scripts\test.ps1 -q -m live
```

本地联调（把工作区 12 个包以可编辑模式装进任意解释器）：

```powershell
./scripts/dev-install-liorium.ps1                       # 装进 .venv
./scripts/dev-install-liorium.ps1 -Python C:\Python311\python.exe -AllExtras
```

发布走 GitHub Actions 的 **Trusted Publishing**（OIDC，无需任何 API token）：
推一个 `v*` tag 即可把 `liorium` 发到 PyPI（12 个模块在同一个 wheel 里，PyPI 侧只需 1 条
pending publisher，见 [`docs/PUBLISHING.md`](docs/PUBLISHING.md)）。

## 不变量（违反即跑偏，计划 §8）

1. `liorium` 任何包不许 import 旧后端（`tests/test_architecture.py` 用 AST 守这条线）。
2. 任何包不许读产品配置文件。
3. 基座中间件顺序不可被 realm 改变。
4. 不许出现裸 `asyncio.create_task`（一律 `ctx.spawn` / 宿主托管）。
5. 不许新增 LangChain 消息类型；`Context` 只存在于独立 channel。
6. 工具 schema 变化必须伴随 `CacheEpoch +1`。
7. 工具名冲突必须报错。
8. `run()` 之外没有第二条 Agent 事件流。
9. VFS 里不放真相（只放投影）；realm 状态不放 checkpoint。
10. 每个 realm 能力包必须提供自己的 realm；只提供工具实现、不提供 realm 的包（如 `liorium-core-tool`）
    由 realm 壳包（`liorium-core-tool-realm`）负责装配，**Agent 自己不添加任何工具**。
