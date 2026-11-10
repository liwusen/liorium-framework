"""会话压缩中间件（保命机制，始终启用）。

规格书：``docs/design/compact.md``（893 行，逐字冻结文本都在里面）。
计划：``docs/plans/2026-11-04-liorium-agent-core.md`` §4.1 / §4.2 /
§E4 / §E5 / §3.8。

三条不可动摇的前提（§1）：

1. **触发判据只认 API 上报的 token 用量**（``runtime.usage``）。拿不到上报值 ⇒
   不压缩 + 告警一次，绝不用 tiktoken / 字符数 / 消息条数做本地推算。
2. **压缩调用复用主请求的逐字节前缀**（同样的 System、同样的 tools schema、
   同样的 messages），以命中上游前缀缓存。因此 payload = "当前完整 messages +
   一条尾端追加的指令 ``HumanMessage``"，**不是**"只发送将被丢弃的那一段"。
3. **失败不隐瞒也不打断**：重试后仍失败 ⇒ 不改动 checkpoint（压缩不可逆，
   写回就等于永久丢历史），本轮继续，错误进日志。手动 ``force_compact`` 是唯一
   把异常抛给调用方的路径（渲染失败卡片）。

不变量（§17，改代码前逐条对照）：

- **I1** 触发只看上报值；**I2** 找"最近一条**带用量**的 AI 消息"；
  **I3** Anthropic 缓存计入 input、OpenAI/Gemini 的 cached 是子集；
  **I4** ``keep_rounds`` 是上限不是下限；**I5** 摘要消息不算一轮、丢弃段必须含真实轮次
  （这两条是唯一的"防每回合重复压缩"机制）；**I6** 切点绝不拆 tool 组；
  **I7** 不切进 System；**I8** payload = 完整 messages + 尾端指令；
  **I9** 先 ``repair_dangling_tool_calls`` 再追加指令；**I10** tools 前缀 = mock +
  服务端 specs，``tool_choice`` 写法随格式；**I11** 默认不设输出上限、不裁剪输入；
  **I12** 写回结构 = ``[RemoveMessage, 头部..., *preserved]``；**I13** 摘要段用标记包裹、
  先剥旧再写新；**I14** 失败不写 checkpoint；**I15** 事件分流靠 ``COMPACT_TAG``；
  **I16** ``_ensure_message_ids`` 是唯一的有意 in-place 副作用；**I18** 不本地统计图片 token。

与原型的三处有意差异（Liorium 形态，行为不变）：

1. 摘要调用走 ``self.model``（或构造时给的 ``compact_model``）的 ``ainvoke``，
   **不**用 ``astream``：v1 的压缩事件由本类经构造参数 ``emit`` 推给宿主，
   不需要流式分片，因此一次调用只产生一条 ``compact_delta``。
2. 配置项（成本控制开关 / 两个上限）来自构造参数，不读任何产品配置。
3. VNG 通知的"跨压缩保留"抽成宿主注入的 ``preserve_predicate`` 钩子，
   核心不 import 任何产品模块。
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable, Sequence

from langchain.agents.middleware.summarization import SummarizationMiddleware
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from liorium.core.log import get_logger
from liorium.core.types import Event, ev_compact_delta, ev_compact_start

from ..repair import repair_dangling_tool_calls
from ..usage import is_anthropic_format, is_truncated, reported_tokens

log = get_logger("compact")

COMPACT_TAG = "liorium_compact_summarizer"
"""摘要调用的事件标记（§3，冻结）。

宿主的事件生产者据此把该调用的输出与主模型输出分流，转成
``compact_start`` / ``compact_delta`` / ``compact_done``，**绝不泄进正文 ``delta``**。
"""

COMPACT_MARKER_START = "<!-- liorium:compact-summary:start -->"
COMPACT_MARKER_END = "<!-- liorium:compact-summary:end -->"
"""System 中摘要段的起止标记（§3，冻结）。

下次压缩据此剥离旧摘要再写入新摘要，保证 System 不随压缩次数无限增长（I13）。
"""

COMPACT_INSTRUCTION = """停止你当前正在执行的任务，不要再调用任何工具，也不要继续之前的步骤。

请把以上全部对话压缩成一份结构化中文摘要，作为后续继续工作时的上下文。严格使用下面 6 个二级标题，不要增删标题、不要写寒暄、不要输出与摘要无关的内容：

## 目标与进度
用户的核心目标，以及目前推进到哪一步。

## 未完成事项
尚未完成、待验证或待用户确认的事项，逐条列出。

## 关键约束与用户偏好
必须遵守的约束、用户的明确偏好与否决过的方案。

## 结论与失败尝试
已经确认的结论，以及尝试过但失败的做法及其原因（避免重复踩坑）。

## 环境与状态
仍然有效的文件路径、命令、配置项、服务/插件/Skill/MCP 状态。

## 下一步
接下来应当执行的动作。

要求：
- 保留仍然有效的文件路径、命令、配置名，不要改写或省略它们。
- 工具调用折叠为「做了什么、结果如何、有什么影响」，不要罗列原始输出。
- 图片等多模态内容只转述与任务相关的信息。
- 直接输出摘要正文，不要有任何前言或结束语。"""
"""压缩指令全文（§4，**逐字冻结**；末尾无换行）。

它是 payload 的**最后一条**消息（不是 System 的一部分：改 System 会改变被缓存
前缀的最前端）。文案里"不要再调用任何工具"与 ``tool_choice`` 是两处独立的约束点，
测试断言子串 ``"调用任何工具"`` —— 改写文案会直接打破该断言。
"""

_NO_SYSTEM_SUMMARY_PREFIX = "以下是此前对话的压缩摘要（背景资料，不是新的用户请求）：\n\n"
"""无 SystemMessage 时摘要消息的前缀（§10.2，逐字冻结）。"""

EmitFn = Callable[[Event], Awaitable[None]]
"""把一条事件推入当前回合的事件队列（宿主提供）。"""

PreservePredicate = Callable[[Sequence[Any], Sequence[Any]], Sequence[Any]]
"""跨压缩保留钩子：``(messages, preserved) -> 额外要保留的消息``。

Liorium 用它替代原型里对 ``vng``（视觉小说模式通知）的硬依赖：宿主注入一个谓词，
把"落在丢弃段但必须跨压缩保留"的消息返回来，本类把它们插在"System/摘要之后、
保留轮次之前"。去重由钩子自己负责（``preserved`` 已经包含的消息不要再返回）。
"""


# ---------------------------------------------------------------------------
# content / 摘要文本工具（§14）
# ---------------------------------------------------------------------------


def message_content_to_text(content: Any) -> str:
    """把消息 content 归一化成纯文本（§14.5，与原型逐字一致）。

    ``None`` → ``""``；``str`` → 原样；``list`` → 只拼接 ``type`` 归一化后等于
    ``"text"`` 的块的 ``text`` 字段（image / reasoning / tool_use / thinking 一律丢弃）；
    其他类型 → ``str(content)``。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").strip().lower() == "text":
                text_val = block.get("text")
                if text_val is not None:
                    parts.append(str(text_val))
        return "".join(parts)
    return str(content)


def _content_to_text(content: Any) -> str:
    """内部别名（原型里有同名函数，便于对照）。"""
    return message_content_to_text(content)


def _strip_summary(content: Any) -> str:
    """剥掉 System 内容里上一次写入的摘要段，只留原始 prompt（§14.2）。"""
    text = content if isinstance(content, str) else str(content or "")
    start = text.find(COMPACT_MARKER_START)
    if start < 0:
        return text.rstrip()
    end = text.find(COMPACT_MARKER_END, start)
    if end < 0:
        return text[:start].rstrip()
    tail = text[end + len(COMPACT_MARKER_END):]
    return (text[:start] + tail).rstrip()


def _wrap_summary(prompt_text: str, summary: str) -> str:
    """把摘要段包进标记后拼到 System prompt 末尾（§14.3）。

    性质：``_strip_summary(_wrap_summary(base, summary)) == base``。
    """
    return (
        f"{prompt_text.rstrip()}\n\n"
        f"{COMPACT_MARKER_START}\n"
        f"{_NO_SYSTEM_SUMMARY_PREFIX}"
        f"{summary.strip()}\n"
        f"{COMPACT_MARKER_END}"
    )


_ANCHOR_MAX_CHARS = 2000
"""单轮中切时，把原始任务重新插到保留段前，最多保留多少字符。"""

_CONTINUE_PREFIX = "（继续上一条任务；历史已压缩，以下是该任务的原始请求）\n"


def _is_compact_summary(message: Any) -> bool:
    """判断消息是否为压缩写回的摘要（它是固定头部，**不算一轮对话**，§14.4 / I5）。

    没有 SystemMessage 时（Subagent、以及主 Agent 的 state 里也可能没有 System），
    摘要写成首条 HumanMessage。若把它当成一轮，``_determine_cutoff_index`` 就永远
    算得出"可压缩的轮次"，导致压缩后每回合都再压一次（每次都是一整轮模型调用）。
    """
    kwargs = getattr(message, "additional_kwargs", None) or {}
    return str(kwargs.get("lc_source") or "") == "liorium_compact"


def _is_injected_context(message: Any) -> bool:
    """运行中注入的 ``<context ...>`` 不是真实用户回合（不能当任务锚点）。"""
    if not isinstance(message, HumanMessage):
        return False
    return str(getattr(message, "content", "") or "").lstrip().startswith("<context")


def _reported_tokens(messages: Sequence[AnyMessage], api_format: str | None = None) -> int:
    """取最近一条**带 API 用量**的 AIMessage 的真实上下文占用（无数据时返回 0，§6.1）。

    口径交给 :mod:`liorium.agent.runtime.usage`：Anthropic 的缓存读写必须计入输入
    （否则 prompt 缓存命中率高的长会话永远不触发压缩），OpenAI/Gemini 的 cached 是
    输入子集（重复相加会虚高）。依然不本地估算：找不到真实上报值就返回 0。
    """
    return reported_tokens(messages, api_format)


# ---------------------------------------------------------------------------
# tools 前缀（§14.1 / §8.2）
# ---------------------------------------------------------------------------


def _noop(**_kwargs: Any) -> str:
    """mock 工具的实现：恒返回空串，绝不执行真实逻辑。"""
    return ""


def _mock_tool(tool: BaseTool) -> BaseTool:
    """构造与真实工具 schema 逐字节一致、但实现为空操作的 mock 工具。

    工具定义位于请求最前端，只要 schema 有一个字节不同，前缀缓存就失效；
    因此必须原样保留 name / description / args_schema。
    """
    args_schema = tool.args_schema

    if args_schema is not None:
        return StructuredTool.from_function(
            func=_noop,
            name=tool.name,
            description=tool.description,
            args_schema=args_schema,
        )
    return StructuredTool.from_function(
        func=_noop,
        name=tool.name,
        description=tool.description,
    )


def build_mock_tools(tools: Sequence[BaseTool] | None) -> list[BaseTool]:
    """把工具列表整体替换为 schema 相同的 noop mock 工具（§14.1）。"""
    return [_mock_tool(tool) for tool in (tools or [])]


def forced_no_tool_choice(api_format: str | None) -> str | dict[str, str]:
    """摘要调用用的"禁止调用工具"写法（§8.2）。

    - Anthropic：必须传 dict ``{"type": "none"}``。SDK 的字符串 ``tool_choice``
      只接受 ``auto`` / ``any`` / 工具名，传 ``"none"`` 会被解释成
      "调用名为 none 的工具"。
    - 其他（OpenAI Chat/Responses、Gemini、以及 ``api_format`` 为 None）：
      字符串 ``"none"``。
    """
    if is_anthropic_format(api_format):
        return {"type": "none"}
    return "none"


# ---------------------------------------------------------------------------
# 切点与 id（§7 / §15，内化 langchain 私有语义，不再跨库调用私有 API，I19）
# ---------------------------------------------------------------------------


def find_safe_cutoff_point(messages: Sequence[Any], cutoff_index: int) -> int:
    """把落在 ``ToolMessage`` 上的切点**向前**回退到发起该调用的 ``AIMessage``（§7.2）。

    保证 ``AIMessage(tool_calls) + ToolMessage`` 整组落在同一侧（本模块的策略是
    "整组一起被压缩"，比"整组保留"更激进）。方向很重要：回退（变小），不是前进 ——
    因此 ``AIMessage(tool_calls)`` 会随它的 ``ToolMessage`` 一起进丢弃段，
    不会出现"ToolMessage 被保留、它的 AIMessage 被压缩"的悬空。

    找不到匹配的 ``AIMessage`` 时（畸形历史），兜底"越过这些 ToolMessage 之后"，
    避免留下孤立 tool 结果。
    """
    if cutoff_index >= len(messages) or not isinstance(messages[cutoff_index], ToolMessage):
        return cutoff_index

    tool_call_ids: set[str] = set()
    idx = cutoff_index
    while idx < len(messages) and isinstance(messages[idx], ToolMessage):
        tool_call_id = messages[idx].tool_call_id
        if tool_call_id:
            tool_call_ids.add(tool_call_id)
        idx += 1

    for i in range(cutoff_index - 1, -1, -1):
        message = messages[i]
        if isinstance(message, AIMessage) and message.tool_calls:
            ai_tool_call_ids = {call.get("id") for call in message.tool_calls if call.get("id")}
            if tool_call_ids & ai_tool_call_ids:
                return i

    return idx


def ensure_message_ids(messages: Sequence[Any]) -> None:
    """给没有 ``id`` 的消息**就地**补 uuid（I16：本模块唯一的有意 in-place 副作用）。

    ``add_messages`` reducer 靠 id 做 upsert；没有 id 的保留消息会被当成新消息，
    压缩后消息列表会重复膨胀。
    """
    for message in messages:
        if message.id is None:
            message.id = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------


class CompactMiddleware(SummarizationMiddleware):
    """按 API 上报的 token 用量自动压缩会话上下文（继承 ``SummarizationMiddleware``）。

    继承基类只为复用它的构造校验 / profile 读取 / token 计数器挑选；
    **切点、触发判定、摘要生成、写回全部由本类的自有实现接管**（I19），
    基类的 ``_create_summary`` / ``_build_new_messages`` 不会被调用。

    同步入口 ``before_model`` 不提供（见该方法）；Liorium 只有异步回合循环。

    Args:
        model: 主模型实例。用于摘要调用（未传 ``compact_model`` 时）、基类构造
            （``with_retry`` / ``_llm_type``）与日志。
        emit: 事件出口；按 ``compact_start`` → ``compact_delta`` → ``compact_done``
            顺序推送。为 None 时不发事件（测试/无界面的宿主）。
        api_format: 主模型 API 格式（``openai_chat`` / ``anthropic`` /
            ``google_gemini`` / None）。None ⇒ 全按 OpenAI 风格处理
            （``tool_choice="none"``、cached 是子集）。
        context_window: 模型上下文窗口（token）。``<= 0`` 表示未知 ⇒ 自动压缩
            **停用**并告警一次（宁可不压，也不能把阈值算成 1 导致每回合都压）。
        threshold_ratio: 触发比例，默认 0.8（留 20% 给指令与摘要输出）。
        keep_rounds: 压缩后保留最近多少轮（**上限不是下限**，I4）。
        max_output_tokens: 成本控制下的摘要输出上限；0 = 不设（默认关）。
        cost_control: 成本控制开关（默认关）。打开后才会裁剪输入 + 设输出上限，
            两者都以牺牲前缀缓存/摘要覆盖范围为代价（§9.2）。
        max_input_tokens: 成本控制下的输入上限（token，粗估 2.5 字符/token）；0 = 不设。
        tools: 与主请求一致的客户端工具列表；构造时立刻转成同 schema 的 noop mock。
        server_tool_specs: 主请求启用的服务端工具规格（provider 托管工具）。
            压缩请求必须带同样的 specs，否则 tools 前缀与主请求不一致，前缀缓存整段失效。
        summary_max_tokens: 压缩模型自身的输出上限（Anthropic 构造时已设定）；
            仅用于"摘要是否被该上限截断"的判定，0 = 未知/不适用。
        compact_model: 摘要专用模型（已关闭 thinking）。None ⇒ 复用 ``model``。
        preserve_predicate: 跨压缩保留钩子（见 :data:`PreservePredicate`）。
    """

    def __init__(
        self,
        *,
        model: Any,
        emit: EmitFn | None = None,
        api_format: str | None = None,
        context_window: int = 0,
        threshold_ratio: float = 0.8,
        keep_rounds: int = 3,
        max_output_tokens: int = 0,
        cost_control: bool = False,
        max_input_tokens: int = 0,
        tools: Sequence[BaseTool] | None = None,
        server_tool_specs: Sequence[dict[str, Any]] | None = None,
        summary_max_tokens: int = 0,
        compact_model: Any | None = None,
        preserve_predicate: PreservePredicate | None = None,
    ) -> None:
        window = int(context_window or 0)
        ratio = float(threshold_ratio)
        # 用绝对 token 阈值（不是 fraction）：fraction 依赖 model.profile，
        # 而 Liorium 按宿主配置持有上下文长度，不依赖 profile。
        threshold = max(1, int(window * ratio)) if window > 0 else 0
        self.keep_rounds = max(1, int(keep_rounds))
        super().__init__(
            model=model,
            # window 未知时给基类一个 1：基类构造要求 > 0，而真正的判定在本类
            # 的 _should_summarize 里（threshold_tokens == 0 ⇒ 永不触发）。
            trigger=("tokens", threshold or 1),
            # keep 仅占位：实际保留策略由 _determine_cutoff_index 按"轮次"决定。
            keep=("messages", self.keep_rounds),
            # 压缩调用要复用完整前缀以命中缓存，因此不做任何裁剪（基类默认 4000）。
            trim_tokens_to_summarize=None,
        )
        self.compact_model = compact_model if compact_model is not None else model
        self.emit = emit
        self.api_format = str(api_format or "openai_chat")
        self.context_window = window
        self.threshold_ratio = ratio
        self.threshold_tokens = threshold
        self.mock_tools = build_mock_tools(tools)
        self.server_tool_specs = [dict(spec) for spec in (server_tool_specs or [])]
        self.max_output_tokens = max(0, int(max_output_tokens or 0))
        self.cost_control = bool(cost_control)
        self.max_input_tokens = max(0, int(max_input_tokens or 0))
        self.summary_max_tokens = max(0, int(summary_max_tokens or 0))
        self.preserve_predicate = preserve_predicate

        # 输出上限曾导致摘要被截断 → 本次进程内不再施加成本上限（§8.5 自愈：
        # 否则每回合都会用过小的上限压缩失败、白烧一整轮调用）。
        self._cost_cap_bypassed = False
        self._tool_choice_warned = False
        self._warned_no_usage = False
        self._warned_no_context_window = False

    # ── 触发与边界（§6 / §7） ──

    def _should_summarize(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        """触发判定：**上报值 ≥ 阈值**（§6.3，等价重写基类的 clause 逻辑）。

        不复制基类的 "reported token 兜底" 路径：它取最后一条 AIMessage 的
        ``usage_metadata.total_tokens``，还要求 ``response_metadata["model_provider"]``
        与 ``model._get_ls_params()`` 匹配。Liorium 只依赖自己的上报值。
        """
        if self.threshold_tokens <= 0:
            return False
        return total_tokens >= self.threshold_tokens

    def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int:
        """按"轮次"确定切点：保留最近 N 轮，绝不拆散 tool_call 组（§7.1）。

        轮次 = 一条 ``HumanMessage`` 及其之后的全部消息（直到下一条 HumanMessage）。
        压缩写回的摘要消息**不算轮次**（I5）——否则"摘要 + 保留 N 轮"永远比
        ``keep_rounds`` 多一轮，切点恒 > 0，压缩后每回合都会再压一次。

        只有一个用户请求、但里面跑了几十次工具调用的 agentic 长循环，真实轮数
        永远 <= ``keep_rounds``；此时退化为按**完整 tool 组**切分（保留最近 N 个
        组），否则最需要压缩的场景反而永远返回 0。

        返回 0 表示"不该压缩"（而不是"从头开始压"）。
        """
        round_starts = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, HumanMessage) and not _is_compact_summary(message)
        ]
        if not round_starts:
            return 0

        # 不能把 System 切开：至少留出全部 System 消息（I7）。
        system_count = sum(1 for message in messages if isinstance(message, SystemMessage))

        fallback = False
        if len(round_starts) > self.keep_rounds:
            cutoff = round_starts[-self.keep_rounds]
        else:
            # 先按旧语义保留最后一整轮。
            cutoff = round_starts[-1]
            # 只有一个用户请求时，丢弃段里没有任何真实轮次（只有 System/旧摘要）；
            # 这正是 agentic 长循环：退化为按完整 tool 组切，保留最近 N 个组。
            has_earlier_round = any(
                isinstance(message, HumanMessage)
                and not _is_compact_summary(message)
                and not _is_injected_context(message)
                for message in messages[:cutoff]
            )
            if not has_earlier_round:
                units = [cutoff]
                units.extend(
                    index
                    for index, message in enumerate(messages)
                    if index > cutoff
                    and isinstance(message, AIMessage)
                    and message.tool_calls
                    and self._is_complete_tool_group(messages, index)
                )
                if len(units) > self.keep_rounds:
                    cutoff = units[-self.keep_rounds]
                    fallback = cutoff > round_starts[-1]

        # 丢弃段里至少要有一条**真实轮次**，否则只是把已有摘要重新摘要一遍：
        # 白花一整轮压缩调用，而且状态不会真正变小（I5）。
        if not any(
            isinstance(message, HumanMessage)
            and not _is_compact_summary(message)
            and not _is_injected_context(message)
            for message in messages[:cutoff]
        ):
            return 0

        # 单轮内切时，丢弃段里还必须至少有一组完整的 tool 调用，否则只是
        # 把"当前任务的开头"摘要一遍，收益抵不过一次压缩调用。
        if fallback and not any(
            isinstance(message, AIMessage)
            and self._is_complete_tool_group(messages, index)
            for index, message in enumerate(messages[:cutoff])
        ):
            return 0

        if cutoff <= system_count:
            return 0
        return self._find_safe_cutoff_point(messages, cutoff)

    @staticmethod
    def _is_complete_tool_group(messages: list[AnyMessage], index: int) -> bool:
        """该 AIMessage 的所有 tool_call 后面都有对应 ToolMessage。"""
        message = messages[index]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            return False
        wanted = {
            str(call.get("id"))
            for call in message.tool_calls
            if call.get("id")
        }
        if not wanted:
            return False
        seen: set[str] = set()
        cursor = index + 1
        while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
            seen.add(str(messages[cursor].tool_call_id))
            cursor += 1
        return wanted <= seen

    def _with_turn_anchor(
        self,
        messages: list[AnyMessage],
        cutoff: int,
        preserved: list[AnyMessage],
    ) -> list[AnyMessage]:
        """保留段不以 Human 开头时，把当前任务的原始请求补回去。

        单轮中切后，保留段会以 ``AIMessage(tool_calls)`` 开头；Anthropic/Gemini
        这类提供商要求消息以 user 开头，补一条 Human 既满足协议，也给模型一个
        明确的任务锚点（摘要里虽然也有，但模型更认最近的一条 Human）。
        """
        if not preserved or isinstance(preserved[0], HumanMessage):
            return preserved
        anchor: HumanMessage | None = None
        for message in reversed(messages[:cutoff]):
            if (
                isinstance(message, HumanMessage)
                and not _is_compact_summary(message)
                and not _is_injected_context(message)
            ):
                anchor = message
                break
        if anchor is None:
            return preserved
        text = message_content_to_text(anchor.content)
        if len(text) > _ANCHOR_MAX_CHARS:
            text = text[:_ANCHOR_MAX_CHARS] + "\n…（原始请求过长，已截断）"
        anchored = HumanMessage(
            content=f"{_CONTINUE_PREFIX}{text}",
            additional_kwargs={"lc_source": "liorium_compact_anchor"},
        )
        return [anchored, *preserved]

    @staticmethod
    def _find_safe_cutoff_point(messages: list[AnyMessage], cutoff_index: int) -> int:
        """内化实现，见模块级 :func:`find_safe_cutoff_point`（I6）。"""
        return find_safe_cutoff_point(messages, cutoff_index)

    @staticmethod
    def _ensure_message_ids(messages: list[AnyMessage]) -> None:
        """内化实现，见模块级 :func:`ensure_message_ids`（I16）。"""
        ensure_message_ids(messages)

    @staticmethod
    def _partition_messages(
        conversation_messages: list[AnyMessage], cutoff_index: int
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """切成 ``(要压缩的段, 要保留的段)``；只有后者参与写回（§11.1）。"""
        return conversation_messages[:cutoff_index], conversation_messages[cutoff_index:]

    def _cost_control_active(self) -> bool:
        """成本控制（输入/输出上限）当前是否生效（§9.1，默认关）。"""
        if self._cost_cap_bypassed:
            return False
        return self.cost_control

    @staticmethod
    def _is_truncated(max_out: int, finish_reason: Any, usage: Any) -> bool:
        """摘要输出是否被上限截断（见 :func:`liorium.agent.runtime.usage.is_truncated`）。"""
        return is_truncated(max_out, finish_reason, usage)

    @staticmethod
    def _log_usage(usage: Any, api_format: str = "openai_chat") -> None:
        """记录压缩调用真实用量，让压缩成本可审计（§8.6，文案逐字冻结）。

        ``cache_read`` 是判断"前缀缓存有没有命中"的唯一依据：它若远小于
        ``input_tokens``，说明前缀没对齐，整段历史都在按全价计费。
        Anthropic 另外会上报 ``cache_creation``（本回合写入缓存的量），
        单独打印才能区分"写缓存"与"读缓存"。
        """
        if not usage:
            log.info("压缩调用用量: 提供商未返回 usage_metadata（无法核算成本）")
            return
        details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        log.info(
            "压缩调用用量(%s): input=%s output=%s cache_read=%s cache_creation=%s "
            "reasoning=%s total=%s",
            api_format,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            details.get("cache_read"),
            details.get("cache_creation"),
            output_details.get("reasoning"),
            usage.get("total_tokens"),
        )

    def _apply_input_cap(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        """粗粒度输入上限（仅成本控制开启且 ``max_input_tokens > 0`` 时生效，§9.2）。

        超限时从**最旧**的消息开始丢弃。这只是成本安全阀，不是触发判据。
        两个必须知道的代价：

        1. **会打破前缀缓存**：payload 不再以对话起点开头，于是只有 System 与
           工具定义还能命中缓存，被丢弃段之后的全部历史都按全价计费。
        2. **会缩小摘要覆盖范围**：被丢掉的最旧消息不会进入摘要（摘要看不到它们），
           但那段历史照样会被写回删除 ⇒ **永久丢失**。cap 过小时还会丢进本轮
           本来要保留的尾部轮次。

        所以默认 0（不限制），且必须配合 ``cost_control`` 一起打开。
        """
        cap = self.max_input_tokens
        if cap <= 0:
            return messages

        # 粗估：中文环境约 2.5 字符/token。
        budget_chars = int(cap * 2.5)
        total = 0
        kept: list[AnyMessage] = []
        for message in reversed(messages):
            content = message.content if isinstance(message.content, str) else str(message.content)
            size = len(content or "")
            # 超预算且已保留至少一条 → 停止（保证至少留 1 条）。
            if total + size > budget_chars and kept:
                break
            total += size
            kept.append(message)
        kept.reverse()
        if len(kept) != len(messages):
            log.warning(
                "压缩输入超过 COMPACT_MAX_INPUT_TOKENS=%d（约 %d 字符），"
                "已丢弃最旧的 %d 条消息：摘要覆盖范围变小（这部分历史将永久丢失），"
                "且 payload 不再与主请求前缀一致（前缀缓存只剩 System 与工具定义可用）",
                cap,
                budget_chars,
                len(messages) - len(kept),
            )
        return kept

    # ── 压缩调用（§8） ──

    async def _acompact(self, messages: list[AnyMessage], on_delta: Any = None) -> str:
        """用**当前完整上下文 + 尾端指令**生成摘要（§8）。

        关键：payload 前缀必须与主请求逐字节一致（同样的 System、同样的工具
        schema），这样这次调用才能命中上游的前缀缓存。因此调用方传进来的是
        完整 ``messages``（= 主请求 messages 本身），**不要**改成只传将要丢弃的
        段落：那样 payload 不再以对话起点开头，前缀缓存会整段失效（I8）。

        压缩调用不经过中间件链，因此这里自己修复悬空的 tool_calls；修复放在追加
        指令**之前**，使序列结尾的悬空走"注入占位"而不是被判为打断作废（I9）。

        Args:
            on_delta: 可选回调，收到增量文本时调用（同步或异步均可）。手动
                ``force_compact`` 路径传入；自动路径不传，改由 ``emit`` 发
                ``compact_delta``（同一段文本只走其中一条通道，避免重复上屏）。

        Raises:
            RuntimeError: 摘要被输出上限截断（§8.5）。调用方据此**不写回**
                checkpoint —— 写回就等于永久丢掉摘要没覆盖到的历史。
        """
        if not messages:
            return ""

        cost_control = self._cost_control_active()
        if cost_control:
            messages = self._apply_input_cap(list(messages))
        # I9：先修复，再追加指令。
        payload: list[AnyMessage] = [
            *repair_dangling_tool_calls(list(messages)),
            HumanMessage(content=COMPACT_INSTRUCTION),
        ]

        # 工具前缀必须与主请求一致（客户端工具的 mock + 同样的服务端工具 specs），
        # 否则 tools 定义不同 → 前缀缓存整段失效（I10）。
        tool_specs: list[Any] = [*self.mock_tools, *self.server_tool_specs]
        runnable = self._bind_compact_tools(self.compact_model, tool_specs)

        # 摘要输出上限：仅成本控制开启时施加；自定义模型/测试替身可能不支持 bind，
        # 失败时退化为不设上限而不是让压缩失败（I11）。
        max_out = self.max_output_tokens if cost_control else 0
        if max_out > 0:
            try:
                runnable = runnable.bind(max_tokens=max_out)
            except Exception as exc:  # noqa: BLE001 - 不支持 bind 不是压缩失败
                log.debug("压缩模型不支持 bind(max_tokens)，跳过输出上限: %s", exc)
                max_out = 0
        # 实际生效的输出上限：成本上限 > 构造时的模型自身上限。
        effective_max = max_out or self.summary_max_tokens
        # 瞬时故障在进程内重试（3 次尝试）；仍失败则异常上抛给调用方降级。
        runnable = runnable.with_retry()

        await self._emit_event(ev_compact_start())
        response = await runnable.ainvoke(
            payload,
            config={
                "tags": [COMPACT_TAG],
                "metadata": {"liorium_event": "compact"},
            },
        )

        usage = getattr(response, "usage_metadata", None)
        meta = getattr(response, "response_metadata", None) or {}
        # OpenAI 用 finish_reason，Anthropic 用 stop_reason（max_tokens）。
        finish_reason = meta.get("finish_reason") or meta.get("stop_reason")
        summary = _content_to_text(getattr(response, "content", "")).strip()

        # 截断判定之前记账：成功与失败都要能核算成本（§8.6）。
        self._log_usage(usage, self.api_format)

        if summary:
            if on_delta is not None:
                result = on_delta(summary)
                if hasattr(result, "__await__"):
                    await result
            else:
                await self._emit_event(ev_compact_delta(summary))

        if self._is_truncated(effective_max, finish_reason, usage):
            # 摘要被截断 ⇒ 按失败处理：不写回 checkpoint（原文会被写回删掉），
            # 同时放弃成本上限自愈，避免下一回合再用过小的上限白烧一次调用。
            self._cost_cap_bypassed = max_out > 0
            raise RuntimeError(
                f"摘要被输出上限 {effective_max} 截断"
                f"（finish_reason={finish_reason}，api_format={self.api_format}），"
                "本次不写回 checkpoint 以免丢失摘要未覆盖的历史"
                + (
                    "；已自动放弃 COMPACT_MAX_OUTPUT_TOKENS，请调大该值或关闭 "
                    "COMPACT_COST_CONTROL_ENABLED"
                    if max_out > 0
                    else "；该上限来自模型自身 max_tokens，请调大模型的"
                    " model_max_output_tokens"
                )
            )
        return summary

    def _bind_compact_tools(self, model: Any, tool_specs: list[Any]) -> Any:
        """把工具绑定到压缩模型并禁止本次调用真正使用工具（§8.2）。

        ``tool_choice`` 的写法随格式不同（见 :func:`forced_no_tool_choice`），
        写错会静默变成"必须调用某个工具"。绑定失败时逐级降级：先去掉
        ``tool_choice``，再省略服务端 specs，最后不绑工具——压缩本身不能因此失败。
        """
        if not tool_specs:
            return model
        choice = forced_no_tool_choice(self.api_format)
        try:
            return model.bind_tools(tool_specs, tool_choice=choice)
        except Exception as exc:  # noqa: BLE001 - 降级路径
            if not self._tool_choice_warned:
                self._tool_choice_warned = True
                log.warning(
                    "压缩请求无法绑定 tool_choice=%r（%s），改用无 tool_choice 的工具前缀："
                    "摘要阶段可能调用工具，前缀缓存仍保持一致",
                    choice,
                    exc,
                )
        try:
            return model.bind_tools(tool_specs)
        except Exception as exc:  # noqa: BLE001 - 降级路径
            if self.server_tool_specs:
                log.warning(
                    "压缩请求无法绑定服务端工具规格（%s），本次省略："
                    "前缀缓存可能不完整",
                    exc,
                )
        if self.mock_tools:
            try:
                return model.bind_tools(self.mock_tools)
            except Exception as exc:  # noqa: BLE001 - 最后一级降级
                log.warning("压缩请求无法绑定客户端工具 mock（%s），本次不绑工具", exc)
        return model

    # ── 写回（§10 / §11） ──

    def _build_replacement(
        self,
        messages: list[AnyMessage],
        summary: str,
        preserved: list[AnyMessage] | tuple[AnyMessage, ...] = (),
    ) -> list[AnyMessage]:
        """构造写回的**头部**：唯一 System（并入摘要）或摘要 HumanMessage（§10）。

        调用方把 ``preserved`` 拼在返回值之后，并用
        ``RemoveMessage(REMOVE_ALL_MESSAGES)`` 清空后按目标顺序重排，因此摘要能精确
        落在"System 之后、保留消息之前"（I12）。

        **多条 SystemMessage 必须合并成一条**：Gemini 只认第一条 system instruction
        （其余被丢），Anthropic 要求 system 是单独参数。合并保留原有顺序与内容
        （各自剥掉上一轮的摘要段），新摘要追加在末尾（I13）。

        ``preserved`` 与 ``preserve_predicate`` 的用途只有一个：跨压缩保留额外消息
        且不重复插入。
        """
        system_messages = [m for m in messages if isinstance(m, SystemMessage)]
        if system_messages:
            head = system_messages[0]
            if len(system_messages) == 1:
                merged_text = _strip_summary(head.content)
            else:
                merged_text = "\n\n".join(
                    text
                    for text in (_strip_summary(message.content) for message in system_messages)
                    if text
                )
            # model_copy 保留 head 的 id 与其余字段：add_messages reducer 会**就地替换**
            # 那条 System，而不是新增一条。
            replacement: list[AnyMessage] = [
                head.model_copy(update={"content": _wrap_summary(merged_text, summary)})
            ]
        else:
            # Subagent：system prompt 由 create_agent 在模型调用时注入，不在 state 中。
            # 此时没有可并入的 System，退化为把摘要作为首条 HumanMessage（§10.2）。
            replacement = [
                HumanMessage(
                    content=f"{_NO_SYSTEM_SUMMARY_PREFIX}{summary}",
                    additional_kwargs={"lc_source": "liorium_compact"},
                )
            ]

        # 跨压缩保留钩子（VNG 通知的 Liorium 等价物，I17）：任何异常都不得影响压缩。
        if self.preserve_predicate is not None:
            try:
                extras = self.preserve_predicate(list(messages), list(preserved)) or []
                replacement.extend(message for message in extras if message is not None)
            except Exception as exc:  # noqa: BLE001 - 通知保留失败不应影响压缩
                log.debug("压缩后需要保留的额外消息插入跳过: %s", exc)
        return replacement

    async def _emit_event(self, event: Event) -> None:
        """把事件推给宿主（``emit`` 为 None 时静默丢弃）。"""
        if self.emit is None:
            return
        await self.emit(event)

    async def _emit_done(self, ok: bool, error: str | None = None) -> None:
        """收尾压缩卡片。成功路径**绝不**带 ``ok=False``（I15）。"""
        payload: dict[str, Any] = {"ok": ok}
        if error:
            payload["error"] = error
        await self._emit_event(Event("compact_done", payload))

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """模型调用前判定是否压缩；需要时返回写回的 state update（§6 / §11.1）。

        严格顺序：补 id → 读上报值（0 ⇒ 跳过 + 告警一次）→ 比阈值 → 算切点
        → 生成摘要 → 写回。任一环节失败都返回 ``None``（不改动 checkpoint，本轮继续）。
        """
        messages = state["messages"]
        self._ensure_message_ids(messages)

        if self.threshold_tokens <= 0:
            if not self._warned_no_context_window:
                self._warned_no_context_window = True
                log.warning(
                    "上下文窗口未知（context_window=%s）：自动压缩不会触发。"
                    "请在宿主配置里提供模型的上下文长度（Limits.context_window）",
                    self.context_window,
                )
            return None

        # 判定一律基于 API 上报值；没有 API 数据时不猜、不压缩（I1）。
        reported = _reported_tokens(messages, self.api_format)
        if reported <= 0:
            if not self._warned_no_usage and any(
                isinstance(message, AIMessage) for message in messages
            ):
                self._warned_no_usage = True
                log.warning(
                    "模型未上报 token 用量（api_format=%s）：自动压缩不会触发。"
                    "若该网关需要 stream_options.include_usage，请检查 Provider 的"
                    " stream_usage 设置；否则请手动 /compact",
                    self.api_format,
                )
            return None
        if not self._should_summarize(messages, reported):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            return None

        _to_summarize, preserved = self._partition_messages(messages, cutoff_index)
        preserved = self._with_turn_anchor(messages, cutoff_index, preserved)
        try:
            # 传完整 messages：payload 前缀与主请求逐字节一致（含同样的 tool_call
            # 自修复），这是命中上游前缀缓存的前提，不要改成只传被丢弃的段落。
            summary = await self._acompact(messages)
        except Exception as exc:  # noqa: BLE001 - 失败降级：不动 checkpoint，本轮继续
            log.error("会话压缩失败（已重试），本次不改动 checkpoint，本轮继续: %s", exc)
            await self._emit_done(False, str(exc))
            return None
        if not summary:
            log.error("会话压缩结果为空，本次不改动 checkpoint")
            await self._emit_done(False, "会话压缩结果为空")
            return None

        update = {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *self._build_replacement(messages, summary, preserved),
                *preserved,
            ]
        }
        log.info(
            "会话压缩完成: 上报 tokens=%d 阈值=%d 压缩 %d 条、保留 %d 条",
            reported,
            self.threshold_tokens,
            cutoff_index,
            len(preserved),
        )
        await self._emit_done(True)
        return update

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """同步入口**不提供**（§2 的已知不对称）。

        基类的同步路径会走 ``_create_summary`` + ``_build_new_messages``：摘要写成
        ``Here is a summary of the conversation to date:`` 前缀的 HumanMessage，语义与
        本类的写回完全不同。Liorium 是异步优先（回合循环只有 ``ainvoke``），宁可在这里
        响亮失败，也不要让两条路径分叉、把错误格式的摘要写进 checkpoint。
        """
        raise NotImplementedError(
            "CompactMiddleware 只提供异步路径（abefore_model）；"
            "同步 before_model 会让摘要写回退化成基类格式，因此显式禁用"
        )

    # ── 手动触发（/compact，§11.2） ──

    async def force_compact(
        self, messages: list[AnyMessage], on_delta: Any = None
    ) -> dict[str, Any] | None:
        """无视阈值强制压缩一次，返回可交给 ``aupdate_state`` 的 state update。

        供宿主的 ``/compact`` 命令使用：它不在 graph 运行中，因此需要显式把更新写回
        checkpoint（必须带 ``as_node="model"``，否则图有 model/tools 两节点时
        langgraph 报 Ambiguous update），并自行推送 ``compact_*`` 事件。

        与自动路径的唯一区别是"无视阈值"；失败**上抛**（宿主渲染失败卡片），
        但上抛前会先发一条 ``compact_done(ok=False, error=...)`` 收尾卡片。
        """
        if not messages:
            return None
        self._ensure_message_ids(messages)
        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            log.info("会话压缩跳过：没有可压缩的历史轮次")
            return None
        _to_summarize, preserved = self._partition_messages(messages, cutoff_index)
        preserved = self._with_turn_anchor(messages, cutoff_index, preserved)
        try:
            # 与自动压缩一致：传完整 messages 以复用主请求前缀（缓存前提）。
            summary = await self._acompact(messages, on_delta=on_delta)
        except Exception as exc:  # noqa: BLE001 - 手动路径要上抛给命令处理器
            log.error("手动会话压缩失败: %s", exc)
            await self._emit_done(False, str(exc))
            raise
        if not summary:
            log.error("手动会话压缩结果为空")
            await self._emit_done(False, "对话压缩结果为空")
            raise RuntimeError("对话压缩结果为空")

        update = {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *self._build_replacement(messages, summary, preserved),
                *preserved,
            ]
        }
        log.info("手动会话压缩完成: 压缩 %d 条、保留 %d 条", cutoff_index, len(preserved))
        await self._emit_done(True)
        return update


__all__ = [
    "COMPACT_INSTRUCTION",
    "COMPACT_MARKER_END",
    "COMPACT_MARKER_START",
    "COMPACT_TAG",
    "CompactMiddleware",
    "EmitFn",
    "PreservePredicate",
    "build_mock_tools",
    "ensure_message_ids",
    "ev_compact_delta",
    "ev_compact_start",
    "find_safe_cutoff_point",
    "forced_no_tool_choice",
    "message_content_to_text",
]
