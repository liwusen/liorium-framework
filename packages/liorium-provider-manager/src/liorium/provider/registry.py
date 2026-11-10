"""ProviderRegistry：宿主使用的高层门面。

- 把配置解析成 ``ModelProvider``（保持配置顺序）；
- 解析 ``Provider::model`` → :class:`ResolvedModel`（格式、端点、上下文、输出上限、思考能力）；
- 构建模型（同步，不联网）；
- 显式刷新 Provider 模型列表 / 应用 models.dev 目录。

本模块不读产品配置路径、不读 stdin；默认配置发现与交互向导属于宿主
（``liorium-cli``）。``from_env`` 只读取显式传入的环境映射，默认前缀 ``LIORIUM_``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .catalog import enrich_all
from .discovery import auto_load_model_for_provider
from .errors import ProviderConfigError
from .formats import (
    API_FORMATS,
    DEFAULT_FORMAT,
    build_chat_model,
    describe_all_formats,
    detect_url_format,
    normalize_format,
    resolve_route,
)
from .schema import (
    DEFAULT_CONTEXT_LENGTH,
    ModelProvider,
    ModelProviders,
    dumps,
    loads,
    normalize_provider_entries,
    parse_spec,
    prune_model_selection,
)
from .thinking_caps import ThinkingCapability, resolve_thinking_capability


@dataclass(frozen=True)
class ResolvedModel:
    """``provider + model`` 的最终解析结果。"""

    provider: ModelProvider
    model: str
    api_format: str
    base_url: str
    context_window: int
    max_output_tokens: int
    format_source: str
    format_reason: str
    thinking: ThinkingCapability

    @property
    def label(self) -> str:
        return f"{self.provider.name}::{self.model}"

    def describe(self) -> str:
        window = f"{self.context_window:,} tokens" if self.context_window else "上下文未知"
        output = f"{self.max_output_tokens:,}"
        return (
            f"{self.label}  [{self.api_format}]  {self.base_url}  "
            f"({window}, out {output})"
        )


class ProviderRegistry:
    """providers + 默认模型。"""

    def __init__(
        self,
        model_providers: ModelProviders | None = None,
        *,
        source: Path | None = None,
    ) -> None:
        self.model_providers = model_providers or ModelProviders()
        self.source = Path(source) if source is not None else None
        self.providers: dict[str, ModelProvider] = {}
        for provider in self.model_providers.providers:
            if provider.name in self.providers:
                raise ProviderConfigError(f"provider 名字重复: {provider.name!r}")
            self.providers[provider.name] = provider
        self.main_model = self.model_providers.main_model

    # -- 查询 -----------------------------------------------------------

    def provider(self, name: str) -> ModelProvider:
        try:
            return self.providers[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.providers)) or "（空）"
            raise ProviderConfigError(
                f"未知 provider: {name!r}（可用: {known}）"
            ) from exc

    def resolve(
        self,
        label: str,
        *,
        fallback_window: int = DEFAULT_CONTEXT_LENGTH,
        api_format: str | None = None,
    ) -> ResolvedModel:
        """``Provider::model`` / ``model`` → :class:`ResolvedModel`。"""
        text = (label or "").strip()
        if not text:
            raise ProviderConfigError("模型标签为空")
        if "::" in text:
            provider_name, model = text.split("::", 1)
            provider = self.provider(provider_name.strip())
        else:
            provider, model = self._find_model(text)

        model = model.strip()
        if provider.models and model not in provider.models:
            raise ProviderConfigError(
                f"provider {provider.name!r} 没有模型 {model!r}"
                f"（可用: {', '.join(provider.models)}）"
            )

        route_provider = provider
        if api_format is not None:
            fmt = normalize_format(api_format)
            if fmt is None:
                raise ProviderConfigError(
                    f"未知 api 格式: {api_format!r}（支持: {', '.join(API_FORMATS)}）"
                )
            routes = dict(provider.model_api_formats or {})
            routes[model] = fmt
            route_provider = provider.model_copy(update={"model_api_formats": routes})

        decision = resolve_route(route_provider, model)
        thinking = resolve_thinking_capability(provider, model)
        return ResolvedModel(
            provider=provider,
            model=model,
            api_format=decision.api_format,
            base_url=str(decision.base_url or ""),
            context_window=provider.context_window(model, fallback_window),
            max_output_tokens=provider.max_output_tokens(model),
            format_source=decision.source,
            format_reason=decision.reason,
            thinking=thinking,
        )

    def _find_model(self, model: str) -> tuple[ModelProvider, str]:
        matches = [p for p in self.providers.values() if model in p.models]
        if len(matches) == 1:
            return matches[0], model
        if not matches:
            raise ProviderConfigError(
                f"找不到模型 {model!r}；请用 Provider::model 形式，或先 /model 查看可用模型"
            )
        names = ", ".join(p.name for p in matches)
        raise ProviderConfigError(
            f"模型 {model!r} 在多个 provider 里都有（{names}），请写 Provider::model"
        )

    def labels(self) -> list[str]:
        return [f"{p.name}::{m}" for p in self.providers.values() for m in p.models]

    def default_label(self) -> str:
        if self.main_model:
            try:
                return self.resolve(self.main_model).label
            except ProviderConfigError:
                pass
        for provider in self.providers.values():
            if provider.models:
                return f"{provider.name}::{provider.models[0]}"
        raise ProviderConfigError("配置里没有任何模型")

    def to_dict(self) -> dict[str, Any]:
        return self.model_providers.model_dump(mode="json", exclude_none=True)

    def save(self, path: str | Path) -> Path:
        """原子写入配置（0600），返回路径。"""
        return dumps(self.model_providers, path)

    # -- 装载 -----------------------------------------------------------

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, source: Path | None = None
    ) -> "ProviderRegistry":
        normalized = normalize_provider_entries(raw)
        try:
            providers = ModelProviders.model_validate(normalized)
        except Exception as exc:  # pydantic ValidationError
            raise ProviderConfigError(f"provider 配置不合法: {exc}") from exc
        if not providers.providers:
            raise ProviderConfigError("配置里没有任何 provider")
        return cls(providers, source=source)

    @classmethod
    def load(cls, path: str | Path) -> "ProviderRegistry":
        target = Path(path).expanduser()
        providers = loads(target)
        if not providers.providers:
            raise ProviderConfigError(f"配置里没有任何 provider: {target}")
        return cls(providers, source=target)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        prefix: str = "LIORIUM_",
    ) -> "ProviderRegistry | None":
        """用环境变量拼一个 ``env`` provider；缺 base_url/model 时返回 None。"""
        source = os.environ if env is None else env

        def read(name: str, default: str = "") -> str:
            return str(source.get(prefix + name, default) or "").strip()

        base_url = read("BASE_URL").rstrip("/")
        model = read("CHAT_MODEL") or read("MODEL")
        if "::" in model:
            model = model.split("::", 1)[1].strip()
        key = read("CHAT_KEY") or read("API_KEY")
        if not base_url or not model:
            return None
        api_format = (
            normalize_format(read("API_FORMAT"))
            or detect_url_format(base_url)
            or DEFAULT_FORMAT
        )
        raw_window = read("CONTEXT_WINDOW")
        context_window = DEFAULT_CONTEXT_LENGTH
        if raw_window:
            try:
                context_window = int(raw_window.replace(",", "").replace("_", ""))
            except ValueError as exc:
                raise ProviderConfigError(
                    f"{prefix}CONTEXT_WINDOW={raw_window!r} 不是整数"
                ) from exc
        provider = ModelProvider(
            name="env",
            base_url=base_url,
            key=key,
            models=[model],
            model_context_lengths={model: context_window},
            model_api_formats={model: api_format},
            api_format=api_format,
            thinking_type=read("THINKING_TYPE") or "auto",
        )
        providers = ModelProviders(
            providers=[provider], main_model=f"env::{model}"
        )
        return cls(providers)

    # -- 构建 / 刷新 -----------------------------------------------------

    def make_chat_model(
        self,
        resolved: ResolvedModel,
        *,
        intensity: str | None = None,
        temperature: float | None = 0.3,
        purpose: str = "chat",
        timeout: float = 180.0,
        max_retries: int = 2,
        max_tokens: int = 8192,
    ) -> Any:
        """按解析结果构建 LangChain Chat 模型（不联网）。"""
        if not resolved.provider.api_key:
            raise ProviderConfigError(
                f"provider {resolved.provider.name!r} 没有 API key；"
                "请检查配置或删掉配置后重新填写"
            )
        return build_chat_model(
            resolved.provider,
            resolved.model,
            intensity=intensity,
            temperature=temperature,
            purpose=purpose,
            timeout=timeout,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )

    async def refresh_models(
        self,
        names: Iterable[str] | None = None,
        *,
        force: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """从各 Provider ``/models`` 刷新模型列表（显式网络操作）。

        单个 Provider 失败只记录在返回值里，旧列表保持不变。
        """
        wanted = list(names) if names is not None else list(self.providers)
        results: dict[str, dict[str, Any]] = {}
        for name in wanted:
            provider = self.provider(name)
            try:
                models = await auto_load_model_for_provider(provider, force=force)
            except Exception as exc:  # noqa: BLE001 - 汇总给宿主展示
                results[name] = {"ok": False, "error": str(exc)}
                continue
            results[name] = {"ok": True, "models": list(models)}
        prune_model_selection(self.model_providers)
        return results

    def enrich(self, catalog: dict[str, Any]) -> list[dict[str, Any]]:
        """把 models.dev 目录应用到全部 Provider（只改内存）。"""
        return enrich_all(self.model_providers, catalog)

    # -- 兼容辅助 ---------------------------------------------------------

    def try_main_openai_credentials(self) -> tuple[str, str, str] | None:
        """返回主模型的 OpenAI Chat 兼容端点；不是该格式时返回 None。"""
        if not self.main_model:
            return None
        try:
            provider_name, model_name = parse_spec(self.main_model)
        except ProviderConfigError:
            return None
        provider = self.providers.get(provider_name)
        if provider is None:
            return None
        try:
            decision = resolve_route(provider, model_name)
        except ProviderConfigError:
            return None
        if decision.api_format not in ("openai_chat", "openai_responses"):
            return None
        explicit_chat = (provider.format_base_urls or {}).get("openai_chat")
        if explicit_chat:
            from .formats import normalize_format_base_url

            normalized = normalize_format_base_url("openai_chat", explicit_chat)
            if normalized:
                return model_name, provider.key or "", normalized
        return model_name, provider.key or "", decision.base_url or provider.base_url

    def get_main_openai_credentials(self) -> tuple[str, str, str]:
        """同 :meth:`try_main_openai_credentials`，不可用时抛 ``ProviderConfigError``。"""
        credentials = self.try_main_openai_credentials()
        if credentials is None:
            spec = self.main_model or "(未配置)"
            raise ProviderConfigError(
                f"主模型 {spec} 不是 OpenAI Chat 兼容格式，当前功能需要 OpenAI 兼容端点。"
                "请在配置器中为该 Provider 设置 format_base_urls['openai_chat']，"
                "或把主模型指向一个 OpenAI 兼容的 Provider。"
            )
        return credentials

    def get_model_api_format(self, spec: str) -> str:
        """返回 spec 对应的 API 格式；无法解析时按 openai_chat 处理。"""
        try:
            provider_name, model_name = parse_spec(spec)
        except ProviderConfigError:
            return DEFAULT_FORMAT
        provider = self.providers.get(provider_name)
        if provider is None:
            return DEFAULT_FORMAT
        try:
            return resolve_route(provider, model_name).api_format
        except ProviderConfigError:
            return DEFAULT_FORMAT

    def get_provider_decisions(self) -> dict[str, dict[str, dict[str, Any]]]:
        """全部 Provider 的格式决策，供宿主展示。"""
        return describe_all_formats(self.model_providers)


__all__ = ["ProviderRegistry", "ResolvedModel"]
