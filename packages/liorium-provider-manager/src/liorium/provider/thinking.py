"""思考（thinking / reasoning）预设与按格式翻译。

每个预设把强度（low/medium/high）映射成对应 OpenAI Chat 方言的参数：

- ``openai``：``reasoning_effort``
- ``qwen`` / ``mimo`` / ``glm``：``extra_body.enable_thinking + thinking_level``
- ``deepseek``：``extra_body.thinking + reasoning_effort``
- ``minimax``：``reasoning_effort``
- ``none``：不发送

OpenAI Responses / Anthropic / Gemini 的参数结构与 ChatOpenAI 完全不同，由
:func:`get_format_thinking_params` 按各自协议生成；``option`` 来自
``thinking_caps`` 的解析结果，决定用等级还是预算以及支持哪些档位。

``thinking_type`` 为 ``auto`` 时按 provider/model 名字推断方言；显式值优先。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages.ai import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

from liorium.core.log import get_logger

from .thinking_caps import (
    GOOGLE_EFFORT_LEVELS,
    MODE_BUDGET,
    MODE_EFFORT,
    MODE_TOGGLE,
    ThinkingOption,
    budget_for_intensity,
    level_for_intensity,
)

log = get_logger("provider.thinking")

THINKING_LEVELS = ("off", "low", "medium", "high")
"""宿主可选的强度；``off`` 在构建模型前被规范成 ``None``。"""

THINKING_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "none": {},
    "openai": {  # reasoning_effort only, no extra_body
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "high"},
    },
    # Qwen (Alibaba Cloud Bailian) passes thinking config through extra_body.
    "qwen": {
        "low": {"extra_body": {"enable_thinking": True, "thinking_level": "low"}},
        "medium": {"extra_body": {"enable_thinking": True, "thinking_level": "medium"}},
        "high": {"extra_body": {"enable_thinking": True, "thinking_level": "high"}},
    },
    # MiMo (Xiaomi open platform) thinking config via extra_body.
    "mimo": {
        "low": {"extra_body": {"enable_thinking": True, "thinking_level": "low"}},
        "medium": {"extra_body": {"enable_thinking": True, "thinking_level": "medium"}},
        "high": {"extra_body": {"enable_thinking": True, "thinking_level": "high"}},
    },
    # Zhipu AI GLM thinking config via extra_body.
    "glm": {
        "low": {"extra_body": {"enable_thinking": True, "thinking_level": "low"}},
        "medium": {"extra_body": {"enable_thinking": True, "thinking_level": "medium"}},
        "high": {"extra_body": {"enable_thinking": True, "thinking_level": "high"}},
    },
    # Minimax reasoning config.
    "minimax": {
        "low": {"reasoning_effort": "low"},
        "medium": {"reasoning_effort": "medium"},
        "high": {"reasoning_effort": "high"},
    },
    # DeepSeek: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
    "deepseek": {
        "low": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "low",
        },
        "medium": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
        },
        "high": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "max",
        },
    },
}

#: 自动方言推断：provider/model 名字里出现这些片段时选择对应预设。
_DIALECT_HINTS: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("qwq", "qwen"),
    ("tongyi", "qwen"),
    ("glm", "glm"),
    ("zhipu", "glm"),
    ("chatglm", "glm"),
    ("mimo", "mimo"),
    ("minimax", "minimax"),
    ("abab", "minimax"),
)


def normalize_intensity(value: str | None) -> str | None:
    """把宿主传入的强度规范成 ``low/medium/high`` 或 ``None``。

    ``off`` / ``none`` / 空串都表示不启用思考；非法值抛 ``ValueError``，
    由宿主决定如何展示（不静默当成 medium）。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "off", "none", "disabled", "false", "0"):
        return None
    if text in ("low", "medium", "high"):
        return text
    raise ValueError(
        f"未知思考强度: {value!r}（可用: {', '.join(THINKING_LEVELS)}）"
    )


def resolve_thinking_dialect(provider: Any, model: str) -> str:
    """决定 OpenAI Chat 使用哪种思考方言。

    显式 ``thinking_type`` 优先；``auto`` / 未知值按 provider/model 名字推断，
    推断不出时回落到标准 ``openai``（``reasoning_effort``）。
    """
    raw = str(getattr(provider, "thinking_type", "auto") or "auto").strip().lower()
    if raw == "none":
        return "none"
    if raw in THINKING_PRESETS:
        return raw
    if raw not in ("", "auto"):
        log.warning(
            "Provider '%s' 的 thinking_type '%s' 未知（可用: auto, %s），按名字推断",
            getattr(provider, "name", ""),
            raw,
            ", ".join(name for name in THINKING_PRESETS if name != "none"),
        )
    haystack = f"{getattr(provider, 'name', '')} {model}".lower()
    for needle, dialect in _DIALECT_HINTS:
        if needle in haystack:
            return dialect
    return "openai"


def get_thinking_params(preset_name: str, intensity: str = "medium") -> dict[str, Any]:
    """返回 ChatOpenAI 的思考 kwargs（未知预设按 ``none`` 处理）。"""
    preset = THINKING_PRESETS.get(preset_name, THINKING_PRESETS.get("none", {}))
    if isinstance(preset, dict) and "low" in preset:
        return dict(preset.get(intensity, preset.get("medium", {})))
    return dict(preset)


# Anthropic thinking budget / Gemini thinking budget 的强度映射。
THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}

# Anthropic 的 thinking budget 下限（低于它上游直接 400）
ANTHROPIC_MIN_THINKING_BUDGET = 1024


def get_format_thinking_params(
    api_format: str,
    preset_name: str,
    intensity: str = "medium",
    *,
    option: ThinkingOption | None = None,
) -> dict[str, Any]:
    """按 API 格式翻译思考参数。

    ``option`` 来自 ``thinking_caps`` 的解析结果（手动覆盖 > ``/models`` >
    models.dev > 模型名启发式），决定「用等级还是预算」以及支持哪些档位；
    为 None 时保持历史行为（Gemini 恒用 ``thinking_budget``）。

    ``preset_name == "none"`` 时恒为空（该 Provider 强制关闭思考）。
    ``openai_chat`` 沿用历史预设（qwen/deepseek/mimo/glm/minimax…），其方言由
    Provider 的 ``thinking_type`` 表达，不按目录数据改写；其余三种格式只使用
    强度 → 各自的官方参数结构，预设名对它们是 OpenAI 兼容网关的方言。
    """
    if str(preset_name or "") == "none":
        return {}
    if api_format == "openai_chat":
        return get_thinking_params(preset_name, intensity)

    effort = intensity if intensity in THINKING_BUDGETS else "medium"
    budget = THINKING_BUDGETS[effort]
    if api_format == "openai_responses":
        level = (
            level_for_intensity(effort, option.levels)
            if option is not None and option.mode == MODE_EFFORT
            else effort
        )
        return {"reasoning": {"effort": level}}
    if api_format == "anthropic_messages":
        if option is not None and option.mode == MODE_BUDGET:
            budget = budget_for_intensity(
                effort,
                option,
                THINKING_BUDGETS,
                floor=ANTHROPIC_MIN_THINKING_BUDGET,
            )
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    if api_format == "google_gemini":
        # include_thoughts 让 Gemini 返回思考摘要，前端才能显示 reasoning_delta
        if option is None:
            return {"thinking_budget": budget, "include_thoughts": True}
        if option.mode == MODE_EFFORT:
            # Gemini 3/4 只接受 thinking level；ChatGoogleGenerativeAI 的
            # reasoning_effort 是 Literal，先过滤掉目录里的 xhigh/max 等方言。
            return {
                "reasoning_effort": level_for_intensity(
                    effort, option.levels, allowed=GOOGLE_EFFORT_LEVELS
                ),
                "include_thoughts": True,
            }
        if option.mode == MODE_BUDGET:
            return {
                "thinking_budget": budget_for_intensity(
                    effort, option, THINKING_BUDGETS
                ),
                "include_thoughts": True,
            }
        if option.mode == MODE_TOGGLE:
            return {"include_thoughts": True}
        return {}
    return {}


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that preserves ``reasoning_content``.

    ``langchain-openai`` 1.x deliberately discards non-standard response fields such
    as ``reasoning_content`` (used by OpenAI o1/o3, DeepSeek R1, etc.). This wrapper
    intercepts ``_convert_chunk_to_generation_chunk`` and copies
    ``reasoning_content`` (and fallback keys ``reasoning`` / ``think``) into the
    ``AIMessageChunk.additional_kwargs`` so downstream code can yield
    ``reasoning_delta`` events.
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None
        # Extract reasoning_content from the raw API delta
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            if isinstance(delta, dict):
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("think")
                )
                if reasoning:
                    message = generation_chunk.message
                    if isinstance(message, AIMessageChunk):
                        message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


__all__ = [
    "ANTHROPIC_MIN_THINKING_BUDGET",
    "ReasoningChatOpenAI",
    "THINKING_BUDGETS",
    "THINKING_LEVELS",
    "THINKING_PRESETS",
    "get_format_thinking_params",
    "get_thinking_params",
    "normalize_intensity",
    "resolve_thinking_dialect",
]
