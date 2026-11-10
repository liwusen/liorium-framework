"""Provider ``/models`` 自动发现。

各 Provider 的元数据丰富程度差异很大（实测）：

- deepseek：``context_window`` / ``max_output_tokens`` / ``input_modalities`` /
  ``output_modalities`` / ``effort`` / ``api_capabilities``
- OpenRouter：``context_length`` / ``architecture`` / ``pricing`` / ``top_provider`` /
  ``supported_parameters`` / ``knowledge_cutoff`` / ``reasoning`` / ``benchmarks``
- aliyun / OpenCode Go：只有 ``id`` / ``object`` / ``created`` / ``owned_by``

本模块只解析真正消费的字段：上下文长度、最大输出、格式能力、思考能力。
其余字段没有使用方，解析它们只会变成死代码。
"""

from __future__ import annotations

from typing import Any

import httpx

from liorium.core.log import get_logger

from .constants import DEFAULT_USER_AGENT, common_headers
from .errors import ProviderConfigError
from .formats import API_FORMATS
from .thinking_caps import capability_from_models_entry

log = get_logger("provider.discovery")

MODELS_TIMEOUT_SECONDS = 10.0


def _extract_context_length(item: dict) -> int | None:
    """从 ``/models`` 条目里提取上下文长度（token）。

    字段名随 Provider 而异：OpenRouter 用 ``context_length``，DeepSeek 用
    ``context_window``。取不到或值非法时返回 None（调用方保留「未配置」状态，
    由 ``DEFAULT_CONTEXT_LENGTH`` 兜底）。
    """
    for key in ("context_length", "context_window"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    # OpenRouter 部分条目把 context_length 也放在 top_provider 下
    top_provider = item.get("top_provider")
    if isinstance(top_provider, dict):
        value = top_provider.get("context_length")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


def _extract_max_output_tokens(item: dict) -> int | None:
    """从 ``/models`` 条目里提取最大输出 token（字段名随 Provider 而异）。"""
    for key in ("max_output_tokens", "max_completion_tokens"):
        value = item.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    top_provider = item.get("top_provider")
    if isinstance(top_provider, dict):
        value = top_provider.get("max_completion_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    return None


def _extract_api_capabilities(item: dict) -> dict:
    """从 ``/models`` 条目里提取「该模型支持哪些 API 格式」的能力声明。

    只保留已知格式键（未知键没有消费方，留着只会让配置持续膨胀）；值保持
    原始 dict，这样上游新增的约束字段（如 anthropic_messages 的 beta 列表）
    不需要改代码就能被前端展示。
    """
    caps = item.get("api_capabilities")
    if not isinstance(caps, dict):
        return {}
    return {
        str(key): value
        for key, value in caps.items()
        if str(key) in API_FORMATS and isinstance(value, dict)
    }


def model_entry(model_id: str, item: dict | None) -> dict[str, Any]:
    """把 ``/models`` 的一条记录规范成本项目消费的模型条目。"""
    source = item or {}
    return {
        "id": model_id,
        "context_length": _extract_context_length(source),
        "max_output_tokens": _extract_max_output_tokens(source),
        "api_capabilities": _extract_api_capabilities(source),
        # 思考能力（effort / budget_tokens / toggle）：Gemini 3/4 只认等级，
        # 2.5 只认预算，构建模型前必须先知道该用哪种形状。
        "thinking": capability_from_models_entry(source),
    }


async def fetch_provider_model_entries(
    provider: Any,
    *,
    timeout: float = MODELS_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[dict[str, Any]]:
    """从 Provider 的 ``GET {base_url}/models`` 拉取模型条目（含元数据）。

    带超时；对非 OpenAI 兼容的响应结构做容错解析，任何异常都会抛出明确的
    ``ProviderConfigError``（宿主据此提示用户）。

    返回 ``[{"id", "context_length", "max_output_tokens", "api_capabilities", "thinking"}]``。
    """
    headers = common_headers(
        user_agent=user_agent,
        opencode_go=bool(getattr(provider, "opencode_go", False)),
    )
    key = str(getattr(provider, "key", "") or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    base_url = str(getattr(provider, "base_url", "") or "")
    try:
        # 不能用 urljoin(base_url, "/models")——"/models" 是绝对路径会丢弃
        # base_url 的路径前缀（如 /v1、/compatible-mode/v1），导致 404/502。
        models_url = base_url.rstrip("/") + "/models"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(models_url, headers=headers)
            response.raise_for_status()
            models_data = response.json()
    except httpx.HTTPError as exc:
        raise ProviderConfigError(f"请求 {base_url}/models 失败: {exc}") from exc
    except ValueError as exc:
        raise ProviderConfigError(f"{base_url}/models 返回的不是合法 JSON: {exc}") from exc

    entries: list[dict[str, Any]] = []
    # OpenAI 兼容: {"data": [{"id": "..."}]}
    data = models_data.get("data") if isinstance(models_data, dict) else None
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not model_id:
                continue
            entries.append(model_entry(str(model_id), item))
    # 兜底: 直接是字符串数组（无元数据）
    elif isinstance(models_data, list):
        entries = [
            model_entry(str(model), None) for model in models_data if str(model).strip()
        ]
    if not entries:
        raise ProviderConfigError(f"无法从 {base_url}/models 解析模型列表")
    return entries


async def get_provider_models_by_api(provider: Any, **kwargs: Any) -> list[str]:
    """从 Provider 的 ``/models`` 拉取模型名列表（只要 id）。"""
    return [entry["id"] for entry in await fetch_provider_model_entries(provider, **kwargs)]


async def auto_load_model_for_provider(
    provider: Any,
    *,
    force: bool = False,
    timeout: float = MODELS_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[str]:
    """自动从 Provider API 拉取模型列表并写入 ``provider.models``。

    ``force=False``（默认，构建 LLM 的热路径）：已有 models 时跳过网络请求（幂等）。
    ``force=True``（宿主「自动加载模型」按钮）：丢弃已有 models 后重新拉取，保证
    列表与 Provider 端一致；拉取失败时抛错，旧列表不被清空。

    同时用 API 返回的元数据**自动填充** ``model_context_lengths``、
    ``model_max_output_tokens``、``model_api_capabilities``、``model_thinking_caps``：

    - 只填空缺项，**不覆盖用户手工配置的值**（用户配置优先）；
    - ``force=True`` 时顺带清理已不在模型列表中的陈旧条目。
    """
    if not (force or not provider.models):
        return list(provider.models)

    entries = await fetch_provider_model_entries(
        provider, timeout=timeout, user_agent=user_agent
    )
    provider.models = [entry["id"] for entry in entries]

    lengths = dict(provider.model_context_lengths or {})
    outputs = dict(provider.model_max_output_tokens or {})
    capabilities = dict(provider.model_api_capabilities or {})
    thinking_caps = dict(provider.model_thinking_caps or {})
    if force:
        # 显式刷新：丢弃已不在列表中的模型（其元数据也就无从生效）
        keep = set(provider.models)
        lengths = {k: v for k, v in lengths.items() if k in keep}
        outputs = {k: v for k, v in outputs.items() if k in keep}
        capabilities = {k: v for k, v in capabilities.items() if k in keep}
        thinking_caps = {k: v for k, v in thinking_caps.items() if k in keep}
    filled = 0
    for entry in entries:
        model_id = entry["id"]
        reported = entry.get("context_length")
        if reported is not None and not lengths.get(model_id):
            lengths[model_id] = reported
            filled += 1
        reported_output = entry.get("max_output_tokens")
        if reported_output is not None and not outputs.get(model_id):
            outputs[model_id] = reported_output
        reported_caps = entry.get("api_capabilities")
        if reported_caps and not capabilities.get(model_id):
            capabilities[model_id] = reported_caps
        reported_thinking = entry.get("thinking")
        if reported_thinking and not thinking_caps.get(model_id):
            thinking_caps[model_id] = reported_thinking
    provider.model_context_lengths = lengths
    provider.model_max_output_tokens = outputs
    provider.model_api_capabilities = capabilities
    provider.model_thinking_caps = thinking_caps
    if filled:
        log.info(
            "已从 /models 元数据自动填充 %d 个模型的上下文长度（provider=%s）",
            filled,
            getattr(provider, "name", ""),
        )
    return list(provider.models)


__all__ = [
    "MODELS_TIMEOUT_SECONDS",
    "auto_load_model_for_provider",
    "fetch_provider_model_entries",
    "get_provider_models_by_api",
    "model_entry",
]
