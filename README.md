# Project Liorium

- 模块：`liorium.core` / `liorium.vfs` / `liorium.realm_base` / `liorium.core_tool` /
  `liorium.core_tool_realm` / `liorium.agent` / `liorium.memory` / `liorium.skills` /
  `liorium.mcp` / `liorium.todo_realm` / `liorium.provider` / `liorium.cli`
- 安装：`pip install "liorium[all]"`
  只要框架运行时用 `pip install liorium`；单独选装见下
- 仓库：<https://github.com/liwusen/liorium-framework>

## 文档

文档入口：[`docs/README.md`](docs/README.md)

| 文档                                             | 内容                                                                     |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| [`docs/GUIDE.md`](docs/GUIDE.md)               | 安装、最小 Agent、事件表、中断/注入、压缩与成本、多 Agent 并发                                 |
| [`docs/API.md`](docs/API.md)                   | API 介绍                                                                 |
| [`docs/EXTENDING.md`](docs/EXTENDING.md)       | 自定义 Realm / Middleware / 调度参数（`priority` / `FLAG`）/ model_factory / 工具 |
| [`docs/MEMORY.md`](docs/MEMORY.md)             | 记忆自系统                                                                  |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构介绍                                                                   |

### 例子: 最小 Agent

```python
import asyncio
from pathlib import Path

from liorium.agent import AgentSpec, Limits, LioriumAgent


def make_chat_model(label: str):
    from langchain_openai import ChatOpenAI
    provider, model = label.split("::", 1)
    return ChatOpenAI(model=model, base_url="https://api.deepseek.com", api_key="...")


async def main() -> None:
    agent = LioriumAgent(
        spec=AgentSpec(
            id="main",
            inbox_dir=Path("agents/main"),
            model="deepseek::chat",
            prompt_sources=("AGENT.md", "ROLE.md", "COREMEMORY.md"),
            limits=Limits(context_window=128_000, max_turns=200),
        ),
        model_factory=make_chat_model,
    )
    await agent.start()                       # 不调模型（计划 §F7）
    try:
        async for event in agent.run("今天出门要带伞吗？"):
            print(event.to_dict())            # start / delta / reasoning_delta / tool_* / done
    finally:
        await agent.close()


asyncio.run(main())
```

- 叫停 = `await agent.interrupt(reason="user_stop")` + 一个新的 `run()`（两个回合）。
- 运行中补充信息 = `await agent.inject([ContextItem(source="trigger:desktop", text="开始下雨了")])`，
  **永不打断**，在下一个安全点物化成独立 user 消息。
- 热重载 = `await agent.apply(new_spec)`：只重建指纹变化的部件；失败保留旧部件。
- 清空对话 = `await agent.reset_dialog()`（纯状态写入，不调模型；同时清空
  `state_scope="conversation"` 的 realm 状态）。
