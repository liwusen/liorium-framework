"""Liorium 异常类型。

原则（仓库 AGENTS.md「不进行错误隐瞒」）：能力起不来、工具名冲突、挂载环，
都必须以显式异常或 ``failed`` 状态暴露，不做静默 ``continue``。
"""

from __future__ import annotations


class LioriumError(Exception):
    """Liorium 全部异常的基类。"""


class SpecError(LioriumError):
    """``AgentSpec`` 不合法（缺字段、路径不存在、环依赖等）。"""


class ConfigError(LioriumError):
    """宿主传入的端口/参数不合法。"""


# ---------------------------------------------------------------------------
# realm
# ---------------------------------------------------------------------------


class RealmError(LioriumError):
    """realm 生命周期或装配错误。"""


class RealmNotFoundError(RealmError):
    """``AgentSpec.realms`` 引用了没有工厂的 realm id。"""


class RealmDependencyError(RealmError):
    """``requires`` 成环或缺失。"""


class RealmStartError(RealmError):
    """``start()`` 失败。v1 语义 = 该 realm 标 ``failed`` 降级，不整体失败（§C5）。"""


class ToolNameConflictError(RealmError):
    """两个 realm（或 realm 与基座）声明了同名工具。必须报错（§C3）。"""

    def __init__(self, name: str, owners: list[str]) -> None:
        self.name = name
        self.owners = list(owners)
        super().__init__(f"工具名冲突: {name!r} 由 {', '.join(self.owners)} 同时声明")


# ---------------------------------------------------------------------------
# VFS
# ---------------------------------------------------------------------------


class VfsError(LioriumError):
    """VFS 操作错误基类。"""


class MountConflictError(VfsError):
    """挂载前缀与已有文件/目录冲突（§3.6 规则 6：报错，不静默覆盖）。"""


class MountCycleError(VfsError):
    """A 挂 B、B 挂 A（§D4 环检测）。"""


class NodeNotFoundError(VfsError):
    """路径不存在。"""


class VfsReadOnlyError(VfsError):
    """对只读挂载点写入。"""


class ContentTypeError(VfsError):
    """内容类型不支持该操作（例如对 Binary 做行选择器）。"""


# ---------------------------------------------------------------------------
# 回合
# ---------------------------------------------------------------------------


class TurnInterrupted(LioriumError):
    """回合被 ``interrupt()`` 取消。宿主翻译成 ``interrupted`` 帧。"""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        super().__init__(f"回合被中断: {reason}" if reason else "回合被中断")


class TurnError(LioriumError):
    """回合执行失败。宿主翻译成 ``error`` 帧。"""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.message = message
        self.cause = cause
        super().__init__(message)


class TurnDropped(LioriumError):
    """回合在排队阶段被丢弃（调度器拥塞 + ``FLAG.DROPPABLE``）。

    与 ``TurnInterrupted`` 区分开：被丢弃的回合**从来没开始执行**，没有产生任何
    事件，也不该被当成"被中断"（前者可能已经落过部分 delta）。
    """

    def __init__(self, reason: str = "congested") -> None:
        self.reason = reason
        super().__init__(f"回合被丢弃: {reason}")


class CheckpointError(LioriumError):
    """checkpoint 打开/读取/写入失败。"""


__all__ = [
    "CheckpointError",
    "ConfigError",
    "ContentTypeError",
    "MountConflictError",
    "MountCycleError",
    "NodeNotFoundError",
    "RealmDependencyError",
    "RealmError",
    "RealmNotFoundError",
    "RealmStartError",
    "SpecError",
    "ToolNameConflictError",
    "TurnDropped",
    "TurnError",
    "TurnInterrupted",
    "LioriumError",
    "VfsError",
    "VfsReadOnlyError",
]
