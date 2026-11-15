"""``build()``：把 ``AgentProfile`` 编译成运行时三件套（计划 F1 / §3.1 / §3.4）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from liorium.agent import AgentSpec, BootSpec, LioriumAgent, Limits, RealmSpec
from liorium.provider import ChatModelPool, ProviderConfigError, ProviderRegistry

from .errors import ProfileBuildError
from .schema import AgentProfile

_FORBIDDEN_PORTS = ("spec", "model_factory", "compact_model_factory", "realm_factories")
"""由 build 决定的参数：宿主传了直接 TypeError，不静默覆盖。"""


@dataclass(frozen=True)
class ProfileBuild:
    """一次编译的产物：宿主可以直接拿三件套，也可以拿一个未 ``start()`` 的 Agent。"""

    profile: AgentProfile
    spec: AgentSpec
    realm_factories: dict[str, Any]
    registry: ProviderRegistry
    pool: ChatModelPool

    def create_agent(self, **ports: Any) -> LioriumAgent:
        """按这份编译结果创建 Agent（未 ``start()``）；``ports`` 转发给 ``LioriumAgent``。"""
        forbidden = [name for name in _FORBIDDEN_PORTS if name in ports]
        if forbidden:
            raise TypeError(
                f"这些参数由 profile.build() 决定，不能覆盖: {', '.join(sorted(forbidden))}"
            )
        return LioriumAgent(
            spec=self.spec,
            model_factory=self.pool.chat,
            compact_model_factory=self.pool.compact,
            realm_factories=self.realm_factories,
            **ports,
        )

    async def close(self) -> None:
        """只关模型池（幂等）。推荐顺序：``agent.close()`` → ``build.close()``。"""
        await self.pool.aclose()


def build_profile(
    profile: AgentProfile,
    *,
    registry: ProviderRegistry | None = None,
    model: str | None = None,
) -> ProfileBuild:
    base = profile.base_dir
    registry = registry if registry is not None else _load_registry(profile, base=base)

    label = model or profile.model
    if not label:
        try:
            label = registry.default_label()
        except ProviderConfigError as exc:
            raise ProfileBuildError(
                "没有可用模型：profile.model 未配置，provider 里也没有 main_model"
            ) from exc
    try:
        resolved = registry.resolve(label)
    except ProviderConfigError as exc:
        raise ProfileBuildError(f"模型不可用: {label!r}（{exc}）") from exc

    limits = _build_limits(profile, resolved)
    workspace = _absolute(base, profile.workspace) if profile.workspace else Path.cwd()
    inbox = (
        _absolute(workspace, profile.inbox)
        if profile.inbox
        else workspace / ".liorium" / "agent"
    )
    realms = tuple(
        RealmSpec(
            id=entry.id,
            config=dict(entry.config),
            requires=profile._realm_requires.get(entry.id, ()),  # noqa: SLF001
        )
        for entry in profile.realms
    )
    spec = AgentSpec(
        id=str(profile.id),
        inbox_dir=inbox,
        model=resolved.label,
        prompt_sources=tuple(profile.prompt),
        realms=realms,
        toolsets={name: tuple(ids) for name, ids in profile.toolsets.items()},
        limits=limits,
        boot=BootSpec(message=profile.boot.message),
        workspace_root=workspace,
    )
    pool = ChatModelPool(
        registry,
        thinking=profile.thinking,
        temperature=profile.temperature,
    )
    return ProfileBuild(
        profile=profile,
        spec=spec,
        realm_factories=dict(profile.realm_factories),
        registry=registry,
        pool=pool,
    )


def _load_registry(profile: AgentProfile, *, base: Path) -> ProviderRegistry:
    raw = profile.provider_file
    if not raw:
        raise ProfileBuildError(
            "没有 provider 配置：给 build(registry=...) 或 profile.provider_file"
        )
    path = _absolute(base, raw)
    if not path.is_file():
        raise ProfileBuildError(f"provider 文件不存在: {path}")
    try:
        return ProviderRegistry.load(path)
    except ProviderConfigError as exc:
        raise ProfileBuildError(f"provider 配置不可用: {path}（{exc}）") from exc


def _build_limits(profile: AgentProfile, resolved: Any) -> Limits:
    values = profile.limits.model_dump()
    if "context_window" not in profile.limits.model_fields_set:
        # 与今天 CLI 的 _build_spec 一致：未显式给出就从 provider 元数据补，
        # 否则压缩阈值会退化（context_window=0）。显式 0 表示"未知"，不覆盖。
        values["context_window"] = int(getattr(resolved, "context_window", 0) or 0)
    try:
        return Limits(**values)
    except Exception as exc:  # noqa: BLE001 - SpecError 等
        raise ProfileBuildError(f"limits 不合法: {exc}") from exc


def _absolute(base: Path, raw: str) -> Path:
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


__all__ = ["ProfileBuild", "build_profile"]
