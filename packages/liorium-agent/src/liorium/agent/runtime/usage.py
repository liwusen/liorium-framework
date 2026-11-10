"""LLM 用量（token）归一化 —— 自动压缩触发判据的**唯一**数据来源。

规格来源：``docs/design/compact.md`` §6.1（归一化口径）、§6.2（不本地估算图片）、
§8.5（截断判定）、§16.2（逐条断言）。原型：

四种 API 的上报口径不同，直接相加、或直接读 ``total_tokens`` 都会算错上下文占用，
进而让自动压缩要么永不触发、要么过早触发：

- **Anthropic**：``input_tokens`` **不含**缓存读/写，真实输入 =
  ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``。
  只看 ``input_tokens`` 时，prompt 缓存命中率高的长会话会显示很小的占用，
  自动压缩静默失效（I3）。
- **OpenAI Chat / Responses**：``prompt_tokens`` 已包含缓存命中的部分，
  ``cached_tokens`` 是它的**子集**，重复相加会虚高（``1000+800+100`` 应为 ``1100``）。
- **Google Gemini**：``prompt_token_count`` 同样已包含
  ``cached_content_token_count``，也是子集。

不变量：

- **I1**：触发判据只认这里读出来的 API 上报值；任何本地推算都不参与判定。
  拿不到上报值就返回 ``None`` / ``0``，让调用方"不压缩 + 告警一次"。
- **I2**：``last_usage`` 从后向前找「最近一条**带用量**的 AIMessage」，
  不是「最后一条 AIMessage」（后者可能是本地构造/插件注入的消息）。
- **I18**：不本地统计图片 token；图片由 provider 计数。

本模块只依赖 ``langchain-core`` 的消息类型，不读任何产品配置、不 import 任何
宿主模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import AIMessage, AnyMessage

ANTHROPIC_FORMAT_PREFIX = "anthropic"
"""Anthropic 风格的 ``api_format`` 前缀（``anthropic`` / ``anthropic_messages``）。"""


@dataclass(frozen=True, slots=True)
class Usage:
    """一次模型调用归一化后的用量（全部字段都是**已经算好**的绝对值）。

    ``context_tokens`` 是压缩触发判据用的"这次请求真实占了多少上下文"：
    Anthropic 为 ``input + output``，其余格式为 ``total_tokens``。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    context_tokens: int = 0


def is_anthropic_format(api_format: str | None) -> bool:
    """``api_format`` 是否属于 Anthropic 口径。

    宿主传进来的可能是 ``anthropic`` 或 ``anthropic_messages``（见
    ``TurnRuntime.api_format`` 的注释），两种都认；None / 空串按 OpenAI 口径处理。
    """
    return str(api_format or "").strip().lower().startswith(ANTHROPIC_FORMAT_PREFIX)


# ---------------------------------------------------------------------------
# 读取：只读 API 上报值，绝不本地估算
# ---------------------------------------------------------------------------


def _int(value: Any) -> int:
    """把上报值收敛成非负整数（``bool`` / 负数 / 非数字一律当 0）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def _usage_metadata(message: Any) -> dict[str, Any]:
    usage = getattr(message, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) else {}


def _response_metadata(message: Any) -> dict[str, Any]:
    meta = getattr(message, "response_metadata", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _raw_usage(message: Any) -> dict[str, Any]:
    """``response_metadata["usage"]`` 里的原始 usage（各 provider 原样上报）。"""
    usage = _response_metadata(message).get("usage")
    return dict(usage) if isinstance(usage, dict) else {}


def _infer_anthropic(raw: dict[str, Any], meta: dict[str, Any]) -> bool:
    """``api_format`` 缺失时按上报字段形态反推口径。

    只用于让 ``normalize_usage(message)`` 这种"没带格式"的调用仍然正确：
    Anthropic 的原始 usage 一定带 ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``，且输入字段名是 ``input_tokens`` 而非
    ``prompt_tokens``。
    """
    if any(key in raw for key in ("cache_read_input_tokens", "cache_creation_input_tokens")):
        return True
    if "anthropic" in str(meta.get("model_provider") or "").lower():
        return True
    return "input_tokens" in raw and "prompt_tokens" not in raw


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def normalize_usage(message: Any, api_format: str | None = None) -> Usage | None:
    """把一条消息的用量归一化成 :class:`Usage`；没有任何上报值时返回 ``None``。

    Args:
        message: 通常是 ``AIMessage``；缺属性时按"无上报值"处理。
        api_format: 主模型的 API 格式。``None`` 时按上报字段形态反推
            （见 :func:`_infer_anthropic`）；这只影响 Anthropic 的缓存口径，
            不会改变"绝不本地估算"的语义。

    数据来源优先级：``message.usage_metadata``（dict）与
    ``message.response_metadata["usage"]``（原始 dict）合并使用；两者都空 ⇒ ``None``。
    """
    usage = _usage_metadata(message)
    raw = _raw_usage(message)
    if not usage and not raw:
        return None

    details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    cache_read = _int(details.get("cache_read"))
    cache_creation = _int(details.get("cache_creation"))
    reasoning = _int(output_details.get("reasoning"))

    anthropic = (
        is_anthropic_format(api_format)
        if api_format is not None
        else _infer_anthropic(raw, _response_metadata(message))
    )

    if anthropic:
        # Anthropic 原始口径：input 不含缓存读写，必须补加（I3）。
        raw_input = _int(raw.get("input_tokens"))
        raw_cache_read = _int(raw.get("cache_read_input_tokens")) or cache_read
        raw_cache_creation = _int(raw.get("cache_creation_input_tokens")) or cache_creation
        input_tokens = raw_input + raw_cache_read + raw_cache_creation
        # 新版 langchain-anthropic 已把缓存折进 usage_metadata.input_tokens；
        # 取较大值，既不会重复相加，也兼容旧版本/流式缺失的情况。
        input_tokens = max(input_tokens, _int(usage.get("input_tokens")))
        output_tokens = _int(raw.get("output_tokens")) or _int(usage.get("output_tokens"))
        cache_read = raw_cache_read or cache_read
        cache_creation = raw_cache_creation or cache_creation
        context = input_tokens + output_tokens
        total = context
    else:
        input_tokens = _int(usage.get("input_tokens")) or _int(raw.get("prompt_tokens"))
        output_tokens = _int(usage.get("output_tokens")) or _int(raw.get("completion_tokens"))
        # 缓存命中是 input 的子集：这里只展示，绝不加到 input/total 上（I3）。
        total = _int(usage.get("total_tokens")) or _int(raw.get("total_tokens"))
        if total <= 0:
            # 部分 provider 的 total 恒为 0：退化到 input + output（§6.1）。
            total = input_tokens + output_tokens
        context = total

    if input_tokens == 0 and output_tokens == 0 and total == 0:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        reasoning_tokens=reasoning,
        total_tokens=total,
        context_tokens=context,
    )


def last_usage(messages: Sequence[AnyMessage], api_format: str | None = None) -> Usage | None:
    """最近一条**带用量**的 ``AIMessage`` 的归一化用量（I2）。

    不能"遇到最后一条 AIMessage 就定论"：它可能是本地构造 / 插件注入的消息
    （没有 ``usage_metadata``）。那样自动压缩会静默失效，会话一路涨到必须手动
    ``/compact`` —— 而手动压缩要对整段历史跑一次完整模型调用，正是最贵的那条路径。
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            snapshot = normalize_usage(message, api_format)
            if snapshot is not None:
                return snapshot
    return None


def reported_tokens(messages: Sequence[AnyMessage], api_format: str | None = None) -> int:
    """当前上下文的真实 token 占用（API 上报值；一条都没有时返回 ``0``）。

    ``0`` 的语义是"没有上报数据"，不是"上下文是空的"——调用方据此**不压缩**（I1）。
    """
    snapshot = last_usage(messages, api_format)
    return snapshot.context_tokens if snapshot else 0


def context_tokens(messages: Sequence[AnyMessage], api_format: str | None = None) -> int:
    """:func:`reported_tokens` 的同义函数（规格 §6.1 用的名字）。"""
    return reported_tokens(messages, api_format)


# ---------------------------------------------------------------------------
# 截断判定（摘要被输出上限砍掉 = 压缩失败，绝不写回）
# ---------------------------------------------------------------------------


def is_truncated(max_tokens: int, finish_reason: Any, usage: Any) -> bool:
    """摘要输出是否被上限截断（§8.5）。

    各格式的"到顶"信号不同：OpenAI 用 ``finish_reason == "length"``，
    Anthropic 用 ``stop_reason == "max_tokens"``。部分 provider 都不返回，
    退化为比较上报的 ``output_tokens`` 是否已触顶。三者都拿不到时无法判断，
    按**未截断**处理（不额外制造假失败）。

    Args:
        max_tokens: 本次调用实际生效的输出上限；``<= 0`` 表示没有上限 ⇒ 不可能截断。
        finish_reason: ``finish_reason`` 或 ``stop_reason``。
        usage: ``usage_metadata`` dict（也接受 :class:`Usage`）。
    """
    if max_tokens <= 0:
        return False
    reason = str(finish_reason or "").strip().lower()
    if reason in ("length", "max_tokens"):
        return True
    if isinstance(usage, dict):
        output = usage.get("output_tokens")
    else:
        output = getattr(usage, "output_tokens", None)
    if isinstance(output, int) and not isinstance(output, bool) and output >= max_tokens:
        return True
    return False


__all__ = [
    "ANTHROPIC_FORMAT_PREFIX",
    "Usage",
    "context_tokens",
    "is_anthropic_format",
    "is_truncated",
    "last_usage",
    "normalize_usage",
    "reported_tokens",
]
