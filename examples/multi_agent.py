"""多 Agent 示例：两个 ``LioriumAgent`` + 一个调用方 semaphore。

计划依据 §Phase 2「多 Agent 宿主示例（2 个 ``LioriumAgent`` + semaphore）」：

- **Agent 本身没有"并发上限"这个概念**：`run()` 返回的就是这一条消息的事件流，
  "同一时刻最多 N 个 Agent 在跑"由调用方用 semaphore 包住 `async for` 即可；
- 两个 Agent 各自身份独立（自己的 inbox / checkpoint / prompt），共享同一份
  provider 配置与一个 semaphore；
- 只用一个进程、一个事件循环：``asyncio.gather`` 并发跑，谁先拿到 semaphore 谁先说。

用法：

    python examples/multi_agent.py -p "用一句话介绍 Liorium"
    python examples/multi_agent.py -p "..." --model Go::deepseek-v4.1-flash --concurrency 1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

from liorium.agent import AgentSpec, Limits, RealmSpec, LioriumAgent
from liorium.cli.providers import (
    ProviderConfigError,
    ProviderRegistry,
    make_chat_model,
    registry_from_args_or_prompt,
)
from liorium.core.types import Event
from liorium.core_tool_realm import CoreToolRealm

ROLES: dict[str, str] = {
    "planner": (
        "# 规划者\n\n"
        "你把用户的任务拆成最多 3 步的执行计划，每步一行，不要写正文。\n"
        "需要查资料时用 read / find 工具，不要凭印象编造路径。\n"
    ),
    "writer": (
        "# 写作者\n\n"
        "你把用户的任务写成两三句完整、可交付的正文，不要输出计划或步骤。\n"
        "需要查资料时用 read / find 工具，不要凭印象编造路径。\n"
    ),
}

EmitFn = Callable[[str, Event], None]


def build_agents(
    *,
    workspace: Path,
    model_factory: Callable[[str], Any],
    model_label: str,
    context_window: int = 128_000,
) -> list[LioriumAgent]:
    """按 ``ROLES`` 建两个独立 Agent（各自的 inbox / checkpoint / prompt）。"""
    agents: list[LioriumAgent] = []
    for name, role in ROLES.items():
        inbox = workspace / "agents" / name
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "AGENT.md").write_text(role, encoding="utf-8")
        spec = AgentSpec(
            id=name,
            inbox_dir=inbox,
            model=model_label,
            prompt_sources=("AGENT.md",),
            # 工具全部来自 realm：这两个角色要 read / find 查资料，所以装 core。
            realms=(RealmSpec(id="core"),),
            limits=Limits(context_window=context_window, max_turns=6),
            workspace_root=workspace,
        )
        agents.append(
            LioriumAgent(
                spec=spec,
                model_factory=model_factory,
                realm_factories={"core": CoreToolRealm},
            )
        )
    return agents


async def run_agents(
    *,
    prompt: str,
    workspace: Path,
    model_factory: Callable[[str], Any],
    model_label: str,
    concurrency: int = 1,
    context_window: int = 128_000,
    emit: EmitFn | None = None,
) -> dict[str, str]:
    """两个 Agent 并发跑同一条 prompt，返回 ``{agent_id: 正文}``。

    ``concurrency`` 是**调用方**的策略：同一时刻最多几个 Agent 在跑（整回合，
    包括工具执行）。库不提供这个上限，也不该提供 —— 它是"谁能同时说话"的产品决策。
    """
    if concurrency < 1:
        raise ValueError("concurrency 必须 >= 1")
    agents = build_agents(
        workspace=workspace,
        model_factory=model_factory,
        model_label=model_label,
        context_window=context_window,
    )
    await asyncio.gather(*(agent.start() for agent in agents))
    semaphore = asyncio.Semaphore(concurrency)
    outputs: dict[str, str] = {}

    async def drain(agent: LioriumAgent) -> None:
        chunks: list[str] = []
        async with semaphore:
            async for event in agent.run(prompt):
                if event.type == "delta":
                    chunks.append(str(event.get("content") or ""))
                if emit is not None:
                    emit(agent.spec.id, event)
        outputs[agent.spec.id] = "".join(chunks)

    try:
        await asyncio.gather(*(drain(agent) for agent in agents))
    finally:
        await asyncio.gather(*(agent.close() for agent in agents))
    return outputs


def make_model_factory(registry: ProviderRegistry, *, temperature: float = 0.3):
    """按 ``Provider::model`` 标签构造模型（同一个标签复用实例）。"""
    cache: dict[str, Any] = {}

    def factory(label: str) -> Any:
        if label not in cache:
            cache[label] = make_chat_model(registry.resolve(label), temperature=temperature)
        return cache[label]

    return factory


def _default_emit(agent_id: str, event: Event) -> None:
    if event.type == "delta":
        print(f"[{agent_id}] {event.get('content') or ''}", flush=True)
    elif event.type == "tool_start":
        print(f"[{agent_id}] ⏺ {event.get('tool_name')}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Liorium 多 Agent 示例（2 个 Agent + 调用方 semaphore）")
    parser.add_argument("-p", "--prompt", required=True, help="两个 Agent 共同处理的任务")
    parser.add_argument("--config", help="provider 配置路径（默认找 ~/.liorium/providers.json）")
    parser.add_argument("--model", help="模型标签 Provider::model（默认用配置里的主模型）")
    parser.add_argument("--workspace", default=".liorium-multi", help="工作区根目录（默认 .liorium-multi）")
    parser.add_argument("--concurrency", type=int, default=1, help="同时允许几个 Agent 在跑（默认 1）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = registry_from_args_or_prompt(
            config=Path(args.config).expanduser() if args.config else None
        )
        resolved = registry.resolve(args.model or registry.default_label())
    except ProviderConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    outputs = asyncio.run(
        run_agents(
            prompt=args.prompt,
            workspace=Path(args.workspace).expanduser().resolve(),
            model_factory=make_model_factory(registry),
            model_label=resolved.label,
            concurrency=args.concurrency,
            context_window=resolved.context_window or 128_000,
            emit=_default_emit,
        )
    )
    print()
    for agent_id, text in outputs.items():
        print(f"===== {agent_id} =====")
        print(text.strip() or "(没有输出)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
