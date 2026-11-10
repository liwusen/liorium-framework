# Realm 实例作用域（`instance_scope`）

> 2026-11-08 设计。解决的问题：子代理继承父 Agent 的 realm 装配之后，
> **同一份状态 / 同一个挂载点被两套实例同时拥有**。

## 1. 为什么会有这个问题

一棵 Agent 树共享**同一个** `VirtualFileSystem`（子代理能看见父的文件，这是设计）。
而 realm 会占三类运行时资源：

| 资源 | 树内是否唯一 | 谁在按 Agent 复制就会出事 |
|---|---|---|
| `self.state`（JSON） | 否（每 Agent 一个 inbox） | —— |
| VFS 挂载点（`/memories`、`/skills`…） | **是** | 第二次挂同一前缀 ⇒ 冲突 / 幽灵投影 |
| 外部连接与库（sqlite、MCP 子进程） | **是** | 两份连接、双写、锁冲突 |

所以「状态冲突」不是某个 realm 写得不好，而是**实例作用域没有被建模**。

## 2. 契约

`liorium.realm_base.BaseRealm` / `Realm` 新增类属性：

```python
class BaseRealm:
    instance_scope: str = "agent"   # "agent" | "tree"
```

| 取值 | 含义 | 谁在用 |
|---|---|---|
| `"agent"`（默认） | 每个 Agent（含子代理）一份实例；状态落在**它自己的** `inbox/realm_state/<id>.json` | `core`（无状态无挂载）、`todo`（只有 JSON 状态） |
| `"tree"` | 整棵 Agent 树**一份实例**：一个状态文件、一次挂载、一条连接 | `memory`、`skills`、`mcp`、`subagents` |

## 3. 两条强制不变式

**A. `"agent"` 级 realm 不许返回挂载点。**
`RealmManager.start()` 在 `instance.start()` 之后检查 `mounts()`：
非空且 `instance_scope != "tree"` ⇒ `RealmError`，该 realm 标 `failed`
（原因进 `status().realms[*].error`）。非法取值同样报错，不静默当默认。

> 于是「父子两套状态抢同一路径」在结构上写不出来，不靠 review、不靠约定。

**B. `"tree"` 级 realm 由子 Agent 借用，不重建。**
`RealmManager.assemble()` 遇到「树根已装配且 `instance_scope="tree"`」的同名 realm ⇒
直接放**同一个 `RealmRecord`**（同一实例），不调工厂、不 start、不重新挂载。

借用的职责切分：

| 能力 | 所有者（树根） | 借用者（子 Agent） |
|---|---|---|
| `tools()` | 提供 | **继承**（子代理因此能用 memory / read / todo 的工具） |
| `mounts()` | 挂载一次 | 不挂（已挂在共享 VFS 上） |
| `state` | 唯一写入方 | 不碰 |
| `prompt_fragment()` / `middleware()` | 生效 | 不重复生效（子代理 prompt 是 TASK，不塞宿主提示） |
| `context_items()` 节奏 | 每条 run 递减一次 | 不递减（否则父子互相消费提醒额度） |
| `start/stop/restart/set_enabled/refresh` | 唯一开关方 | 抛 `RealmError`（"树级共享实例…"） |

`status()` 里借用来的那条带 `shared=True`，便于宿主/CLI 解释"为什么它不归我管"。

## 4. 子代理装配

`SubagentRealm._ensure_agent()` 给子 Agent 的 `realms` = 父的 `realms` **减去 `subagents` 自己**：

- `tree` 级（memory / skills / mcp）：借用树根的同一实例 —— 工具可用、挂载只做一次、状态唯一；
- `agent` 级（core / todo）：在子代理自己的 inbox 下新建一份，天然隔离；
  `core` 读子代理自己的 `toolsets`，所以 `newSubagent(toolsets="read,find")` 依旧生效；
- `subagents` 永不继承：否则子代理能再造兄弟，递归失控。

## 5. 状态落在哪

```
<root inbox>/
├── realm_state/todo.json          # 父的 todo（agent 级）
├── realm_state/subagents.json     # 子代理名册（树级 realm，但状态落在根 Agent 的 inbox）
├── memory/memory.sqlite           # 树级：子代理共用这一份
├── subagents/<name>/TASK.md       # 子代理的任务说明 / output.md / finalResult.md
└── subagents/<name>/realm_state/todo.json   # 子代理自己的 todo（agent 级）
```

`subagents` 是 `"tree"` 级实例，所以它的名册也跟着根 Agent 的 inbox 走 —— 重启后由
[`subagent-state.md`](subagent-state.md) 的恢复流程读回，子代理列表与 `subagents://` 投影不丢。

`state_scope="conversation"` 仍按 `thread_id` 分桶，与 `instance_scope` 正交：
tree 级 + conversation 级 = 用**根 Agent 的**会话桶（子 Agent 不重复分桶）。

## 6. 什么时候该选哪个

- 有 `mounts()` ⇒ **必须** `"tree"`（框架会拦）；
- 无挂载但持有全树唯一的资源（全局库、外部连接、单例缓存）⇒ `"tree"`；
- 只有 JSON 状态 / 纯内存缓存 ⇒ `"agent"`（默认），让每个子代理互不干扰；
- 想让子代理共用一份待办清单？给 `TodoRealm` 设 `instance_scope = "tree"` 即可（一行），
  代价是子代理与父编辑同一份清单。

## 7. 证据

`packages/liorium-agent/tests/test_realm_scope.py`（8 条）：

- `agent` 级 + 挂载 ⇒ 启动即 `failed` 且错误里带 `instance_scope`；
- 非法取值 ⇒ 启动即 `failed`；
- 子 Agent 借用树级实例：同一实例、只 start 一次、工具可见、挂载一次、`status().shared`、
  不重复 prompt 段、不消费周期上下文；
- 子 Agent 不能 `stop` / `restart` / `set_enabled` 借用来的 realm，但能开关自己的；
- 关闭子 Agent 不停止树级实例；
- `agent` 级状态文件父子各一份（互相看不到对方的待办）；
- 子 Agent 的 `core` 只挂自己的 `toolsets` 子集；
- 真实子代理链路：继承 `tree-x / core / todo`，`subagents` 被排除，`tree_root` 指向根。

子代理名册 / 正文跨进程恢复另见 [`subagent-state.md`](subagent-state.md)
（`packages/liorium-agent/tests/test_subagent_state.py`，9 条）。
