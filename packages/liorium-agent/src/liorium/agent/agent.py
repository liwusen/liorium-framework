"""``LioriumAgent`` —— 一个类，三个正交维度（身份 / 关系 / 权限）。

计划依据：``__dev__/Liorium Event 事件流原生 定稿.md``。

- 身份：``id`` / 根目录 / 记忆 / checkpoint
- 关系：``parent`` / ``lifecycle_owner`` / ``accepts_external_turns``
- 权限：谁能中断 / 配置（v1 不做权限系统，保留端口）

**事件流原生**（2026-11-12 起）：Agent 是**自持 worker**——

- 外部只能 ``push()`` 投递入站条目、``stream()`` 观察全局只读事件流、
  ``inject()`` 走底层 Context 通道；
- 回合的创建、调度、取消全部由 Agent 内部（``TurnWorker``）裁决；
- 发布者永不等待消费者（``EventHub`` 每订阅者一条无界队列，``put_nowait``）。

生命周期仍是显式操作：``start`` / ``close`` / ``push`` / ``stream`` / ``apply``。
**身份不变 ⇒ 实例永不重建**，``apply()`` 只原位替换可变部件。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from liorium.core.errors import SpecError, ToolNameConflictError, TurnError
from liorium.core.log import get_logger
from liorium.core.types import ContextItem, Event, ev_input
from liorium.vfs.fs import VirtualFileSystem

from .context import ContextChannel
from .realm.base import RealmMessage, TurnInfo
from .realm.hooks import hook_middleware
from .realm.host import RealmHostPort
from .realm.manager import CURRENT_REALM_MESSAGE, RealmFactory, RealmManager
from .realm.subagent import SubagentRealm
from .runtime.hub import EventHub
from .runtime.inbox import BatchWindow, Bucket, Entry, Flag, Inbox
from .runtime.worker import TurnWorker
from .spec import AgentSpec
from .status import AgentStatus, ApplyResult, CacheEpoch

log = get_logger("liorium.agent")


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

    同一时刻只允许绑一个出口（常驻 worker 串行跑回合，压进错回合的队列就是
    "别的消息的事件串了"）。绑定冲突（理论上只会在内部不变量被破坏时出现）记日志
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
        batch_window: BatchWindow | None = None,
        env_interval: float = 30.0,
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
            # realm 主动推送的端口（2026-11-14）：`ctx.emit/push/interrupt/turn`。
            # 用工厂而不是实例：端口要绑定 realm_id，而 realm 在 start() 时才逐一起。
            host_factory=self._make_realm_host,
        )

        from .runtime.checkpoint import CheckpointStore
        from .runtime.output_store import OutputStore

        self.checkpoint = CheckpointStore(
            Path(checkpoint_path) if checkpoint_path else spec.inbox_dir / "checkpoint.db"
        )
        if self.artifacts is None:
            self.artifacts = OutputStore(root=spec.inbox_dir / "artifacts")
        # realm 的运行时上下文（workspace root / artifacts / spec）在这里定稿：
        # 不进 RealmSpec.config（config 参与指纹，绝对路径会污染它）。
        self.realms.configure(
            agent_spec=spec,
            workspace_root=spec.effective_workspace_root,
            artifacts=self.artifacts,
            aux_model_factory=lambda: self._aux_for(spec),
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
        self._message_count = 0
        self._subagent_realm: SubagentRealm | None = None
        self._artifacts_mount_owned = False
        self._warned_compact_disabled = False

        # 入站队列 / 扇出中心 / 常驻 worker（定稿 §1：worker 拥有回合）
        # ``env_interval``：``ENVIRONMENT`` 回合中途投递的最小间隔（秒，<=0 表示每次安全点都投）。
        self._inbox = Inbox(batch_window=batch_window, env_interval=env_interval)
        self._hub = EventHub()
        self._worker = TurnWorker(self, inbox=self._inbox, hub=self._hub)

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

        **不调模型**（计划 §F7）：首次引导由调用方 ``push(boot.message, id=...)``。
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
        self._worker.start()
        log.info("agent 启动: %s（realm=%s）", self.spec.id, self.realms.order)

    def _make_subagent_realm(self, realm_spec: Any) -> SubagentRealm:
        realm = SubagentRealm(parent=self, config=dict(realm_spec.config))
        self._subagent_realm = realm
        return realm

    def _make_realm_host(self, realm_id: str) -> RealmHostPort:
        """给一个 realm 建宿主端口（``RealmManager.start()`` 时逐个调用）。"""
        return RealmHostPort(self, realm_id)

    def _aux_for(self, spec: AgentSpec) -> Any:
        """realm 的 aux 模型（``ctx.model``）：优先 compact 工厂（不思考、无工具）。

        返回实例而不是缓存：「谁造谁关」归工厂；``apply()`` 换模型后下一次访问自然拿到新的。
        """
        factory = self.compact_model_factory or self.model_factory
        return factory(spec.model)

    def _hook_middleware(self, realm_id: str, instance: Any) -> Any:
        """只读钩子的适配器：没覆盖钩子的 realm 返回 ``None``（不装空转中间件）。"""
        return hook_middleware(
            instance,
            realm_id=realm_id,
            model=self.spec.model,
            turn_info=lambda: self._worker.current_turn_info,
        )

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
        if CURRENT_REALM_MESSAGE.get() is not None:
            # close() 要停 realm（等 record.send_lock），而 handler 正持着它 ⇒ 互锁
            raise TurnError("不能在 realm 消息处理（on_message）里 close() agent")
        self._closed = True
        # 收当前回合（interrupted(close)）/ 丢 pending / 关流 / 停 worker
        await self._worker.close()
        self._started = False
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
            await self._aclose_factories()

    async def _aclose_factories(self) -> None:
        """关掉工厂自带的客户端池（``ChatModelPool.aclose``）；裸函数工厂没有这个方法。

        「谁造谁关」：Agent 只调工厂的 ``aclose``，**不**对模型实例做鸭子类型关闭
        （那会关掉 langchain 模块级 ``lru_cache`` 里的共享客户端，同 base_url 的模型全废）。
        去重 + 幂等：``.chat`` / ``.compact`` 可能指向同一个 pool。
        """
        closed: set[int] = set()
        for factory in (self.model_factory, self.compact_model_factory):
            if factory is None or id(factory) in closed:
                continue
            closed.add(id(factory))
            aclose = getattr(factory, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不能拖垮 close()
                    log.warning("关闭模型工厂失败: %s", exc)

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
            # realm 自己的中间件 + 只读钩子适配器（每个 realm：middleware 在外、钩子紧随）
            realm_middlewares=self.realms.realm_middlewares(hook_factory=self._hook_middleware),
            context_channel=self.context_channel,
            on_context_materialized=self._worker.on_context_materialized,
            # 回合进行中按 env_interval 节流注入 ENVIRONMENT（不开回合、不打扰模型）。
            env_context=self._worker.pull_env_context,
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
    # 入站 / 出站（事件流原生）
    # ------------------------------------------------------------------

    def push(
        self,
        text: str,
        *,
        id: str,
        bucket: Bucket = Bucket.MAJOR,
        flags: Flag = Flag.NONE,
        origin: str = "user",
        timeout: float | None = None,
        context: Sequence[ContextItem] = (),
    ) -> None:
        """投递一条入站条目：**同步、无返回值、入队即返回**。

        - ``id`` 必填，是事件流的关联键（``push`` 没有返回值，这是唯一关联手段）；
        - ``bucket`` 只表达"何时投递"（见 :class:`Bucket`）：
          ``IMMEDIATE``/``MAJOR`` 入队即排回合，``BATCHED`` 攒满合批窗口后**开自己的
          回合**（回合在跑就排队等它结束），``ENVIRONMENT`` 永不开回合、只做 context；
        - ``flags`` 见 :class:`Flag`：``AUTO``（空闲开回合 / 忙碌注入当前回合，仅
          ``MAJOR`` 有意义，与 ``IMMEDIATE`` 互斥 ⇒ ``ValueError``）、``EXCLUSIVE``
          （本条开启的回合拒绝被 ``IMMEDIATE`` 抢占）、``DROPPABLE``（深度达
          ``QUEUE_MAX`` 时丢弃并发 ``input.dropped``）；
        - ``timeout`` 是**消费截止时间**（秒，monotonic）：条目在被物化进消息之前到期
          即作废，永不物化，发 ``input.expired``；
        - ``context`` 并入本回合的 user 消息（退回形态①）。

        节奏控制（什么时候该开口）**不属于 Core**：VTuber 场景由适配器自己的合批
        缓冲 + 开口闸门在 ``push`` 之前完成（定稿 §10）。
        """
        if self._closed:
            raise TurnError("agent 已 close，不能 push()")
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 push()")
        if not id:
            raise ValueError("push() 的 id 不能为空")

        bucket = Bucket(bucket)
        flags = Flag(flags)
        if flags & Flag.AUTO and bucket is Bucket.IMMEDIATE:
            raise ValueError("Flag.AUTO 与 Bucket.IMMEDIATE 互斥")
        items = tuple(context)

        if flags & Flag.AUTO and bucket is Bucket.MAJOR and self._worker.turn_running:
            # AUTO + 忙碌 ⇒ 注入当前回合的 context（安全点物化），不开新回合。
            self._worker.forget_delivered(id)
            self.context_channel.queue(
                [ContextItem(source=origin, text=text, meta={"input_id": id}), *items]
            )
            self._hub.publish(ev_input(id, "accepted"))
            return

        entry = Entry(
            id=id,
            text=text,
            bucket=bucket,
            flags=flags,
            origin=origin,
            timeout=timeout,
            context=items,
        )
        outcome = self._inbox.push(entry)
        self._hub.publish(ev_input(id, "accepted" if outcome == "accepted" else "dropped"))
        if outcome != "accepted":
            return
        # 同 id 复用（旧的已物化 / 已被替换）：新条目要有自己的 input.delivered。
        self._worker.forget_delivered(id)
        self._worker.wake()
        if bucket is Bucket.IMMEDIATE and self._worker.turn_running:
            self._worker.notify_immediate()

    def stream(self, *, ids: Sequence[str] | None = None) -> AsyncIterator[Event]:
        """打开一条只读事件流：**可开多个**，彼此独立，每个都是完整视图。

        - ``stream()`` 本身只构造订阅对象；真正的订阅发生在 ``__aiter__``
          （``async for`` 开始那一刻），所以 ``s = agent.stream()`` 之后不迭代不会泄漏；
        - **不回放**：从订阅时刻起的事件，没有历史、没有快照；
        - ``ids=[...]``：只产出 ``start.inputs`` 与该集合有交集的回合的帧，以及这些
          id 自己的 ``input`` 生命周期帧；
        - 订阅者慢 / 抛错 / ``aclose`` 只影响它自己；**没人订阅时 Agent 照常跑回合**。
        """
        return self._hub.subscribe(ids=ids)

    def inject(self, items: Sequence[ContextItem] | ContextItem) -> None:
        """底层 Context 通道：**永不打断**，在下一个安全点（或下一回合开始）物化。

        不经 Inbox、不参与 bucket/timeout/合批、不发 ``input`` 帧 —— 这是 realm
        内部 ``ctx.inject()`` 的宿主侧对应物、库内部注入与测试用的旁路。
        """
        if isinstance(items, ContextItem):
            items = [items]
        self.context_channel.queue(list(items))

    async def interrupt(self, *, reason: str = "user") -> bool:
        """取消当前回合（模型流 / 工具执行 / 本回合托管任务）；队列保留。"""
        return self._worker.interrupt(reason or "user")

    async def send_to_realm(
        self,
        realm_id: str,
        name: str,
        data: Mapping[str, Any] | None = None,
        *,
        origin: str = "host",
        timeout: float | None = None,
    ) -> Any:
        """把一条消息交给运行中的 realm，等它处理完并返回 handler 的返回值（2026-11-14）。

        这是**宿主 → realm** 的公共入口：外部主程序（GUI / webhook / cron）改一条待办、
        送来一条领域事件，不需要模型参与，也不需要等 50 回合的周期上下文。

        ```python
        reply = await agent.send_to_realm("todo", "todo.add", {"text": "写 demo"})
        ```

        - **立即执行**：不排队、不等当前回合结束（与正在跑的回合并发）⇒ handler 必须廉价，
          慢活自己 ``ctx.spawn``；同 realm 的消息彼此串行（``record.send_lock``）；
        - **等结果**：handler 的返回值原样返回；返回值 ``None`` 也合法（纯事件通知）；
        - realm 侧由 ``BaseRealm.on_message()`` 接收（默认为"不接受消息"，抛 ``RealmError``）；
        - 错误：未 ``start()`` / 已 ``close()`` ⇒ ``TurnError``；未装配 ⇒
          ``RealmNotFoundError``；状态不是 ``running``（被 ``/realm disable`` 或启动失败）⇒
          ``RealmError``；handler 抛的异常原样上抛；``timeout`` 到期 ⇒ ``RealmError``；
        - `instance_scope="tree"` 的 realm 由**树根那份实例**处理（子 Agent 的 record 就是
          根的那条），所以它的 ``ctx.emit`` / ``ctx.push`` 都落在根 Agent 上；
        - 消息不进事件流、不进历史、不影响 prompt / 指纹 —— 想广播就让 handler 自己
          ``ctx.emit(...)``（会落成 ``realm`` 帧）；
        - handler 里**不要**调 ``refresh_realms`` / ``set_realm_enabled`` / ``reset_dialog`` /
          ``close``（会抛 ``TurnError``：那些操作要维护队列，而消息正持着 realm 锁）。
        """
        if self._closed:
            raise TurnError("agent 已 close，不能 send_to_realm()")
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 send_to_realm()")
        message = RealmMessage(realm_id=realm_id, name=name, data=data or {}, origin=origin)
        return await self.realms.send(realm_id, message, timeout=timeout)

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

        async def _do() -> ApplyResult:
            if self._closed:
                raise TurnError("agent 已 close，不能 apply()")
            return await self._apply_locked(spec, force=force)

        return await self._worker.run_maintenance(_do, name="apply")

    async def _apply_locked(self, spec: AgentSpec, *, force: bool = False) -> ApplyResult:
        """真正的 apply 实现；调用方必须保证没有并发回合。"""
        if spec.id != self.spec.id:
            raise SpecError(f"Agent 身份不可变: {self.spec.id!r} -> {spec.id!r}")
        if spec.inbox_dir != self.spec.inbox_dir:
            raise SpecError("AgentSpec.inbox_dir 是身份的一部分，不能通过 apply 改变")

        old = self.spec
        # aux 工厂跟着**目标** spec 走：即使只换模型、不重启 realm，``ctx.model`` 也必须
        # 拿到新模型（closure 捕获 spec，而不是 self.spec —— 后者要等运行时提交后才变）。
        self.realms.configure(
            agent_spec=spec,
            workspace_root=spec.effective_workspace_root,
            artifacts=self.artifacts,
            aux_model_factory=lambda: self._aux_for(spec),
        )
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
                aux_model_factory=lambda: self._aux_for(old),
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
        if self._worker.in_turn_task():
            raise TurnError("不能在当前回合内部调用 refresh_realms()/reset_dialog()")
        if CURRENT_REALM_MESSAGE.get() is not None:
            # realm 消息 handler 持着 record.send_lock；维护操作要等维护队列 ⇒ 必然互锁
            raise TurnError(
                "不能在 realm 消息处理（on_message）里调用 refresh_realms()/"
                "set_realm_enabled()/reset_dialog()"
            )

    async def refresh_realms(self, *, force: bool = True) -> bool:
        """调用方主动触发 realm 刷新（回合边界之外）；返回是否真的刷新过。

        与回合边界自动 reconcile 的区别：这里由**调用方**决定时机。典型场景是调用方
        刚做完一件它自己知道真相源变了的事（装了技能、另一个进程写了记忆库、MCP
        服务器上线），不想等到下一次 ``push()`` 触发的回合边界。

        ``force=True``（默认）先对所有支持 ``mark_dirty()`` 的 realm 置脏 —— 调用方
        已经知道变了，不必等 mtime/revision 比对；``force=False`` 只刷新自报脏的。

        工具 schema / system prompt（技能清单、realm prompt 段）变了会重建运行时，
        ``CacheEpoch`` 随之 +1，已经进上下文的旧 system 消息由中间件按 epoch 换新。
        失败不静默：单个 realm 刷新失败只让它降级（``status().realms[*].error``）。
        """
        if not self._started or self._graph is None:
            raise TurnError("agent 未 start()，不能 refresh_realms()")
        self._assert_maintenance_allowed()

        async def _do() -> bool:
            if self._closed:
                raise TurnError("agent 已 close，不能 refresh_realms()")
            if force:
                self.realms.mark_all_dirty()
            changed = await self.realms.refresh()
            if changed:
                await self._build_runtime()
                log.info("调用方触发 realm 刷新: epoch=%s", self._epoch.value)
            return changed

        # 与回合并发地 stop/start realm 会让运行中的 graph 指向被停实例；插队执行。
        return await self._worker.run_maintenance(_do, name="refresh_realms")

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

        async def _do() -> bool:
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

        # 与回合并发地 stop/start realm 会让运行中的 graph 指向被停实例；插队执行。
        return await self._worker.run_maintenance(_do, name="set_realm_enabled")

    async def reset_dialog(self) -> None:
        """清空对话 + 重写 system 段（不调模型）。

        执行前清空 Inbox 里全部条目并逐条发 ``input.dropped`` —— 它们属于被清掉的
        对话（定稿 §5.5）。``state_scope="conversation"`` 的 realm 状态随对话一起重置。
        """
        if self._graph is None:
            raise TurnError("agent 未 start()")
        self._assert_maintenance_allowed()

        async def _do() -> None:
            if self._closed:
                raise TurnError("agent 已 close，不能 reset_dialog()")
            for entry in self._inbox.clear():
                self._hub.publish(ev_input(entry.id, "dropped"))
            prompt = self._prompt.text if getattr(self, "_prompt", None) is not None else ""
            await self.checkpoint.reset_dialog(self._graph, self._thread_config(), prompt)
            await self.realms.reset_conversation_state()
            reset_artifacts = getattr(self.artifacts, "reset", None)
            if callable(reset_artifacts):
                reset_artifacts()

        await self._worker.run_maintenance(_do, name="reset_dialog")

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> AgentStatus:
        return AgentStatus(
            agent_id=self.spec.id,
            running=self._worker.turn_running,
            turn_id=self._worker.current_turn_id,
            queue_depth=self._inbox.depth(),
            spec_fingerprint=self.spec.fingerprint(),
            epoch=self._epoch,
            realms=tuple(self.realms.status()),
            messages=self._message_count,
            pending_context=self.context_channel.pending_count(),
            pending_by_bucket=tuple(
                (bucket.name, count)
                for bucket, count in self._inbox.depth_by_bucket().items()
            ),
            started=self._started,
            closed=self._closed,
        )


__all__ = ["LioriumAgent"]
