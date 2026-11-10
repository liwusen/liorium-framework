"""``SubagentRealm`` —— 子代理也是 ``LioriumAgent``，共享父的 vfs / memory。

计划 §B1：顶层 Agent 与 Subagent 是**关系**差别，不是类别差别。
计划 §D2：Subagent 共享父的 ``VirtualFileSystem`` 实例，靠命名空间约定隔离
（子代理写 ``/subagents/<name>/``）。
计划 §E2：没有通用 ``notify()`` 通道；需要对外报事的 realm 自己暴露 ``events()``。

父工具（名字保持稳定，前端/模型侧不用改）：

- ``newSubagent`` / ``invokeSubagent`` / ``wait_for_subagent`` / ``stopSubagent`` / ``removeSubagent``
- ``updateSubagent``（2026-11-08）：改工具集，**不用删库重建**

**完成通知**（2026-11-08）：子代理进入终态（done/error/stopped）时不再只走宿主侧
``events()``，还会排一条 ``ContextItem``；``context_every = 1`` 让它在下一回合自动
以 context 出现在父模型的上下文里 —— 否则模型不主动 ``listSubagents`` 就永远不知道
子代理已经跑完了。

**状态持久化**（2026-11-08，见 ``docs/design/subagent-state.md``）：名册与正文分开落盘 ——
结构化元数据进 ``inbox/realm_state/subagents.json``（框架的 ``JsonStateStore``），
任务 / 转录 / 结果进 ``inbox/subagents/<name>/{TASK,output,finalResult}.md``。
写序固定为「先正文、后名册」：崩在中间只会状态旧一点，不会出现"done 但结果空"。
进程重启后上次仍在跑的记录标 ``interrupted``，再次 ``invokeSubagent`` 会接着它自己的
``checkpoint.db`` 继续对话。
"""

from __future__ import annotations

import asyncio
import gc
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping

from langchain_core.tools import tool

from liorium.core.errors import TurnError, TurnInterrupted
from liorium.core.log import get_logger
from liorium.core.types import ContextItem, Text
from liorium.vfs.fs import VirtualFileSystem
from liorium.vfs.node import VfsMount, VfsNode

from .base import BaseRealm, RealmContext, TurnInfo

if TYPE_CHECKING:  # pragma: no cover
    from ..agent import LioriumAgent

log = get_logger("liorium.realm.subagents")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

DEFAULT_SUBAGENT_TOOLSET = ("read", "search", "find", "write", "edit")
"""子代理默认工具集：能读、能搜、能写文件；不给 execute（v1 保守）。"""

_STATE_VERSION = 1
"""名册格式版本（写进 state 文件，便于将来迁移）。"""

_CHILDREN_KEY = "children"
"""名册在 ``realm_state/subagents.json`` 里的键。"""

_TASK_FILE = "TASK.md"
_OUTPUT_FILE = "output.md"
_RESULT_FILE = "finalResult.md"

_RESTART_ERROR = "进程重启，该回合已中断（可再次 invokeSubagent 接着对话继续）"


def _as_float(value: Any) -> float | None:
    """把 state 里的时间戳夹成 ``float | None``（损坏值不静默当 0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _write_text(path: Path, text: str) -> None:
    """写正文；空正文 ⇒ 删掉旧文件（避免上一次的结果/转录残留）。"""
    if not text:
        path.unlink(missing_ok=True)
        return
    path.write_text(text, encoding="utf-8")


@dataclass
class ChildRecord:
    name: str
    agent_id: str
    inbox: Path
    toolsets: tuple[str, ...] = DEFAULT_SUBAGENT_TOOLSET
    system_prompt: str = ""
    model: str | None = None
    status: str = "idle"
    """idle / running / done / error / stopped / interrupted"""
    error: str | None = None
    output: str = ""
    final_result: str = ""
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    task: "asyncio.Task[Any] | None" = None
    agent: "LioriumAgent | None" = None

    def overview(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "toolsets": list(self.toolsets),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_chars": len(self.output),
        }

    # -- 名册 ↔ 记录（只带结构化字段；正文在文件里）-----------------------

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "toolsets": list(self.toolsets),
            "model": self.model,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any], *, inbox: Path) -> "ChildRecord":
        """从名册条目重建记录（正文由调用方回填）。坏条目抛 ``ValueError``。"""
        name = str(meta.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"非法子代理名: {name!r}")
        raw_toolsets = meta.get("toolsets") or DEFAULT_SUBAGENT_TOOLSET
        if not isinstance(raw_toolsets, (list, tuple)):
            raw_toolsets = DEFAULT_SUBAGENT_TOOLSET
        toolsets = tuple(str(item).strip() for item in raw_toolsets if str(item).strip())
        model = meta.get("model")
        error = meta.get("error")
        return cls(
            name=name,
            agent_id=str(meta.get("agent_id") or f"subagent-{name}"),
            inbox=inbox,
            toolsets=toolsets or DEFAULT_SUBAGENT_TOOLSET,
            model=str(model) if model else None,
            status=str(meta.get("status") or "idle"),
            error=None if error is None else str(error),
            created_at=_as_float(meta.get("created_at")),
            started_at=_as_float(meta.get("started_at")),
            finished_at=_as_float(meta.get("finished_at")),
        )


class SubagentRealm(BaseRealm):
    """把一个 Agent 变成"能 spawn 子代理"的 Agent。"""

    id = "subagents"
    requires: list[str] = []
    state_scope = "agent"
    instance_scope = "tree"
    """子代理命名空间（``/subagents`` 挂载 + 子 Agent 生命周期）整棵树只有一份。"""

    context_every = 1
    """每回合都给一次注入机会：有子代理转场就提醒，没有就返回空（不消费周期）。"""

    def __init__(self, *, parent: "LioriumAgent", params: dict[str, Any] | None = None) -> None:
        super().__init__()  # 提供 self.ctx / self.log（start() 之前也能安全读）
        self.parent = parent
        self.params = dict(params or {})
        self.children: dict[str, ChildRecord] = {}
        self._vfs = VirtualFileSystem(owner_id=f"{parent.spec.id}:subagents")
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._pending: list[ContextItem] = []
        """待注入的转场提醒（done/error/stopped）；下一回合由 ``context_items`` 取走。"""
        self._default_toolset = tuple(
            self.params.get("default_toolsets") or DEFAULT_SUBAGENT_TOOLSET
        )
        self._max_depth = int(self.params.get("max_depth", parent.spec.limits.max_subagent_depth))
        # 0 = 不限制（按宿主/用户要求允许任意 N 个并行子代理）。
        self._max_children = int(self.params.get("max_children", 0) or 0)
        # 挂载点 /subagents 直接挂这个 VFS：它的根就是子代理命名空间，
        # 所以每个子代理目录直接是 root.children[name]（不要再套一层 "subagents" 目录）。
        self._vfs.root.description = "子代理命名空间"

    # ------------------------------------------------------------------
    # Realm 协议
    # ------------------------------------------------------------------

    def mounts(self) -> dict[str, VfsMount]:
        return {"/subagents": VfsMount(prefix="/subagents", vfs=self._vfs, readonly=False)}

    def prompt_fragment(self) -> str:
        return (
            "## 子代理\n\n"
            "- `newSubagent(name, prompt)` 建一个子代理：它是同类的 Agent，只听你给的任务，"
            "产出写在 `subagents://<name>/finalResult.md`。\n"
            "- `invokeSubagent(name, message)` 启动它（异步，立刻返回）；之后可以用 "
            "`wait_for_subagent(name)` 等一个，或 `wait_for_subagents(names)` 一次等多个，"
            "也可以先干别的。\n"
            "- 多个子代理可以同时运行（没有数量上限）；给它们互不依赖的任务才能真并行。\n"
            "- `stopSubagent(name)` 叫停，`removeSubagent(name)` 删除，"
            "`updateSubagent(name, toolsets)` 改它的工具集（不用删库重建，对话历史保留）。\n"
            "- 给子代理的任务要**自包含**：它看不到你的对话历史，只知道你写在 `prompt` 里的内容。\n"
            "- 任务 / 转录 / 结果都在 `subagents://<name>/` 下，**进程重启后仍在**；"
            "同名子代理再次 invoke 会接着它上次的对话继续。\n"
            "- 适用条件：任务能独立完成、且过程不需要你逐句看到；简单的读文件/搜索别开子代理。\n"
            "- 子代理跑完（完成 / 失败 / 被叫停）会自动给你一条 `context` 提示，不用反复 "
            "`listSubagents` 查进度。"
        )

    def context_items(self, turn: TurnInfo) -> list[ContextItem]:
        """第五拍：把"上次之后有子代理转了状态"这件事告诉模型（取走即清空）。

        返回空列表 ⇒ 框架**不消费**注入周期（``RealmManager.context_for_turn``），
        所以没有转场时它永远不会占额度，有转场时下一回合立刻送达。
        """
        if not self._pending:
            return []
        items, self._pending = self._pending, []
        return items

    def _note(self, record: ChildRecord) -> None:
        """给一次终态转场排一条提醒（最多保留最近 20 条，避免刷屏）。"""
        if record.status == "done":
            text = f"子代理 {record.name} 已完成，结果在 subagents://{record.name}/finalResult.md"
        elif record.status == "error":
            text = f"子代理 {record.name} 失败了：{record.error or '未知错误'}"
        elif record.status == "stopped":
            text = f"子代理 {record.name} 被叫停（已产生的输出保留）"
        else:
            return
        self._pending.append(ContextItem(source=f"subagent:{record.name}", text=text))
        if len(self._pending) > 20:
            del self._pending[: len(self._pending) - 20]

    async def start(self, ctx: RealmContext) -> None:
        self.ctx = ctx
        await self._restore()

    async def stop(self, ctx: RealmContext) -> None:
        try:
            await self.shutdown()
        finally:
            self.ctx = None

    async def shutdown(self) -> None:
        # 先取消并**等**所有回合真正收尾，再关 Agent：取消是异步投递的，提前关会让
        # 正在 _ensure_agent 里的回合留下一个开着 checkpoint 的孤儿（Windows 上删不掉）。
        tasks = [
            record.task
            for record in self.children.values()
            if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
        for record in list(self.children.values()):
            agent = record.agent
            if agent is not None:
                record.agent = None
                await self._close_agent(agent)
        # 只在确实还有记录时回写：close() 会先调 shutdown() 再由 stop_all() 调一次，
        # 第二次面对空字典若也回写，就会把磁盘上的名册擦掉。
        if self.children:
            for record in self.children.values():
                await self._safe_sync(record)
            self.children.clear()
        self._subscribers.clear()
        self._pending.clear()

    @staticmethod
    async def _cancel_round(record: ChildRecord, *, timeout: float = 5.0) -> None:
        """取消并**等**这一回合真正收尾。

        取消是异步投递的：只 ``task.cancel()`` 就去关 Agent / 删目录，会撞上还开着的
        ``checkpoint.db``（Windows 上是 ``WinError 32``）。
        """
        task = record.task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait([task], timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 收尾失败也要继续关
            log.error("等待子代理回合结束失败: %s: %s", record.name, exc)

    @staticmethod
    async def _close_agent(agent: Any) -> None:
        """关掉子代理 Agent，且**不让取消打断关连接**。

        ``LioriumAgent.close()`` 里有关 checkpoint 的 await；如果本任务正被取消
        （stopSubagent / 宿主关闭），那个 await 会被打断，sqlite 句柄留在进程里
        ——在 Windows 上子代理目录就再也删不掉了。``shield`` 让关闭跑完。
        """
        try:
            await asyncio.shield(agent.close())
        except Exception as exc:  # noqa: BLE001 - 关闭失败只报错
            log.error("关闭子代理 Agent 失败: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # 状态持久化（名册 + 正文）
    # ------------------------------------------------------------------

    def _child_inbox(self, name: str) -> Path:
        return self.parent.spec.inbox_dir / "subagents" / name

    async def _restore(self) -> None:
        """从 ``realm_state/subagents.json`` 恢复名册，并把正文回填进记录。"""
        # 同一个实例可能被 stop→start（/realm disable→enable）复用：清掉上一轮留下的
        # 投影，名册才是权威（否则已删的子代理节点会一直挂在 subagents:// 下）。
        self._vfs.root.children.clear()
        raw = await self.state.get(_CHILDREN_KEY, [])
        if raw in (None, [], {}):
            return
        if not isinstance(raw, list):
            self.log.warning(
                "subagents 名册格式非法（期望列表，收到 %s），按空名册启动", type(raw).__name__
            )
            return
        interrupted: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                self.log.warning("跳过非对象的子代理条目: %r", item)
                continue
            name = str(item.get("name") or "")
            try:
                record = ChildRecord.from_meta(item, inbox=self._child_inbox(name))
            except ValueError as exc:
                self.log.warning("跳过坏的子代理条目: %s", exc)
                continue
            record.system_prompt = self._read_text(record.inbox / _TASK_FILE)
            record.output = self._read_text(record.inbox / _OUTPUT_FILE)
            record.final_result = self._read_text(record.inbox / _RESULT_FILE)
            if record.status in ("running", "starting"):
                # 上个进程没来得及收尾：回合已经断了，状态要诚实。
                record.status = "interrupted"
                record.error = record.error or _RESTART_ERROR
                record.finished_at = record.finished_at or time.time()
                interrupted.append(record.name)
            self.children[record.name] = record
            self._mount_child(record)
        if not self.children:
            return
        self.log.info(
            "恢复 %d 个子代理%s",
            len(self.children),
            f"（{', '.join(interrupted)} 上次在运行，已标 interrupted）" if interrupted else "",
        )
        if interrupted:
            await self._persist()

    async def _persist(self) -> None:
        """回写名册（原子替换）。正文不在里面，所以这个文件始终很小。"""
        async with self._lock:
            await self.save(
                version=_STATE_VERSION,
                **{_CHILDREN_KEY: [record.to_meta() for record in self.children.values()]},
            )

    async def _write_texts(self, record: ChildRecord) -> None:
        """正文落盘：任务说明 / 本次转录 / 最终结果。"""
        await asyncio.to_thread(self._write_texts_sync, record)

    def _write_texts_sync(self, record: ChildRecord) -> None:
        record.inbox.mkdir(parents=True, exist_ok=True)
        _write_text(record.inbox / _TASK_FILE, record.system_prompt)
        _write_text(record.inbox / _OUTPUT_FILE, record.output)
        _write_text(record.inbox / _RESULT_FILE, record.final_result)

    async def _sync(self, record: ChildRecord) -> None:
        """一次转场的完整落盘：**先正文、后名册**（崩在中间只会状态旧一点）。"""
        await self._write_texts(record)
        await self._persist()

    async def _safe_sync(self, record: ChildRecord) -> None:
        """落盘失败只报错，不改写子代理的成败（磁盘满不该让回合变 error）。"""
        try:
            await self._sync(record)
        except Exception as exc:  # noqa: BLE001
            log.error("子代理状态落盘失败: %s: %s", record.name, exc, exc_info=exc)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            log.error("读取子代理文件失败: %s: %s", path, exc)
            return ""

    # ------------------------------------------------------------------
    # 事件（realm 自己暴露，不做全局 notify）
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """订阅子代理事件流（宿主用）。"""
        queue = self.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)

    def _publish(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢消费者丢最旧（不阻塞 agent 回合）
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        realm = self

        @tool
        async def newSubagent(name: str, prompt: str, toolsets: str = "") -> str:
            """创建子代理。name 只能含字母数字下划线连字符；prompt 是它的完整任务说明（它看不到你的历史）。工具集用逗号分隔，默认 read,search,find,write,edit。同名子代理若目录已存在，会接着它上次的对话继续。"""
            return await realm.create_child(name, prompt, toolsets=toolsets)

        @tool
        async def invokeSubagent(name: str, message: str) -> str:
            """启动子代理执行任务（异步返回）。结果稍后用 wait_for_subagent 取，或读 subagents://<name>/finalResult.md。"""
            return await realm.invoke_child(name, message)

        @tool
        async def wait_for_subagent(name: str, timeout: float = 600.0) -> str:
            """等待子代理完成并返回最终结果（超时返回当前状态与已有输出）。"""
            return await realm.wait_child(name, timeout=timeout)

        @tool
        async def wait_for_subagents(
            names: str, mode: str = "all", timeout: float = 600.0
        ) -> str:
            """等待多个子代理（names 逗号分隔；mode=all 等全部 / any 等任一）。"""
            return await realm.wait_children(names, mode=mode, timeout=timeout)

        @tool
        async def stopSubagent(name: str) -> str:
            """叫停子代理当前回合（已产生的输出保留）。"""
            return await realm.stop_child(name)

        @tool
        async def removeSubagent(name: str) -> str:
            """删除子代理及其目录（含对话记录与产出，不可恢复）。"""
            return await realm.remove_child(name)

        @tool
        async def updateSubagent(name: str, toolsets: str) -> str:
            """改子代理的工具集（不用删库重建）。toolsets 逗号分隔，如 `read,search,find,write,edit,execute`；下次 invokeSubagent 生效，已有对话历史保留。"""
            return await realm.update_child(name, toolsets=toolsets)

        @tool
        async def listSubagents() -> str:
            """列出所有子代理的名称、状态与输出长度（含进程重启后恢复的）。"""
            return json.dumps(
                {"subagents": [r.overview() for r in realm.children.values()]},
                ensure_ascii=False,
            )

        return [
            newSubagent,
            invokeSubagent,
            wait_for_subagent,
            wait_for_subagents,
            stopSubagent,
            removeSubagent,
            updateSubagent,
            listSubagents,
        ]

    # ------------------------------------------------------------------
    # 生命周期实现
    # ------------------------------------------------------------------

    async def create_child(self, name: str, prompt: str, *, toolsets: str = "") -> str:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            return json.dumps(
                {"ok": False, "error": f"非法子代理名: {name!r}（字母数字开头，仅含字母数字_-，≤32 字符）"},
                ensure_ascii=False,
            )
        if name in self.children:
            return json.dumps({"ok": False, "error": f"子代理已存在: {name}"}, ensure_ascii=False)
        if self._max_children > 0 and len(self.children) >= self._max_children:
            return json.dumps(
                {"ok": False, "error": f"子代理数量已达上限 {self._max_children}；先 removeSubagent 掉不用的"},
                ensure_ascii=False,
            )
        names = tuple(n.strip() for n in toolsets.split(",") if n.strip()) or self._default_toolset
        warning = self._toolset_warning(names)

        inbox = self._child_inbox(name)
        inbox.mkdir(parents=True, exist_ok=True)
        # 目录里已有 checkpoint ⇒ 上一次的子代理（名册丢过 / 删失败）：对话会接着走。
        resumed = (inbox / "checkpoint.db").exists()
        agent_id = f"subagent-{name}"
        record = ChildRecord(
            name=name,
            agent_id=agent_id,
            inbox=inbox,
            toolsets=names,
            system_prompt=prompt,
            created_at=time.time(),
        )
        self.children[name] = record
        self._mount_child(record)
        try:
            await self._sync(record)
        except Exception as exc:  # noqa: BLE001 - 落盘失败就别假装建好了
            self.children.pop(name, None)
            self._vfs.root.children.pop(name, None)
            return json.dumps(
                {"ok": False, "error": f"子代理状态落盘失败: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        self._publish({"type": "queued", "agent_id": agent_id, "name": name})
        result: dict[str, Any] = {
            "ok": True,
            "name": name,
            "agent_id": agent_id,
            "toolsets": list(names),
            "resumed": resumed,
        }
        if warning:
            result["warning"] = warning
        return json.dumps(result, ensure_ascii=False)

    async def update_child(self, name: str, *, toolsets: str) -> str:
        """改子代理工具集。**不删目录**：只换工具，对话历史留在 checkpoint 里。"""
        record = self.children.get(name)
        if record is None:
            return json.dumps({"ok": False, "error": f"没有子代理: {name}"}, ensure_ascii=False)
        if record.status == "running":
            return json.dumps(
                {"ok": False, "error": f"子代理正在运行: {name}；先 stopSubagent 或等它结束"},
                ensure_ascii=False,
            )
        names = tuple(item.strip() for item in str(toolsets or "").split(",") if item.strip())
        if not names:
            return json.dumps(
                {"ok": False, "error": "toolsets 不能为空（全部取消请传具体列表，别传空串）"},
                ensure_ascii=False,
            )
        if names == record.toolsets:
            return json.dumps(
                {"ok": True, "name": name, "toolsets": list(names), "changed": False},
                ensure_ascii=False,
            )
        previous = record.toolsets
        record.toolsets = names
        # ``AgentSpec.toolsets`` 是构造期快照：关掉旧 Agent，下次 invoke 用新集合重建。
        # 对话历史在自己的 checkpoint.db 里，重建后接着走（与进程重启恢复同一条路）。
        if record.agent is not None:
            agent, record.agent = record.agent, None
            await self._close_agent(agent)
        await self._safe_sync(record)
        self._publish(
            {"type": "updated", "agent_id": record.agent_id, "name": name, "toolsets": list(names)}
        )
        result: dict[str, Any] = {
            "ok": True,
            "name": name,
            "toolsets": list(names),
            "previous": list(previous),
            "changed": True,
        }
        warning = self._toolset_warning(names)
        if warning:
            result["warning"] = warning
        return json.dumps(result, ensure_ascii=False)

    def _toolset_warning(self, names: tuple[str, ...]) -> str:
        """``names`` 里有父 Agent 没有的工具时给一句可读提示（不拒绝创建）。"""
        unknown = self._unknown_toolsets(names)
        if not unknown:
            return ""
        known = ", ".join(sorted(self._known_tools())) or "（父 Agent 工具尚未装配）"
        return f"父 Agent 没有这些工具，子代理拿不到: {', '.join(unknown)}（可选: {known}）"

    def _known_tools(self) -> set[str]:
        """父 Agent 当前可用的工具名（toolsets 校验用）。"""
        tools = getattr(self.parent, "tools", None) or []
        names = {str(getattr(item, "name", "")).strip() for item in tools}
        return {name for name in names if name}

    def _unknown_toolsets(self, names: tuple[str, ...]) -> list[str]:
        """``names`` 里父 Agent 没有的工具（父还没装配完时返回空，不误伤）。"""
        known = self._known_tools()
        if not known:
            return []
        return sorted(set(names) - known)

    def _mount_child(self, record: ChildRecord) -> None:
        node = VfsNode(
            name=record.name,
            is_directory=True,
            description=f"子代理 {record.name}",
        )
        node.add_child(
            VfsNode(
                name="overview.md",
                provider=lambda _n, r=record: Text(
                    json.dumps(r.overview(), ensure_ascii=False, indent=2)
                ),
            )
        )
        node.add_child(
            VfsNode(name="output.md", provider=lambda _n, r=record: Text(r.output))
        )
        node.add_child(
            VfsNode(
                name="finalResult.md",
                provider=lambda _n, r=record: Text(r.final_result or "（尚无结果）"),
            )
        )
        node.add_child(
            VfsNode(
                name="TASK.md",
                provider=lambda _n, r=record: Text(r.system_prompt),
            )
        )
        self._vfs.root.add_child(node)

    async def _ensure_agent(self, record: ChildRecord) -> "LioriumAgent":
        existing = record.agent
        if existing is not None:
            if existing.started:
                return existing
            # 上次启动被取消/失败：先收尾再重建，别把"开着 checkpoint 的半成品"当活人用。
            record.agent = None
            try:
                await existing.close()
            except Exception as exc:  # noqa: BLE001
                log.error("回收未启动完成的子代理失败: %s: %s", record.name, exc)
        from ..agent import LioriumAgent
        from ..spec import AgentSpec

        # 继承父的 realm 装配，按各自声明的 instance_scope 落地：
        # - "tree"（memory / skills / mcp …）：借用根 Agent 的同一实例 —— 工具可用、
        #   挂载只做一次、状态文件唯一；
        # - "agent"（core / todo …）：在子代理自己的 inbox 下新建一份，天然隔离；
        # "subagents" 永不继承：否则子代理能再造兄弟，递归失控。
        inherited = tuple(r for r in self.parent.spec.realms if r.id != self.id)
        spec = AgentSpec(
            id=record.agent_id,
            inbox_dir=record.inbox,
            model=record.model or self.parent.spec.model,
            prompt_sources=(),
            realms=inherited,
            toolsets={"default": record.toolsets},
            limits=self.parent.spec.limits,
            boot=None,
            workspace_root=self.parent.spec.effective_workspace_root,
        )
        agent = LioriumAgent(
            spec=spec,
            model_factory=self.parent.model_factory,
            compact_model_factory=self.parent.compact_model_factory,
            vfs=self.parent.vfs,
            memory=self.parent.memory,
            realm_factories=self.parent.realm_factories,
            artifacts=self.parent.artifacts,
            root_agent=self.parent,
            api_format=self.parent.api_format,
            priority=self.parent.priority - 1,
            system_prompt_override=record.system_prompt or None,
            tool_names=record.toolsets,
        )
        try:
            # 先登记再 start：启动期被取消时 remove/shutdown 仍能找到它并关掉这里
            # 打开的 checkpoint（否则 Windows 上子代理目录会删不掉）。
            record.agent = agent
            await agent.start()
        except BaseException:
            # 启动期被取消（stopSubagent / 宿主关闭）：收尾要抗取消（shield），
            # 否则 sqlite 连接会漏，子代理目录删不掉。
            await self._close_agent(agent)
            raise
        return agent

    async def invoke_child(self, name: str, message: str) -> str:
        record = self.children.get(name)
        if record is None:
            return json.dumps({"ok": False, "error": f"没有子代理: {name}"}, ensure_ascii=False)
        if self.ctx is None:
            return json.dumps({"ok": False, "error": "realm 未 start"}, ensure_ascii=False)
        if record.status == "running":
            return json.dumps(
                {"ok": False, "error": f"子代理正在运行: {name}；先 stopSubagent 或等它结束"},
                ensure_ascii=False,
            )
        previous = record.status
        record.status = "running"
        record.error = None
        record.output = ""
        record.final_result = ""
        record.started_at = time.time()
        record.finished_at = None
        # 上一次的提醒已经过时（它又要开跑了），别让模型下回合看到旧状态。
        self._pending = [item for item in self._pending if item.source != f"subagent:{name}"]
        # 先把"开跑"写进名册：进程此刻崩掉，下次也能看到它（并标 interrupted）。
        try:
            await self._sync(record)
        except Exception as exc:  # noqa: BLE001 - 落盘失败就别起跑，否则会卡在"运行中"
            record.status = previous
            record.error = f"状态落盘失败: {type(exc).__name__}: {exc}"
            log.error("子代理起跑前落盘失败: %s: %s", name, exc)
            return json.dumps({"ok": False, "error": record.error}, ensure_ascii=False)
        self._publish({"type": "input", "agent_id": record.agent_id, "content": message})

        async def _runner() -> None:
            await self._run_child(record, message)

        record.task = self.ctx.spawn(_runner(), name=f"subagent:{name}")
        return json.dumps(
            {"ok": True, "name": name, "status": "running", "agent_id": record.agent_id},
            ensure_ascii=False,
        )

    async def _run_child(self, record: ChildRecord, message: str) -> None:
        agent_id = record.agent_id
        try:
            agent = await self._ensure_agent(record)
            async for event in agent.run(message, origin="subagent"):
                payload = event.to_dict()
                if event.type in ("delta", "reasoning_delta"):
                    record.output += str(event.get("content") or "")
                self._publish(
                    {"type": payload.pop("type"), "agent_id": agent_id, **payload}
                )
            record.status = "done"
        except TurnInterrupted:
            record.status = "stopped"
            self._publish({"type": "stopped", "agent_id": agent_id, "name": record.name})
        except (TurnError, Exception) as exc:  # noqa: BLE001 - 子代理失败不拖垮父
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            log.error("子代理失败: %s: %s", record.name, exc, exc_info=exc)
            self._publish(
                {"type": "error", "agent_id": agent_id, "message": record.error}
            )
        finally:
            record.finished_at = time.time()
            if record.status == "done":
                record.final_result = record.output
                self._publish({"type": "finalResult", "agent_id": agent_id, "content": record.final_result})
            # 终态平进 context 队列：父模型不主动轮询也能知道它跑完了。
            self._note(record)
            # 取消路径（stopSubagent / 宿主关闭）也会走到这里：落盘尽力而为，
            # 失败/被再次取消时由 shutdown() 兜底。
            await self._safe_sync(record)

    async def wait_child(self, name: str, *, timeout: float = 600.0) -> str:
        record = self.children.get(name)
        if record is None:
            return json.dumps({"ok": False, "error": f"没有子代理: {name}"}, ensure_ascii=False)
        task = record.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"等待超时（{timeout}s），子代理仍在运行",
                        "status": record.status,
                        "output": record.output,
                    },
                    ensure_ascii=False,
                )
        return json.dumps(
            {
                "ok": record.status == "done",
                "name": name,
                "status": record.status,
                "error": record.error,
                "final_result": record.final_result,
            },
            ensure_ascii=False,
        )

    async def wait_children(
        self,
        names: str | list[str] | tuple[str, ...],
        *,
        mode: str = "all",
        timeout: float = 600.0,
    ) -> str:
        """等一组子代理：``mode="all"`` 等全部，``mode="any"`` 等任一完成。

        超时**不取消**子代理（它们仍在后台跑），只返回当前状态，调用方可以稍后
        再等或直接读 ``subagents://<name>/output.md``。
        """
        if mode not in ("all", "any"):
            return json.dumps(
                {"ok": False, "error": f"mode 只支持 all/any: {mode!r}"},
                ensure_ascii=False,
            )
        if isinstance(names, str):
            wanted = [item.strip() for item in names.split(",") if item.strip()]
        else:
            wanted = [str(item).strip() for item in names if str(item).strip()]
        if not wanted:
            return json.dumps({"ok": False, "error": "names 不能为空"}, ensure_ascii=False)

        records: list[ChildRecord] = []
        missing: list[str] = []
        for name in wanted:
            record = self.children.get(name)
            if record is None:
                missing.append(name)
            else:
                records.append(record)
        if not records:
            return json.dumps(
                {"ok": False, "error": "没有可等待的子代理", "missing": missing},
                ensure_ascii=False,
            )

        tasks = {record.task for record in records if record.task is not None and not record.task.done()}
        pending: set[Any] = set()
        if tasks:
            _done, pending = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=(
                    asyncio.ALL_COMPLETED if mode == "all" else asyncio.FIRST_COMPLETED
                ),
            )
        still_running = sorted(
            record.name for record in records if record.task in pending
        )
        items = [
            {
                "name": record.name,
                "status": record.status,
                "error": record.error,
                "final_result": record.final_result,
            }
            for record in records
        ]
        return json.dumps(
            {
                "ok": all(item["status"] == "done" for item in items),
                "mode": mode,
                "subagents": items,
                "missing": missing,
                "still_running": still_running,
                "timed_out": bool(pending),
            },
            ensure_ascii=False,
        )

    async def stop_child(self, name: str) -> str:
        record = self.children.get(name)
        if record is None:
            return json.dumps({"ok": False, "error": f"没有子代理: {name}"}, ensure_ascii=False)
        if record.agent is not None:
            await record.agent.interrupt(reason="stop_subagent")
        if record.status == "running":
            # 取消任务不会经过 `_run_child` 的异常分支（CancelledError 是 BaseException），
            # 这里先标 stopped，CLI/宿主立刻能看到正确状态。
            record.status = "stopped"
        self._publish({"type": "stopping", "agent_id": record.agent_id, "name": name})
        await self._cancel_round(record)
        await self._safe_sync(record)
        return json.dumps({"ok": True, "name": name, "status": record.status}, ensure_ascii=False)

    async def remove_child(self, name: str) -> str:
        record = self.children.get(name)
        if record is None:
            return json.dumps({"ok": False, "error": f"没有子代理: {name}"}, ensure_ascii=False)
        await self._cancel_round(record)
        if record.agent is not None:
            # 先关（释放 checkpoint 句柄），删不掉时记录还能再起一个新 Agent。
            agent, record.agent = record.agent, None
            await self._close_agent(agent)
        failure = await self._delete_files(record)
        if failure is not None:
            return json.dumps(
                {"ok": False, "error": f"删除子代理目录失败（记录保留）: {failure}"},
                ensure_ascii=False,
            )
        self.children.pop(name, None)
        self._vfs.root.children.pop(name, None)
        await self._persist()
        self._publish({"type": "deleted", "agent_id": record.agent_id, "name": name})
        return json.dumps({"ok": True, "deleted": name}, ensure_ascii=False)

    async def _delete_files(self, record: ChildRecord, *, attempts: int = 8) -> str | None:
        """删掉子代理目录（对话 checkpoint + 它自己的 realm 状态 + 产出）。失败返回原因。

        Windows 上 sqlite 句柄在 ``conn.close()`` 之后可能还要过一小会儿才真正释放，
        所以这里重试几次；真删不掉才把原因报出去（记录保留，不会假装删了）。
        """
        target = record.inbox.resolve()
        root = (self.parent.spec.inbox_dir / "subagents").resolve()
        if target.parent != root:
            return f"拒绝删除 {target}（不在 {root} 下）"
        last: str | None = None
        for attempt in range(attempts):
            try:
                await asyncio.to_thread(shutil.rmtree, target)
                return None
            except FileNotFoundError:
                return None
            except OSError as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < attempts:
                    # 子代理在 start() 的 checkpoint.open() 阶段被取消时，aiosqlite 可能
                    # 留一个没人持有的 sqlite 连接（取消点发生在它自己线程里）；句柄要等
                    # 对象被回收才释放 ⇒ 重试前主动收一次垃圾。
                    gc.collect()
                    await asyncio.sleep(0.15)
        return last


__all__ = ["DEFAULT_SUBAGENT_TOOLSET", "ChildRecord", "SubagentRealm"]
