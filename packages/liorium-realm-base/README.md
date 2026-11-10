# liorium-realm-base

realm 的**契约与写作 API**：`Realm` 协议、`BaseRealm`、`RealmContext`、`JsonStateStore`、
`@realm_tool`。模块路径 `liorium.realm_base`。

这个包存在的唯一理由：**写一个 realm 不该背 langchain / langgraph**。
它只依赖 `liorium-core` + `liorium-vfs`，模块级不 import 任何 `langchain*` / `langgraph*`
（架构守卫 `test_realm_base_stays_light` 守着这条）。

`liorium-agent` 依赖本包并原样 re-export：`from liorium.agent import BaseRealm` 等旧路径继续可用。

## 安装

```powershell
uv add liorium-realm-base            # 只写挂载/状态/周期上下文
uv add "liorium-realm-base[tools]"   # 还要用 @realm_tool 提供工具（装 langchain-core）
```

## 最小 realm（不写任何脚手架）

```python
from liorium.realm_base import BaseRealm, realm_tool
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.node import VfsMount, VfsNode


class TodoRealm(BaseRealm):
    id = "todo"          # 必须等于 RealmSpec.id

    async def on_start(self) -> None:
        self.items: list[str] = list(await self.state.get("items", []))

    @realm_tool
    async def todo_add(self, text: str) -> str:
        """加一条待办。"""
        self.items.append(text)
        await self.save(items=self.items)      # 写状态 + 立刻落盘
        return f"已加入：{text}"

    def mounts(self) -> dict[str, VfsMount]:
        fs = VirtualFileSystem(owner_id="todo")
        fs.root.add_child(VfsNode(name="todo.md", content=None, description="待办清单"))
        return {"/todo": VfsMount(prefix="/todo", vfs=fs, readonly=True)}

    def prompt_fragment(self) -> str:
        return "待办（`todo://`，只读）：用 `todo_add` 维护。"
```

装配端不变：`RealmSpec(id="todo")` + `realm_factories={"todo": TodoRealm}`（类本身就是
`(spec) -> Realm` 工厂，因为 `BaseRealm.__init__(self, spec=None)`）。

## 契约

| 成员 | 必须 | 语义 |
|---|---|---|
| `id` | 是 | 必须等于 `RealmSpec.id`（不一致 `RealmError`） |
| `requires` | 否 | 依赖的 realm id；`RealmManager` 拓扑排序，成环/缺依赖 `RealmDependencyError` |
| `state_scope` | 否 | `"agent"`（默认，跨对话）或 `"conversation"`（按 thread 分桶，`reset_dialog()` 清空） |
| `tools()` | 否 | 默认收集 `@realm_tool` 方法；同名工具冲突 ⇒ 整个装配报 `ToolNameConflictError` |
| `mounts()` | 否 | `dict[prefix, VfsMount]`；非 `VfsMount` 抛 `RealmError` |
| `prompt_fragment()` | 否 | 进 system prompt 的「realm 片段」段 |
| `middleware()` | 否 | 追加到基座**外层**的中间件（顺序不可重排） |
| `start(ctx)` / `stop(ctx)` | 否 | 默认实现：挂 `self.ctx` → 调 `on_start()` / `on_stop()` |
| `dirty()` / `mark_dirty()` / `async refresh()` | 否 | v1.1 运行期刷新（真相源变了） |
| `skills()` | 否 | 预载进 prompt「技能清单」的条目 |
| `context_every` / `context_first` / `context_origins` / `context_items(turn)` | 否 | 第五拍：周期上下文（2026-11-07 已接线）。`0` = 不参与；返回空不消费周期，见 [`docs/design/realm-context.md`](../../../docs/design/realm-context.md) |
| `ctx.inject(text, source=..., meta=...)` / `ctx.inject_items(items)` | 否 | 推送侧：随时把 Context 排到下一个安全点（同步、永不打断） |

## 写作 API

| 名字 | 作用 |
|---|---|
| `BaseRealm.__init__(spec=None)` | 存 `self.spec` / `self.params`；子类不写 `__init__` 也能当工厂 |
| `self.ctx` | `RealmContext`（`start()` 之后可用，`stop()` 时清空） |
| `self.state` | `JsonStateStore`；未 `start()` 时抛 `RealmError` |
| `await self.save(**values)` | `update_many` + `flush`，补上「忘了 flush 就等于没存」的坑 |
| `on_start()` / `on_stop()` | 默认生命周期钩子，此时 `self.ctx` / `self.state` 可用 |
| `@realm_tool` | 标记方法为工具；名字默认取方法名，描述默认取 docstring |
| `build_realm_tools(realm)` | 自定义 `tools()` 时复用收集逻辑 |

### `@realm_tool` 的细节

- 只标记、不注册；`tools()` 默认实现按**类定义顺序**收集，子类覆盖父类同名方法只留子类那份；
- 自己重写 `tools()` 的 realm 完全不受影响（老写法零改动）；
- 关键字参数原样转给 `langchain_core.tools.tool`：

  ```python
  @realm_tool(name="todo_list", description="列出全部待办", return_direct=True)
  def list_all(self) -> str: ...
  ```

- 同步 / 异步方法都支持；工具函数的第一个参数是 `self`，LangChain 的 schema 不会暴露它。

## 依赖方向

```
liorium-core   liorium-vfs
      \          /
   liorium-realm-base        ← 零 langchain（tools extra 才装 langchain-core）
           ↑
      liorium-agent          （依赖本包并 re-export）
           ↑
   liorium-cli / 宿主
```

`liorium-realm-base` **不许** import `liorium.agent`（否则 agent → 本包 → agent 成环），
架构守卫 `test_liorium_dependency_direction` 会拦住。

## 与 `liorium.agent.realm` 的关系

`liorium.agent.realm.manager.RealmManager`（装配 / 拓扑排序 / 冲突检测 / 失败降级）**留在
`liorium-agent`**：它要做 VFS 挂载、工具冲突检测与运行时重建，天然属于 Agent 侧。
本包只有「realm 这一侧」的契约。
