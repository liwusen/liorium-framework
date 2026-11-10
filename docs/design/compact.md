# 会话压缩（Compact）实现规格

> 状态：行为规格（2026-11-04 冻结）
> 目标读者：在 Liorium 中重写压缩模块的工程师。**本文是唯一必要输入**。
> 依据测试：`packages/liorium-agent/tests/test_compact_*.py`
> 相关决策条目：`docs/plans/2026-11-04-liorium-agent-core.md` 的 C8、E4、F6、§5、§7（"compact.py 行为回归 = 最高风险：压缩不可逆"）。

本文出现的字符串、标识符、路径、日志文案均**逐字取自原型**，属于冻结格式，重写时不得"顺手润色"。

---

## 1. 模块定位

压缩是 Agent 的**保命机制**（始终启用，不受任何插件/配置开关控制是否装配），职责是在上下文接近模型上限时，把旧对话折叠成一份结构化中文摘要写回消息列表，从而释放空间、让会话继续。

三条不可动摇的设计前提：

1. **触发判据只认 LLM API 上报的 token 用量**。绝不用 tiktoken / 字符数 / 消息条数做本地推算来决定"是否压缩"。拿不到上报值 ⇒ 不压缩（并告警一次），而不是猜。
2. **压缩调用必须复用主请求的逐字节前缀**（同样的 System、同样的 tools schema、同样的 messages），以命中上游**前缀缓存**。因此发送的 payload 是"当前完整 messages + 一条尾端追加的指令 HumanMessage"，**不是**"只发送将被丢弃的那一段"。
3. **失败不隐瞒也不打断**：重试后仍失败 ⇒ 不改动 checkpoint，本轮继续，错误进日志。压缩绝不把异常抛给回合执行路径（手动 `/compact` 是唯一例外，它把异常抛给命令处理器以渲染失败卡片）。

## 2. 依赖与继承

```
langchain.agents.middleware.summarization.SummarizationMiddleware   ← 基类（唯一继承）
  └─ CompactMiddleware                                              ← 原型实现
langchain.agents.middleware.types.AgentMiddleware                   ← 基类的基类（hook 定义处）
```

`CompactMiddleware` **只替换**四个扩展点（3 个同名覆盖 + 1 个改名替代）：

| 扩展点 | 原型方法 | 基类方法 |
|---|---|---|
| 切点判定 | `_determine_cutoff_index(self, messages: list[AnyMessage]) -> int` | 同名（`keep=("messages", N)` 的通用实现） |
| 摘要生成 | `async _acompact(self, messages: list[AnyMessage], on_delta=None) -> str` | `_create_summary` / `_acreate_summary`（**改名替代**，基类方法不再被调用） |
| 写回 | `_build_replacement(self, messages, summary, preserved=()) -> list[AnyMessage]` | `_build_new_messages(summary)`（**改名替代**） |
| 异步 hook | `async abefore_model(self, state, runtime) -> dict[str, Any] \| None` | 同名（基类内部用 `_acreate_summary` + `_build_new_messages`） |

**完全复用**的基类能力（这些私有方法在 Liorium 里必须重新实现或等价替换，语义见 §7、§15）：

- `_should_summarize(messages, total_tokens) -> bool`
- `_find_safe_cutoff_point(messages, cutoff_index) -> int`（staticmethod）
- `_ensure_message_ids(messages) -> None`（staticmethod）
- `_partition_messages(conversation_messages, cutoff_index) -> tuple[list, list]`
- `self._summary_model = model.with_retry()`（基类构造时建立，压缩路径又重新建了一次，见 §8.4）
- 类属性 `transformers = (InternalCallTransformer,)`（基类声明，未覆盖）：让带内部调用元数据的模型调用 token 不进入 `run.messages`。

**未覆盖** `before_model`（同步入口）。这是已知不对称：同步路径会走基类的 `_create_summary` + `_build_new_messages`，语义与异步路径不同（摘要写成 `Here is a summary of the conversation to date:` 前缀的 HumanMessage）。Liorium 若不再需要同步路径，应直接不提供；若要保留，必须补齐等价行为，不要让两条路径分叉。

## 3. 对外符号清单（模块级）

| 符号 | 类型 | 签名 / 值 |
|---|---|---|
| `COMPACT_TAG` | `str` | `"liorium_compact_summarizer"` |
| `COMPACT_MARKER_START` | `str` | `"<!-- liorium:compact-summary:start -->"` |
| `COMPACT_MARKER_END` | `str` | `"<!-- liorium:compact-summary:end -->"` |
| `COMPACT_INSTRUCTION` | `str` | 见 §4（完整指令文本，冻结） |
| `log` | `Logger` | `get_logger("liorium.compact")`（若用标准 logging：`logger name = "liorium.compact"`） |
| `CompactMiddleware` | `class` | 见 §5 |
| `build_mock_tools(tools: Sequence[BaseTool] \| None) -> list[BaseTool]` | 函数 | 见 §14.1 |
| `build_compact_middleware(*, model: Any, tools: Sequence[BaseTool] \| None, model_spec: str \| None) -> CompactMiddleware \| None` | `async` 函数 | 见 §14.6 |
| `_reported_tokens(messages: Sequence[AnyMessage], api_format: str = "openai_chat") -> int` | 函数 | 见 §6 |
| `_strip_summary(content: Any) -> str` | 函数 | 见 §14.2 |
| `_wrap_summary(prompt_text: str, summary: str) -> str` | 函数 | 见 §14.3 |
| `_is_compact_summary(message: Any) -> bool` | 函数 | 见 §14.4 |
| `_content_to_text(content: Any) -> str` | 函数 | 见 §14.5 |
| `message_content_to_text` | 转发导出 | 来自旧后端 `core.message`（模块级 import 后即成为本模块属性，测试通过 `compact` 间接使用） |

`_mock_tool(tool: BaseTool) -> BaseTool` 为内部辅助（见 §14.1）。

## 4. 冻结文本：压缩指令

`COMPACT_INSTRUCTION` 全文（**逐字**，含空行与标点；末尾无换行）：

```
停止你当前正在执行的任务，不要再调用任何工具，也不要继续之前的步骤。

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
- 直接输出摘要正文，不要有任何前言或结束语。
```

注意：指令里同时存在**两处**"不要调用工具"的约束点（第一句"不要再调用任何工具"，以及 `tool_choice` 禁止工具）。测试 `test_compact_call_reuses_full_prefix_with_mock_tools` 断言指令文本包含子串 **`"调用任何工具"`**——改写文案会直接打破该断言。

指令以 `HumanMessage(content=COMPACT_INSTRUCTION)` 形式**追加在 payload 末尾**，不是 System 的一部分。理由：改 System 会改变被缓存的前缀（System 位于前缀最前端）。

## 5. 类 `CompactMiddleware`

### 5.1 构造签名与默认值

```python
def __init__(
    self,
    *,
    model: Any,
    compact_model: Any,
    tools: Sequence[BaseTool] | None = None,
    context_length: int,
    threshold_ratio: float = 0.8,
    keep_rounds: int = 3,
    api_format: str = "openai_chat",
    server_tool_specs: Sequence[dict[str, Any]] | None = None,
    summary_max_tokens: int = 0,
) -> None
```

全部参数为 keyword-only。`context_length`、`model`、`compact_model` 必填。

| 参数 | 语义 |
|---|---|
| `model` | 主模型实例。仅用于：基类构造（`with_retry`、`_llm_type` 选近似计数器、profile），以及基类 `_should_summarize` 的 reported-token 兜底路径。**不用于生成摘要**（摘要用 `compact_model`）。 |
| `compact_model` | 摘要专用模型，已关闭 thinking（reasoning 参数不属于被缓存前缀，关掉不破缓存，且避免思考 token 吃掉摘要余量）。 |
| `tools` | 与主请求一致的工具列表；构造时立刻转成 mock（`build_mock_tools`），不保留原引用。 |
| `context_length` | 当前模型上下文长度（token），来自 provider 配置。 |
| `threshold_ratio` | 触发比例，默认 `0.8`（留 20% 给指令与摘要输出）。 |
| `keep_rounds` | 压缩后保留最近多少"轮"（一条 HumanMessage 及其后全部消息为一轮）。 |
| `api_format` | 主模型 API 格式：`openai_chat` / `openai_responses` / `anthropic_messages` / `google_gemini` 等。决定用量口径与 `tool_choice` 写法。 |
| `server_tool_specs` | 主请求启用的**服务端工具**规格（provider 托管工具，如 `{"type": "web_search_20250305", "name": "web_search"}`）。压缩请求必须带同样的 specs，否则 tools 前缀与主请求不一致，前缀缓存整段失效。构造时**深拷贝为 `[dict(spec) for spec in ...]`**。 |
| `summary_max_tokens` | 压缩模型自身的输出上限（Anthropic 构造时已设定）。仅用于"摘要是否被该上限截断"的判定；`0` = 未知/不适用。 |

### 5.2 构造体行为

```python
threshold = max(1, int(context_length * float(threshold_ratio)))
super().__init__(
    model=model,
    trigger=("tokens", threshold),
    keep=("messages", max(1, int(keep_rounds))),
    trim_tokens_to_summarize=None,
)
```

- `trigger` 用**绝对 token 阈值**而非 `("fraction", ...)`：fraction 依赖 `model.profile.max_input_tokens`，本项目按 provider 配置持有上下文长度，不依赖 profile（也避免基类因缺 profile 直接 `ValueError`）。
- `keep` 是**占位**：实际保留策略由被覆盖的 `_determine_cutoff_index` 按"轮次"决定。保留它只为满足基类构造校验（必须 > 0）。
- `trim_tokens_to_summarize=None`：压缩调用要复用完整前缀，**不做任何裁剪**（基类默认是 `4000`，会把 payload 截短 → 前缀失效）。

构造后写入实例属性：

```python
self.compact_model = compact_model
self.mock_tools = build_mock_tools(tools)          # list[BaseTool]
self.context_length = int(context_length)
self.threshold_ratio = float(threshold_ratio)
self.threshold_tokens = threshold
self.keep_rounds = max(1, int(keep_rounds))
self.api_format = str(api_format or "openai_chat")
self.server_tool_specs = [dict(spec) for spec in (server_tool_specs or [])]
self.summary_max_tokens = int(summary_max_tokens or 0)
self._cost_cap_bypassed = False   # 输出上限曾导致截断 → 本进程内不再施加成本上限
self._tool_choice_warned = False  # tool_choice 绑定失败只告警一次
self._warned_no_usage = False     # "无 API 用量"告警只发一次
```

### 5.3 基类构造的隐含要求（测试替身必须满足）

- `model.with_retry()` 必须存在（基类存为 `self._summary_model`）。
- `model._llm_type` 必须可读（`startswith("anthropic-chat")` 决定近似计数器参数）。
- `model.profile` 可缺省：只有 `fraction` 触发或 `keep` 为 fraction 时才强制要求。
- 测试替身用 `_llm_type = "openai-chat"` / `"fake"`、`profile: dict = {}`。

## 6. 触发判定与 token 计量

`abefore_model` 的判定顺序（**严格**）：

```python
messages = state["messages"]
self._ensure_message_ids(messages)

reported = _reported_tokens(messages, self.api_format)   # 0 = 无上报数据
if reported <= 0:
    if not self._warned_no_usage and any(isinstance(m, AIMessage) for m in messages):
        self._warned_no_usage = True
        log.warning(...)                                  # 见 §13
    return None

if not self._should_summarize(messages, reported):        # 用上报值当 total_tokens
    return None

cutoff_index = self._determine_cutoff_index(messages)
if cutoff_index <= 0:
    return None
```

### 6.1 `_reported_tokens`

```python
def _reported_tokens(messages: Sequence[AnyMessage], api_format: str = "openai_chat") -> int:
    return usage_mod.context_tokens(messages, api_format)
```

`usage_mod = <旧后端的 usage 模块>`，`context_tokens(messages, api_format) -> int`：**从后向前**扫描，取最近一条**带用量上报**的 `AIMessage`，归一化后返回 `context_tokens`；一条都没有时返回 `0`。

归一化规则（`normalize_usage`，必须等价实现）：

| 格式 | `input_tokens` | `context_tokens` |
|---|---|---|
| `anthropic_messages` | `raw.input_tokens + raw.cache_read_input_tokens + raw.cache_creation_input_tokens`，再取 `max(上述, usage_metadata.input_tokens)`（新版 SDK 已把缓存折进 metadata，取较大值避免重复相加） | `input_tokens + output_tokens` |
| 其他（`openai_chat` / `openai_responses` / `google_gemini`） | `usage_metadata.input_tokens`，退化到 `raw.prompt_tokens` | `total_tokens`（退化到 `raw.total_tokens`；再退化到 `input+output`） |

要点：

- **Anthropic 的缓存读写必须计入 input**，否则 prompt 缓存命中率高的长会话永远显示很小占用、自动压缩静默失效。
- **OpenAI/Gemini 的 `cache_read` 是 input 的子集**，绝不能相加（`1000+800+100` ≠ `1900`，应为 `1100`）。
- 数据来源优先级：`message.usage_metadata`（dict）与 `message.response_metadata["usage"]`（原始 dict）二选一合并；两者都空 ⇒ `normalize_usage` 返回 `None`。
- "最近一条带用量的 AIMessage"，**不是**"最后一条 AIMessage"。最后一条可能是本地构造/插件注入的消息（无 `usage_metadata`）；若因此返回 0，自动压缩会静默失效，会话一路涨到必须手动 `/compact`（最贵路径）。这是回归测试 1 的核心。

### 6.2 多模态 / 图片 token

- **不本地统计图片 token**：触发判据完全来自 API 上报值，图片由 provider 计费与计数，压缩侧不做任何换算。这是有意的——任何本地图片估算都会与上游口径漂移。
- 唯一与图片有关的本地近似在**成本控制的输入裁剪**（`_apply_input_cap`，§9）：它对非字符串 content 取 `len(str(content))`，即把整个 content block 列表（含 base64 / URL）当字符数计。这是粗估，**不是** token 语义，也**不参与触发判定**。
- 摘要流式文本提取只取 `type == "text"` 的块（`message_content_to_text`），摘要模型输出里的图片/推理/工具块一律丢弃。

### 6.3 `_should_summarize` 的实际语义

基类实现（`trigger=("tokens", threshold)` 归一化成 `[{"tokens": threshold}]`）：

```
对每个 clause：clause_met = True
  tokens: 若 (total_tokens < threshold) 且 (基类自己的 reported 兜底不为真) → clause_met = False
返回是否存在 clause_met
```

因为调用方传入的 `total_tokens` 就是上报值，等价于：**`reported >= threshold` 即触发**。基类的兜底路径（取最后一条 AIMessage 的 `usage_metadata.total_tokens`，并要求 `response_metadata["model_provider"]` 与 `model._get_ls_params()["ls_provider"]` 匹配）在我们的调用方式下几乎不会被走到；**Liorium 不要依赖它**（它在 `total_tokens < threshold` 且最后一条 AI 的 total 恰好 ≥ 阈值时才生效，且依赖 provider 别名表与 `_get_ls_params`）。等价重写为两行即可：

```python
threshold = self.threshold_tokens
triggered = reported >= threshold
```

### 6.4 "不重复压缩"的自愈逻辑

压缩后 checkpoint 里的最后一条 `AIMessage` **仍带着压缩前的 `usage_metadata`**，因此下一回合 `reported` 仍然 ≥ 阈值。防止"每回合都再压一次"的**不是** token 判定，而是切点算法里的两道闸（§7）：丢弃段必须含真实轮次、摘要消息不算一轮。重写时若丢掉这两道闸，会立刻表现为"每回合一次全量摘要调用"（每次都是最贵的一条路径）。

## 7. 切点算法

### 7.1 `_determine_cutoff_index`

```python
@override
def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int
```

定义：**轮次起点** = 一条 `HumanMessage`，且 `not _is_compact_summary(message)`（压缩写回的摘要不算一轮）。逐条：

1. `round_starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not _is_compact_summary(m)]`
2. `round_starts` 为空 ⇒ 返回 `0`。
3. 取保留边界：
   - `len(round_starts) > self.keep_rounds` ⇒ `cutoff = round_starts[-self.keep_rounds]`
   - 否则（轮数不足）⇒ `cutoff = round_starts[-1]`（**退化为只保留最后一轮**，保证仍能压出空间，而不是因为"轮数不够"完全不压）
4. **丢弃段必须至少含一条真实轮次**：若 `messages[:cutoff]` 里没有任何"非摘要的 HumanMessage" ⇒ 返回 `0`（否则只是把已有摘要重新摘要一遍：白烧一次调用，状态不会变小）。
5. **不能切进 System**：`system_count = sum(1 for m in messages if isinstance(m, SystemMessage))`；若 `cutoff <= system_count` ⇒ 返回 `0`。
6. 返回 `self._find_safe_cutoff_point(messages, cutoff)`（tool 组保护，见 §7.2）。

注意第 3 步的语义：`keep_rounds` 是"最多保留 N 轮"，不是"至少"。`keep_rounds=3` 且只有 3 轮时，保留 1 轮、压掉 2 轮。

### 7.2 `_find_safe_cutoff_point`（基类 staticmethod，必须等价实现）

签名：`_find_safe_cutoff_point(messages: list[AnyMessage], cutoff_index: int) -> int`

语义：切点若落在 `ToolMessage` 上，把切点**向前回退**到发起该 `tool_calls` 的 `AIMessage`，使 `AIMessage(tool_calls) + ToolMessage` 整组落在**同一侧**（此处策略是"整组一起被压缩"，比"整组保留"更激进）。

```
若 cutoff_index >= len(messages) 或 messages[cutoff_index] 不是 ToolMessage → 原样返回
收集从 cutoff_index 起连续 ToolMessage 的 tool_call_id 集合 tool_call_ids
从 cutoff_index-1 向前扫：
  遇到 AIMessage 且其 tool_calls 的 id 与 tool_call_ids 有交集 → 返回该 AIMessage 的下标 i
兜底（找不到匹配的 AIMessage）：返回"越过这些 ToolMessage 之后"的下标 idx（避免留下孤立 tool 结果）
```

方向很重要：**回退（变小）**，不是前进。所以切点可能比"按轮次算出的起点"更靠前，即 `AIMessage(tool_calls)` 被划入**丢弃段**（进而被压缩调用读到、写出摘要），而它后面的 `ToolMessage` 也被划入丢弃段——绝不允许出现"ToolMessage 被保留但它的 AIMessage 被压缩"的悬空。

### 7.3 用例（可作移植验收的黄金样例）

`keep_rounds=1`，`[S, H(r1), A, H(r2), A, H(r3), A]`（S=System，H=Human，A=AI）：

- `round_starts=[1,3,5]`，`3 > 1` ⇒ `cutoff=5`；丢弃段含真实轮次 r1；`system_count=1`，`5>1`；`messages[5]` 是 Human 不是 Tool ⇒ 返回 **5**。
- 保留区 `messages[5:]` = `[H(r3), A]`。

`keep_rounds=3`，`[H(summary, lc_source=liorium_compact), H(r1), A, H(r2), A, H(r3), A]`：

- `round_starts=[1,3,5]`（0 被排除），`3 > 3` 为假 ⇒ `cutoff=round_starts[-1]=5`；丢弃段含真实轮次 ⇒ **5**（不是 1）。这是回归测试 2 的断言点。

`keep_rounds=1`，`[S, H(summary), H(r1), A]`：

- `round_starts=[2]`，`1 > 1` 为假 ⇒ `cutoff=2`；丢弃段 `[S, summary]` 无真实轮次 ⇒ **0**（不压缩）。

`keep_rounds=1`，`[S, H(only)]`：

- `round_starts=[1]`，`cutoff=1`；丢弃段无真实 Human ⇒ **0**。

切点落在 ToolMessage 上（`keep_rounds=1`，`[S, H(r1), A(tool_calls[c1]), T(c1), H(r2), A]`）：

- `round_starts=[1,4]` ⇒ `cutoff=4`（`messages[4]` 是 Human，不是 Tool）⇒ 返回 4。tool 组天然同侧。
- 真正会被回退的场景是"轮次起点恰好是 ToolMessage"：只可能出现在没有对应 Human 起点的畸形历史里；此时靠 §7.2 兜底。

## 8. 压缩调用 `_acompact`

```python
async def _acompact(self, messages: list[AnyMessage], on_delta=None) -> str
```

### 8.1 payload 构造顺序（顺序即契约）

```python
if not messages:
    return ""                       # 不发调用
cost_control = self._cost_control_active()
if cost_control:
    messages = self._apply_input_cap(list(messages))        # 仅在成本控制开启时
payload = [
    *repair_dangling_tool_calls(list(messages)),            # 先修复
    HumanMessage(content=COMPACT_INSTRUCTION),              # 再追加指令
]
```

- **修复必须在追加指令之前**：修复器区分"被打断"（清空 `tool_calls`）与"序列结尾真悬空"（注入占位 `ToolMessage`）。若先追加指令，原本位于序列结尾的悬空会被判成"被 Human 打断"→ 作废而不是注入占位，上游对"缺 tool 结果"仍可能报错/语义丢失。测试 `test_compact_call_placeholder_for_trailing_dangling_tool_calls` 断言：占位必须在指令**之前**（`sent[-1]` 是 HumanMessage，`sent[-2]` 是 ToolMessage）。
- `repair_dangling_tool_calls(list(messages))` 返回**新列表**，且只对需要清空 `tool_calls` 的 `AIMessage` 做 `model_copy(deep=True)`；其余消息对象共享。它不修改入参、不写回 checkpoint。
- 压缩调用**不经过中间件链**（直接打 provider），所以必须自己修复；主模型调用由 `ToolCallRepairMiddleware` 兜住。

### 8.2 工具前缀与 `tool_choice`

```python
tool_specs = [*self.mock_tools, *self.server_tool_specs]     # 客户端 mock 在前，服务端 specs 在后
runnable = self._bind_compact_tools(self.compact_model, tool_specs)
```

`_bind_compact_tools(self, model, tool_specs)` 的三级降级：

1. `tool_specs` 为空 ⇒ 直接返回 `model`（不绑）。
2. `choice = forced_no_tool_choice(self.api_format)`；`model.bind_tools(tool_specs, tool_choice=choice)`。
3. 失败（`Exception`）⇒ 只告警一次（`_tool_choice_warned`），再试 `model.bind_tools(tool_specs)`（无 `tool_choice`）。
4. 再失败 ⇒ 若有 `server_tool_specs` 告警"本次省略"；若 `self.mock_tools` 非空 ⇒ `model.bind_tools(self.mock_tools)`。
5. 最后仍不行 ⇒ 返回 `model`（不绑工具）。

`forced_no_tool_choice(api_format) -> str | dict[str, str]`：

| `api_format` | 值 |
|---|---|
| `anthropic_messages` | `{"type": "none"}` |
| 其他（OpenAI Chat/Responses、Gemini） | `"none"` |

Anthropic 必须用 dict：SDK 的字符串 `tool_choice` 只接受 `auto` / `any` / 工具名，传 `"none"` 会被解释成"调用名为 none 的工具"。

### 8.3 输出上限

```python
max_out = conf.COMPACT_MAX_OUTPUT_TOKENS if cost_control else 0
if max_out > 0:
    try:
        runnable = runnable.bind(max_tokens=max_out)
    except Exception:
        log.debug(...); max_out = 0
effective_max = max_out or self.summary_max_tokens
```

- 默认（成本控制关闭）**不施加任何输出上限**，摘要不可能被截断。
- `bind` 失败（自定义模型/测试替身不支持）⇒ 退化为不设上限，**不让压缩失败**。
- `effective_max` = 成本上限优先，其次模型自身上限（Anthropic 构造时就定了）。

### 8.4 重试与流式

```python
runnable = runnable.with_retry()
```

`Runnable.with_retry()` 默认：`stop_after_attempt=3`（共 3 次尝试）、`retry_if_exception_type=(Exception,)`、`wait_exponential_jitter=True`。瞬态故障（429、超时、5xx）在**进程内**重试；仍失败则异常上抛给 `abefore_model` 的 `try/except`（降级为不压缩），或上抛给 `force_compact` 的调用方（`/compact` 失败卡片）。

调用形态：

```python
async for chunk in runnable.astream(
    payload,
    config={"tags": [COMPACT_TAG], "metadata": {"liorium_event": "compact"}},
):
    reported = getattr(chunk, "usage_metadata", None)
    if reported:
        usage = reported                                  # 取最后一个真值
    meta = getattr(chunk, "response_metadata", None) or {}
    if meta.get("finish_reason"):
        finish_reason = meta["finish_reason"]             # OpenAI
    elif meta.get("stop_reason"):
        finish_reason = meta["stop_reason"]               # Anthropic
    text = _content_to_text(getattr(chunk, "content", ""))
    if text:
        chunks.append(text)
        if on_delta is not None:
            result = on_delta(text)
            if hasattr(result, "__await__"):
                await result
summary = "".join(chunks).strip()
```

- `tags=[COMPACT_TAG]` 是**事件分流契约**：lifecycle 的流式生产者据此把该调用的 `on_chat_model_stream` 转成 `compact_delta`，绝不泄进正文 `delta`（§12）。
- `metadata={"liorium_event": "compact"}` 供追踪/日志识别。
- `on_delta` 支持同步或异步回调（用 `hasattr(result, "__await__")` 判断）。自动压缩路径**不传**（靠 astream_events 的 tag 分流）；手动 `/compact` 路径传入（它不在 graph 运行中，拿不到 astream_events）。
- 摘要 = 所有分片文本拼接后 `.strip()`；**空摘要**由调用方判为失败（§10、§11）。

### 8.5 截断判定

```python
@staticmethod
def _is_truncated(max_out: int, finish_reason: Any, usage: Any) -> bool
```

```
max_out <= 0                          → False
lower(finish_reason) in {"length", "max_tokens"} → True
usage 是 dict 且 usage["output_tokens"] >= max_out → True
否则 → False
```

即：OpenAI 用 `finish_reason == "length"`，Anthropic 用 `stop_reason == "max_tokens"`，部分 provider 都不返回则用上报 `output_tokens` 触顶兜底；三者都拿不到 ⇒ 按未截断处理（**不制造假失败**）。

截断的处理是**抛错而不是写回**：

```python
if self._is_truncated(effective_max, finish_reason, usage):
    self._cost_cap_bypassed = max_out > 0
    raise RuntimeError(截断消息)
```

理由：写回就等于永久丢掉"摘要没覆盖到的历史"（原文会被 `_build_replacement` 删除）。同时**自愈**：若本次上限来自成本控制，就在本进程内放弃该上限，避免每回合都用过小的上限白烧一次调用。异常文案（逐字拼接，注意两处"；"分隔）：

```
摘要被输出上限 {effective_max} 截断（finish_reason={finish_reason}，api_format={self.api_format}），本次不写回 checkpoint 以免丢失摘要未覆盖的历史
  + （若 max_out > 0）"；已自动放弃 COMPACT_MAX_OUTPUT_TOKENS，请调大该值或关闭 COMPACT_COST_CONTROL_ENABLED"
  + （否则）          "；该上限来自模型自身 max_tokens，请调大模型的 model_max_output_tokens"
```

测试断言：`test_compact_raises_on_truncated_summary` 要求消息含 `"4096"`（即 `effective_max`）；`test_truncated_summary_raises_and_cap_self_heals` 要求匹配 `"截断"`。

### 8.6 用量日志

`_log_usage(usage, api_format="openai_chat")`（staticmethod），在**截断判定之前**调用，因此成功与失败都会记账：

- 无 usage：`log.info("压缩调用用量: 提供商未返回 usage_metadata（无法核算成本）")`
- 有 usage：`log.info("压缩调用用量(%s): input=%s output=%s cache_read=%s cache_creation=%s reasoning=%s total=%s", ...)`
  - `input_token_details.cache_read` 是"前缀缓存有没有命中"的唯一依据；远小于 `input_tokens` ⇒ 前缀没对齐，整段历史按全价计费。
  - `input_token_details.cache_creation`（Anthropic 写缓存量）单独打印，用于区分"写缓存"与"读缓存"。
  - `output_token_details.reasoning` 单独打印。

## 9. 成本控制（默认关闭）

### 9.1 `_cost_control_active()`

```python
if self._cost_cap_bypassed:
    return False
return bool(conf.COMPACT_COST_CONTROL_ENABLED)
```

配置键语义（`config.py` 读取口径一并冻结）：

| 键 | 默认 | 读取规则 |
|---|---|---|
| `COMPACT_THRESHOLD_RATIO` | `0.8` | `float(config.get(...) or 0.8)`；异常回退 0.8；**不在 [0.1, 0.95] 内回退 0.8** |
| `COMPACT_KEEP_ROUNDS` | `3` | `int(...)`；异常回退 3；`< 1` 回退 3 |
| `COMPACT_COST_CONTROL_ENABLED` | `False` | 字符串按 `{"1","true","yes","on"}` 解析，否则 `bool(...)` |
| `COMPACT_MAX_INPUT_TOKENS` | `0`（不限制） | `int(...)`，`< 0` 归 0；**需开关打开才生效** |
| `COMPACT_MAX_OUTPUT_TOKENS` | `4000` | `int(...)`，`< 0` 归 0；**需开关打开才生效** |

五个键必须出现在公共配置默认值（前端"AI 服务商/高级配置"靠 `默认值 ∪ 配置文件` 渲染），见测试 `test_cost_control_keys_are_exposed_to_frontend`。

### 9.2 `_apply_input_cap(messages)`（staticmethod）

仅在成本控制开启且 `COMPACT_MAX_INPUT_TOKENS > 0` 时生效：

```
budget_chars = int(cap * 2.5)          # 中文环境约 2.5 字符/token
从最后一条往前累加 len(content)（非 str 时用 len(str(content))）
超预算且已保留至少一条 → 停止（保证至少留 1 条）
reverse 回正序返回
```

超限时 `log.warning`："压缩输入超过 COMPACT_MAX_INPUT_TOKENS=%d（约 %d 字符），已丢弃最旧的 %d 条消息：摘要覆盖范围变小（这部分历史将永久丢失），且 payload 不再与主请求前缀一致（前缀缓存只剩 System 与工具定义可用）"

两个必须写进文档的代价：**打破前缀缓存**（payload 不再以对话起点开头）、**缩小摘要覆盖范围**（被丢的最旧消息不进摘要，但照样被写回删除 ⇒ 永久丢失；cap 过小还会丢进本应保留的尾部轮次）。因此默认 0。

## 10. 写回 `_build_replacement`

```python
def _build_replacement(
    self,
    messages: list[AnyMessage],
    summary: str,
    preserved: list[AnyMessage] | tuple[AnyMessage, ...] = (),
) -> list[AnyMessage]
```

**只返回"头部"**（合并后的 System + 摘要，或摘要 HumanMessage，可能加 VNG 通知）；`preserved` 由调用方拼在末尾。`preserved` 参数的**唯一用途**是 VNG 通知去重。

### 10.1 有 SystemMessage：合并成唯一一条并追加摘要

```
system_messages = [m for m in messages if isinstance(m, SystemMessage)]
head = system_messages[0]
若只有 1 条：merged_text = _strip_summary(head.content)
若多条：  merged_text = "\n\n".join(非空的 _strip_summary(m.content) for m in system_messages)
merged = head.model_copy(update={"content": _wrap_summary(merged_text, summary)})
replacement = [merged]
```

- **多条 System 必须合并为一条**：Gemini 只认第一条 system instruction（其余被丢），Anthropic 要求 system 是单独参数。
- 合并**保留原有顺序**与内容，每段先剥掉上一轮摘要（保证 System 不随压缩次数增长），新摘要追加在末尾。
- `model_copy(update=...)` 保留 `head` 的 `id` 与其余字段（测试 `test_build_replacement_merges_into_system` 断言 `only.id == "s1"`）。保留 id 让 `add_messages` reducer **就地替换**那条 System，而不是新增一条。

### 10.2 无 SystemMessage（Subagent）：摘要退化为首条 HumanMessage

```python
HumanMessage(
    content=(
        "以下是此前对话的压缩摘要（背景资料，不是新的用户请求）：\n\n"
        f"{summary}"
    ),
    additional_kwargs={"lc_source": "liorium_compact"},
)
```

`lc_source="liorium_compact"` 是**幂等标记**：`_is_compact_summary` 据此不把它当一轮（§7.1），否则"摘要 + 保留 N 轮"永远比 `keep_rounds` 多一轮，切点恒 > 0，压缩后每回合都再压一次。

背景：Subagent 的 system prompt 由 `create_agent` 在**调用时注入**，不在 state 里；主 Agent 的 state 里也可能没有 System。

### 10.3 VNG（视觉小说）模式通知跨压缩保留

```python
try:
    from <旧后端>.agent import vng as vng_mode
    notice = vng_mode.latest_vng_notice_message(messages)
    if notice is not None and not any(vng_mode.is_vng_notice(m) for m in preserved):
        replacement.append(notice)
except Exception:
    log.debug("VNG 模式通知跨压缩保留跳过: %s", exc)
```

- 通知是一条普通 `HumanMessage`（`additional_kwargs={"lc_source": "liorium_vng_mode", "vng_mode": "on"|"off"}`）。若落在被丢弃段，Agent 会立刻忘记自己仍处于该模式，故重新插在"System/摘要之后、保留轮次之前"。
- `preserved` 里已有通知则不重复插入（避免连续两条）。
- **任何异常都不得影响压缩**（`log.debug` 后继续）。`ensure_vng_notice` 是下一轮兜底。

## 11. 状态更新与手动 `/compact`

### 11.1 `abefore_model` 的返回值

```python
return {
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *self._build_replacement(messages, summary, preserved),
        *preserved,
    ]
}
```

- `REMOVE_ALL_MESSAGES == "__remove_all__"`（`langgraph.graph.message`）。`add_messages` reducer 见到它**先清空**，再按顺序 append，因此摘要能精确落在"System 之后、保留消息之前"。用 `RemoveMessage` 逐条删是做不到插入位置的。
- `_partition_messages(messages, cutoff_index)` → `(messages_to_summarize, preserved)`，只要 `preserved`；`messages_to_summarize` 仅用于日志条数（等于 `cutoff_index`）。
- 保留消息**保持原 id**，reducer 视为 upsert，不会产生重复。
- 成功日志（透传 `reported`）：`"会话压缩完成: 上报 tokens=%d 阈值=%d 压缩 %d 条、保留 %d 条"`，参数为 `reported, self.threshold_tokens, cutoff_index, len(preserved)`。

异常与空结果降级：

```python
except Exception as exc:
    log.error("会话压缩失败（已重试），本次不改动 checkpoint，本轮继续: %s", exc)
    return None
if not summary:
    log.error("会话压缩结果为空，本次不改动 checkpoint")
    return None
```

### 11.2 `force_compact`（手动触发）

```python
async def force_compact(self, messages: list[AnyMessage], on_delta=None) -> dict[str, Any] | None
```

与自动路径的**唯一区别**是"无视阈值"：

1. `messages` 为空 ⇒ `return None`（调用方据此给"没有可压缩的上下文"卡片）。
2. `self._ensure_message_ids(messages)`。
3. `cutoff_index = self._determine_cutoff_index(messages)`；`<= 0` ⇒ `log.info("会话压缩跳过：没有可压缩的历史轮次")` + `return None`。
4. `_partition_messages` 取 `preserved`。
5. `await self._acompact(messages, on_delta=on_delta)`；异常 ⇒ `log.error("手动会话压缩失败: %s", exc)` 后 **`raise`**（与自动路径相反，调用方要渲染失败卡片）。
6. 空摘要 ⇒ `raise RuntimeError("对话压缩结果为空")`。
7. 成功日志：`"手动会话压缩完成: 压缩 %d 条、保留 %d 条"`（`cutoff_index, len(preserved)`）。
8. 返回与 §11.1 完全相同的 dict，由调用方 `agent.aupdate_state(config, update, as_node="model")` 写回（必须显式 `as_node`，否则图有 model/tools 两节点时 langgraph 报 Ambiguous update）。

## 12. 事件协议 `compact_start` / `compact_delta` / `compact_done`

压缩本身**不直接发事件**，只打 `COMPACT_TAG`；事件由宿主的事件生产者/命令处理器产生。Liorium 需要保留同一套字段（冻结，前端按此渲染卡片）：

| 事件 | 字段 | 产生时机 |
|---|---|---|
| `compact_start` | `agent_id`（`"main"` 或子代理 id）、`type` | 收到**第一个**带 `COMPACT_TAG` 的 `on_chat_model_stream` 分片时（每个压缩调用只发一次） |
| `compact_delta` | 上述 + `content`（纯文本增量） | 每个有文本的压缩分片 |
| `compact_done` | 上述 + 可选 `ok: bool`、可选 `error: str` | 压缩调用 `on_chat_model_end`；或主模型开始输出（`_close_compact()`）；或流结束/中断/异常兜底；或 `/compact` 命令收尾 |

不变量：

- 摘要文本**绝不**进入正文 `delta`。分流发生在生命周期层：`is_compact_call = COMPACT_TAG in (event.get("tags") or [])`。
- 成功时**不得出现 `ok=False`**：手动 `/compact` 路径完全不发 `ok` 字段（`_main_event_payload("compact_done")`），自动路径发 `ok: true`（`{"type": "compact_done", "ok": ok}`），子代理路径成功时也不发 `ok`。测试 `test_compact_success_carries_no_failure_flag` 只断言 `done.get("ok") is not False` 且 `"error" not in done`（对"缺省"和 `True` 都成立）；失败时一律 `ok=False` 且 `error` 非空。
- 子代理侧同构，且流结束时若卡片仍未收尾，补 `{"type": "compact_done", "ok": False}`。
- 手动 `/compact` 的事件序列是稳定的**逐字**序列（`test_compact_event_sequence_is_stable`）：

| 场景 | 事件序列 |
|---|---|
| 成功 | `compact_start` → `compact_delta` × N → `compact_done` → `done` |
| 无可压缩轮次（`force_compact` 返回 None） | `compact_start` → `compact_delta`（"当前会话没有可压缩的历史轮次。"）→ `compact_done` → `done` |
| `aupdate_state` 失败 | `compact_start` → `compact_delta`… → `compact_done` → `error` |
| 压缩抛错 | `compact_start` → `compact_done(ok=False, error=…)` → `error` |
| 空会话 | `compact_start` → `compact_delta`（含"没有可压缩的上下文"）→ `compact_done` → `done` |
| 中间件未就绪 | `compact_done(ok=False)` → `error`（无 `compact_start`；前端靠 `compact_done` 兜底建卡片） |

- 终结事件集合 `{"done", "interrupted", "error"}` 必须至少出现一个，否则前端 `sendToChat` 的 Promise 永不 settle（输入框永久"发送中"）。这是回归测试 3。
- 发送必须走 `_safe_send`（吞掉"连接已关"异常，只记日志），否则会杀死整个 WS 接收循环。

## 13. 错误与降级矩阵

| 场景 | 行为 | 是否改 checkpoint |
|---|---|---|
| 无 API 用量上报 | 返回 `None`；仅当存在 `AIMessage` 且未告警过时 `log.warning`（文案见下） | 否 |
| 未达阈值 | 返回 `None` | 否 |
| `cutoff_index <= 0` | 返回 `None` | 否 |
| 压缩调用抛错（含 429 重试耗尽） | `log.error`，返回 `None`，回合继续 | 否 |
| 摘要为空 | `log.error`，返回 `None` | 否 |
| 摘要被输出上限截断 | `_acompact` 抛 `RuntimeError`；自动路径 → 返回 `None`；手动路径 → 上抛 | 否 |
| `bind_tools` 不支持 `tool_choice` | 降级为无 `tool_choice`，只告警一次 | — |
| `bind_tools` 不支持服务端 specs | 省略 specs，告警（缓存前缀受损但能压） | — |
| `bind(max_tokens=...)` 不支持 | 跳过输出上限，`log.debug` | — |
| 服务端工具配置非法（构建期） | `log.error`，`server_tool_specs=[]`，压缩仍可用 | — |
| 压缩模型构建失败 / 无 model spec | `build_compact_middleware` 返回 `None`，auto compact 不启用（**不阻断 Agent 启动**） | — |
| VNG 通知保留失败 | `log.debug`，继续 | — |

`_warned_no_usage` 的告警文案（逐字）：

```
模型未上报 token 用量（api_format=%s）：自动压缩不会触发。若该网关需要 stream_options.include_usage，请检查 Provider 的 stream_usage 设置；否则请手动 /compact
```

> **429 相关**：压缩调用自身靠 `with_retry()`（3 次）；外层 `astream_events` 生产者另有 `max_attempts = 3` 的限流重试（`is_rate_limit_error`），二者互不依赖——压缩失败不会触发外层重试，也不会把 429 暴露给用户（回合继续，只是没压）。若上游随后真的报 `context_length_exceeded`，照常暴露给用户。

## 14. 辅助函数语义

### 14.1 `build_mock_tools` / `_mock_tool`

```python
def build_mock_tools(tools: Sequence[BaseTool] | None) -> list[BaseTool]
def _mock_tool(tool: BaseTool) -> BaseTool
```

用途：把压缩请求的 tools 前缀换成"schema 逐字节一致、实现为空"的 mock，从而既能命中前缀缓存，又不会真的执行工具。

- 有 `args_schema`：`StructuredTool.from_function(func=_noop, name=tool.name, description=tool.description, args_schema=args_schema)`
- 无 `args_schema`：`StructuredTool.from_function(func=_noop, name=tool.name, description=tool.description)`
- `_noop(**kwargs) -> str` 恒返回 `""`。

测试要求：`convert_to_openai_tool(mock) == convert_to_openai_tool(real)`（**逐字节相同**，name/description/参数 schema 都不能变——tools 定义位于请求最前端，差一个字节前缀缓存就整段失效）；`mock.invoke({"path": "/etc/passwd"}) == ""`（不执行真实实现）。

### 14.2 `_strip_summary(content) -> str`

剥掉 System 内容里上一次写入的摘要段，只留原始 prompt：

```
text = content if isinstance(content, str) else str(content or "")
start = text.find(COMPACT_MARKER_START)
若 start < 0                     → text.rstrip()
end = text.find(COMPACT_MARKER_END, start)
若 end < 0                       → text[:start].rstrip()      # 只有 start：截断即丢
否则                             → (text[:start] + text[end+len(END):]).rstrip()
```

性质：`_strip_summary(_wrap_summary(base, summary)) == base`（测试 `test_strip_and_wrap_summary_are_inverse`）。

### 14.3 `_wrap_summary(prompt_text, summary) -> str`

```
f"{prompt_text.rstrip()}\n\n"
f"{COMPACT_MARKER_START}\n"
f"以下是此前对话的压缩摘要（背景资料，不是新的用户请求）：\n\n"
f"{summary.strip()}\n"
f"{COMPACT_MARKER_END}"
```

性质：二次包装后旧摘要消失、`COMPACT_MARKER_START` 只出现一次（`test_strip_summary_replaces_previous_summary`）。

### 14.4 `_is_compact_summary(message) -> bool`

```python
kwargs = getattr(message, "additional_kwargs", None) or {}
return str(kwargs.get("lc_source") or "") == "liorium_compact"
```

只认 `lc_source == "liorium_compact"`。普通 HumanMessage / AIMessage 必须返回 `False`。

### 14.5 `_content_to_text(content) -> str`

```python
def _content_to_text(content: Any) -> str:
    from <旧后端>.agent.runtime import state as runtime_state   # 原样保留的冗余 import
    return message_content_to_text(content)
```

`message_content_to_text` 语义：`None` → `""`；`str` → 原样；`list` → 只拼接 `type` 归一化后等于 `"text"` 的块的 `text` 字段（`str(block["text"])`），其余块（image / reasoning / tool_use / thinking…）忽略；其他类型 → `str(content)`。

### 14.6 `build_compact_middleware`（工厂，async）

```python
async def build_compact_middleware(
    *, model: Any, tools: Sequence[BaseTool] | None, model_spec: str | None
) -> CompactMiddleware | None
```

流程：

1. `model_spec` 为空 ⇒ `log.warning("未配置模型 spec，auto compact 未启用")` + `return None`。
2. `providers = runtime_state.get_model_providers()`。
3. `context_length = get_context_length(providers, model_spec)`（模型未配置/无法解析 ⇒ 回退 `DEFAULT_CONTEXT_LENGTH = 128000`）。
4. `compact_model = await build_chat_model_from_spec(providers, spec=model_spec, intensity=None, purpose="compact")`；异常 ⇒ `log.error("构建压缩模型失败，auto compact 未启用: %s")` + `return None`。
   - `intensity=None`：**关闭 thinking**（reasoning 参数不属于被缓存前缀，关掉不破缓存，且不浪费摘要输出余量）。
   - `purpose="compact"`：让 builder 给需要显式输出上限的格式（Anthropic）设置合适上限，而不是 SDK 默认 1024（会把摘要截断成半句话）。
5. `api_format = get_model_api_format(providers, model_spec)`（无法解析 ⇒ `"openai_chat"`）。
6. `server_tool_specs = resolve_server_tool_specs(providers, model_spec)`；`ValueError` ⇒ `log.error("压缩请求无法解析服务端工具规格（本次省略）: %s")` + `[]`。
7. `summary_max_tokens = int(getattr(compact_model, "max_tokens", 0) or 0) if api_format == "anthropic_messages" else 0`。
8. 构造 `CompactMiddleware(..., threshold_ratio=conf.COMPACT_THRESHOLD_RATIO, keep_rounds=conf.COMPACT_KEEP_ROUNDS, api_format=api_format, server_tool_specs=server_tool_specs, summary_max_tokens=summary_max_tokens)` 并返回。

装配位置（语义约束）：中间件链顺序为 `插件 → mm_bridge → tool_repair → server_tools → compact → model_retry(最内层)`。compact 必须在 `mm_bridge`/`tool_repair` **之后（更外层）追加**，保证判定基于"未桥接、未修复"的原始消息；`model_retry` 始终最内层，只重放模型请求本身。Subagent 侧：compaction 中间件按**该 Subagent 自己的 model_spec 与 toolset** 重建（mock 工具必须匹配它自己的 tool schema），并在栈中替换掉旧实例。

## 15. 外部契约速查（Liorium 需自带等价实现）

| 契约 | 签名 | 语义 |
|---|---|---|
| `SummarizationMiddleware._should_summarize` | `(self, messages: list[AnyMessage], total_tokens: int) -> bool` | 见 §6.3；本模块只依赖 `("tokens", N)` 一条 clause |
| `SummarizationMiddleware._find_safe_cutoff_point` | `@staticmethod (messages: list[AnyMessage], cutoff_index: int) -> int` | 见 §7.2，切点落 ToolMessage 时**向前**回退到对应 AIMessage |
| `SummarizationMiddleware._ensure_message_ids` | `@staticmethod (messages: list[AnyMessage]) -> None` | 为 `msg.id is None` 的消息**就地**赋值 `str(uuid.uuid4())`；无返回值 |
| `SummarizationMiddleware._partition_messages` | `@staticmethod (conversation_messages, cutoff_index) -> tuple[list, list]` | `(messages[:cutoff], messages[cutoff:])` |
| `repair_dangling_tool_calls` | `(messages: list[AnyMessage]) -> list[AnyMessage]` | §16 陷阱 4；返回新列表，不改入参 |
| `forced_no_tool_choice` | `(api_format: str) -> str \| dict[str, str]` | 见 §8.2 |
| `vng.latest_vng_notice_message` | `(messages: Sequence[Any]) -> Any \| None` | 反向找 `additional_kwargs["lc_source"] == "liorium_vng_mode"` 的消息 |
| `vng.is_vng_notice` | `(message: Any) -> bool` | 同上判定 |
| `usage.context_tokens` / `normalize_usage` / `last_usage` | 见 §6.1 | 只读 API 上报值，绝不本地估算 |

## 16. 测试清单（必须全部移植并跑绿）

### 16.1 `test_compact_middleware.py`（离线，19 个）

| # | 测试 | 断言 |
|---|---|---|
| 1 | `test_reported_tokens_uses_api_value` | 多轮历史（500 / 9000）取**最新**上报值 `9000` |
| 2 | `test_reported_tokens_zero_without_api_data` | 无 AI 用量 ⇒ `0` |
| 3 | `test_mock_tool_schema_is_byte_identical` | `convert_to_openai_tool(mock) == convert_to_openai_tool(real)`；`name` / `description` 相同 |
| 4 | `test_mock_tool_does_not_execute_real_impl` | `mock.invoke({"path": "/etc/passwd"}) == ""` |
| 5 | `test_cutoff_keeps_last_n_rounds` | `keep_rounds=1`，3 轮 ⇒ cutoff `5` |
| 6 | `test_cutoff_never_splits_tool_call_group` | 保留区与丢弃区**各自**不存在"有 ToolMessage 无 tool_calls"的悬空（双向子集断言） |
| 7 | `test_cutoff_advances_to_include_ai_when_boundary_is_tool_message` | 切点落在 tool 组上时，保留区 ToolMessage ⊆ 有 tool_calls 的 AIMessage |
| 8 | `test_cutoff_zero_when_nothing_to_compress` | `[S, H(only)]` ⇒ `0` |
| 9 | `test_strip_and_wrap_summary_are_inverse` | 包装含起止标记；`_strip_summary(_wrap_summary(base, s)) == base` |
| 10 | `test_strip_summary_replaces_previous_summary` | 二次包装：旧摘要消失、新摘要存在、`START` 计数为 1 |
| 11 | `test_build_replacement_merges_into_system` | 长度 1、是 SystemMessage、`id == "s1"`、原文与摘要都在 |
| 12 | `test_build_replacement_falls_back_to_human_without_system` | 无 System ⇒ 长度 1、HumanMessage、含摘要 |
| 13 | `test_abefore_model_skips_without_api_tokens` | 无用量 ⇒ `None` |
| 14 | `test_abefore_model_skips_below_threshold` | 100 < 8000 ⇒ `None` |
| 15 | `test_abefore_model_compacts_and_orders_messages` | `updates[0]` 是 `RemoveMessage(id=REMOVE_ALL_MESSAGES)`；`updates[1]` 是 SystemMessage 且 `id == "s1"`、含摘要；`updates[2:]` 类型序列 `["HumanMessage","AIMessage"]` 且 `updates[2].content == "r2"` |
| 16 | `test_compact_call_reuses_full_prefix_with_mock_tools` | `sent[:len(messages)]` 的 id 序列与原始一致；`len(sent) == len(messages)+1`；`sent[-1]` 是 HumanMessage 且含 `"调用任何工具"`；绑定工具的 schema 与真实工具一致 |
| 17 | `test_abefore_model_degrades_on_failure` | 压缩调用抛错 ⇒ 返回 `None` |
| 18 | `test_compact_call_repairs_dangling_tool_calls_mid_history` | 历史中段悬空 ⇒ payload 无 tool_call 违规、且**不注入** ToolMessage（清空 tool_calls 而非补占位） |
| 19 | `test_compact_call_placeholder_for_trailing_dangling_tool_calls` | 序列结尾悬空 ⇒ 注入占位（`tool_call_id == "c9"`）；`sent[-1]` 是 HumanMessage、`sent[-2]` 是 ToolMessage |

测试前提工具 `_tool_call_violations(messages)`：按 OpenAI 约束校验——每个 assistant `tool_call` 必须在遇到下一条非 tool 消息之前被对应 `ToolMessage` 响应；序列结尾不得有未响应调用。

### 16.2 `test_compact_formats.py`（离线，18 个测试函数，其中 1 个参数化 7 组）

| 测试 | 断言 |
|---|---|
| `test_anthropic_context_includes_cache_read_and_creation` | `input_tokens == 1050`、`cache_read_tokens == 900`、`cache_creation_tokens == 50`、`context_tokens == 1070` |
| `test_anthropic_does_not_double_count_when_sdk_already_folded_cache` | SDK 已折叠时 `context_tokens == 1070`（`max()` 语义） |
| `test_openai_cached_tokens_are_subset_not_additive` | `context_tokens == 1100`（不是 1900）、`cache_read_tokens == 800` |
| `test_gemini_cached_tokens_are_subset_not_additive` | `context_tokens == 5200`、`reasoning_tokens == 120` |
| `test_usage_without_any_report_returns_none` | `normalize_usage` 与 `last_usage` 均 `None` |
| `test_usage_total_falls_back_to_input_plus_output` | `total_tokens == 0` ⇒ `context_tokens == 15` |
| `test_reported_tokens_uses_latest_reporting_message` | 跳过无 usage 的本地 AIMessage，取 `110` |
| `test_reported_tokens_anthropic_reads_raw_usage` | `10 + 990 + 100 == 1100` |
| `test_is_truncated`（7 组参数） | `(100,"length",None)→True`；`(100,"max_tokens",None)→True`；`(100,"stop",None)→False`；`(100,None,{"output_tokens":100})→True`；`(100,None,{"output_tokens":99})→False`；`(0,"length",None)→False`；`(100,"end_turn",{"output_tokens":5})→False` |
| `test_build_replacement_merges_all_system_messages` | 两条 System 合并为 1 条，两段文本与摘要都在，`"liorium:compact-summary:start"` 计数为 1 |
| `test_build_replacement_without_system_uses_human_message` | `additional_kwargs["lc_source"] == "liorium_compact"` |
| `test_compact_binds_server_tools_after_mocks_with_anthropic_none` | `tool_choice == {"type": "none"}`；`tools[0].name == "read_file"`、`tools[-1]["name"] == "web_search"`（mock 在前、服务端 specs 在后） |
| `test_compact_uses_string_none_for_openai` | `tool_choice == "none"` |
| `test_compact_falls_back_when_tool_choice_unsupported` | `_tool_choice_warned is True`；最后一次绑定的 `tool_choice is None` |
| `test_compact_raises_on_truncated_summary` | `stop_reason="max_tokens"` + `summary_max_tokens=4096` ⇒ `RuntimeError` 含 `"4096"`；`_cost_cap_bypassed is False`（上限来自模型自身，不触发自愈） |
| `test_compact_cost_cap_truncation_bypasses_cap` | 成本上限 500 + `stop_reason="length"` ⇒ 抛错；`_cost_cap_bypassed is True`；`bind_log[-1]["max_tokens"] == 500` |
| `test_abefore_model_writes_merged_state` | `updated[0]` 是 RemoveMessage；`updated[1]` 是 SystemMessage 且含 `sys-a`、`sys-b`、摘要 |
| `test_abefore_model_skips_without_usage` | 无 usage ⇒ `None` |

### 16.3 `test_compact_regressions.py`

**用量提取**

| 测试 | 断言 |
|---|---|
| `test_reported_tokens_skips_ai_without_usage` | 末尾 AIMessage 无 usage ⇒ 继续向前取 `900_001`（不是 0） |
| `test_reported_tokens_zero_when_no_usage_anywhere` | 全无 ⇒ `0` |
| `test_reported_tokens_ignores_non_ai_messages` | 只看 AI ⇒ `501` |
| `test_reported_tokens_zero_on_empty` | `[]` ⇒ `0` |

**切点 / 摘要标记**

| 测试 | 断言 |
|---|---|
| `test_is_compact_summary_marker` | 摘要 HumanMessage → `True`；普通 Human / AI → `False` |
| `test_cutoff_skips_when_only_old_summary_is_compressible` | `keep_rounds=3`，`[summary, r1, a1]` ⇒ `0`（否则只是重摘要） |
| `test_cutoff_compacts_real_rounds_not_the_summary` | `keep_rounds=3`，`[summary, r1..r3]` ⇒ `cutoff != 1`（不得把摘要当第 1 轮）、`cutoff == messages.index(messages[5])`（即 r3 的下标 5）、且 `not _is_compact_summary(messages[cutoff])` |
| `test_cutoff_requires_a_real_discarded_round` | `[S, summary, r1, a1]`、`keep_rounds=1` ⇒ `round_starts=[2]` 退化取 2，丢弃段只有 sys+摘要 ⇒ `0` |

**`/compact` 事件（宿主侧，模块需配合）**

| 测试 | 断言 |
|---|---|
| `test_compact_event_sequence_is_stable`（4 组） | 逐字事件序列：成功 `[compact_start, compact_delta, compact_delta, compact_done, done]`；`force_compact` 返回 None `[compact_start, compact_delta, compact_done, done]`；`aupdate_state` 失败 `[..., compact_done, error]`；压缩抛错 `[compact_start, compact_done, error]`；所有 payload 的 `agent_id == "main"`；成功路径增量拼成 `"## 目标与进度\n摘要正文"` |
| `test_compact_failure_marks_card_failed` | 失败路径最后一条 `compact_done` 的 `ok is False` 且 `error` 非空 |
| `test_compact_success_carries_no_failure_flag` | 成功 `done.get("ok") is not False` 且无 `error` 键 |
| `test_compact_without_messages_still_terminates` | 空会话事件序列 `[compact_start, compact_delta, compact_done, done]`，delta 含 `"没有可压缩的上下文"` |
| `test_compact_missing_middleware_fails_visibly` | 中间件为 None ⇒ `[compact_done, error]`，首条 `ok is False` |
| `test_safe_send_swallows_closed_socket` | `_safe_send` 在 socket 关闭时返回 `False`，不抛 |
| `test_compact_survives_dead_socket` | 全程发送失败仍返回 `handled is True`、`reply is None`，且确实尝试过推送 |

### 16.4 `test_compact_cost_control.py`

| 测试 | 断言 |
|---|---|
| `test_cost_control_off_by_default_leaves_payload_intact` | 开关关闭时 `bind_kwargs == []`（不设输出上限）；payload 条数 = `len(messages)+1`（不裁剪） |
| `test_cost_control_on_applies_both_caps` | `bind_kwargs == [{"max_tokens": 123}]`；`cap=5` token → `12` 字符 ⇒ payload 只剩 1 条消息 + 指令 = 2 条 |
| `test_truncated_summary_raises_and_cap_self_heals` | 第一次抛 `RuntimeError`（match `"截断"`）；第二次不再 `bind`（`bind_kwargs` 仍只有一条 `{"max_tokens": 50}`）；`_cost_cap_bypassed is True` |
| `test_truncation_detected_by_output_tokens_without_finish_reason` | 无 `finish_reason` 但 `output_tokens == cap` ⇒ 抛错（match `"截断"`） |
| `test_usage_is_logged_for_cost_audit` | logger `"liorium.compact"` 的 INFO 记录含 `"压缩调用用量"`（开关开/关都记录） |
| `test_cost_control_keys_are_exposed_to_frontend` | 公共配置默认值：`COMPACT_COST_CONTROL_ENABLED is False`、`COMPACT_MAX_INPUT_TOKENS == 0`、`COMPACT_MAX_OUTPUT_TOKENS == 4000`、`COMPACT_THRESHOLD_RATIO == 0.8`、`COMPACT_KEEP_ROUNDS == 3`；且 `config` 生效值类型与默认值一致 |

## 17. 陷阱与不变量（重写时的检查表）

- **I1 触发判据唯一来源是 API 上报值。** 没有上报值 ⇒ `0` ⇒ 不压缩 + 告警一次；禁止任何本地 token 推算参与判定。
- **I2 从后向前找"最近一条带用量的 AIMessage"**，不是"最后一条 AIMessage"。
- **I3 Anthropic 缓存读写计入 input；OpenAI/Gemini 的 cached 是子集不相加。** 反了会让压缩永不触发或过早触发。
- **I4 `keep_rounds` 是上限不是下限。** 轮数不足时退化为"只保留最后一轮"，仍要压。
- **I5 摘要消息（`lc_source == "liorium_compact"`）不算一轮**，且丢弃段必须含至少一条真实轮次。这两条是本模块唯一的"防每回合重复压缩"机制（压缩后上报值仍然虚高）。
- **I6 切点绝不拆散 `AIMessage(tool_calls)` 与其 `ToolMessage`。** 边界落在 ToolMessage 上时切点**向前**回退。
- **I7 不切进 System：** `cutoff <= system_count` 直接放弃本轮压缩。
- **I8 压缩 payload = 完整 messages + 尾端指令。** 不能只发"将被丢弃的段"；否则前缀缓存整段失效（最贵的错误）。
- **I9 payload 先 `repair_dangling_tool_calls`，再追加指令。** 顺序反了会把"结尾悬空"误判为"被打断"，从而作废而不是补占位。
- **I10 tools 前缀 = mock（同 schema）+ 服务端 specs，顺序与主请求一致；`tool_choice` 写法随格式。** Anthropic 必须 `{"type": "none"}`。
- **I11 默认不设输出上限、不裁剪输入。** 两个上限只在显式开关下生效；截断即失败（不写回）+ 进程内自愈。
- **I12 写回结构固定为 `[RemoveMessage(REMOVE_ALL_MESSAGES), 头部..., *preserved]`。** 头部 = 唯一合并 System（保留原 id）或摘要 HumanMessage；多条 System 必须合并。
- **I13 摘要段用 `<!-- liorium:compact-summary:start/end -->` 包裹并先剥旧再写新。** System 不随压缩次数增长；`_strip_summary`/`_wrap_summary` 互为逆。
- **I14 失败不写 checkpoint、不打断回合；手动路径才上抛。** 空摘要同样视为失败。
- **I15 事件分流靠 `COMPACT_TAG` tag；成功 `compact_done` 绝不带 `ok=False`（自动路径为 `ok: true`，手动/子代理路径缺省），失败带 `ok=False` + `error`。** 摘要文本不得进入正文 `delta`。
- **I16 `_ensure_message_ids` 会**就地**给消息补 uuid**（唯一的有意 in-place 副作用）。除此之外不修改入参：`_acompact` 先 `list()` 拷贝、`repair` 返回新列表、`_build_replacement` 用 `model_copy(update=...)`。
- **I17 VNG 通知必须跨压缩保留，且失败不影响压缩。**
- **I18 不本地统计图片 token**；成本上限的字符估算是唯一的"非 token"近似，且仅用于裁剪。
- **I19 依赖 langchain 私有方法 ⇒ 必须锁次要版本**（计划 A/F 节已记录：`_find_safe_cutoff_point` / `_ensure_message_ids` / `_should_summarize` 的签名是小版本级的稳定面）。Liorium 应把这些语义**内化**为自有实现，不再跨库调用私有 API。

## 18. Liorium 移植建议（最小形状）

| 原型 | Liorium 建议 |
|---|---|
| `CompactMiddleware(SummarizationMiddleware)` | `CompactMiddleware(Middleware)`，自带 `before_model` / `abefore_model`，把 §7.2 / §7.1 / §11 的三个私有方法搬到本类（`_find_safe_cutoff_point` 作为纯函数 `find_safe_cutoff_point(messages, cutoff)` 导出，便于单测）。 |
| `_should_summarize` | 一行 `reported >= threshold`；不要复制基类的 reported 兜底。 |
| `_ensure_message_ids` | 保留（LangGraph reducer 需要），但明确"就地补 id"是唯一允许的副作用。 |
| `_acompact(on_delta=...)` | 保持 `on_delta` 可选回调；自动路径改用宿主事件流（`COMPACT_TAG` 等价物 → `compact_*` 事件），手动路径用回调。 |
| `build_mock_tools` | 独立小工具函数，随压缩模块导出（schema 逐字节一致是硬要求）。 |
| `vng` 依赖 | 抽成"可跨压缩保留的消息谓词"钩子（宿主注入 `preserve_predicate(messages) -> list[Message]`），不要让核心 import 产品模块。 |
| `conf.COMPACT_*` | 改成构造参数 + 显式 `CompactPolicy` dataclass（默认值照抄 §9.1 表），宿主负责从配置读取。 |
| 事件发射 | 只在核心产生"压缩开始/增量/结束"通知；`done`/`error` 终结事件归宿主的回合协议（I15）。 |

验收标准：把 §16 的 4 个测试文件按行为 1:1 移植到 `liorium-agent` 的测试里，全部跑绿；再补一个"压缩两次后 System 只含一段摘要、且第二次不再触发无意义压缩"的用例（覆盖 I5 + I13）。
