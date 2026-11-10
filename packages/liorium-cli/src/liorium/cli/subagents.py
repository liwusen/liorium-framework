"""CLI 侧的子代理观测：消费 ``SubagentRealm`` 的事件流并渲染。

``SubagentRealm`` 自己暴露 ``events()``（没有全局 notify，见 realm 设计）：CLI 作为
宿主起一个后台任务持续消费，按子代理名分桶保存最近 ``history`` 条事件。

- 父回合**进行中**时，只把关键事件（创建 / 启动 / 工具 / 完成 / 失败）实时打到终端，
  正文增量不混排 —— 多子代理并行时逐字混排会完全没法读。
- ``/subagent <name>`` 从缓冲区取快照；``/subagent`` 列状态。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from liorium.core.log import get_logger

from .ui import Ui, char_count, clock, duration, short_json, truncate

log = get_logger("liorium.cli.subagents")

_MAX_AGENTS = 64
"""最多同时跟踪多少个子代理（超出丢最早的桶，防止长会话无界增长）。"""

_PAYLOAD_MAX_CHARS = 4000
"""单条事件里正文/结果的保留上限；完整内容仍在 ``subagents://<name>/output.md``。"""

_KEY_EVENT_TYPES = frozenset(
    {
        "queued",
        "input",
        "tool_start",
        "tool_result",
        "compact_start",
        "compact_done",
        "done",
        "error",
        "stopped",
        "stopping",
        "deleted",
    }
)


@dataclass(frozen=True)
class SubagentEvent:
    """缓冲区里的一条子代理事件（带本地到达时间）。"""

    at: float
    type: str
    agent_id: str
    payload: dict[str, Any]


class SubagentMonitor:
    """订阅一个 ``SubagentRealm``，按名字保存最近事件。"""

    def __init__(
        self,
        *,
        ui: Ui,
        history: int = 500,
        live: Callable[[], bool] | None = None,
    ) -> None:
        self.ui = ui
        self.history = max(1, int(history))
        self._live = live or (lambda: False)
        self._buffers: dict[str, deque[SubagentEvent]] = {}
        self._names: dict[str, str] = {}
        self._realm: Any = None
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[Any] | None = None

    # -- 生命周期 -------------------------------------------------------

    @property
    def realm(self) -> Any:
        return self._realm

    async def attach(self, realm: Any) -> None:
        """开始消费 ``realm.events()``（重复调用会先停掉上一个）。

        订阅必须在 ``create_task`` **之前**同步完成：否则 ``create_child`` /
        ``invoke_child`` 在监控任务第一次被调度前发布的事件会直接丢掉。
        """
        await self.close()
        self._realm = realm
        self._queue = realm.subscribe()
        self._task = asyncio.create_task(
            self._consume(realm, self._queue), name="liorium-cli-subagent-events"
        )

    async def close(self) -> None:
        task, self._task = self._task, None
        queue, self._queue = self._queue, None
        realm, self._realm = self._realm, None
        if queue is not None and realm is not None:
            try:
                realm.unsubscribe(queue)
            except Exception as exc:  # noqa: BLE001
                log.warning("取消子代理事件订阅失败: %s", exc)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 关闭期日志即可，不拖垮退出
            log.warning("子代理事件消费任务退出异常: %s", exc)

    def clear(self) -> None:
        self._buffers.clear()
        self._names.clear()

    async def _consume(
        self, realm: Any, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        try:
            while True:
                payload = await queue.get()
                self._handle(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 观测失败不该影响主流程
            log.warning("子代理事件消费失败（后续事件不再显示）: %s", exc)
        finally:
            try:
                realm.unsubscribe(queue)
            except Exception:  # noqa: BLE001
                pass

    # -- 查询 -----------------------------------------------------------

    def names(self) -> list[str]:
        return list(self._buffers)

    def count(self, name: str) -> int:
        buffer = self._buffers.get(name)
        return len(buffer) if buffer is not None else 0

    def last(self, name: str) -> SubagentEvent | None:
        buffer = self._buffers.get(name)
        return buffer[-1] if buffer else None

    def snapshot(self, name: str, limit: int = 20) -> list[SubagentEvent]:
        buffer = self._buffers.get(name)
        if not buffer:
            return []
        if limit <= 0:
            return []
        start = max(0, len(buffer) - limit)
        return list(buffer)[start:]

    def drop(self, name: str) -> None:
        self._buffers.pop(name, None)

    # -- 写入 -----------------------------------------------------------

    def _handle(self, payload: dict[str, Any]) -> None:
        payload = _prune(dict(payload))
        agent_id = str(payload.get("agent_id") or "")
        name = str(payload.get("name") or self._names.get(agent_id) or self._name_for(agent_id))
        if agent_id and name:
            self._names[agent_id] = name
        if not name:
            name = agent_id or "?"
        buffer = self._buffers.get(name)
        if buffer is None:
            if len(self._buffers) >= _MAX_AGENTS:
                self._buffers.pop(next(iter(self._buffers)), None)
            buffer = deque(maxlen=self.history)
            self._buffers[name] = buffer
        event = SubagentEvent(
            at=time.time(),
            type=str(payload.get("type") or "?"),
            agent_id=agent_id,
            payload=payload,
        )
        buffer.append(event)
        if event.type in _KEY_EVENT_TYPES and self._live():
            self._render_live(name, event)
        if event.type == "deleted":
            # 删除后不再保留这个子代理的缓冲区：`/subagent` 列表里也不该再出现。
            self.drop(name)
            self._names.pop(agent_id, None)

    def _name_for(self, agent_id: str) -> str:
        realm = self._realm
        children = getattr(realm, "children", None)
        if not agent_id or not isinstance(children, dict):
            return ""
        for name, record in children.items():
            if getattr(record, "agent_id", "") == agent_id:
                return str(name)
        return ""

    # -- 实时渲染 -------------------------------------------------------

    def _render_live(self, name: str, event: SubagentEvent) -> None:
        if self.ui.json_mode:
            return  # JSON Lines 模式不能混入人类可读行
        line = _live_line(event, self.ui)
        if line is None:
            return
        style, text, indent = line
        ui = self.ui
        prefix = ui.violet(f"[sub:{name}]")
        ui.ensure_newline()
        rendered = getattr(ui, style)(text)
        if indent:
            ui.writeln(f"  {prefix} {rendered}")
        else:
            ui.writeln(f"{prefix} {rendered}")


# ---------------------------------------------------------------------------
# 事件 → 文本
# ---------------------------------------------------------------------------


def _live_line(event: SubagentEvent, ui: Ui) -> tuple[str, str, bool] | None:
    """``(ui 方法名, 文本, 是否缩进)``；正文增量返回 None（不实时混排）。"""
    payload = event.payload
    kind = event.type
    if kind == "queued":
        return "dim", "已创建，等待启动", False
    if kind == "input":
        text = _text(payload.get("content"))
        return (
            "mint",
            f"{ui.glyph('start')} 开始：{truncate(text, limit=120, max_lines=1)}",
            False,
        )
    if kind == "tool_start":
        return "violet", f"{_tool(payload)}{short_json(payload.get('args'), limit=120)}", False
    if kind == "tool_result":
        text = truncate(_text(payload.get("output")), limit=160, max_lines=2)
        return "dim", f"↳ {text}", True
    if kind == "compact_start":
        return "yellow", "上下文压缩中…", False
    if kind == "compact_done":
        ok = payload.get("ok")
        if ok is False:
            return "red", f"压缩失败：{payload.get('error') or ''}", False
        return "dim", "压缩完成", False
    if kind == "done":
        return "mint", f"{ui.glyph('done')} 回合完成", False
    if kind == "finalResult":
        return None  # done 已经报过，结果留给 /subagent 看
    if kind == "error":
        return "red", f"失败：{payload.get('message') or payload.get('error') or '?'}", False
    if kind in ("stopped", "stopping"):
        return "yellow", "已停止", False
    if kind == "deleted":
        return "dim", "已删除", False
    return None


def _snapshot_line(event: SubagentEvent) -> str:
    payload = event.payload
    kind = event.type
    if kind == "start":
        return "开始生成"
    if kind == "queued":
        return "已创建，等待启动"
    if kind == "input":
        return "启动：" + truncate(_text(payload.get("content")), limit=160, max_lines=2)
    if kind == "tool_start":
        return f"调用 {_tool(payload)}{short_json(payload.get('args'), limit=160)}"
    if kind == "tool_result":
        return "结果 " + truncate(_text(payload.get("output")), limit=240, max_lines=3)
    if kind == "compact_start":
        return "上下文压缩中…"
    if kind == "compact_done":
        if payload.get("ok") is False:
            return f"压缩失败：{payload.get('error') or ''}"
        return "压缩完成"
    if kind == "done":
        return "回合完成"
    if kind == "finalResult":
        return f"最终结果 {char_count(len(_text(payload.get('content'))))} 字符"
    if kind == "error":
        return f"失败：{payload.get('message') or payload.get('error') or '?'}"
    if kind in ("stopped", "stopping"):
        return "已停止"
    if kind == "deleted":
        return "已删除"
    detail = {
        key: value
        for key, value in payload.items()
        if key not in ("type", "agent_id", "name")
    }
    return truncate(short_json(detail, limit=200).strip(), limit=200, max_lines=2)


def _iter_snapshot_lines(events: list[SubagentEvent]) -> list[str]:
    """把连续 delta 合并成一行（否则最近 20 条可能全是增量碎片）。"""
    lines: list[str] = []
    index = 0
    while index < len(events):
        event = events[index]
        if event.type in ("delta", "reasoning_delta"):
            total = 0
            start = index
            while index < len(events) and events[index].type in ("delta", "reasoning_delta"):
                total += len(_text(events[index].payload.get("content")))
                index += 1
            label = "正文" if event.type == "delta" else "推理"
            lines.append(f"{clock(event.at)}  {label:<10} +{total} 字符（{index - start} 片段）")
            continue
        lines.append(
            f"{clock(event.at)}  {_label(event.type):<10} {_snapshot_line(event)}"
        )
        index += 1
    return lines


def _label(kind: str) -> str:
    return {
        "start": "开始",
        "tool_start": "工具",
        "tool_result": "结果",
        "queued": "创建",
        "input": "启动",
        "done": "完成",
        "error": "错误",
        "stopped": "停止",
        "stopping": "停止中",
        "deleted": "删除",
        "compact_start": "压缩",
        "compact_done": "压缩",
        "finalResult": "结果",
    }.get(kind, kind)


# ---------------------------------------------------------------------------
# 命令渲染
# ---------------------------------------------------------------------------


def resolve_name(realm: Any, name: str) -> str:
    """把命令里的名字归一化：接受子代理名、``subagent-<name>`` 或完整 ``agent_id``。"""
    children = getattr(realm, "children", None)
    if not isinstance(children, dict):
        return name
    if name in children:
        return name
    for child_name, record in children.items():
        if getattr(record, "agent_id", "") == name:
            return str(child_name)
    prefix = "subagent-"
    if name.startswith(prefix) and name[len(prefix) :] in children:
        return name[len(prefix) :]
    return name


def render_list(ui: Ui, realm: Any, monitor: SubagentMonitor | None) -> None:
    """``/subagent``：列出所有子代理的状态与最后一条事件。"""
    children: dict[str, Any] = dict(getattr(realm, "children", {}) or {})
    names = sorted(set(children) | set(monitor.names() if monitor is not None else ()))
    if not names:
        ui.info("还没有子代理。让 Agent 用 newSubagent / invokeSubagent 创建。")
        return

    ui.writeln()
    for name in names:
        record = children.get(name)
        status = str(getattr(record, "status", "removed"))
        last = monitor.last(name) if monitor is not None else None
        elapsed = _elapsed(record)
        output = len(str(getattr(record, "output", "") or ""))
        detail = f"{duration(elapsed):>8}  输出 {char_count(output):>6}"
        if last is not None:
            detail += f"  最后 {clock(last.at)} {_label(last.type)}"
        ui.writeln(f"  {ui.violet(name.ljust(16))}{ui.mint(status.ljust(9))}{ui.dim(detail)}")
    ui.writeln(ui.dim("  /subagent <name> [n] 看最近 n 条事件（默认 20）；stop|rm <name> 控制"))
    ui.writeln()


def render_snapshot(
    ui: Ui,
    realm: Any,
    name: str,
    monitor: SubagentMonitor | None,
    *,
    limit: int = 20,
) -> bool:
    """``/subagent <name> [n]``：状态头 + 最近事件。找不到返回 False。"""
    children: dict[str, Any] = dict(getattr(realm, "children", {}) or {})
    record = children.get(name)
    events = monitor.snapshot(name, limit) if monitor is not None else []
    if record is None and not events:
        return False

    ui.writeln()
    if record is None:
        ui.writeln(f"  子代理 {ui.violet(name)}  {ui.dim('[已删除]')}")
    else:
        status = str(getattr(record, "status", "?"))
        elapsed = _elapsed(record)
        output = len(str(getattr(record, "output", "") or ""))
        ui.writeln(
            f"  子代理 {ui.violet(name)}  {ui.mint(f'[{status}]')}  "
            f"{ui.dim(f'{duration(elapsed)}  输出 {char_count(output)} 字符')}"
        )
        toolsets = ", ".join(getattr(record, "toolsets", ()) or ())
        if toolsets:
            ui.writeln(f"    {ui.dim('工具')}  {toolsets}")
        task = truncate(str(getattr(record, "system_prompt", "") or ""), limit=240, max_lines=2)
        if task and task != "(无输出)":
            ui.writeln(f"    {ui.dim('任务')}  {task}")
        error = getattr(record, "error", None)
        if error:
            ui.writeln(f"    {ui.red('错误')}  {error}")

    total = monitor.count(name) if monitor is not None else len(events)
    if not events:
        ui.info("还没有事件（子代理尚未启动，或事件缓冲区为空）")
        ui.writeln()
        return True
    ui.writeln(ui.dim(f"  最近 {len(events)} 条事件（缓冲区共 {total} 条）:"))
    for line in _iter_snapshot_lines(events):
        ui.writeln(f"  {line}")
    ui.writeln()
    return True


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _elapsed(record: Any) -> float | None:
    started = getattr(record, "started_at", None)
    if not started:
        return None
    finished = getattr(record, "finished_at", None)
    return (finished or time.time()) - started


def _tool(payload: dict[str, Any]) -> str:
    name = payload.get("tool_name") or payload.get("name") or "?"
    return str(name)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    """限制单条事件的正文体积：缓冲区不是真相，完整输出在 VFS 里。"""
    for key in ("content", "output"):
        value = payload.get(key)
        if isinstance(value, str) and len(value) > _PAYLOAD_MAX_CHARS:
            payload[key] = value[:_PAYLOAD_MAX_CHARS] + "…"
    return payload


__all__ = [
    "SubagentEvent",
    "SubagentMonitor",
    "render_list",
    "render_snapshot",
    "resolve_name",
]
