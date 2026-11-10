# liorium-core-tool-realm

Liorium 的**核心工具能力包**：把 `liorium-core-tool` 里的六个工具
（`read` / `write` / `edit` / `search` / `find` / `execute`）装成一个 realm。

两个包都保留，职责分开：

| 包 | 负责 |
|---|---|
| `liorium-core-tool` | 工具实现、`ToolRuntime`（路径解析 / VFS 读写 / 进程端口） |
| `liorium-core-tool-realm` | realm 外壳：装配进 Agent、工具子集、prompt 段、生命周期 |

**Agent 自己不添加任何工具**：工具全部来自用户装配的 realm。不装这个包，Agent 就是零工具；
装了它并写 `RealmSpec(id="core")`，模型才看得到六个核心工具。

## 用法

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.core_tool_realm import CoreToolRealm

spec = AgentSpec(
    id="demo",
    inbox_dir=inbox,
    model="provider::model",
    prompt_sources=("AGENT.md",),
    realms=(RealmSpec(id="core"),),          # ← 用户自己加
    toolsets={"default": ("read", "find")},  # ← 可选：只给两个工具
)

agent = LioriumAgent(
    spec=spec,
    model_factory=make_model,
    realm_factories={"core": CoreToolRealm},  # ← 用户自己注册工厂
)
```

CLI 里等价于默认装配 + `--tools read,find`；`--no-core-tools` 则不装这个 realm。

## API

`from liorium.core_tool_realm import CORE_REALM_ID, CoreToolRealm`

| 名字 | 说明 |
|---|---|
| `CORE_REALM_ID` | `"core"`（realm id，装配时 `RealmSpec(id=...)` 与工厂键都用它） |
| `CoreToolRealm` | `BaseRealm` 子类；类属性 `id="core"`、`instance_scope="agent"`、`state_scope="agent"`、`context_every=0` |
| `DEFAULT_MAX_OUTPUT_CHARS` | `120`，与 `Limits.max_tool_output_chars` 一致 |

工具子集来源（先到先得）：

1. `RealmSpec.params["tools"]`（字符串或字符串序列；未知名字抛 `ValueError`，realm 标 `failed`）；
2. `AgentSpec.toolsets["default"]`；
3. 都没有 ⇒ 六个全给。

其它 `params`：`max_tool_output_chars`（默认 120）。运行时依赖由框架提供：
`RealmContext.vfs` / `workspace_root` / `artifacts`；独立使用时可以在构造器里传
`CoreToolRealm(spec, artifacts=...)`。

## 实例作用域

`instance_scope="agent"`：本 realm 无挂载、无外部连接、无状态，每个 Agent（含子代理）各建一份
是安全的。子代理因此可以用 `newSubagent(toolsets="read,find")` 拿到不同的子集，
而父 Agent 仍可持有全部六个工具。

## 测试

```bash
git clone https://github.com/liwusen/liorium-framework.git
cd liorium-framework
.\.venv\Scripts\python.exe -m pytest -q packages/liorium-core-tool-realm
```
