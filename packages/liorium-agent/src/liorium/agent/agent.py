"""``LioriumAgent`` —— 一个类，三个正交维度（身份 / 关系 / 权限）。

计划 §B1/§B2/§B4/§B5/§3.2：

- 身份：``id`` / 根目录 / 记忆 / checkpoint
- 关系：``parent`` / ``lifecycle_owner`` / ``accepts_external_turns``（由调用方决定谁 run 它）
- 权限：谁能中断 / 配置（v1 不做权限系统，保留端口）

生命周期语义收敛成 4 个显式操作：``start`` / ``close`` / ``run`` / ``apply``（+ ``reset_dialog``）。
**身份不变 ⇒ 实例永不重建**，``apply()`` 只原位替换可变部件。

2026-11-08 起没有「宿主端口」：``run()`` 返回的生成器是**唯一**事件出口（只产出这一个
回合的事件），排队 / 抢占 / 丢弃全在 ``TurnScheduler`` 里，跨 Agent 的并发由调用方自己管。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from liorium.core.errors import (
    SpecError,
    ToolNameConflictError,
    TurnError,
    TurnInterrupted,
)
from liorium.core.log import get_logger
from liorium.core.types import ContextItem, Event
from liorium.vfs.fs import VirtualFileSystem

from .context import ContextChannel
from .realm.base import RealmTaskScope, TurnInfo
from .realm.manager import RealmFactory, RealmManager
from .realm.subagent import SubagentRealm
from .runtime.scheduler import DEFAULT_FLAGS, FLAG, TurnScheduler
from .runtime.types import TurnRequest, TurnRuntime
from .spec import AgentSpec
from .status import AgentStatus, ApplyResult, CacheEpoch

log = get_logger("liorium.agent")

_SENTINEL = object()


def _same_callable(left: Callable[..., Any], right: Callable[..., Any]) -> bool:
    """两个可调用对象是否指向**同一个调用目标**。

    ``queue.put`` / ``obj.method`` 每次属性访问都会新建一个 bound method 对象，
    所以 ``left is right`` 对同一目标也为 ``False``；按 ``__self__`` / ``__func__``
    比才是"同一个方法"。
    """
    if left is right:
        return True
    left_self = getattr(left, "__self__", None)
    right_self = getattr(right, "__self__", None)
    if left_self is None or right_self is None:
        return False
    return left_self is right_self and (
        getattr(left, "__func__", None) is getattr(right, "__func__", None)
    )


class _TurnEmitter:
    """可换目标的回合事件出口（压缩中间件在编译期就要拿到它）。

    同一时刻只允许绑一个出口：``run()`` 是唯一事件通道，压进错回合的队列就是
    "别的消息的事件串了"。绑定冲突（理论上只会在内部不变量被破坏时出现）记日志
    并覆盖；解绑是**条件解绑**（只有当前绑的仍是本回合的 ``fn`` 才清空，按调用目标
    比较，不按 bound method 对象身份），这样旧回合收尾不会把新回合的出口一起关掉。
    """

    def __init__(self) -> None:
        self._fn: Callable[[Event], Any] | None = None

    def bind(self, fn: Callable[[Event], Any] | None) -> None:
        if fn is not None and self._fn is not None and not _same_callable(self._fn, fn):
            log.error("回合事件出口被重复绑定：旧回合收尾顺序异常")
        self._fn = fn

    def unbind(self, fn: Callable[[Event], Any]) -> None:
        """只在当前绑定的仍是 ``fn`` 时清空（别的回合已接管就不动）。"""
        if self._fn is not None and _same_callable(self._fn, fn):
            self._fn = None

    async def __call__(self, event: Event) -> None:
        if self._fn is None:
            return
        result = self._fn(event)
        if asyncio.iscoroutine(result):
            await result


@dataclass(frozen=True)
class _RuntimeParts:
    """一次完整构建产物；只有全部字段都就绪后才允许提交到 Agent。"""

    spec: AgentSpec
    model: Any
    tools: list[Any]
    middleware: list[Any]
    compact: Any
    graph: Any
    prompt: Any


class LioriumAgent:
    """Liorium 的 Agent 实例。"""

    def __init__(
        self,
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
        tool_names: Sequence[str] | None = None,
    ) -> None:
        self.spec = spec
        self.model_factory = model_factory
        self.compact_model_factory = compact_model_factory
        self.parent = root_agent
        # 血缘树的根：`instance_scope="tree"` 的 realm 归根 Agent 那份实例。
        self._tree_root: LioriumAgent = root_agent._tree_root if root_agent is not None else self
        self.memory = memory if memory is not None else (root_agent.memory if root_agent else None)
        # 血缘根：独立顶层 Agent 各自一棵 VFS；Subagent 共享父的实例（§D2）
        self.vfs = vfs if vfs is not None else (
            root_agent.vfs if root_agent is not None else VirtualFileSystem(owner_id=spec.id)
        )
        self.artifacts = artifacts if artifacts is not None else (
            root_agent.artifacts if root_agent is not None else None
        )
        self.api_format = api_format or (root_agent.api_format if root_agent else None)
        # 顶层 Agent 默认 thread_id="main"：旧 checkpoint.db 的 thread 就叫 main，
        # 这样才能"加载旧库继续对话"（计划 §3.7 / §F5）。子代理用自己的 id。
        self.thread_id = thread_id or ("main" if root_agent is None else spec.id)
        self.priority = priority
        """本 Agent 的默认调度优先级；``run(priority=...)`` 可按次覆盖。"""
        self.system_prompt_override = system_prompt_override
        self._tool_names: tuple[str, ...] | None = (
            None if tool_names is None else tuple(str(n).strip() for n in tool_names if str(n).strip())
        )
        """非 None ⇒ 只保留这些名字的工具（子代理的 ``toolsets`` 靠它真正生效）。

        2026-11-08：``AgentSpec.toolsets`` 在此之前只参与指纹/``profile`` 合并，**没有任何
        代码拿它过滤工具** —— ``newSubagent(toolsets=...)`` 声明了却不生效。工具全部来自
        realm（见 ``_prepare_runtime``），所以过滤只能发生在装配末尾。
        """

        self.realm_factories: dict[str, RealmFactory] = dict(realm_factories or {})
        # Context 通道要先于 RealmManager 建好：第五拍（context_items）与 ctx.inject()
        # 都往它排队，manager 在 start() 时把它交给每个 RealmContext。
        self.context_channel = ContextChannel()
        self.realms = RealmManager(
            agent_id=spec.id,
            vfs=self.vfs,
            state_dir=spec.inbox_dir / "realm_state",
            factories=self.realm_factories,
            # conversation scope（v1.1）按对话分桶；一个 Agent 实例 = 一个 thread。
            conversation_id=self.thread_id,
            context_channel=self.context_channel,
            # 子 Agent：从树根借用 `instance_scope="tree"` 的 realm（同一实例/状态/挂载）。
            parent=self._tree_root.realms if root_agent is not None else None,
        )

        from .runtime.checkpoint import CheckpointStore
        from .runtime.output_store import OutputStore

        self.checkpoint = CheckpointStore(
            Path(checkpoint_path) if checkpoint_path else spec.inbox_dir / "checkpoint.db"
        )
        if self.artifacts is None:
            self.artifacts = OutputStore(root=spec.inbox_dir / "artifacts")
        # realm 的运行时上下文（workspace root / artifacts / spec）在这里定稿：
        # 不进 RealmSpec.params（params 参与指纹，绝对路径会污染它）。
        self.realms.configure(
            agent_spec=spec,
            workspace_root=spec.effective_workspace_root,
            artifacts=self.artifacts,
        )

        self._started = False
        self._closed = False
        self._model: Any = None
        self._graph: Any = None
        self._tools: list[Any] = []
        self._middleware: list[Any] = []
        self._compact: Any = None
        self._epoch = CacheEpoch()
        self._emitter = _TurnEmitter()
        self._turn_seq = itertools.count(1)
        self._current_turn_id: str | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._turn_scope: RealmTaskScope | None = None
        self._interrupt = asyncio.Event()
        self._turn_reason = ""
        self._message_count = 0
        self._subagent_realm: SubagentRealm | None = None
        self._artifacts_mount_owned = False
        self._warned_compact_disabled = False

        self._scheduler = TurnScheduler()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def graph(self) -> Any:
        return self._graph

    @property
    def model(self) -> Any:
        return self._model

    @property
    def tools(self) -> list[Any]:
        return list(self._tools)

    @property
    def tree_root(self) -> "LioriumAgent":
        """血缘树的根 Agent（顶层 Agent 返回自己）；树级 realm 的实例归它。"""
        return self._tree_root

    @property
    def epoch(self) -> CacheEpoch:
        return self._epoch

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # start / close
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """建 VFS / 起 realm / 开 checkpoint / 建图 / 播种 system 消息。

        **不调模型**（计划 §F7）：首次引导由调用方 ``run(boot.message)``。
        """
        if self._started:
            return
        if self._closed:
            raise TurnError("agent 已 close，不能重新 start")
        self.spec.inbox_dir.mkdir(parents=True, exist_ok=True)

        if "subagents" not in self.realm_factories:
            self.realm_factories["subagents"] = self._make_subagent_realm
        # RealmManager 构造时拷了一份工厂表；start 时同步调用方在这之后注册的工厂
        self.realms.factories.update(self.realm_factories)

        self.realms.attach_specs(self.spec.realms)
        self.realms.assemble(self.spec.realms)
        await self.realms.start_all()
        await self._mount_artifacts()
        await self._build_runtime()
        self._started = True
        log.info("agent 启动: %s（realm=%s）", self.spec.id, self.realms.order)

    def _make_subagent_realm(self, realm_spec: Any) -> SubagentRealm:
        realm = SubagentRealm(parent=self, params=dict(realm_spec.params))
        self._subagent_realm = realm
        return realm

    async def _mount_artifacts(self) -> None:
        """把 OutputStore 投影到 VFS 的 ``/artifacts``（``artifact://`` 的真实入口）。

        子代理共享父的 VFS：已存在同名挂载时直接复用，既不会重复挂载，也不会在
        子代理关闭时把父的挂载卸掉。
        """
        store = self.artifacts
        if store is None:
            return
        if await self.vfs.stat("/artifacts") is not None:
            self._artifacts_mount_owned = False
            return

        from liorium.core.types import Image, Text
        from liorium.vfs.node import VfsNode

        projection = VirtualFileSystem(owner_id=f"{self.spec.id}:artifacts")
        root = projection.root
        root.is_directory = True
        root.should_be_included_in_search = False
        root.description = "工具输出（只读）"

        def _node(name: str) -> Any:
            artifact = store.get(name)
            if artifact is None:
                return None
            if artifact.content_type == "image" or (artifact.images and not artifact.content):
                image = artifact.images[0] if artifact.images else None
                if image is None:
                    return None
                content = Image(
                    data=bytes(image["data"]),
                    mime=str(image.get("mime") or "image/png"),
                )
                return VfsNode(
                    name=name,
                    provider=lambda _n, c=content: c,
                    description=artifact.tool_name,
                    should_be_included_in_search=False,
                )
            text = str(artifact.content or "")
            return VfsNode(
                name=name,
                provider=lambda _n, c=Text(text): c,
                description=artifact.tool_name,
                should_be_included_in_search=False,
            )

        def _children(_node: Any) -> list[Any]:
            out: list[Any] = []
            for output_id in store.list_ids():
                item = _node(output_id)
                if item is not None:
                    out.append(item)
            return out

        root.child_provider = lambda _n, name: _node(name)
        root.children_provider = _children
        try:
            self.vfs.mount_vfs("/artifacts", projection, readonly=True)
        except Exception as exc:  # noqa: BLE001 - 回读不可用不该拖垮 start
            log.warning("挂载 /artifacts 失败（artifact:// 回读可能不可用）: %s", exc)
            return
        self._artifacts_mount_owned = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        await self.interrupt(reason="agent_close")
        # 排队中的 run() 不能永远睡在 scheduler 上
        self._scheduler.reset()
        try:
            if self._subagent_realm is not None:
                await self._subagent_realm.shutdown()
            await self.realms.stop_all()
        finally:
            if self._artifacts_mount_owned:
                self.vfs.unmount("/artifacts")
                self._artifacts_mount_owned = False
            await self.checkpoint.close()
            self._graph = None

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------

    async def _build_runtime(self, *, spec: AgentSpec | None = None) -> None:
        """按目标 spec 重建可变部件：先完整构建，成功后再原子替换。"""
        target = spec if spec is not None else self.spec
        parts = await self._prepare_runtime(target)
        self._commit_runtime(parts)

    async def _prepare_runtime(self, spec: AgentSpec) -> _RuntimeParts:
        """构建新运行时；全部是局部变量，失败时 ``self`` 保持原样。"""
        from .runtime.state import LioriumState  # 延迟导入，避免包导入期依赖 langgraph
        from .runtime.tools import wrap_tools
        from .runtime.middleware import build_chain
        from .runtime.middleware.compact import CompactMiddleware
        from .runtime.checkpoint import thread_config
        from langchain.agents import create_agent

        model = self.model_factory(spec.model)
        compact_model = (
            self.compact_model_factory(spec.model)
            if self.compact_model_factory is not None
            else model
        )
        # Agent 自己**不再**添加任何工具：工具全部来自用户装配的 realm（``spec.realms``）。
        # `read/write/...` 由 `CoreToolRealm` 提供，`todo` 由 `TodoRealm` 提供 —— 想要什么装什么。
        tools: list[Any] = list(self.realms.tools())
        if self._tool_names is not None:
            allowed = set(self._tool_names)
            kept = [t for t in tools if str(getattr(t, "name", "")) in allowed]
            if len(kept) != len(tools):
                log.info(
                    "agent %s 按 toolsets 过滤工具：保留 %d / 共 %d",
                    spec.id,
                    len(kept),
                    len(tools),
                )
            tools = kept
        self._assert_tool_uniqueness(tools)
        if not tools:
            log.info(
                "agent %s 没有装配任何工具（realms=%s）：模型看不到任何工具",
                spec.id,
                self.realms.order,
            )

        wrapped = wrap_tools(
            tools,
            artifacts=self.artifacts,
            max_chars=spec.limits.max_tool_output_chars,
        )

        prompt = self._build_prompt(spec)

        # 压缩始终装配：context_window<=0 时 threshold=0，自动压缩不触发，
        # 但「窗口未知」的告警与手动 force_compact 仍然可用。
        if (
            spec.limits.context_window <= 0
            and self.parent is None
            and not self._warned_compact_disabled
        ):
            self._warned_compact_disabled = True
            log.warning(
                "context_window 未配置（0）：自动压缩不会触发；"
                "请设置 Limits.context_window 或手动 /compact。agent=%s",
                spec.id,
            )
        compact = CompactMiddleware(
            model=compact_model,
            emit=self._emitter,
            api_format=self.api_format,
            context_window=spec.limits.context_window,
            threshold_ratio=spec.limits.compact_threshold_ratio,
            keep_rounds=spec.limits.compact_keep_rounds,
            # 成本控制打开时才施加输出上限；摘要模型自身的上限单独传
            max_output_tokens=(
                spec.limits.compact_max_output_tokens
                if spec.limits.compact_cost_control
                else 0
            ),
            summary_max_tokens=(
                spec.limits.compact_max_output_tokens
                if (self.api_format or "").startswith("anthropic")
                else 0
            ),
            cost_control=spec.limits.compact_cost_control,
            max_input_tokens=spec.limits.compact_max_input_tokens,
            tools=wrapped,
        )

        chain = build_chain(
            artifacts=self.artifacts,
            compact=compact,
            realm_middlewares=self.realms.realm_middlewares(),
            context_channel=self.context_channel,
        )

        saver = await self.checkpoint.open()
        graph = create_agent(
            model=model,
            tools=wrapped,
            middleware=chain,
            state_schema=LioriumState,
            checkpointer=saver,
            name=spec.id,
        )
        config = thread_config(self.thread_id, recursion_limit=spec.limits.recursion_limit)
        # 先用新 graph 播种 system prompt；失败则整体不提交，旧运行时不受影响。
        await self.checkpoint.seed_system_prompt(graph, config, prompt.text)
        return _RuntimeParts(
            spec=spec,
            model=model,
            tools=wrapped,
            middleware=chain,
            compact=compact,
            graph=graph,
            prompt=prompt,
        )

    def _commit_runtime(self, parts: _RuntimeParts) -> None:
        """原子提交：只做同步赋值，不会出现「一半新、一半旧」。"""
        epoch = self._epoch.bumped(
            tool_schema_fp=self._tool_schema_fingerprint(parts.tools),
            prompt_fp=parts.prompt.fingerprint() if parts.prompt is not None else "",
            reason="apply" if self._epoch.value else "start",
        )
        self.spec = parts.spec
        self._model = parts.model
        self._tools = parts.tools
        self._middleware = parts.middleware
        self._compact = parts.compact
        self._graph = parts.graph
        self._prompt = parts.prompt
        self._system_prompt_loaded = parts.prompt.text
        self._epoch = epoch

    def _thread_config(self) -> dict[str, Any]:
        from .runtime.checkpoint import thread_config

        return thread_config(self.thread_id, recursion_limit=self.spec.limits.recursion_limit)

    def _assert_tool_uniqueness(self, tools: Sequence[Any]) -> None:
        """工具名全局唯一（跨 realm）；重复即报错，不静默取第一个。

        ``RealmManager`` 启动时已经查过一次跨 realm 冲突，这里是重建运行时的兜底
        （含 realm 自己返回两个同名工具的情况）。
        """
        owners: dict[str, list[str]] = {}
        for tool in tools:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
            if not name:
                raise SpecError(f"工具没有名字: {tool!r}")
            owners.setdefault(name, []).append(self._tool_owner(tool))
        dupes = {n: o for n, o in owners.items() if len(o) > 1}
        if dupes:
            name, rids = next(iter(dupes.items()))
            raise ToolNameConflictError(name, rids)

    def _tool_owner(self, tool: Any) -> str:
        """工具属于哪个 realm（只用于冲突报错的可读性）。"""
        for realm_id in self.realms.order:
            record = self.realms.record(realm_id)
            if any(existing is tool for existing in record.tools):
                return realm_id
        return "realm"

    def _build_prompt(self, spec: AgentSpec | None = None) -> Any:
        target = spec if spec is not None else self.spec
        from .runtime.prompt import build_system_prompt

        if self.system_prompt_override is not None:
            from .runtime.prompt import SystemPrompt

            return SystemPrompt(
                text=self.system_prompt_override,
                sections=(("identity", self.system_prompt_override),),
            )
        return build_system_prompt(
            target,
            realm_fragments=self.realms.prompt_fragments(),
            # 计划 §F2：技能清单预载进 prompt（能力发现性优先于 token 成本）
            skills=self.realms.skill_names(),
        )

    def _tool_schema_fingerprint(self, tools: Sequence[Any] | None = None) -> str:
        from liorium.core.canonical import fingerprint

        target = self._tools if tools is None else tools
        schemas = []
        for tool in target:
            try:
                from langchain_core.utils.function_calling import convert_to_openai_tool

                schemas.append(convert_to_openai_tool(tool))
            except Exception:  # noqa: BLE001 - 无法转换时退化为名字（会在日志里可见）
                log.warning("工具 schema 无法转换，指纹退化为名字: %s", getattr(tool, "name", "?"))
                schemas.append({"name": getattr(tool, "name", "")})
        schemas.sort(key=lambda s: str(s.get("function", {}).get("name") or s.get("name", "")))
        return fingerprint(schemas)

    def _bump_epoch(self) -> None:
        prompt_fp = self._prompt.fingerprint() if self._prompt is not None else ""
        self._epoch = self._epoch.bumped(
            tool_schema_fp=self._tool_schema_fingerprint(),
            prompt_fp=prompt_fp,
            reason="apply" if self._epoch.value else "start",
        )

    # ------------------------------------------------------------------
    # run / inject / interrupt
    # ------------------------------------------------------------------

    def run(
        self,
        text: str,
        *,
        context: Sequence[ContextItem] = (),
        origin: str = "user",
        priority: int | None = None,
        flags: FLAG = DEFAULT_FLAGS,
    ) -> AsyncIterator[Event]:
        """跑一个回合，返回**只属于这一条消息**的异步事件流。

        - 迭代结束 = 回合结束；错误以异常抛出（``TurnInterrupted`` / ``TurnError`` /
          ``TurnDropped``）；
        - ``priority=None`` 用本 Agent 的默认优先级（``self.priority``）；整数**越大越优先**，
          同优先级 FIFO；
        - ``flags`` 见 :class:`liorium.agent.FLAG`：``INTERRUPTING`` 可抢占正在跑的低优先级
          回合、``EXCLUSIVE`` 拒绝被抢占、``DROPPABLE`` 拥塞时可被丢弃；
        - 等待执行位期间**不产出任何事件**；不向任何别的消费者扇出（没有宿主 sink）。
        """
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 run()")
        effective = self.priority if priority is None else int(priority)
        return self._run_iter(
            text,
            context=tuple(context),
            origin=origin,
            priority=effective,
            flags=FLAG(flags),
        )

    async def _run_iter(
        self,
        text: str,
        *,
        context: Sequence[ContextItem],
        origin: str,
        priority: int,
        flags: FLAG,
    ) -> AsyncIterator[Event]:
        index = next(self._turn_seq)
        turn_id = f"{self.spec.id}#{index}"
        # 先拿回合位，再做 reconcile：并发 run() 时第二个回合不能在第一回合还在
        # 流式输出/执行工具时 stop_all()/start_all()、替换 graph、抢 pending context。
        await self._scheduler.acquire(
            priority=priority,
            flags=flags,
            tag=turn_id,
            on_preempt=self._signal_preempt,
        )
        try:
            if self._closed or not self._started:
                raise TurnError("agent 已 close，不能 run()")
            self._current_turn_id = turn_id
            self._interrupt.clear()
            self._turn_reason = ""
            if self.parent is None:
                # 子代理的 spec 由父固定，不做回合边界 realm 刷新（它的 realm 集合是空的，
                # 而且重建会把父的 spec 套到子代理身份上）。
                await self._reconcile_realms()
            if self._interrupt.is_set():
                raise TurnInterrupted(reason=self._turn_reason or "interrupt")
            # 回合开始时到达的 context：并入该回合 user 消息（形态①，计划 §4.3）
            merged_context = tuple(context) + tuple(self.context_channel.take_pending())
            # 第五拍：到期才问；排到下一个安全点物化成独立 <context> user 消息（形态②）。
            realm_context = await self._context_for_turn(index, turn_id, origin)
            if realm_context:
                self.context_channel.queue(realm_context)
            stream = self._drive_turn(turn_id, text, merged_context, origin)
            try:
                async for event in stream:
                    yield event
            finally:
                # 消费者中途放弃时 ``async for`` 不会关内层生成器，它的
                # ``finally``（解绑出口 / 取消本回合任务）会推迟到异步终结器，
                # 那时下一个回合可能已经在跑。显式 aclose：先收尾，再让外层 release。
                await stream.aclose()
        finally:
            self._current_turn_id = None
            self._scheduler.release()

    async def _context_for_turn(self, index: int, turn_id: str, origin: str) -> list[ContextItem]:
        """第五拍：问各 realm「本回合有要注入的吗」；节拍器自身出 bug 不该炸回合。"""
        turn = TurnInfo(index=index, id=turn_id, origin=origin)
        try:
            return await self.realms.context_for_turn(turn)
        except Exception as exc:  # noqa: BLE001 - 单 realm 已在 manager 里隔离，这里只兜底
            log.error("realm 周期上下文失败: %s", exc, exc_info=exc)
            return []

    async def _reconcile_realms(self) -> None:
        """回合边界的 realm reconcile（计划 §B6 + §Phase 2）。

        realm 自报 ``dirty()``（MCP 发现新工具 / 装了技能）⇒ 重挂载 + 重取工具 +
        重建运行时（工具 schema / prompt 变了，``CacheEpoch`` 自然 +1）。

        **失败不静默**：记日志并用旧部件继续跑（降级但可用，计划 §B7）。

        2026-11-08 起没有 ``spec_provider()`` 拉模式 reconcile：spec 的变更一律走显式
        ``apply()``（宿主端口已删除，库不再替调用方猜"新 spec 到了没有"）。
        """
        try:
            refreshed = await self.realms.refresh()
        except Exception as exc:  # noqa: BLE001 - 刷新机制自身不该炸回合
            log.error("realm 刷新失败，继续用旧部件: %s", exc, exc_info=exc)
            return
        if not refreshed:
            return
        try:
            await self._build_runtime()
            log.info("realm dirty 刷新: epoch=%s", self._epoch.value)
        except Exception as exc:  # noqa: BLE001
            log.error("realm 刷新后重建运行时失败，继续用旧运行时: %s", exc, exc_info=exc)

    async def _drive_turn(
        self,
        turn_id: str,
        text: str,
        context: Sequence[ContextItem],
        origin: str,
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        # bound method 每次访问都是新对象：只取一次，bind/unbind/emit 用同一个。
        emit = queue.put
        scope = RealmTaskScope(log_name=f"turn.{turn_id}")
        self._turn_scope = scope

        request = TurnRequest(
            agent_id=self.spec.id,
            turn_id=turn_id,
            text=text,
            context=context,
            origin=origin,
        )
        runtime = TurnRuntime(
            spec=self.spec,
            graph=self._graph,
            checkpointer=self.checkpoint.saver,
            model=self._model,
            tools=self._tools,
            middleware=self._middleware,
            artifacts=self.artifacts,
            vfs=self.vfs,
            emit=emit,
            interrupt=self._interrupt,
            tasks=scope,
            thread_id=self.thread_id,
            api_format=self.api_format,
            extra={"context_channel": self.context_channel},
        )
        self._emitter.bind(emit)

        from .runtime.loop import execute_turn

        error: BaseException | None = None

        async def _produce() -> None:
            nonlocal error
            try:
                async for event in execute_turn(runtime, request):
                    await queue.put(event)
            except BaseException as exc:  # noqa: BLE001 - 原样带回消费者
                error = exc
            finally:
                await queue.put(_SENTINEL)

        task = scope.spawn(_produce(), name=f"turn:{turn_id}")
        self._turn_task = task
        seen_done = False
        try:
            while True:
                get_task = asyncio.ensure_future(queue.get())
                waiters: set[Any] = {get_task}
                waiters.add(task)
                done, _pending = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED
                )
                if get_task in done:
                    item = get_task.result()
                else:
                    get_task.cancel()
                    # 生产者已结束但没有哨兵（被取消时）：把队列里剩下的取完再退出
                    if queue.empty():
                        break
                    item = queue.get_nowait()
                if item is _SENTINEL:
                    break
                if self._interrupt.is_set():
                    # 中断后不再转发残余事件（终结语义 = 迭代结束 + 异常）
                    continue
                if getattr(item, "type", None) == "done":
                    seen_done = True
                yield item
        finally:
            self._emitter.unbind(emit)
            try:
                await scope.cancel_all(timeout=5.0)
            finally:
                # 只清自己的：更晚收尾的旧回合不许抹掉新回合的字段。
                if self._turn_scope is scope:
                    self._turn_scope = None
                if self._turn_task is task:
                    self._turn_task = None
                try:
                    snapshot = await self._graph.aget_state(self._thread_config())
                    self._message_count = len(
                        (getattr(snapshot, "values", None) or {}).get("messages") or []
                    )
                except Exception:  # noqa: BLE001 - 统计失败不影响回合结果
                    pass

        if self._interrupt.is_set() and not seen_done:
            raise TurnInterrupted(reason=self._turn_reason or "interrupt")
        if error is not None:
            if isinstance(error, (TurnInterrupted, TurnError)):
                raise error
            raise TurnError(f"回合失败: {error}", cause=error) from error

    async def inject(self, items: Sequence[ContextItem]) -> None:
        """运行中注入，**永不打断**：排队到下一个安全点物化成独立 user 消息。"""
        if isinstance(items, ContextItem):
            items = [items]
        self.context_channel.queue(list(items))

    async def interrupt(self, *, reason: str = "") -> bool:
        """取消当前回合（模型流 / 工具执行 / 本回合托管任务）。"""
        if not self._signal_preempt(reason):
            return False
        if self._turn_scope is not None:
            await self._turn_scope.cancel_all(timeout=5.0)
        return True

    def _signal_preempt(self, reason: str) -> bool:
        """同步打断当前回合：``interrupt()`` 与调度器抢占共用这一条路径。

        返回是否真的有回合被打断。执行位被维护操作（``apply`` / ``reset_dialog`` …）
        占着时 ``_current_turn_id`` 是 ``None``，此时抢占者只会排队 —— 不会因为
        "抢不到"而报错。
        """
        if self._current_turn_id is None:
            return False
        self._turn_reason = reason
        self._interrupt.set()
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
        return True

    # ------------------------------------------------------------------
    # apply / reset
    # ------------------------------------------------------------------

    async def apply(
        self,
        spec: AgentSpec,
        *,
        force: bool = False,
    ) -> ApplyResult:
        """按 spec 差量替换部件（排队到当前回合之后）。

        ``force=True`` 即使 spec 指纹没有变化也重建运行时（调用方侧的模型构建参数
        变了，例如思考强度；spec 本身仍是同一个标签）。
        """
        self._assert_maintenance_allowed()
        await self._scheduler.acquire(priority=self.priority, tag="apply")
        try:
            if self._closed:
                raise TurnError("agent 已 close，不能 apply()")
            return await self._apply_locked(spec, force=force)
        finally:
            self._scheduler.release()

    async def _apply_locked(self, spec: AgentSpec, *, force: bool = False) -> ApplyResult:
        """真正的 apply 实现；调用方必须保证没有并发回合。"""
        if spec.id != self.spec.id:
            raise SpecError(f"Agent 身份不可变: {self.spec.id!r} -> {spec.id!r}")
        if spec.inbox_dir != self.spec.inbox_dir:
            raise SpecError("AgentSpec.inbox_dir 是身份的一部分，不能通过 apply 改变")

        old = self.spec
        old_parts = old.part_fingerprints()
        new_parts = spec.part_fingerprints()
        changed = tuple(k for k in new_parts if old_parts.get(k) != new_parts[k])
        if not changed and not force:
            return ApplyResult(
                changed=(), skipped=tuple(new_parts), fingerprint=spec.fingerprint(), epoch=self._epoch
            )

        failed: list[tuple[str, str]] = []
        skipped = tuple(k for k in new_parts if k not in changed)

        # 全有或全无：只有 spec + realm + 运行时都成功，才提交新 spec。
        # 否则 self.spec 与 graph 会分叉（下一回合指纹相同 → 永远用旧 graph）。
        if "realms" in changed:
            try:
                self.realms.attach_specs(spec.realms)
                # 调用方可能在 start() 之后才注册 realm 工厂（MCP/插件式发现），
                # apply 时同步一次，避免"新 realm 找不到工厂"
                self.realms.factories.update(self.realm_factories)
                # realm 的运行时上下文跟着新 spec 走（workspace root 可能变了）
                self.realms.configure(
                    agent_spec=spec,
                    workspace_root=spec.effective_workspace_root,
                    artifacts=self.artifacts,
                )
                # 先停旧 realm：它的挂载/后台任务必须先撤掉，否则新 realm 挂同一前缀
                # 会 MountConflictError，而且旧挂载会变成指向旧实例的幽灵投影。
                await self.realms.stop_all()
                self.realms.assemble(spec.realms)
                await self.realms.start_all()
            except Exception as exc:  # noqa: BLE001
                failed.append(("realms", f"{type(exc).__name__}: {exc}"))
                log.error("apply(realm) 失败，回滚到旧 realm: %s", exc, exc_info=exc)
                await self._rollback_realms(old)
                return ApplyResult(
                    changed=(),
                    skipped=skipped,
                    failed=tuple(failed),
                    fingerprint=self.spec.fingerprint(),
                    epoch=self._epoch,
                    forced=force,
                )

        try:
            parts = await self._prepare_runtime(spec)
        except Exception as exc:  # noqa: BLE001
            failed.append(("runtime", f"{type(exc).__name__}: {exc}"))
            log.error("apply(runtime) 失败，沿用旧运行时: %s", exc, exc_info=exc)
            if "realms" in changed:
                # 新 realm 已经启动；回滚后旧运行时必须重新绑回旧 realm 实例。
                await self._rollback_realms(old)
            return ApplyResult(
                changed=(),
                skipped=skipped,
                failed=tuple(failed),
                fingerprint=self.spec.fingerprint(),
                epoch=self._epoch,
                forced=force,
            )

        self._commit_runtime(parts)
        return ApplyResult(
            changed=changed,
            skipped=skipped,
            failed=tuple(failed),
            fingerprint=self.spec.fingerprint(),
            epoch=self._epoch,
            forced=force,
        )

    async def _rollback_realms(self, old: AgentSpec) -> None:
        """把 realm 恢复到旧 spec；成功后旧运行时重新绑定到新实例。"""
        try:
            await self.realms.stop_all()
            self.realms.attach_specs(old.realms)
            self.realms.factories.update(self.realm_factories)
            self.realms.configure(
                agent_spec=old,
                workspace_root=old.effective_workspace_root,
                artifacts=self.artifacts,
            )
            self.realms.assemble(old.realms)
            await self.realms.start_all()
        except Exception as exc:  # noqa: BLE001 - 回滚也失败：标 failed 但继续
            log.error("apply(realm) 回滚失败: %s", exc, exc_info=exc)
            return
        # assemble() 会创建全新的旧 realm 实例：旧 graph 里的工具对象已经指向
        # 被 stop 的实例，必须重建一次运行时。
        try:
            self._commit_runtime(await self._prepare_runtime(old))
        except Exception as exc:  # noqa: BLE001
            log.error("apply 回滚后重建旧运行时失败: %s", exc, exc_info=exc)

    def _assert_maintenance_allowed(self) -> None:
        """维护操作（refresh/reset）不能从当前回合内部重入，否则会自锁。"""
        if self._turn_task is not None and asyncio.current_task() is self._turn_task:
            raise TurnError("不能在当前回合内部调用 refresh_realms()/reset_dialog()")

    async def refresh_realms(self, *, force: bool = True) -> bool:
        """调用方主动触发 realm 刷新（回合边界之外）；返回是否真的刷新过。

        与回合边界自动 reconcile 的区别：这里由**调用方**决定时机。典型场景是调用方
        刚做完一件它自己知道真相源变了的事（装了技能、另一个进程写了记忆库、MCP
        服务器上线），不想等到下一次 ``run()``。

        ``force=True``（默认）先对所有支持 ``mark_dirty()`` 的 realm 置脏 —— 调用方
        已经知道变了，不必等 mtime/revision 比对；``force=False`` 只刷新自报脏的。

        工具 schema / system prompt（技能清单、realm prompt 段）变了会重建运行时，
        ``CacheEpoch`` 随之 +1，已经进上下文的旧 system 消息由中间件按 epoch 换新。
        失败不静默：单个 realm 刷新失败只让它降级（``status().realms[*].error``）。
        """
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 refresh_realms()")
        self._assert_maintenance_allowed()
        # 与回合并发地 stop/start realm 会让运行中的 graph 指向被停实例；排队。
        await self._scheduler.acquire(priority=self.priority, tag="refresh_realms")
        try:
            if self._closed:
                raise TurnError("agent 已 close，不能 refresh_realms()")
            if force:
                self.realms.mark_all_dirty()
            changed = await self.realms.refresh()
            if changed:
                await self._build_runtime()
                log.info("调用方触发 realm 刷新: epoch=%s", self._epoch.value)
            return changed
        finally:
            self._scheduler.release()

    async def set_realm_enabled(self, realm_id: str, enabled: bool) -> bool:
        """开/关一个已装配的 realm，并重建运行时（工具 / prompt / 中间件同步变化）。

        返回**调用后是否在运行**：``False`` 可能是「本来就关着」「刚被关掉」，
        也可能是启动失败（§C5 降级，原因见 ``status().realms[*].error``）。
        未装配的 realm 抛 ``RealmNotFoundError`` —— 装配集合由 spec 决定，
        运行期不改（``--no-memory`` 这类启动开关要重启 CLI 才生效）。

        ``stop()`` 撤掉该 realm 的工具、挂载、prompt 段与 ``ctx.spawn`` 的后台任务；
        ``start()`` 复用同一个实例、重新建 ``RealmContext``（状态从 state 文件读回）。
        """
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 set_realm_enabled()")
        self._assert_maintenance_allowed()
        # 与回合并发地 stop/start realm 会让运行中的 graph 指向被停实例；排队。
        await self._scheduler.acquire(priority=self.priority, tag="set_realm_enabled")
        try:
            if self._closed:
                raise TurnError("agent 已 close，不能 set_realm_enabled()")
            record = self.realms.record(realm_id)
            if (record.state == "running") == enabled:
                return record.state == "running"
            await self.realms.set_enabled(realm_id, enabled)
            try:
                await self._build_runtime()
            except Exception as exc:  # noqa: BLE001 - 回滚 realm 状态，避免 graph 与实例分叉
                log.error(
                    "切换 realm 后重建运行时失败，回滚: %s: %s", realm_id, exc, exc_info=exc
                )
                try:
                    await self.realms.set_enabled(realm_id, not enabled)
                    await self._build_runtime()
                except Exception as exc2:  # noqa: BLE001 - 回滚也失败：保留原异常
                    log.error("回滚 realm 状态后重建运行时仍失败: %s", exc2, exc_info=exc2)
                raise
            record = self.realms.record(realm_id)
            log.info(
                "realm %s %s（状态 %s，epoch=%s）",
                realm_id,
                "启用" if enabled else "停用",
                record.state,
                self._epoch.value,
            )
            return record.state == "running"
        finally:
            self._scheduler.release()

    async def reset_dialog(self) -> None:
        """清空对话 + 重写 system 段（不调模型）。

        ``state_scope="conversation"`` 的 realm 状态随对话一起重置（v1.1）。
        """
        if self._graph is None:
            raise TurnError("agent 未 start()")
        self._assert_maintenance_allowed()
        await self._scheduler.acquire(priority=self.priority, tag="reset_dialog")
        try:
            if self._closed:
                raise TurnError("agent 已 close，不能 reset_dialog()")
            prompt = self._prompt.text if getattr(self, "_prompt", None) is not None else ""
            await self.checkpoint.reset_dialog(self._graph, self._thread_config(), prompt)
            await self.realms.reset_conversation_state()
            reset_artifacts = getattr(self.artifacts, "reset", None)
            if callable(reset_artifacts):
                reset_artifacts()
        finally:
            self._scheduler.release()

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> AgentStatus:
        return AgentStatus(
            agent_id=self.spec.id,
            running=self._current_turn_id is not None,
            turn_id=self._current_turn_id,
            queue_depth=self._scheduler.queue_depth(),
            spec_fingerprint=self.spec.fingerprint(),
            epoch=self._epoch,
            realms=tuple(self.realms.status()),
            messages=self._message_count,
            pending_context=self.context_channel.pending_count(),
            started=self._started,
            closed=self._closed,
        )


__all__ = ["LioriumAgent"]
