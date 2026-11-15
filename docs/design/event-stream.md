# 事件流原生：`push` / `stream` / 常驻 worker

> 2026-11-12 实现（定稿 `__dev__/Liorium Event 事件流原生 定稿.md`）。本文是这套设计的
> **权威文档**：签名 / 字段 / 默认值以源码为准（`agent.py`、`runtime/{hub,inbox,worker,loop}.py`），
> 与代码冲突时以代码为准。

## 1. 一句话

`LioriumAgent` 是**自持 worker + 入站队列 + 只读事件流**：调用方只能 `push()` 投递、
`stream()` 观察、`inject()` 走底层通道；回合的创建 / 调度 / 取消全部由 Agent 内部裁决。
`run()` 已删除 —— 不再存在"调用方 `async for` 驱动回合"这回事。

```
调用方 ──push()──▶ Inbox（bucket/flags/timeout/id，同 id 原位替换）
                     │ 唤醒
                     ▼
        TurnWorker（常驻、单执行位）──▶ execute_turn（一个回合）
                     │ 每帧
                     ▼
        EventHub（put_nowait 扇出，永不阻塞）──▶ stream() × N（只读、不回放）
```

## 2. 三条设计原则

1. **worker 拥有回合**：worker 在 `start()` 时拉起、`close()` 时收；它取条目、跑回合、
   补终态帧。调用方无法"开始一个回合"，只能"投递一个事件"。
2. **发布者永不等待消费者**：`push()` 与每帧发布都是 `put_nowait`；订阅者慢 / 抛错 / 被 GC
   只影响它自己（订阅者队列**无界**，见 §6）。
3. **取消范围由 worker 裁决**：调用方只说"何时投递"（bucket）和"这条回合能不能被抢"
   （`Flag.EXCLUSIVE`）；"能不能丢已经说出去的话"由 worker 按外化状态裁决（§5.2）。

## 3. 公共 API

```python
LioriumAgent(..., batch_window: BatchWindow | None = None)   # None = inbox.py 默认值；不进 AgentSpec / 指纹
```

| 方法 | 同步/异步 | 返回 | 语义 |
|---|---|---|---|
| `start()` | async | `None` | 起 realm / 建图 / 拉起 worker；**不调模型**；幂等；`close()` 后再 `start()` 抛 `TurnError` |
| `close()` | async | `None` | 取消当前回合（发 `interrupted(close)`）/ 丢弃全部 pending（**不发帧**）/ 停 worker / 关 hub；幂等 |
| `push(text, *, id, bucket=Bucket.MAJOR, flags=Flag.NONE, origin="user", timeout=None, context=())` | **同步** | `None` | 入队即返回；`id` 必填（流上唯一的关联键）；未 `start()` / 已 `close()` 抛 `TurnError`；`Flag.AUTO` + `Bucket.IMMEDIATE` 抛 `ValueError` |
| `stream(*, ids=None)` | **同步** | `AsyncIterator[Event]`（实际是 `Subscription`） | 只构造订阅对象，**注册发生在 `__aiter__`**；可开多个、不回放（§6） |
| `inject(items)` | **同步** | `None` | 直写 `ContextChannel`：不进 Inbox、不参与 bucket/timeout/合批、不发 `input` 帧、**永不打断** |
| `interrupt(*, reason="user")` | async | `bool` | 取消当前回合，**队列保留**；没有在跑的回合返回 `False` |
| `apply(spec, *, force=False)` | async | `ApplyResult` | 差量热重载；**插队**执行（§5.5） |
| `refresh_realms(*, force=True)` | async | `bool` | 调用方主动刷新 realm；插队执行 |
| `set_realm_enabled(realm_id, enabled)` | async | `bool` | 运行期开关一个已装配 realm；插队执行 |
| `reset_dialog()` | async | `None` | 清空对话 + 重写 system 段；执行前清空 Inbox 并逐条发 `input.dropped` |
| `status()` | 同步 | `AgentStatus` | 可序列化快照 |

`status()` 关键字段：`running`（是否有回合在跑）、`turn_id`（当前回合 id，空闲 `None`）、
`queue_depth`（Inbox 全部待处理条目数）、`pending_context`（`ContextChannel` 待物化数）、
`pending_by_bucket`（`tuple[tuple[str, int], ...]`，桶名大写、四个桶恒出现，供适配器看积压构成）。

## 4. 入站：Bucket / Flag / 合批 / timeout

### 4.1 `Bucket` 只表达"何时投递"

| bucket | 何时投递 | 隐含形态 | 会开回合 |
|---|---|---|---|
| `IMMEDIATE` | 立刻；可取消在途且未外化的模型轮 | user_message | 会 |
| `MAJOR` | 排队，等当前回合结束 | user_message | 会 |
| `BATCHED` | 参与合批窗口，**窗口到期后开回合**（整批合成一条 user 消息） | user_message | 会（窗口到期后） |
| `ENVIRONMENT` | 不触发投递、不刷新合批计时器；回合进行中在安全点按 `env_interval`（默认 30 s）节流投递 | context | **不会** |

`Bucket.opens_turn` 就是"不是 `ENVIRONMENT`"。**桶之间没有优先级阶梯**：
`IMMEDIATE` 只是插队 + 可在安全点接管当前回合，其余一律 FIFO。

`ENVIRONMENT` 的投递时机有三条：回合开始时整批搭车（形态①）、批回合成回合时一起走、
以及**回合进行中**在安全点按 `env_interval` 取用（形态②，独立 `<context>` user 消息）。
节流按"上一次投递"算：`Inbox.take_environment()` 未满间隔就返回 `[]` 且队列一格不动；
首次投递不受限（`last_at is None` ⇒ 立即可投），`env_interval <= 0` 表示每次安全点都投。
回合开始那条路径**不受**节流（回合开头是快照点），但会重置计时器。空闲期不投递、
不唤醒：没有任何定时器，`next_wakeup()` 也不把环境算作唤醒源。

### 4.2 `Flag`（`IntFlag`，默认 `NONE`）

| flag | 位 | 语义 |
|---|---|---|
| `AUTO` | 4 | 运行时决定形态：idle → 开回合；busy → 直接注入当前回合的 context（安全点物化）。**只对 `MAJOR` 有意义**；与 `IMMEDIATE` 互斥（`ValueError`） |
| `EXCLUSIVE` | 1 | 本条开启的回合拒绝被 `IMMEDIATE` 抢占；**不挡**显式 `interrupt()` 与 `close()` |
| `DROPPABLE` | 2 | Inbox 深度 `>= QUEUE_MAX`（50）时立即丢弃，发 `input.dropped` |

`AUTO` + `MAJOR` + 忙碌这条路径**不入 Inbox**（不占队列、不产生 pending、没有 `expired`），
只发一条 `input(accepted)`，然后等安全点物化。

### 4.3 合批窗口（只对 `BATCHED` 生效）

```
deliver_at = min(first_at + max_age, max(first_at + min_age, last_at + quiet_gap))
默认 quiet_gap=2.5s  min_age=0s  max_age=15s  max_size=100（到量立即开回合）
```

参数位置：`runtime/inbox.py::BatchWindow`（frozen dataclass）+ `LioriumAgent(batch_window=...)`；
**不进 `AgentSpec`**（spec 字段参与指纹，改 2.5 秒的窗口不该触发 `apply()` 重建图）。
`ENVIRONMENT` 不参与上述计算：它在回合开始时搭车（包括批回合），回合进行中另按
`env_interval` 节流投递（见 §4.1）。

窗口到期后**按 `AUTO` 处理**：没有回合在跑就直接开一个新回合（整批各条 `text` 按队列序换行
拼成一条普通 user 消息，各自的 `context` 与搭车的 `ENVIRONMENT` 并入同一条）；有回合在跑就
留在 Inbox 排队，等它结束再开；已有 `MAJOR` / `IMMEDIATE` 在排队则不单独开，整批按形态①并进
那一条的回合。所以**不存在"攒了批却永远不说话"**的档位（参照实现的 `debounce` 也是到期即投递、
投递即开回合）。

### 4.4 同 id 原位替换

`id` 必填，是流的关联键（`push()` 没有返回值，这是唯一关联手段）。pending 中已有同 `id` ⇒ 新条目
**原位替换**旧条目：占据旧条目的队列位置（不挪队尾）；继承合批锚点 `first_at` / `last_at`
（**不刷新** `last_at`，高频状态更新不会饿死批次）；`timeout` 从**本次 push 时刻**重新起算；
bucket / flags 按新条目自己的走。已被消费（已物化）的 `id` 再次 push ⇒ 视为普通新条目，
**不报错**（`delivered` 去重，不会重复发 `input.delivered`）。

### 4.5 `timeout` = 消费截止时间（惰性扫描）与 `QUEUE_MAX`

- `timeout` 起点 `time.monotonic()`；默认 `None` = 永不过期（库不猜业务）。语义：条目被
  **物化进消息之前**到期即作废，**永不物化**，发 `input.expired`；已物化的条目不再有过期概念；
  同批其它条目照常投递（不连坐）；
- 执行点是**惰性**的：worker 取条目前扫一遍、空闲时按 `inbox.next_wakeup()`（最近的 deadline
  或合批 `deliver_at`）定时唤醒后再扫，**不额外起定时器 task**。`QUEUE_MAX = 50` 是 Inbox 深度
  上限，只有带 `Flag.DROPPABLE` 的条目会因此被立即丢弃（`input.dropped`）。

## 5. 回合调度

### 5.1 单执行位、一条条目一个回合

- 单执行位串行：任意时刻最多一个回合在跑（`ExecSlot.occupied`）；
- `IMMEDIATE` 排在所有 `MAJOR` 之前，同档 FIFO（`Inbox.peek_turn_entry()`）；**一个回合由
  一条开回合条目驱动**（不做跨条目的回合级合并），`start.inputs` 永远是列表；合批窗口到期时
  整批先合成**一条**条目，所以一个批回合的 `start.inputs` 会是整批 id（队列序）；
- 回合开始时 Inbox 里所有 context 类条目（未到期的 `BATCHED` / `ENVIRONMENT`）并入本回合的
  user 消息（形态①）；回合进行中到达的在安全点物化成独立消息（形态②）——
  `inject()` 的内容，以及按 `env_interval` 节流取到的 `ENVIRONMENT`（§4.1）。

### 5.2 `externalized`（已外化）

判据：**已经发出过正文 `delta` 帧，或已经发起过工具调用**；`reasoning_delta` 不算。未外化 ⇒
可以整轮丢弃，等价于"换条消息重来"；已外化 ⇒ 已发出的帧收不回来，只能停轮并保留前缀。实现上
两种情形走**同一条路径**（切轮 → `round_aborted` → 同一 `turn_id` 换消息），`externalized`
当前只用于记账与日志，不改变分支。

### 5.3 `IMMEDIATE` 的四个相位

| 回合此刻处于 | 未外化 | 已外化 |
|---|---|---|
| 排队中（未开跑） | 插到队首（下一次 `peek_turn_entry()` 先取它） | 同左 |
| 模型轮 streaming | 取消本轮、同一回合换新消息重新请求 | 停掉本轮、保留已发文本，同一回合注入新消息，进下一轮 |
| 工具执行中 | **不取消**：等这组工具跑完，在下一个安全点注入 | 同左 |
| `before_model`（安全点） | 直接注入 | 直接注入 |

**`IMMEDIATE` 永不结束回合、不发终态帧**（`turn_id` 不变）。模型轮被切掉时补一条**非终态**
`round_aborted` 供 UI 标记半句话，然后同一回合内用新消息续跑（并补 `input(delivered)`）；
`close()` / `interrupt()` 直接产出 `interrupted` 终态帧，**不补** `round_aborted`。

### 5.4 安全点

安全点 = **模型调用之前**（`ContextMergeMiddleware.before_model`）。物理约束：
`AIMessage(tool_calls)` 与其全部 `ToolMessage` 必须紧邻，中途插入 `Human` 会被 provider 400，
所以"等安全点"的真实延迟 = 当前工具组剩余执行时间（最慢的那个工具）。抢占判定发生在两个图
事件之间：worker 把"有待接管条目"的信号交给 `execute_turn`，由它与 `stream.__anext__()` 赛跑
—— 模型还没吐第一个 token 时也能立刻被唤醒裁决；工具里 `await` 时也会收到信号，但只记为
"待接管"、不打断工具，等这组工具跑完再接管。

### 5.5 `EXCLUSIVE` 与维护插队

`EXCLUSIVE` 只拒绝 `IMMEDIATE` 抢占：`IMMEDIATE` 条目正常入队（插到 `MAJOR` 之前），等这个
回合结束再跑；不挡 `interrupt()`，也不挡 `close()`。

`apply` / `refresh_realms` / `set_realm_enabled` / `reset_dialog` 都通过 `ExecSlot` **插队**：
等当前回合结束、在取下一个 Inbox 条目之前执行，期间 worker 不取新条目；调用方 `await` 的是
"真正执行完"的结果；从回合任务内部调用（例如工具里）会抛 `TurnError`（会自锁）。
`reset_dialog()` 执行前清空 Inbox 全部条目并**逐条**发 `input.dropped`（它们属于被清掉的对话）；`close()` 相反：丢弃 pending **不发帧**。

## 6. `stream()` 契约

- **可开多个**，彼此独立，每个都是完整视图（不是"分片"）；
- **不回放**：只有订阅之后发布的事件，没有历史、没有快照、没有 `from_cursor`；
- **订阅发生在 `__aiter__`**（`async for` 开始那一刻），不是 `stream()` 调用时 ——
  `s = agent.stream()` 之后不迭代不会泄漏队列；注册与 `aclose()` 都幂等；
- 每订阅者一条**无界队列**；发布是 `put_nowait`，**永不等待**、永不因发布抛错；
- **不做相邻 delta 合并**（每个事件单独一帧）；订阅者 `aclose()` / 抛错 / 被 GC → 只摘掉它自己；
- **没人订阅时 Agent 照常跑回合**（数据在 checkpoint 里，不在流里）；
- `ids=[...]`：只产出 `start.inputs` 与该集合有交集的回合的帧（含其终态帧），以及这些 `id`
  自己的 `input` 帧；不带 `turn_id` 的帧（`compact_*`、空闲时的 `realm`）归属当前回合；
- `close()` 之后订阅立即结束；`close()` 之前已入队的帧（例如 `interrupted(close)`）仍会被读完。

## 7. 事件类型总表（14 种）

保留 9 种：`start`、`delta`、`reasoning_delta`、`tool_start`、`tool_result`、`compact_start` /
`compact_delta` / `compact_done`、`done`；新增 4 种见下表。

| type | 字段 | 时刻 / 终态 |
|---|---|---|
| `start` | `turn_id`、`origin`、`inputs: [id, ...]` | 回合开始；每回合恰好一条 |
| `delta` / `reasoning_delta` | `content` | 正文 / 思考增量；**只有 `delta` 算外化** |
| `tool_start` / `tool_result` | `tool_name`、`args`/`output`、`call_id` | 工具开始 / 结果成对；`call_id` = LangGraph `run_id`；**算外化**；工具抛异常也发 `tool_result`，正文 `[工具执行出错] 类名: 消息` |
| `compact_start` / `compact_delta` / `compact_done` | 无 / `content` / `ok` | 压缩；`ok=False` 表示压缩失败但回合继续 |
| `round_aborted` | `turn_id`、`reason`（v1 只有 `"preempted"`） | **非终态**：模型轮被 `IMMEDIATE` 切掉 |
| `interrupted` | `turn_id`、`reason`（`user` / `close`，或调用方自定义） | **终态**；抢占不走这条（见 `round_aborted`） |
| `error` | `turn_id`、`error: "类名: 消息"` | **终态**；traceback 只进日志 |
| `input` | `id`、`state` ∈ `accepted` / `delivered` / `expired` / `dropped`、`turn_id`（仅 `delivered` 带，且恒不为空） | 入站生命周期，不属任何回合正文 |
| `done` | `turn_id` | **终态**；**不带 `final_text`**（文本已经流过 `delta`） |
| `realm` | `realm`（谁发的）、`name`（事件名）、`data`、`turn_id`（回合内发出才带） | realm 主动推送；**非终态**、**不开回合**、不落盘（2026-11-14） |

**硬规则**：每个回合**恰好一条**终态帧（`done` / `interrupted` / `error`），且是该回合**最后**一条事件；`interrupted` / `error` 由 worker 补齐，`done` 由 `execute_turn` 产出。

**`realm` 帧的三条边界**（2026-11-14）：只有 realm 自己能发（`ctx.emit`，见
[EXTENDING §1.5](../EXTENDING.md)），宿主侧不提供入口；**永不是终态**，类型校验天然挡住
`done` / `interrupted` / `error`；**永不开回合**，空闲时只到无过滤订阅者，`ids=[...]` 的订阅者
按"不带 `turn_id` 的帧归属当前回合"处理（与 `compact_*` 同规则）。

**与工具帧的相对顺序不保证**：`ctx.emit` 在工具体内直接 `hub.put_nowait`，而
`tool_start` / `tool_result` 由事件翻译器从 LangGraph 事件流产出，两条路径独立调度 ——
实测工具里 emit 的帧可能排在它的 `tool_start` **之前**（不是之后）。宿主若要按因果排序，
用 `realm` 帧自己的 `turn_id` + 数据里的业务 id，别假设"工具帧先到"。

## 8. 生命周期

| 场景 | 行为 |
|---|---|
| `start()` 前 / `close()` 后 `push()` | 抛 `TurnError` |
| `close()` | 回合在跑 → 取消并发 `interrupted(close)`；队列非空 → 丢弃全部 pending，**不发帧**；停 worker；关 hub |
| `interrupt()` | 只取消当前回合，**队列保留**（下一次取条目继续跑） |
| 维护操作 | 插队（§5.5）；回合内重入抛 `TurnError` |

## 9. `inject()`：保留的底层通道

直接写 `ContextChannel`：不经 Inbox、不参与 bucket/timeout/合批、**不发 `input` 帧**；在下一个
安全点（或下一回合开始）物化；用途是 realm 内部 `ctx.inject()` 的宿主侧对应物与测试旁路。
语义不变：**永不打断** —— "叫停"用 `interrupt()`，"插话"用 `push(bucket=Bucket.IMMEDIATE)`。

## 10. 明确不做（YAGNI）

pending 持久化 / cursor / 崩溃补投（内存队列，重启即丢）；事件回放 / `subscribe(from_cursor)`；
工具的 `interruptible` 属性（v1 工具一律不可中断）；播放闸门与节奏控制（在适配器侧）；Core 级
合批；相邻 delta 合并、**有界队列**、`lagged` 帧；candidate / deferred 投影；旧的调用级优先级、
抢占标志、拥塞丢弃异常与优先级堆调度器（删除记录见 [`scheduler.md`](scheduler.md)）。

## 附录 A：bucket 选择指南（写给调用方）

```
这件事需要模型做点什么吗？
├─ 需要，且等不了当前回答说完  ──────────────▶ IMMEDIATE
├─ 需要，但可以等这句说完      ──────────────▶ MAJOR
├─ 需要，但想攒一小会儿一起说  ──────────────▶ BATCHED（合批窗口到期后自己开回合）
└─ 不需要，只是让它在场        ──────────────▶ ENVIRONMENT
```

| bucket | 一句话 | 开回合 | 典型产物 | 反模式 |
|---|---|---|---|---|
| `IMMEDIATE` | 现在就说，正在说也停下来说 | ✅ | 用户插话、SC / 上舰、紧急告警 | 当"高优先级队列"用（它会切在途模型轮） |
| `MAJOR` | 等这句说完 | ✅ | CLI 每一行、子代理请求、定时提醒 | 推高频环境事件（每条一次模型调用） |
| `BATCHED` | 攒一会儿一起说 | ✅（窗口到期后） | 批量 webhook、邮件摘要、合并后的弹幕转述 | 把窗口调到极小去腌高频事件（每次到期一次模型调用） |
| `ENVIRONMENT` | 不用专门说，顺路带上 | ❌ | 观众数、正在听的歌、桌面状态 | 塞"需要被回应"的用户发言；或把 `env_interval` 调到极小当轮询用 |

两条边界：**只有 `ENVIRONMENT` 永不开回合**（`BATCHED` 在合批窗口到期后开）；
**桶之间没有优先级阶梯**。`ENVIRONMENT` 不会提升为开回合的依据，唯一的变化是
它能在回合进行中按 `env_interval` 补投一次（安全点物化，不打断模型）。

## 附录 B：适配器侧约定（不属于 Core，但必须遵守）

VTuber / 直播场景的"要不要开口"仍由**适配器**的闸门决定，且发生在 `push()` **之前**；
"多条弹幕攒一小会儿一起说"可以直接用 `bucket=BATCHED` + 合批窗口表达（窗口到期即开回合）。

- 闸门看 `status().pending_by_bucket` 判断积压构成（该回的有几条、可丢的有几条）；
- 适配器慢 ≠ Agent 慢：Agent 不等任何人消费，适配器必须自己持续消费自己的 `stream()`；
- 想不被打断 → `flags=Flag.EXCLUSIVE`；想表达"晚了就没意义" → `timeout=...`（过期即作废）。

## 测试与验收

| 文件 | 关注点 |
|---|---|
| `packages/liorium-agent/tests/test_event_hub.py` | 多订阅者各收全量、不回放、慢订阅者不阻塞、异常隔离、`close()` 结束订阅 |
| `packages/liorium-agent/tests/test_inbox.py` | 桶映射、同 id 原位替换与锚点继承、`timeout` 过期（patch monotonic）、`DROPPABLE`、`AUTO` 与 `IMMEDIATE` 互斥、四旋钮合批 |
| `packages/liorium-agent/tests/test_worker.py` | FIFO、`IMMEDIATE` 四相位、`EXCLUSIVE`、维护插队、`reset_dialog` 清队、终态帧恰好一次、`close()` 发 `interrupted(close)` |
| `packages/liorium-agent/tests/test_buckets.py` | 已取代 `test_scheduler.py` / `test_turn_flags.py`：Agent 级 bucket/flags 行为与终态帧 |
| `packages/liorium-agent/tests/test_realm_capabilities.py` | 2026-11-14：`ctx.emit` / `ctx.push` / `ctx.interrupt` / `ctx.turn`、`realm` 帧、六个只读钩子的顺序与容错 |

跨包金样 `tests/test_event_contract.py`：14 种事件类型，`round_aborted` / `interrupted` /
`error` / `input` 的字段冻结。
