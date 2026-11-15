"""事件流完整使用示例：一个 Agent 从 ``start()`` 到 ``close()`` 的全部对外动作。

覆盖现阶段（2026-11）的公共面::

    start / push / stream / status / interrupt / close

    Bucket.MAJOR        入队即排回合（等当前回合结束）
    Bucket.BATCHED      合批窗口到期后开回合，整批合成一条 user 消息
    Bucket.ENVIRONMENT  永不开回合：回合开始搭车；回合进行中按 env_interval 节流注入
    Flag.AUTO           忙碌 ⇒ 注入当前回合；空闲 ⇒ 照常开回合
    env_interval        ENVIRONMENT 回合中途注入的最小间隔（秒，<=0 = 每个安全点都投）

四个场景：

    ① 一个回合：工具帧 + 中途插话（Flag.AUTO）+ 环境注入（env_interval=0，立即）
    ② 节流：env_interval=60，回合中途推的环境条目等下一个回合再搭车
    ③ 合批：两条 BATCHED 合成一个回合、一条 user 消息
    ④ 中断：工具执行中 interrupt()，回合以 interrupted 收尾

全程离线：模型是脚本假模型，工具是本地 Event 闸门；不联网、不读 .env。

跑法（仓库根目录）::

    .venv\\Scripts\\python.exe examples\\event_stream.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

# --- 从源码检出直接运行：补上 packages/*/src（已把包装进解释器时这段可以删） -----
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _src in sorted((_REPO_ROOT / "packages").glob("*/src")):
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from liorium.agent import (  # noqa: E402
    AgentSpec,
    BaseRealm,
    BatchWindow,
    Bucket,
    Flag,
    Limits,
    LioriumAgent,
    RealmSpec,
)
from liorium.core.types import Event  # noqa: E402

TERMINAL = frozenset({"done", "interrupted", "error"})
"""一个回合只有一条终态帧（``done`` / ``interrupted`` / ``error``），它永远是最后一帧。"""

USAGE = {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18}
"""假模型也"上报用量"：真实 provider 都会报，压缩中间件靠它判断上下文占用。"""


# ---------------------------------------------------------------------------
# 假模型：按脚本返回答复（字符串 = 分块流式正文，AIMessage = 可带 tool_calls）
# ---------------------------------------------------------------------------


class ScriptedModel(BaseChatModel):
    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "liorium-example-scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedModel":
        return self

    def _next(self) -> Any:
        index = len(self.calls) - 1
        if index < len(self.responses):
            return self.responses[index]
        return self.responses[-1] if self.responses else ""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        item = self._next()
        message = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        message = message.model_copy(update={"usage_metadata": USAGE})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.calls.append(list(messages))
        item = self._next()
        if isinstance(item, AIMessage):
            if item.content:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=item.content, usage_metadata=USAGE)
                )
            if item.tool_calls:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="", tool_calls=item.tool_calls, usage_metadata=USAGE
                    )
                )
            return
        text = str(item)
        middle = len(text) // 2 or len(text)
        for chunk in (text[:middle], text[middle:]):
            if chunk:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=chunk, usage_metadata=USAGE)
                )


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# ---------------------------------------------------------------------------
# 最小 realm：只提供一个工具（工具永远来自 realm，Agent 自己不添加工具）
# ---------------------------------------------------------------------------


class SlowRealm(BaseRealm):
    """``investigate`` 会停在闸门上，给宿主留出一个"回合正在进行"的窗口。"""

    id = "slow"

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.entered = entered
        self.release = release

    def tools(self) -> list[Any]:
        entered, release = self.entered, self.release

        @tool
        async def investigate(topic: str) -> str:
            """模拟一个慢工具（真实场景：检索、抓网页、跑命令）。"""
            entered.set()
            await release.wait()
            return f"{topic}：3 条资料"

        return [investigate]


# ---------------------------------------------------------------------------
# 宿主侧小工具：建 Agent / 收一个回合 / 打印
# ---------------------------------------------------------------------------


def build_agent(
    workspace: Path,
    model: ScriptedModel,
    *,
    entered: asyncio.Event,
    release: asyncio.Event,
    env_interval: float = 30.0,
    batch_window: BatchWindow | None = None,
) -> LioriumAgent:
    (workspace / "AGENT.md").write_text("你是 Liorium 的示例角色，回答简短。", encoding="utf-8")
    spec = AgentSpec(
        id="demo",
        inbox_dir=workspace,
        model="scripted::demo",  # 不透明标签：只进指纹与日志
        prompt_sources=("AGENT.md",),
        realms=(RealmSpec(id="slow"),),
        limits=Limits(context_window=8_000, max_turns=8),
        workspace_root=workspace,
    )
    realm = SlowRealm(entered, release)
    return LioriumAgent(
        spec=spec,
        model_factory=lambda _label: model,
        realm_factories={"slow": lambda _spec: realm},
        env_interval=env_interval,
        batch_window=batch_window,
    )


def show(event: Event, t0: float) -> None:
    payload = event.to_dict()
    payload.pop("type", None)
    detail = " ".join(
        f"{key}={value!r}" if isinstance(value, (list, dict)) else f"{key}={value}"
        for key, value in payload.items()
        if value not in (None, "", 0, [], {})
    )
    print(f"  +{time.monotonic() - t0:5.2f}s {event.type:<10} {detail}")


def show_status(agent: LioriumAgent, label: str) -> None:
    status = agent.status()
    buckets = {name: count for name, count in status.pending_by_bucket if count}
    print(
        f"  [status:{label}] running={status.running} turn_id={status.turn_id} "
        f"queue={status.queue_depth} pending_context={status.pending_context} "
        f"buckets={buckets or '-'}"
    )


def show_model_calls(model: ScriptedModel, title: str) -> None:
    print(f"  [模型] {title}")
    for index, call in enumerate(model.calls, start=1):
        last = next((m for m in reversed(call) if isinstance(m, HumanMessage)), None)
        text = "" if last is None else str(last.content).replace("\n", " ⏎ ")
        print(f"    #{index}: {text}")


async def collect_turn(
    iterator: Any, t0: float, *, timeout: float = 5.0
) -> list[Event]:
    """收齐"当前这一回合"的帧：到终态帧为止（``input`` 帧也算在内）。"""
    events: list[Event] = []
    while True:
        event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        events.append(event)
        show(event, t0)
        if event.type in TERMINAL:
            return events


# ---------------------------------------------------------------------------
# 场景 ①：一个回合里的工具、插话、环境注入
# ---------------------------------------------------------------------------


async def scene_turn(workspace: Path) -> None:
    print("\n=== ① 一个回合：工具帧 + 中途插话（AUTO）+ 环境注入（env_interval=0）===")
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedModel(
        responses=[
            tool_call("investigate", {"topic": "直播间"}, "call_1"),
            "目前 42 位观众，气氛不错。",
        ]
    )
    agent = build_agent(workspace, model, entered=entered, release=release, env_interval=0.0)
    await agent.start()
    t0 = time.monotonic()
    subscription = agent.stream()  # 全局流：不传 ids，所有回合的帧都从这里来
    iterator = subscription.__aiter__()
    try:
        # 先 stream 再 push：订阅发生在 __aiter__，反过来会漏掉 input.accepted 甚至首帧。
        agent.push("现在直播怎么样？", id="q1")
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        print("  [宿主] 工具正在执行 —— 此刻回合还在跑，可以插话 / 推环境数据")

        # 回合进行中插话：AUTO ⇒ 不开新回合，注入当前回合。
        agent.push("顺便说一下，我是新来的", id="q2", flags=Flag.AUTO)
        # 回合进行中推环境：ENVIRONMENT 永不开回合，env_interval=0 ⇒ 下一个安全点就投。
        agent.push("观众数 42", id="env1", bucket=Bucket.ENVIRONMENT)
        await asyncio.sleep(0.05)
        show_status(agent, "工具执行中")

        release.set()
        await collect_turn(iterator, t0)
    finally:
        release.set()
        await subscription.aclose()
        await agent.close()
    show_model_calls(model, "每次调用看到的最后一条 user 消息")
    print("  → 只有一条 start / 一条 done；插话与环境在安全点合流成一条 <context> 消息。")
    print("  → ENVIRONMENT 没有开回合，只是让正在跑的回合多看到一条消息。")


# ---------------------------------------------------------------------------
# 场景 ②：ENVIRONMENT 的节流（计时从"上一次投递"算）
# ---------------------------------------------------------------------------


async def scene_throttle(workspace: Path) -> None:
    print("\n=== ② 节流：env_interval=60，回合中途的环境条目等下一个回合 ===")
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedModel(
        responses=[
            tool_call("investigate", {"topic": "开播时长"}, "call_1"),
            "刚开播 3 分钟，人还不多。",
            "42 是刚才那条环境消息里的。",
        ]
    )
    agent = build_agent(workspace, model, entered=entered, release=release, env_interval=60.0)
    await agent.start()
    t0 = time.monotonic()
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    try:
        # 回合开始前推环境：turn 起点搭车（形态①），这一次投递同时起算 60s 计时器。
        agent.push("开播 3 分钟", id="env0", bucket=Bucket.ENVIRONMENT)
        agent.push("现在直播怎么样？", id="q1")
        await asyncio.wait_for(entered.wait(), timeout=5.0)

        # 60s 没到：这条环境留在 Inbox 里，当前回合看不到。
        agent.push("观众数 42", id="env1", bucket=Bucket.ENVIRONMENT)
        await asyncio.sleep(0.05)
        show_status(agent, "回合中（env1 在排队）")
        release.set()
        await collect_turn(iterator, t0)

        # 下一个回合开始：env1 按形态①并入该回合的 user 消息。
        agent.push("那 42 是哪来的？", id="q2")
        await collect_turn(iterator, t0)
    finally:
        release.set()
        await subscription.aclose()
        await agent.close()
    show_model_calls(model, "每次调用看到的最后一条 user 消息")
    print("  → env1 在第一个回合里没有 delivered 帧（模型也看不到）。")
    print("  → 第二个回合的 start.inputs 带上了 env1：它搭了下一个回合的车。")


# ---------------------------------------------------------------------------
# 场景 ③：BATCHED 合批开回合
# ---------------------------------------------------------------------------


async def scene_batch(workspace: Path) -> None:
    print("\n=== ③ 合批：两条 BATCHED 合成一个回合 ===")
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedModel(responses=["收到两条消息，一起回答。"])
    agent = build_agent(
        workspace,
        model,
        entered=entered,
        release=release,
        # 生产默认是 quiet_gap=2.5s；示例调小一点，跑得快。
        batch_window=BatchWindow(quiet_gap=0.05, max_age=0.5),
    )
    await agent.start()
    t0 = time.monotonic()
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    try:
        # 两条 BATCHED：入队不开回合，安静 0.05s 后窗口到期，整批开一个回合。
        agent.push("弹幕：主播唱歌", id="b1", bucket=Bucket.BATCHED)
        agent.push("弹幕：主播跳舞", id="b2", bucket=Bucket.BATCHED)
        await collect_turn(iterator, t0)
    finally:
        await subscription.aclose()
        await agent.close()
    show_model_calls(model, "每次调用看到的最后一条 user 消息")
    print("  → 一个回合代表整批：start.inputs 有两条 id，但只有一条 user 消息。")


# ---------------------------------------------------------------------------
# 场景 ④：中断进行中的回合
# ---------------------------------------------------------------------------


async def scene_interrupt(workspace: Path) -> None:
    print("\n=== ④ 中断：工具执行中 interrupt() ===")
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedModel(responses=[tool_call("investigate", {"topic": "长篇故事"}, "call_1")])
    agent = build_agent(workspace, model, entered=entered, release=release)
    await agent.start()
    t0 = time.monotonic()
    subscription = agent.stream()
    iterator = subscription.__aiter__()
    try:
        agent.push("讲个长故事", id="q1")
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        ok = await agent.interrupt(reason="user")
        print(f"  [宿主] interrupt() -> {ok}")
        await collect_turn(iterator, t0)
    finally:
        release.set()
        await subscription.aclose()
        await agent.close()
    print("  → 回合以 interrupted 收尾（仍是一条终态帧），队列里没跑的消息不受影响。")


# ---------------------------------------------------------------------------


async def main() -> None:
    scenes = (scene_turn, scene_throttle, scene_batch, scene_interrupt)
    for scene in scenes:
        workspace = Path(tempfile.mkdtemp(prefix="liorium-example-"))
        print(f"\n--- 工作区：{workspace} ---")
        try:
            await scene(workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
    print("\n全部场景跑完。")


if __name__ == "__main__":
    asyncio.run(main())
