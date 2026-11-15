"""只读钩子的适配器（2026-11-14）。

六个钩子里，``on_turn_start`` / ``on_turn_end`` 由 worker 驱动（回合边界），
四个 model/tool 钩子由本模块的 ``AgentMiddleware`` 适配器驱动：
**进入时发 start，返回或异常时发 end**。

三条硬约束写进实现，不靠自觉：

- **没有 ``nxt()``**：不把"继续执行"的句柄交给 realm，也没有返回值通道；
- **拿不到 langchain 对象**：``ModelCall.messages`` 是 tuple 快照、``ToolCall.args`` 是
  ``MappingProxyType``，``ModelRequest`` / ``ToolCallRequest`` 不出这个文件；
- **异常只记日志**：观测者出 bug 不许拖垮回合（``on_*_end`` 在工具/模型失败路径上也会触发，
  但钩子自己的异常一律吞掉）。

取消（``CancelledError``）不走 end 钩子：任务已被取消，再 ``await`` 只会立刻再被取消。
"""

from __future__ import annotations

import inspect
import time
from types import MappingProxyType
from typing import Any, Callable, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from liorium.core.log import get_logger
from liorium.realm_base import (
    HookOutcome,
    ModelCall,
    ToolCall,
    TurnInfo,
    overrides_hook,
)

from ..runtime.tools import looks_like_tool_error

log = get_logger("liorium.agent.realm_hooks")

MODEL_TOOL_HOOKS = ("on_model_start", "on_model_end", "on_tool_start", "on_tool_end")
TURN_HOOKS = ("on_turn_start", "on_turn_end")


def has_model_tool_hooks(instance: Any) -> bool:
    return any(overrides_hook(instance, name) for name in MODEL_TOOL_HOOKS)


def has_turn_hooks(instance: Any) -> bool:
    return any(overrides_hook(instance, name) for name in TURN_HOOKS)


def _outcome(started: float, error: BaseException | None = None) -> HookOutcome:
    return HookOutcome(
        ok=error is None,
        error=None if error is None else f"{type(error).__name__}: {error}",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


class RealmHooksMiddleware(AgentMiddleware):
    """把一个 realm 的四个 model/tool 钩子接到中间件链的最外层。

    一个 realm 一个实例；只在该 realm 覆盖了钩子时才装配（见 ``has_model_tool_hooks``）。
    """

    def __init__(
        self,
        realm: Any,
        *,
        realm_id: str,
        model: str,
        turn_info: Callable[[], "TurnInfo | None"],
    ) -> None:
        super().__init__()
        self.realm = realm
        self.realm_id = realm_id
        self.model = model
        self._turn_info = turn_info
        self._turn_id: str | None = None
        self._model_index = 0

    # -- 内部 -----------------------------------------------------------

    @property
    def name(self) -> str:
        """每个 realm 一个实例：langchain 按 ``name`` 去重，同名会被拒。"""
        return f"realm_hooks:{self.realm_id}"

    async def _notify(self, name: str, *args: Any) -> None:
        """调用 realm 的钩子；**任何异常只记日志**（观测者不许拖垮回合）。"""
        hook = getattr(self.realm, name, None)
        if hook is None:
            return
        try:
            result = hook(*args)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 故意兜住
            log.error(
                "realm %s 的 %s 失败（已忽略）: %s: %s",
                self.realm_id,
                name,
                type(exc).__name__,
                exc,
            )

    def _current_turn(self) -> "TurnInfo | None":
        try:
            return self._turn_info()
        except Exception:  # noqa: BLE001 - 回合信息取不到不该影响调用
            return None

    def _next_model_index(self) -> int:
        turn = self._current_turn()
        turn_id = turn.id if turn is not None else None
        if turn_id != self._turn_id:
            self._turn_id = turn_id
            self._model_index = 0
        self._model_index += 1
        return self._model_index

    # -- 模型 -----------------------------------------------------------

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        turn = self._current_turn()
        call = ModelCall(
            turn=turn,
            model=self.model,
            messages=tuple(getattr(request, "messages", ()) or ()),
            index=self._next_model_index(),
        )
        await self._notify("on_model_start", call)
        started = time.monotonic()
        try:
            response = await handler(request)
        except Exception as exc:
            await self._notify("on_model_end", call, _outcome(started, exc))
            raise
        await self._notify("on_model_end", call, _outcome(started))
        return response

    # -- 工具 -----------------------------------------------------------

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        raw = getattr(request, "tool_call", None) or {}
        args = raw.get("args") if isinstance(raw, dict) else None
        call = ToolCall(
            turn=self._current_turn(),
            name=str((raw.get("name") if isinstance(raw, dict) else "") or ""),
            args=MappingProxyType(dict(args or {})),
            call_id=str((raw.get("id") if isinstance(raw, dict) else "") or ""),
        )
        await self._notify("on_tool_start", call)
        started = time.monotonic()
        try:
            result = await handler(request)
        except Exception as exc:
            await self._notify("on_tool_end", call, _outcome(started, exc))
            raise
        # 工具自己的异常会被 wrap_tools 兜成 ``工具执行出错\n[完整输出: artifact://…]``
        # （或 ToolNode 的 ``ToolMessage(status="error")``），不会往外抛；看结果形状才知道失败。
        status = getattr(result, "status", None)
        content = getattr(result, "content", None)
        if status == "error" or (isinstance(result, ToolMessage) and looks_like_tool_error(content)):
            error = RuntimeError(str(content or "工具执行失败"))
            await self._notify("on_tool_end", call, _outcome(started, error))
        else:
            await self._notify("on_tool_end", call, _outcome(started))
        return result


def hook_middleware(
    instance: Any,
    *,
    realm_id: str,
    model: str,
    turn_info: Callable[[], "TurnInfo | None"],
) -> RealmHooksMiddleware | None:
    """该 realm 需要钩子适配器吗？不需要返回 ``None``（保持链条最短）。"""
    if not has_model_tool_hooks(instance):
        return None
    return RealmHooksMiddleware(instance, realm_id=realm_id, model=model, turn_info=turn_info)


def turn_hook_instances(records: Sequence[Any], name: str) -> list[Any]:
    """运行中的、覆盖了 ``name`` 的 realm 实例（保持声明序）。"""
    out: list[Any] = []
    for record in records:
        if record.state != "running":
            continue
        if overrides_hook(record.instance, name):
            out.append(record.instance)
    return out


__all__ = [
    "MODEL_TOOL_HOOKS",
    "TURN_HOOKS",
    "RealmHooksMiddleware",
    "has_model_tool_hooks",
    "has_turn_hooks",
    "hook_middleware",
    "overrides_hook",
    "turn_hook_instances",
]
