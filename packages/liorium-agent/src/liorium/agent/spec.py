"""``AgentSpec`` —— 可序列化的默认装配。

计划依据 §3.1 / §B2 / §3.9：

- spec 只装**能被读出来**的东西；模型实例 / 记忆实例 / VFS / 回调 / websocket /
  时钟 / 许可位一律走 ports；
- 指纹只覆盖结构（``model`` / ``prompt_sources`` / ``realms`` / ``toolsets`` /
  ``limits``），不覆盖 ``id``（身份）、``inbox_dir``（身份）、``boot``（宿主引导语）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from liorium.core.canonical import fingerprint
from liorium.core.errors import SpecError


@dataclass(frozen=True)
class Limits:
    """阈值。全部有默认值，宿主可只覆盖关心的项。"""

    max_turns: int = 500
    """单回合最多模型调用次数（防死循环）。"""

    recursion_limit: int = 1500
    """LangGraph 图递归上限。"""

    compact_threshold_ratio: float = 0.8
    """上下文占用超过 ``context_window * ratio`` 触发压缩。"""

    compact_keep_rounds: int = 3
    """压缩时保留的完整轮数（上限，不是下限）。"""

    compact_max_output_tokens: int = 4000
    """摘要调用输出上限；0 = 不设。"""

    compact_cost_control: bool = False
    """成本控制开关（默认关；打开后才裁剪输入并设输出上限）。"""

    compact_max_input_tokens: int = 0
    """成本控制下的输入上限；0 = 不设。"""

    max_tool_output_chars: int = 120
    """工具输出短于此且不含换行时不进 artifact（保持现状语义）。"""

    max_subagent_depth: int = 2
    """子代理递归深度上限。"""

    context_window: int = 0
    """模型上下文窗口（token）。0 = 未知，压缩只能用 API 上报值判定。"""

    def __post_init__(self) -> None:
        if self.max_turns <= 0:
            raise SpecError("Limits.max_turns 必须 > 0")
        if not 0 < self.compact_threshold_ratio <= 1:
            raise SpecError("Limits.compact_threshold_ratio 必须在 (0, 1]")
        if self.compact_keep_rounds < 1:
            raise SpecError("Limits.compact_keep_rounds 必须 >= 1")
        if self.context_window < 0:
            raise SpecError("Limits.context_window 不能为负")


@dataclass(frozen=True)
class BootSpec:
    """首次引导语。

    **库不自动执行**（计划 §F7 / §3.9）：``start()`` 绝不调模型；
    宿主拿到这条消息后自己决定要不要 ``run(boot.message)``。
    """

    message: str | None = None


@dataclass(frozen=True)
class RealmSpec:
    """一个 realm 的声明。``params`` 进指纹，所以必须可序列化。"""

    id: str
    params: Mapping[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise SpecError("RealmSpec.id 不能为空")
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "requires", tuple(self.requires))
        try:
            fingerprint(self.params)
        except TypeError as exc:  # 不可序列化参数会污染指纹
            raise SpecError(f"realm {self.id!r} 的 params 不可序列化: {exc}") from exc


@dataclass(frozen=True)
class Profile:
    """profile 语法糖（计划 §C2 / §Phase 2）：一组可复用的 realm + prompt + toolset。

    ``AgentSpec.with_profile(profile)`` 把三者**合并**进 spec，指纹覆盖合并结果，
    所以"用 profile 装的"和"手写同一组 realm"是同一个装配（不会产生两套行为）。

    合并规则（显式声明总是赢）：

    - ``realms``：profile 的在前，spec 已有的按 id 覆盖 profile 里的同 id 项；
    - ``prompt_sources``：profile 的在前，spec 追加，按出现顺序去重；
    - ``toolsets``：同名 key 以 spec 为准。
    """

    id: str
    realms: tuple[RealmSpec, ...] = ()
    prompt_sources: tuple[str, ...] = ()
    toolsets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise SpecError("Profile.id 不能为空")
        object.__setattr__(self, "realms", tuple(self.realms))
        object.__setattr__(self, "prompt_sources", tuple(str(p) for p in self.prompt_sources))
        object.__setattr__(
            self, "toolsets", {str(k): tuple(str(t) for t in v) for k, v in self.toolsets.items()}
        )
        seen: set[str] = set()
        for realm in self.realms:
            if not isinstance(realm, RealmSpec):
                raise SpecError(f"profile {self.id!r} 的 realms 里出现非 RealmSpec: {realm!r}")
            if realm.id in seen:
                raise SpecError(f"profile {self.id!r} 里 realm id 重复: {realm.id!r}")
            seen.add(realm.id)

    def expand(self, spec: "AgentSpec") -> "AgentSpec":
        """合并进 spec（不改原 spec）。"""
        spec_realms = {r.id: r for r in spec.realms}
        merged_realms = tuple(
            spec_realms.get(r.id, r) for r in self.realms
        ) + tuple(r for r in spec.realms if r.id not in {p.id for p in self.realms})
        prompt_sources: list[str] = []
        for source in (*self.prompt_sources, *spec.prompt_sources):
            if source not in prompt_sources:
                prompt_sources.append(source)
        toolsets: dict[str, tuple[str, ...]] = {
            **{k: tuple(v) for k, v in self.toolsets.items()},
            **{k: tuple(v) for k, v in spec.toolsets.items()},
        }
        return replace(
            spec,
            realms=merged_realms,
            prompt_sources=tuple(prompt_sources),
            toolsets=toolsets,
        )


@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent 的默认装配。

    ``workspace_root`` 为 None 时等于 ``inbox_dir``（计划 §3.9）。
    """

    id: str
    inbox_dir: Path
    model: str
    """不透明标签（``provider::model``），只进指纹与日志；实例由 ``model_factory`` 提供。"""
    prompt_sources: tuple[str, ...] = ()
    """prompt 文件路径，按序拼接；相对路径相对于 ``inbox_dir``。"""
    realms: tuple[RealmSpec, ...] = ()
    toolsets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """工具子集，**由 realm 解释**：`"default"` 键目前归 `core` realm（`CoreToolRealm`）读，
    用来筛六个核心工具名。Agent 自己不再读它（2026-11-08：Agent 不添加任何工具）。"""
    limits: Limits = field(default_factory=Limits)
    boot: BootSpec | None = None
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise SpecError("AgentSpec.id 不能为空")
        object.__setattr__(self, "inbox_dir", Path(self.inbox_dir))
        if self.workspace_root is not None:
            object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "prompt_sources", tuple(str(p) for p in self.prompt_sources))
        object.__setattr__(self, "realms", tuple(self.realms))
        object.__setattr__(
            self,
            "toolsets",
            {str(k): tuple(str(t) for t in v) for k, v in self.toolsets.items()},
        )
        seen: set[str] = set()
        for r in self.realms:
            if not isinstance(r, RealmSpec):
                raise SpecError(f"realms 里出现非 RealmSpec: {r!r}")
            if r.id in seen:
                raise SpecError(f"realm id 重复: {r.id!r}")
            seen.add(r.id)
        for name in self.toolsets:
            if not name:
                raise SpecError("toolset 名字不能为空")

    # -- 派生 -----------------------------------------------------------

    @property
    def effective_workspace_root(self) -> Path:
        return self.workspace_root if self.workspace_root is not None else self.inbox_dir

    @property
    def realm_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.realms)

    def realm(self, realm_id: str) -> RealmSpec:
        for r in self.realms:
            if r.id == realm_id:
                return r
        raise SpecError(f"spec 里没有 realm: {realm_id!r}")

    # -- 指纹 -----------------------------------------------------------

    def fingerprint(self) -> str:
        """结构指纹（计划 §4.2）：排除身份与 boot。"""
        return fingerprint(
            {
                "model": self.model,
                "prompt_sources": self.prompt_sources,
                "realms": self.realms,
                "toolsets": self.toolsets,
                "limits": self.limits,
            }
        )

    def part_fingerprints(self) -> dict[str, str]:
        """每个可差量替换部件的指纹（``apply()`` 只重建变化的部件）。"""
        return {
            "model": fingerprint(self.model),
            "prompt": fingerprint(self.prompt_sources),
            "realms": fingerprint(self.realms),
            "toolsets": fingerprint(self.toolsets),
            "limits": fingerprint(self.limits),
        }

    # -- 变换 -----------------------------------------------------------

    def with_realms(self, realms: tuple[RealmSpec, ...]) -> "AgentSpec":
        return replace(self, realms=realms)

    def with_toolsets(self, toolsets: Mapping[str, tuple[str, ...]]) -> "AgentSpec":
        return replace(self, toolsets=toolsets)

    def with_limits(self, limits: Limits) -> "AgentSpec":
        return replace(self, limits=limits)

    def with_profile(self, profile: Profile) -> "AgentSpec":
        """profile 语法糖：把 ``profile`` 合并进本 spec（见 ``Profile.expand``）。"""
        if not isinstance(profile, Profile):
            raise SpecError(f"with_profile 需要 Profile，收到 {type(profile).__name__}")
        return profile.expand(self)


__all__ = ["AgentSpec", "BootSpec", "Limits", "Profile", "RealmSpec"]
