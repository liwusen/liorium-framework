# liorium-cli

Liorium 的官方命令行 Agent：真实模型 + realm 能力包（核心工具 / 长期记忆 / 技能 / 待办 /
子代理），流式渲染、`/model` 热切换、Ctrl+C 中断。

六个核心工具由 `core` realm（`liorium-core-tool-realm`）提供：默认装配，`--no-core-tools`
关掉，`--tools read,find` 只要子集。工具全部来自 realm，Agent 本身不带工具。

## 安装与启动

在工作区里（开发）：

```powershell
uv sync --all-packages --all-extras
uv run python -m liorium.cli        # 仓库内：root 是虚拟 workspace，不装 console script
```

从 PyPI 安装后直接：

```powershell
liorium-cli setup          # 先配置 provider（可交互，也可 setup providers add …）
liorium-cli
liorium-cli -p "看看当前目录有什么文件"
echo "总结 README" | liorium-cli --json
```

基础安装（`pip install liorium`）自带 OpenAI 兼容 SDK（CLI 的默认格式），所以 DeepSeek /
OpenRouter / 本地 vLLM 这类端点装完就能用；Anthropic / Google 协议需要
`liorium[anthropic]` / `liorium[google]`（或一次装 `liorium[all]`）。

## 配置（provider / 记忆系统）

两个文件，都**可以不存在**；缺省时用内置默认，启动不会因配置缺失失败：

| 文件 | 内容 |
|---|---|
| `~/.liorium/providers.json` | provider / key / base_url / 每模型格式 / 上下文长度 / 思考方言（0600） |
| `~/.liorium/config.json` | 非密钥设置：默认模型、思考强度、工作区、记忆（目录 / embedding / 开关）、技能、UI |

优先级：**命令行 > 进程环境变量 > `./.env` > `config.json` > 内置默认**；`LIORIUM_HOME`
可以整体换掉配置根目录（多套配置 / 测试）。

### 命令行配置

```powershell
liorium-cli setup                        # 交互式菜单（provider / 记忆 / 查看 / 体检）
liorium-cli setup show                   # 全部配置一览（密钥打码）
liorium-cli setup doctor                 # 体检：文件 / key / 目录 / 依赖
liorium-cli setup init                   # 只建空骨架，不写任何凭据
liorium-cli setup path                   # 打印各配置文件位置

liorium-cli setup providers list
liorium-cli setup providers add deepseek `
    --base-url https://api.deepseek.com --model deepseek-chat --model deepseek-reasoner `
    --api-format openai_chat --context 128000 --thinking deepseek
liorium-cli setup providers set-key deepseek      # 不回显；也可 --key sk-…
liorium-cli setup providers edit deepseek --model deepseek-v4.1-flash --rm-model deepseek-chat
liorium-cli setup providers default deepseek::deepseek-chat
liorium-cli setup providers test deepseek::deepseek-chat          # 本地校验；--network 请求 /models
liorium-cli setup providers rm deepseek

liorium-cli setup memory show
liorium-cli setup memory set dir D:/data/liorium-memory
liorium-cli setup memory set embed-model text-embedding-3-small
liorium-cli setup memory set vectors off
liorium-cli setup memory enable / disable
liorium-cli setup memory init                     # 建目录 + 空记忆库

liorium-cli setup set thinking low                # 其余设置：get / set / unset
liorium-cli setup --json show                     # 脚本化：JSON 输出
```

语义约定：

- 写入**原子**（临时文件 + `os.replace`，0600），写失败会报错，不会假装成功；
- `add` 的失败不会留下半个 provider；重名 / 非法 URL / 未知格式 / 非法上下文一律拒绝并给出可用值；
- 第一个 provider 自动成为默认模型；`rm` 掉默认 provider 时自动回退到下一个可用模型；
- 密钥只进 `providers.json`，`config.json` 里只有 `memory.embed_key`（显示时打码）。

### CLI 内交互式配置

直接 `liorium-cli`，在提示符里：

```
liorium[...]> /setup              # 顶层菜单：provider / 记忆 / 查看 / 体检 / 建空配置
liorium[...]> /setup providers    # 直接进 provider 子菜单
liorium[...]> /setup memory       # 直接进记忆子菜单
liorium[...]> /setup show         # 当前配置一览
```

改完立刻生效：provider 变更会重新读 `providers.json` 并热切换到默认模型；记忆开关走
`/realm enable|disable memory`；embedding 设置变化会重建记忆 realm。只有**记忆库目录**
变更需要重启（realm 路径在启动时装配），提示会写明。

### 新电脑第一次运行

什么配置都没有时，`liorium-cli` 会：

1. 建出 `~/.liorium/`、空的 `providers.json`（`{"providers": []}`）与 `config.json`；
2. 进入「未配置」REPL：`/help` `/setup` `/show` `/doctor` `/init` `/path` `/exit` 可用，
   普通提问只提示先配置；
3. 非交互场景（`-p`、`--json`、管道）直接以退出码 2 提示 `liorium-cli setup …`，不会卡住等 stdin。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--workspace` | 工作区根（裸路径与 `execute` 的 cwd），默认当前目录 |
| `--inbox` | Agent 根（prompt / checkpoint / 记忆 / 子代理），默认 `<workspace>/.liorium-cli/agent` |
| `--provider` / `--model` | 指定 provider 与模型（也支持 `Provider::model`） |
| `--thinking {off,low,medium,high}` | 思考强度（默认 `medium`；`off` 关闭） |
| `--no-thinking` | 等价 `--thinking off` |
| `--hide-reasoning` | 只隐藏 `reasoning_delta` 显示（不改变是否请求） |
| `--models-dev {off,cache,refresh}` | 目录策略；默认 `cache`（只读本地缓存，不联网） |
| `--models-dev-cache` | 目录缓存文件；默认 `~/.liorium/cache/models_dev/api.json` |
| `--refresh-models` | 启动时从各 Provider `/models` 刷新模型列表（联网） |
| `--no-core-tools` | 不装配核心工具（`read/write/edit/search/find/execute`），Agent 变零工具 |
| `--tools read,write,find` | 只装配这些核心工具（写进 `AgentSpec.toolsets["default"]`）；与 `--no-core-tools` 互斥 |
| `--no-memory` / `--no-skills` / `--no-todo` / `--no-subagents` | 关掉对应能力 |
| `--memory-dir` / `--skills-dir` | 自定义记忆库 / 技能目录 |
| `-p, --prompt` | 单轮模式：跑完退出 |
| `--json` | 事件以 JSON Lines 输出（配合 `-p` / 管道） |
| `--list-models` | 列出配置里的模型（格式 / 上下文 / 输出 / 思考形状）后退出 |

思考开启时**不发送 `temperature`**（Responses / Anthropic 的 thinking 与 temperature
不兼容）；`--temperature` 在 `--thinking off` 时生效。`aux` / `compact` 调用永远不思考。

## 斜杠命令

`/help` `/setup` `/model` `/provider` `/thinking` `/models-dev` `/clear` `/status` `/tools`
`/memory` `/skills` `/todo` `/realm` `/reload` `/inject` `/context` `/history` `/subagent` `/exit`。

- `/setup`：见上「CLI 内交互式配置」。
- `/todo`：列出待办（默认只看未完成，`/todo all` 含已完成）；未完成的待办会由
  `liorium-todo-realm` 每 50 个回合往上下文里提醒一次（`context_first` 让第 1 回合先提醒）。
- `/realm`：列出每个 realm 的状态 / 工具数 / 挂载 / 上下文节奏（`每 50 回合 · 下次 12`），
  `/realm <id>` 看详情（工具、挂载、状态文件、prompt 段）；`/realm disable <id>` /
  `/realm enable <id>` 运行期开或关 —— 工具、挂载、prompt 段与 `ctx.spawn` 的后台任务一起
  撤下 / 恢复，`epoch` +1，不需要重启。只对**已装配**的 realm 有效；`--no-memory` 这类
  启动开关决定的集合要重启 CLI 才变。realm 自己起不来时其它 realm 不受影响，
  `/realm` 会把 `failed: 原因` 显示出来。
- `/thinking`：无参查看当前强度与模型思考形状/来源；`/thinking high` 运行中切换并强制重建
  模型（失败保留旧运行时）。
- `/models-dev`：`status` 看目录缓存；`refresh` 联网刷新并重新路由当前模型（只改内存，
  不写回配置）。

### 子代理

子代理默认启用，由模型通过 `newSubagent` / `invokeSubagent` / `wait_for_subagents`
等工具创建和驱动（数量不设上限，能并行跑）。CLI 在后台订阅 `SubagentRealm` 的事件流：

- 父回合进行中，只实时打印关键事件（创建 / 启动 / 工具调用 / 完成 / 失败），
  正文增量不混排 —— 多个子代理并行时逐字混排没法读；
- `/subagent` 列出所有子代理的状态、运行时长、输出长度与最后一条事件；
- `/subagent <name>` 查看该子代理最近 20 条事件，`/subagent <name> 50` 可改条数；
  名字也接受 `subagent-<name>` 或事件里的完整 `agent_id`；
- `/subagent stop <name>` / `/subagent rm <name>` 停止或删除（删除会连它的目录与对话一起删）。

事件缓冲区每子代理最多 500 条、单条正文截断到 4K 字符；完整输出始终以
`subagents://<name>/output.md` / `finalResult.md` 为准。

子代理**有状态**：名册（名称 / 工具集 / 状态 / 时间戳）在 `<inbox>/realm_state/subagents.json`，
任务与产出在 `<inbox>/subagents/<name>/` 下。重开 CLI 后 `/subagent` 仍能列出它们、
`subagents://` 仍能读到上次的结果；上次跑到一半的会显示 `interrupted`，模型再次
`invokeSubagent` 会接着它自己的对话继续。

## 代码结构

```
src/liorium/cli/
├── app.py        # 参数解析 / .env 装载 / 冷启动 / run_cli
├── session.py    # CliSession：Agent 装配、REPL、斜杠命令
├── setup.py      # setup 子命令 + 交互式向导（provider / 记忆）
├── config.py     # ~/.liorium/config.json：schema / 读写 / 冷启动骨架
├── subagents.py  # 子代理事件监控与渲染
├── providers.py  # 配置搜索 / 向导 / 模型工厂（委托 liorium.provider）
├── env.py        # .env 解析与优先级
└── ui.py         # 终端颜色 / 截断 / 时间格式化
```

## 测试

```powershell
uv run pytest packages/liorium-cli/tests -q
```
