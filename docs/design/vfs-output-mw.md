# VFS / 输出存储 / 请求改写中间件语义规格（Liorium 实现基线）

> 状态：行为规格（2026-11-04 冻结）。冻结 VFS、工具输出存储与四个中间件的精确语义。
> 目标读者：在 Liorium 中重写 VFS、工具输出存储与这四个中间件的工程师。
> 依据测试：`tests/{test_mm_bridge,test_tool_call_repair,test_server_tools,test_model_retry_middleware}.py`，另见 `tests/test_vfs_handler_ack.py`、`tests/test_harness.py`。
> 计划锚点：`docs/plans/2026-11-04-liorium-agent-core.md` 的 C8 / D1 / D3 / D4 / D8 / F6 与 §3.7「数据格式冻结」。

本文所有行内 `代码`、引用块与表格里的字符串都是**逐字契约**；`\n` 表示真实换行，`{...}` 表示 f-string 插值点。重实现必须产出逐字节相同的文本与相同的消息顺序。

---

## 0. 六条不可违反的不变量

1. **工具结果文本里永不出现 base64 图片数据。** 图片本体只进 `OutputStore`，文本只留 `artifact://<id>`；`data:` URL 进文本会按文本计 token（历史事故：3.4MB 截图 → 2,550,260 token → 上游 400）。
2. **多模态桥接永不删除历史消息。** 删 `ToolMessage` 会让 `AIMessage(tool_calls)` 悬空，上游立刻 400 `An assistant message with 'tool_calls' must be followed by tool messages`。
3. **`repair_dangling_tool_calls` 只改本次请求**，不写回 checkpoint；checkpoint 允许存在悬空调用。
4. **被包装的工具必须在每次调用时现取 `OutputStore` 单例**（`get_output_store()`），不能在包装期捕获，否则用户点「新对话」后写入旧实例。
5. **VFS 是进程内单例**，路径一律先归一化；插件负责卸载自己挂载的节点，core 不代劳。
6. **write/edit 不做任何行尾归一化**（无 `\r\n`→`\n` 转换），效果完全依赖 Python 文本模式的隐式转换（§2.5）。

---

## 1. VFS 运行时（`tools/vfs.py` 转发到 VFS 运行时）

### 1.1 节点模型 `VfsNode`

普通对象（非 dataclass）：

| 字段 | 类型 / 默认 | 语义 |
|---|---|---|
| `name` | `str`，必填 | 单段名；根为 `''` |
| `is_directory` | `bool`，必填 | 目录不使用 `content` |
| `content` | `Any`，`None` | 普通节点负载（`str`/`bytes`/任意对象） |
| `children` | `dict[str, VfsNode]`，`{}` | 插入序保存，读取时 `sorted()` |
| `symbolic_func` | `(normalized_path) -> Any \| Awaitable[Any]`，`None` | 内容函数，可同步可异步 |
| `should_be_included_in_search` | `bool`，`True` | `False` 时 `search()` 不调用该节点 |
| `writable` | `bool`，`True` | `write_symbolic` 默认置 `False` |
| `description` | `str`，`''` | 列举时展示 `[用途说明]`，空串不展示 |
| `write_handler` / `edit_handler` | `(node, content)`，`None` | 同步/异步皆可；存在时完全接管写入 |

只读派生：`is_symbolic`、`has_write_handler`、`has_edit_handler`。

### 1.2 路径归一化 `normalize_path`

```
str(path or '').strip().replace('\\','/') → 空则 '/'；不以 '/' 开头则补
按 '/' 切分：空段与 '.' 丢弃；'..' 弹上一段（栈空静默忽略）
'/' + '/'.join(segments)，空则 '/'
```

`get_path_parts('/')` → `[]`，其余 → `normalized.lstrip('/').split('/')`。无尾斜杠语义：`/a/b/` 与 `/a/b` 同节点。

### 1.3 `scheme://` → 节点

VFS 运行时**完全不认识 scheme**，`agent://` 由工具层剥离：`write` 用 `raw[len("agent://"):].strip("/")` 再 `"/"+path.strip("/")`；`edit` 用 `"/" + path[len("agent://"):].strip("/")`；`read` 用 `core/uri.py::parse` 拆出 `path`/`:N-M`/`:raw`/`?query` 后补 `/`；`find` **保留尾斜杠**（`p[len("agent://"):]`）以区分「列目录」；`search` 用 `"/" + (rest.strip("/") or "/")`。

其余 scheme 由 `detect_unsupported_protocol(uri, supported)` 拦截：仅当 uri 命中 `_ALL_SCHEMES`（`artifact`/`memory`/`skill`/`agent`/`img_source`/`sourcecode`）且不在 `supported` 时报错，文本为

```
{scheme}:// 协议不支持此操作（支持: {', '.join(f'{s}://' for s in 排序后 supported 去掉 file)} 或直接写文件路径）
```

去掉 `file` 后为空时回退显示 `file://`。

### 1.4 挂载 / 卸载（没有 mount/unmount API）

三个正原语：`mkdir(path, *, description='')`（逐段建，已存在复用，`description` 非空才覆盖）；`write_symbolic(path, func, *, should_be_included_in_search=True, writable=False, description='')`（**总是整体替换**该节点，旧 handler 丢失，`description` 未给则继承旧值）；`set_write_handler` / `set_edit_handler(path, func)`（只对已存在节点生效，否则 `FileNotFoundError`）。另有 `set_description(path, text)`（同样会抛）与 `mount_tree(virtual_root, real_root, *, include_binary=False)`：`rglob('*')` 读入整棵树，UTF-8 文本存 `str`，解码失败且 `include_binary=False` 时**跳过**、否则存 `bytes`，`real_root` 无效时 `raise FileNotFoundError(real_root)`；**当前仓库无调用方**。

**卸载由挂载方自己实现**（事实标准见 `default_plugins/agent-communicate/vfs_surface.py::unmount`）：逐个 `delete(prefix + '/' + name)`，目录空了再 `delete(prefix)`，不变量是「插件卸载后不残留任何该插件节点」。插件入口是 `PluginContext.vfs_write_symbolic` / `vfs_set_write_handler` / `vfs_set_edit_handler` / `vfs_delete` / `vfs_write` / `vfs_list`，全部转发到同一单例。

### 1.5 读 / 写 / 编辑 / 删除

- `exists/is_dir/is_file`：读锁下按段下钻，缺失 `False`。
- `list_dir(path)`：非目录或不存在 → `None`；否则 `sorted(children)`（仅一层，不带 `/`）。
- `walk(path='/')`：前序，父在前、子按名排序，**含起点自身**；路径不存在 `[]`；拼接 `current.rstrip('/')+'/'+child`，根特判 `'/'+child`。`glob(pattern)` = `normalize_path(pattern)` 后 `fnmatch` 过滤 `walk('/')` 全集（`fnmatch` 的 `*` 可跨 `/`）。
- `read(path)`：锁内取 `symbolic_func`/`content` 快照；不存在或目录 → `None`；symbol 节点在**锁外**调用 `symbolic_func(normalize_path(path))`，awaitable 则 await（因此可安全递归调 VFS，`/index.md` 靠这点回调 `refresh_runtime_nodes`）。`read_text(path, default='')` 经 `content_to_text`（`str` 原样；`bytes` 试 UTF-8，失败 `None`；其它 `str(x)`；`None` → `None`），`None` 时返回 `default`。
- `write(path, content, *, writable=True, description='')`：根 → `ValueError('Cannot write to root directory')`；`_ensure_dir_path_unlocked` 自动建父目录，中间段是文件 → `ValueError(f'Path segment is not a directory: {part}')`；**有 handler** 时 VFS 不替换内容，锁释放后调用 `handler(existing, content)`（awaitable 则 await），返回值**仅非空 `str`** 透传、其余归一 `None`；**无 handler** 时若旧节点 `is_symbolic and not writable` → `PermissionError(f'Symbolic node is read-only: {normalize_path(path)}')`，否则**整体替换**为新 `VfsNode(is_directory=False)`（旧 `symbolic_func`/handlers 全丢），`description = description or 旧描述`，返回 `None`。
- `edit(path, edited_content, *, writable=True)`：与 `write` 同构，只换 `edit_handler`，根错误为 `'Cannot edit root directory'`；语义是「提交这份完整新内容」而非 diff。
- `delete(path)`：根 → `ValueError('Cannot delete root directory')`；父不是目录 → `False`；否则 `children.pop(name, None) is not None`（删目录即整棵子树）。

**handler 调用在写锁之外**（`async with` 先结束），故意如此，使 handler 内可继续 await VFS 而不自死锁。

### 1.6 `search` 语义

读锁内递归收集**文件**节点（按名排序），锁外取内容：symbol 节点在 `include_symbolic=False` 或 `should_be_included_in_search=False` 时跳过，否则锁外调用 `symbolic_func(current_path)`；普通节点直接取 `content`；`content_to_text` 非 `None` 且 `str(keyword or '') in text` → 命中并追加绝对路径。即**纯子串、大小写敏感、无行号、无正则**。

### 1.7 锁模型 `AsyncRWLock`

`asyncio.Condition`；**写者优先**：`acquire_read` 在 `writer or writers_waiting > 0` 时等待；读者归零时 `notify_all`；`acquire_write` 自增 `writers_waiting`，等 `not writer and readers == 0`。`read_lock()` / `write_lock()` 返回一次性 async context manager。

### 1.8 错误消息（逐字）

| 触发点 | 消息 |
|---|---|
| 中间路径段是文件 | `Path segment is not a directory: {part}` |
| 写根 / 编辑根 / 删根 | `Cannot write to root directory` / `Cannot edit root directory` / `Cannot delete root directory` |
| symbol 节点只读 | `Symbolic node is read-only: {normalized_path}`（`PermissionError`） |
| 向根注册 symbol | `Cannot write symbolic content to root directory` |
| 根上设 write/edit handler | `Cannot set write handler on root directory` / `Cannot set edit handler on root directory` |
| handler 或描述目标不存在 | `VFS node not found: {normalized_path}`（`FileNotFoundError`） |
| `mount_tree` 源无效 | `FileNotFoundError(real_root)`（消息就是实参字符串） |

### 1.9 内置节点树（`_ensure_core_structure`，单例首次创建时执行一次）

目录 `/plugins`、`/subagents`；以下 symbol 节点全部只读：

| 路径 | 内容 |
|---|---|
| `/tool_use.md` | `_read_task_section('## 核心工具速查')`（读 `AGENT_ROOT/TASK.md` 章节） |
| `/mc.md` | `_read_task_section('## Minecraft 操作系统说明')` |
| `/subagenting.md` | `_subagenting_doc()` 常量协议文本 |
| `/avatoolset` | `'# Available Toolsets For Subagents\n\n' + manager.format_available_toolsets()`；manager 为 `None` 时 `(subagent manager 未初始化)` |
| `/ava_subagent_models` | provider 列表（含 `main_model` 与白名单） |
| `/pc_info` | `# pc_info` + username/platform/python/cwd |
| `/index.md` | `index_doc`：先 `refresh_runtime_nodes`，再枚举根节点生成 `- agent://{item}{'/' if is_dir else ''}` |

`TASK.md` 异常文本：`[找不到 TASK.md: {task_path}]`、`[TASK.md 中找不到章节: {section_title}]`。

`refresh_runtime_nodes(vfs=None)`：清空 `/subagents/*`，再按 `manager.list_statuses()` 为每个 subagent 注册 `overview.md`（`format_subagent_overview`）、`output.md`（`format_subagent_output`）、`finalResult.md`（`format_subagent_final_result`）；**每次调用全量重建**。单例入口 `get_agent_vfs(refresh=False)`（`asyncio.Lock` 双检），`refresh=True` 时再调一次 `refresh_runtime_nodes`；`tools/vfs.py` 里 `VirtualFileSystem = AsyncVirtualFileSystem`。

### 1.10 工具实际使用的 API 面

| 工具 | 调用序列 |
|---|---|
| `read`(agent) | `get_agent_vfs(refresh=True)` → `refresh_runtime_nodes` → 目录：`list_dir` + 每项 `is_dir` + `get_node`(取 `description`)；文件：`is_dir` → `read_text(normalized, default='')` → `_apply_selector_to_text` |
| `write`(agent) | `get_agent_vfs(refresh=True)` → `refresh_runtime_nodes` → `write("/"+path.strip("/"), content)` |
| `edit`(agent) | `get_agent_vfs(refresh=True)` → `read_text(vfs_path, default='')` → `edit(vfs_path, result)` |
| `find`(agent) | `get_agent_vfs(refresh=True)` → `walk('/')` → 逐条 `is_dir` |
| `search`(agent) | `get_agent_vfs(refresh=True)` → 每个 scope 一次 `search(scope, pattern, include_symbolic=True)` |
| 插件 | `write_symbolic` / `set_write_handler` / `set_edit_handler` / `write` / `edit` / `delete` / `list_dir` / `mkdir` |

注意 `read` 与 `write` 都会重建 `/subagents` **两遍**（`refresh=True` 一次 + 显式 `refresh_runtime_nodes` 一次）：语义无差别，仅性能差异。

### 1.11 必须保留的怪癖

- 无 handler 的 `write`/`edit` **替换**节点 → 原 `symbolic_func` 永久丢失。
- 有 handler 的写入不改节点内容：读回仍是 `symbolic_func` 输出，handler 只负责副作用与 ACK。
- `get_node(path)` 在读锁内返回**节点引用**（非快照），调用方可读可变字段。
- `search` 对 symbol 节点逐个 await → 慢节点串行拖慢整次搜索。
- `..` 不会越出根（栈空静默忽略）。

### 1.12 计划已定的差异（Liorium 决策，非现状）

上述九条是**现状**。计划的 D3/D4/D8 与 §3.7 另作了明确取舍，重实现时以计划为准、本文为回归基线：

- **scheme 与挂载点同名**（D3）：`<scheme>://a/b` ≡ `/<scheme>/a/b`，无翻译表；`agent://` 落在 `/`，工具输出落在 `/artifacts` + `artifacts://`，`skill://`→`skills://`、`sourceCode://`→`code://`、`img_source://` 拆成 `screen://`/`camera://`。本文 §1.3 的「工具层剥离前缀」做法将被解析器取代。
- **`mount_vfs(prefix, other_vfs, readonly)` + 环检测**（D4，记 owner 链）：替代「插件直接往同一棵树写节点」，`mount_tree` 这类全量拷贝不保留。
- **节点内容 = 封闭三元联合** `Text | Image(bytes, mime) | Binary(bytes, mime)`（D8）；`read` 按**内容类型**分派而非按 scheme，图片永不内联 base64——与本文 §4.7 / §5 的现状等价物是 `content_type` 字段。
- **中间件顺序由库固定**（C8）：`repair` 永远打开、`model_retry` 永远最内层，realm 只能在外层追加，不可重排。
- **`write`/`edit` handler 契约要显式定义**：现状 handler 收到「完整新内容」并返回 ACK 字符串；`liorium-vfs` 的 `edit(path, old, new)` 已是 diff 形态，重实现必须明确 ACK 与 `edit_handler(new_content)` 的对应关系，否则 `↳ ack` 会静默消失。
- **冻结的仍冻结**（§3.7）：`↳ ack`、`artifact://<id>`、`[完整输出: ...]` 与行选择器输出格式必须逐字保持；`checkpoint.db` 与 memory store 必须能读旧库。
- **测试是唯一可无损转移的资产**（F6）：`test_mm_bridge`、`test_tool_call_repair`、`test_vfs_handler_ack` 等应整体移植，而不是重写断言。

---

## 2. `write` / `edit` 工具与 `_patch_utils.py`

### 2.1 `write`

`async def write(path: str, content: str) -> str`（`@register` + `@tool`，全位置参数），`content = str(content or "")`。分派：`memory://` → `agent://` → 文件系统。

| 情形 | 返回（逐字） |
|---|---|
| path 空 | `错误: path 不能为空` |
| 不支持的 scheme | `write: {err}` |
| 文件越界 | `错误: 不允许写入项目目录外的路径: {file_path}` |
| 文件写异常 | `写入文件出错: {e}` |
| 文件成功 | `已写入 {file_path} ({size} bytes)`，`size = len(content.encode('utf-8'))` |
| agent 路径空 | `错误: agent:// 路径不能为空` |
| agent handler 抛错 | `写入 agent 资源出错: {e}` |
| agent 成功 | `已写入 agent://{path} ({size} bytes)`，有 ACK 时追加 `\n↳ {ack}` |
| memory 路径空 / 模块缺失 / 写异常 / 成功 | `错误: memory:// 路径不能为空` / `(记忆模块不可用)` / `写入记忆库出错: {e}` / `已写入 memory://{path} ({size} bytes)` |

相对路径解析：依次试 `(WORKDIR_ROOT, PROJECT_ROOT, CONFIG_ROOT)`，命中条件 `candidate.parent.exists() or str(base) == str(WORKDIR_ROOT)`，取首个后 `.resolve()`；边界是**字符串前缀比较**（`str(file_path).startswith(str(project_root)) or ...config_root`），同前缀兄弟目录会被放行。父目录 `mkdir(parents=True, exist_ok=True)`；memory 分支额外 `schedule_extract(content, path)`（异常吞掉）。

### 2.2 `edit`

`async def edit(path: str, old_str: str, new_str: str) -> str`。预检先于任何 IO：`old_str == ""` →

```
edit: old_str 不能为空（空串会匹配整个文件）。
处理: 新建文件用 write(path, content)；在文件末尾追加时，old_str 应包含文件末尾的锚点行（先用 read 确认最后一行）。
```

`old_str == new_str` → `edit: old_str 与 new_str 完全相同，本次编辑无任何变更，未写入。`；不支持的 scheme → `edit: {err}`。

| 后端 | 读原文 | 写回 |
|---|---|---|
| `memory://` | `store.file_read(mem_path)["content"]` | `store.file_write` + `schedule_extract` |
| `agent://` | `vfs.read_text(vfs_path, default='')`，**`''` 视为不存在** | `vfs.edit`（只触发 `edit_handler`，不触发 `write_handler`） |
| 文件 | `Path(path)`；相对路径基准**只有** `WORKDIR_ROOT` | `Path.write_text(content, encoding='utf-8')` |

| 情形 | 返回 |
|---|---|
| memory 文档不存在 | `文档不存在: memory://{mem_path}\n建议: 先用 write("memory://{mem_path}", content) 创建文档，再编辑。` |
| memory 读/写失败 | `无法读取记忆文档 memory://{mem_path}: {e}` / `无法写入记忆文档 memory://{mem_path}: {e}` |
| agent 读异常 / 空内容 / 写失败 | `无法读取 agent 文档 {path}: {e}` / `文档不存在: {path}` / `无法写入 agent 文档 {path}: {e}` |
| 文件不存在 | `文件不存在: {file_path}\n建议: 先用 read("{path}") 确认路径，或 write("{path}", content) 创建文件。` |
| 文件读 / 写失败 | `无法读取文件 {file_path}: {e}` / `无法写入文件 {file_path}: {e}` |
| 匹配 0 处 | `edit: old_str 在 {path} 中匹配 0 处，文件未被修改。\n处理: 1) 用 read("{path}") 重新读取，从输出中逐字复制 old_str；2) 检查缩进、行尾空格、全角/半角字符是否一致；3) old_str 是普通字符串不是正则，不要包含 \n 转义符——多行直接写真实换行。` |
| 匹配 n>1 处 | `edit: old_str 在 {path} 中匹配 {n} 处（要求恰好 1 处），文件未被修改。\n处理: 在 old_str 前后多包含几行上下文使其唯一（可用 read("{path}") 查看各处差异）；若确实要替换所有 {n} 处相同片段，请改为逐处编辑（每处 old_str 带不同上下文）。` |
| 成功 | `已编辑 {path} (1 处替换)`，有 ACK 追加 `\n↳ {ack}` |

**不对称怪癖**：`write` 可写 `PROJECT_ROOT`/`CONFIG_ROOT`，`edit` 相对路径只认 `WORKDIR_ROOT`；`edit` 对绝对路径**不做**越界检查。

### 2.3 `replace_exact`（edit 唯一匹配内核）

```python
n = content.count(old_str)
if n != 1: return None, n
return content.replace(old_str, new_str, 1), 1
```

纯字面量、大小写敏感、无正则、无空白归一化、**全文件唯一**才写回；`new_str` 为空串即删除。

### 2.4 `_patch_utils.py` 的 patch 语义（当前无生产调用方，语义仍冻结）

同文件的 `safe_read_file_range(file_path, start_line, end_line)`（返回 `文件不存在: {file_path}` / ``File: `{file_path}`. Empty file.`` / `无效行范围: start_line={s}, end_line={e}` / ``File: `{file_path}`. Lines {a} to {b} ({total} lines total):\n{body}``）与 `find_skill_root` / `install_skill_from_slug`（skill 包下载安装）当前都无生产调用方，属死代码，Liorium 可不移植。

`extract_section_chunks`：`patch_text.splitlines()`（顺带吃掉 `\r`），空 → `ValueError("Patch 为空")`；`lines[0].strip() != "*** Begin Patch"` 或 `lines[-1].strip() != "*** End Patch"` → `ValueError("Patch 必须以 *** Begin Patch 开始并以 *** End Patch 结束")`；遍历 `lines[1:-1]`，段头正则 `^\*\*\*\s+(Add|Update|Delete)\s+File:\s+(.+?)\s*$`，命中即切段（`path` 再 `.strip()`），未命中行（含空行）原样进 `section_lines`；无任何段 → `ValueError("Patch 中未找到任何文件操作段")`。

`apply_update_hunks(original, section_lines)`：只有以 `@@` 开头的行开启新 hunk（`@@` 后内容被忽略、**不解析行号**）；hunk 内 `-` 行去首字符进 `old_lines`、`+` 行去首字符进 `new_lines`、**其它上下文行被完全丢弃**；`old_chunk = "\n".join(old_lines)`。`old_chunk` 非空时，`old_chunk not in content` → `ValueError(f"更新失败，未在文件中找到旧代码块:\n{old_chunk[:200]}")`，命中则 `content.replace(old_chunk, new_chunk, 1)`（**只替换第一处**）；`old_chunk` 空且 `new_chunk` 非空时追加（必要时先补 `\n`）；两者皆空则跳过。hunk 顺序执行，后者看到前者结果。

`apply_patch_text`：`Add` → `p.parent.mkdir(parents=True, exist_ok=True)`，段内每行去掉首个 `+`，`"\n".join`，非空且不以 `\n` 结尾则补 `\n`，`write_text(encoding='utf-8')`，记录 `Add {target_path}`；`Delete` → 存在则 `unlink()`（不存在**不报错**），记录 `Delete {target_path}`；`Update` → 不存在或非文件 `ValueError(f"Update 失败，文件不存在: {target_path}")`，否则 `read_text` → `apply_update_hunks` → `write_text`，记录 `Update {target_path}`；其它动作 `ValueError(f"不支持的 patch 动作: {action}")`。返回 `"Patch 应用成功:\n" + "\n".join(changed)`（顺序 = 段顺序）。

空白结论：`+`/`-` 后的空格、Tab、尾随空格逐字保留；不 dedent、不 trim、无模糊匹配；唯一「锚点」是 `old_chunk` 的精确子串首次出现。

### 2.5 行尾（缺失的）归一化

`Path.read_text` 走通用换行（磁盘 `\r\n` → 内存 `\n`），`Path.write_text` 默认 `newline=None`（写 `\n` 按平台转 `os.linesep`，Windows 即 `\r\n`），因此「读→改→写」在内存里稳定、磁盘字节变 CRLF，且 `write` 报的 `{size} bytes` 按 `\n` 计（小于实际磁盘字节）。`_patch_utils` 同样 `read_text`/`write_text`，`splitlines()` 顺带吃 `\r`。VFS 内容节点是内存对象，**不经任何换行转换**。不要新增显式归一化：`edit` 的逐字符契约依赖「读回的 `\n` 与模型写的 `\n` 一致」。

---

## 3. `execute` 工具

签名 `async def execute(language: str, code: str, *, timeout: int = 30, cwd: str = "") -> str`；`timeout = max(5, min(int(timeout), 300))`，`lang = language.strip().lower()`。分派：`shell`、`python`（`sys.executable -c code`）、`js`（`bun` → `LIORIUM_NODEJS`（须是文件）→ `<PROJECT_ROOT 父>/.nodejs/node.exe` → `node`，按序试、`FileNotFoundError` 跳过，全败返回 `找不到 JavaScript 运行时 (bun/node)`）；`java` 且 `code == "world.execute(me);"` → 返回 `EXECUTION` 并 `log.critical("ILLEGAL ARGUMENT")`（彩蛋，保留）；其它 → `不支持的语言: {language}。支持 shell`。

**Shell 解析**：候选 `("zsh","bash","sh","pwsh","powershell","cmd")`；参数前缀 `pwsh`/`powershell` → `("-NoProfile","-Command")`、`cmd` → `("/c",)`、其余 `("-c",)`。`shutil.which` 命中后再真正探测 `(*argv, "exit 0")`（10s 超时，返回码 0 才算可用；超时则杀进程跳过），结果缓存进模块级 `_shell_argv`/`_shell_resolved`（`asyncio.Lock` 保护，**进程内只探一次**）；全不可用 → `找不到命令解释器`。

**进程与 cwd**：`asyncio.create_subprocess_exec(*argv, command, stdout=PIPE, stderr=PIPE, cwd=work_dir)`；无 `shell=True`、无 env 覆盖（python 额外 `env.setdefault("PYTHONIOENCODING","utf-8")`）；`work_dir = cwd or WORKDIR_ROOT`，`cwd` **原样传入、不校验**，非法 cwd 的 `FileNotFoundError` 与「解释器不存在」共用同一条 `找不到命令解释器`；stdout/stderr 全量进内存。

**危险命令（仅 shell）**：`SECURITY_SYS_ENABLED` 为假（默认）直接执行；`security_check_command` 导入失败（`ImportError`）也直接执行；`ok = await security_check_command(command)`，`not ok` → `安全检查拒绝执行该命令`，抛异常 → `f"安全检查失败，拒绝执行命令: {e}"`（失败即拒绝，不降级放行）。`python`/`js` **不做**安全检查。

**超时与终止**：`asyncio.wait_for(proc.communicate(), timeout=timeout)`；超时后 Windows 用 `taskkill /F /T /PID {pid}`（杀进程树，10s 上限后 `kill()`），其它平台 `proc.kill()`，再次 `wait_for(proc.communicate(), timeout=5)` 并吞异常。返回 `命令超时 ({timeout}s)` / `Python 执行超时 ({timeout}s)` / `JS 执行超时 ({timeout}s)`。解释器不存在：shell 与非法 cwd 均为 `找不到命令解释器`，python 为 `找不到 Python 解释器`，js 为 `找不到 JavaScript 运行时 (bun/node)`。

**返回文本**：按序 `"\n".join` —— ① `stdout.decode('utf-8', errors='ignore').strip()`（python/js 用 `errors='replace'`）② `f"[stderr]\n{stderr...strip()}"` ③ `f"[exit code: {proc.returncode}]"`（仅非 0）；全空 → `(无输出)`。空 stdout 不产生空段。**工具内无输出上限**：截断只发生在包装层（§4.6，>60000 字符或 >300 行换摘要）。

---

## 4. `OutputStore`（`agent/runtime/turn/output_store.py`）

### 4.1 `Artifact` 数据模型

| 字段 | 类型 / 默认 | 说明 |
|---|---|---|
| `output_id` | `str` | 形如 `shell_3` |
| `content` | `str` | 文本负载（多模态时为描述文本） |
| `tool_name` | `str` | 原始工具名 |
| `content_type` | `"text" \| "image" \| "multimodal"`，`"text"` | 决定 `summary()` 分支 |
| `content_base64` | `str \| None` | 二进制 base64（不含 `data:` 头） |
| `mime_type` | `str \| None` | 如 `image/png` |
| `metadata` | `dict` | 多模态含 `image_count`、`images` |
| `created_at` | `float` | `time.time()` |

派生：`size = len(content) + len(content_base64 or "") // 4 * 3`；`lines = content.count("\n") + (1 if content else 0)`。

### 4.2 ID 生成

`_short_tool_name`：`strip()`（空 → `"tool"`）→ 去后缀 `Tool`/`_tool`/`Func` → 查表 → 否则 `name.lower()[:12]`。表：`sys_exec→shell`、`python_exec→py`、`read_text_file→read`、`write_text_file→write`、`list_directory→ls`、`memory_search→mem`、`request_human_approval→hil`、`show_nimble_window→nimble`。ID = `f"{short}_{counter}"`，`counter` 从 0 起、`put`/`put_multimodal` 自增、随持久化保存、`clear()` 归零。

### 4.3 持久化 / 单例 / 重置

文件 `os.path.join(state.AGENT_ROOT, "artifact.json")`，JSON `{"counter": int, "artifacts": [{八个字段}]}`，`ensure_ascii=False, indent=2`，每次 `put`/`put_multimodal`/`clear` 全量重写并自动建父目录。`_load()` 对缺失文件与解析失败**静默返回**，逐条容错重建。`get_output_store()` 每次解析 `AGENT_ROOT`，`_store is None` 或 `storage_path` 变化则新建；`state` 不可用时 `storage_path = ""`（纯内存）。

`reset_output_store(*, clear_persisted=False)`：`clear_persisted=True` 时先 `clear()`（空 payload 写回），无实例则删文件；随后 `_store = None`。生产调用点仅 `api/chat/chat.py::_clear_session_locked`（新对话/清空对话）。**不存在 per-turn store**：作用域是「进程 + AGENT_ROOT」，跨 turn 累积直到显式 reset；Liorium 若引入 per-turn 隔离，必须保证 `artifact://` 的跨 turn 可读性不回退。

### 4.4 `summary()` 模板（逐字）

```python
if content_type == "multimodal":
    n = metadata.get("image_count", 0); desc = content or "图片输出"
    return f"[{desc} — {n}张图片: artifact://{output_id}]"
if content_type == "image":
    return f"[图片: artifact://{output_id}]"
if not content:
    return "(空输出)"
```

截断：`len(lines) <= max_lines and len(content) <= max_chars` 原样返回，否则

```python
preview = "\n".join(content.split("\n")[:max_lines])
if len(preview) > max_chars: preview = preview[:max_chars] + "…"   # 全角省略号
return preview + f"\n[内容过长,已经截断.请使用行数选择器读取完整输出: artifact://{output_id}]"
```

常量 `DEFAULT_MAX_SUMMARY = 60000`、`DEFAULT_MAX_LINES = 300`。footer 内是半角逗号与句点（`内容过长,已经截断.请使用...`），不要「修正」成中文标点。`store.summary(id)` 对未知 id 返回 `[找不到 artifact: {output_id}]`。

### 4.5 `get` / `peek` / `list_ids`

`Artifact.get(offset=0, limit=None)`：按 `\n` 切分后切片，`offset` **0 起**；`limit=None` 到末尾；空内容 → `""`；图片**不在此**还原 data URL。`peek(id)` → `{"output_id","tool_name","size","lines","created_at"}`，缺失 `None`。`list_ids()` 按 `created_at` 升序（最新在最后）。`__contains__` 直接查 dict。

### 4.6 工具包装层如何写入 store（`middleware/__init__.py`）

- `_output_store_wrapped` 标记保证**只包装一次**（主 Agent 与 Subagent 共享同一批工具对象）。
- async `@tool` 只包装 `_arun`；sync `@tool` 包装 `_run` 且 `_arun` 走 `asyncio.to_thread`，并打警告 `SYNC TOOL WILL BE NO LONGER SUPPORTED IN FUTURE VERSIONS.`。
- 调用前后各给插件 `tool_call_pre` / `tool_call_post` 一次改写机会（异常静默）。
- 工具抛异常 → `store.put(str(e), tool_name=..., metadata={"status": "error"})`，返回 `工具执行出错\n[完整输出: artifact://{output_id}]`。
- 多模态 JSON（`str` 可解析或 `dict`，且 `kind == "multimodal_tool_result"`）：已是引用形态（有 `artifact` 无 `images`）→ **不重复存**、原样 `json.dumps`；否则 `put_multimodal` 后返回

  ```json
  {"kind": "multimodal_tool_result", "text": "{text}\n[{count} 张图片已保存: artifact://{output_id}]", "artifact": "{output_id}"}
  ```

- 普通结果：`str` 原样、`dict`/`list` → `json.dumps(..., ensure_ascii=False)`、其它 `str(...)`；**`len <= 120` 且不含 `\n` → 直接返回不建 artifact**；否则 `put` + `store.summary(id)`。
- 多模态 payload 生产者形状：`{"kind":"multimodal_tool_result","text":"...","images":[{"url":"data:image/png;base64,..."}],"meta":{...}}`（`meta` 可选，`img_source://` 带 `grid`/`scale`/`camera_id`）。

### 4.7 `read artifact://<id>` 的重发语义（`tools/read.py::_read_artifact`）

| 情形 | 行为 |
|---|---|
| 无 id | 空 store → `(没有可用的 artifact)`；否则 `"可用的 artifact:\n"` + 最后 20 个 `"  artifact://{aid}"` |
| id 不存在 | `[找不到 artifact: {output_id}]` |
| 有行选择器，或 `show_line_number` 且非 image/multimodal | `_apply_selector_to_text(art.content, ...)`（1 起绝对行号，`5:line` 形式） |
| image/multimodal 且 `force_plain_text=True` | 仅 `art.content or f"图片 artifact: {output_id}"` |
| image/multimodal 未强制纯文本 | 引用 JSON：`{"kind":"multimodal_tool_result","text":"{desc}\n[图片: artifact://{id}]","artifact":"{id}"}`（`ensure_ascii=False`） |
| 普通文本 | `art.get()`（全文） |

重发引用 JSON 经包装层命中「已是引用形态」分支，**不会再存一份**。

---

## 5. `MultimodalBridgeMiddleware`（`middleware/mm_bridge.py`，内置常开）

### 5.1 扫描与去重

`before_model(state, runtime)`（`abefore_model` 直接委托同步实现）：空 `messages` → `None`；刷新 `_max_pixels = max(0, int(getattr(conf,'MM_BRIDGE_MAX_PIXELS',2000000) or 2000000))`（默认 `2_000_000`）；`bridged = _bridged_source_keys(messages)`（遍历**全部消息、不限类型**，取 `additional_kwargs[SOURCE_KEY_FIELD]`，键名常量 `_mm_bridge_source`，非空入集合）；`for msg in reversed(messages)` 只处理 `ToolMessage`，key 已在 `bridged` 则跳过；解析 payload → 转消息成功则 `bridged.add(key)` 并收集；无新增 → `None`，否则 `additions.reverse()`（恢复时间序）返回 `{"messages": additions}`。

**扫描范围 = 请求全量消息**，无「回看 N 条」上限（旧 6 条窗口已被回归测试禁止）。`_tool_message_key`：优先 `str(msg.id)`；缺 id 时 `f"fallback:{tool_call_id}:{sha256(str(content).encode('utf-8')).hexdigest()[:16]}"`（跨进程稳定）。

### 5.2 payload 识别

`content` 是 `dict` → 直接用；`str` → `json.loads`（`JSONDecodeError`/`TypeError` → `None`）；其它 → `None`。最终必须是 `dict` 且 `data.get("kind") == "multimodal_tool_result"`。

### 5.3 图片 URL 解析与缩放

`_image_urls`：① `payload["images"]` 规范为 URL 列表（元素是非空 `str` 直接用；是 `dict` 且有 `url` 用 `str(url)`）；② 无 URL 时看 `payload["artifact"]`（`strip()` 非空）→ `get_output_store().get(id)`：artifact 不存在 → `[]`（安静跳过，Agent 仍可 `read artifact://<id>` 复看）；否则 `art.metadata["images"]` 有 URL 就用；否则 `art.content_base64` → `[f"data:{art.mime_type or 'image/png'};base64,{art.content_base64}"]`；否则 `[]`。

`_maybe_resize_image_url`：`not url or max_pixels <= 0` → 原样返回（`max_pixels=0` 关闭缩放；空 url → `None`）；非 `data:` URL 原样透传；`data:` 头无 `;base64` 原样透传；解码 → `Image.open` → `w*h <= max_pixels` 原样返回原 URL（**不重编码**）；否则 `scale = sqrt(max_pixels/(w*h))`、`new_w/new_h = max(1,int(w*scale))`、`Image.Resampling.LANCZOS`，格式 `img.format or "PNG"`、非 PNG/JPEG/WEBP 一律回落 `PNG`（`resize` 产物 `format` 为 `None`，实际几乎总是 PNG），MIME 沿用原 `data:` 头；任何异常 → 原样返回原 URL。

### 5.4 消息形状（逐字）

```python
blocks = []
if payload.get("text"): blocks.append({"type": "text", "text": str(payload["text"])})
blocks.extend({"type": "image_url", "image_url": {"url": <url>}} for 每个可用图片)
return HumanMessage(content=blocks,
    additional_kwargs={"_mm_bridge_generated": True, "_mm_bridge_source": source_key})
```

一张可用图片都没有 → 返回 `None`（不注入；`ToolMessage` 已有描述文本）。统一产出 **OpenAI 风格** `{"type":"image_url"}` 块，Anthropic / Gemini 由各自 SDK 转换（→ `image/source`、→ `inline_data`），**不要手写各格式图片块**。桥接消息是**追加**的 `HumanMessage`（LangGraph `messages` 归约为 add），位于末尾、内部时间序升序。

### 5.5 硬约束

永不删除历史消息：不产生 `RemoveMessage`、不动源 `ToolMessage`；图片收敛交给 `CompactMiddleware`（连同 tool 组一起整理）。注入幂等：同一源最多一条桥接消息，进程重启、重复扫描、压缩丢弃后重注入都不重复。只处理 `ToolMessage`；其余类型即使内容像 payload 也不碰。

---

## 6. `ToolCallRepairMiddleware`（`middleware/tool_call_repair.py`，内置常开）

### 6.1 占位文本（逐字）

```python
PLACEHOLDER_CONTENT = "[工具调用结果缺失，已由 Liorium 自动补全]"
```

占位消息：`ToolMessage(content=PLACEHOLDER_CONTENT, tool_call_id=<id>, name=<工具名 or None>)`。

### 6.2 两种「工具调用」来源

`_all_tool_calls(msg) = msg.tool_calls + msg.invalid_tool_calls`（参数 JSON 解析失败）。理由：`langchain_openai` 的序列化条件是 `if message.tool_calls or message.invalid_tool_calls`，两者都会变成上游 `tool_calls`，都必须被逐条响应。`server_tool_use` / `web_search_call` 不参与（无 `tool_call_id`、不需要响应）；清理 content 时只认 `_TOOL_CALL_BLOCK_TYPES = {"tool_use","function_call","tool_call"}`。

### 6.3 主算法（单趟状态机）

```
repaired = []; pending: dict[tool_call_id → (name, ai_index)]; injected = 0; dropped = 0
对每条 message：
  AIMessage:
    calls = tool_calls + invalid_tool_calls
    若 calls 非空:
        若 pending 非空 → drop_pending()        # 前一批被新调用打断 = 作废
        ai_idx = len(repaired); repaired.append(message)
        对每个 call: id 非空 → pending[id] = (name, ai_idx)   # 空 id 被忽略
    否则:
        若 pending 非空 → drop_pending(); repaired.append(message)
  ToolMessage:
    若 id 且在 pending → pending.pop(id); repaired.append(message)
    否则 → dropped += 1                        # 孤立 ToolMessage 直接丢弃
  其它（Human/System/…）:
    若 pending 非空 → drop_pending(); repaired.append(message)
序列结束若 pending 非空 → flush_pending()
```

`drop_pending()`：对每个 pending id，取 `repaired[ai_idx]`（仍是 `AIMessage` 且「还有任何 tool_call」时）→ `repaired[ai_idx] = _without_tool_call(ai, id)`，`dropped += 1`；随后清空。`flush_pending()`：按 pending 插入序（= 调用出现序）**追加**占位，`injected += 1`；随后清空。日志仅在 `injected or dropped` 时输出：`ToolCallRepair: 注入 %d 个占位 ToolMessage，作废 %d 个被打断的 tool_calls`。

### 6.4 `_without_tool_call` 的三重清理

① `model_copy(deep=True)`（不改入参）；② 从 `tool_calls` 与 `invalid_tool_calls` 删掉该 id；③ `content` 是 `list` 时删掉 `type ∈ {tool_use, function_call, tool_call}` 且 `id == tool_call_id` 的 dict 块。只删结构化列表不够：Anthropic 的 `tool_use`、Responses 的 `function_call` 是 content block，序列化时会重新变成调用声明，仍会 400（`tool_use ids were found without tool_result blocks`）。

### 6.5 边界与交互

- **纯函数**：返回新列表，入参与元素不被修改。
- `request.messages` 为空 → 直接 `handler(request)`，无 `override`。
- 入口是 `wrap_model_call` / `awrap_model_call`（**模型调用级**），经 `request.override(messages=repair(...))` 生效 → 只改本次请求，不写回 checkpoint。
- **与压缩**：`CompactMiddleware._acompact` 在追加 `COMPACT_INSTRUCTION` **之前**调用同一函数；顺序关键——先修复，末尾悬空才走「注入占位」而不是被随后追加的 Human 判成「打断作废」。
- **与中断**：`GraphBubbleUp`（`interrupt()`）不经过本层；用户新消息打断表现为序列中的 `HumanMessage` → 归入「作废」分支（清空 `tool_calls`），**不注入占位**。
- **与 server tools**：`server_tool_use` 块永不被删；这类消息没有 `tool_calls` 列表，不进 pending。
- 空 `tool_call_id`：不进 pending（既不会被占位也不会被清空）。
- 同一 id 重复出现：`pending` dict 覆盖，最后一个生效，占位只有一个。
- 「t1 已响应、t2 未响应且已到序列结尾」→ t2 走 `flush_pending` 注入占位。

---

## 7. `ModelRetryMiddleware` 接线（`middleware/model_retry.py`）

既有实现只做**参数固定与去重接线**，重试逻辑来自 langchain 的 `langchain.agents.middleware.ModelRetryMiddleware`。

常量：`INITIAL_DELAY = 1.0`、`BACKOFF_FACTOR = 2.0`、`MAX_RETRIES = 2`（不含首次调用）。构造 `ModelRetryMiddleware(max_retries=2, backoff_factor=2.0, initial_delay=1.0, on_failure="error")`，其余取库默认（`retry_on=default_retry_on`、`max_delay=60.0`、`jitter=True`）。

`with_model_retry(middlewares=None)`：过滤掉所有已存在的 `ModelRetryMiddleware`（**替换而非叠加**，避免双重退避），新实例**追加到末尾**（链首最外、末尾最贴近真实模型调用，因此只重放模型请求本身），返回 `list[AgentMiddleware]`。三处装配点都必须经过：主 Agent（`build._compose_runtime_extensions`）、Subagent（`SubagentManager.newSubagent`）、Araya（`ArayaRuntime._init_agent`）。

`default_retry_on`：`ModelError` 看 `exc.is_retryable`，**其余异常一律重试**。

| 异常 | 重试 |
|---|---|
| `ModelRateLimitError`(429)、`ModelAPIError`(5xx)、`ModelConnectionError`、`ModelTimeoutError` | ✅ |
| `ModelAuthenticationError`(401)、`ModelPermissionDeniedError`(403)、`ModelInvalidRequestError`(400)、`ModelNotFoundError`(404)、`ContextOverflowError` | ❌ 立即抛出 |
| 非 `ModelError` 的普通异常（如 `ConnectionError`） | ✅ |
| `GraphBubbleUp`（`interrupt()`） | ❌ **显式 re-raise，永不重试** |

当前实现**没有**直接的状态码判断；Liorium 若要按状态码区分，须在 wrapper 层自定义 `retry_on`。

退避：`delay = min(initial_delay * backoff_factor ** attempt, 60.0)`（`attempt` 从 0 起），再叠加 `±25%` jitter 并截到 `>= 0`；同步 `time.sleep`、异步 `asyncio.sleep`。尝试次数 `max_retries + 1 = 3`，最坏额外等待 ≈ 3 秒（不含 jitter）。

失败语义：`on_failure="error"` → 重试耗尽后**原样 re-raise 同一异常对象**，不注入 `AIMessage`、不吞异常、不降级。作用域仅 `wrap_model_call`/`awrap_model_call`（模型调用）：工具执行失败不走这里（`ToolRetryMiddleware` 未挂），外层逻辑不会因重试重跑。**流式**：重试在 handler 内层，已被下游接收的增量 token 无法撤回，因此流式中途失败后重试可能让前端出现重复片段；本项目没有缓冲/去重层，也**不发射**任何重试事件（`compact_*` 事件由 lifecycle/compact 产生），且无日志输出。

---

## 8. `ServerToolMiddleware`（`middleware/server_tools.py`）

`SERVER_TOOL_FORMATS` 目前只有 `web_search` 一个工具：

| api_format | spec |
|---|---|
| `openai_responses` | `{"type": "web_search"}` |
| `anthropic_messages` | `{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}` |
| `google_gemini` | `{"google_search": {}}` |
| `openai_chat` | **不支持** → `ValueError(f"API 格式 {api_format} 不支持服务端工具 '{target}'（支持: ...）")` |

未知工具名 → `ValueError(f"未知的服务端工具: {target}")`；空名跳过；每次返回 `dict(spec)` 副本。

`resolve_server_tool_specs(providers, spec)`：`spec` 空 → `[]`；`parse_spec` 得 `provider_name, model_name`；按 `p.name == provider_name` 找 provider，找不到 → `[]`；`names = provider.model_server_tools.get(model_name) or []`，空 → `[]`；`decision = resolve_route(provider, model_name)`（手动 > provider 默认 > models.dev 目录 > base_url 路径 > 多格式族推断 > 回退 `openai_chat`）；`build_server_tool_specs(decision.api_format, names)`。**失败即抛错不静默**：用户勾了服务端工具却因配置不生效，会表现为「明明开了联网搜索却不会搜」的假故障。

注入：`__init__` 内 `self.specs = [dict(s) for s in (specs or [])]`；`wrap_model_call`/`awrap_model_call` 在 `specs` 为空时**原样透传**（`requests[0] is original`，不 `override`），否则 `handler(request.override(tools=[*request.tools, *self.specs]))`——服务端 specs 永远追加在本地工具**之后**（`tools[0]` 是本地工具、`tools[-1]` 是 spec）。这些 spec 不进 `ToolNode`、不产生 `tool_calls`，结果以 `server_tool_use` / `web_search_call` / `google_search_result` 等 content block 回到消息。`build_server_tool_middleware`：解析失败先 `log.error("服务端工具配置无效（spec=%s）: %s", ...)` 再 re-raise；无 specs → `None`；有则 `log.info("模型 %s 启用服务端工具: %s", ...)` 并返回实例。

与压缩协作：`CompactMiddleware` 的工具前缀 = `mock_tools + server_tool_specs`（与主请求逐字节一致以命中前缀缓存），并用 `forced_no_tool_choice(api_format)` 阻止摘要真的联网：`openai_chat`/`openai_responses`/`google_gemini` → `"none"`；`anthropic_messages` → `{"type": "none"}`（字符串 `"none"` 会被解释成「调用名为 none 的工具」）。绑定失败逐级降级：去 `tool_choice` → 去 server specs → 只绑 `mock_tools` → 不绑工具。

---

## 9. 测试断言清单（逐条）

### 9.1 `tests/test_mm_bridge.py`（8 个）

共用：`_ref_message` 造只有引用的 ToolMessage（`{"kind":"multimodal_tool_result","text":"截图\n[图片: artifact://<id>]","artifact":"<id>"}`）；`_image_urls` 只收带 `_mm_bridge_generated` 的 `HumanMessage` 的 `image_url` 块；fixture `isolated_output_store` 把 `AGENT_ROOT` 指到 `tmp_path`。

| 测试 | 断言 |
|---|---|
| `test_bridge_rehydrates_image_from_artifact_ref` | `MM_BRIDGE_MAX_PIXELS=640000`、源图 1200×1200 → 恰好 1 个 URL；以 `data:image/png;base64,` 开头；解码后 `w*h <= 640000` 且 `width < 1200` |
| `test_bridge_skips_ref_without_artifact` | artifact 不存在时 `before_model` 返回 `None` |
| `test_bridge_never_removes_messages` | 返回非空；无 `RemoveMessage`；**全部**是 `HumanMessage` |
| `test_bridge_is_idempotent_across_restart` | 首次注入长度 1；把结果并入 state 后再调 → `None` |
| `test_bridge_injects_only_unbridged_sources` | 两个源、第一张已桥接 → 第二次只返回 1 条且含 image URL |
| `test_bridge_scans_all_messages_without_scan_limit` | 引用消息后再挂 6 条普通 ToolMessage，仍能桥接（长度 1、含 URL） |
| `test_bridge_message_converts_to_anthropic_image_block` | `ChatAnthropic._get_request_payload` 后 content 含 `type=="image"` 块，`source.type=="base64"`、`source.media_type=="image/png"` |
| `test_bridge_message_converts_to_gemini_inline_data` | `_parse_chat_history` 后存在 `part.inline_data` |

### 9.2 `tests/test_tool_call_repair.py`（15 个）

| 测试 | 断言 |
|---|---|
| `test_dropped_call_also_removes_content_block` | `ai.tool_calls == []`；content 只剩 `["text"]`（`tool_use` 块被删） |
| `test_dropped_function_call_block_for_responses` | content 只剩 `[]`（`function_call` 块被删） |
| `test_server_tool_blocks_are_never_removed` | 长度仍 2；`repaired[1].content == ai.content`（原样保留、不注入占位） |
| `test_complete_sequence_is_unchanged` | 完整序列 `repaired == messages`（逐元素相等） |
| `test_interrupted_tool_call_is_voided_not_placeholder` | 无任何 `ToolMessage`；`t1` 不在 tool ids；`ai.tool_calls == []` |
| `test_partial_response_then_interrupt_voids_remaining` | `t1` 在、`t2` 不在；`ai.tool_calls` 剩余 id 列表 `== ["t1"]` |
| `test_dangling_tool_calls_at_end_get_placeholder` | `t1` 在 tool ids；占位 `content == PLACEHOLDER_CONTENT` |
| `test_orphan_tool_message_is_dropped` | `ghost` 不在 tool ids；AIMessage 保留 |
| `test_tool_message_with_unknown_id_after_pending_is_dropped` | `t2` 与 `t1` 都不在 tool ids（未知 id 丢弃 + pending 被后续 Human 作废） |
| `test_original_list_not_mutated` | 调用后入参列表 `== snapshot` |
| `test_trailing_invalid_tool_call_gets_placeholder` | `bad1` 在 tool ids；占位 `name == "read_file"` |
| `test_interrupted_invalid_tool_call_is_voided` | 无 `ToolMessage`；`ai.invalid_tool_calls == []` |
| `test_valid_and_invalid_tool_calls_in_same_message` | tool ids `== {"t1","bad1"}`；`tool_calls` 保留 `["t1"]`；`invalid_tool_calls` 保留 `["bad1"]` |
| `test_wrap_tools_skips_non_base_tool_without_crashing` | 传入 `[BaseTool, lambda]` → 长度 1 且 `result[0] is sample_tool` |
| `test_wrap_model_call_forwards_repaired_messages` | handler 被调用；转发消息里 `t1` 不在 tool ids；`ai.tool_calls == []` |

### 9.3 `tests/test_server_tools.py`（12 个）

| 测试 | 断言 |
|---|---|
| `test_specs_are_copied_per_instance` | 构造后把原 dict 改成 `"mutated"`，中间件仍是 `[{"type": "web_search"}]` |
| `test_sync_wrap_appends_specs_after_local_tools` | `seen` 非空；`tools[0] is local_tool`；`tools[-1]["name"] == "web_search"` |
| `test_async_wrap_appends_specs` | `seen[0][0] is local_tool`；`seen[0][-1] == {"google_search": {}}` |
| `test_empty_specs_is_passthrough` | `requests[0] is original`（未 override） |
| `test_resolve_specs_for_each_format` | `GW::gpt-model` → `[{"type":"web_search"}]`；`GW::claude-model` 的 `[0]["type"] == "web_search_20250305"` |
| `test_resolve_specs_rejects_chat_format` | `openai_chat` 抛 `ValueError` |
| `test_resolve_specs_empty_when_not_enabled` | 未配置 → `[]` |
| `test_build_middleware_returns_none_without_tools` | 返回 `None` |
| `test_build_middleware_returns_instance_with_tools` | `isinstance(..., ServerToolMiddleware)`；`specs == [{"type":"web_search"}]` |
| `test_repair_keeps_server_tool_blocks_and_injects_nothing` | 长度 2；`repaired[1].content == ai.content`；无 `ToolMessage` |
| `test_build_server_tool_specs_is_stable_for_all_formats` | responses → `[{"type":"web_search"}]`；gemini → `[{"google_search":{}}]` |
| `test_compact_binds_server_specs_with_mocks` | `tool_choice == "none"`；`tools[0].name == "local_tool"`；`tools[-1] == {"type":"web_search"}` |

### 9.4 `tests/test_model_retry_middleware.py`（6 个）

`_FlakyModel`：前 `fail_times` 次 `_generate` 抛 `ConnectionError("transient model failure")`，之后返回 `content="pong"`。

| 测试 | 断言 |
|---|---|
| `test_with_model_retry_replaces_instead_of_stacking` | 仅 1 个 `ModelRetryMiddleware`；`stack[-1] is not stale`；`isinstance(stack[0], ToolCallRepairMiddleware)` |
| `test_transient_model_failure_is_retried` | `model.calls == MAX_RETRIES + 1`（=3）；最后一条消息 `content == "pong"` |
| `test_exhausted_retries_raise_original_error` | 抛 `ConnectionError`；`model.calls == MAX_RETRIES + 1` |
| `test_main_agent_composition_gets_retry_middleware` | `tools == []`；1 个重试中间件；`middlewares[-1].__class__.__name__ == "ModelRetryMiddleware"` |
| `test_subagent_agents_get_retry_middleware` | 3 次 `newSubagent`（默认 / 显式中间件 / 已含重试的组合栈）都只得到 **1 个** |
| `test_araya_agent_gets_retry_middleware` | `_init_agent()` 传入的 `middleware` 里恰好 1 个 |

### 9.5 建议一并移植的相关锁死测试（不在本次清单内）

`tests/test_vfs_handler_ack.py`：`write`/`edit` 返回 handler 的非空 `str`；`None`/`""`/`dict`/`list`/`int` → `None`；无 handler → `None`；`↳ ` 追加（多行 ACK 保留换行、无 ACK 时输出不含 `↳`、handler 抛错输出 `写入 agent 资源出错` + 原始异常文本）；`edit` 只触发 `edit_handler`（拿到替换后的**完整新内容**），普通内容节点仍直接替换。`tests/test_harness.py`：ID 前缀（`sys_exec→shell_`、`python_exec→py_`）；`read artifact://` 全文/行范围/缺失（含 `找不到`）；图片 artifact 的 `force_plain_text` 不含 `base64`；多模态结果文本无 `data:image`/`base64` 且带 `artifact://<id>`；2.6MB base64 → 结果长度 `< 1000`；已引用形态不重复存；reset 后写入的 artifact 仍可解析。
