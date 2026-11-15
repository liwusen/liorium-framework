# Project Liorium

- 模块：`liorium.core` / `liorium.vfs` / `liorium.realm_base` / `liorium.core_tool` /
  `liorium.core_tool_realm` / `liorium.agent` / `liorium.memory` / `liorium.skills` /
  `liorium.mcp` / `liorium.todo_realm` / `liorium.provider` / `liorium.profile` / `liorium.cli`
- 安装：`pip install "liorium[all]"`
  只要框架运行时用 `pip install liorium`；单独选装见下
- 仓库：<https://github.com/liwusen/liorium-framework>

## 文档

文档入口：[`docs/README.md`](docs/README.md)

| 文档                                             | 内容                                                                     |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| [`docs/GUIDE.md`](docs/GUIDE.md)               | 安装、最小 Agent、事件表、中断/注入、压缩与成本、多 Agent 并发                                 |
| [`docs/API.md`](docs/API.md)                   | API 介绍                                                                 |
| [`docs/EXTENDING.md`](docs/EXTENDING.md)       | 自定义 Realm / 主动推送与只读钩子 / 宿主向 realm 发消息 / Middleware / 入站调度（`Bucket` / `Flag`）/ model_factory / 工具 |
| [`docs/design/event-stream.md`](docs/design/event-stream.md) | 事件流原生：`push` / `stream` / bucket / 终态帧 / `realm` 帧（权威文档）              |
| [`docs/MEMORY.md`](docs/MEMORY.md)             | 记忆自系统                                                                  |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构介绍                                                                   |

### 命令行：安装后先配置

```powershell
pip install "liorium[all]"
liorium-cli setup                 # 交互式：provider / 记忆系统（也支持 setup providers add …）
liorium-cli setup show            # 配置一览（密钥打码）
liorium-cli setup profile show    # Agent 装配：模型 / realm / 工具集（profile.json）
liorium-cli setup doctor          # 体检：文件 / key / 目录 / 依赖
liorium-cli                       # 进入 REPL；/setup 也能配
```

配置在 `~/.liorium/`：`providers.json`（provider + 密钥，0600）、`profile.json`
（Agent 装配：模型 / realm / 工具集 / 提示词）与 `config.json`（只剩 UI 偏好）。
三个文件都可以不存在——新机器第一次运行会生成默认 profile 与空骨架，进入「未配置」模式，
不会因为缺配置起不来；旧 `config.json` 里的装配字段会自动迁移一次（备份 `.bak`）。详见
[`packages/liorium-cli/README.md`](packages/liorium-cli/README.md)。

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
        stream = agent.stream(ids=["hello"])  # ① 先订阅（__aiter__ 才注册，不回放）
        iterator = stream.__aiter__()
        agent.push("今天出门要带伞吗？", id="hello")   # ② 同步投递，入队即返回
        async for event in iterator:          # ③ 消费到终态帧
            if event.type == "delta":
                print(event.get("content"), end="")
            elif event.type in ("done", "interrupted", "error"):
                break
        await stream.aclose()
    finally:
        await agent.close()


asyncio.run(main())
```

- 投递 = `agent.push(...)`（`id` 必填，是流的关联键）；观察 = `agent.stream()`（可开多个、
  不回放）；每个回合以**恰好一条**终态帧 `done` / `interrupted` / `error` 收尾。详见
  [`docs/design/event-stream.md`](docs/design/event-stream.md)。
- 叫停 = `await agent.interrupt(reason="user_stop")` + 再 `push()` 一条（两个回合）。
- 插话 = `agent.push(..., bucket=Bucket.IMMEDIATE)`：在同一回合的安全点接管，补一条非终态
  `round_aborted`，`turn_id` 不变。
- 运行中补充信息 = `agent.inject([ContextItem(source="trigger:desktop", text="开始下雨了")])`
  （同步），**永不打断**，在下一个安全点物化成独立 user 消息。
- 热重载 = `await agent.apply(new_spec)`：只重建指纹变化的部件；失败保留旧部件。
- 清空对话 = `await agent.reset_dialog()`（纯状态写入，不调模型；同时清空
  `state_scope="conversation"` 的 realm 状态）。
