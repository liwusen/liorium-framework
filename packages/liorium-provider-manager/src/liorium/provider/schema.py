"""Provider 配置 schema 与持久化。

字段与 ``providers.json`` 兼容：既有配置可以直接读，不需要迁移。
所有 ``model_*`` 字典/路由都原样保留；未知字段通过 ``extra="allow"`` 保留并在保存时
写回，避免上游（上游）新增字段被本库静默丢弃。

本模块**不读任何产品配置路径**：``loads`` / ``dumps`` 只接受显式路径。默认配置发现、
环境变量构造、交互向导都属于宿主（``liorium-cli``）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ProviderConfigError

DEFAULT_CONTEXT_LENGTH = 128_000
"""模型未配置上下文长度时的默认值（DeepSeek/Qwen 等主流模型均为 128k 量级）。"""

DEFAULT_MAX_OUTPUT_TOKENS = 8_192
"""模型未配置最大输出 token 时的安全默认值。"""


class ModelProvider(BaseModel):
    """一个 Provider 及其模型清单（与 ``ModelProvider`` 同 schema）。"""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    name: str
    base_url: str
    key: str | None = None
    models: list[str] = Field(default_factory=list)

    # ── 模型限制 ──
    model_context_lengths: dict[str, int] = Field(default_factory=dict)
    model_max_output_tokens: dict[str, int] = Field(default_factory=dict)

    # ── 思考 ──
    #: OpenAI Chat 的方言：auto / openai / qwen / deepseek / glm / mimo / minimax / none
    thinking_type: str = "auto"
    #: 模型 → 用户手动指定的思考形状：effort / budget / toggle / none
    model_thinking_modes: dict[str, str] = Field(default_factory=dict)
    #: 模型 → Provider ``/models`` 自报的思考能力（规范化后的 dict）
    model_thinking_caps: dict[str, dict] = Field(default_factory=dict)

    # ── 格式 ──
    #: Provider 级默认格式：auto / openai_chat / openai_responses /
    #: anthropic_messages / google_gemini
    api_format: str = "auto"
    #: 单模型显式覆盖（只存用户显式设置，自动推断结果不落盘到这里）
    model_api_formats: dict[str, str] = Field(default_factory=dict)
    #: 格式 → SDK 根地址；覆盖目录与推导结果
    format_base_urls: dict[str, str] = Field(default_factory=dict)
    #: 模型 → ``/models`` 上报的格式能力声明（规范化后）
    model_api_capabilities: dict[str, dict] = Field(default_factory=dict)

    # ── models.dev 目录（持久化缓存，构建模型时无需联网） ──
    #: 匹配到的 models.dev provider id；None = 自动匹配，用户可 pin
    catalog_provider_id: str | None = None
    #: model → {format, base_url, host_matched, npm, shape, reasoning, thinking}
    model_catalog_routes: dict[str, dict] = Field(default_factory=dict)
    #: 上次用目录刷新本 Provider 的时间（epoch 秒）
    catalog_synced_at: float | None = None

    # ── 其它 ──
    #: OpenCode Go 订阅适配：请求带稳定的 ``x-opencode-session`` 头
    opencode_go: bool = False
    #: 模型 → 用户显式启用的服务端工具名（web_search）
    model_server_tools: dict[str, list[str]] = Field(default_factory=dict)
    #: 流式是否请求用量统计；None = 按格式默认
    stream_usage: bool | None = None

    # -- 兼容/便捷访问 ---------------------------------------------------

    @property
    def api_key(self) -> str:
        """``key`` 的别名（宿主侧代码常用 ``api_key``）。"""
        return self.key or ""

    def context_window(self, model: str, fallback: int = 0) -> int:
        """模型的上下文长度（token）；未配置时返回 ``fallback``。"""
        value = (self.model_context_lengths or {}).get(model)
        if isinstance(value, int) and value > 0:
            return value
        return int(fallback)

    def max_output_tokens(
        self, model: str, fallback: int = DEFAULT_MAX_OUTPUT_TOKENS
    ) -> int:
        """模型的最大输出 token；未配置时返回 ``fallback``。"""
        value = (self.model_max_output_tokens or {}).get(model)
        if isinstance(value, int) and value > 0:
            return value
        return int(fallback)


class ModelProviders(BaseModel):
    """全部 Provider + 主模型 / 子代理模型选择。"""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    providers: list[ModelProvider] = Field(default_factory=list)
    main_model: str | None = None
    subagent_models: list[str] | None = None

    def find(self, name: str) -> ModelProvider | None:
        """按名字找 Provider（找不到返回 None）。"""
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def loads(path: str | Path) -> ModelProviders:
    """读取 provider 配置；不存在/非法 JSON/结构错误抛 ``ProviderConfigError``。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise ProviderConfigError(f"provider 配置不存在: {target}")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProviderConfigError(f"provider 配置不是合法 JSON: {target}（{exc}）") from exc
    if not isinstance(raw, Mapping):
        raise ProviderConfigError(f"provider 配置顶层必须是对象: {target}")
    try:
        return ModelProviders.model_validate(raw)
    except ValidationError as exc:
        raise ProviderConfigError(f"provider 配置不合法: {target}（{exc}）") from exc


def dumps(providers: ModelProviders, path: str | Path) -> Path:
    """原子写入 provider 配置（临时文件 + ``os.replace``；权限 0600）。

    返回写入路径。写盘失败直接抛 OSError —— 调用方（配置器/向导）必须知道配置
    没有落盘，不能假装成功。
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        providers.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        indent=2,
    )
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


# ---------------------------------------------------------------------------
# 查询 / 选择
# ---------------------------------------------------------------------------


def parse_spec(spec: str) -> tuple[str, str]:
    """解析 ``provider::model`` 为 ``(provider_name, model_name)``。"""
    text = str(spec or "").strip()
    if "::" not in text:
        raise ProviderConfigError(
            f"Invalid model spec '{spec}', expected 'provider::model'."
        )
    provider_name, model_name = text.split("::", 1)
    provider_name, model_name = provider_name.strip(), model_name.strip()
    if not provider_name or not model_name:
        raise ProviderConfigError(
            f"Invalid model spec '{spec}', expected 'provider::model'."
        )
    return provider_name, model_name


def resolve_provider(providers: ModelProviders, provider_name: str) -> ModelProvider:
    """按名称查找 Provider，找不到抛 ``ProviderConfigError``。"""
    provider = providers.find(provider_name)
    if provider is None:
        known = ", ".join(p.name for p in providers.providers) or "（空）"
        raise ProviderConfigError(
            f"Provider '{provider_name}' not found.（可用: {known}）"
        )
    return provider


def new_provider(
    providers: ModelProviders,
    name: str,
    base_url: str,
    key: str | None = None,
) -> ModelProvider:
    """新建 Provider 并追加到集合。"""
    provider = ModelProvider(name=name, base_url=base_url, key=key)
    providers.providers.append(provider)
    return provider


def remove_provider(providers: ModelProviders, name: str) -> bool:
    """按名字删除 Provider；找到并删除返回 True。"""
    for index, provider in enumerate(providers.providers):
        if provider.name == name:
            del providers.providers[index]
            return True
    return False


def remove_model_from_provider(
    providers: ModelProviders, provider_name: str, model_name: str
) -> bool:
    """从指定 Provider 删除一个模型；找到并删除返回 True。"""
    provider = providers.find(provider_name)
    if provider is None or model_name not in provider.models:
        return False
    provider.models.remove(model_name)
    return True


def get_main_provider(providers: ModelProviders) -> ModelProvider:
    """返回 ``main_model`` 对应的 Provider；未配置时抛 ``ProviderConfigError``。"""
    if not providers.main_model:
        raise ProviderConfigError("main_model is not configured.")
    provider_name, _ = parse_spec(providers.main_model)
    return resolve_provider(providers, provider_name)


def get_main_credentials(providers: ModelProviders) -> tuple[str, str, str]:
    """返回 ``(model_name, api_key, base_url)``；未配置时返回 ``("", "", "")``。"""
    if not providers.main_model:
        return "", "", ""
    try:
        provider_name, model_name = parse_spec(providers.main_model)
    except ProviderConfigError:
        return "", "", ""
    provider = providers.find(provider_name)
    if provider is None:
        return "", "", ""
    return model_name, provider.key or "", provider.base_url


def get_context_length(providers: ModelProviders, spec: str) -> int:
    """返回 spec 对应模型的上下文长度；无法解析时回退 ``DEFAULT_CONTEXT_LENGTH``。"""
    try:
        provider_name, model_name = parse_spec(spec)
    except ProviderConfigError:
        return DEFAULT_CONTEXT_LENGTH
    provider = providers.find(provider_name)
    if provider is None:
        return DEFAULT_CONTEXT_LENGTH
    return provider.context_window(model_name, DEFAULT_CONTEXT_LENGTH)


def is_model_spec_usable(providers: ModelProviders, spec: str) -> bool:
    """spec 指向的 Provider 存在，且已加载模型时模型在列表里。

    ``provider.models`` 为空表示「还没拉过模型」（新建 / 离线 / 旧配置迁移），
    此时不做判定，避免首次配置被整份误删。
    """
    try:
        provider_name, model_name = parse_spec(spec)
    except ProviderConfigError:
        return False
    provider = providers.find(provider_name)
    if provider is None:
        return False
    return not provider.models or model_name in provider.models


def prune_model_selection(providers: ModelProviders) -> list[str]:
    """剔除 ``subagent_models`` 里已不存在的 Provider/模型，返回被剔除项。"""
    kept: list[str] = []
    dropped: list[str] = []
    for raw in providers.subagent_models or []:
        spec = str(raw or "").strip()
        if not spec:
            continue
        if is_model_spec_usable(providers, spec) and spec not in kept:
            kept.append(spec)
        else:
            dropped.append(spec)
    if dropped:
        providers.subagent_models = kept or None
    return dropped


def get_default_subagent_model(providers: ModelProviders) -> str:
    """默认子代理模型：``subagent_models`` 第一个可用项，否则 ``main_model``。"""
    for raw in providers.subagent_models or []:
        spec = str(raw or "").strip()
        if spec and is_model_spec_usable(providers, spec):
            return spec
    if providers.main_model:
        return providers.main_model
    raise ProviderConfigError(
        "No subagent model configured: set subagent_models or main_model."
    )


def is_subagent_model_allowed(providers: ModelProviders, spec: str) -> bool:
    """spec 是否在子代理白名单里（或等于主模型）。"""
    if spec in (providers.subagent_models or []):
        return True
    return spec == providers.main_model


def normalize_provider_entries(raw: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规范化顶层配置的 ``providers`` 条目（兼容 ``api_key`` 别名）。

    只做宿主需要立即反馈的字段校验；其余交给 pydantic。返回可安全
    ``model_validate`` 的副本。
    """
    entries = raw.get("providers")
    if entries is None:
        entries = []
    if not isinstance(entries, (list, tuple)):
        raise ProviderConfigError(f"providers 必须是数组，收到 {type(entries).__name__}")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ProviderConfigError(f"providers 里出现非对象条目: {entry!r}")
        item = dict(entry)
        name = str(item.get("name") or "").strip()
        if not name:
            raise ProviderConfigError("provider 缺少 name")
        base_url = str(item.get("base_url") or "").strip()
        if not base_url:
            raise ProviderConfigError(f"provider {name!r} 缺少 base_url")
        if not item.get("key") and item.get("api_key"):
            item["key"] = item.get("api_key")
        item.pop("api_key", None)
        item["name"] = name
        item["base_url"] = base_url
        models = item.get("models") or []
        if isinstance(models, (list, tuple)):
            item["models"] = [str(m).strip() for m in models if str(m).strip()]
        normalized.append(item)
    return {**raw, "providers": normalized}


__all__ = [
    "DEFAULT_CONTEXT_LENGTH",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ModelProvider",
    "ModelProviders",
    "dumps",
    "get_context_length",
    "get_default_subagent_model",
    "get_main_credentials",
    "get_main_provider",
    "is_model_spec_usable",
    "is_subagent_model_allowed",
    "loads",
    "new_provider",
    "normalize_provider_entries",
    "parse_spec",
    "prune_model_selection",
    "remove_model_from_provider",
    "remove_provider",
    "resolve_provider",
]
