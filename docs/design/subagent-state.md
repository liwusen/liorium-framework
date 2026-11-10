# 子代理状态持久化（`SubagentRealm`）

> 2026-11-08 设计。解决的问题：子代理**名册只在内存**。进程一重启，`subagents://` 空了、
> `listSubagents` 空了、`wait_for_subagent` 说"没有子代理"——可磁盘上它们的对话
> （`<child>/checkpoint.db`）、它们自己的 realm 状态（`<child>/realm_state/`）都还在。

## 1. 只补缺的那一环

子代理的**对话**本来就能持久：每个子代理是一个 `LioriumAgent`，checkpoint 在
`<child>/checkpoint.db`，`thread_id = "subagent-<name>"`，路径与线程名都由
名字确定。所以这次要落盘的不是对话，而是**名册 + 正文**：

| 需要活过进程重启 | 落点 |
|---|---|
| 有哪些子代理、用什么工具集 / 模型、状态、时间戳 | `realm_state/subagents.json` |
| 任务说明 / 本次转录 / 最终结果 | `subagents/<name>/{TASK,output,finalResult}.md` |
| 对话 | 已有：`subagents/<name>/checkpoint.db` |
| 子代理自己的 realm 状态 | 已有：`subagents/<name>/realm_state/` |

## 2. 磁盘布局

```
<inbox>/
├── realm_state/subagents.json            # 名册（JsonStateStore，原子替换；始终很小）
└── subagents/<name>/
    ├── TASK.md                           # create 时写
    ├── output.md                         # 回合结束时写（流式增量不落盘）
    ├── finalResult.md                    # done 时写
    ├── checkpoint.db               # 已有：子代理对话
    └── realm_state/todo.json             # 已有：子代理自己的 realm 状态
```

名册格式（`version` 留迁移余地；未知键忽略、坏条目跳过并告警）：

```json
{ "version": 1,
  "children": [
    { "name": "research-1", "agent_id": "subagent-research-1",
      "toolsets": ["read", "find"], "model": null, "status": "done",
      "error": null, "created_at": 1794134831.25,
      "started_at": 1794134831.26, "finished_at": 1794134831.29 } ] }
```

**结构化元数据进 JSON，正文进文件**：转录可以上百 KB，若塞进名册，每次转场都要重写
一次全量 JSON；分开之后名册稳定在几百字节。

## 3. 写序：先正文、后名册

```
写 TASK.md / output.md / finalResult.md  →  回写 subagents.json（原子替换）
```

崩在中间只会得到"状态旧一点"（比如 `running` 但转录已是最新），不会出现
"`done` 但结果文件是空的"。反过来就会。

## 4. 状态机

```
idle ──invoke──▶ running ──done───▶ done
                    ├──error──────▶ error
                    ├──stopSubagent▶ stopped
                    └──进程退出────▶ （名册仍是 running）──重启──▶ interrupted
```

`interrupted` 是独立状态，不是 `stopped` 换个说法：

- 用户没有叫停它，宿主不该把"上次被重启打断"和"我叫它停的"混为一谈；
- 它**可以直接 `invokeSubagent` 重启**（接着它自己的对话继续），`stopped` 也可以；
- `wait_for_subagent` 对它返回 `ok=false`、`status="interrupted"`（没有结果就是没有）。

恢复时会把这个新状态**回写名册**，否则 `/subagent` 会一直显示"运行中"。

## 5. 恢复时机与语义

- 在 `SubagentRealm.start()` 里恢复（`RealmManager.start()` 随后才挂 `/subagents`），
  所以重启后 `/subagent`、`subagents://<name>/finalResult.md` 立刻可用；
- 恢复只重建 `ChildRecord` 与正文，**不**重建子 Agent（`agent=None`）。下次
  `invokeSubagent` 才 lazily `start()` 一个 `LioriumAgent`，inbox / id / thread 都对得上
  ⇒ checkpoint 自然接上，`seed_system_prompt` 用 `TASK.md` 重写 system 段并保留历史；
- `max_children` 把恢复的算进去（重启不会凭空多出额度）；
- 名册 JSON 坏掉 ⇒ 沿用 `JsonStateStore` 的既有语义：整个 realm `failed`，
  原因进 `status().realms["subagents"].error`，不静默当空名册；
- 单条坏条目（名字非法 / 不是对象）⇒ 跳过 + `log.warning`，其余照常恢复；
- 正文在恢复时全量读回内存（VFS provider 与 CLI 都直接读字段）。真遇到超大转录，
  再谈 `params["restore_max_chars"]` 之类的上限，现在不加。

## 6. 关闭与删除

- `shutdown()`：先取消并**等**所有回合真正收尾，再关 Agent，最后回写名册。
  取消是异步投递的，不等就去关会在 `_ensure_agent()` 里留下一个开着 checkpoint 的孤儿。
  回写**只在 `children` 非空时**做 —— `close()` 会先调 `shutdown()`、再由 `stop_all()`
  调一次，第二次面对空字典若也回写就把磁盘名册擦掉了；
- `remove_child()`：等收尾 → 关 Agent（`asyncio.shield`，不被取消打断）→ 删目录 → 回写名册。
  删除前校验目标必须是 `inbox/subagents/<name>`；删不掉就保留记录并返回 `ok=false`，
  不假装删了；
- `newSubagent` 撞上"目录在、名册没有"（上次崩在两次写入之间 / 删失败）时返回
  `"resumed": true`，让模型知道旧对话还在。

## 7. 已知限制

| 限制 | 说明 |
|---|---|
| 单写者 | 两个进程指向同一 inbox 会互相覆盖名册 —— 与其它 realm 状态同一假设，不在本次解决 |
| Windows 句柄 | 若子代理在 `checkpoint.open()` 阶段被取消，aiosqlite 可能留一个没人持有的连接，句柄要等对象回收；`remove_child()` 的重试里带 `gc.collect()`，其余情况由进程退出回收 |
| 转录体积 | 恢复时全量进内存；暂不截断 |

## 8. 证据

`packages/liorium-agent/tests/test_subagent_state.py`（9 条）：

- 名册与三份正文活过重启，`listSubagents` / `wait_for_subagent` / `subagents://` 都能读到；
- 名册里**没有**转录正文（元数据与正文分家）；
- 上次 `running` ⇒ 重启后 `interrupted`，且名册被回写；
- 重启后 `invokeSubagent` 接着上次对话（子 Agent 历史里能看到重启前那条 user 消息）；
- 目录在、名册无 ⇒ `newSubagent` 返回 `resumed: true`；
- `removeSubagent` 真删目录（含 `checkpoint.db`）；
- 恢复的子代理计入 `max_children`；
- `/realm disable subagents` → `enable`：名册从磁盘读回、投影单份、结果仍可读；
- 名册坏掉 ⇒ realm `failed` 且错误带路径，Agent 其余部分照常。
