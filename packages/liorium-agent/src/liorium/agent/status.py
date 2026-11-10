"""``AgentStatus`` / ``ApplyResult`` / ``CacheEpoch``。

计划依据 §3.2、§4.2、§B7：``apply()`` 返回差量结果；``CacheEpoch`` 单整数，
只观测记账，不参与任何决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RealmState = Literal["pending", "starting", "running", "failed", "stopped"]


@dataclass(frozen=True)
class CacheEpoch:
    """缓存世代：**仅当** 工具 schema 集合 + system prompt 变化时 +1。

    v1 只观测记账（计划 §E4）：不重排、不拆段、不预热。
    """

    value: int = 0
    tool_schema_fp: str = ""
    prompt_fp: str = ""
    reason: str = ""

    def bumped(self, *, tool_schema_fp: str, prompt_fp: str, reason: str) -> "CacheEpoch":
        if tool_schema_fp == self.tool_schema_fp and prompt_fp == self.prompt_fp:
            return self
        return CacheEpoch(
            value=self.value + 1,
            tool_schema_fp=tool_schema_fp,
            prompt_fp=prompt_fp,
            reason=reason,
        )


@dataclass(frozen=True)
class RealmStatus:
    id: str
    state: RealmState = "pending"
    error: str | None = None
    tools: tuple[str, ...] = ()
    mounts: tuple[str, ...] = ()
    shared: bool = False
    """``True`` = 这条 realm 是``instance_scope="tree"``，从根 Agent 借用（状态/挂载归根）。"""
    context_due: int = 0
    """第五拍：还有几回合到期（``0`` = 本回合就问；不参与周期上下文的 realm 恒为 0）。"""
    context_injections: int = 0
    """本进程内成功注入 Context 的次数（``ctx.inject`` 不计入）。"""


@dataclass(frozen=True)
class AgentStatus:
    """``status()`` 的快照。不含运行时对象（模型/VFS），只放可序列化事实。"""

    agent_id: str
    running: bool = False
    turn_id: str | None = None
    queue_depth: int = 0
    spec_fingerprint: str = ""
    epoch: CacheEpoch = field(default_factory=CacheEpoch)
    realms: tuple[RealmStatus, ...] = ()
    messages: int = 0
    pending_context: int = 0
    started: bool = False
    closed: bool = False


@dataclass(frozen=True)
class ApplyResult:
    """``apply()`` 的差量结果。

    ``failed`` 不为空时沿用旧部件跑（降级但可用，计划 §B7）。
    ``forced=True`` 表示宿主显式要求重建（即使 spec 指纹没变），
    例如只改了宿主侧的模型构建参数（思考强度）。
    """

    changed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    fingerprint: str = ""
    epoch: CacheEpoch = field(default_factory=CacheEpoch)
    forced: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


__all__ = ["AgentStatus", "ApplyResult", "CacheEpoch", "RealmState", "RealmStatus"]
