# Realm 的第五拍：按回合节奏注入 Context

> 状态：**已实现（2026-11-07）**——代码块是落地后的形状；实现证据见
> [ACCEPTANCE.md](ACCEPTANCE.md) 的「2026-11-07 变更：接线第五拍 + `liorium-todo-realm`」。
> 目标读者：实现者（`liorium-agent`）与 realm 作者。
> 前置阅读：[turn-loop.md](turn-loop.md) §1（回合基线）、[EXTENDING.md](../EXTENDING.md) §1.4（周期上下文）。
> 另见：[realm-scope.md](realm-scope.md)（实例作用域：哪些 realm 整棵树只有一份、
> 子 Agent 只借工具，决定第五拍的节奏归谁）。
> 依据代码（行号取 2026-11-07 的 `main`）：`packages/liorium-agent/src/liorium/agent/{agent.py,context.py,status.py}`、
> `realm/{base.py,manager.py}`、`runtime/loop.py`、`runtime/middleware/context_merge.py`、`packages/liorium-core/src/liorium/core/types.py`。

## 1. 一句话

**框架管「什么时候」，realm 只管「是什么」。**

realm 作者写「到期了，当前待办是这 3 条」；框架负责「什么时候到期、怎么把它变成一条不破坏缓存与工具组的消息」。

## 2. 问题：四件套全是静态面

Realm 的契约——`tools()` / `mounts()` / 状态 / `prompt_fragment()`——描述的都是「这个能力提供什么」，只在装配和刷新时被读一次。唯一的运行期动态面是 `dirty()` → `refresh()`，它解决的是**真相源变了**（装了技能、MCP 上线）。

缺的那一拍是：**按回合节奏主动往上下文里投喂信息**。没有它，"每 50 轮提醒一次 todo"只能由宿主或中间件**越权**完成。

### 2.1 四条绕行路线的成本

| 路线 | 能实现 | 代价 |
|---|---|---|
| 动态改 `prompt_fragment()` | 勉强 | 要走 `mark_dirty()` → `refresh()` → `_build_runtime()`：`CacheEpoch` +1，且 `seed_system_prompt` 会用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 重写整段历史（`runtime/checkpoint.py:91-101`）⇒ **全部前缀缓存失效**。todo 一变就重写全部历史，代价完全不成比例；而且 prompt 段是"能力说明"的位置，不该装高频数据 |
| 在 `middleware()` 里自己 append 消息 | 能 | 绕过 `ContextChannel` 的审计（`state.context` 没有记录）；`before_model` **每个模型调用**都跑（一个回合可能几十次），幂等要自己做；还要自己保证不拆散 `AIMessage(tool_calls)` + `ToolMessage` 组 —— `context_merge.py:7-13` 已把这条约束写清楚，等于重写一遍还更容易写错 |
| 宿主起定时器调 `agent.inject()` | 能 | 时序和内容都跑到宿主：宿主得懂 todo 的内部结构，换一个能力就要改一次宿主，realm 的封装等于不存在 |
| `run(text, context=[...])` | 不能 | 只有**发起回合的人**有这个入口，realm 拿不到 |

### 2.2 根因

`Realm` 协议里没有「回合边界」这个钩子：realm 不知道"又过了一轮"，也就没有任何合法时机说话。

## 3. 现状锚点（实现前必须知道的事实）

| 事实 | 位置 | 对本设计的意义 |
|---|---|---|
| 回合 id 是 `f"{spec.id}#{index}"`，计数器 `_turn_seq` 进程内从 1 递增 | `agent.py:526` | `TurnInfo.index` 复用同一个计数器，不新增状态 |
| 回合开始只取一次 pending（并入 user 消息） | `agent.py:543` | 节拍器不能自己拼消息，必须"排队 + 安全点" |
| 执行位顺序：`scheduler.acquire`（拿位）→ 才问 `context_for_turn` | `agent.py`（`_run_iter`） | 节拍器挂在**拿到执行位之后**：不抢位，也不在排队期间白算 |
| 形态①：Context 并入 user 消息 | `loop.py:80-85` | 本方案**不用**它（见 §6 注） |
| 形态②：安全点物化成独立 user 消息 | `context_merge.py:42-59` | 本方案直接复用 |
| pending 只有两个消费者 | `agent.py:543`、`context_merge.py:44` | 排队是安全的：任何时刻只有一处取走 |
| 审计记录集中在 `ContextChannel` | `context.py:79-100` | 白拿 `state.context` 审计与前端可读性 |
| realm 状态按 `state_scope` 分文件；`start()` 时 `load`，`start`/`stop` 时 `flush` | `liorium/realm_base/base.py:124-192`（`JsonStateStore`）、`manager.py:157/167/337` | 相位存这里 ⇒ 跨重启自然保留，不用新机制 |
| `<context source="…" at="HH:MM">` 格式**冻结** | `core/types.py:104-106` | 物化格式不变，不新增"改格式 ⇒ 全历史失效"的风险 |
| `RealmManager` 在 `ContextChannel` **之前**构造 | `agent.py:123`、`agent.py:131` | 要把通道交给 manager，这两行必须对调 |

## 4. 概念：五件套 = 2 个静态面 + 3 条节拍

| 触发源 | 钩子 | 框架做的事 | 典型用途 |
|---|---|---|---|
| 真相源变了 | `dirty()` / `refresh()` | 重挂载 + 重取工具 + 必要时重建运行时 | 装了技能、MCP 上线 |
| **每回合** | **`context_items(turn)`** | 交给 `ContextChannel`，在安全点物化成一条 `<context>` 消息 | **todo 每 50 轮提醒** |
| **随时** | **`ctx.inject(...)`** | 同上，永不打断 | 后台 watcher 发现新事实 |

关键实现选择：**不造新的注入管道，让"拉"复用"推"的管道**。

```
context_items(turn)  ← 框架按节奏调用，不碰历史
        │
        └─► ContextChannel.queue(items) ─► ContextMergeMiddleware.before_model
                                              └─► 独立 <context> user 消息（含审计）
```

于是 `loop.py` / `_drive_turn` / 压缩中间件**一行都不用改**：审计（`state.context`）、"注入消息不算回合起点"的压缩语义（`tests/test_compact_cut_with_injected_context.py` 已覆盖）、前端可读性全部白拿。

## 5. API

### 5.1 `TurnInfo`

```python
# liorium/realm_base/base.py（agent 已 re-export）
@dataclass(frozen=True, slots=True)
class TurnInfo:
    index: int          # 本进程内第几个 run()，从 1 开始（与 turn_id 的序号同一个计数器）
    id: str             # 与事件里的 turn_id 相同：f"{agent_id}#{index}"
    origin: str = "user"   # "user" / "trigger" / …
```

### 5.2 拉：`context_items(turn)` + 三个类属性

```python
class BaseRealm:
    context_every: int = 0        # 0 = 不参与（默认）；N = 每 N 回合给一次机会
    context_first: bool = False   # True：第 1 回合先注入一次，再进入 N 的节奏
    context_origins: tuple[str, ...] = ()   # 空 = 所有 origin 都计时

    def context_items(self, turn: TurnInfo):
        """返回 None / str / ContextItem / 它们的序列；可 sync 可 async。"""
        return None
```

框架侧语义（这是本 API 的灵魂，必须钉死）：

| 规则 | 行为 |
|---|---|
| `context_every = 0` | 永不调用 `context_items()`，零成本（现有三个能力包行为不变） |
| 计时 | 每执行一个 **origin 匹配**的回合，`due -= 1`；`due <= 0` 时才调用 |
| 返回非空 | 注入，`due = context_every`（相位从**成功注入**起算），并落盘 |
| 返回空 | **不消费这个周期**：`due` 停在 0，下一回合继续问 —— 清单空着不浪费额度，一有 todo 立刻提醒 |
| 抛异常 | 记 `error` 日志、跳过该 realm，其它 realm 照常（同 `prompt_fragments()` 的降级）；**不改** `record.state`，下一回合重试 |
| 返回类型 | `str` 自动包成 `ContextItem(source="realm:<id>")`；单条也接受；序列顺序即声明序 |
| 线程模型 | `context_items()` 在**持有执行位**时执行，必须廉价（只读内存/状态）。重活放 `ctx.spawn` + `ctx.inject` |
| 参数覆盖 | `RealmSpec.params["context_every"]` / `["context_first"]` 优先于类属性；`params` 进 spec 指纹 ⇒ 改节奏 = `apply()` 立即生效 |

一条节奏一个 realm。要「待办每 50 轮 + 日程每 10 轮」就拆成两个 realm —— 与 C2「可组合、不嵌套」一致，不在 API 里塞 `schedules=[...]`。

### 5.3 推：`ctx.inject()`

```python
@dataclass
class RealmContext:
    ...
    context: "ContextSink | None" = None      # 由 RealmManager 注入；未装配时为 None

    def inject(self, text: str, *, source: str | None = None,
               meta: dict[str, Any] | None = None) -> None:
        """排队到下一个安全点；永不打断。"""

    def inject_items(self, items: Iterable[ContextItem]) -> None: ...
```

> 实现细节：`ContextSink` 是定义在 `liorium.realm_base` 里的**结构协议**（只要求
> `queue(items)`）。原稿写的是 `ContextChannel`，但 `liorium-realm-base` 不许 import
> `liorium.agent`（否则成环），所以用协议把类型依赖断在这里；`ContextChannel` 天然满足。

- **同步、非阻塞**：只入队，不 await、不碰历史（物化发生在 `before_model`）。
- 没有 `ContextChannel` 时抛 `RealmError`，**不静默吞**（仓库的"不隐瞒错误"）。
- 后台任务必须先 `ctx.spawn`（架构守卫禁止裸 `asyncio.create_task`）；与当前回合无关的活传 `contextvars.Context()`（`base.py:33-37`）。

### 5.4 装配侧不变

仍然是 `RealmSpec(id="todo")` + `realm_factories={"todo": TodoRealm}`；类本身就是 `(spec) -> Realm` 工厂。新增的只有类属性与一个可选方法。

## 6. 一次到期回合的时序

```
run(text)                                           agent.py
  └─ _run_iter
       index = next(self._turn_seq); turn_id = f"{spec.id}#{index}"
       await scheduler.acquire(...)                       ← 拿执行位（可能排队/被抢占）
       ├─ _reconcile_realms()           ← 可能替换 realm 实例
       ├─ merged_context = user context + take_pending()
       ├─ ★ items = await realms.context_for_turn(TurnInfo(...))       ← 新增
       ├─ ★ if items: channel.queue(items)                             ← 新增
       └─ _drive_turn → execute_turn
            └─ abefore_model → ContextMergeMiddleware._materialize     :67
                 └─ 独立 HumanMessage("<context source=… at=…>")
```

三处顺序都有理由：

1. **在 `_reconcile_realms()` 之后**：reconcile 可能 `apply()` 重建 realm 实例，必须问"当前"的实例，而不是上一回合的旧对象。
2. **在拿到执行位之后**：此刻回合真的要跑了，注入的内容不会在排队期间过期；还在排队（或已被抢占）时不白算。
3. **在 `take_pending()` 之后**：所以本回合的注入**不会**并进用户那句话，而是走形态②成为独立消息（§15 待确认第 4 条）。

> 注：若将来要选形态①（并入 user 消息），把调用挪到 `take_pending()` 之前即可，`loop.py` 仍然不用改 —— 但那样节拍器会跑在本回合开始之前，违反第 2 条，不推荐。

## 7. 实现

### 7.1 改动清单

| 文件 | 改动 | 量级 |
|---|---|---|
| `liorium-realm-base` 的 `realm_base/base.py`（2026-11-07 已从 agent 搬出） | `TurnInfo`；`BaseRealm` 的 3 个类属性 + `context_items()`；`ContextSink` 协议 + `RealmContext.context/inject()/inject_items()`；`RealmRecord.context_due/context_injections` | 已落地 |
| `realm/manager.py` | `RealmManager(context_channel=…)` 并可写进 `ctx.context`；`context_for_turn(turn)` 与 5 个私有助手 | 已落地 |
| `agent.py` | 对调两行构造顺序；`_run_iter` 里 `index` / `TurnInfo` / `context_for_turn` / `queue`；`status()` 透出 | 已落地 |
| `status.py` | `RealmStatus.context_due` / `.context_injections` | 已落地 |
| `realm/__init__.py`、`agent/__init__.py` | 导出 `TurnInfo` | 已落地（2026-11-07 搬家时） |
| `runtime/loop.py`、`runtime/middleware/context_merge.py` | **不改** | 0 |
| 文档 | `EXTENDING.md` §1.1 契约表 + §1.4「周期上下文」；`API.md` 的 `RealmContext`/`RealmStatus`/第五拍语义；`docs/README.md` 术语表 | 已落地 |
| `packages/liorium-todo-realm/` | §12 的完整示例（成包：两个工具 + 四状态 + 每 50 回合提醒） | 已落地 |

### 7.2 `RealmManager.context_for_turn()`

```python
_DUE_KEY = "__context_due__"   # 框架写、realm 不得读写（双下划线前缀约定保留键）

async def context_for_turn(self, turn: TurnInfo) -> list[ContextItem]:
    """按每个 running realm 的节奏收集本回合要注入的 Context；不改历史。"""
    out: list[ContextItem] = []
    for rid in self._order:
        record = self._records[rid]
        if record.state != "running" or record.context is None:
            continue
        cadence = self._cadence(record)                    # 0 ⇒ 不参与
        if cadence <= 0 or not self._origin_matches(record, turn.origin):
            continue
        due = await self._due(record, cadence)             # 惰性从 state 读回 + 夹紧
        if due > 0:                                        # 未到期：只走一格
            record.context_due = due - 1
            continue
        try:
            items = _normalize(await _ask(record.instance, turn), rid)
        except Exception as exc:  # noqa: BLE001 - 一个 realm 坏了不影响别的
            self.log.error("realm context_items 失败，本回合跳过: %s: %s", rid, exc, exc_info=exc)
            continue
        if not items:                                      # 空 ⇒ 不消费周期
            continue
        out.extend(items)
        record.context_injections += 1
        await self._reset_due(record, cadence)             # 相位从成功注入起算并落盘
    return out
```

助手（同文件）：

```python
async def _ask(instance, turn: TurnInfo):
    hook = getattr(instance, "context_items", None)
    if not callable(hook):
        return None
    result = hook(turn)
    return await result if inspect.isawaitable(result) else result


def _normalize(result, rid: str) -> list[ContextItem]:
    if result is None:
        return []
    if isinstance(result, ContextItem):
        return [result]
    if isinstance(result, str):
        text = result.strip()
        return [ContextItem(source=f"realm:{rid}", text=text)] if text else []
    out: list[ContextItem] = []
    for item in result:                     # 非可迭代 / 非法元素 ⇒ TypeError ⇒ 走异常路径
        if item is None:
            continue
        if isinstance(item, ContextItem):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append(ContextItem(source=f"realm:{rid}", text=item))
        else:
            raise TypeError(f"context_items() 返回了不支持的元素: {type(item).__name__}")
    return out


async def _due(self, record, cadence: int) -> int:
    if record.context_due is None:
        stored = await record.context.state.get(_DUE_KEY)
        initial = 1 if self._flag(record, "context_first", False) else cadence
        due = int(stored) if isinstance(stored, (int, float)) else initial
        record.context_due = min(max(due, 0), cadence)     # 节奏调小后不会等旧的大数字
    return record.context_due


async def _reset_due(self, record, cadence: int) -> None:
    record.context_due = cadence
    await record.context.state.set(_DUE_KEY, cadence)
    record.context.state.flush()                            # 每 N 回合一次 IO，可以接受
```

`_cadence` / `_flag` 读 `record.context.params`（优先）再读实例属性；非法值记 `warning` 并按 `0`/`False` 处理。

### 7.3 `agent.py` 的两处接线

```python
# __init__：对调构造顺序，把通道交给 manager
self.context_channel = ContextChannel()
self.realms = RealmManager(
    ...,
    context_channel=self.context_channel,
)

# _run_iter：拿到执行位之后、驱动回合之前
index = next(self._turn_seq)
turn_id = f"{self.spec.id}#{index}"
...
if self._interrupt.is_set():
    raise TurnInterrupted(reason=self._turn_reason or "interrupt")
try:
    items = await self.realms.context_for_turn(TurnInfo(index, turn_id, origin))
except Exception as exc:  # noqa: BLE001 - 节拍器自身不该炸回合
    log.error("realm 周期上下文失败: %s", exc, exc_info=exc)
    items = []
if items:
    self.context_channel.queue(items)
```

单 realm 的隔离已经在 `context_for_turn` 里做了；这里外层再兜一层，只防 manager 自身的 bug。

## 8. 相位与持久化

| 场景 | 行为 |
|---|---|
| 存储位置 | realm 自己的 `JsonStateStore`（`realm_state/<id>.json`；`conversation` scope 按 thread 分桶）⇒ 自动跟随 `state_scope` |
| 写入时机 | **只在成功注入时** `set` + `flush`（每 N 回合一次小文件原子替换，可忽略） |
| 崩溃/重启 | 从 state 读回倒计时；未落盘的递减丢失 ⇒ 相位最多偏移 N 轮（可接受，写进 EXTENDING） |
| `restart(realm_id)` / `apply()` | 实例重建但状态文件不变 ⇒ 相位保留 |
| `reset_dialog()` | `conversation` scope 随状态文件删除一起重置；`agent` scope **保留**（要重置就删文件） |
| `context_every` 改小 | 读回值被 `min(stored, cadence)` 夹紧，不会卡在旧的大数字上 |
| `context_injections` | 进程内计数（不落盘），只给 `status()` 观测 |

## 9. 降级与观测

- 单个 realm 的 `context_items()` 抛异常 ⇒ `log.error` + 本回合跳过 + 下一回合重试；**不**把 `record.state` 改成 `failed`。理由：它是读操作，失败不该摘掉这个 realm 的工具与挂载（`refresh()` 的 `failed` 语义是"装配坏了"，与这里不同）。
- `RealmStatus` 增两个字段（均有默认值，向后兼容）：

  ```python
  @dataclass(frozen=True)
  class RealmStatus:
      id: str
      state: RealmState = "pending"
      error: str | None = None
      tools: tuple[str, ...] = ()
      mounts: tuple[str, ...] = ()
      context_due: int = 0            # 还有几回合到期（0 = 本回合就问）
      context_injections: int = 0     # 本进程成功注入次数
  ```

- `AgentStatus.pending_context` 已存在，可直接看到"排队待物化"的条数。
- **不新增事件类型**（`AGENT_EVENT_TYPES` 不变）：注入发生在模型调用之前，前端看不到额外事件，只在历史里看到消息。
- 日志文案（便于测试断言）：`realm context_items 失败，本回合跳过: <id>: <exc>`、`realm 周期上下文失败: <exc>`。

## 10. 兼容性

1. **不改 `Realm` Protocol**。它是 `@runtime_checkable`，往里加方法会让所有老 realm 的 `isinstance` 失效；新钩子一律走 `BaseRealm` + 鸭子类型（与 `dirty` / `skills` 同一套约定）。
2. `context_every` 默认 `0` ⇒ 现有三个能力包**行为完全不变**，`CacheEpoch` 不因本特性 +1。
3. 注入内容落在消息列表**末尾**，前面的历史前缀照常命中缓存 —— 这正是选它而不是改 `prompt_fragment()` 的核心理由。
4. 架构守卫：`TurnInfo` / 基类声明落在 `liorium-realm-base`（只依赖 core + vfs，**不** import agent），manager / agent 的接线在 `liorium-agent`；新增依赖 `liorium-agent → liorium-realm-base` 是单向的。
5. 能力包现在**允许 import `liorium.agent`**（2026-11-06 决策），所以 `memory/skills/mcp` 可以直接继承 `BaseRealm` 并在自家 realm 里实现 `context_items()`，不需要新的桥接包。

## 11. 测试

新增 `packages/liorium-agent/tests/test_realm_context.py`（沿用现有 fake model + `RealmSpec` 的写法；落地版 15 个，下表的 9 条之外还覆盖 `context_first=False`、`context_origins`、`params` 覆盖、非法节奏按 0、`ctx.inject` 与拉取同批物化、`status()` 计数）：

| 测试 | 断言 |
|---|---|
| `test_fires_every_n_turns` | `context_every=3, context_first=True` ⇒ 注入发生在第 1、4、7 回合；历史里 `<context source="realm:x"` 恰好 3 条 |
| `test_empty_does_not_consume_cycle` | 到期回合返回 `[]` ⇒ 下一回合仍然问；内容一出现立即注入 |
| `test_sync_and_async_hook` | `def` / `async def` 都能用；`str` / `ContextItem` / `list[str]` 都被归一化成正确定源 |
| `test_cadence_survives_restart` | `restart()` 后相位不重置（读回 state） |
| `test_conversation_scope_resets_on_reset_dialog` | `reset_dialog()` 清空相位 |
| `test_hook_failure_degrades` | A 抛异常、B 正常 ⇒ B 照常注入；A 仍是 `running`；`caplog` 有 `error` |
| `test_materializes_as_separate_message` | 消息序列为 `[user, context, ai…]`；`state.context` 有审计记录且 `turn_id` 正确 |
| `test_inject_from_background_task` | `ctx.spawn` 里 `ctx.inject()` ⇒ 安全点物化，不打断、不拆工具组 |
| `test_every_zero_is_noop` | 默认 `0` ⇒ `context_items()` 一次都不被调用（哨兵计数） |

## 12. 示例：`packages/liorium-todo-realm`

> 落地版见 [`liorium-todo-realm`](../../packages/liorium-todo-realm/README.md)（两个工具
> `todo` / `todo_list`、四个状态、`context_every=50`）。下面是**设计时的原稿**，保留它是因为
> "为什么这样设计"仍以这里为准。

```python
"""todo realm：工具 + 只读挂载 + 周期上下文（每 50 回合提醒一次）。"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from liorium.agent import BaseRealm
from liorium.core.types import ContextItem, Text
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.node import VfsMount, VfsNode


class TodoRealm(BaseRealm):
    id = "todo"
    state_scope = "agent"
    context_every = 50      # 每 50 回合给一次机会；RealmSpec.params["context_every"] 可覆盖
    context_first = True    # 开局先提醒一次，别让模型蒙在鼓里 50 轮

    def __init__(self, spec: Any = None) -> None:
        self.params = dict(getattr(spec, "params", {}) or {})
        self.items: list[dict[str, Any]] = []
        self._ctx = None
        self._tools: list[Any] | None = None

    # -- 生命周期 -------------------------------------------------------
    async def start(self, ctx) -> None:
        self._ctx = ctx
        self.items = list(await ctx.state.get("items", []))

    async def stop(self, ctx) -> None:
        await self._save()          # 兜底落盘
        self._ctx = None
        self._tools = None

    # -- 工具 -----------------------------------------------------------
    def tools(self) -> list[Any]:
        if self._tools is None:
            realm = self

            @tool
            async def todo_add(text: str) -> str:
                """加一条待办。"""
                realm.items.append({"text": text, "done": False})
                await realm._save()
                return f"已加入待办：{text}"

            @tool
            async def todo_done(index: int) -> str:
                """把第 index 条待办标记完成（从 1 开始）。"""
                if not 1 <= index <= len(realm.items):
                    return f"没有第 {index} 条待办（共 {len(realm.items)} 条）"
                realm.items[index - 1]["done"] = True
                await realm._save()
                return f"已完成：{realm.items[index - 1]['text']}"

            self._tools = [todo_add, todo_done]
        return list(self._tools)

    # -- 能力面 ---------------------------------------------------------
    def mounts(self) -> dict[str, VfsMount]:
        return {"/todo": VfsMount(prefix="/todo", vfs=self._tree(), readonly=True)}

    def prompt_fragment(self) -> str:
        return "待办（`todo://todo.md`，只读）：用 `todo_add` / `todo_done` 维护。"

    # -- 第五件套：周期上下文 -------------------------------------------
    def context_items(self, turn):
        open_items = [i for i in self.items if not i["done"]]
        if not open_items:
            return []               # 空清单不占周期：下回合继续问
        body = "\n".join(f"{n}. {i['text']}" for n, i in enumerate(open_items, 1))
        return [
            ContextItem(
                source="realm:todo",
                text=f"当前待办（{len(open_items)} 条未完成）：\n{body}",
                meta={"open": len(open_items)},
            )
        ]

    # -- 内部 -----------------------------------------------------------
    async def _save(self) -> None:
        if self._ctx is None:
            return
        await self._ctx.state.set("items", self.items)
        self._ctx.state.flush()     # store 只标 dirty，不 flush 就等于没存

    def _tree(self) -> VirtualFileSystem:
        fs = VirtualFileSystem(owner_id="todo")
        fs.root.add_child(
            VfsNode(
                name="todo.md",
                provider=lambda _n: Text(self._render()),   # 读模型：每次读现算
                description="待办清单",
                should_be_included_in_search=False,
            )
        )
        return fs

    def _render(self) -> str:
        if not self.items:
            return "（空）"
        return "\n".join(f"- [{'x' if i['done'] else ' '}] {i['text']}" for i in self.items)
```

装配：

```python
AgentSpec(
    ...,
    realms=[RealmSpec(id="todo", params={"context_every": 50})],
)
realm_factories={"todo": TodoRealm}   # 类本身即 (spec) -> Realm 工厂
```

一次到期回合里，模型看到的历史形如：

```
user:      今天要写周报
user:      <context source="realm:todo" at="18:20">当前待办（1 条未完成）：
           1. 写周报</context>
assistant: …
```

## 13. 写作糖（已实现，2026-11-07）

`@realm_tool` / `self.state` / `save()` / `on_start/on_stop` 已随 `liorium-realm-base` 落地
（API 见 [该包 README](../../packages/liorium-realm-base/README.md)、[API.md §10](../API.md)）。
§12 的 `TodoRealm` 去掉脚手架后就是最终落地的
[`liorium-todo-realm`](../../packages/liorium-todo-realm/README.md)：

```python
from liorium.realm_base import BaseRealm, realm_tool


class TodoRealm(BaseRealm):
    id = "todo"
    context_every = 50
    context_first = True

    async def on_start(self) -> None:
        self.items = list(await self.state.get("items", []))

    @realm_tool(name="todo")
    async def todo(self, action: str, text: str = "", id: str = "", status: str = "") -> str:
        """增删改待办。"""
        ...

    def context_items(self, turn):
        ...   # §12 原样：没有 pending/processing 就返回 None
```

边界：

- 糖只减样板、不改语义 ⇒ §5–§9 的 API 形状不受影响；
- `context_items()` 的**节拍器已接线**（`RealmManager.context_for_turn()`），默认
  `context_every=0` 的能力包行为完全不变；
- 早前设想"糖必须抽成 `liorium-realm-kit`，因为能力包不许 import `liorium.agent`"——禁令已解除，
  而糖的实体直接落在 `liorium-realm-base`（比 agent 还轻），因此**不需要**额外的 kit 包。

## 14. 风险与取舍

| 取舍 | 说明 |
|---|---|
| 注入会留在历史 | 反复注入同一份 todo 会产生近似重复消息。框架**不做**隐式去重（会破坏"追加不重写前缀"的直觉）；realm 可用 `meta` 带 `digest` 自行决定"没变就不注入"：`if digest == self._last: return []`（返回空即不消费周期） |
| 必须廉价 | `context_items()` 在持有执行位时执行。检索、网络、LLM 一律放 `ctx.spawn` + `ctx.inject`。这条要写进 EXTENDING 的「常见坑」 |
| 同一批 Context 的两种落点 | 若回合在第一次 `before_model` 之前被中断，排队的 items 会留在 pending，下次 `run()` 时以形态① 并入 user 消息。**格式相同**，只是位置不同，属于既有语义 |
| 相位漂移 | 崩溃最多偏移 N 轮；`context_every` 调小时被夹紧，不会长期不触发 |
| 一条节奏一个 realm | 不做 `schedules=[...]`；组合靠多个 realm（与 C2 一致） |
| 计数口径 | `index` 是"第几个 `run()`"，不是"第几次模型调用"，也不是"第几个成功完成的回合"（被中断的也占号）。对节拍无影响 |

## 15. 待拍板

| # | 问题 | 本提案默认 | 影响 |
|---|---|---|---|
| 1 | 「一轮」指什么？ | 一次 `run()`（opening 到 `done` 的整个模型/工具循环） | 若改指"一次模型调用"，节拍器要挂到 `before_model`，长工具链里会来得很快 |
| 2 | 到期但没有内容 | 下回合立即补发（`due` 停在 0），不跳过 | todo 场景必需；若改跳过，空清单会白等一整个周期 |
| 3 | 相位跨进程重启 | 保留（存 realm state） | 若想"每次启动重新计 N 轮"，则纯内存、不落盘，可省掉每 N 轮的 IO |
| 4 | 注入形态 | 独立 `<context>` user 消息（形态②） | 若改为并入 user 消息，节拍器要挪到 `take_pending()` 之前（那样会在排队期间算，不推荐） |
| 5 | `params` 覆盖键名 | `context_every` / `context_first`（框架级） | realm 想要更友好的别名（如 `inject_every`）可在自己的 `__init__` 里映射，框架不认别名 |

## 16. 验收（Definition of Done）

- [x] `context_every` / `context_first` / `context_origins` / `context_items()` 在 `BaseRealm` 上可用；`TurnInfo` 已从 `liorium.agent` 导出。
- [x] 默认 `0` 时现有测试**全部不变地通过**（尤其 `test_turn_loop.py`、`test_realm_v11.py`、`test_compact_cut_with_injected_context.py`）。
- [x] §11 的测试全绿（落在 `packages/liorium-agent/tests/test_realm_context.py`，15 个）。
- [x] 历史里注入消息是独立 user 消息，`state.context` 的 `turn_id` 与事件 `turn_id` 一致。
- [x] `packages/liorium-todo-realm` 跑通"加待办 → 重启 → 相位保留 → 到期注入"（端到端见 `tests/test_v11_integration.py`）。
- [x] `EXTENDING.md` 契约表新增两行 + §1.4「周期上下文」（含"必须廉价"的坑）；`API.md` 的 `RealmContext` / `RealmStatus` / 第五拍语义同步；`docs/README.md` 术语表新增「周期上下文」；本文删除「未实现」标记。
