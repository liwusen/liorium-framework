# Liorium API 参考

逐包列出**公开面**（`__all__`）的签名、语义与关键不变量。签名逐字抄自
`packages/*/src/**`；列为「子模块导出」的名字不在包的顶层 `__all__` 里，导入路径写在条目上。
默认值与「不变量」都以源码为准，本文与代码冲突时以代码为准。

约定：

- 12 个模块共用同一个顶层包名：`from liorium.agent import LioriumAgent`。开发期这 12 个目录
  各自是可编辑安装的命名空间包；发布版把它们打进同一个 wheel，`liorium/__init__.py` 提供
  `__version__`。两种情况下的导入路径完全一致；
- 所有 dataclass 都是 frozen（除 `RealmContext`、`RealmRecord`、`ToolRuntime` 等运行期对象）；
- 「不变量」是本仓库用测试钉住的行为（`tests/test_architecture.py`、`tests/test_event_contract.py` 等）。

---

## 1. `liorium.core`

`import liorium.core as core`。零第三方依赖（架构守卫会检查），无 IO。

### 1.1 内容模型与事件

| 名字 | 签名 / 字段 | 语义与不变量 |
|---|---|---|
| `Text` | `Text(text: str)` | 文本节点内容；`text` 必须是 `str`，否则 `TypeError` |
| `Image` | `Image(data: bytes, mime: str)` | 图片节点内容；`read` 走多模态路径，**绝不**把 base64 内联进文本 |
| `Binary` | `Binary(data: bytes, mime: str)` | 非图片二进制；`read` 只给「类型 + 大小 + artifact 引用」 |
| `VfsContent` | `Union[Text, Image, Binary]` | 封闭三元联合；新增成员是破坏性变更 |
| `content_to_text` | `content_to_text(content: VfsContent) -> str \| None` | 投影成可搜索文本；非 `Text` 返回 `None`（`search`/`find` 跳过） |
| `content_size` | `content_size(content: VfsContent) -> int` | 字节体积（`Text` 按 UTF-8 编码计） |
| `content_kind` | `content_kind(content: VfsContent) -> Literal["text", "image", "binary"]` | 稳定标签，日志/审计用，勿改 |
| `ContextItem` | `ContextItem(source: str, text: str, meta: dict = {}, interrupt: bool = False)` | 一条注入上下文；`source` 不能为空、`text` 必须是 `str`；`interrupt` 当前只被记录、不生效 |
| `render_context_block` | `render_context_block(items: list[ContextItem], at: str) -> str` | 渲染成可并入 user 消息的文本块；`at` 是宿主时钟的 `HH:MM` |
| `Event` | `Event(type: str, payload: dict = {})` | 一条事件；未知 `type` 抛 `ValueError`。`to_dict()` 返回 `{"type": ..., **payload}` 扁平形态；支持 `event["k"]` / `event.get("k")` |
| `AGENT_EVENT_TYPES` | `("start", "delta", "reasoning_delta", "tool_start", "tool_result", "compact_start", "compact_delta", "compact_done", "done")` | `run()` 只产出这 9 种（`tests/test_event_contract.py` 冻结） |
| `ALL_EVENT_TYPES` | 等于 `AGENT_EVENT_TYPES` | `Event` 的合法类型全集（宿主事件已于 2026-11-08 删除） |
| `ev_start` | `ev_start(turn_id: str) -> Event` | 事件构造快捷方式（其余 `ev_*` 同理） |
| `ev_delta` | `ev_delta(content: str) -> Event` | |
| `ev_reasoning_delta` | `ev_reasoning_delta(content: str) -> Event` | |
| `ev_tool_start` | `ev_tool_start(tool_name: str, args: Any, call_id: str) -> Event` | |
| `ev_tool_result` | `ev_tool_result(tool_name: str, output: str, call_id: str) -> Event` | |
| `ev_compact_start` | `ev_compact_start() -> Event` | payload 为空 |
| `ev_compact_delta` | `ev_compact_delta(content: str) -> Event` | |
| `ev_compact_done` | `ev_compact_done(ok: bool) -> Event` | 压缩中间件失败时会直接构造 `Event("compact_done", {"ok": False, "error": "..."})` |
| `ev_done` | `ev_done(turn_id: str) -> Event` | |

### 1.2 URI

| 名字 | 签名 / 字段 | 语义与不变量 |
|---|---|---|
| `parse` | `parse(raw: str) -> ParsedUri` | 拆 `scheme` / `path` / `selector` / `query` / `trailing_slash`；裸路径与 `file://` 都是 `scheme="file"`；解析器不认识任何具体 scheme |
| `vfs_uri` | `vfs_uri(scheme: str, path: str) -> str` | `(scheme, path)` → `scheme://path`（去重前导斜杠；path 为空时是 `scheme://`） |
| `ParsedUri` | `raw: str`、`scheme: str`、`path: str`、`selector: str \| None = None`、`query: dict[str, list[str]] = {}`、`trailing_slash: bool = False` | 一次解析结果 |
| `ParsedUri.selector_lines` | `-> tuple[int, int] \| None` | 1-indexed 闭区间；`:raw` 单独用返回 `None` |
| `ParsedUri.is_raw` | `-> bool` | 是否关闭结构化摘要 |
| `ParsedUri.is_dir` | `-> bool` | 无行选择器且（path 为空或以 `/` 结尾） |
| `ParsedUri.is_vfs` | `-> bool` | `scheme != "file"` |
| `ParsedUri.vfs_path` | `-> str` | `/<scheme>/<path>`；非 VFS 抛 `VfsError` |

不变量：`<scheme>://a/b` ≡ `/<scheme>/a/b`（scheme 与挂载点同名，无翻译表）；
选择器从右最长匹配（`:-20:-10` 不会被截成 `:-10`）；`normalize_vfs_path` 里 `..`
越界**报错**而不是夹紧。

### 1.3 指纹、日志、异常

| 名字 | 签名 | 语义与不变量 |
|---|---|---|
| `canonicalize` | `canonicalize(value: Any) -> Any` | dataclass → `{"__type__", "fields"}`；`Path` → posix 字符串；set/frozenset → 排序列表；dict 键排序；bytes → sha256 摘要。**不可序列化对象抛 `TypeError`**（不 `repr()` 兜底） |
| `fingerprint` | `fingerprint(value: Any) -> str` | `sha256(canonical JSON)`，hex 摘要 |
| `short_fingerprint` | `short_fingerprint(value: Any, length: int = 12) -> str` | 仅用于日志 |
| `configure` | `configure(level: int \| str \| None = None) -> None` | 给 `liorium` logger 挂 stderr handler，幂等；级别优先级：显式参数 > `LIORIUM_LOG_LEVEL` > `INFO` |
| `get_logger` | `get_logger(name: str \| None = None) -> logging.Logger` | 取 `liorium.<name>` |
| `__version__` | `"0.1.0"` | 包版本 |

异常（全部继承 `LioriumError`）：

| 名字 | 语义 |
|---|---|
| `LioriumError` | 全部异常基类 |
| `SpecError` | `AgentSpec` 不合法（缺字段、prompt 文件不存在、realm id 重复 …） |
| `ConfigError` | 宿主传入的端口/参数不合法 |
| `RealmError` | realm 生命周期或装配错误 |
| `RealmNotFoundError` | spec 引用了没有工厂的 realm id |
| `RealmDependencyError` | `requires` 成环或依赖缺失 |
| `RealmStartError` | `start()` 失败（v1 语义 = 该 realm 标 `failed` 降级） |
| `ToolNameConflictError` | 同名工具被多处声明；字段 `name`、`owners` |
| `VfsError` | VFS 操作错误基类 |
| `MountConflictError` | 挂载前缀与已有节点冲突（报错，不静默覆盖） |
| `MountCycleError` | 挂载成环 / 挂载链过深 |
| `NodeNotFoundError` | 路径不存在 |
| `VfsReadOnlyError` | 对只读挂载/节点写入 |
| `ContentTypeError` | 内容类型不支持该操作 |
| `TurnInterrupted` | 回合被 `interrupt()` 或抢占取消；字段 `reason`（抢占时是 `"preempted"`） |
| `TurnDropped` | 回合在排队阶段被丢弃（`FLAG.DROPPABLE` + 拥塞）；字段 `reason`，从未开始执行 |
| `TurnError` | 回合失败；字段 `message`、`cause` |
| `CheckpointError` | checkpoint 打开/读/写失败 |

### 1.4 端口（子模块 `liorium.core.ports`）

这三个 `Protocol` 是库与工具之间唯一允许的跨包契约（不在包 `__all__`，从子模块导入）：

| 名字 | 方法 | 语义 |
|---|---|---|
| `ArtifactStore` | `put(text, *, tool_name="", metadata=None) -> str`、`put_multimodal(payload, *, tool_name="") -> str`、`get(artifact_id)`、`summary(artifact_id) -> str`、`exists(artifact_id) -> bool` | 工具输出完整内容存储；`OutputStore` 是内置实现 |
| `ProcessRunner` | `async run(command, *, cwd=None, timeout=None, env=None) -> tuple[int, str, str]` | `execute` 工具的进程端口（宿主可替换成沙箱/审计） |
| `Clock` | `now() -> float` | 时间戳；播放/时限判断都在宿主侧 |

---

## 2. `liorium.vfs`

`from liorium.vfs import VirtualFileSystem, VfsNode, VfsMount`。零第三方依赖，不持久化。

### 2.1 `VirtualFileSystem`

```python
VirtualFileSystem(*, root_name: str = "", owner_id: str = "")
```

读模型 / 操作界面，不是状态存储。所有权单位是**血缘根**：独立顶层 Agent 各自一棵树，
子代理共享父实例。所有读写走写者优先的 `AsyncRWLock`。

| 方法 | 签名 | 语义与不变量 |
|---|---|---|
| `resolve` | `resolve(path: str) -> ResolvedNode` | 解析（穿透挂载点），缺失抛 `NodeNotFoundError` |
| `exists` | `exists(path: str) -> bool` | 不抛异常的存在性判断 |
| `mount_vfs` | `mount_vfs(prefix: str, other: VirtualFileSystem, *, readonly: bool = True) -> VfsNode` | 挂另一个 VFS；`prefix="/"` 抛 `MountConflictError`，挂到自己或成环抛 `MountCycleError`，前缀冲突报错 |
| `mount_node` | `mount_node(prefix: str, node: VfsNode, *, readonly: bool = True, description: str = "") -> VfsNode` | 挂单个节点（可含子树） |
| `mount` | `mount(mount: VfsMount) -> VfsNode` | 按 `VfsMount` 描述挂载（realm 装配走这里） |
| `unmount` | `unmount(prefix: str) -> bool` | 摘挂载；不存在返回 `False` |
| `mounts` | `-> dict[str, VfsNode]`（property） | `prefix -> 挂载点节点` 快照 |
| `mount_parents` | `mount_parents() -> list[VirtualFileSystem]` | 挂到我身上的 VFS（环检测用） |
| `read` | `async read(path: str) -> VfsContent \| None` | 目录返回 `None`，缺失抛 `NodeNotFoundError` |
| `read_text` | `async read_text(path: str) -> str` | 非文本抛 `VfsError` |
| `stat` | `async stat(path: str) -> VfsNode \| None` | 缺失返回 `None` |
| `list` | `async list(path: str = "/") -> list[VfsNode]` | 列一层（穿透挂载，懒加载目录按需展开、不缓存），按名字排序 |
| `walk` | `async walk(root: str = "/") -> list[tuple[str, VfsNode]]` | 深度优先，含挂载子树；发现环抛 `MountCycleError` |
| `find` | `async find(pattern: str, root: str = "/", *, include_projected: bool = False) -> list[str]` | glob 找**文件**；pattern 不含 `/` 匹配 basename |
| `write` | `async write(path: str, content: VfsContent) -> None` | 写文件（缺失则创建，中间目录自动补齐，跨挂载委托给被挂载的 VFS）。**实现会返回 handler 的 ACK 字符串**（注解写的是 `None`，要 ACK 用 `write_async` 或看返回值） |
| `write_async` | `async write_async(path: str, content: VfsContent) -> str \| None` | 同上，但允许节点上的 async handler；返回 ACK |
| `edit` | `async edit(path: str, old: str, new: str, **kwargs) -> None` | 字符串替换；投影节点走 `edit_handler` |
| `mkdir` | `async mkdir(path: str) -> VfsNode` | 建目录（含中间层） |
| `remove` | `async remove(path: str) -> bool` | 删节点；只读节点抛 `VfsReadOnlyError`；**懒加载投影节点**（`child_provider` 能找到、但树里没有实体）抛 `VfsError`（要去改真相源） |
| `dump_tree` | `dump_tree(path: str = "/", max_depth: int = 3) -> str` | 缩进文本，日志/调试用 |

`ResolvedNode`（子模块导出）：`node: VfsNode`、`path: str`、`readonly: bool`、`owner: VirtualFileSystem`。

### 2.2 `VfsNode`

```python
VfsNode(
    name: str,
    is_directory: bool = False,
    content: VfsContent | None = None,
    children: dict[str, VfsNode] = {},
    mount: VirtualFileSystem | None = None,
    provider: NodeProvider | None = None,          # (node) -> VfsContent | None，可 async
    write_handler: WriteHandler | None = None,     # (node, content) -> Any，可 async
    edit_handler: EditHandler | None = None,       # (node, old, kwargs) -> Any，可 async
    create_handler: CreateHandler | None = None,   # (dir_node, relative_path, content) -> Any
    child_provider: ChildProvider | None = None,   # (dir_node, child_name) -> VfsNode | None，必须同步
    children_provider: ChildrenProvider | None = None,  # (dir_node) -> Iterable[VfsNode]，必须同步
    should_be_included_in_search: bool = True,
    writable: bool = True,
    readonly: bool = False,
    description: str = "",
    meta: dict[str, Any] = {},
)
```

不变式：

- `provider` 与 `content` 互斥，`provider` 优先；读内容时现算，**不缓存**（真相源可能在外部变）；
- `should_be_included_in_search=False` 的投影节点不参与 `search` / `find`；
- `child_provider` / `children_provider` 必须是同步函数——异步会被关掉协程并抛
  `VfsError`（`_resolve` 是同步路径）；
- `create_handler` 只接管「本树里还不存在的路径」，返回的字符串进 `↳ ack` 通道。

便捷属性：`is_symbolic`、`has_write_handler`、`has_edit_handler`、`has_create_handler`、
`has_child_provider`、`has_children_provider`、`is_lazy`、`is_mount_point`；
方法 `child(name)`、`add_child(node)`。

### 2.3 `VfsMount`

```python
VfsMount(prefix: str, vfs: VirtualFileSystem | None = None, node: VfsNode | None = None,
         readonly: bool = True, description: str = "")
```

realm 声明挂载的返回类型。`prefix` 自动补前导 `/` 并去尾 `/`；**必须且只能**给 `vfs`
或 `node` 之一（否则 `ValueError`）。`prefix` 与 scheme 同名：`/skills` ⇔ `skills://`。

（`AsyncRWLock` 在子模块 `liorium.vfs.locks`，是 VFS 与 realm 状态 store 共用的实现细节。）

---

## 3. `liorium.core-tool`

`from liorium.core_tool import CORE_TOOL_NAMES, ResolvedPath, ToolRuntime, build_core_tools, build_read_tool`。

### 3.1 `ToolRuntime`

```python
ToolRuntime(
    vfs: VirtualFileSystem,
    workspace_root: Path,
    artifacts: ArtifactStore,
    max_output_chars: int = 120,
    default_timeout: float = 120.0,
    env: Mapping[str, str] | None = None,
    process_runner: ProcessRunner | None = None,
)
```

六个核心工具共享的运行时。构造校验：`vfs` 必须是 `VirtualFileSystem`、`artifacts` 必须实现
`ArtifactStore`、`max_output_chars >= 0`、`default_timeout > 0`，否则 `ConfigError`；
`workspace_root` 会 `expanduser().resolve()`。

| 成员 | 签名 | 语义 |
|---|---|---|
| `resolve_path` | `resolve_path(uri: str) -> ResolvedPath` | 裸路径 / `file://` → 真实文件；`<scheme>://` → VFS |
| `exists` / `is_dir` | `async exists(target: ResolvedPath) -> bool` / `async is_dir(target) -> bool` | 真实文件系统 helper（`to_thread`） |
| `read_bytes` / `write_bytes` | `async read_bytes(target) -> bytes` / `async write_bytes(target, data: bytes) -> None` | 写会自动建父目录 |
| `read_text` | `async read_text(target) -> str` | UTF-8，换行统一成 `\n` |
| `read_text_with_newline` | `async read_text_with_newline(target) -> tuple[str, str]` | 同时返回行尾风格（`edit` 保留 CRLF 用） |
| `write_text` | `async write_text(target, text: str, *, newline: str = "\n") -> None` | 按指定行尾编码 |
| `vfs_stat` | `async vfs_stat(path: str) -> VfsNode \| None` | |
| `vfs_read_text` | `async vfs_read_text(path: str) -> str \| None` | 不存在/非文本返回 `None` |
| `vfs_write_text` | `async vfs_write_text(path: str, text: str) -> str \| None` | 返回 handler 的 ACK；只读抛 `VfsReadOnlyError`，写失败抛 `VfsError`（不静默丢内容） |
| `vfs_edit_text` | `async vfs_edit_text(path: str, old: str, new: str) -> str \| None` | 有 `edit_handler` 时给它 `(old, {"new": new})`，否则「替换第一处」 |
| `run_process` | `async run_process(command, *, cwd=None, timeout=None, env=None) -> tuple[int, str, str]` | `process_runner` 为空时用 `execute.DefaultProcessRunner`；超时抛 `TimeoutError` |

`ResolvedPath`（frozen dataclass）：`uri: ParsedUri`、`is_vfs: bool`、
`path: str`（VFS 绝对路径或真实文件绝对路径）、`display: str`（面向模型的写法）、
property `scheme`。

### 3.2 工具装配

| 名字 | 签名 | 语义 |
|---|---|---|
| `CORE_TOOL_NAMES` | `("read", "write", "edit", "search", "find", "execute")` | 六个核心工具名（顺序固定） |
| `build_core_tools` | `build_core_tools(runtime: ToolRuntime, *, names: Sequence[str] \| None = None) -> list[BaseTool]` | `names=None` 全给；未知名字抛 `ValueError`（不静默跳过） |
| `build_read_tool` | `build_read_tool(runtime: ToolRuntime) -> BaseTool` | 单独装配 `read`（工具名恒为 `read`） |

### 3.3 六个工具的入参 / 出参

工具都是 `langchain_core.tools.BaseTool`，返回值是**给模型看的文本**（长结果由 Agent 的
`OutputStore` 包装层换成 artifact 引用）。

| 工具 | 签名 | 返回 |
|---|---|---|
| `read` | `async read(uri: str, *, force_plain_text: bool = False, show_line_number: bool = False, with_metadata: bool = True) -> str` | 文件正文 / 目录列举 / 图片多模态 JSON / 二进制说明；支持选择器 `:50-100`、`:-3`、`:50+20`、`:raw` |
| `write` | `async write(path: str, content: str) -> str` | 真实文件：`已写入 {file_path} ({size} bytes)`；VFS：`已写入 {scheme}://{path} ({size} bytes)`，handler 有 ACK 时追加 `\n↳ {ack}`；只读挂载**抛错** |
| `edit` | `async edit(path: str, old_str: str, new_str: str) -> str` | 恰好 1 处才写回；0 处/多处返回可操作错误文本且文件不动；成功 `已编辑 {label} (1 处替换)` |
| `search` | `async search(pattern: str, path: str = "", root: str = "", max_results: int = 50) -> str` | `路径:行号: 命中行`；只搜文本（二进制/图片/投影节点跳过），隐藏文件跳过；上限 1000 |
| `find` | `async find(pattern: str, root: str = "", max_results: int = 50) -> str` | glob 匹配文件与目录；**目录结果带 `/` 后缀**；上限 1000 |
| `execute` | `async execute(command: str, cwd: str = "", timeout: float \| None = None, shell: str = "auto") -> str` | 首行恒为 `[Shell: ...]`（披露这次交给谁跑），其后 `stdout` → `[stderr]` → `[exit code: N]` 分段；默认超时 `ToolRuntime.default_timeout`，上限 3600s；超时杀整个进程树并返回 `命令超时 (Ns)` |

不变量：工具层不做权限判断、不读产品配置（允许 import `liorium.agent`，当前未使用）；
`execute` 在 Windows 单行走 `%COMSPEC% /c`、POSIX 走 `/bin/sh -c`；Windows 上**含换行的
命令自动改走 `powershell.exe -NoProfile -Command`**（cmd.exe 会在第一个换行处截断，
换行后的内容连同后续行被静默丢弃 —— 2026-11-08 实测），`shell="cmd"` 时宁可报错也不
半截执行。`read` / `search` 的真实文件走 `liorium.core_tool.sniff`：UTF-16 / GB18030
文本按文本读出（非 UTF-8 时首行加 `[编码: ...]`），只有判不出文本才回二进制说明，
且 `类型:` 与内容判定一致（扩展名猜测降级为注脚）。

### 3.4 `liorium.core-tool-realm`（同族 realm 壳）

`from liorium.core_tool_realm import CORE_REALM_ID, CoreToolRealm`。
**Agent 自己不添加任何工具**：六个核心工具要写 `RealmSpec(id="core")` 才会被装配。

| 名字 | 签名 / 取值 | 语义 |
|---|---|---|
| `CORE_REALM_ID` | `"core"` | realm id（`RealmSpec(id=...)` 与工厂键） |
| `CoreToolRealm` | `CoreToolRealm(spec, *, artifacts=None)` | `BaseRealm` 子类：`id="core"`、`instance_scope="agent"`、`state_scope="agent"`、无挂载、无周期上下文 |
| 工具子集 | `params={"tools": ...}` > `AgentSpec.toolsets["default"]` > 六个全给 | 未知名字由 `build_core_tools` 抛 `ValueError` ⇒ realm 标 `failed` |
| `params` | `tools`、`max_tool_output_chars`（默认 120） | 必须在 `RealmSpec.params` 里（参与指纹） |
| 运行时依赖 | `RealmContext.vfs` / `workspace_root` / `artifacts` | 由 Agent 填；独立使用时用 `CoreToolRealm(spec, artifacts=...)` |

`prompt_fragment()` 只描述本实例实际挂上的工具（子集里没有 `execute` 就不提它）。


---

## 4. `liorium.agent`

`from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent, ...`。
子模块导出（不在顶层 `__all__`）的名字会单独标注。

### 4.1 `AgentSpec` 家族

#### `Limits`

```python
Limits(
    max_turns: int = 500,
    recursion_limit: int = 1500,
    compact_threshold_ratio: float = 0.8,
    compact_keep_rounds: int = 3,
    compact_max_output_tokens: int = 4000,
    compact_cost_control: bool = False,
    compact_max_input_tokens: int = 0,
    max_tool_output_chars: int = 120,
    max_subagent_depth: int = 2,
    context_window: int = 0,
)
```

校验：`max_turns > 0`、`0 < compact_threshold_ratio <= 1`、`compact_keep_rounds >= 1`、
`context_window >= 0`，否则 `SpecError`。语义见 [GUIDE](GUIDE.md) 的「压缩与成本控制」一节。

#### `BootSpec`

```python
BootSpec(message: str | None = None)
```

首次引导语；**库不自动执行**（`start()` 绝不调模型）。

#### `RealmSpec`

```python
RealmSpec(id: str, params: Mapping[str, Any] = {}, requires: tuple[str, ...] = ())
```

`id` 非空；`params` 会被 `fingerprint()` 试算一次，不可序列化直接 `SpecError`
（避免不可指纹化的参数污染 `apply()`）。

#### `Profile`

```python
Profile(id: str, realms: tuple[RealmSpec, ...] = (), prompt_sources: tuple[str, ...] = (),
        toolsets: Mapping[str, tuple[str, ...]] = {})
```

| 成员 | 签名 | 语义 |
|---|---|---|
| `id` | `str` | 非空；realm id 在 profile 内不可重复 |
| `expand` | `expand(spec: AgentSpec) -> AgentSpec` | 合并进 spec（不改原 spec）：profile 的 realm 在前、同 id 以 spec 为准；`prompt_sources` 去重合并；`toolsets` 同名 key 以 spec 为准 |

#### `AgentSpec`

```python
AgentSpec(
    id: str,
    inbox_dir: Path,
    model: str,
    prompt_sources: tuple[str, ...] = (),
    realms: tuple[RealmSpec, ...] = (),
    toolsets: Mapping[str, tuple[str, ...]] = {},
    limits: Limits = Limits(),
    boot: BootSpec | None = None,
    workspace_root: Path | None = None,
)
```

| 成员 | 签名 / 类型 | 语义 |
|---|---|---|
| `effective_workspace_root` | `-> Path`（property） | `workspace_root` 为 `None` 时等于 `inbox_dir` |
| `realm_ids` | `-> tuple[str, ...]`（property） | 声明的 realm id 顺序 |
| `realm` | `realm(realm_id: str) -> RealmSpec` | 取一条声明；没有则 `SpecError` |
| `fingerprint` | `fingerprint() -> str` | 结构指纹，**排除** `id` / `inbox_dir` / `boot` |
| `part_fingerprints` | `part_fingerprints() -> dict[str, str]` | 键固定为 `model` / `prompt` / `realms` / `toolsets` / `limits`，`apply()` 按它做差量 |
| `with_realms` | `with_realms(realms: tuple[RealmSpec, ...]) -> AgentSpec` | 不可变变换 |
| `with_toolsets` | `with_toolsets(toolsets: Mapping[str, tuple[str, ...]]) -> AgentSpec` | |
| `with_limits` | `with_limits(limits: Limits) -> AgentSpec` | |
| `with_profile` | `with_profile(profile: Profile) -> AgentSpec` | 语法糖；非 `Profile` 抛 `SpecError` |

不变量：`realms` 里出现非 `RealmSpec` 或 id 重复 → `SpecError`；`toolsets` 的 key 不能为空；
身份（`id` / `inbox_dir`）不参与指纹，也不允许通过 `apply()` 变更。

### 4.2 `LioriumAgent`

```python
LioriumAgent(
    *,
    spec: AgentSpec,
    model_factory: Callable[[str], Any],
    compact_model_factory: Callable[[str], Any] | None = None,
    vfs: VirtualFileSystem | None = None,
    memory: Any = None,
    realm_factories: Mapping[str, RealmFactory] | None = None,
    artifacts: Any = None,
    root_agent: "LioriumAgent | None" = None,
    api_format: str | None = None,
    checkpoint_path: Path | None = None,
    thread_id: str | None = None,
    priority: int = 0,
    system_prompt_override: str | None = None,
)
```

| 成员 | 签名 | 语义与不变量 |
|---|---|---|
| `spec` | `AgentSpec` | 当前装配（`apply()` 后是最新的） |
| `model_factory` | `Callable[[str], Any]` | 可运行时替换（测试常用） |
| `compact_model_factory` | `Callable[[str], Any] \| None` | 摘要专用模型工厂；`None` = 复用主模型。用于让 compact 调用不启用思考 |
| `vfs` | `VirtualFileSystem` | 未传则新建，`owner_id=spec.id`；传给 `root_agent` 则共享父的树 |
| `realms` | `RealmManager` | 装配、生命周期、dirty 刷新 |
| `context_channel` | `ContextChannel` | `inject()` 的队列与审计记录 |
| `checkpoint` | `CheckpointStore` | `AsyncSqliteSaver` 生命周期；默认 `<inbox>/checkpoint.db` |
| `artifacts` | `ArtifactStore` | 默认 `OutputStore(root=<inbox>/artifacts)` |
| `thread_id` | `str` | 顶层默认 `"main"`（能读旧库），子代理默认自己的 `spec.id` |
| `priority` | `int` | 本 Agent 的**默认**调度优先级（子代理是父 -1）；`run(priority=...)` 可按次覆盖，越大越优先 |
| `graph` / `model` / `tools` / `epoch` / `started` | property | 运行时快照，`tools` 返回副本。**工具全部来自 realm**：没装配任何 realm ⇒ `tools == []`（零工具合法，`start()` 只记一条 INFO） |
| `start` | `async start() -> None` | 起 realm / 开 checkpoint / 建图 / 播种 system 消息；**不调模型**；已 close 再 start 抛 `TurnError` |
| `close` | `async close() -> None` | 幂等；内部 `interrupt(reason="agent_close")` + 停 realm + 关 checkpoint |
| `run` | `run(text: str, *, context: Sequence[ContextItem] = (), origin: str = "user", priority: int \| None = None, flags: FLAG = FLAG.NONE) -> AsyncIterator[Event]` | **只产出这一条消息**的事件；未 `start()` 抛 `TurnError`；迭代结束 = 回合结束；等待执行位期间不产出事件 |
| `inject` | `async inject(items: Sequence[ContextItem]) -> None` | 永不打断；单个 `ContextItem` 也接受 |
| `interrupt` | `async interrupt(*, reason: str = "") -> bool` | 取消当前回合；空闲返回 `False` |
| `apply` | `async apply(spec: AgentSpec, *, force: bool = False) -> ApplyResult` | 差量替换；`id`/`inbox_dir` 变了抛 `SpecError`；失败保留旧部件。`force=True` 即使指纹没变也重建运行时（调用方侧模型参数变了，例如思考强度）；`ApplyResult.forced` 标记 |
| `refresh_realms` | `async refresh_realms(*, force: bool = True) -> bool` | 返回是否真的刷新过；未 start 抛 `TurnError` |
| `set_realm_enabled` | `async set_realm_enabled(realm_id: str, enabled: bool) -> bool` | 运行期开/关一个**已装配**的 realm 并重建运行时：工具 / 挂载 / prompt 段随之撤下或恢复，`epoch` 相应 +1。返回调用后是否在运行（启动失败按 §C5 降级为 `failed`，原因见 `status()`）；未装配抛 `RealmNotFoundError` |
| `reset_dialog` | `async reset_dialog() -> None` | 清空对话 + 重写 system 段（不调模型）；清 `conversation` scope 状态 |
| `status` | `status() -> AgentStatus` | 可序列化快照 |

刻意不存在：`invoke()` / `submit()` / `events()` / `notify()`。

### 4.3 状态对象

#### `CacheEpoch`

```python
CacheEpoch(value: int = 0, tool_schema_fp: str = "", prompt_fp: str = "", reason: str = "")
```

| 成员 | 签名 | 语义 |
|---|---|---|
| `bumped` | `bumped(*, tool_schema_fp: str, prompt_fp: str, reason: str) -> CacheEpoch` | 两个指纹都没变返回自身；否则 `value + 1` |

只有「工具 schema 集合」或「system prompt」变化才 +1（`limits` 变化不算）。

#### `ApplyResult`

```python
ApplyResult(changed: tuple[str, ...] = (), skipped: tuple[str, ...] = (),
            failed: tuple[tuple[str, str], ...] = (), fingerprint: str = "",
            epoch: CacheEpoch = CacheEpoch(), forced: bool = False)
```

`ok`（property）= `not failed`。`failed` 元素是 `(部件名, "异常类型: 消息")`。
`forced=True` 表示本次是调用方显式 `apply(force=True)`（spec 无差异也会重建运行时）。

#### `AgentStatus` / `RealmStatus`

```python
AgentStatus(agent_id: str, running: bool = False, turn_id: str | None = None,
            queue_depth: int = 0, spec_fingerprint: str = "", epoch: CacheEpoch = CacheEpoch(),
            realms: tuple[RealmStatus, ...] = (), messages: int = 0,
            pending_context: int = 0, started: bool = False, closed: bool = False)

RealmStatus(id: str, state: RealmState = "pending", error: str | None = None,
            tools: tuple[str, ...] = (), mounts: tuple[str, ...] = (),
            shared: bool = False,
            context_due: int = 0, context_injections: int = 0)
```

`RealmState = Literal["pending", "starting", "running", "failed", "stopped"]`（子模块导出）。

`shared`：`True` = 这条是 `instance_scope="tree"` 的树级实例，从根 Agent **借用**来的
（状态 / 挂载 / 开关都归树根；子 Agent 的 `set_enabled` / `stop` 会抛 `RealmError`）。

`context_due`：第五拍还有几回合到期（`0` = 本回合就问；不参与周期上下文的 realm 恒为 `0`）。
`context_injections`：本进程内成功注入次数（`ctx.inject()` 推送不计入）。

### 4.4 调度：`FLAG` / `priority` / `TurnDropped`

```python
class FLAG(IntFlag):
    NONE = 0
    INTERRUPTING = 1   # 可抢占正在跑的低优先级回合
    EXCLUSIVE = 2      # 拒绝被抢占
    DROPPABLE = 4      # 拥塞时可被丢弃

DEFAULT_FLAGS = FLAG.NONE
QUEUE_MAX = 50
```

| 名字 | 签名 | 语义 |
|---|---|---|
| `FLAG` | 见上 | 一次 `run()` 的调度标志，正交可组合；位值冻结（`test_scheduler.py`） |
| `DEFAULT_FLAGS` | `FLAG.NONE` | 默认可被抢占、不抢别人、拥塞时不丢 |
| `run(priority=...)` | `int \| None` | 调用级优先级，**越大越优先**；`None` 用 `self.priority`（构造参数，默认 0；子代理父 -1）。同档 FIFO |
| `run(flags=...)` | `FLAG` | `INTERRUPTING` 只在优先级**严格更高**且对方非 `EXCLUSIVE` 时生效；被抢占方抛 `TurnInterrupted(reason="preempted")` |
| `TurnDropped` | `TurnDropped(reason: str = "congested")` | `DROPPABLE` 且 `queue_depth() >= QUEUE_MAX` 时抛；回合**从未开始**，不产生事件 |
| `QUEUE_MAX` | `50` | 只有 `DROPPABLE` 受它影响；其它回合无限排队 |

调度语义、抢占时序与不变量见 [`design/scheduler.md`](design/scheduler.md)。

### 4.5 `ContextChannel`

```python
ContextChannel()
```

| 方法 | 签名 | 语义 |
|---|---|---|
| `queue` | `queue(items: Sequence[ContextItem]) -> None` | 入队待物化 |
| `has_pending` / `pending_count` | `-> bool` / `-> int` | |
| `take_pending` | `take_pending() -> list[ContextItem]` | 取走全部（调用方负责写进消息） |
| `peek_pending` | `peek_pending() -> list[ContextItem]` | 只读 |
| `record_materialized` | `record_materialized(items, *, message_id, turn_id, at=None) -> list[ContextRecord]` | 写审计记录 |
| `history` | `history() -> list[dict[str, Any]]` | 审计记录（纯 JSON） |
| `clear` | `clear() -> None` | 清队列与记录 |

`ContextRecord`（子模块导出）：`source`、`text`、`at`、`message_id`、`turn_id`、`meta`，
`to_dict()` 输出纯 JSON。

### 4.6 realm 子系统

> 2026-11-07：本节的 `Realm` / `BaseRealm` / `RealmContext` / `JsonStateStore` / `RealmTaskScope` /
> `RealmRecord` 实体已搬到 `liorium.realm_base`（§10），`liorium.agent.realm` 只是 re-export，
> 签名不变。写作 API（`@realm_tool` / `save()` / `on_start`）见 §10。

#### `Realm` 协议

```python
class Realm(Protocol):
    id: str
    requires: list[str]
    state_scope: str
    instance_scope: str
    def tools(self) -> list[Any]: ...
    def prompt_fragment(self) -> str: ...
    def middleware(self) -> list[Any]: ...
    def mounts(self) -> dict[str, "VfsMount"]: ...
    async def start(self, ctx: RealmContext) -> None: ...
    async def stop(self, ctx: RealmContext) -> None: ...
```

可选钩子（`BaseRealm` 有默认实现，协议里不强制）：`dirty()`、`mark_dirty()`、
`clear_dirty()`、`async refresh()`、`skills()`。

#### `BaseRealm`

```python
class BaseRealm:
    id: str = ""
    requires: list[str] = []
    state_scope: str = "agent"
    instance_scope: str = "agent"   # "agent" | "tree"（2026-11-08，见 design/realm-scope.md）
    context_every: int = 0          # 第五拍：每 N 回合问一次（0 = 不参与）
    context_first: bool = False     # 第 1 回合先问一次，再进入 N 的节奏
    context_origins: tuple[str, ...] = ()   # 只对哪些 origin 计时；空 = 全部
```

`instance_scope="agent"`（默认）：每个 Agent 一份实例，状态在各自 `inbox/realm_state/` 下，
**不许**返回挂载点（`RealmManager` 启动期报 `RealmError`）；`"tree"`：整棵树一份实例，
子 Agent 只借它的工具（状态 / 挂载 / 提示词 / 中间件 / 周期上下文都归树根）。


全部方法都有空实现：`tools()` / `prompt_fragment()` / `middleware()` / `mounts()` /
`skills()` / `dirty()` / `mark_dirty()` / `start()` / `stop()` / `context_items()`。
`tools()` 默认收集 `@realm_tool` 方法；`start()/stop()` 默认挂 `self.ctx` 并调
`on_start()/on_stop()`（§10.2）。
**没有** `clear_dirty()` 的默认实现——`RealmManager` 会退化为 `mark_dirty(False)`。

#### `RealmContext`（子模块 `liorium.agent.realm`）

```python
RealmContext(realm_id: str, vfs: VirtualFileSystem, state: JsonStateStore,
             params: dict[str, Any] = {}, agent_id: str = "",
             inbox_dir: Path | None = None, workspace_root: Path | None = None,
             artifacts: Any = None, agent_spec: Any = None,
             conversation_id: str = "main",
             tasks: RealmTaskScope = RealmTaskScope(), context: ContextSink | None = None)
```

| 成员 | 签名 | 语义 |
|---|---|---|
| `log` | property | `liorium.realm.<realm_id>` logger |
| `spawn` | `spawn(coro, *, name=None) -> asyncio.Task` | **唯一**允许的后台任务入口；`stop` 时统一取消 |
| `state` | `JsonStateStore` | 见下 |
| `inbox_dir` | `Path \| None` | Agent 根目录（能力包定位自己的数据） |
| `workspace_root` | `Path \| None` | 裸路径基准（`AgentSpec.effective_workspace_root`），可能与 `inbox_dir` 不同 |
| `artifacts` | `Any` | `ArtifactStore` 端口（Agent 的 `OutputStore`）；独立使用可为 `None` |
| `agent_spec` | `Any` | 装配进 Agent 时的 `AgentSpec`（只读，`core` realm 用它读 `toolsets`） |
| `conversation_id` | `str` | `state_scope="conversation"` 的分桶键 |
| `context` | `ContextSink \| None` | 由 `RealmManager` 注入的 Context 通道（`queue(items)` 一个方法） |
| `inject` | `inject(text, *, source=None, meta=None) -> None` | 推一条 Context 到下一个安全点；同步、永不打断；无通道或空文本抛 `RealmError` |
| `inject_items` | `inject_items(items: Sequence[ContextItem]) -> None` | 推一批现成的（空序列是空操作） |

`ContextSink` 是**结构协议**（只要求 `queue`）：`liorium.realm_base` 不许 import
`liorium.agent`，所以不能直接标注 `ContextChannel`；`ContextChannel` 天然满足它。

#### `JsonStateStore`（子模块导出）

```python
JsonStateStore(path: Path, *, realm_id: str = "")
```

| 方法 | 签名 | 语义 |
|---|---|---|
| `load` | `load() -> None` | 读盘；损坏抛 `RuntimeError`（不静默重建） |
| `flush` | `flush() -> None` | 原子落盘（临时文件 + replace）；未脏则不写 |
| `get` | `async get(key: str, default: Any = None) -> Any` | 读锁 |
| `set` | `async set(key: str, value: Any) -> None` | 立刻 `json.dumps` 校验；不可序列化直接抛错 |
| `delete` | `async delete(key: str) -> bool` | 返回是否真的删了 |
| `all` | `async all() -> dict[str, Any]` | 副本 |
| `update_many` | `async update_many(values: dict[str, Any]) -> None` | 批量写 |
| `updated_at` | property `-> float` | 文件 mtime，不存在为 `0.0` |

#### `RealmTaskScope`（子模块导出）

```python
RealmTaskScope(*, log_name: str = "realm")
```

`spawn(coro, *, name=None) -> asyncio.Task`、`async cancel_all(timeout: float = 5.0)`、
`__len__`。**全仓库唯一允许调用 `asyncio.create_task` 的地方**（架构守卫白名单）。

#### `RealmRecord`（子模块导出）

`spec_id`、`instance`、`order`、`state`、`error`、`started_at`、`context`、`tools`、
`context_due`（`None` = 还没从 state 读回）、`context_injections`；property `id`（等于 `spec_id`）。

#### `RealmManager`（子模块 `liorium.agent.realm`）

```python
RealmManager(*, agent_id: str, vfs: VirtualFileSystem, state_dir: Path,
             factories: dict[str, RealmFactory] | None = None,
             on_turn_event: Callable[[Event], None] | None = None,
             conversation_id: str = "main",
             context_channel: ContextChannel | None = None)
```

| 方法 | 签名 | 语义 |
|---|---|---|
| `assemble` | `assemble(specs: Iterable[Any]) -> None` | 建实例 + `requires` 拓扑排序；没工厂 `RealmNotFoundError`，缺依赖/成环 `RealmDependencyError`，id 不一致 `RealmError` |
| `start_all` / `stop_all` | `async -> None` | 按拓扑序启动；停止按反序 |
| `start` | `async start(realm_id: str) -> None` | 建 `RealmContext`（含状态文件）、挂载、取工具、查冲突；失败标 `failed` 并撤挂载（降级但可用） |
| `stop` | `async stop(realm_id: str) -> None` | 取消后台任务、flush 状态、撤挂载 |
| `restart` | `async restart(realm_id: str, *, spec: Any = None) -> None` | 先停旧 →（可选换实例）→ 起新 |
| `set_enabled` | `async set_enabled(realm_id: str, enabled: bool) -> bool` | 只开关一个已装配的 realm（装配集合不变）；返回状态是否真的变了。停用后 `status()` 显示 `stopped`，工具 / 挂载 / prompt 段不再生效 |
| `context_cadence` | `context_cadence(realm_id: str) -> int` | 该 realm 的第五拍节奏（`RealmSpec.params` 覆盖类属性；`0` = 不注入） |
| `refresh` | `async refresh() -> bool` | 刷新所有自报 `dirty()` 的运行中 realm；返回是否重建过部件 |
| `mark_all_dirty` | `mark_all_dirty() -> list[str]` | 给支持 `mark_dirty()` 的运行中 realm 置脏，返回 id 列表 |
| `skill_names` | `skill_names() -> list[str]` | 汇总 `skills()` 条目（进 prompt 技能清单） |
| `context_for_turn` | `async context_for_turn(turn: TurnInfo) -> list[ContextItem]` | 第五拍：按每个运行中 realm 的节奏收集本回合要注入的 Context；**不改历史**（见下） |
| `reset_conversation_state` | `async reset_conversation_state() -> list[str]` | 停 → 删当前会话状态文件 → 重启；返回被重置的 realm id |
| `tools` | `tools() -> list[Any]` | 运行中 realm 的工具（声明序） |
| `prompt_fragments` | `prompt_fragments() -> list[tuple[str, str]]` | `(realm_id, fragment)` |
| `realm_middlewares` | `realm_middlewares() -> list[Any]` | 追加到基座**外层**的中间件 |
| `attach_specs` | `attach_specs(specs: Iterable[Any]) -> None` | 登记 spec（`ctx.params` 来源） |
| `status` | `status() -> list[Any]` | `list[RealmStatus]` |
| `record` | `record(realm_id: str) -> RealmRecord` | 未装配抛 `RealmNotFoundError` |
| `order` / `factories` / `conversation_id` | property / 属性 | 启动顺序、工厂表、会话 id |

`RealmFactory`（子模块导出）：`Callable[[Any], Realm]`。

#### 第五拍：`context_for_turn()` 的语义

`LioriumAgent._run_iter` 在拿到执行位后调用一次，把返回值排进 `ContextChannel`；
`ContextMergeMiddleware` 在下一个安全点物化成独立 `<context>` user 消息。

| 规则 | 行为 |
|---|---|
| `context_every = 0` | 一次都不问（默认；现有 realm 零成本） |
| `record.state != "running"` | 跳过（`/realm disable` 或启动失败）：不消费周期、不注入 |
| 计时 | 每个 origin 匹配的回合 `due -= 1`；归零那一回合调用 `context_items(turn)` |
| 参数 | `RealmSpec.params["context_every"]` / `["context_first"]` 覆盖类属性；非法值记 warning 按 `0` |
| 返回空 | **不消费周期**（`due` 停在 0，下一回合继续问） |
| 返回非空 | `record.context_injections += 1`，相位重置为 N 并落盘到 realm 自己的 state（保留键 `__context_due__`） |
| 归一化 | `None` / `str` / `ContextItem` / 序列；`str` 自动包成 `ContextItem(source=f"realm:{id}")`；空串丢弃；其它类型记 error 跳过 |
| 异常 | 记 `realm context_items 失败，本回合跳过: <id>: <exc>`，只跳过该 realm，不改 `record.state` |

`TurnInfo(index: int, id: str, origin: str = "user")`：`index` 是"本进程第几个 `run()`"，
与事件里的 `turn_id`（`f"{agent_id}#{index}"`）同一个计数器。

状态文件位置由 `state_scope` 决定：`agent` → `state_dir/<id>.json`；
`conversation` → `state_dir/<id>/conversation-<conversation_id>.json`；其它值抛 `RealmError`。

#### `SubagentRealm`

```python
SubagentRealm(*, parent: LioriumAgent, params: dict[str, Any] | None = None)
```

`id="subagents"`、`state_scope="agent"`、`instance_scope="tree"`。装配方式：spec 里写
`RealmSpec(id="subagents")`，工厂由 `LioriumAgent.start()` 自动注册。

| 成员 | 签名 | 语义 |
|---|---|---|
| `params` | `{"default_toolsets": tuple, "max_depth": int, "max_children": int}` | 默认工具集是 `("read","search","find","write","edit")`；`max_depth` 默认取 `limits.max_subagent_depth`；`max_children` 默认 `0` = 不限制 |
| `mounts` | `mounts() -> dict[str, VfsMount]` | `/subagents`（可写） |
| `tools` | `tools() -> list[Any]` | `newSubagent`、`invokeSubagent`、`wait_for_subagent`、`wait_for_subagents`、`stopSubagent`、`removeSubagent`、`listSubagents` |
| `children` | `dict[str, ChildRecord]` | 子代理记录；`max_children` 为上限（含重启后恢复的）；进程重启后由名册重建 |
| `create_child` | `async create_child(name: str, prompt: str, *, toolsets: str = "") -> str` | 名字必须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`；返回 JSON（`resumed` = 目录里已有旧 checkpoint） |
| `invoke_child` | `async invoke_child(name: str, message: str) -> str` | 异步启动（走 `ctx.spawn`），先落盘再起跑 |
| `wait_child` | `async wait_child(name: str, *, timeout: float = 600.0) -> str` | 等结果；超时返回状态与已有输出 |
| `wait_children` | `async wait_children(names, *, mode: str = "all", timeout: float = 600.0) -> str` | 等一组（`all` / `any`） |
| `stop_child` | `async stop_child(name: str) -> str` | `interrupt()` + 取消任务 + 等收尾 |
| `remove_child` | `async remove_child(name: str) -> str` | 删记录 + 卸投影 + 关 Agent + **删目录**（含对话与产出，不可恢复） |
| `shutdown` | `async shutdown() -> None` | 取消/关闭全部子代理并回写名册（`close()` 会调） |
| `subscribe` / `unsubscribe` | `subscribe() -> asyncio.Queue` / `unsubscribe(queue) -> None` | 宿主订阅子代理事件 |
| `events` | `async events() -> AsyncIterator[dict[str, Any]]` | 子代理事件流（realm 自己暴露，不是 Agent 的第二条事件流） |

**状态持久化**（2026-11-08，见 [`design/subagent-state.md`](design/subagent-state.md)）：
名册写 `inbox/realm_state/subagents.json`（`JsonStateStore`，含 `version`），正文写
`inbox/subagents/<name>/{TASK,output,finalResult}.md` —— 结构化元数据与正文分家，
名册始终很小。`ChildRecord.status` ∈ `idle` / `running` / `done` / `error` / `stopped` /
`interrupted`；上次进程退出时仍在 `running` 的，恢复时标 `interrupted` 并回写名册。
子代理的对话本来就在 `<child>/checkpoint.db`（`thread_id = "subagent-<name>"`），
所以恢复后再次 `invoke_child` 是**接着聊**，不是重开。

---

## 5. `liorium.memory`

`from liorium.memory import MemoryRealm, SqliteMemoryStore, EntityGraph, ...`。只依赖标准库 +
`liorium.core` + `liorium.vfs`（允许 import `liorium.agent`，当前未使用）。语义级检索相关的子模块另有依赖：
`liorium.memory.vectors` 需要 `numpy` + `nano-vectordb`，`liorium.memory.embeddings` 需要 `httpx`
（都在 `liorium-memory[vector]` extra 里；不装就只有关键词检索，核心功能不受影响）。

> 实现说明、42 个旧接口的映射、**全部偏差**见 [MEMORY](MEMORY.md)。

### 5.1 常量

| 名字 | 值 | 语义 |
|---|---|---|
| `MOUNT_PREFIX` | `"/memories"` | 挂载点 |
| `SCHEME` | `"memories"` | scheme |
| `STORE_DIR_NAME` | `"memory"` | 默认目录 `<inbox>/memory` |
| `MIN_SCORE_PATCH` / `MAX_SCORE_PATCH` | `-0.15` / `0.15` | `set_score_patch` 的闭区间 |

### 5.2 `SqliteMemoryStore`

```python
SqliteMemoryStore(
    db_path: Path | str, *,
    content_dir: Path | str | None = None,      # 默认 <db 父目录>/content
    index_dir: Path | str | None = None,        # 默认 <db 父目录>/index
    keyword_index: bool = True,                 # 关掉就不建 fts.sqlite
    fts_cache_mb: int = 20,
)
```

与旧 `memory.sqlite` **同 schema v1**（`nodes` / `edges` / `tags` / `chunks` / `tasks`），
正文在 `<store_dir>/content/<path>`。单连接 + `RLock` 串行化；WAL + `busy_timeout=5000`；
`schema_version != 1` 直接 `RuntimeError`（不静默迁移）。

读写：

| 方法 | 签名 | 语义 |
|---|---|---|
| `close` | `close() -> None` | 关主库 + side-car；也支持 `with` |
| `revision` | `revision() -> tuple[int, int, float]` | `(节点数, 分块数, 最大 updated_at)`；realm 用它判脏 |
| `node` | `node(path: str) -> MemoryNode \| None` | 单节点元数据 |
| `children` | `children(path: str = "/", *, limit: int \| None = None, offset: int = 0) -> list[MemoryNode]` | 一层子节点（目录在前；可省略 limit 取全部） |
| `count_children` / `count_files` / `chunk_count` | `(path: str = "/") -> int` / `() -> int` / `() -> int` | 只走 `COUNT(*)` |
| `all_nodes` | `all_nodes() -> list[MemoryNode]` | 全量（浅层在前；大库慎用） |
| `tags_of` | `tags_of(path: str) -> tuple[str, ...]` | 标签（插入序） |
| `read` | `read(path: str) -> MemoryEntry` | 不存在 `FileNotFoundError`，目录 `IsADirectoryError`；图片/二进制进 `data` |
| `write` | `write(path: str, text: str, *, description: str \| None = None, declared_by: str = "agent", tags: Iterable[str] \| None = None, index: bool = True) -> MemoryNode` | 覆盖写；自动建祖先；`index=True` 时重建分块并同步关键词索引 |
| `write_attachment` | `write_attachment(path: str, data: bytes, *, content_type: str = "image/png", description: str = "", declared_by: str = "agent") -> MemoryNode` | 二进制附件；`[image:<type>] <description>` 生成一个可检索 chunk |
| `mkdir` | `mkdir(path: str, *, description: str = "") -> MemoryNode` | 建目录（幂等） |
| `delete` | `delete(path: str) -> bool` | 目录递归删；`/` 不允许删 |
| `set_tags` | `set_tags(path: str, tags: Iterable[str], *, managed_by: str \| None = None) -> MemoryNode` | 整组替换；**不刷** `updated_at` |
| `set_score_patch` | `set_score_patch(path: str, score_patch: float) -> MemoryNode` | ±0.15，越界 `ValueError`；不刷 `updated_at` |
| `rename` / `move` / `copy` | `rename(path, new_name) -> str` / `move(path, dest_dir) -> str` / `copy(src, dest) -> str` | 子树路径重写 + 正文搬移 + chunk 重算 |
| `search` | `search(query: str, *, scope: str = "/", top_k: int = 5, tags: Iterable[str] = ()) -> list[SearchHit]` | **纯词法**打分（tf-idf + 标签/路径/新鲜度/`score_patch`）；`tags` 有交集即命中 |
| `advanced_search` | `advanced_search(query: str \| None = None, *, scope: str = "/", tags: Iterable[str] = (), tag_logic: str = "AND", date_from: str \| None = None, date_to: str \| None = None, declared_by: str \| None = None, content_type: str \| None = None, top_k: int = 20, sort_by: str = "relevance", sort_order: str = "desc") -> list[dict[str, Any]]` | 多条件检索；**只返回文件** |

变更流 / 任务（宿主 API，不是模型工具）：

| 方法 | 签名 | 语义 |
|---|---|---|
| `changed_nodes` | `changed_nodes(since_ts: float, *, scope: str = "/", tags: Iterable[str] = (), include_entities: bool = False, limit: int \| None = None) -> list[dict[str, Any]]` | 之后被写过的节点 |
| `add_task` / `update_task` / `get_tasks` | `add_task(task_type, payload=None) -> dict` / `update_task(task_id, status, error="") -> None` / `get_tasks(*, limit=200) -> list[dict]` | `tasks` 表 |

派生索引：

| 成员 | 签名 | 语义 |
|---|---|---|
| `fts` | property `-> FtsIndex \| None` | 关键词索引（惰性创建；`keyword_index=False` 时为 `None`） |
| `vector_index` | `bool`（属性） | 打开后写库会入队向量任务（由启用向量检索的一方置 True） |
| `chunk_rows` / `iter_chunk_ids` / `chunk_rows_by_ids` / `existing_chunk_ids` | 流式/批量读分块 | 重建索引、向量对账用 |
| `reindex_keyword` | `reindex_keyword() -> int` | 从 `chunks` 全量重建 FTS，返回条数 |
| `keyword_index_status` | `keyword_index_status() -> dict[str, Any]` | `{enabled, stale, indexed, chunks, pending_vectors}` |
| `meta_many` / `preview_many` | `meta_many(paths) -> dict[str, dict]` / `preview_many(paths) -> dict[str, str]` | 批量取检索后要用的元数据 / 首块片段 |

### 5.3 数据模型

| 名字 | 字段 | 语义 |
|---|---|---|
| `MemoryNode` | `path`、`kind`（`"dir"`/`"file"`）、`name`、`description`、`updated_at`、`content_type`、`chunk_count`、`score_patch`、`tags`；property `uri` | 路径节点只读投影 |
| `MemoryEntry` | `path`、`name`、`description`、`updated_at`、`tags`、`content_type`、`text`、`data`、`line_count`；property `is_image` | 一次读取结果 |
| `SearchHit` | `path`、`score`、`snippet`、`description`、`updated_at`、`tags`、`chunk_index`；property `uri`；`to_dict()` | 纯词法检索命中 |
| `SearchResult` | `path`、`score`、`snippet`、`description`、`updated_at`、`tags`、`sources`、`matched_chunks`；property `uri`；`to_dict(content=None)` | 融合后的检索结果（`sources` ∈ `keyword`/`vector`/`graph`） |
| `Entity` | `id`（`ent_*`）、`name`、`entity_type`、`description`、`properties`、`kb_refs`、`created_at`、`updated_at`；`to_dict()` | 实体（`nodes.type="entity"`，`path` 为 `NULL`） |
| `Relation` | `source`、`target`、`type`、`key`；`to_dict()` | 一条边 |

### 5.4 辅助函数

| 名字 | 签名 | 语义 |
|---|---|---|
| `normalize_memory_path` | `normalize_memory_path(path: str) -> str` | `a/b` → `/a/b`；含 `..` 抛 `ValueError` |
| `chunk_text` | `chunk_text(text: str, *, max_chars: int = 3000, overlap: int = 300) -> list[str]` | 带重叠切块 |
| `tokenize` | `tokenize(text: str) -> list[str]` | ASCII 词 + 中文二元组，去重保序（查询侧） |

### 5.5 `MemoryRealm`

```python
MemoryRealm(
    spec: Any = None, *,
    store: SqliteMemoryStore | None = None,
    embedder: Embeddings | None = None,            # OpenAI 兼容 embedding 服务
    vectors: VectorBackend | None = None,          # 分块向量后端
    entity_vectors: VectorBackend | None = None,   # 实体名向量（去重）
    extractor: EntityExtractor | None = None,      # LLM 实体抽取
    search_config: SearchConfig | None = None,
)
```

`id="memory"`、`requires=()`、`state_scope="agent"`。`RealmSpec.params` 键见 [MEMORY §8](MEMORY.md)
（`db_path` / `content_dir` / `list_limit` / `keyword_index` / `vectors` / `extraction`）。
**服务对象不进 params**（params 要参与指纹），用 realm 工厂闭包注入。

| 成员 | 签名 | 语义 |
|---|---|---|
| `start` / `stop` | `async start(ctx)` / `async stop(ctx)` | 开库、建空投影、体检并（必要时）重建关键词索引、起索引器；`stop` 先收尾索引器再关库 |
| `mounts` | `mounts() -> dict[str, VfsMount]` | `{"/memories": ...}`（可写） |
| `tools` | `tools() -> list[Any]` | 7 个工具，见 5.6 |
| `prompt_fragment` | `prompt_fragment() -> str` | 用法说明（含是否有语义检索） |
| `middleware` / `skills` | `() -> list` | 空 |
| `dirty` / `mark_dirty` / `clear_dirty` / `refresh` | 见 realm 协议 | 按 `store.revision()` 对账；投影懒加载，不重建树 |
| `store` | property `-> SqliteMemoryStore \| None` | 未 `start()` 且未注入时为 `None` |
| `graph` | property `-> EntityGraph` | 实体/关系（惰性构建，可用注入的 store） |
| `search_engine` | property `-> MemorySearch` | 混合检索器（惰性构建） |
| `search` | `search(query, *, scope="/", top_k=5, tags=None) -> list[dict]` | 同步、纯词法（兼容旧调用） |
| `asearch` | `async asearch(query, *, scope="/", top_k=5, tags=None, return_mode="compact", mode="auto") -> list[dict]` | 混合检索（工具走这个） |
| `list_dir` | `list_dir(path="/", *, limit=200, offset=0) -> tuple[list[MemoryNode], int]` | 分页列一层 |
| `delete` / `set_tags` / `set_score` | 见 `memory_delete` / `memory_tag` / `memory_score` | 写操作，返回 dict |
| `drain_index` | `async drain_index(*, max_batches=200) -> int` | 把待向量化作业跑完（CLI/测试） |
| `drain_extraction` | `async drain_extraction(timeout=10.0) -> None` | 等后台抽取收尾（测试/CLI） |
| `add_chat_record` | `add_chat_record(user_text, assistant_text, *, attachments=None) -> dict` | **API**：`/records/…` + 记录实体 + `from`/`next` 链 |
| `write_diary` | `write_diary(content: str) -> dict` | **API**：`/diary/…` + diary 记录实体 |
| `declare_file_update` | `declare_file_update(file_path: str, kb_path: str \| None = None) -> dict` | **API**：把宿主本地 UTF-8 文件导入 `/imports/<name>` |
| `write_attachment` | `write_attachment(path, data, *, content_type="image/png", description="") -> dict` | 写二进制并索引描述 |

状态文件 `realm_state/memory.json` 的键：`db`、`content_dir`、`entries`（文件数）、`top_level`、
`entities`（实体数）、`vectors`（向量条数，未启用为 0）、`index`（`keyword_index_status()`）。

### 5.6 模型工具（7 个，`build_memory_tools(realm)`，顺序稳定）

| 工具 | 签名 | 返回 |
|---|---|---|
| `memory_search` | `async (query, scope="/", top_k=5, tags="", return_mode="compact")` | JSON。`return_mode`：`compact` / `snippets` / `paths` / `full` |
| `memory_list` | `async (path="/", limit=50, offset=0)` | JSON（`total`/`offset`/`returned`/`items`）；`limit` 夹到 1–500 |
| `memory_delete` | `async (path, recursive=False)` | JSON（`status=ok`）；目录必须 `recursive=True` |
| `memory_tag` | `async (path, tags)` | JSON；`tags` 逗号分隔，空串清空 |
| `memory_score` | `async (path, score_patch)` | JSON；±0.15，越界被拒 |
| `memory_graph` | `async (action, target="", depth=1)` | `action`：`neighbors`（实体 id）/ `detail`（实体 id）/ `search`（名字片段）/ `entities_of`（记忆路径） |
| `memory_attach` | `async (path, content_base64, content_type="image/png", description="")` | 存二进制附件；描述可被检索 |

重命名 / 移动 / 复制 / 多条件检索 / 变更流 / 任务 / 聊天记录 / 日记 / 声明文件更新都是 **API，不是模型工具**
（与既有实现一致：这些要么是宿主职责，要么模型没有足够信息）。

工具出错返回 `{"status": "error", "error": "..."}` 文本，不炸回合。

### 5.7 子系统（`liorium.memory.*` 子模块）

| 模块 | 关键符号 | 说明 |
|---|---|---|
| `fts` | `FtsIndex`、`tokenize_terms`、`query_terms`、`match_expression`、`FtsHit` | side-car FTS5（预分词）+ 向量任务队列 |
| `vectors` | `VectorBackend`、`NanoVectorBackend`、`VectorRecord`、`VectorHit`、`EMBED_DIM=1536`、`METADATA_KEYS` | 与旧 `chunks.vdb` 同格式 |
| `embeddings` | `Embeddings`、`OpenAICompatibleEmbeddings`、`EmbeddingError` | OpenAI 兼容 `/embeddings`（httpx，批量+并发+维度校验） |
| `search` | `MemorySearch`、`SearchConfig`、`SearchResult`、`MODES`、`RETURN_MODES` | RRF 融合（关键词 + 向量 + 图谱） |
| `indexer` | `MemoryIndexer` | 后台消费向量队列（重试/丢弃/对账） |
| `graph` | `EntityGraph`、`Entity`、`Relation`、`ENTITY_TYPES`、`RELATION_TYPES`、`FROM_EDGE` | 实体/关系（递归 CTE，无投影） |
| `extract` | `EntityExtractor`、`LlmExtractor`、`MemoryExtractor`、`ExtractionResult`、`parse_extraction_json`、`EXTRACTION_PROMPT` | 抽取流水线（单飞 + 上下文隔离） |
| `migrate` | `migrate_legacy_json`、`needs_migration`、`MigrationReport`、`MigrationError` | 旧 JSON 库 → SQLite |

---

## 6. `liorium.skills`

`from liorium.skills import SkillsRealm, discover_skills, ...`。

### 6.1 常量与数据

| 名字 | 值 / 字段 | 语义 |
|---|---|---|
| `MOUNT_PREFIX` | `"/skills"` | 挂载点（只读） |
| `SCHEME` | `"skills"` | scheme |
| `SKILLS_DIR_NAME` | `"skill.d"` | 默认目录 `<inbox>/skill.d` |
| `Skill` | `slug`、`path`、`name`、`description`、`version`；property `skill_md`、`uri`；方法 `to_dict()` | 一个已安装技能（只描述磁盘事实） |

### 6.2 函数

| 名字 | 签名 | 语义 |
|---|---|---|
| `discover_skills` | `discover_skills(root: Path \| str) -> list[Skill]` | 扫描 `<root>/<slug>/SKILL.md`；隐藏目录（`.` / `_` 开头）与没有 `SKILL.md` 的目录跳过 |
| `catalog_signature` | `catalog_signature(root: Path \| str) -> tuple[tuple[str, int, int], ...]` | `(slug, SKILL.md mtime_ns, 目录 mtime_ns)` 有序元组；`dirty()` 用它 |
| `read_skill_meta` | `read_skill_meta(skill_dir: Path) -> dict[str, Any]` | 优先 `_meta.json`，否则 `SKILL.md` frontmatter；一定带 `slug`，缺 `version` 补 `"0.0.0"` |
| `split_frontmatter` | `split_frontmatter(text: str) -> tuple[dict[str, Any], str]` | `(frontmatter, 正文)`；没有 frontmatter 返回 `({}, text)` |

### 6.3 `SkillsRealm`

```python
SkillsRealm(spec: Any = None, *, skills_dir: str | Path | None = None)
```

`id="skills"`、`state_scope="agent"`。`params` 键：`skills_dir`（默认 `<inbox>/skill.d`）、
`max_prompt_skills`（默认 20，不能为负）。

| 成员 | 签名 | 语义 |
|---|---|---|
| `start` / `stop` | `async -> None` | 发现技能、建挂载树、写状态（`root` / `skills` / `count`） |
| `mounts` | `mounts() -> dict[str, VfsMount]` | `/skills` 只读挂载 |
| `tools` | `tools() -> list[Any]` | `list_skills`（返回 `{"skills": [...]}` JSON） |
| `prompt_fragment` | `prompt_fragment() -> str` | 没有技能时返回空串 |
| `skills` | `skills() -> list[str]` | 预载进 system prompt 的清单条目（超上限给一句提示） |
| `dirty` / `mark_dirty` / `clear_dirty` | `-> bool` / `mark_dirty(flag: bool = True)` / `-> None` | mtime 指纹变了就脏；`mark_dirty(True)` 强制脏 |
| `root` | property `-> Path \| None` | 技能根目录 |
| `list_skills` | `list_skills() -> list[dict[str, str]]` | 结构化清单 |
| `list_skills_json` | `list_skills_json() -> str` | JSON 形态（工具用） |
| `middleware` | `-> list[Any]` | 空 |

---

## 7. `liorium.mcp`

`from liorium.mcp import McpRealm, McpServerConfig, ...`。

### 7.1 配置与协议

| 名字 | 签名 / 字段 | 语义 |
|---|---|---|
| `McpServerConfig` | `id: str`、`transport: str = "stdio"`、`command: str = ""`、`args: tuple[str, ...] = ()`、`url: str = ""`、`headers: Mapping[str, str] = {}`、`env: Mapping[str, str] = {}`、`enabled: bool = True`、`description: str = ""` | 可序列化配置（进 `RealmSpec.params`）。`transport` 接受 `stdio` / `sse` / `streamable-http`（别名 `http`、`streamable_http`）；`stdio` 必须有 `command`，其它必须有 `url`，否则 `ValueError` |
| `McpServerConfig.from_dict` | `from_dict(server_id: str, raw: Mapping[str, Any]) -> McpServerConfig` | 从 dict 构造 |
| `McpServerConfig.to_dict` | `to_dict() -> dict[str, Any]` | |
| `McpToolSpec` | `name: str`、`description: str = ""`、`input_schema: Mapping[str, Any] = {}`；方法 `to_dict()` | 运行时发现的工具 |
| `McpClient` | `server_id: str`；`async connect()`、`async list_tools() -> list[McpToolSpec]`、`async call_tool(tool_name, arguments) -> str`、`async close()` | 传输无关端口（宿主可注入） |
| `SdkMcpClient` | `SdkMcpClient(config: McpServerConfig)` | 官方 SDK 实现；`mcp` 延迟导入，没装时 `connect()` 抛 `McpError` |
| `McpError` | `RuntimeError` 子类 | 连接/调用失败 |
| `TRANSPORTS` | `("stdio", "sse", "streamable-http")`（子模块导出） | 支持的传输 |

### 7.2 `McpRealm`

```python
McpRealm(spec: Any = None, *, client_factory: Callable[[McpServerConfig], McpClient] | None = None)
```

`id="mcp"`、`state_scope="agent"`。`params` 键：

| 键 | 默认 | 语义 |
|---|---|---|
| `servers` | `{}` | `{server_id: {...}}` 或 `[{ "id": ..., ...}]`；配置见 `McpServerConfig` |
| `strict` | `False` | 所有 server 都连不上时：`True` ⇒ realm 启动失败；`False` ⇒ 留空工具、记 error |
| `prefix_tools` | `True` | 工具名是否加 `<server>_` 前缀 |

| 成员 | 签名 | 语义 |
|---|---|---|
| `start` / `stop` | `async -> None` | 连接、发现工具、写状态；`stop` 关所有 client |
| `refresh` | `async refresh() -> None` | 重连（dirty 后由 `RealmManager` 在回合边界调） |
| `tools` | `tools() -> list[Any]` | 按当前连接的工具规格构造（名字 `<server>_<tool>`） |
| `prompt_fragment` | `prompt_fragment() -> str` | 列出已连接 server 与工具名 |
| `skills` / `middleware` | `-> list[Any]` | 空 |
| `dirty` / `mark_dirty` / `clear_dirty` | `-> bool` / `mark_dirty(flag: bool = True)` / `-> None` | 配置签名变了或宿主置脏 |
| `set_servers` | `set_servers(servers: Iterable[McpServerConfig \| Mapping[str, Any]] \| Mapping[str, Any]) -> None` | 替换配置并标脏（宿主唯一入口） |
| `enabled_servers` | property `-> list[McpServerConfig]` | `enabled=True` 的配置 |
| `clients` | property `-> dict[str, McpClient]` | 已连接 client 副本 |
| `tool_specs` | property `-> dict[str, list[McpToolSpec]]` | 每个 server 的工具规格 |
| `tool_names` | `tool_names() -> list[str]` | 暴露给模型的工具名（排序） |
| `exposed_name` | `exposed_name(server_id: str, tool_name: str) -> str` | 前缀规则 |
| `status` | `status() -> list[dict[str, Any]]` | 每个 server 的 `id` / `enabled` / `transport` / `connected` / `tools` / `error` |
| `call_tool` | `async call_tool(server_id: str, tool_name: str, arguments: Mapping[str, Any]) -> str` | 未连接抛 `McpError` |
| `MOUNT_PREFIX` | `""` | 模块常量：MCP 不提供挂载 |

### 7.3 Schema 工具

| 名字 | 签名 | 语义 |
|---|---|---|
| `build_args_schema` | `build_args_schema(tool_name: str, schema: dict[str, Any] \| None) -> Any` | JSON Schema → pydantic 模型；无参数返回 `None`；schema 太怪时也返回 `None`（不阻断工具发现） |
| `build_tool_description` | `build_tool_description(description: str, args_schema: Any) -> str` | 原文 + `Args:` 字段说明 |

> **已知缺口**：当前 `McpRealm` 没有实现 `Realm.mounts()`，而 `RealmManager.start()` 会调用它，
> 因此**经 `AgentSpec` 装配的 MCP realm 会被标 `failed`**（降级、不影响其它 realm）。
> `packages/liorium-mcp/tests/test_mcp_realm.py` 直接用假 `ctx` 测 realm，覆盖不到这条装配路径。
> 补一个 `def mounts(self): return {}` 即可接通；本文不把「MCP 工具已可用」写成既成事实。

---

## 8. `liorium.cli`

`from liorium.cli import main`（或直接 `liorium-cli` / `python -m liorium.cli`）。
这是**宿主**包：装配 `LioriumAgent`、渲染事件、提供 REPL 与子代理观测；宿主策略
（并发、展示、命令）不回流到 `liorium-agent`。

### 8.1 入口

| 名字 | 签名 | 语义 |
|---|---|---|
| `main` | `main(argv: Sequence[str] \| None = None) -> int` | 控制台入口；返回进程退出码（130 = Ctrl+C） |
| `run_cli` | `async run_cli(argv=None, *, registry=None, model_factory=None, input_fn=input, install_signal=True) -> int` | 可嵌入的异步入口（测试/别的宿主复用） |
| `build_parser` | `build_parser() -> argparse.ArgumentParser` | 参数表；`--env-file` / `--no-env` 先于 parser 求值 |
| `CliOptions` | `workspace`、`inbox`、`system_prompt`、`temperature`、`max_turns`、`thinking`、`show_reasoning`、`json_mode`、`color`、`show_banner`、`enable_core_tools`、`tools`、`enable_memory`、`enable_skills`、`enable_subagents`、`memory_dir`、`skills_dir`、`embed_*`、`enable_vectors`、`enable_extraction`、`models_dev_cache`、`env_report` | 一次会话的配置 |
| `CliSession` | `CliSession(registry, resolved, options, *, input_fn=input, ui=None, model_factory=None)` | `async start/close`、`async run_turn(text) -> bool`、`async repl() -> int`、`async handle_command(line) -> bool` |

### 8.2 子代理观测

| 名字 | 签名 | 语义 |
|---|---|---|
| `SubagentMonitor` | `SubagentMonitor(*, ui, history=500, live: Callable[[], bool] \| None = None)` | 后台消费 `SubagentRealm.subscribe()` 的队列；按名字保存最近 `history` 条事件 |
| `attach` / `close` | `async attach(realm)` / `async close()` | 订阅在 `create_task` **之前**完成（否则创建/启动事件会丢） |
| `snapshot` | `snapshot(name, limit=20) -> list[SubagentEvent]` | 最近事件（连续 delta 在渲染层合并） |
| `render_list` / `render_snapshot` | `render_list(ui, realm, monitor)` / `render_snapshot(ui, realm, name, monitor, *, limit=20) -> bool` | `/subagent`、`/subagent <name>` 的输出 |
| `resolve_name` | `resolve_name(realm, name) -> str` | 接受子代理名、`subagent-<name>` 或完整 `agent_id` |

`SubagentEvent` 字段：`at: float`、`type: str`、`agent_id: str`、`payload: dict`。
缓冲区每子代理最多 500 条、单条 `content`/`output` 截断到 4000 字符；完整输出以
`subagents://<name>/output.md` / `finalResult.md` 为准。父回合进行中只实时渲染关键事件
（创建 / 启动 / 工具 / 压缩 / 完成 / 失败）；`--json` 模式不混入人类可读行。

### 8.3 Provider / Env

| 模块 | 公开名字 |
|---|---|
| `liorium.cli.providers` | `Provider`（= `ModelProvider`）、`ProviderRegistry`、`ResolvedModel`、`ProviderConfigError`、`make_chat_model`、`load_registry`、`registry_from_args_or_prompt`、`prompt_for_registry`、`save_registry`、`normalize_format`、`infer_format_from_url`、`default_config_candidates`、`DEFAULT_FORMAT`、`SUPPORTED_FORMATS`、`THINKING_DIALECTS` |
| `liorium.cli.env` | `EnvReport`、`EnvFileError`、`load_env`、`parse_env_file`、`write_env_file`、`default_env_candidates`、`env_bool/env_int/env_float/env_str/env_path`、`as_dict`、`ENV_PREFIX` |
| `liorium.cli.ui` | `Ui`、`configure_streams`、`truncate`、`indent`、`short_json`、`clock`、`duration`、`char_count`、`local_time`、`memory_meta` |

`liorium.cli.providers` 是**宿主适配层**：配置搜索、环境变量、向导、保存路径。
真正的 schema / 格式 / 思考 / models.dev / 模型构建在 `liorium.provider`（见 §9）。
配置优先级：**命令行 > 进程环境变量 > `./.env` / `./.env.local` > 默认**；
`LIORIUM_*` 前缀与 `--no-*` 开关一一对应（含 `LIORIUM_NO_SUBAGENTS`）。

思考开关：`--thinking off|low|medium|high`（默认 `medium`）、`--no-thinking`
（= off）、`--hide-reasoning`（只隐藏显示）。`LIORIUM_TEMPERATURE` 只在思考关闭时生效。

models.dev：`--models-dev off|cache|refresh`（默认 `cache`）、`--models-dev-cache`、
`--refresh-models`。`cache` 不联网；`refresh` 下载目录；识别结果只改内存，不回写配置。

---

## 9. `liorium.provider`

`from liorium.provider import ...`。LLM Provider 管理：配置 schema、四种 API 格式、
思考能力/参数、模型构建、`/models` 自动发现、models.dev 目录。本包不读产品配置
路径、不读 stdin；默认配置发现属于宿主。

### 9.1 配置与注册表

| 名字 | 签名 / 语义 |
|---|---|
| `ModelProvider` | Pydantic 模型；字段与 `providers.json` 同 schema（`key`、`models`、`model_context_lengths`、`thinking_type`、`model_api_formats`、`format_base_urls`、`model_*_caps`、`model_catalog_routes`、`catalog_provider_id`、`opencode_go`、`stream_usage` …）；`api_key` 只读属性；`context_window(model, fallback)` / `max_output_tokens(model, fallback)` |
| `ModelProviders` | `providers`、`main_model`、`subagent_models`；`find(name)` |
| `loads(path)` / `dumps(providers, path)` | 读/原子写配置（0600）；未知字段保留 |
| `ProviderRegistry` | `providers`（有序 dict）、`main_model`、`source`、`model_providers`；`load/from_dict/from_env`、`resolve(label, *, fallback_window=128_000, api_format=None)`、`labels/default_label/to_dict/save`、`make_chat_model`、`refresh_models`、`enrich` |
| `ResolvedModel` | `provider/model/api_format/base_url/context_window/max_output_tokens/format_source/format_reason/thinking`；`label`、`describe()` |
| `ProviderConfigError` | 配置缺失/歧义/格式不可用；同时是 `LioriumError` 与 `ValueError` |

### 9.2 格式与模型构建

| 名字 | 签名 / 语义 |
|---|---|
| `API_FORMATS` | `("openai_chat", "openai_responses", "anthropic_messages", "google_gemini")` |
| `resolve_route(provider, model) -> FormatDecision` | 手动 > provider 默认 > catalog > URL > 模型族 > openai_chat |
| `normalize_format_base_url(api_format, url)` | 归一化成 SDK 根地址；非法地址返回 `None` |
| `detect_api_format/detect_url_format/family_format/normalize_format` | 通用推断 |
| `build_chat_model(provider, model, *, intensity=None, temperature=0.3, timeout=180, max_retries=2, max_tokens=8192, purpose="chat")` | 同步构建；`purpose=aux/compact` 不思考；思考开启时省略 temperature |
| `build_chat_model_from_spec(providers, spec="", intensity=None, *, purpose="chat", auto_load=True)` | 异步入口；models 为空时尝试 `/models` |
| `close_chat_model(model)` | 只关闭模型私有客户端，不碰共享客户端 |
| `describe_provider_formats/describe_all_formats` | 宿主展示用（含思考形状） |
| `build_server_tool_specs / supported_server_tools` | provider 托管工具的格式映射 |

### 9.3 思考

| 名字 | 签名 / 语义 |
|---|---|
| `ThinkingCapability` / `ThinkingOption` | `options`、`reasoning`、`source`；`option_for(api_format, model=...)` |
| `resolve_thinking_capability(provider, model)` | 手动 > `/models` > models.dev > heuristic |
| `capability_from_models_entry/capability_from_catalog_model` | 解析 `reasoning_options` / DeepSeek `effort.supported_levels` / `reasoning:false` |
| `level_for_intensity / budget_for_intensity` | 档位就近映射 / 预算钳制 |
| `resolve_thinking_dialect(provider, model)` | OpenAI Chat 方言：显式 `thinking_type` 优先，`auto` 按名字推断，回落 `openai` |
| `normalize_intensity(value)` | `off/none/""` → `None`；`low/medium/high`；非法抛 `ValueError` |
| `get_format_thinking_params / get_thinking_params` | 四格式参数翻译 |
| `ReasoningChatOpenAI` | 保留 OpenAI Chat 的 `reasoning_content` 到 `additional_kwargs` |

### 9.4 `/models` 与 models.dev

| 名字 | 签名 / 语义 |
|---|---|
| `fetch_provider_model_entries(provider, *, timeout=10, user_agent=...)` | 解析 OpenAI `data[]` / 裸数组；保留 base_url 路径前缀 |
| `auto_load_model_for_provider(provider, force=False)` | 自动填充 context/output/caps/thinking，只填空缺；force 裁剪陈旧项 |
| `ensure_catalog(*, allow_network=True, force=False, cache_path=None)` | 内存 → 磁盘 → 网络；失败回退陈旧缓存或 `{}` |
| `refresh_catalog(*, cache_path=None)` | 强制下载并原子写缓存 |
| `catalog_status(*, cache_path=None)` | 状态（来源/数量/年龄/错误/URL） |
| `match_provider / route_model / enrich_provider / enrich_all` | host + 模型重合匹配、格式路由、补全限制（不覆盖用户值） |
| `default_cache_path()` | `~/.liorium/cache/models_dev/api.json` |

不变量：

- `host_matched=False` 时只采用目录格式，端点仍用用户 `base_url`；
- 显式指定格式却没有可用端点时抛错，不静默换主机；
- 目录刷新/下载失败不阻塞启动；
- 识别结果只改内存，不写回配置。

---

## 10. `liorium.realm-base`

realm 的契约与写作 API（2026-11-07 从 `liorium-agent` 搬出）。模块路径 `liorium.realm_base`；
只依赖 `liorium-core` + `liorium-vfs`，**模块级不 import `langchain*` / `langgraph*`**。
`liorium-agent` 依赖它并 re-export：§4.6 的签名对 `from liorium.agent import ...` 同样成立，
且是**同一批对象**。

### 10.1 公开面

| 名字 | 说明 |
|---|---|
| `Realm` / `BaseRealm` / `RealmContext` / `JsonStateStore` / `RealmTaskScope` / `RealmRecord` / `utc_stamp` | 与 §4.6 完全同一批对象（`is` 相同） |
| `TurnInfo` | `TurnInfo(index: int, id: str, origin: str = "user")`；回合身份，`index` 与 `turn_id` 的序号是同一个计数器 |
| `ContextSink` | 结构协议：`queue(items: Sequence[ContextItem]) -> None`；`RealmContext.context` 的类型，`ContextChannel` 天然满足 |
| `realm_tool` | 见 §10.3 |
| `build_realm_tools(realm)` / `marked_methods(cls)` | 自定义 `tools()` 时复用收集逻辑 |

### 10.2 `BaseRealm` 的写作 API

```python
class BaseRealm:
    def __init__(self, spec: Any = None) -> None: ...   # 存 spec / params；ctx=None；带 logger

    ctx: RealmContext | None      # start() 挂上，stop() 清空
    state: JsonStateStore         # property；未 start() 抛 RealmError
    async def save(self, **values: Any) -> None    # update_many + 一定 flush
    async def on_start(self) -> None               # 默认 start() 里调用
    async def on_stop(self) -> None                # 默认 stop() 里调用
    def tools(self) -> list[Any]                   # 默认收集 @realm_tool
```

| 行为 | 语义 |
|---|---|
| `__init__(spec)` | `self.spec` / `self.params = dict(spec.params)`；类本身可直接当 `(spec) -> Realm` 工厂 |
| `start(ctx)` / `stop(ctx)` | 默认：`self.ctx = ctx` → `await self.on_start()`；`on_stop()` 之后清空 `ctx` |
| `tools()` | 按类定义顺序收集 `@realm_tool` 方法；**没有标记方法时返回 `[]`，且不 import langchain** |
| `save(**values)` | 有值先 `update_many`，然后**一定** `flush()`（补上"忘了 flush"的坑） |
| `state` | 未 `start()` 抛 `RealmError`，不返回 `None` |

### 10.3 `@realm_tool`

```python
realm_tool(fn=None, *, name=None, description=None, **tool_kwargs)
```

- 轻量标记：装饰时不 import langchain、不构建对象；
- `name` 默认取方法名，`description` 默认取 docstring；
- 其余关键字原样转给 `langchain_core.tools.tool`（`return_direct=True` 之类）；
- 子类覆盖父类同名方法 ⇒ 只留子类那份；自己重写 `tools()` ⇒ 糖完全不生效（老写法零改动）；
- 构建工具需要 `langchain-core`（`liorium-realm-base[tools]`）；缺包时抛 `RealmError` 并带安装提示，不静默返回空。

不变量：

- `import liorium.realm_base` 不加载任何 `langchain*` / `langgraph*`（守卫 `test_realm_base_stays_light`）；
- `liorium.realm_base` 不许 import `liorium.agent`（否则 agent → 它 → agent 成环，守卫 `test_liorium_dependency_direction`）；
- `liorium.agent` re-export 的名字与 `liorium.realm_base` 是同一对象（`is` 相同）。

## 11. `liorium.todo_realm`

状态化待办能力包（extra `[todo]`）。模块 `liorium.todo_realm`；只依赖
`liorium-core` + `liorium-realm-base`（**不** import `liorium.agent` / `liorium-vfs`）。

### 11.1 公开面

| 名字 | 说明 |
|---|---|
| `TodoRealm` | `BaseRealm` 子类：`id="todo"`、`state_scope="agent"`、`context_every=50`、`context_first=True` |
| `Todo` | dataclass：`id` / `text` / `state` / `created_at` / `updated_at`；`to_dict()` / `from_dict()`（严格：未知状态抛 `ValueError`） |
| `TodoState` / `STATES` / `ACTIVE_STATES` | `Literal["pending","processing","blocked","finished"]`；`ACTIVE_STATES = ("pending","processing")` |
| `parse_state(value)` | 大小写不敏感归一；非法值抛 `ValueError`（带允许值） |
| `render_list` / `render_context` / `CONTEXT_REMINDER` | 渲染工具输出 / 注入正文 / 固定的行为提醒句 |

### 11.2 工具

```python
async def todo(action: str, text: str = "", id: str = "", status: str = "") -> str
def todo_list(include_finished: bool = False) -> str
```

| 输入 | 返回 |
|---|---|
| `action="add"` + 非空 `text` | `已添加 t3 [pending] 写周报`（`status` 可省略，默认 pending） |
| `action="update"` + `id` + 至少一个 `text`/`status` | `已更新 t3 [processing] 写周报` |
| `action="remove"` + `id` | `已删除 t3：写周报` |
| 任何非法输入（未知 action / 未知 id / 空 text / 非法状态） | `错误：…` 文本；**不抛异常**、不改状态 |

`action` / `status` 声明成 `str` 而不是 `Literal`：schema 里由 docstring 说明取值，
入参大小写都收（与 `liorium-memory` 的 `return_mode` 同一取向）。

### 11.3 状态与节奏

- 状态文件：`<inbox>/realm_state/todo.json`（`state_scope="agent"`，`reset_dialog()` 不清空）；
- 键：`items`（`Todo.to_dict()` 列表）、`next_id`（下一个 id，删除后**不复用**）、
  `__context_due__`（框架保留，realm 不读写）；
- 每次工具写操作 `save()` 立即落盘；
- `context_items()`：只读内存；仅当存在 `pending` / `processing` 时返回一条
  `ContextItem(source="realm:todo", meta={"open": n, "ids": [...]})`，正文按
  「processing → pending → blocked」排序并附「按状态更新」提醒；否则返回 `None`（不消费周期）；
- 节奏可用 `RealmSpec(id="todo", params={"context_every": 10, "context_first": False})` 覆盖。

### 11.4 宿主便利方法

`todos(*, include_finished=False) -> list[Todo]`、`counts() -> dict[str, int]`、
`summary_line() -> str`（CLI 的 `/todo` 与 `/status` 用）。
