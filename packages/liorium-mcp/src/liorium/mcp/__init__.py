"""liorium.mcp —— MCP 能力包（``[mcp]`` extra）。

计划依据 §B2 / §B6 / §Phase 2：

- 工具来自**运行时发现**的 MCP server（不进 ``AgentSpec``）
- 宿主 mark dirty 后，Agent 在回合边界重连并换工具集（工具 schema 变 ⇒ ``CacheEpoch +1``）
- 断线重连不靠"记得重建"，靠 reconcile

用法：

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.mcp import McpRealm

agent = LioriumAgent(
    spec=AgentSpec(
        ...,
        realms=[
            RealmSpec(
                id="mcp",
                params={"servers": {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}},
            )
        ],
    ),
    realm_factories={"mcp": McpRealm},
    ...
)
```

宿主改了配置：

```python
realm = agent.realms.record("mcp").instance
realm.set_servers([...])          # 或 realm.mark_dirty()
# 下一次 agent.run(...) 的回合边界会自动重连并替换工具
```
"""

from __future__ import annotations

__version__ = "0.1.0"

from .client import McpClient, McpError, McpServerConfig, McpToolSpec, SdkMcpClient
from .realm import McpRealm
from .schema import build_args_schema, build_tool_description

__all__ = [
    "McpClient",
    "McpError",
    "McpRealm",
    "McpServerConfig",
    "McpToolSpec",
    "SdkMcpClient",
    "build_args_schema",
    "build_tool_description",
    "__version__",
]
