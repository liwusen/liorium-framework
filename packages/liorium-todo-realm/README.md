# liorium-todo-realm

状态化待办能力包：两个工具 + 一次持久的清单 + **每 50 个回合**的上下文提醒。

```bash
uv add liorium-todo-realm
```

## 快速开始

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.todo_realm import TodoRealm

agent = LioriumAgent(
    spec=AgentSpec(
        id="demo",
        inbox_dir=...,
        model="...",
        realms=[RealmSpec(id="todo")],          # 节奏默认 50 回合
    ),
    realm_factories={"todo": TodoRealm},        # 类本身就是 (spec) -> Realm 工厂
    ...
)
```

节奏按 spec 覆盖（`params` 进 spec 指纹 ⇒ 改节奏会触发 `apply()`）：

```python
realms=[RealmSpec(id="todo", params={"context_every": 10, "context_first": False})]
```

## 两个工具

| 工具 | 作用 | 主要参数 |
|---|---|---|
| `todo` | 增 / 改 / 删 | `action`（`add`/`update`/`remove`）、`text`、`id`、`status` |
| `todo_list` | 查 | `include_finished`（默认 `False`） |

```text
todo(action="add", text="写周报")                  → 已添加 t1 [pending] 写周报
todo(action="update", id="t1", status="processing") → 已更新 t1 [processing] 写周报
todo(action="update", id="t1", status="finished")   → 已更新 t1 [finished] 写周报
todo(action="remove", id="t1")                      → 已删除 t1：写周报
```

失败一律返回 `错误：…` 文本（不抛异常、不中断回合）——模型看得到原因，能自己改参数。

## 四个状态

| 状态 | 含义 | 会触发提醒 |
|---|---|---|
| `pending` | 还没开始 | 是 |
| `processing` | 在做 | 是 |
| `blocked` | 卡在外部条件上 | 否（但注入时会一起列出来） |
| `finished` | 完成 | 否 |

工具入参大小写都收（`PROCESSING` / `processing` 等价）；状态文件里统一小写。

## 每 50 回合注入一次：只在真的有活时

```python
class TodoRealm(BaseRealm):
    context_every = 50      # 每 50 个 run() 给一次机会（0 = 不参与）
    context_first = True    # 第 1 回合先提醒一次（针对上次启动遗留的待办）

    def context_items(self, turn):
        if not any(t.state in ("pending", "processing") for t in self._items):
            return None     # ← 返回空 = 不消费周期
        return ContextItem(source="realm:todo", text=render_context(self._items))
```

框架侧语义（`RealmManager.context_for_turn`：

- 每执行一个回合 `due -= 1`；`due` 归零那一回合才调用 `context_items()`；
- 返回空 ⇒ **不消费这个周期**：清单空着不浪费额度，一有 `pending`/`processing` 就立刻提醒；
- 返回非空 ⇒ 相位重置为 `50` 并落盘（`__context_due__` 键），所以**跨重启保留**；
- 注入落在消息列表末尾的一条独立 user 消息里（格式 `<context source="realm:todo" …>`），
  前面的历史前缀照常命中缓存 —— 这也是它不写进 `prompt_fragment()` 的原因。

一次到期回合里模型看到的历史：

```text
user:      今天要写周报
user:      <context source="realm:todo" at="18:20">待办提醒：2 条未完成
           - [processing] t1 写周报
           - [pending] t2 回邮件

           请按实际进度用 `todo` 工具更新状态：开始做 → processing；卡住 → blocked；做完 → finished。</context>
assistant: …
```

## 状态文件

`<inbox>/realm_state/todo.json`（`state_scope="agent"`：待办跟着 Agent 走，`reset_dialog()`
不会清空）。

```json
{
  "items": [
    {"id": "t1", "text": "写周报", "state": "processing",
     "created_at": "2026-11-07T21:40:00", "updated_at": "2026-11-07T21:41:00"}
  ],
  "next_id": 2,
  "__context_due__": 47
}
```

- `items` / `next_id` 由本包维护；`next_id` 保证删除后的 id **不复用**；
- `__context_due__` 是框架保留键（双下划线前缀），realm 不要读写；
- 每次工具写操作立刻落盘（`save()` = `update_many` + `flush`）；
- 状态文件损坏（未知状态 / 缺字段）⇒ `on_start()` 抛错，该 realm 标 `failed`
  并在 `status().realms[*].error` 里可见，其它 realm 照常。

## 依赖

```text
liorium-core  ←  liorium-realm-base  ←  liorium-todo-realm
```

不依赖 `liorium.agent`、不依赖 `liorium-vfs`（本 realm 没有 VFS 挂载）。它是"能力包不必背
Agent 运行时"的示范：`tests/test_architecture.py` 把允许面钉死在
`{liorium.core, liorium.realm_base}`。

## 不做

- 没有 `todo://` 只读挂载（读走 `todo_list`）；
- 没有优先级 / 截止时间 / 负责人（需要就扩 `Todo` 字段，或另写一个 realm —— 一个节奏一个 realm）；
- 框架不会替你改状态：状态迁移由模型通过 `todo` 工具完成，提醒只是提醒。
