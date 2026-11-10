"""Realm 契约、``RealmContext``、状态 store 与写作 API。

一个能力 = **四件套**：工具 + VFS 挂载 + 状态 + ``prompt_fragment()``；
另有两条可选节拍：``dirty()``（真相源变了，v1.1）与 ``context_items()``（周期上下文，
第五拍，2026-11-07 起由 ``RealmManager`` 驱动，见 ``docs/design/realm-context.md``）。

**实例作用域**（2026-11-08）：``instance_scope="agent"``（默认，每个 Agent 一份、
状态落各自 inbox）或 ``"tree"``（整棵 Agent 树一份，持有挂载/外部连接的 realm 必须选它）。
框架强制不变式：``agent`` 级 realm 不许返回挂载点 —— 见 :class:`BaseRealm`。

写作 API（2026-11-07 起）：``@realm_tool`` + ``self.state`` + ``save()`` + ``on_start/on_stop``
—— 子类不写 ``__init__`` / ``start`` / ``stop`` / ``tools`` 也能用。

realm 只能往基座中间件链**外层**追加，不能重排（§C8）。
后台任务必须走 ``ctx.spawn``，禁止裸 ``asyncio.create_task``（§C4）。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Protocol, Sequence, runtime_checkable

from liorium.core.errors import RealmError
from liorium.core.log import get_logger
from liorium.core.types import ContextItem
from liorium.vfs.locks import AsyncRWLock

from .tools import build_realm_tools, realm_tool

if TYPE_CHECKING:  # pragma: no cover
    from liorium.vfs.fs import VirtualFileSystem
    from liorium.vfs.node import VfsMount


class ContextSink(Protocol):
    """``ctx.inject()`` 的落点：只要能收下待物化的 Context 就行。

    刻意用**结构类型**而不是 ``liorium.agent.context.ContextChannel``：本包不许 import
    ``liorium.agent``（否则 ``agent → realm_base → agent`` 成环），而 Agent 侧的通道只需要
    实现 ``queue(items)`` 这一个方法即可满足本协议。
    """

    def queue(self, items: Sequence[ContextItem]) -> None: ...


class RealmTaskScope:
    """``ctx.spawn`` 的宿主：托管后台任务，``stop`` 时统一取消。

    这是 **唯一** 允许调用 ``asyncio.create_task`` 的地方（架构守卫白名单）。

    ``context`` 参数：默认继承当前 ``contextvars``（LangChain 的
    ``var_child_runnable_config`` 就在里面）。做**跟当前回合无关**的后台活（记忆实体
    抽取、向量索引）时必须传一个空白 :class:`contextvars.Context`，否则那次 LLM 调用
    会成为主 run 的子 run，``on_chat_model_stream`` 会冒泡进主 Agent 的事件流，
    被当成主回复的 delta 发出去。
    """

    def __init__(self, *, log_name: str = "realm") -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self.log = get_logger(log_name)

    def spawn(
        self,
        coro: Awaitable[Any],
        *,
        name: str | None = None,
        context: "contextvars.Context | None" = None,
    ) -> "asyncio.Task[Any]":
        if self._closed:
            raise RuntimeError("realm 已停止，不能再 spawn 后台任务")
        if context is None:
            task = asyncio.create_task(coro, name=name)
        else:
            task = asyncio.create_task(coro, name=name, context=context)
        self._tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.log.error("realm 后台任务异常: %s", exc, exc_info=exc)

        task.add_done_callback(_done)
        return task

    def __len__(self) -> int:
        return len(self._tasks)

    async def cancel_all(self, timeout: float = 5.0) -> None:
        self._closed = True
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)


@dataclass
class RealmContext:
    """交给 realm ``start()`` / ``stop()`` 的运行时上下文。"""

    realm_id: str
    vfs: "VirtualFileSystem"
    state: "JsonStateStore"
    params: dict[str, Any] = field(default_factory=dict)
    agent_id: str = ""
    inbox_dir: Path | None = None
    """Agent 的根目录（``AgentSpec.inbox_dir``）；能力包用它定位自己的数据（v1.1）。"""
    workspace_root: Path | None = None
    """裸路径的基准目录（``AgentSpec.effective_workspace_root``）。

    与 ``inbox_dir`` 不一定相同（CLI：工作区是 cwd，inbox 是 ``.liorium-cli/agent``）。
    需要解析裸路径的 realm（核心工具）用它，别自己猜。
    """
    artifacts: Any = None
    """``ArtifactStore`` 端口（Agent 的 ``OutputStore``）；独立使用时可自行注入。"""
    agent_spec: Any = None
    """装配进 Agent 时的 ``AgentSpec``（只读）。realm 可据此读 ``toolsets`` 等结构配置，
    但**不得**修改它；独立使用时为 ``None``。"""
    conversation_id: str = "main"
    """``state_scope="conversation"`` 的 realm 用它给状态分桶（v1.1）。"""
    tasks: RealmTaskScope = field(default_factory=RealmTaskScope)
    context: "ContextSink | None" = None
    """Context 通道（由 ``RealmManager`` 注入；独立使用时为 ``None``）。"""

    @property
    def log(self):
        return get_logger(f"realm.{self.realm_id}")

    def spawn(
        self,
        coro: Awaitable[Any],
        *,
        name: str | None = None,
        context: "contextvars.Context | None" = None,
    ) -> "asyncio.Task[Any]":
        """托管后台任务。禁止裸 ``asyncio.create_task``。

        ``context`` 传空白的 ``contextvars.Context()`` 可让任务与当前回合解耦
        （见 :class:`RealmTaskScope` 的说明）。
        """
        return self.tasks.spawn(coro, name=name or f"realm:{self.realm_id}", context=context)

    def inject(
        self,
        text: str,
        *,
        source: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """推一条 Context 到下一个安全点物化（同步、永不打断）。

        与第五拍 ``context_items()`` 的分工：``context_items()`` 由框架按节奏**拉**，
        这里由 realm 自己决定时机**推**（后台 watcher 发现新事实）。重活先 ``spawn``。
        没有通道时抛 ``RealmError``，不静默吞（装配期就该发现）。
        """
        if not isinstance(text, str) or not text.strip():
            raise RealmError(f"realm {self.realm_id!r} 的 inject() 需要非空文本")
        self.inject_items(
            [
                ContextItem(
                    source=source or f"realm:{self.realm_id}",
                    text=text,
                    meta=dict(meta or {}),
                )
            ]
        )

    def inject_items(self, items: Sequence[ContextItem]) -> None:
        """推一批现成的 ``ContextItem``（同上；空序列是空操作）。"""
        if self.context is None:
            raise RealmError(
                f"realm {self.realm_id!r} 没有 Context 通道"
                "（独立使用 RealmContext 时 inject() 不可用；装配进 Agent 后才有）"
            )
        batch = list(items)
        if batch:
            self.context.queue(batch)


class JsonStateStore:
    """realm 状态 store：``scope="agent"``，按 realm 一个 JSON 文件。

    计划 §C6：状态生命周期 ≠ 对话生命周期；realm 状态**不放** checkpoint。
    文件位于 ``inbox_dir/realm_state/<realm_id>.json``，内容必须是可 JSON 化的
    （不可 JSON 化会立刻报错，不静默丢弃）。
    """

    def __init__(self, path: Path, *, realm_id: str = "") -> None:
        self.path = Path(path)
        self.realm_id = realm_id
        self._data: dict[str, Any] = {}
        self._lock = AsyncRWLock()
        self._dirty = False
        self.log = get_logger(f"realm.state.{realm_id}" if realm_id else "realm.state")

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"realm 状态文件损坏: {self.path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"realm 状态文件不是 JSON 对象: {self.path}")
        self._data = raw

    def flush(self) -> None:
        """落盘（原子替换）。"""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._lock.read_lock():
            return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        json.dumps(value)  # 立刻验证可序列化
        async with self._lock.write_lock():
            self._data[key] = value
            self._dirty = True

    async def delete(self, key: str) -> bool:
        async with self._lock.write_lock():
            existed = key in self._data
            self._data.pop(key, None)
            self._dirty = self._dirty or existed
            return existed

    async def all(self) -> dict[str, Any]:
        async with self._lock.read_lock():
            return dict(self._data)

    async def update_many(self, values: dict[str, Any]) -> None:
        for v in values.values():
            json.dumps(v)
        async with self._lock.write_lock():
            self._data.update(values)
            self._dirty = True

    @property
    def updated_at(self) -> float:
        return self.path.stat().st_mtime if self.path.exists() else 0.0


@dataclass(frozen=True, slots=True)
class TurnInfo:
    """一个回合的身份（第五拍 ``context_items(turn)`` 的入参）。

    ``index`` 与 ``turn_id`` 的序号是**同一个计数器**（``LioriumAgent._turn_seq``），
    语义是"本进程内第几个 ``run()``"，不是"第几次模型调用"。
    """

    index: int
    id: str
    origin: str = "user"


@runtime_checkable
class Realm(Protocol):
    """realm 协议（计划 §3.3）。

    类属性 ``id`` / ``requires`` / ``state_scope`` / ``instance_scope``；方法见下。

    ``instance_scope``（2026-11-08）决定实例作用域：``"agent"`` = 每个 Agent 一份，
    ``"tree"`` = 整棵 Agent 树一份（有挂载/外部连接/全局存储的必须选它）。
    详见 :class:`BaseRealm` 上的同名类属性说明。

    v1.1 追加两个**可选**钩子（实现能力包时才需要）：

    - ``dirty()`` / ``mark_dirty()``：运行期真相源变了（MCP 发现新工具、装了技能），
      由 Agent 在**回合边界**刷新工具/挂载/提示词，而不是让宿主记得重建；
    - ``skills()``：该 realm 想预载进 system prompt「技能清单」段的条目。
    """

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


class BaseRealm:
    """给宿主一个可继承的默认实现（全部可选方法 + 写作 API）。

    子类典型写法（一个方法不写脚手架）::

        class TodoRealm(BaseRealm):
            id = "todo"
            context_every = 50

            async def on_start(self) -> None:
                self.items = await self.state.get("items", [])

            @realm_tool
            async def todo_add(self, text: str) -> str:
                \"\"\"加一条待办。\"\"\"
                self.items.append({"text": text})
                await self.save(items=self.items)
                return f"已加入：{text}"

    - ``start()`` / ``stop()`` 的默认实现只负责挂 ``self.ctx`` 并调 ``on_start()`` /
      ``on_stop()``；要更多控制就整体覆盖它们；
    - ``tools()`` 的默认实现收集 ``@realm_tool`` 标记的方法（子类覆盖后以子类为准）；
    - ``save()`` = ``update_many`` + ``flush``，顺手补上"忘了 flush"的坑。
    """

    id: str = ""
    requires: list[str] = []
    state_scope: str = "agent"

    # -- 实例作用域（2026-11-08）：决定"一棵 Agent 树里有几份实例" --
    instance_scope: str = "agent"
    """``"agent"``（默认）或 ``"tree"``。

    - ``"agent"``：每个 Agent（含子代理）各一份实例，状态落在它自己的
      ``inbox_dir/realm_state/`` 下 ⇒ 天然隔离。**不允许有 VFS 挂载**（挂载点是全树共享的，
      两条状态抢同一路径必然打架），``RealmManager`` 启动期会直接报错。
    - ``"tree"``：整棵 Agent 树共享**同一个实例**（同一个状态文件、同一次挂载、同一条外部
      连接）。持有 VFS 挂载点 / 外部连接 / 全局存储的 realm 必须选它；子代理借用它时只借
      ``tools()``，不重复挂载、不重复消费 ``context_items`` 节奏、不重复生效中间件与提示词。
    """

    # -- 第五拍：周期上下文（声明；节拍器由 RealmManager 驱动，见 docs/design/realm-context.md）--
    context_every: int = 0
    """每 N 个回合给一次注入机会；``0`` = 不参与（默认）。``RealmSpec.params`` 可覆盖。"""
    context_first: bool = False
    """``True``：第 1 回合先注入一次，再进入 ``context_every`` 的节奏。"""
    context_origins: tuple[str, ...] = ()
    """只对哪些 ``run(origin=...)`` 计时；空 = 全部。"""

    def __init__(self, spec: Any = None) -> None:
        self.spec = spec
        self.params: dict[str, Any] = dict(getattr(spec, "params", {}) or {})
        self.ctx: RealmContext | None = None
        self.log = get_logger(f"realm.{self.id or type(self).__name__}")

    # ------------------------------------------------------------------
    # 状态 / 写作 API
    # ------------------------------------------------------------------

    @property
    def state(self) -> JsonStateStore:
        """本 realm 的状态 store（未 ``start()`` 时抛 ``RealmError``）。"""
        if self.ctx is None:
            raise RealmError(f"realm {self.id or type(self).__name__!r} 还没 start()，没有状态可读写")
        return self.ctx.state

    async def save(self, **values: Any) -> None:
        """写状态并**立刻落盘**（``JsonStateStore.set`` 只标 dirty）。"""
        if values:
            await self.state.update_many(values)
        self.state.flush()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, ctx: RealmContext) -> None:
        self.ctx = ctx
        await self.on_start()

    async def stop(self, ctx: RealmContext) -> None:
        try:
            await self.on_stop()
        finally:
            self.ctx = None

    async def on_start(self) -> None:
        """``start()`` 默认实现里的钩子；``self.ctx`` / ``self.state`` 已经可用。"""

    async def on_stop(self) -> None:
        """``stop()`` 默认实现里的钩子；此时 ``self.ctx`` 仍然可用。"""

    # ------------------------------------------------------------------
    # 四件套
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        """默认实现：收集 ``@realm_tool`` 标注的方法（没有则 ``[]``，且不 import langchain）。"""
        return build_realm_tools(self)

    def prompt_fragment(self) -> str:
        return ""

    def middleware(self) -> list[Any]:
        return []

    def mounts(self) -> dict[str, "VfsMount"]:
        return {}

    def skills(self) -> list[str]:
        """预载进 system prompt「技能清单」的条目（v1.1 可选钩子）。"""
        return []

    def dirty(self) -> bool:
        """真相源是否变了、需要 Agent 在回合边界刷新（v1.1 可选钩子）。"""
        return False

    def mark_dirty(self) -> None:
        """由宿主/realm 自己标记"需要刷新"（v1.1 可选钩子）。"""

    def context_items(self, turn: TurnInfo) -> Any:
        """第五拍：返回 ``None`` / ``str`` / ``ContextItem`` / 它们的序列（可 sync 可 async）。

        框架侧语义（``RealmManager.context_for_turn``，见 ``docs/design/realm-context.md``）：
        每 N 回合问一次；返回空**不消费周期**（下一回合继续问），非空才重置倒计时。
        调用发生在持有输出许可期间 ⇒ 必须廉价（只读内存 / 状态，别做 IO 与 LLM）。
        """
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r}>"


@dataclass
class RealmRecord:
    """装配期记录：spec + 实例 + 运行态。"""

    spec_id: str
    instance: Any
    order: int = 0
    state: str = "pending"
    error: str | None = None
    started_at: float | None = None
    context: RealmContext | None = None
    tools: list[Any] = field(default_factory=list)
    context_due: int | None = None
    """第五拍倒计时（``None`` = 还没从 state 读回；0 = 本回合就该问）。"""
    context_injections: int = 0
    """本进程内成功注入次数（只用于 ``status()`` 观测，不落盘）。"""

    @property
    def id(self) -> str:
        return self.spec_id


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


__all__ = [
    "BaseRealm",
    "ContextSink",
    "JsonStateStore",
    "Realm",
    "RealmContext",
    "RealmRecord",
    "RealmTaskScope",
    "TurnInfo",
    "realm_tool",
    "utc_stamp",
]
