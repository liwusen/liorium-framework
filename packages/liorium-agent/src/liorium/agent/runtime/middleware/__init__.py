"""基座中间件链 —— **顺序由库固定，realm 只能往最外层追加**（计划 §4.1/§C8）。

固定顺序（外 → 内）::

    realm 中间件（按声明序）
      → context_merge    运行中到达的 Context 在安全点物化成独立 user 消息
      → mm_bridge        把 artifact 引用还原成图片块
      → tool_repair      永不允许悬空 tool_call 到达模型
      → server_tools     provider 托管工具规格（可选）
      → compact          保命压缩（payload 与主请求逐字节一致）
      → model_retry      最内层，只重放模型请求

``build_chain`` 是唯一装配点；任何重排都会破坏正确性前提（重试必须最内层，
repair 必须永远打开）。

``context_merge`` 紧跟 realm 之后（基座最外层）：它只**追加**消息，越外层越早
把新消息放进状态，内层（mm_bridge / tool_repair / compact）就能看到"最新一条
user 消息"，而它自己不需要看见任何重写。
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents.middleware import ModelRetryMiddleware

from ...context import ContextChannel
from .compact import CompactMiddleware
from .context_merge import ContextMergeMiddleware
from .mm_bridge import MultimodalBridgeMiddleware
from .server_tools import ServerToolsMiddleware
from .tool_repair import ToolCallRepairMiddleware

#: 固定顺序的基座中间件类（不含 realm 层）
BASE_ORDER = (
    "context_merge",
    "mm_bridge",
    "tool_repair",
    "server_tools",
    "compact",
    "model_retry",
)


def build_chain(
    *,
    artifacts: Any,
    compact: CompactMiddleware | None = None,
    server_tool_specs: Sequence[Any] = (),
    max_retries: int = 2,
    realm_middlewares: Sequence[Any] = (),
    context_channel: ContextChannel | None = None,
) -> list[Any]:
    """装配完整中间件链：realm 外层 + 基座固定顺序。

    - ``compact`` 为 None 时（例如假模型测试）跳过压缩，但顺序其它项不变；
    - ``context_channel`` 为 None 时**不装** ``context_merge``（没有通道就没有
      待物化项，装了也是空转；宿主不用 ``inject()`` 时保持链条最短）。
    """
    chain: list[Any] = []
    chain.extend(realm_middlewares)
    if context_channel is not None:
        chain.append(ContextMergeMiddleware(context_channel))
    chain.append(MultimodalBridgeMiddleware(artifacts=artifacts))
    chain.append(ToolCallRepairMiddleware())
    if server_tool_specs:
        chain.append(ServerToolsMiddleware(specs=tuple(server_tool_specs)))
    if compact is not None:
        chain.append(compact)
    chain.append(
        ModelRetryMiddleware(
            max_retries=max_retries,
            retry_on=(Exception,),
            on_failure="error",
        )
    )
    return chain


__all__ = ["BASE_ORDER", "ContextMergeMiddleware", "build_chain"]
