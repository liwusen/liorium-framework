"""四种 LLM API 格式的解析与模型构建（不包含任何具体 Provider 的模型表）。

支持的格式：

- ``openai_chat``：OpenAI Chat Completions（含所有 OpenAI 兼容网关）
- ``openai_responses``：OpenAI Responses API（无状态，``store=False``）
- ``anthropic_messages``：Anthropic Messages API
- ``google_gemini``：Google Gemini generateContent

格式来源的优先级（高 → 低）：

1. ``provider.model_api_formats[model]``（用户显式覆盖）
2. ``provider.api_format``（Provider 级声明，非 auto）
3. ``provider.model_catalog_routes[model]``（models.dev 目录推断）
4. base_url 的路径启发式（``/anthropic``、``/messages``、``/google``、``/v1beta``、``/responses``）
5. 多格式 Provider 的模型族回退（仅当目录里确实出现 ≥2 种格式时）
6. ``openai_chat``（最终兜底，附 warning）

端点来源：``provider.format_base_urls[format]`` > 目录路由（仅 host 相同时）>
``provider.base_url``。显式指定了格式却没有可用端点时**报错**，绝不静默把请求
发到别的主机。

思考参数的形状由 ``thinking_caps`` 解析；具体参数由 ``thinking`` 翻译：
手动覆盖 > Provider ``/models`` 自报 > models.dev 目录 > 模型名启发式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

from liorium.core.log import get_logger

from .constants import DEFAULT_USER_AGENT, common_headers
from .errors import ProviderConfigError
from .thinking import (
    ReasoningChatOpenAI,
    get_format_thinking_params,
    get_thinking_params,
    normalize_intensity,
    resolve_thinking_dialect,
)
from .thinking_caps import (
    ThinkingCapability,
    ThinkingOption,
    describe_thinking,
    resolve_thinking_capability,
)

log = get_logger("provider.formats")

API_FORMATS = (
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
    "google_gemini",
)
SUPPORTED_FORMATS = API_FORMATS

FORMAT_LABELS = {
    "openai_chat": "OpenAI Chat Completions",
    "openai_responses": "OpenAI Responses",
    "anthropic_messages": "Anthropic Messages",
    "google_gemini": "Google Gemini",
}

DEFAULT_FORMAT = "openai_chat"

_FORMAT_ALIASES = {
    "openai_chat": "openai_chat",
    "openai": "openai_chat",
    "chat": "openai_chat",
    "completions": "openai_chat",
    "openai_responses": "openai_responses",
    "responses": "openai_responses",
    "anthropic": "anthropic_messages",
    "anthropic_messages": "anthropic_messages",
    "claude": "anthropic_messages",
    "messages": "anthropic_messages",
    "gemini": "google_gemini",
    "google": "google_gemini",
    "google_gemini": "google_gemini",
    "generativeai": "google_gemini",
}

# 默认走 Responses API 的 SDK 包（只有当 shape 不是 completions 时成立）
_RESPONSES_NPM = {"@ai-sdk/openai", "@ai-sdk/azure", "@ai-sdk/xai"}

# 模型族回退：仅在「目录里该 Provider 出现 ≥2 种格式」时启用，
# 属于通用命名规则，不是某个 Provider 的模型表。
_FAMILY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^claude"), "anthropic_messages"),
    (re.compile(r"^gemini"), "google_gemini"),
    (re.compile(r"^(gpt-5|gpt-6|o3|o4|codex)"), "openai_responses"),
)

_ANTHROPIC_SUFFIXES = ("/v1/messages", "/messages", "/v1")
_GOOGLE_SUFFIXES = (
    "/v1beta/models",
    "/v1/models",
    "/v1beta",
    "/v1",
)
_OPENAI_SUFFIXES = ("/chat/completions", "/responses")


@dataclass(frozen=True)
class FormatDecision:
    """一个模型最终的格式决策（含来源与端点）。"""

    api_format: str
    source: str  # manual | provider_default | catalog | url_path | family | fallback
    reason: str
    base_url: str | None


def normalize_format(raw: str | None) -> str | None:
    """把用户输入的格式别名规范成四种 canonical 格式之一。"""
    if not raw:
        return None
    key = str(raw).strip().lower().replace("-", "_")
    return _FORMAT_ALIASES.get(key)


# ── 通用协议推断 ──


def detect_api_format(*, npm: str = "", shape: str = "", api: str = "") -> str:
    """按 npm 包名 / shape / 端点路径与主机推断 API 格式（通用规则，无 Provider 特判）。"""
    npm_l = str(npm or "").lower()
    shape_l = str(shape or "").lower()
    path = _url_path(api)
    host = _url_host(api)

    if "anthropic" in npm_l or "anthropic" in host or "/anthropic" in path or "/messages" in path:
        return "anthropic_messages"
    if (
        "google" in npm_l
        or "vertex" in npm_l
        or "googleapis" in host
        or "generativelanguage" in host
        or "/google" in path
        or "/v1beta" in path
    ):
        return "google_gemini"
    if shape_l == "responses":
        return "openai_responses"
    if shape_l == "completions":
        return "openai_chat"
    if npm_l in _RESPONSES_NPM:
        return "openai_responses"
    if path.rstrip("/").endswith("/responses"):
        return "openai_responses"
    return "openai_chat"


def detect_url_format(base_url: str) -> str | None:
    """只看端点路径/主机的格式启发式；无明确信号时返回 None。"""
    path = _url_path(base_url)
    host = _url_host(base_url)
    if "anthropic" in host or "/anthropic" in path or path.rstrip("/").endswith("/messages"):
        return "anthropic_messages"
    if (
        "googleapis" in host
        or "generativelanguage" in host
        or "/google" in path
        or "/v1beta" in path
    ):
        return "google_gemini"
    if path.rstrip("/").endswith("/responses"):
        return "openai_responses"
    return None


def _url_host(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        return (urlparse(text).hostname or "").lower()
    except ValueError:
        return ""


def _url_path(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        return (urlparse(text).path or "").lower()
    except ValueError:
        return ""


def normalize_format_base_url(api_format: str, url: str) -> str | None:
    """把端点归一化成对应 SDK 期望的「根地址」。

    - Anthropic：去掉尾部 ``/v1/messages``、``/messages``、``/v1``（SDK 自己追加）；
    - Google：去掉尾部 ``/v1beta/models/...``、``/v1beta``、``/v1``（SDK 自己追加）；
    - OpenAI：去掉尾部 ``/chat/completions``、``/responses``，**保留** ``/v1``。

    非法/非 http(s) 地址返回 None（调用方据此报错或回退），不静默使用。
    """
    text = str(url or "").strip()
    if not text:
        return None
    if "//" not in text:
        text = "//" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    path = (parsed.path or "").rstrip("/")
    if api_format == "anthropic_messages":
        suffixes = _ANTHROPIC_SUFFIXES
    elif api_format == "google_gemini":
        suffixes = _GOOGLE_SUFFIXES
        # `/v1beta/models/<model>:generateContent` 这类完整资源路径要截到 /models 之前
        lowered = path.lower()
        for marker in ("/v1beta/models/", "/v1/models/"):
            index = lowered.find(marker)
            if index >= 0:
                path = path[:index]
                break
    else:
        suffixes = _OPENAI_SUFFIXES
    changed = True
    while changed and path:
        changed = False
        lowered = path.lower()
        for suffix in suffixes:
            if lowered.endswith(suffix):
                path = path[: -len(suffix)].rstrip("/")
                changed = True
                break
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def family_format(model: str) -> str | None:
    """按模型名前缀给出通用族回退格式（claude / gemini / gpt-5 等）。"""
    name = str(model or "").strip().lower()
    for pattern, api_format in _FAMILY_PATTERNS:
        if pattern.search(name):
            return api_format
    return None


def _catalog_format(route: Any) -> str | None:
    if not isinstance(route, dict):
        return None
    fmt = str(route.get("format") or "")
    return fmt if fmt in API_FORMATS else None


def is_multi_format(provider: Any) -> bool:
    """目录里是否出现 ≥2 种格式（多格式聚合 Provider 的判定依据）。"""
    routes = getattr(provider, "model_catalog_routes", None) or {}
    formats = {_catalog_format(route) for route in routes.values()}
    formats.discard(None)
    return len(formats) >= 2


def resolve_route(provider: Any, model: str) -> FormatDecision:
    """决定 ``model`` 使用哪种 API 格式与端点（见模块 docstring 的优先级）。"""
    catalog_route = (getattr(provider, "model_catalog_routes", None) or {}).get(model)
    manual = (getattr(provider, "model_api_formats", None) or {}).get(model)
    provider_default = str(getattr(provider, "api_format", "auto") or "auto")
    base_url = str(getattr(provider, "base_url", "") or "")

    if manual in API_FORMATS:
        return _decision(
            provider,
            model,
            manual,
            source="manual",
            reason="用户为此模型显式指定格式",
            catalog_route=catalog_route,
        )

    if provider_default in API_FORMATS:
        return _decision(
            provider,
            model,
            provider_default,
            source="provider_default",
            reason="Provider 级 api_format 声明",
            catalog_route=catalog_route,
        )

    catalog_format = _catalog_format(catalog_route)
    if catalog_format:
        return _decision(
            provider,
            model,
            catalog_format,
            source="catalog",
            reason="models.dev 目录推断",
            catalog_route=catalog_route,
        )

    url_format = detect_url_format(base_url)
    if url_format:
        return _decision(
            provider,
            model,
            url_format,
            source="url_path",
            reason="base_url 路径表明协议",
            catalog_route=None,
        )

    if is_multi_format(provider):
        guessed = family_format(model)
        if guessed:
            return _decision(
                provider,
                model,
                guessed,
                source="family",
                reason="目录显示该 Provider 支持多种格式，按模型族回退",
                catalog_route=None,
            )

    log.warning(
        "模型 '%s'（provider=%s）无法确定 API 格式，回退到 openai_chat",
        model,
        getattr(provider, "name", ""),
    )
    return _decision(
        provider,
        model,
        "openai_chat",
        source="fallback",
        reason="无任何格式线索，回退 OpenAI Chat Completions",
        catalog_route=None,
    )


def _decision(
    provider: Any,
    model: str,
    api_format: str,
    *,
    source: str,
    reason: str,
    catalog_route: Any,
) -> FormatDecision:
    base_url = _resolve_base_url(provider, api_format, catalog_route=catalog_route)
    if base_url is None:
        if source == "fallback":
            raise ProviderConfigError(
                f"Provider '{getattr(provider, 'name', '')}' 没有可用的 base_url，"
                f"无法为模型 '{model}' 构建 LLM"
            )
        # auto 路径：目标格式没有可用端点 → 退化为 openai_chat（显式格式仍会报错）
        if source not in ("manual", "provider_default"):
            log.warning(
                "模型 '%s' 的目标格式 %s 没有可用端点，回退 openai_chat",
                model,
                api_format,
            )
            return FormatDecision(
                api_format="openai_chat",
                source="fallback",
                reason=f"{reason}（端点不可用，回退 chat）",
                base_url=_resolve_base_url(provider, "openai_chat", catalog_route=None),
            )
        raise ProviderConfigError(
            f"Provider '{getattr(provider, 'name', '')}' 的格式 {api_format} 没有可用端点："
            "请设置 format_base_urls 或 provider.base_url"
        )
    return FormatDecision(
        api_format=api_format, source=source, reason=reason, base_url=base_url
    )


def _resolve_base_url(
    provider: Any, api_format: str, *, catalog_route: Any
) -> str | None:
    explicit = (getattr(provider, "format_base_urls", None) or {}).get(api_format)
    if explicit:
        normalized = normalize_format_base_url(api_format, explicit)
        if normalized is None:
            raise ProviderConfigError(
                f"Provider '{getattr(provider, 'name', '')}' 的 format_base_urls"
                f"[{api_format}] 不是合法的 http(s) 地址: {explicit}"
            )
        return normalized
    # 目录端点只对「同一种格式 + host 匹配」有效：
    # - 用户手动把格式改成别的时，目录端点是另一种协议的路径，用了必然 404；
    # - host 不匹配时（只按模型名匹配到目录），请求不能发到用户没配置密钥的主机。
    if (
        isinstance(catalog_route, dict)
        and catalog_route.get("host_matched")
        and _catalog_format(catalog_route) == api_format
    ):
        catalog_base = catalog_route.get("base_url")
        if catalog_base:
            normalized = normalize_format_base_url(api_format, str(catalog_base))
            if normalized:
                return normalized
    return normalize_format_base_url(
        api_format, str(getattr(provider, "base_url", "") or "")
    )


def describe_provider_formats(provider: Any) -> dict[str, dict[str, Any]]:
    """返回该 Provider 每个模型的格式决策（管理端/宿主展示用）。

    同时给出思考参数形状的解析结果（``thinking``），便于用户判断自动推断是否可靠。
    """
    result: dict[str, dict[str, Any]] = {}
    for model in getattr(provider, "models", None) or []:
        try:
            decision = resolve_route(provider, model)
        except ProviderConfigError as exc:
            result[model] = {
                "api_format": None,
                "source": "error",
                "reason": str(exc),
                "base_url": None,
                "thinking": None,
            }
            continue
        thinking = None
        if resolve_thinking_dialect(provider, model) != "none":
            thinking = describe_thinking(
                resolve_thinking_capability(provider, model),
                decision.api_format,
                model=model,
            )
        result[model] = {
            "api_format": decision.api_format,
            "source": decision.source,
            "reason": decision.reason,
            "base_url": decision.base_url,
            "thinking": thinking,
        }
    return result


def describe_all_formats(providers: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """全部 Provider 的格式决策：``{provider_name: {model: decision}}``。"""
    return {
        provider.name: describe_provider_formats(provider)
        for provider in (getattr(providers, "providers", None) or [])
    }


# ── 服务端（Provider 托管）工具规格 ──

SERVER_TOOL_FORMATS = {
    "web_search": {
        "openai_responses": {"type": "web_search"},
        "anthropic_messages": {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        },
        "google_gemini": {"google_search": {}},
    },
}


def build_server_tool_specs(api_format: str, names: Iterable[str]) -> list[dict[str, Any]]:
    """把工具名翻译成对应格式的 provider-hosted 工具规格。

    ``openai_chat`` 没有服务端工具概念：显式要求时直接报错，不静默忽略。
    """
    specs: list[dict[str, Any]] = []
    for name in names or ():
        target = str(name or "").strip()
        if not target:
            continue
        mapping = SERVER_TOOL_FORMATS.get(target)
        if mapping is None:
            raise ProviderConfigError(f"未知的服务端工具: {target}")
        spec = mapping.get(api_format)
        if spec is None:
            raise ProviderConfigError(
                f"API 格式 {api_format} 不支持服务端工具 '{target}'"
                f"（支持: {', '.join(sorted(mapping))}）"
            )
        specs.append(dict(spec))
    return specs


def supported_server_tools(api_format: str) -> list[str]:
    """该格式支持的服务端工具名（UI 勾选依据）。"""
    return [
        name
        for name, mapping in SERVER_TOOL_FORMATS.items()
        if api_format in mapping
    ]


# ── 模型构建 ──


def _build_context(
    provider: Any, model: str, api_format: str, intensity: str | None
) -> tuple[ThinkingCapability, ThinkingOption | None, str, str | None]:
    """解析一次构建需要的思考上下文。

    返回 ``(capability, option, dialect, effective_intensity)``。
    """
    dialect = resolve_thinking_dialect(provider, model)
    level = normalize_intensity(intensity)
    if level is None or dialect == "none":
        return ThinkingCapability(), None, dialect, None
    capability = resolve_thinking_capability(provider, model)
    if capability.reasoning is False:
        log.info(
            "模型 '%s' 标注为不支持推理（来源: %s），本次不启用思考参数",
            model,
            capability.source or "unknown",
        )
        return capability, None, dialect, None
    option = capability.option_for(api_format, model=model)
    log.debug(
        "模型 '%s' 思考参数: format=%s dialect=%s mode=%s source=%s",
        model,
        api_format,
        dialect,
        option.mode if option is not None else "preset",
        capability.source or "none",
    )
    return capability, option, dialect, level


def build_chat_model(
    provider: Any,
    model: str,
    *,
    intensity: str | None = None,
    temperature: float | None = 0.3,
    timeout: float = 180.0,
    max_retries: int = 2,
    max_tokens: int = 8192,
    purpose: str = "chat",
    user_agent: str = DEFAULT_USER_AGENT,
) -> Any:
    """按 provider 配置构建对应格式的 LangChain Chat 模型（同步、不联网）。

    Args:
        provider: ``ModelProvider``（或其他同字段对象）。
        model: 模型名。
        intensity: ``low`` / ``medium`` / ``high``；``None`` / ``off`` 表示不启用。
        temperature: 采样温度；**思考开启时不发送**（部分协议不允许它与 thinking 共存）。
        timeout: 请求超时（秒）。
        max_retries: SDK 重试次数。
        max_tokens: Anthropic / Gemini 的输出上限。
        purpose: ``chat`` 启用思考；``aux`` / ``compact`` 强制不思考。
        user_agent: 请求 UA；``opencode_go`` 时另加 session 头。
    """
    decision = resolve_route(provider, model)
    capability, option, dialect, level = _build_context(
        provider, model, decision.api_format, intensity
    )
    if purpose != "chat":
        level = None
    if capability.reasoning is False:
        level = None

    kwargs: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "decision": decision,
        "intensity": level,
        "option": option,
        "dialect": dialect,
        "temperature": None if level is not None else temperature,
        "timeout": timeout,
        "max_retries": max_retries,
        "max_tokens": max_tokens,
        "purpose": purpose,
        "user_agent": user_agent,
    }
    if decision.api_format == "openai_responses":
        return _build_openai_responses(**kwargs)
    if decision.api_format == "anthropic_messages":
        return _build_anthropic(**kwargs)
    if decision.api_format == "google_gemini":
        return _build_google(**kwargs)
    return _build_openai_chat(**kwargs)


def _headers(provider: Any, user_agent: str) -> dict[str, str]:
    return common_headers(
        user_agent=user_agent,
        opencode_go=bool(getattr(provider, "opencode_go", False)),
    )


def _stream_usage(provider: Any, *, default: bool = True) -> bool:
    configured = getattr(provider, "stream_usage", None)
    if configured is None:
        return default
    return bool(configured)


def _openai_kwargs(
    provider: Any,
    model: str,
    decision: FormatDecision,
    *,
    temperature: float | None,
    timeout: float,
    max_retries: int,
    user_agent: str,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": provider.api_key,
        "base_url": decision.base_url,
        "timeout": timeout,
        "max_retries": max_retries,
        "default_headers": _headers(provider, user_agent),
        "stream_usage": _stream_usage(provider),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def _build_openai_chat(
    provider: Any,
    model: str,
    decision: FormatDecision,
    intensity: str | None,
    option: ThinkingOption | None,
    dialect: str,
    temperature: float | None,
    timeout: float,
    max_retries: int,
    max_tokens: int,
    purpose: str,
    user_agent: str,
    **_unused: Any,
) -> Any:
    from langchain_openai import ChatOpenAI

    kwargs = _openai_kwargs(
        provider,
        model,
        decision,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        user_agent=user_agent,
    )
    if intensity is not None:
        thinking_params = get_thinking_params(dialect, intensity)
        if "reasoning_effort" in thinking_params:
            kwargs["reasoning_effort"] = thinking_params.pop("reasoning_effort")
        model_kw = thinking_params.pop("model_kwargs", {})
        extra = {**thinking_params.pop("extra_body", {}), **kwargs.get("extra_body", {})}
        if extra:
            kwargs["extra_body"] = extra
        kwargs["model_kwargs"] = {**kwargs.get("model_kwargs", {}), **model_kw}
        return ReasoningChatOpenAI(**kwargs)
    return ChatOpenAI(**kwargs)


def _build_openai_responses(
    provider: Any,
    model: str,
    decision: FormatDecision,
    intensity: str | None,
    option: ThinkingOption | None,
    dialect: str,
    temperature: float | None,
    timeout: float,
    max_retries: int,
    max_tokens: int,
    purpose: str,
    user_agent: str,
    **_unused: Any,
) -> Any:
    from langchain_openai import ChatOpenAI

    kwargs = _openai_kwargs(
        provider,
        model,
        decision,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        user_agent=user_agent,
    )
    kwargs.update(
        {
            "use_responses_api": True,
            "output_version": "responses/v1",
            # 无状态：不依赖 previous_response_id，也不把对话存到 provider 侧。
            "store": False,
            "use_previous_response_id": False,
        }
    )
    if intensity is not None:
        kwargs.update(
            get_format_thinking_params(
                "openai_responses", dialect, intensity, option=option
            )
        )
    return ChatOpenAI(**kwargs)


def _anthropic_max_tokens(provider: Any, model: str, *, purpose: str, budget: int) -> int:
    """Anthropic 必须显式给 ``max_tokens``（SDK 默认 1024 会截断长回答/摘要）。"""
    cap = int((getattr(provider, "model_max_output_tokens", None) or {}).get(model) or 8192)
    if purpose == "compact":
        return max(4096, min(cap, 8192))
    if budget > 0:
        return max(1024, min(cap, budget + 1024))
    return cap


def _build_anthropic(
    provider: Any,
    model: str,
    decision: FormatDecision,
    intensity: str | None,
    option: ThinkingOption | None,
    dialect: str,
    temperature: float | None,
    timeout: float,
    max_retries: int,
    max_tokens: int,
    purpose: str,
    user_agent: str,
    **_unused: Any,
) -> Any:
    from langchain_anthropic import ChatAnthropic

    budget = 0
    thinking_params: dict[str, Any] = {}
    if intensity is not None:
        thinking_params = get_format_thinking_params(
            "anthropic_messages", dialect, intensity, option=option
        )
        thinking = thinking_params.get("thinking")
        if isinstance(thinking, dict):
            budget = int(thinking.get("budget_tokens") or 0)
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": provider.api_key,
        "base_url": decision.base_url,
        "max_tokens": _anthropic_max_tokens(
            provider, model, purpose=purpose, budget=budget
        ),
        "timeout": timeout,
        "max_retries": max_retries,
        "default_headers": _headers(provider, user_agent),
        "stream_usage": _stream_usage(provider),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    kwargs.update(thinking_params)
    return ChatAnthropic(**kwargs)


def _build_google(
    provider: Any,
    model: str,
    decision: FormatDecision,
    intensity: str | None,
    option: ThinkingOption | None,
    dialect: str,
    temperature: float | None,
    timeout: float,
    max_retries: int,
    max_tokens: int,
    purpose: str,
    user_agent: str,
    **_unused: Any,
) -> Any:
    from langchain_google_genai import ChatGoogleGenerativeAI

    cap = int((getattr(provider, "model_max_output_tokens", None) or {}).get(model) or 8192)
    kwargs: dict[str, Any] = {
        "model": model,
        "google_api_key": provider.api_key,
        "max_output_tokens": cap,
        "timeout": timeout,
        "max_retries": max_retries,
        "additional_headers": _headers(provider, user_agent),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if decision.base_url:
        kwargs["base_url"] = decision.base_url
    if intensity is not None:
        kwargs.update(
            get_format_thinking_params(
                "google_gemini", dialect, intensity, option=option
            )
        )
    return ChatGoogleGenerativeAI(**kwargs)


async def build_chat_model_from_spec(
    providers: Any,
    spec: str = "",
    intensity: str | None = None,
    *,
    purpose: str = "chat",
    auto_load: bool = True,
    **kwargs: Any,
) -> Any:
    """按 ``provider::model`` 构建对应格式的 LangChain Chat 模型（异步入口）。

    ``auto_load=True`` 时，Provider 还没有模型列表会先尝试一次 ``/models``；
    拉取失败只 warning 并继续用显式模型（不静默吞掉，也不因为离线而构建失败）。
    """
    from .schema import parse_spec, resolve_provider

    text = str(spec or "").strip() or str(getattr(providers, "main_model", "") or "")
    if not text:
        raise ProviderConfigError("main_model is not configured")
    provider_name, model_name = parse_spec(text)
    provider = resolve_provider(providers, provider_name)
    if auto_load and not provider.models:
        from .discovery import auto_load_model_for_provider

        try:
            await auto_load_model_for_provider(provider)
        except Exception as exc:  # noqa: BLE001 - 离线/网关不支持时继续用显式模型
            log.warning(
                "自动拉取 Provider '%s' 的模型列表失败（继续用显式模型 %s）: %s",
                provider_name,
                model_name,
                exc,
            )
    if provider.models and model_name not in provider.models:
        raise ProviderConfigError(
            f"Model '{model_name}' not found for provider '{provider_name}'."
        )
    return build_chat_model(
        provider, model_name, intensity=intensity, purpose=purpose, **kwargs
    )


async def close_chat_model(model: Any) -> None:
    """关闭模型**自己持有**的 HTTP 客户端；langchain 的共享默认客户端一律不动。

    两个 provider 包都把默认客户端放进模块级 ``lru_cache``，键里只有
    ``(base_url, timeout, ...)``。关掉它等于关掉所有模型的传输层，且缓存条目不会
    被驱逐，之后任何请求都会在 httpx 层抛 "client has been closed"。

    所以只有调用方显式注入的 ``http_async_client`` / ``http_client``（模型私有）
    才允许关闭；默认共享客户端的生命周期由 langchain 的缓存管理。
    """
    if model is None:
        return
    try:
        from langchain_openai import ChatOpenAI

        if isinstance(model, ChatOpenAI):
            if model.http_async_client is not None:
                async_client = model.root_async_client
                if async_client is not None and not async_client.is_closed():
                    await async_client.close()
            if model.http_client is not None:
                sync_client = model.root_client
                if sync_client is not None and not sync_client.is_closed():
                    sync_client.close()
            return
    except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响调用方
        log.debug("关闭 ChatOpenAI 客户端失败: %s", exc)

    # ChatAnthropic 的 `_async_client` 永远取自 langchain_anthropic 的 lru_cache，
    # 没有任何注入私有客户端的入口 —— 任何情况下都不能关，故此处不处理。


def summarize_route_sources(
    routes: dict[str, dict[str, Any]] | None,
) -> dict[str, int]:
    """统计目录路由里各格式数量（宿主概览用）。"""
    counts: dict[str, int] = {fmt: 0 for fmt in API_FORMATS}
    for route in (routes or {}).values():
        fmt = _catalog_format(route)
        if fmt:
            counts[fmt] += 1
    return counts


__all__ = [
    "API_FORMATS",
    "DEFAULT_FORMAT",
    "FORMAT_LABELS",
    "FormatDecision",
    "SERVER_TOOL_FORMATS",
    "SUPPORTED_FORMATS",
    "build_chat_model",
    "build_chat_model_from_spec",
    "build_server_tool_specs",
    "close_chat_model",
    "describe_all_formats",
    "describe_provider_formats",
    "detect_api_format",
    "detect_url_format",
    "family_format",
    "is_multi_format",
    "normalize_format",
    "normalize_format_base_url",
    "resolve_route",
    "summarize_route_sources",
    "supported_server_tools",
]
