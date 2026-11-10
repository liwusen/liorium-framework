# liorium-mcp

Liorium MCP 能力包（`[mcp]` extra）：把 MCP server 的工具接成 Agent 工具。

## 组成

| 模块 | 职责 |
|---|---|
| `client.py` | `McpServerConfig` / `McpToolSpec` / `McpClient` 协议 / `SdkMcpClient`（stdio、sse、streamable-http；`mcp` SDK 延迟导入） |
| `schema.py` | JSON Schema → pydantic 参数模型 + `Args:` 描述 |
| `realm.py` | `McpRealm`：运行时发现工具、单 server 失败降级、`set_servers`/`mark_dirty` 重连 |
| `tools.py` | 工具名 `<server>_<tool>`（`prefix_tools=False` 可关） |

**MCP 工具不进 `AgentSpec`**（计划 §B2）：spec 只写要连哪些 server；工具在运行时发现。

## 用法

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.mcp import McpRealm

agent = LioriumAgent(
    spec=AgentSpec(
        id="main",
        inbox_dir=Path("agents/main"),
        model="deepseek::chat",
        prompt_sources=("AGENT.md",),
        realms=[
            RealmSpec(
                id="mcp",
                params={
                    "servers": {
                        "playwright": {
                            "command": "npx",
                            "args": ["@playwright/mcp"],
                            "description": "浏览器自动化",
                        }
                    },
                    "strict": False,      # True：全部 server 连不上就让 realm 失败
                },
            )
        ],
    ),
    model_factory=make_chat_model,
    realm_factories={"mcp": McpRealm},
)
```

## 宿主 mark dirty（计划 §B6 / §Phase 2）

```python
realm = agent.realms.record("mcp").instance

# 改了配置（增删 server / 换 url）
realm.set_servers({"web": {"transport": "sse", "url": "https://mcp.example.com/sse"}})

# 或同样的事情用 mark_dirty 表达（只重连，配置对象自己替换）
realm.mark_dirty()

async for event in agent.run("查一下这个网页"):
    ...  # 回合边界自动重连 → 换工具集 → 工具 schema 变 ⇒ CacheEpoch +1
```

- 单个 server 连不上：记 `status()` 里的 `error`，其它 server 照常可用；
- 所有 server 都连不上且 `strict=True`：realm 启动失败（`RealmManager` 标 `failed`）；
- 失败不会自动无限重试：宿主再次 `mark_dirty()` 才会重连。

## 自定义传输 / 测试

`McpRealm(spec, client_factory=...)` 接受任意实现 `McpClient` 协议的工厂
（`connect` / `list_tools` / `call_tool` / `close` + `server_id`），所以测试不需要
真的起子进程，宿主也可以塞审计/沙箱包装。

```python
agent = LioriumAgent(
    ...,
    realm_factories={"mcp": lambda spec: McpRealm(spec, client_factory=my_factory)},
)
```
