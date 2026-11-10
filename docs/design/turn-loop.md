# 主 Agent 回合循环语义（Liorium 实现基线）

只描述**现状代码的运行时语义**，不含改造方案；标识符、事件 `type`、日志文案一律照抄源码。
`agent/runtime/turn/{gate,state,usage,session_stats,presence,output_store}.py`、`api/chat/chat.py`、`core/message.py`、`agent/prompt/seed.py`。

## 1. 一个回合的端到端执行

### 1.1 入口签名（逐字）

```python
# runtime/invoke.py
async def _invoke_agent_unlocked(target_agent, payload, config=None)
async def invoke_agent_locked(target_agent, payload, config=None)
async def _stream_agent_producer(target_agent, payload, config, abort_event, queue, stream_state: _StreamState)
async def stream_chat_agent_events(target_agent, payload, config=None, *, abort_event: asyncio.Event | None = None)
async def stream_silent_reply(payload: dict, config=None) -> str
# runtime/scheduler.py
def submit(request: AgentRequest) -> TurnHandle
async def run_exclusive(self, factory, *, label: str = "exclusive", owner: Any = None) -> Any
# runtime/rebuild.py
async def rebuild_runtime(*, reset_dialog: bool = False, no_initial_chat: bool = False)
# api/chat/chat.py
async def _run_agent_stream(websocket: WebSocket, text: str, agent=None, origin: str = 'user', turn_id: str | None = None) -> str
async def _run_slash_command_stream(websocket, factory) -> None;  async def _run_compact_command(websocket) -> None
async def _dispatch_runtime_command(websocket, factory) -> tuple[bool, str | None]
```

- `invoke_agent_locked` = `acquire_agent_lock("invoke_agent_locked")` → `_invoke_agent_unlocked` → `finally: release_agent_lock()`；**没有** `lock_held` 这类布尔参数。
- `stream_chat_agent_events` 是 **async generator**；`payload` 典型为 `{"messages": [{"role": "user", "content": text}]}`。
- 两入口默认 config 相同：`{"configurable": {"thread_id": state.THREAD_ID}, "recursion_limit": 300}`（`THREAD_ID == "main"`）。

### 1.2 非流式调用与 429 退避

`_invoke_agent_unlocked`：`max_attempts = 3`；每次先 `payload = await _apply_llm_request_pre(payload)` → `res = await target_agent.ainvoke(payload, config)`；
异常时若 `is_rate_limit_error(e)` 且 `attempt < 3` → `log.warning("429 限流，重试 attempt=%d/%d", ...)` + `await _sleep_backoff(attempt)`，否则 `raise`；耗尽 → `raise RuntimeError("agent invoke retries exhausted")`。
`_sleep_backoff(attempt)`：`base = 0.8 * (2 ** max(0, attempt - 1))`、`jitter = random.uniform(0.0, 0.35)`、`await asyncio.sleep(min(3.0, base + jitter))`。
`is_rate_limit_error`：小写化后含 `"429"`，或同时含 `"rate"`/`"limit"`，或含 `"bad_response_status_code"`。

插件前置 hook（`_apply_llm_request_pre`）：`plugin_manager is None` 或 messages 为空 → 原样返回；`results = await pm._call_pluggy_hook("llm_request_pre", messages=messages, ctx=None)`，
**第一个非空 list 胜出** → `{**payload, "messages": r}`；抛异常吞掉原样返回。流式路径每次重试前也调一次。

### 1.3 `astream_events(version="v2")` → 内部事件

事件 dict 字段：`event` / `data` / `tags` / `name` / `run_id`；非 dict 与未列出的 `event` 名一律忽略。每条事件到达后先 `scheduler.touch()`（D7 僵死心跳），
再 `if abort_event and abort_event.is_set(): raise asyncio.CancelledError("User interrupted")`。

| LangGraph 事件 | `data` | 内部事件（入队 dict） |
|---|---|---|
| `on_chat_model_stream` | `chunk` | 非 AI chunk 丢弃；否则 `{"type":"reasoning_delta","content":...}`（若有）→ `{"type":"delta","content":...}`（若有） |
| 同上且 `COMPACT_TAG in tags` | `chunk` | 首次 `{"type":"compact_start"}`，之后 `{"type":"compact_delta","content":...}`；**绝不产生 delta** |
| `on_chat_model_end` | — | 压缩调用 → 关闭压缩卡片；主模型 → 忽略 |
| `on_tool_start` | `input` | `{"type":"tool_start","tool_name":str,"args":dict,"call_id":str}` |
| `on_tool_end` | `output` | `{"type":"tool_result","tool_name":str,"output":str \| dict,"call_id":str}` |
| （取锁成功） | — | `{"type":"lock_acquired"}` |

- `event_name = str(event.get("event") or "").strip().lower()`；AI chunk 用 `is_ai_message_chunk(chunk)` 判定。
- `tool_name = str(event.get("name") or data.get("name") or "tool").strip()`；`call_id = str(event.get("run_id") or "")`（**用 LangGraph `run_id` 当工具调用 id**，不是模型的 `tool_call_id`）。
- `args = normalize_tool_args(data.get("input"))`；`output = tool_value_to_text(data.get("output"))`。

压缩分流：`COMPACT_TAG = "liorium_compact_summarizer"`；压缩调用以 `config={"tags": [COMPACT_TAG], "metadata": {"liorium_event": "compact"}}` 发起。
生产者本地 `compact_active: bool` + `_close_compact(*, ok: bool = True)`（为真时置假并投 `{"type":"compact_done","ok":ok}`）；
关闭时机＝压缩调用 `on_chat_model_end`、主模型开始输出、异常/取消（`ok=False`）、等锁失败（`ok=False`）、正常结束（`ok=True`）。摘要文本绝不进正文 `delta`。

### 1.4 内容提取（四种格式）

```python
message_content_to_text(c)   # None->""; str->原样; list->只拼 dict 且 type.lower()=="text" 的 block["text"]（str 项直接拼）; 其它->str(c)
is_ai_message_chunk(m)       # m.type.lower()=="ai" 或类名含 "aimessage"
tool_value_to_text(v)        # None->""; dict->原样(交给外层 json.dumps 只序列化一次); str->原样; 其它->json.dumps(ensure_ascii=False, indent=2)
normalize_tool_args(p)       # dict 原样; str: 空->{}, 解析失败->{"input": text}, 非 dict 结果->{"input": decoded}; None->{}; 其它->{"input": p}
extract_reasoning_text(chunk)  # ① additional_kwargs 依次取 "reasoning_content"/"reasoning"/"think"（非空 str 命中即返回）
                               # ② content 为 list：块 type ∈ {"reasoning","thinking","reasoning_content","thought"}，
                               #    块内依次取 "reasoning"/"thinking"/"text"（命中 break），否则看 "summary"
                               #    （元素 dict 取 item["text"]，str 直接拼）；全部拼接返回，都不命中返回 ""
```

| 格式 | 思考文本落点 | 正文落点 |
|---|---|---|
| OpenAI Chat 兼容（DeepSeek R1/Qwen） | `additional_kwargs["reasoning_content"]` | `content: str` |
| OpenAI Responses | content 块 `{"type":"reasoning","summary":[...]}` | content 块 `{"type":"text",...}` |
| Anthropic | content 块 `{"type":"thinking","thinking":...}` | content 块 `{"type":"text",...}` |
| Gemini | content 块 `{"type":"thinking","thinking":...}` | content 块 `{"type":"text",...}` |

### 1.5 生产者 → 消费者（队列 + 终结标记）

`_StreamState.__slots__ = ("terminal_sent",)`（终结事件恰好一次）；`_StreamFailed(error)` 包装异常由消费者重抛；`_STREAM_DONE = object()` 是队列终结标记。
生产者是独立任务 `create_task(..., name="stream_agent_producer")`，因此**锁的持有时间只覆盖 LLM 会话本身**，与 websocket 发送节拍无关。所有退出路径必过 `_finish(item)`：已投过则 return，否则置位并 `await queue.put(item)`。

```
for attempt in 1..3:
    acquire_agent_lock("stream_chat_agent_events")   # RuntimeError -> _close_compact(False) + _finish(_StreamFailed(e)) + return
    queue.put({"type": "lock_acquired"})            # 每次尝试都投一次（429 重试会重复出现）
    try: <astream_events 循环>; _close_compact(); _finish(_STREAM_DONE); return
    except asyncio.CancelledError: _close_compact(ok=False); _finish(_StreamFailed(asyncio.CancelledError("User interrupted"))); raise
    except Exception as e:
        _close_compact(ok=False)
        if is_rate_limit_error(e) and attempt < 3: log.warning(...)      # 落到循环底部重试
        else: _finish(_StreamFailed(e)); return
    finally: release_llm_stream_lock()               # 在 for 内：每次尝试结束都释放
    await _sleep_backoff(attempt)
_finish(_StreamFailed(RuntimeError("agent stream retries exhausted")))
```

消费者：`queue.get()` 循环，`_STREAM_DONE` → return；`_StreamFailed` → `raise item.error`；否则 yield。
兜底 `producer.add_done_callback(_on_producer_done)`：未投终结标记就结束（等锁被外部 cancel、未启动即取消、异常逃逸）→
`queue.put_nowait(_StreamFailed(RuntimeError("agent stream producer ended without a terminal event")))`。
`finally`：`if not producer.done(): producer.cancel()`，再 `await producer` 并 `except asyncio.CancelledError: pass`。
两个副作用须知：**429 重试先放锁再重新取锁，并再投一次 `lock_acquired`**；回合内 `release_llm_stream_lock()` **提前放掉整个回合的锁**（§2.3）。

### 1.6 chat 侧翻译

| 内部事件 | 前端 payload | 备注 |
|---|---|---|
| `{"type":"lock_acquired"}` | `{"agent_id":"main","type":"streaming"}` | 唯一翻译点，清除前端“排队等待中” |
| `reasoning_delta` / `delta` | `{"agent_id":"main","type":...,"content":...}` | `delta` 另累计 `reply`；均过 hook |
| `compact_start/delta/done` | `dict(event)` + `agent_id="main"` | **不并入 `reply`**（故不进 TTS） |
| `tool_start` / `tool_result` | `dict(event)` + `agent_id="main"` | 过 hook |
| 正常结束 | `{"agent_id":"main","type":"done","turn_id":<turn_id>}` | 发前先 `turn_gate.arm()` |
| `CancelledError` + `turn.force_interrupting` | `{"agent_id":"main","type":"done","forced":True}` | 插话强制打断 |
| 其它 `CancelledError` | `{"agent_id":"main","type":"interrupted"}` | |
| 其它异常 | `{"agent_id":"main","type":"error","error":format_chat_error(e)}` | |

插件 hook `agent_event_sent(event=payload, current_history=current_history, ctx=None)`：`reasoning_delta` 返回 `"__IGNORED__"`/`"__REMOVED__"` → 丢弃，dict → **替换**；
`delta`/`compact_*`/`tool_*` 返回 `None` → 丢弃，dict → 替换；`done` 只调用不采用返回值。实际发出的 payload append 进 `current_history`（`done` 不 append）。

`_run_agent_stream` 固定顺序：① `turn = turn_state.get_turn_state(websocket)`；`abort_evt = turn.begin_turn()`（换新 `asyncio.Event`）；
② `text = await _apply_plugin_message_hooks(text, origin)`，返回 `"__IGNORED__"` → 只发 `done`（**无 turn_id**）并 `return ""`；③ `vng_mode.ensure_vng_notice(...)`（失败只记日志）；
④ 驱动 `stream_chat_agent_events(...)` 且 `finally: await agen.aclose()`；⑤ `schedule_memory_record_sync(text, reply)`（仅 `conf.KB_ENABLED` 且两段非空）；
⑥ `if turn_id: turn_gate.arm(turn_id, len(reply or ""), websocket)`（**必须在 done 之前**）；⑦ 发 `done`。
`stream_silent_reply(payload, config=None)`：仍走 `stream_chat_agent_events`，只累加 `type=="delta"` 的 `message_content_to_text(content)`（理由：事件流持续喂 `touch()`，并统一锁超时/429 语义）。

## 2. 锁与调度

### 2.1 锁对象、诊断字段与获取

```python
agent: "CompiledStateGraph | None" = None;  agent_lock = asyncio.Lock();  THREAD_ID = "main"
AGENT_LOCK_TIMEOUT = 600.0          # 锁等待上限（秒）
agent_lock_owner: str = ""          # f"{owner}@{time.strftime('%H:%M:%S')}"
agent_lock_owner_task: "asyncio.Task | None" = None   # 可重入自检
```

`acquire_agent_lock(owner)` 三条分支（顺序即优先级）：① `if scheduler.in_scheduled_turn(): return`（回合内 no-op，锁已由 worker 持有）；
② `if state.agent_lock_owner_task is asyncio.current_task():` → `raise RuntimeError("主 Agent 锁不可重入：...")`（同任务二次获取立刻失败，而不是卡 600s）；
③ `await asyncio.wait_for(state.agent_lock.acquire(), timeout=state.AGENT_LOCK_TIMEOUT)`，超时 → `RuntimeError(f"等待主 Agent 锁超时（{timeout:g}s）：当前持有者 [...]。...")`，成功 → `mark_agent_lock_acquired(owner)`。
超时文案必须含 `"等待主 Agent 锁超时"`（调用方按子串匹配）。`TimedAgentLock(owner)` 是 `async with` 包装：`__aenter__` 把 `RuntimeError` 转成 `AgentLockBusy`，`__aexit__` 调 `release_agent_lock()`。**中断信号不在进程级 state**（会让一个连接的中断打到另一个连接）。

### 2.2 三种释放与持有区间

| 函数 | 回合外 | 回合内 |
|---|---|---|
| `release_agent_lock()` | `agent_lock.release()` + `mark_agent_lock_released()` | **no-op**（锁归回合所有） |
| `release_llm_stream_lock()` | 同普通释放 | `scheduler.release_turn_lock()`：LLM 会话一结束就提前放锁 |
| `TimedAgentLock.__aexit__` | `release_agent_lock()` | no-op |

`release_turn_lock()` 幂等：`if handle is None or not handle.lock_held: return` → `lock_held = False` → `if state.agent_lock.locked(): release() + mark_agent_lock_released()`。
**契约**：worker 在 `_run_handle` 取锁并置 `lock_held=True`；生产者结束提前释放；worker 收尾 `finally: release_turn_lock()`（通常 no-op）。即“锁覆盖 LLM 会话，不覆盖前端交付”。

### 2.3 常量、档位、请求与句柄

`TRIGGER_QUEUE_MAX = 50`；`STALL_IDLE_SEC = 300.0`；`BATCH_WINDOW_SEC = 30.0`；`TASK_CANCEL_TIMEOUT_SEC = 3.0`。
`TurnKind(IntEnum)`：`INTERRUPT=0, USER=1, TRIGGER=2, BATCH=3`（数字越小越优先，档内 FIFO）。`TurnStatus(str, Enum)`：`PENDING/RUNNING/DONE/INTERRUPTED/ERROR/MERGED/DROPPED`。
`exclusive` **不是档位**：= `USER` + `preemptible=False`。四个正交标志 = `sink`/`droppable`/`gate`/`preemptible`。
`AgentRequest.__slots__` = `kind, run, label, sink="silent", droppable=False, gate="playback", preemptible=True, merge_key=None, owner=None, on_terminal`；
`on_terminal(handle)` 在**恰好一次**终态时调用（含“未开始即被撤单”），用于清理提交方设的全局状态。
`TurnHandle` 字段：`request, seq, status, result, error, created_ts, started_ts, finished_ts, last_progress_ts, task, lock_held, forced, degraded, cancel_reason, gate_wait_since` + `_clock, _done, _terminal_hint, _cancel_requested`。

### 2.4 `submit()` 精确顺序与准入

1. `_check_stall(waiting=True)`（**惰性僵死判定只在这里查**：有人要排队时才有意义）→ `_seq += 1` → 构造 `TurnHandle`。
2. `_merge_into_pending(...)`：同 `merge_key` 的 PENDING 请求被**原位替换**，旧的 `finish(TurnStatus.MERGED)`；命中即返回。
3. `if request.droppable and self.pending_count() >= TRIGGER_QUEUE_MAX:` → `log.warning("调度器拥塞，丢弃可丢请求（%s）", ...)` → `finish(TurnStatus.DROPPED)` 并返回（`droppable` 是拥塞时唯一允许硬丢的请求）。
4. 入 `_pending[kind]` 与 `_handles` → `_degrade_overflow()`：`while len(trigger_queue) > TRIGGER_QUEUE_MAX` 弹最旧、`degraded=True`、`request.kind = BATCH`、`batch.appendleft(...)`（不丢唤醒）→ `_ensure_worker()`（懒启动 `name="agent_scheduler_worker"`）→ `wake()`。
5. `_take_admissible()`：按 `for kind in TurnKind` 逐档、先弹出非 PENDING 残留；**队首不可准入就 `return None`，不越过它**。
6. `_admissible`：① `kind in (TRIGGER, BATCH) and events.ignore_trigger_event.is_set()`；② `gate != "exempt" and turn_gate.should_hold("normal")`（记 `gate_wait_since`，放行时 `log.info("触发器闸门等待 %.2fs 后放行（%s）", ...)`）。闸门只压最低两档，故挡不住 interrupt/user。
7. `_next_handle()`：`wake.clear()`（先清后查，不丢唤醒）→ 再查一次 → `await asyncio.wait_for(wake.wait(), timeout=_wait_timeout())`；`_wait_timeout()` = 闸门剩余 > 0 时 `max(0.02, min(0.5, remaining))`，否则 `0.5`。

### 2.5 执行与收尾

- `_run_handle`：`mark_running()` → `_executing = handle` → `acquire_agent_lock(f"turn:{request.label}")`；失败 → `finish(TurnStatus.ERROR, error=LockBusyError(str(exc)))` 并清 `_executing`（不卡队列）→ `token = _current_turn.set(handle)` → 已终态则不执行 → `create_task(self._invoke_run(handle), name=f"agent_turn:{request.label}")` → `_cancel_requested` 则立刻 `task.cancel()` → `await asyncio.wait({task})`（`suppress(CancelledError)`）→ `if task.cancelled(): finish(INTERRUPTED, TurnCancelled("task cancelled"))`。
- `finally`：`_executing=None`；`handle.task=None`；`release_turn_lock()`；`_current_turn.reset(token)`。worker 自身被 cancel 时先 `task.cancel()` + 有界 `wait(timeout=TASK_CANCEL_TIMEOUT_SEC)`，再 `finish(INTERRUPTED, TurnCancelled("worker shutdown"))` 后 `raise`。
- `_invoke_run`：成功 → `_finalize(DONE, result=...)`；`CancelledError` → `_finalize(INTERRUPTED, TurnCancelled(cancel_reason or "cancelled"))` 并 re-raise；其它异常 → `_finalize(ERROR, error=exc)` + `log.error("回合执行失败（%s）: %s", ...)`。
- `_finalize` 顺序固定：先 `_executing=None` → `release_turn_lock()` → `handle.finish(...)`（`await handle.wait()` 返回时执行位必须已空，否则出现“已完成仍占位”的假僵死）。`finish` 幂等，`_terminal_hint` 可覆盖非 `MERGED/DROPPED/ERROR` 的目标状态。

### 2.6 并发 run 的行为矩阵

| 场景 | 行为 |
|---|---|
| 用户消息（chat WS） | `start` → 若 `not _scheduler().is_idle()` 发 `pending{reason:"waiting_lock"}` → `submit(USER, gate="exempt", owner=websocket, on_terminal=清 ignore_trigger_event)`；同连接新消息先 `turn.abort_event.set()` + `cancel_owner(websocket, reason="superseded_by_new_user_message")` |
| `POST /chat` | `set(events.ignore_trigger_event)` → `submit(USER, sink="silent", gate="exempt", on_terminal=lambda _h: ignore_trigger_event.clear())` → `await handle.wait()`；锁超时 → HTTP 503 `{"error":..., "busy": True}` |
| 会重建的斜杠命令（`/clear` `/thinking` `/effort` `/vng`） | 有 WS → `create_task(_run_slash_command_stream(...))`（立刻返回 `(True, None)`，接收循环不被冻住）；无 WS → `await run_exclusive(factory, label="exclusive")` |
| `/compact` | `create_task(_run_compact_command(ws))` → `run_exclusive(..., label="exclusive:compact")`；失败补 `compact_done{ok:False,error}` + `error` |
| 触发器 | `_submit_trigger_request`：kind 由优先级映射（`interrupt`→INTERRUPT、`batched`→BATCH、其余 TRIGGER）；`owner=None`（**不随连接生死**）；有 chat 连接 → `sink="chat"` 否则 `"silent"`；`gate="exempt"` 仅 INTERRUPT |
| 连接断开 | `cancel_owner(websocket, reason="chat_ws_disconnect")`：撤未开始 + 有界等待在跑的进终态；触发器回合不受影响（刻意不对称） |
| 外部持锁（admin rebuild） | worker 取锁失败 → 该回合 `LockBusyError` → ERROR；`run_exclusive` 调用方转人话 `"主 Agent 正忙（可能有回合正在执行），已放弃该命令，请稍后重试。"` |

`pending` 只在 chat WS 入口、`submit` 之前、`not is_idle()` 时发（payload 固定 `{"agent_id":"main","type":"pending","reason":"waiting_lock"}`）；
`streaming` 只在 `_run_agent_stream` 收到 `lock_acquired` 时发（payload 仅 `agent_id`+`type`）。生产者**不再**自报 waiting_lock。

### 2.7 `test_agent_scheduler.py` 逐条断言（15 例）

fixture `sched_env`：`FakeClock` 从 1000.0 起、`TRIGGER_READ_FLOOR_SEC=0.0`、`TRIGGER_HOLD_MAX_SEC=0.2`、`presence.mark_online()`、清 `ignore_trigger_event`、`tg.reset()`、`AgentScheduler(clock=clock)`，收尾 `shutdown(timeout=1.0)`；`pytestmark = pytest.mark.asyncio(loop_scope="module")`。

1. `test_priority_order_and_fifo`：同同步块提交 batch/trigger/user/interrupt/trigger2 → `log == ["interrupt:start","interrupt:end","user:start","user:end","trigger:start","trigger:end","trigger2:start","trigger2:end","batch:start","batch:end"]`。
2. `test_single_worker_never_overlaps`：5 条 TRIGGER 各 `sleep(0.01)` 统计并发 → `max_active == 1`（"单 worker 必须串行执行"）。
3. `test_gate_hold_blocks_trigger_but_not_interrupt`：`tg.arm("turn-1", text_len=0)` 后 `should_hold("normal") is True`；提交 trigger + alarm(INTERRUPT) → 只 alarm 跑（`log == ["alarm:start","alarm:end"]`）、`trigger.status == PENDING`；`note_playback_done("turn-1")` + `wake()` 后 trigger 跑。
4. `test_ignore_trigger_event_blocks_trigger_not_user`：`ignore_trigger_event.set()` → user 跑、trigger PENDING；`clear()` + `wake()` 后 `trigger.status == DONE`。
5. `test_merge_same_key_covers_pending`：RUNNING 回合阻塞时提交同 `merge_key="same-id"` 的 old/new → `first.status == MERGED`；放行后 `"new:start" in log and "old:start" not in log`、`second.status == DONE`。
6. `test_trigger_overflow_degrades_oldest_to_batch`：`TRIGGER_QUEUE_MAX=3` 后提交 5 条 → `handles[0].degraded is True and handles[1].degraded is True`、`handles[0].request.kind == BATCH`、`handles[4].status == PENDING`（"新来的那条永远能进队"）；放行后 `log.index("t0:start") > log.index("t4:end")`。
7. `test_droppable_shed_when_congested`：`TRIGGER_QUEUE_MAX=1` 且已有 1 条排队 → `droppable=True` 的新请求 `status == DROPPED`。
8. `test_stall_detection_cancels_stuck_turn`：USER 回合卡住 → `clock.advance(STALL_IDLE_SEC + 1.0)` 后提交新请求 → `stuck_turn.status == ERROR`、`cancel_reason == "stalled"`、`isinstance(error, TurnCancelled)`、`"fresh:start" in log`。
9. `test_stall_not_triggered_below_threshold`：每半阈值推进并 `running.touch()` 共 5 次 → `running.status == RUNNING`、`queued.status == PENDING`。
10. `test_cancel_owner_withdraws_pending_and_cancels_running`：同 owner 的 running+queued 被撤 → `cancelled == 2`、两者 `INTERRUPTED`、`other.status != INTERRUPTED`（"别的连接的请求不受影响"）。
11. `test_cancel_running_kinds_marks_forced`：`cancel_running_kinds({TRIGGER}) is True`、`trigger.forced is True`、`status == INTERRUPTED`；再调返回 `False`。
12. `test_cancel_merge_key_only_touches_pending`：`cancel_merge_key("trig-1") == 1` → `queued.status == INTERRUPTED`；再调返回 `0`；running 不受影响。
13. `test_snapshot_exposes_queue_and_constants`：`snap["executing"]["kind"] == "running"`、`snap["queue"] == [{"kind":"queued","waited_sec":3.0,"degraded":False}]`、`snap["constants"]["TRIGGER_QUEUE_MAX"] == sched.TRIGGER_QUEUE_MAX`、`snap["worker_running"] is True`；收尾后 `is_idle() is True` 且 `snapshot()["executing"] is None`。
14. `test_error_turn_does_not_wedge_queue`：抛 `RuntimeError("炸了")` → `bad.status == ERROR`、`isinstance(bad.error, RuntimeError)`、`bad.wait()` 重抛、`"good:start" in log`。
15. `test_worker_holds_agent_lock_and_releases_after_turn`：回合体内 `state.agent_lock.locked()` 为 `True`（`seen == [True]`），结束后 `is False`（D20 迁移期保险丝）。

## 3. 中断语义

### 3.1 分层实现（无模型层/工具层专用取消通道）

| 层 | 载体 | 行为 |
|---|---|---|
| ① 连接级信号 | `TurnState.abort_event: asyncio.Event` | 生产者**每收到一个事件**后检查 `is_set()` → `raise asyncio.CancelledError("User interrupted")`（延迟 = 一个事件） |
| ② 连接级句柄 | `TurnState.turn_handle` | `TurnState.interrupt()`：`abort_event.set()`；`if handle and not handle.is_terminal(): handle.cancel("user_interrupt")` |
| ③ 调度器 | `TurnHandle.cancel(reason)` | PENDING → 立刻 `finish(INTERRUPTED, error=TurnCancelled(reason))`；否则 `_terminal_hint = ERROR if reason=="stalled" else INTERRUPTED` 并 `task.cancel()` |
| ④ asyncio 任务 | `handle.task` | `task.cancel()` 只在 `await` 边界投递 `CancelledError`（同步/阻塞工具不会立即感知） |
| ⑤ 生产者/消费者 | `stream_chat_agent_events` 的 `finally` | 消费者退出（WS 断开、异常）→ `producer.cancel()` + `await producer` |

`TurnState`（`@dataclass`，按 websocket 存于 `weakref.WeakKeyDictionary`）：`abort_event`（每轮 `begin_turn()` 换新对象，避免上轮 set 把新一轮立刻打断）、
`force_interrupting: bool`（插话强制打断标记，改发 `done(forced)` 而非 `interrupted`）、`turn_handle`；访问器 `get_turn_state(websocket)` / `drop_turn_state(websocket)` / `reset()`。
进程级（不属单连接）：`events.ignore_trigger_event`、`turn_gate`。

### 3.2 时序

```mermaid
sequenceDiagram
    participant FE as 前端
    participant WS as chat_websocket
    participant SCH as AgentScheduler
    participant PR as stream_agent_producer
    FE->>WS: {"type":"interrupt"}
    WS->>SCH: TurnState.interrupt(): abort_event.set() + handle.cancel("user_interrupt")
    SCH->>SCH: _terminal_hint=INTERRUPTED; task.cancel()
    WS-->>FE: {"agent_id":"main","type":"interrupt_ack"}
    PR->>PR: 下一事件: abort_event.is_set() -> raise CancelledError("User interrupted")
    PR->>PR: _close_compact(False); _finish(_StreamFailed(...)); raise; finally release_llm_stream_lock()
    WS-->>FE: {"agent_id":"main","type":"interrupted"}（或 done(forced=True)）
```

### 3.3 取消源与 reason 字符串

| 触发 | 调用 | reason | 终态/事件 |
|---|---|---|---|
| 前端 `interrupt` | `TurnState.interrupt()` → `handle.cancel(...)` | `"user_interrupt"` | INTERRUPTED；`interrupted`；`interrupt_ack` |
| 同连接新用户消息 | `cancel_owner(websocket, reason=...)` | `"superseded_by_new_user_message"` | INTERRUPTED（旧回合无终结事件，前端已被新 `start` 重置） |
| chat WS 断开 | `cancel_owner(websocket, reason=...)` | `"chat_ws_disconnect"` | INTERRUPTED |
| 用户插话抢占触发器 | `cancel_running_kinds({TRIGGER, BATCH}, reason=...)` | `"user_interjection"`；`handle.forced = True` | INTERRUPTED；前台触发器流改发 `done(forced=True)` |
| 删除触发器 | `cancel_merge_key(merge_key, reason=...)` | `"trigger_deleted"` | 仅 PENDING → INTERRUPTED |
| 惰性僵死 | `_check_stall` → `_cancel_handle(handle, "stalled")` | `"stalled"` | **ERROR** + `TurnCancelled` |
| 调度器收尾 | `shutdown()` | `"scheduler shutdown"` | INTERRUPTED |
| worker 自身取消 | `_run_handle` 的 `except CancelledError` | `"worker shutdown"` | INTERRUPTED |

插话（`AUTO_FORCE_INTERRUPT`）：`_force_interrupt_trigger` 只对 `{TRIGGER, BATCH}` 生效（**用户回合不在范围内**），`turn.force_interrupting = True`
打在“**触发器流的目标连接**”（`_latest_chat_ws() or websocket`）上并在 `finally` 复位；`_apply_user_interjection` 命中时给文本加前缀 `"(用户插话)"`。

### 3.4 中断后残留的状态

| 残留 | 现状处理 |
|---|---|
| checkpoint 里的部分 `AIMessage` / 未响应 `tool_calls` | **原样留存**。`ToolCallRepairMiddleware`（始终启用）在下一次模型调用前重写 `request.messages`：序列结尾悬空 → 注入占位 `ToolMessage(PLACEHOLDER_CONTENT="[工具调用结果缺失，已由 Liorium 自动补全]")`；被 Human/System/新 AI 打断的 → 清空该 `AIMessage` 的 `tool_calls`（含 content 块 `tool_use`/`function_call`/`tool_call`）；孤立 `ToolMessage` → 丢弃。**修复不写回 checkpoint** |
| 已推给前端的 delta | 不回滚；前端凭 `interrupted`/`done(forced)`/`error` 收尾并 reject 挂起请求 |
| `turn_gate._record` | 中断路径**不 arm**（`arm` 只在正常完成路径），故不压触发器；前端主动打断还会 `sendTurnPlaybackDone(lastTurnId)` 提前放行 |
| `events.ignore_trigger_event` | 由 `on_terminal` 清理（恰好一次）；异常路径另有显式 `clear()` |
| `OutputStore`（`artifact.json`） | 保留；只有 `/clear` 会 `reset_output_store(clear_persisted=True)` |
| 调度器/锁 | `_executing=None`、`handle.task=None`、`release_turn_lock()`（三层幂等） |

已知缺口（复刻需决策）：PENDING 阶段被撤单**不向前端补发终结事件**，由发起方本地收尾。

## 4. `state.py`：进程级状态

### 4.1 字段

| 字段 | 类型/初值 | 写入方 | 用途 |
|---|---|---|---|
| `agent` | `CompiledStateGraph \| None` | `rebuild` | 主 Agent 图实例（每次 rebuild 整只替换） |
| `agent_lock` | `asyncio.Lock()` | fixture 会换新 | 唯一执行锁 |
| `THREAD_ID` / `AGENT_LOCK_TIMEOUT` | `"main"` / `600.0` | 常量 | 线程 id / 锁等待上限 |
| `agent_lock_owner` / `agent_lock_owner_task` | `""` / `None` | `mark_*` | 超时诊断 / 可重入自检 |
| `conn` / `checkpointer` | `aiosqlite.Connection` / `AsyncSqliteSaver` | `rebuild` | 持久化 |
| `RUNTIME_READY` / `RUNTIME_STATUS` / `RUNTIME_ERROR` | `False` / `"starting"` / `""` | `set_runtime_state` | 就绪态 |
| `AGENT_NAME` / `AGENT_ROOT` / `PROMPT` | `conf.AGENT_NAME` / `{CONFIG_ROOT}/agents/{AGENT_NAME}` / `""` | `rebuild`、`makeup_init_prompt` | 角色目录与提示词 |
| `forward_queue` | `asyncio.Queue()` | — | 转发裸字符串命令（非 JSON 事件） |
| `plugin_heartbeat_task` / `processor_prune_task` | `None` | `lifespan` | 后台任务 |
| `plugin_manager` / `subagent_manager` / `compact_middleware` | `None` | `lifespan` / `rebuild` / `build` | 组件句柄 |
| `uvicorn_server` / `fastapi_app` | `None` | `lifespan` | 服务句柄 |

函数：`set_runtime_state(*, ready, status, error="")`、`runtime_not_ready_message()`、`runtime_status_payload()`（键 `ready/status/error/agent_name/agent_root/private_config_missing/private_config_auto_created`）、
`ensure_agent_runtime_ready()`（`agent is None or not RUNTIME_READY` → `RuntimeError`）、`get_model_providers()`、`makeup_init_prompt()`、`has_checkpoint_db(agent_root)`。
`makeup_init_prompt()` 依次拼 `AGENT.md`/`ROLE.md`/`COREMEMORY.md` → Skill YAML 区块 → `agent://index.md` 提示 → 插件 `collect_prompt_suffixes()`；import 期失败 → `set_runtime_state(ready=False, status="waiting_for_config", error=...)`。

### 4.2 图 / 检查点 / config 的持有关系

图由 `build._create_agent_with_extensions(model_name, checkpointer)` 产出，每次 rebuild 整体替换；`checkpointer`/`conn` 同批替换（旧 conn 先 `commit()`+`close()`，异常吞掉）。
config **每次调用现造**（`thread_id` + `recursion_limit`），不缓存。中间件栈（`_compose_runtime_middlewares`）：插件 middlewares → `MultimodalBridgeMiddleware` →
`ToolCallRepairMiddleware` → 可选 `ServerToolMiddleware` → 可选 `CompactMiddleware` → `with_model_retry(...)` 包最外层（重试最内层，只重放模型请求）。
thread：`THREAD_ID = "main"`，chat / trigger / POST / 命令 / seed / compact 全部共用；**没有多会话 id 方案**，“新会话”= 删除该 thread 的 checkpoint 记录。

### 4.3 清空对话（`/clear`）

`_clear_session_locked()`（**必须在调度器 exclusive 回合内**，锁已持有）：① `if state.checkpointer is not None and hasattr(state.checkpointer, "adelete_thread"): await state.checkpointer.adelete_thread(str(state.THREAD_ID))`；
② `if state.subagent_manager is not None: await state.subagent_manager.reset_persistent_state()`；③ `reset_output_store(clear_persisted=True)`；④ `pm.reset_all_plugin_sessions()`；
⑤ `info = await rebuild_runtime(reset_dialog=True, no_initial_chat=False)`；⑥ 返回 `f"已清空当前会话并重建运行时。ready={info.get('ready')} status={info.get('status')}"`。
顺序理由：先删 checkpoint 再发现拿不到锁会得到“会话已清、运行时没重建”的半成品，故整个“清数据 + 重建”在同一临界区。

### 4.4 OutputStore 生命周期

单例 `_store`；`get_output_store()` 每次比较 `storage_path`，`AGENT_ROOT` 变化即重建实例（角色切换）。落盘 `{AGENT_ROOT}/artifact.json`
（`{"counter": int, "artifacts": [...]}`），无路径时纯内存。ID = `f"{_short_tool_name(tool_name)}_{counter}"`，counter 全局递增并持久化。
挂点：`_rebuild_subagent_manager` 末尾调 `get_output_store()`；`_clear_session_locked` 调 `reset_output_store(clear_persisted=True)`（清空 + 删文件）；无参调用只丢引用。
`Artifact.summary()` 截断 60000 字符 / 300 行，尾部追加 `artifact://<id>` 提示。

### 4.5 `rebuild_runtime` 的分支

壳：`try: await _rebuild_runtime_locked(...) except AgentLockBusy as e: return {"agent_name","agent_root","initial_chat_skipped": True, "ready": False, "status": "busy", "error": str(e)}`。
锁内（`async with TimedAgentLock("rebuild_runtime")`）：`conf.reload_configs()` → 后台 `refresh_models_dev_catalog()`（name=`"models_dev_refresh"`，失败只记日志）→ 凭证注入（`DEEPSEEK_API_KEY`、`SEARCHAPI_API_KEY`；`OPENAI_*` 仅 `try_main_openai_credentials()` 非 None 时设）→ 刷新 `AGENT_NAME`/`AGENT_ROOT` → `admin_runtime.sync_template_files(...)` → `state.makeup_init_prompt()` → `tools_mod.refresh_runtime_paths()` → `refresh_runtime_nodes(await get_agent_vfs(refresh=True))` → MCP `load_config` + `sync_servers()` → `araya_runtime...refresh_target_agent()` → `if pm and pm.needs_reload():` `pm.reload(force=True)` + `_sync_plugin_trigger_filters()` + `FrontEndReloadPluginAssets()` → checkpoint 初始化 → `state.agent = await _create_agent_with_extensions(model_name=providers.main_model, checkpointer=state.checkpointer)` → `await _rebuild_subagent_safely(providers)` → `checkpoint_exists = (not args.save_in_memory) and state.has_checkpoint_db(state.AGENT_ROOT)`。

| 分支 | 条件 | 行为 | 返回 |
|---|---|---|---|
| B1 | 锁超时（`AgentLockBusy`） | 只报告状态，**不动 `state.agent`** | `ready=False, status="busy", error="等待主 Agent 锁超时...", initial_chat_skipped=True` |
| B2 | 锁内任一异常 | `state.agent = None`；`set_runtime_state(ready=False, status="waiting_for_config", error=str(e))`；`log.warning("运行时重建降级: %s", e)` | `ready=False, status="waiting_for_config"` |
| B3 | `no_initial_chat and checkpoint_exists` | `log.info("运行时重建跳过初始对话")`；`set_runtime_state(ready=True, status="ready")` | `ready=True, status="ready", initial_chat_skipped=True` |
| B4 | 否则且 `reset_dialog` | **锁外** `await _seed_system_prompt(state.agent)`（纯状态写入，不调模型） | `ready=True, status="ready", initial_chat_skipped=False` |
| B5 | 否则且非 `reset_dialog` | **锁外**：若无 checkpoint 消息先 seed；再 `_invoke_agent_unlocked(state.agent, {"messages":[{"role":"user","content":"请执行启动流程(Bootstrap 流程)[如有]你可以读取agents/{AGENT_NAME}/AGENT.md、ROLE.md、COREMEMORY.md、TASK.md等文件来获取最新的设定内容。\n 这一条对话无需写入日记"}]})` | 同 B4（`initial_chat_skipped=False`） |
| B6 | B4/B5 内异常 | 同 B2 降级 | `ready=False, status="waiting_for_config"` |
| B7 | 函数末尾 | 实际不可达（B3–B5 都已 return），历史兜底 | `ready=True, status=state.RUNTIME_STATUS, initial_chat_skipped=True` |

两个现状细节：**B3 的 `checkpoint_exists` 几乎恒为真**（checkpoint 初始化刚 `aiosqlite.connect` 创建了 `checkpoint.db`，随后才 `has_checkpoint_db()`，故 sqlite 模式下 `no_initial_chat=True` **总是**跳过 bootstrap）；
**B4/B5 在 `TimedAgentLock` 之外**（回合内靠 `in_scheduled_turn()` 变 no-op；启动路径此时并未持锁，`_invoke_agent_unlocked` 实际是无锁调用，bootstrap 与外部 rebuild 可并发）。

## 5. Checkpoint

### 5.1 Saver 与物理格式

生产：`AsyncSqliteSaver(conn=state.conn)`（`conn = await aiosqlite.connect("{AGENT_ROOT}/checkpoint.db")`）；内存模式（`args.save_in_memory`）：`InMemorySaver()`。
表由 saver 的 `setup()` 建（`PRAGMA journal_mode=WAL`）：`checkpoints(thread_id, checkpoint_ns DEFAULT '', checkpoint_id, parent_checkpoint_id, type, checkpoint BLOB, metadata BLOB, PK(thread_id, checkpoint_ns, checkpoint_id))` 与
`writes(thread_id, checkpoint_ns DEFAULT '', checkpoint_id, task_id, idx, channel, type, value BLOB, PK(thread_id, checkpoint_ns, checkpoint_id, task_id, idx))`。
`adelete_thread(thread_id)` = 同一把锁下 `DELETE FROM checkpoints WHERE thread_id = ?`（writes 同条件）。

### 5.2 System 消息植入（`agent/prompt/seed.py`）

`_agent_thread_config()` = `{"configurable": {"thread_id": state.THREAD_ID}, "recursion_limit": 300}`；`_checkpoint_messages(agent)` = `aget_state(config).values["messages"]`。
`_seed_system_prompt(agent) -> bool`：① `prompt = str(state.PROMPT or "")`，`not prompt.strip()` → `log.warning("角色 Prompt 为空，跳过 System 消息植入（AGENT.md 可能未加载）")` + `return False`；
② 拆 `system_messages` 与 `kept`（非 System）；③ 已是目标态（恰一条 System、位于首位、内容等于 prompt）→ `return False`；
④ 否则 `aupdate_state(config, {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), SystemMessage(content=prompt), *kept]}, as_node="model")` → `return True`。
`as_node="model"` 必需（否则 langgraph 抛 `Ambiguous update, specify as_node`）。不用“只发一条 system 消息的模型调用”的原因：Gemini `ValueError: contents are required.`、Anthropic 422 `messages: at least one message is required`。
`reset_dialog=True` **绝不发起模型调用**。

### 5.3 thread id 与删除 / 读路径

`THREAD_ID = "main"` 常量，`configurable.thread_id` 全链路一致，`checkpoint_ns` 保持 `""`。删除由**调用方** `_clear_session_locked` 的 `adelete_thread("main")` 完成，`rebuild_runtime` 自己不删；
写入由 `_seed_system_prompt` 的 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 重排完成 —— **System 恒在最前**且保留非 System 历史（`reload-all` 这类不删 thread 的入口不丢上下文、不累积重复 System）。
读取：`session_stats.get_checkpoint_messages()` = `state.checkpointer.aget_tuple({"configurable":{"thread_id": state.THREAD_ID}})` → `checkpoint_tuple.checkpoint["channel_values"]["messages"]`（缺失/非 list → `[]`），`/session` 与 `GET /session/context` 共用。
`collect_session_stats()` 返回键：`status, model, api_format, messages, has_api_usage, used_tokens, used_tokens_text, context_length, context_length_text, threshold_tokens, ratio, percent, input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_creation_tokens, reasoning_tokens, compact_threshold_ratio`；
`threshold_tokens = max(1, int(context_length * conf.COMPACT_THRESHOLD_RATIO))`，无 API 上报时 `used_tokens_text="—"`。`format_tokens`：`<1000` 原样；`<1e6` 半进位到 K（`999_999 -> 1M`）；`>=1e6` 1 位小数去 `.0`。

## 6. 事件 `type` 总表（冻结新契约用）

前端事件统一由 `_main_event_payload(event_type, **kwargs)` 构造：先 `{"agent_id": "main", "type": event_type}` 再 `update(kwargs)`；`_safe_send` 吞掉发送异常（只 `log.warning`）。

| `type` | 发射点 | 其它 payload 字段 | 到前端 |
|---|---|---|---|
| `start` | chat WS 用户入口 / `_run_slash_command_stream` / `_run_trigger_stream_frontend` | `turn_id`, `origin` ∈ {`"user"`,`"command"`,`"trigger_foreground"`} | ✅ |
| `pending` | chat WS 用户入口，`submit` 之前且 `not is_idle()` | `reason: "waiting_lock"` | ✅ |
| `streaming` | `_run_agent_stream` 收到 `lock_acquired` | 无 | ✅ |
| `delta` | `_run_agent_stream` / 命令回复 | `content`；命令路径另带 `turn_id` | ✅ |
| `reasoning_delta` | `_run_agent_stream` | `content` | ✅ |
| `tool_start` | 生产者→消费者→chat | `tool_name`, `args`(dict), `call_id`(LangGraph `run_id`) | ✅ |
| `tool_result` | 同上 | `tool_name`, `output`(str \| dict), `call_id` | ✅ |
| `compact_start` | 生产者首个压缩 chunk / `/compact` | 无 | ✅ |
| `compact_delta` | 生产者逐 chunk / `_on_delta` / `_notice` | `content` | ✅ |
| `compact_done` | `_close_compact` / `/compact` 正常与失败 | `ok`(bool)，失败路径另带 `error` | ✅ |
| `done` | `_run_agent_stream` 正常结束 / 插件拦截 / 命令流 / `_run_compact_command` / 插话强制打断 | 可选 `turn_id`；插话时 `forced: True` | ✅ |
| `interrupted` | `_run_agent_stream` 的 `except CancelledError`（非强制打断） | 无 | ✅ |
| `error` | `_run_agent_stream` 异常 / 命令后台失败 / WS 无 text / 运行时未就绪 / `_run_compact_command` | `error`(str)；运行时未就绪另带 `runtime`(dict) | ✅ |
| `interrupt_ack` | chat WS 收到 `{"type":"interrupt"}` | 无 | ✅ |
| `vng_mode` | `_set_vng_mode` | `enabled`(bool) | ✅ |
| `subagents_summary` | `_subagents_summary_payload()` | `agent_id="subagents"`, `items`(list) | ✅ |
| `lock_acquired` | 生产者每次成功取锁 | 无（**无 `agent_id`，不外发**） | ❌ 内部（翻译成 `streaming`） |

非事件载荷：`command WS` 的裸字符串（`state.forward_queue`、`backend2frontend` 队列内容，可能是 `SAY` 文本）；子代理事件 `{"agent_id": "subagent-<name>", ...}`（`agent/subagent/events.py` 广播，不属本文范围）。
chat WS 入站：`{"type":"interrupt"}`、`{"type":"turn_playback_done","turn_id":...}`、`{"text"|"message":...}`（裸字符串按 `{"text": raw}` 兜底）。

## 7. 测试断言清单

### 7.1 `test_agent_lock.py`（6 例）

模块级 `pytestmark = pytest.mark.asyncio(loop_scope="module")`；helper `_wait_lock_released(timeout=3.0)` 轮询 `not state.agent_lock.locked()`；fixture `lock_timeout_env` 把 `AGENT_LOCK_TIMEOUT` 临时设 `0.1`，收尾恢复并强释放。

1. `test_stream_starts_with_lock_acquired`：第一项**精确等于** `{"type": "lock_acquired"}`；第二项 `["type"]=="delta"`、`["content"]=="你好"`；`aclose()` 后锁已释放。
2. `test_consumer_pause_does_not_hold_lock`：取到 `lock_acquired` + 一个 delta 后**故意停 0.3s 不消费**，断言 `not state.agent_lock.locked()`（前端节拍不得劫持全局锁）。
3. `test_invoke_agent_locked_timeout_and_recovery`：`hold_agent_lock("test_holder")` 期间 `pytest.raises(RuntimeError)`，消息含 `"等待主 Agent 锁超时"` 与 `"test_holder"`；释放后 `invoke_agent_locked(FakeInvokeAgent(), ...)` 返回 `messages[-1]["content"]=="ok"` 且锁未残留。
4. `test_stream_timeout_when_lock_held`：直接 `agent_lock.acquire()` + `mark_agent_lock_acquired("test_holder")` → `_next(agen)` 抛 `RuntimeError` 且含 `"等待主 Agent 锁超时"`；收尾后锁未 locked。
5. `test_stream_error_propagates_and_releases_lock`：生产者抛 `ValueError("boom")` → 先 `lock_acquired`，再 `pytest.raises(ValueError, match="boom")`；锁已释放。
6. `test_stream_abort_propagates_cancelled`：`abort_event` set + 唤醒 gate 后 `_next(agen)` 抛 `asyncio.CancelledError`；`_wait_lock_released()` 为真。

### 7.2 `test_agent_call_control.py`（13 例）

fixtures：`short_lock_timeout`（`AGENT_LOCK_TIMEOUT = 0.2`）；conftest 的 `hold_agent_lock`（**在独立任务里持锁**，避免被可重入守卫当成同任务二次获取）。helper：`_wait_for_event(ws, type)` 最多 200×0.02s；`_stub_rebuild_deps(monkeypatch)` 把 rebuild 的重活替身化并返回 `FakeInvokeAgent`。

1. `test_producer_cancelled_while_waiting_lock_terminates_consumer`：`AGENT_LOCK_TIMEOUT=30.0`、锁被占；取出 `name=="stream_agent_producer"` 的未完成任务并 cancel → 消费者 `pytest.raises(RuntimeError, match="terminal event")`（5s 内）；锁未 locked。
2. `test_rebuild_runtime_busy_keeps_runtime_intact`：`state.agent = sentinel`，锁被占时 `rebuild_runtime(reset_dialog=False, no_initial_chat=True)` → `ready is False`、`status == "busy"`、`"等待主 Agent 锁超时" in info["error"]`、**`state.agent is sentinel`**（锁忙 ≠ 运行时损坏）。
3. `test_clear_command_does_not_block_receive_loop`：`chat._clear_session_locked` 替身为 `_unreachable`（绝不能被调用）；锁被占时 `_handle_slash_command("/clear", ws)` → `handled is True`、`reply is None`、耗时 `< 1.0`；等到 `done`；`types[:1] == ["start"]`；delta 拼接含 `"正忙"`。
4. `test_compact_command_takes_agent_lock_in_background`：`state.compact_middleware = object()`，锁被占 → `handled is True and reply is None`、耗时 `< 1.0`；等到 `error`；`"compact_done" in types`；该 error 含 `"等待主 Agent 锁超时"`。
5. `test_invoke_agent_locked_inside_scheduled_turn_does_not_reacquire`：USER 回合内 `state.agent_lock.locked()` 为 `True`（`seen == [True]`，文案“回合执行期间主 Agent 锁必须由 worker 持有”），嵌套 `invoke_agent_locked` 立刻返回 `ok`，`agent.calls` 非空，`shutdown` 后锁未 locked。
6. `test_acquire_agent_lock_rejects_reentrancy`：同任务二次 `acquire_agent_lock("inner")` → `pytest.raises(RuntimeError, match="不可重入")` 且耗时 `< 1.0`。
7. `test_clear_success_path_completes_and_releases_lock`：锁空闲时 `/clear` 跑完 → `handled is True and reply is None`、等到 `done`；`reset_output_store` 替身被调用；`checkpointer.deleted == [str(state.THREAD_ID)]`；锁未 locked；delta 含 `"已清空当前会话"`；`fake_rebuild` 内部额外 `invoke_agent_locked`（模拟同一临界区的引导对话）。
8. `test_rebuild_inside_exclusive_turn_seeds_prompt_without_self_lock`：USER(exclusive) 回合内 `rebuild_runtime(reset_dialog=True, no_initial_chat=False)` → `info["ready"] is True`、`initial_chat_skipped is False`、`fake_agent.calls == []`（不得发起模型调用）、`updates` 非空、`isinstance(messages[0], SystemMessage)`、`messages[0].content == state.PROMPT`、`locked_during == [True]`；`shutdown` 后锁未 locked。
9. `test_rebuild_survives_broken_subagent_model`：`get_default_subagent_model` 返回 `"test::missing"` 且 `_rebuild_subagent_manager` 对它抛 `ValueError` → `ready is True`、`status == "ready"`、`state.agent is not None`；`calls == ["test::missing", "test::model"]`（必须回退主模型）。
10. `test_seed_system_prompt_writes_on_empty_checkpoint`：`seeded is True`、`agent.calls == []`、`len(messages)==1`、`isinstance(SystemMessage)`、`content == "角色设定"`。
11. `test_seed_system_prompt_is_idempotent`：`seeded is False`、`agent.updates == []`、类型序列 `["SystemMessage","HumanMessage"]`。
12. `test_seed_system_prompt_replaces_stale_system_and_keeps_history`：`seeded is True`、类型序列 `["SystemMessage","HumanMessage","AIMessage"]`、`messages[0].content == "新设定"`、`messages[1].content == "hi"`、`agent.calls == []`。
13. `test_seed_system_prompt_skips_empty_prompt`：`state.PROMPT=""` → `seeded is False`、`updates == []`。

### 7.3 中断相关：`test_source_interrupt.py` **不存在**

本仓库没有 `backend/tests/test_source_interrupt.py`，也没有 `test_agent_event_broadcast.py`。中断行为的实际测试是 `backend/tests/test_force_interrupt.py`
（8 例，`loop_scope="module"`，fixture `reset_trigger_registry` 每例前 `turn_state.reset()`、收尾强释放锁）：

1. `test_interjection_cancels_busy_trigger`：触发器回合 RUNNING 时 `AUTO_FORCE_INTERRUPT=True` → `_apply_user_interjection("你好", ws) == "(用户插话)你好"`、`handle.status == INTERRUPTED`、`handle.forced is True`。
2. `test_interjection_disabled_keeps_old_behavior`：`AUTO_FORCE_INTERRUPT=False` → 文本不变、`handle.status == RUNNING`。
3. `test_interjection_no_busy_unchanged`：无忙状态 → 文本不变。
4. `test_interjection_does_not_touch_running_user_turn`：正在跑的 USER 回合不受影响（`status == RUNNING`、文本不变）。
5. `test_interjection_bounded_when_target_swallows_cancel`：目标吞掉取消（`sleep(0.4)` 后才 re-raise）、`TASK_CANCEL_TIMEOUT_SEC=0.1` → 文本仍为 `"(用户插话)你好"` 且耗时 `< 1.0`。
6. `test_force_cancelled_stream_sends_done_forced`：`_run_agent_stream` cancel 且 `turn.force_interrupting=True` → `"done" in types`、`done_msg["forced"] is True`、**`"interrupted" not in types`**、3s 内锁释放。
7. `test_normal_cancel_sends_interrupted`：常规 cancel → `"interrupted" in types`，不存在 `done(forced=True)`。
8. `test_interrupt_of_one_connection_does_not_abort_another`：A 的 `interrupt()` 只结束 task_a，`not task_b.done()`、`not turn_state.get_turn_state(ws_b).abort_event.is_set()`（连接级隔离）。

同族相关（未点名）：`test_turn_hold.py`（12 例，`turn_gate` 的 hold/floor/ack/max_hold/新轮覆盖 + `_run_agent_stream` arm）、`test_trigger_hold_loop.py`（3 例，消费侧 hold 循环）。
无关/不存在：`test_agent_event_broadcast.py` 不存在，最近的是 `test_subagent_event_broadcast.py`（6 例，子代理事件广播器）；`test_agent_communicate.py` 是 `agent-communicate`
插件（外部 ACP Agent 信封/队列/状态机/权限/持久化）的测试，与主回合、interrupt、queue 无交集，复刻可忽略。

## 8. 必须保留的不变量

1. **单一执行位**：同进程同时只有一个回合推进 LLM；`agent_lock` 不可重入，同任务二次获取立刻报错。
2. **锁只覆盖 LLM 会话**：生产者结束即放锁，交付（WS 发送、插件 hook）不拖住全局锁。
3. **回合内取/放锁都是 no-op**：由 `in_scheduled_turn()`（ContextVar 传播到子任务）+ `TurnHandle.lock_held` 表达，避免“锁空窗”。
4. **终结事件恰好一次**：`terminal_sent` + `_finish` + `add_done_callback` 三层兜底，消费者永不停在 `queue.get()`。
5. **排队反馈在提交侧**：`pending{reason:"waiting_lock"}` 由入口发，`streaming` 只在真正拿到锁后发。
6. **档位与标志正交**：`interrupt > user|exclusive > trigger > batch`，档内 FIFO；队首不可准入时不越过。
7. **播放闸门只压最低两档**：`gate="exempt"`（用户消息、INTERRUPT 触发器、exclusive 命令）永不被 hold。
8. **触发器回合不绑连接（D19-b）、用户回合绑连接（D21）**；断开只取消用户回合。
9. **中断是连接级状态**：`abort_event` 每轮换新对象，一个连接的 interrupt 不影响另一个连接。
10. **压缩文本不得进正文**：`COMPACT_TAG` 分流，`compact_*` 从不并入 `reply`（故不进 TTS）。
11. **`done` 之前必须 `turn_gate.arm()`**；中断路径不 arm。
12. **`reset_dialog=True` 是纯状态写入**，不发起模型调用。
13. **System 恒在 checkpoint 首位且只有一条**（`REMOVE_ALL_MESSAGES` + 重排 + `as_node="model"`，保留非 System 历史）。
14. **锁忙 ≠ 运行时损坏**：`status="busy"` 时不得清 `state.agent`；子模型失效只降级子代理。
15. **token 只取 API 上报值**（反向扫描第一条带 `usage_metadata` 的 `AIMessage`），无值显示 `—`；Anthropic 输入 = `input_tokens + cache_read + cache_creation`，OpenAI/Gemini 的 cached 是子集不重复相加。
