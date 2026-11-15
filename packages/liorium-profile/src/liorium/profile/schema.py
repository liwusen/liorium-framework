"""Agent Profile 的 Pydantic schema（v1）。

严格性（决策 D2）：``extra="forbid"``、``version`` 必填且必须是 ``1``；profile 会执行
本地代码（realm 的 ``source``），拼错的键必须炸，不能静默变成"配置没生效"。

``LimitsConfig`` 由 :class:`liorium.agent.spec.Limits` **生成**：字段和默认值只有一个
来源，``AgentSpec`` 增删字段时这里自动跟上（``tests/test_schema.py`` 里再钉一次）。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, create_model, field_validator

from liorium.agent.spec import Limits

if TYPE_CHECKING:  # pragma: no cover - 只用于类型
    from liorium.agent import AgentSpec
    from liorium.provider import ProviderRegistry

    from .build import ProfileBuild

THINKING_LEVELS = ("off", "low", "medium", "high")
"""思考强度；``aux`` / ``compact`` 永远不思考，与此无关。"""

BUILTIN_REALMS = frozenset({"subagents"})
"""``source`` 可以省略的内置 realm（工厂由 Agent 自己注册）。"""


def _limits_defaults() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in dataclasses.fields(Limits):
        default = item.default
        out[item.name] = (type(default), Field(default=default))
    return out


LimitsConfig = create_model(
    "LimitsConfig",
    __config__=ConfigDict(extra="forbid"),
    __module__=__name__,
    **{
        name: (annotation, field)
        for name, (annotation, field) in _limits_defaults().items()
    },
)
"""``AgentSpec.limits`` 的镜像；``context_window`` 显式给 ``0`` 表示"未知"。"""

LimitsConfig.__doc__ = "``AgentSpec.limits`` 的 Pydantic 镜像：字段 / 默认值由 dataclass 生成。"


class BootConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = None


class RealmEntry(BaseModel):
    """一条 realm 声明：``id`` + 可选 ``source`` + ``config``。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentProfile(BaseModel):
    """一个 Agent 的装配配方（JSON 的可信映射）。"""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    id: str | None = None
    """``AgentSpec.id``；``load()`` 缺省用文件名 stem，``from_dict()`` 要 ``default_id``。"""
    provider_file: str | None = None
    """providers.json 路径；相对 profile 所在目录。"""
    model: str | None = None
    """``Provider::model`` 标签；``None`` 时 build 用 ``registry.default_label()``。"""
    thinking: Literal["off", "low", "medium", "high"] = "medium"
    temperature: float = 0.3
    workspace: str | None = None
    inbox: str | None = None
    prompt: list[str] = Field(default_factory=list)
    boot: BootConfig = Field(default_factory=BootConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)  # type: ignore[assignment]
    toolsets: dict[str, list[str]] = Field(default_factory=dict)
    realms: list[RealmEntry] = Field(default_factory=list)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    """``load()`` 时定稿：相对路径的基准目录（profile 文件所在目录）。"""
    _realm_factories: dict[str, Any] = PrivateAttr(default_factory=dict)
    """``load()`` 解析出的 ``id -> 类或工厂``（内置 realm 不在里面）。"""
    _realm_requires: dict[str, tuple[str, ...]] = PrivateAttr(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("id 不能是空字符串")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: list[str]) -> list[str]:
        if any(not str(item).strip() for item in value):
            raise ValueError("prompt 里不能有空文件名")
        return [str(item) for item in value]

    def model_post_init(self, __context: Any) -> None:
        seen: set[str] = set()
        for index, realm in enumerate(self.realms):
            if realm.id in seen:
                raise ValueError(f"realms[{index}].id 重复: {realm.id!r}")
            seen.add(realm.id)

    # -- 入口 -----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path, *, env: Any = None) -> "AgentProfile":
        """读一个 profile 文件并完成全部加载期校验（决策 D1）。"""
        from .loader import load

        return load(path, env=env)

    @classmethod
    def from_dict(
        cls,
        raw: Any,
        *,
        base_dir: str | Path | None = None,
        env: Any = None,
        default_id: str | None = None,
    ) -> "AgentProfile":
        """从已解析的 JSON 对象加载（测试 / 配置存在数据库里的宿主用）。"""
        from .loader import from_dict

        return from_dict(raw, base_dir=base_dir, env=env, default_id=default_id)

    # -- 派生 -----------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def realm_factories(self) -> dict[str, Any]:
        """``load()`` 解析出的工厂表（未 load 的 profile 是空 dict）。"""
        return dict(self._realm_factories)

    def realm_ids(self) -> tuple[str, ...]:
        return tuple(realm.id for realm in self.realms)

    # -- 出口 -----------------------------------------------------------

    def build(
        self,
        *,
        registry: "ProviderRegistry | None" = None,
        model: str | None = None,
    ) -> "ProfileBuild":
        """编译成 ``AgentSpec`` + realm 工厂 + 模型池（计划 F1）。"""
        from .build import build_profile

        return build_profile(self, registry=registry, model=model)

    def build_agent(
        self,
        *,
        registry: "ProviderRegistry | None" = None,
        model: str | None = None,
        **ports: Any,
    ) -> Any:
        """便利封装：``build().create_agent(**ports)``（返回未 ``start()`` 的 Agent）。"""
        return self.build(registry=registry, model=model).create_agent(**ports)


__all__ = [
    "AgentProfile",
    "BUILTIN_REALMS",
    "BootConfig",
    "LimitsConfig",
    "RealmEntry",
    "THINKING_LEVELS",
]
