"""Realm 装配 / 生命周期 / 冲突检测。

计划依据：

- §C2 可组合 = ``requires`` 依赖（拓扑排序，成环报错）+ 声明序叠加；不做嵌套
- §C3 工具名冲突必须报错（``failed``，带冲突详情），不允许静默跳过
- §C4 生命周期由库编排：先停旧 → 起新 → 失败回滚；后台任务必须走 ``ctx.spawn``
- §C5 realm 起不来 ⇒ 降级但可用（该 realm 标 ``failed``、移除其工具与节点），
  不是整体 not ready
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from liorium.core.errors import (
    RealmDependencyError,
    RealmError,
    RealmNotFoundError,
    RealmStartError,
    ToolNameConflictError,
)
from liorium.core.log import get_logger
from liorium.core.types import ContextItem, Event
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.node import VfsMount

from .base import JsonStateStore, Realm, RealmContext, RealmRecord, RealmTaskScope, TurnInfo

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from ..context import ContextChannel

RealmFactory = Callable[[Any], Realm]
"""``RealmSpec -> Realm 实例``。"""

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")

_DUE_KEY = "__context_due__"
"""第五拍倒计时在 realm 自己 state 文件里的键（双下划线前缀 = 框架保留键）。"""


def _safe_state_key(value: str) -> str:
    """把 thread/conversation id 变成安全的文件名片段（不静默丢字符，冲突时可见）。"""
    text = _UNSAFE_ID.sub("_", str(value or "").strip()) or "main"
    return text[:80]


class RealmManager:
    """一个 Agent 的全部 realm。

    **实例作用域**（2026-11-08，见 ``docs/design/realm-scope.md``）：realm 声明
    ``instance_scope`` 决定"一棵 Agent 树里有几份实例"。

    - ``"agent"``：本 manager 用工厂新建一份，状态落在自己的 ``state_dir`` 下；
    - ``"tree"``：**借用**树根 manager 已有的那条 ``RealmRecord``（同一实例、同一状态、
      同一挂载），只贡献 ``tools()``；挂载/提示词/中间件/周期上下文都归所有者。

    由此得到一个结构性保证：同一棵 Agent 树里，每个 tree realm 恰好一个实例、一个状态
    文件、一次挂载 —— 「两套状态抢同一路径」不再可能发生。
    """

    def __init__(
        self,
        *,
        agent_id: str,
        vfs: VirtualFileSystem,
        state_dir: Path,
        factories: dict[str, RealmFactory] | None = None,
        on_turn_event: Callable[[Event], None] | None = None,
        conversation_id: str = "main",
        context_channel: "ContextChannel | None" = None,
        parent: "RealmManager | None" = None,
        agent_spec: Any = None,
        workspace_root: Path | None = None,
        artifacts: Any = None,
    ) -> None:
        self.agent_id = agent_id
        self.vfs = vfs
        self.state_dir = Path(state_dir)
        self.factories: dict[str, RealmFactory] = dict(factories or {})
        self.conversation_id = str(conversation_id or "main")
        """``state_scope="conversation"`` 的 realm 用它给状态分桶（v1.1）。"""
        self._context_channel = context_channel
        """第五拍的落点（``ctx.inject`` 用）；未装配为 ``None``，此时注入会报错而非静默丢。"""
        self._parent = parent
        """树根 manager（子 Agent 才有）：``instance_scope="tree"`` 的 realm 从它借用。"""
        self._agent_spec = agent_spec
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._artifacts = artifacts
        self._records: dict[str, RealmRecord] = {}
        self._order: list[str] = []
        self._borrowed: set[str] = set()
        """本 manager 借用（而不是自己启动）的 realm id。"""
        self._on_turn_event = on_turn_event
        self.log = get_logger(f"realm.manager.{agent_id}")

    def configure(
        self,
        *,
        agent_spec: Any = None,
        workspace_root: Path | None = None,
        artifacts: Any = None,
    ) -> None:
        """绑定运行期上下文（Agent 在 ``start()`` / ``apply()`` 时调用）。

        ``RealmContext`` 里这几个字段不是序列化配置（它们随实例走），所以不进
        ``RealmSpec.params``：params 参与指纹，绝对路径会污染它。
        """
        self._agent_spec = agent_spec
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._artifacts = artifacts


    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------

    def assemble(self, specs: Iterable[Any]) -> None:
        """按 ``AgentSpec.realms`` 建实例并算启动顺序（不调 ``start``）。

        ``instance_scope="tree"`` 且树根已装配同名 realm ⇒ **借用**那条记录（不调工厂、
        不重新 start）：同一实例、同一状态文件、同一次挂载；子 Agent 只是多一套工具引用。
        """
        spec_list = list(specs)
        ids = {s.id for s in spec_list}
        shared = self._shared_records()
        records: dict[str, RealmRecord] = {}
        borrowed: set[str] = set()
        for spec in spec_list:
            owner = shared.get(spec.id)
            if owner is not None:
                records[spec.id] = owner
                borrowed.add(spec.id)
                self._warn_borrow_params(spec)
                continue
            factory = self.factories.get(spec.id)
            if factory is None:
                raise RealmNotFoundError(
                    f"realm {spec.id!r} 没有工厂；已注册: {sorted(self.factories)}"
                )
            instance = factory(spec)
            if not getattr(instance, "id", ""):
                instance.id = spec.id
            if instance.id != spec.id:
                raise RealmError(
                    f"realm 工厂返回的 id ({instance.id!r}) 与 spec ({spec.id!r}) 不一致"
                )
            declared = tuple(getattr(instance, "requires", ()) or ())
            for dep in declared:
                if dep not in ids:
                    raise RealmDependencyError(
                        f"realm {spec.id!r} 依赖 {dep!r}，但它不在 spec.realms 里"
                    )
            records[spec.id] = RealmRecord(spec_id=spec.id, instance=instance)

        self._order = self._toposort(records)
        self._records = records
        self._borrowed = borrowed
        # 装配即登记 spec，保证 ctx.params 不依赖调用方是否先 attach_specs()
        self.attach_specs(spec_list)

    def _shared_records(self) -> dict[str, RealmRecord]:
        """树根那边可以借用的 realm（只挑 ``instance_scope="tree"`` 的）。"""
        if self._parent is None:
            return {}
        return {
            rid: record
            for rid, record in self._parent._records.items()  # noqa: SLF001 - 同一实现
            if getattr(record.instance, "instance_scope", "agent") == "tree"
        }

    def _warn_borrow_params(self, spec: Any) -> None:
        """子 Agent 的 realm 参数对树级实例无效：不一致就说出来，别让配置假装生效。"""
        if self._parent is None:  # pragma: no cover - 没有父就没有借用
            return
        owner_params = self._parent._spec_params(spec.id)  # noqa: SLF001
        if dict(spec.params) != owner_params:
            self.log.warning(
                "realm %s 是树级共享实例（instance_scope=\"tree\"）：子 Agent 的 params 被忽略"
                "（生效的是根 Agent 的 %r，收到 %r）",
                spec.id,
                owner_params,
                dict(spec.params),
            )


    @staticmethod
    def _toposort(records: dict[str, RealmRecord]) -> list[str]:
        """``requires`` 拓扑排序；成环抛错。同层按声明序（dict 插入序）。"""
        visited: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成
        order: list[str] = []

        def visit(node: str, chain: tuple[str, ...]) -> None:
            state = visited.get(node, 0)
            if state == 2:
                return
            if state == 1:
                cycle = " -> ".join([*chain, node])
                raise RealmDependencyError(f"realm requires 成环: {cycle}")
            visited[node] = 1
            record = records[node]
            declared = tuple(getattr(record.instance, "requires", ()) or ())
            for dep in declared:
                if dep not in records:
                    raise RealmDependencyError(
                        f"realm {node!r} 依赖 {dep!r}，但它不在 spec.realms 里"
                    )
                visit(dep, (*chain, node))
            visited[node] = 2
            order.append(node)

        for rid in records:
            visit(rid, ())
        return order

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        for rid in self._order:
            if rid in self._borrowed:  # 树级实例归根 Agent 管
                continue
            await self.start(rid)

    async def start(self, realm_id: str) -> None:
        record = self._record(realm_id)
        if realm_id in self._borrowed:
            return
        if record.state == "running":
            return
        instance = record.instance
        record.state = "starting"
        try:
            ctx = RealmContext(
                realm_id=realm_id,
                vfs=self.vfs,
                state=JsonStateStore(self._state_path(realm_id, instance), realm_id=realm_id),
                params=dict(self._spec_params(realm_id)),
                agent_id=self.agent_id,
                inbox_dir=self.state_dir.parent,
                workspace_root=self._workspace_root,
                artifacts=self._artifacts,
                agent_spec=self._agent_spec,
                conversation_id=self.conversation_id,
                tasks=RealmTaskScope(log_name=f"realm.{realm_id}"),
                context=self._context_channel,
            )
            ctx.state.load()
            record.context = ctx
            record.context_due = None  # 新实例：相位从 state 重新读回
            await instance.start(ctx)
            self._assert_mount_scope(realm_id, instance)
            self._mounts_for(realm_id, instance)
            record.tools = list(instance.tools() or [])
            self._assert_tool_conflicts()
            record.state = "running"
            record.error = None
            record.started_at = ctx.state.path.stat().st_mtime if ctx.state.path.exists() else None
            # 启动期间写的状态立刻落盘：realm 状态的生命周期比对话长，不能等 stop 才写
            ctx.state.flush()
            self.log.info("realm 启动: %s（工具 %d 个）", realm_id, len(record.tools))
        except Exception as exc:  # noqa: BLE001 - 故意兜住并降级（§C5）
            record.state = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
            record.tools = []
            self.log.error("realm 启动失败（降级但可用）: %s: %s", realm_id, exc, exc_info=exc)
            await self._safe_stop_context(record)
            # 回滚它留下的挂载（并清空记账，避免显示/回滚指向已卸的挂载）
            self._unmount_instance(instance)

    async def stop(self, realm_id: str) -> None:
        record = self._record(realm_id)
        if realm_id in self._borrowed:
            raise RealmError(
                f"realm {realm_id!r} 是树级共享实例（instance_scope=\"tree\"）："
                "只能由根 Agent 停止"
            )
        if record.state not in ("running", "failed"):
            return
        try:
            if record.context is not None:
                await record.instance.stop(record.context)
        except Exception as exc:  # noqa: BLE001
            self.log.error("realm 停止失败: %s: %s", realm_id, exc, exc_info=exc)
        finally:
            await self._safe_stop_context(record)
            self._unmount_instance(record.instance)
            record.state = "stopped"
            record.tools = []

    async def stop_all(self) -> None:
        for rid in reversed(self._order):
            if rid in self._borrowed:  # 借用来的实例不归本 Agent 停
                continue
            await self.stop(rid)

    async def restart(self, realm_id: str, *, spec: Any = None) -> None:
        """热重载单个 realm：先停旧 → 起新 → 失败时保持 ``failed``。"""
        if realm_id in self._borrowed:
            raise RealmError(
                f"realm {realm_id!r} 是树级共享实例（instance_scope=\"tree\"）：只能由根 Agent 重载"
            )
        await self.stop(realm_id)
        if spec is not None:
            record = self._record(realm_id)
            record.instance = self._records[realm_id].instance = self.factories[realm_id](spec)
        await self.start(realm_id)

    async def set_enabled(self, realm_id: str, enabled: bool) -> bool:
        """开/关一个**已装配**的 realm；返回状态是否真的变了。

        只动这一个 realm：``stop()`` 撤工具、挂载、后台任务与 prompt 段（记录仍在），
        ``start()`` 复用同一个实例并重新建 ``RealmContext``（状态从 state 文件读回）。
        装配集合（``AgentSpec.realms``）不变，所以 ``status()`` 里它显示 ``stopped``
        而不是消失；宿主需要在调用后重建运行时（``LioriumAgent.set_realm_enabled()``）。

        ``instance_scope="tree"`` 的 realm 归根 Agent：子 Agent 调用会抛 ``RealmError``。
        """
        if realm_id in self._borrowed:
            raise RealmError(
                f"realm {realm_id!r} 是树级共享实例（instance_scope=\"tree\"）："
                "请在根 Agent 上开关"
            )
        record = self._record(realm_id)
        if (record.state == "running") == enabled:
            return False
        if enabled:
            await self.start(realm_id)
        else:
            await self.stop(realm_id)
        return True

    def context_cadence(self, realm_id: str) -> int:
        """该 realm 的第五拍节奏（``0`` = 不注入；``RealmSpec.params`` 覆盖类属性）。"""
        return self._cadence(self._record(realm_id))

    # ------------------------------------------------------------------
    # 运行期刷新（v1.1：realm 自报 dirty）
    # ------------------------------------------------------------------

    async def refresh(self) -> bool:
        """刷新所有自报 ``dirty()`` 的运行中 realm；返回是否重建过任何部件。

        用途（计划 §Phase 2）：MCP 发现新工具、技能安装/卸载这类**不进 spec** 的变化，
        由 realm 自己记账；Agent 在回合边界调用本方法，把新工具/新挂载/新提示词
        一并换成新的（工具 schema / prompt 变了，``CacheEpoch`` 会自然 +1）。

        realm 可以额外实现 ``async refresh()`` 做重连（MCP 用）；它先于重挂载执行。

        单个 realm 刷新失败 ⇒ 该 realm 标 ``failed`` 并撤掉它的工具与挂载（§C5 降级），
        不影响其它 realm；错误留在 ``status().realms[*].error`` 里，不静默。
        """
        changed = False
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or rid in self._borrowed:
                continue
            instance = record.instance
            dirty = getattr(instance, "dirty", None)
            if not callable(dirty):
                continue
            try:
                if not dirty():
                    continue
            except Exception as exc:  # noqa: BLE001 - dirty() 坏了按"需要刷新"处理并报错
                self.log.error("realm dirty() 失败，按需刷新处理: %s: %s", rid, exc, exc_info=exc)
            changed = True
            try:
                hook = getattr(instance, "refresh", None)
                if callable(hook):
                    await hook()
                self._remount(rid, instance)
                record.tools = list(instance.tools() or [])
                self._assert_tool_conflicts()
                self._clear_dirty(instance)
                if record.context is not None:
                    record.context.state.flush()
                self.log.info("realm 刷新: %s（工具 %d 个）", rid, len(record.tools))
            except Exception as exc:  # noqa: BLE001 - 降级但可用（§C5）
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                record.tools = []
                self._unmount_instance(instance)
                self._clear_dirty(instance)
                self.log.error("realm 刷新失败（降级但可用）: %s: %s", rid, exc, exc_info=exc)
        return changed

    def mark_all_dirty(self) -> list[str]:
        """给所有支持 ``mark_dirty()`` 的运行中 realm 置脏；返回被置脏的 id。

        用途：宿主**知道**自己刚动了真相源（装了技能、另一个进程写了记忆库），
        要比对的指纹可能相同（同 mtime）或不值得等比，于是主动置脏，再调
        ``refresh()`` 立刻生效 —— 不必等下一个回合边界。

        不支持 ``mark_dirty()`` 的 realm 跳过（它们没有"外部真相源"的概念）。
        """
        marked: list[str] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or rid in self._borrowed:
                continue
            mark = getattr(record.instance, "mark_dirty", None)
            if not callable(mark):
                continue
            try:
                mark(True)
            except TypeError:
                # BaseRealm.mark_dirty() 无参数（默认实现的空钩子）
                try:
                    mark()
                except Exception as exc:  # noqa: BLE001
                    self.log.error("realm mark_dirty 失败: %s: %s", rid, exc)
                    continue
            except Exception as exc:  # noqa: BLE001 - 单个 realm 置脏失败不影响其它
                self.log.error("realm mark_dirty 失败: %s: %s", rid, exc)
                continue
            marked.append(rid)
        return marked

    def skill_names(self) -> list[str]:
        """所有运行中 realm 想预载进 prompt「技能清单」段的条目。

        借用来的树级 realm 不计：子 Agent 的 prompt 由 TASK 覆盖，不重复拼宿主提示。
        """
        out: list[str] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or rid in self._borrowed:
                continue
            collect = getattr(record.instance, "skills", None)
            if not callable(collect):
                continue
            try:
                items = collect() or []
            except Exception as exc:  # noqa: BLE001
                self.log.error("realm skills() 失败: %s: %s", rid, exc)
                continue
            out.extend(str(item).strip() for item in items if str(item).strip())
        return out

    # ------------------------------------------------------------------
    # 第五拍：按回合节奏收集 Context（docs/design/realm-context.md）
    # ------------------------------------------------------------------

    async def context_for_turn(self, turn: TurnInfo) -> list[ContextItem]:
        """按每个运行中 realm 的节奏收集本回合要注入的 Context；**不改历史**。

        - ``context_every = 0``（默认）⇒ 一次都不问，现有 realm 零成本；
        - 每执行一个 origin 匹配的回合 ``due -= 1``，``due <= 0`` 时才问；
        - 返回空 ⇒ **不消费周期**（下一回合继续问）：清单空着不浪费额度，一有内容立刻提醒；
        - 返回非空 ⇒ 相位从**成功注入**起算并落盘；
        - 单个 realm 抛异常 ⇒ 记 error、跳过、下一回合重试，不影响其它 realm，
          也不改 ``record.state``（这是读操作，失败不该摘掉它的工具与挂载）。
        """
        out: list[ContextItem] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or record.context is None:
                continue
            if rid in self._borrowed:  # 节奏归所有者，子 Agent 不消费提醒额度
                continue
            cadence = self._cadence(record)
            if cadence <= 0 or not self._origin_matches(record, turn):
                continue
            due = await self._due(record, cadence)
            if due > 0:  # 先走一格：到期回合是"走完这一格后归零"的那一回合
                due -= 1
                record.context_due = due
            if due > 0:  # 还没到期
                continue
            try:
                items = _normalize(await _ask(record.instance, turn), rid)
            except Exception as exc:  # noqa: BLE001 - 一个 realm 坏了不影响别的
                self.log.error(
                    "realm context_items 失败，本回合跳过: %s: %s", rid, exc, exc_info=exc
                )
                continue
            if not items:  # 空 ⇒ 不消费周期
                continue
            out.extend(items)
            record.context_injections += 1
            await self._reset_due(record, cadence)
        return out

    def _option(self, record: RealmRecord, key: str, default: Any) -> Any:
        """节奏参数：``RealmSpec.params`` 优先，其次实例的类属性。

        ``record.context`` 还没建（未 ``start()``）时从登记的 spec 读，保证
        ``context_cadence()`` 在启动前后给出同一个答案。
        """
        params = getattr(record.context, "params", None)
        if params is None:
            params = self._spec_params(record.spec_id)
        params = params or {}
        if key in params:
            return params[key]
        return getattr(record.instance, key, default)

    def _cadence(self, record: RealmRecord) -> int:
        raw = self._option(record, "context_every", 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            self.log.warning(
                "realm %s 的 context_every 非法: %r（按 0 处理）", record.spec_id, raw
            )
            return 0
        return raw

    def _origin_matches(self, record: RealmRecord, turn: TurnInfo) -> bool:
        raw = self._option(record, "context_origins", ()) or ()
        try:
            origins = tuple(raw)
        except TypeError:
            self.log.warning(
                "realm %s 的 context_origins 不可迭代，按「全部」处理", record.spec_id
            )
            return True
        return not origins or turn.origin in origins

    async def _due(self, record: RealmRecord, cadence: int) -> int:
        """本回合到期没有？惰性从 state 读回相位并夹紧（节奏调小后不会等旧的大数字）。"""
        if record.context_due is None:
            stored = await record.context.state.get(_DUE_KEY)
            initial = 1 if bool(self._option(record, "context_first", False)) else cadence
            if isinstance(stored, (int, float)) and not isinstance(stored, bool):
                due = int(stored)
            else:
                due = initial
            record.context_due = min(max(due, 0), cadence)
        return record.context_due

    async def _reset_due(self, record: RealmRecord, cadence: int) -> None:
        record.context_due = cadence
        await record.context.state.set(_DUE_KEY, cadence)
        record.context.state.flush()  # 每 N 回合一次小文件原子替换，可以接受

    async def reset_conversation_state(self) -> list[str]:
        """重置 ``state_scope="conversation"`` 的 realm 状态（``reset_dialog()`` 用）。

        做法：停 → 删掉当前会话的状态文件 → 重新起。agent scope 的 realm 不动。
        """
        touched: list[str] = []
        for rid in self._order:
            record = self._records[rid]
            if rid in self._borrowed:  # 树级状态归根 Agent，子 Agent 不清别人的
                continue
            if getattr(record.instance, "state_scope", "agent") != "conversation":
                continue
            if record.state not in ("running", "failed"):
                continue
            await self.stop(rid)
            path = self._state_path(rid, record.instance)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 - 删不掉就报错，别假装干净
                self.log.error("会话状态删除失败: %s: %s", path, exc)
            await self.start(rid)
            touched.append(rid)
        return touched

    async def _safe_stop_context(self, record: RealmRecord) -> None:
        if record.context is None:
            return
        try:
            await record.context.tasks.cancel_all()
        finally:
            try:
                record.context.state.flush()
            except Exception as exc:  # noqa: BLE001
                self.log.error("realm 状态落盘失败: %s: %s", record.spec_id, exc)

    # ------------------------------------------------------------------
    # 装配产物
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        out: list[Any] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state == "running":
                out.extend(record.tools)
        return out

    def prompt_fragments(self) -> list[tuple[str, str]]:
        """``(realm_id, fragment)``，按启动顺序（借用来的树级 realm 不计）。"""
        out: list[tuple[str, str]] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or rid in self._borrowed:
                continue
            try:
                fragment = record.instance.prompt_fragment() or ""
            except Exception as exc:  # noqa: BLE001
                self.log.error("realm prompt_fragment 失败: %s: %s", rid, exc)
                continue
            if fragment.strip():
                out.append((rid, fragment))
        return out

    def realm_middlewares(self) -> list[Any]:
        """所有运行中 realm 追加的中间件（按声明序，位于基座外层；借用来的不计）。"""
        out: list[Any] = []
        for rid in self._order:
            record = self._records[rid]
            if record.state != "running" or rid in self._borrowed:
                continue
            try:
                out.extend(record.instance.middleware() or [])
            except Exception as exc:  # noqa: BLE001
                self.log.error("realm middleware 失败: %s: %s", rid, exc)
        return out

    def _assert_mount_scope(self, realm_id: str, instance: Any) -> None:
        """不变式：只有树级实例可以占 VFS 挂载点。

        挂载点是**全树共享**的（子代理看见父的文件），同一个 scheme 不可能同时指向两套
        状态。所以 ``instance_scope="agent"`` 的 realm 一旦返回挂载点，就是设计错误 ——
        启动期直接报错，而不是等到父子抢占时才炸。
        """
        scope = str(getattr(instance, "instance_scope", "agent") or "agent")
        if scope not in ("agent", "tree"):
            raise RealmError(
                f"realm {realm_id!r} 的 instance_scope 非法: {scope!r}（允许 agent / tree）"
            )
        mounts = instance.mounts() or {}
        if mounts and scope != "tree":
            raise RealmError(
                f"realm {realm_id!r} 声明 instance_scope=\"agent\" 却返回了挂载点"
                f"（{', '.join(sorted(str(k) for k in mounts))}）：挂载点是全树共享的，"
                "同一路径不可能有两份状态 ⇒ 持有挂载点的 realm 必须声明 instance_scope=\"tree\""
            )

    def _mounts_for(self, realm_id: str, instance: Any) -> None:
        mounts = instance.mounts() or {}
        applied: list[str] = []
        try:
            for prefix, mount in mounts.items():
                if not isinstance(mount, VfsMount):
                    raise RealmError(
                        f"realm {realm_id!r} 的 mounts() 返回了非 VfsMount: {type(mount).__name__}"
                    )
                self.vfs.mount(mount)
                # 记实际前缀（不是 dict key），否则 status().mounts / 回滚会错位
                applied.append(mount.prefix)
        except Exception:
            for prefix in applied:
                self.vfs.unmount(prefix)
            raise
        instance._liorium_mounts = applied

    def _unmount_instance(self, instance: Any) -> None:
        for prefix in list(getattr(instance, "_liorium_mounts", ())):
            try:
                self.vfs.unmount(prefix)
            except Exception as exc:  # noqa: BLE001 - 撤挂载失败也要继续撤其余的
                self.log.error("撤挂载失败: %s: %s", prefix, exc)
        instance._liorium_mounts = []

    def _remount(self, realm_id: str, instance: Any) -> None:
        """先撤旧挂载再按当前 ``mounts()`` 重挂（dirty 刷新用）。"""
        self._unmount_instance(instance)
        self._mounts_for(realm_id, instance)

    @staticmethod
    def _clear_dirty(instance: Any) -> None:
        clear = getattr(instance, "clear_dirty", None)
        if callable(clear):
            clear()
            return
        mark = getattr(instance, "mark_dirty", None)
        # 没有 clear_dirty 的 realm：约定 mark_dirty(bool) 也能清（能力包实现见 liorium.*）
        if callable(mark):
            try:
                mark(False)  # type: ignore[call-arg]
            except TypeError:
                pass

    def _assert_tool_conflicts(self) -> None:
        owners: dict[str, list[str]] = {}
        for rid in self._order:
            record = self._records[rid]
            if record.state not in ("running", "starting"):
                continue
            for tool in record.tools:
                name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
                if not name:
                    raise RealmError(f"realm {rid!r} 的工具没有名字: {tool!r}")
                owners.setdefault(name, []).append(rid)
        for name, rids in owners.items():
            if len(rids) > 1:
                raise ToolNameConflictError(name, rids)

    def _record(self, realm_id: str) -> RealmRecord:
        try:
            return self._records[realm_id]
        except KeyError as exc:
            raise RealmNotFoundError(f"未装配的 realm: {realm_id!r}") from exc

    def _state_path(self, realm_id: str, instance: Any) -> Path:
        """按 ``state_scope`` 决定状态文件位置（计划 §C6 / §3.3，conversation 为 v1.1）。"""
        scope = str(getattr(instance, "state_scope", "agent") or "agent")
        if scope == "agent":
            return self.state_dir / f"{realm_id}.json"
        if scope == "conversation":
            return (
                self.state_dir
                / realm_id
                / f"conversation-{_safe_state_key(self.conversation_id)}.json"
            )
        raise RealmError(
            f"realm {realm_id!r} 的 state_scope 非法: {scope!r}（允许 agent / conversation）"
        )

    def _spec_params(self, realm_id: str) -> dict[str, Any]:
        spec = getattr(self, "_specs", {}).get(realm_id)
        return dict(getattr(spec, "params", {}) or {})

    def attach_specs(self, specs: Iterable[Any]) -> None:
        self._specs = {s.id: s for s in specs}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def status(self) -> list[Any]:
        from ..status import RealmStatus

        out = []
        for rid in self._order:
            record = self._records[rid]
            mounts = tuple(getattr(record.instance, "_liorium_mounts", ()))
            out.append(
                RealmStatus(
                    id=rid,
                    state=record.state,  # type: ignore[arg-type]
                    error=record.error,
                    tools=tuple(getattr(t, "name", "") for t in record.tools),
                    mounts=mounts,
                    shared=rid in self._borrowed,
                    context_due=int(record.context_due or 0),
                    context_injections=record.context_injections,
                )
            )
        return out

    def record(self, realm_id: str) -> RealmRecord:
        return self._record(realm_id)

    def is_borrowed(self, realm_id: str) -> bool:
        """这条 realm 是不是从树根借来的树级实例（``instance_scope="tree"``）。"""
        return realm_id in self._borrowed

    @property
    def borrowed(self) -> tuple[str, ...]:
        return tuple(rid for rid in self._order if rid in self._borrowed)

    @property
    def order(self) -> list[str]:
        return list(self._order)

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["RealmFactory", "RealmManager"]


# ---------------------------------------------------------------------------
# 第五拍的纯函数助手
# ---------------------------------------------------------------------------


async def _ask(instance: Any, turn: TurnInfo) -> Any:
    """调用 realm 的 ``context_items()``（sync / async 都行；没有这个钩子就跳过）。"""
    hook = getattr(instance, "context_items", None)
    if not callable(hook):
        return None
    result = hook(turn)
    return await result if inspect.isawaitable(result) else result


def _normalize(result: Any, rid: str) -> list[ContextItem]:
    """把 ``context_items()`` 的返回值归一化成 ``ContextItem`` 列表。

    接受 ``None`` / ``str`` / ``ContextItem`` / 它们的序列；空字符串丢弃；
    其余类型抛 ``TypeError``（由调用方记日志并跳过，不静默篡改语义）。
    """
    if result is None:
        return []
    if isinstance(result, ContextItem):
        return [result]
    if isinstance(result, str):
        text = result.strip()
        return [ContextItem(source=f"realm:{rid}", text=text)] if text else []
    out: list[ContextItem] = []
    for item in result:  # 非可迭代 ⇒ TypeError ⇒ 走异常降级路径
        if item is None:
            continue
        if isinstance(item, ContextItem):
            out.append(item)
        elif isinstance(item, str):
            if item.strip():  # 空串 = 这个 realm 本回合没内容，不算错误
                out.append(ContextItem(source=f"realm:{rid}", text=item))
        else:
            raise TypeError(f"context_items() 返回了不支持的元素: {type(item).__name__}")
    return out
