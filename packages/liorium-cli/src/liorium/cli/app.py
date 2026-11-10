"""``liorium-cli`` 入口：参数解析、``.env`` 装载、单轮 / REPL 分发。"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Sequence

from liorium.provider import catalog_status, describe_thinking, ensure_catalog

from .env import (
    EnvFileError,
    EnvReport,
    env_bool,
    env_float,
    env_int,
    env_path,
    env_str,
    load_env,
)
from .providers import (
    ProviderConfigError,
    ProviderRegistry,
    ResolvedModel,
    normalize_format,
    registry_from_args_or_prompt,
)
from .session import CliOptions, CliSession
from .ui import Ui, configure_streams


THINKING_CHOICES = ("off", "low", "medium", "high")
MODELS_DEV_CHOICES = ("off", "cache", "refresh")


def _thinking_default() -> str:
    """``--thinking`` 的默认值；环境变量非法时明确报错。"""
    if env_bool("LIORIUM_NO_THINKING"):
        return "off"
    raw = (env_str("LIORIUM_THINKING") or "medium").strip().lower()
    if raw not in THINKING_CHOICES:
        raise ValueError(
            f"LIORIUM_THINKING={raw!r} 不合法（可用: {', '.join(THINKING_CHOICES)}）"
        )
    return raw


def _models_dev_default() -> str:
    raw = (env_str("LIORIUM_MODELS_DEV") or "cache").strip().lower()
    if raw not in MODELS_DEV_CHOICES:
        raise ValueError(
            f"LIORIUM_MODELS_DEV={raw!r} 不合法（可用: {', '.join(MODELS_DEV_CHOICES)}）"
        )
    return raw


def _models_dev_cache(explicit: Path | None) -> Path:
    """models.dev 缓存候选：显式 → 环境变量 → ``~/.liorium/cache/models_dev/api.json``。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path_value = env_path("LIORIUM_MODELS_DEV_CACHE")
    if env_path_value is not None:
        return env_path_value
    return Path.home() / ".liorium" / "cache" / "models_dev" / "api.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liorium-cli",
        description="Liorium CLI Agent（真实模型 + realm 能力包：核心工具 / 记忆 / 技能 / 待办 / 子代理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  liorium-cli
  liorium-cli -p "看看当前目录有什么文件"
  liorium-cli --provider Go --model deepseek-v4.1-flash
  liorium-cli --skills-dir examples/skills
  liorium-cli --tools read,find,execute
  liorium-cli --no-core-tools --no-memory --no-skills --no-todo --no-subagents
  liorium-cli --list-models
  echo "总结 README" | liorium-cli --json

等价的模块入口: python -m liorium.cli
""",
    )
    parser.add_argument("--env-file", type=Path, default=None, help="从指定 .env 加载配置（默认找 ./.env、./.env.local）")
    parser.add_argument("--no-env", action="store_true", help="不加载任何 .env（只用真实环境变量与命令行）")
    parser.add_argument(
        "--config", type=Path, default=env_path("LIORIUM_PROVIDER_CONFIG"),
        help="provider 配置路径（默认 ~/.liorium/providers.json 或 ./providers.json；也可 LIORIUM_PROVIDER_CONFIG）",
    )
    parser.add_argument("--provider", default=env_str("LIORIUM_PROVIDER") or None, help="provider 名（覆盖 main_model 的 provider）")
    parser.add_argument("--model", default=env_str("LIORIUM_MODEL") or None, help="模型名或 Provider::model")
    parser.add_argument(
        "--api-format", default=env_str("LIORIUM_API_FORMAT") or None,
        help="强制 api 格式（openai_chat/openai_responses/anthropic_messages/google_gemini）",
    )
    parser.add_argument(
        "--workspace", type=Path, default=env_path("LIORIUM_WORKSPACE"),
        help="工作区根（裸路径与 execute 的 cwd），默认当前目录",
    )
    parser.add_argument(
        "--inbox", type=Path, default=env_path("LIORIUM_INBOX"),
        help="Agent 根目录（prompt/checkpoint），默认 <workspace>/.liorium-cli/agent",
    )
    parser.add_argument(
        "--system", type=Path, default=env_path("LIORIUM_SYSTEM"),
        help="用这个文件当 system prompt（替代 inbox/AGENT.md）",
    )
    parser.add_argument(
        "--no-core-tools",
        action="store_true",
        default=env_bool("LIORIUM_NO_CORE_TOOLS"),
        help="不装配核心工具（read / write / edit / search / find / execute）",
    )
    parser.add_argument(
        "--tools",
        default=env_str("LIORIUM_TOOLS") or None,
        help="只装配这些核心工具（逗号分隔，如 read,write,find）；默认六个全给",
    )
    parser.add_argument("--no-memory", action="store_true", default=env_bool("LIORIUM_NO_MEMORY"), help="不启用长期记忆（memories://）")
    parser.add_argument("--no-skills", action="store_true", default=env_bool("LIORIUM_NO_SKILLS"), help="不启用技能库（skills://）")
    parser.add_argument(
        "--no-todo",
        action="store_true",
        default=env_bool("LIORIUM_NO_TODO"),
        help="不启用待办（todo / todo_list 工具，及每 50 回合的待办提醒）",
    )
    parser.add_argument(
        "--no-subagents",
        action="store_true",
        default=env_bool("LIORIUM_NO_SUBAGENTS"),
        help="不启用子代理（newSubagent / invokeSubagent）",
    )
    parser.add_argument(
        "--memory-dir", type=Path, default=env_path("LIORIUM_MEMORY_DIR"),
        help="记忆库目录（默认 <inbox>/memory）",
    )
    parser.add_argument(
        "--embed-model",
        default=env_str("LIORIUM_EMBED_MODEL"),
        help="embedding 模型（如 text-embedding-3-small）；给了才启用语义检索",
    )
    parser.add_argument(
        "--embed-base-url",
        default=env_str("LIORIUM_EMBED_BASE_URL"),
        help="embedding 的 OpenAI 兼容 base_url（默认跟随当前 provider）",
    )
    parser.add_argument(
        "--embed-key",
        default=env_str("LIORIUM_EMBED_KEY"),
        help="embedding 的 API key（默认跟随当前 provider）",
    )
    parser.add_argument("--no-vectors", action="store_true", default=env_bool("LIORIUM_NO_VECTORS"), help="不建向量索引（只跑关键词检索）")
    parser.add_argument("--no-extraction", action="store_true", default=env_bool("LIORIUM_NO_EXTRACTION"), help="写记忆后不自动抽实体/关系")
    parser.add_argument(
        "--skills-dir", type=Path, default=env_path("LIORIUM_SKILLS_DIR"),
        help="技能目录（默认 <inbox>/skill.d；示例: examples/skills）",
    )
    parser.add_argument("-p", "--prompt", default=env_str("LIORIUM_PROMPT") or None, help="单轮模式：执行一次后退出")
    parser.add_argument("--json", action="store_true", default=env_bool("LIORIUM_JSON"), help="事件以 JSON Lines 输出（不渲染）")
    parser.add_argument("--no-color", action="store_true", default=env_bool("LIORIUM_NO_COLOR"), help="关闭 ANSI 颜色")
    parser.add_argument(
        "--thinking",
        choices=THINKING_CHOICES,
        default=_thinking_default(),
        help="思考强度（默认 medium；off 关闭）",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_const",
        const="off",
        dest="thinking",
        help="关闭思考（等价 --thinking off；显示开关是 --hide-reasoning）",
    )
    parser.add_argument(
        "--hide-reasoning",
        action="store_true",
        default=env_bool("LIORIUM_HIDE_REASONING"),
        help="只隐藏 reasoning_delta 显示（不改变是否请求思考）",
    )
    parser.add_argument("--temperature", type=float, default=env_float("LIORIUM_TEMPERATURE", 0.3), help="采样温度（默认 0.3；思考开启时不发送）")
    parser.add_argument("--max-turns", type=int, default=env_int("LIORIUM_MAX_TURNS", 500), help="单回合模型调用上限（默认 500）")
    parser.add_argument(
        "--models-dev",
        choices=MODELS_DEV_CHOICES,
        default=_models_dev_default(),
        help="models.dev 目录策略：cache 只读本地缓存（默认），refresh 显式联网",
    )
    parser.add_argument(
        "--models-dev-cache",
        type=Path,
        default=env_path("LIORIUM_MODELS_DEV_CACHE"),
        help="models.dev 缓存文件（默认 ~/.liorium/cache/models_dev/api.json）",
    )
    parser.add_argument(
        "--refresh-models",
        action="store_true",
        default=env_bool("LIORIUM_REFRESH_MODELS"),
        help="启动时从各 Provider /models 刷新模型列表（网络操作）",
    )
    parser.add_argument("--list-models", action="store_true", help="列出配置里的模型后退出")
    return parser


def _resolve_initial(registry: ProviderRegistry, args: argparse.Namespace) -> ResolvedModel:
    label: str
    if args.model and "::" in args.model:
        label = args.model
    elif args.provider:
        provider = registry.provider(args.provider)
        model = args.model or (provider.models[0] if provider.models else "")
        label = f"{provider.name}::{model}"
    elif args.model:
        label = registry.resolve(args.model).label
    else:
        label = registry.default_label()

    resolved = registry.resolve(label)
    if args.api_format:
        fmt = normalize_format(args.api_format)
        if fmt is None:
            raise ProviderConfigError(
                f"未知 api 格式: {args.api_format!r}（支持 {', '.join(('openai_chat', 'openai_responses', 'anthropic_messages', 'google_gemini'))}）"
            )
        resolved = registry.resolve(label, api_format=fmt)
    return resolved


def _scan_env_args(argv: Sequence[str]) -> tuple[Path | None, bool]:
    """先扫一遍 ``--env-file`` / ``--no-env``：必须在建 parser 之前加载 .env。

    argparse 的 ``default=`` 是**建 parser 时**求值的，所以顺序只能是：
    读 argv 里的 env 参数 → 加载 .env 进 ``os.environ`` → 再建 parser。
    """
    env_file: Path | None = None
    use_env = True
    index = 0
    while index < len(argv):
        arg = str(argv[index])
        if arg == "--no-env":
            use_env = False
        elif arg == "--env-file" and index + 1 < len(argv):
            env_file = Path(str(argv[index + 1]))
            index += 1
        elif arg.startswith("--env-file="):
            env_file = Path(arg.split("=", 1)[1])
        index += 1
    return env_file, use_env


def _parse_tools(raw: str | None) -> tuple[str, ...]:
    """``--tools read,write`` → ``("read", "write")``（保持顺序、去重）。"""
    if not raw:
        return ()
    names: list[str] = []
    for item in str(raw).split(","):
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


async def _prepare_registry(registry: ProviderRegistry, args: argparse.Namespace) -> None:
    """启动期的 models.dev / /models 刷新（只改内存，不写回配置）。"""
    catalog: dict[str, Any] = {}
    cache_file = _models_dev_cache(args.models_dev_cache)
    if args.models_dev != "off":
        catalog = await ensure_catalog(
            allow_network=args.models_dev == "refresh",
            force=args.models_dev == "refresh",
            cache_path=cache_file,
        )
        if args.models_dev == "refresh" and not catalog:
            status = catalog_status(cache_path=cache_file)
            print(
                f"models.dev 刷新失败: {status.get('last_error') or '目录为空'}",
                file=sys.stderr,
            )
    if args.refresh_models:
        results = await registry.refresh_models()
        for name, result in results.items():
            if not result.get("ok"):
                print(
                    f"刷新 provider {name!r} 的模型列表失败: {result.get('error')}",
                    file=sys.stderr,
                )
    if catalog:
        registry.enrich(catalog)


def _print_model_list(registry: ProviderRegistry, *, color: bool = True) -> int:
    ui = Ui(color=color)
    try:
        default = registry.default_label()
    except ProviderConfigError:
        default = ""
    for label in registry.labels():
        try:
            resolved = registry.resolve(label)
        except ProviderConfigError as exc:
            print(f"{label}: {exc}", file=sys.stderr)
            continue
        line = (f"{ui.glyph('arrow')} " if label == default else "  ") + resolved.describe()
        shape = describe_thinking(
            resolved.thinking, resolved.api_format, model=resolved.model
        )
        if resolved.thinking.reasoning is False:
            line += f"  思考 none（{resolved.thinking.source or 'unknown'}）"
        elif shape:
            levels = "/".join(shape.get("levels") or ())
            suffix = f"({levels})" if levels else ""
            line += f"  思考 {shape.get('mode')}{suffix}（{shape.get('source')}）"
        ui.writeln(line)
    return 0


async def run_cli(
    argv: Sequence[str] | None = None,
    *,
    registry: ProviderRegistry | None = None,
    model_factory: Callable[[str], Any] | None = None,
    input_fn: Callable[[str], str] = input,
    install_signal: bool = True,
) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    env_file, use_env = _scan_env_args(raw_argv)
    env_report = EnvReport()
    if use_env:
        try:
            env_report = load_env(env_file)
        except EnvFileError as exc:
            print(f".env 错误: {exc}", file=sys.stderr)
            return 2
        for warning in env_report.warnings:
            print(f".env 提示: {warning}", file=sys.stderr)

    try:
        parser = build_parser()
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    args = parser.parse_args(raw_argv)
    if args.no_core_tools and args.tools:
        print("配置错误: --tools 与 --no-core-tools 不能同时使用", file=sys.stderr)
        return 2

    configure_streams()
    if os.name == "nt":
        os.system("")  # 打开 Windows 控制台的 ANSI 处理

    try:
        if registry is None:
            registry = registry_from_args_or_prompt(
                config=args.config,
                input_fn=input_fn,
            )
            await _prepare_registry(registry, args)
    except ProviderConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    try:
        if args.list_models:
            return _print_model_list(registry, color=not args.no_color)
        resolved = _resolve_initial(registry, args)
    except ProviderConfigError as exc:
        print(f"模型解析失败: {exc}", file=sys.stderr)
        return 2

    workspace = (args.workspace or Path.cwd()).expanduser().resolve()
    inbox = (args.inbox or (workspace / ".liorium-cli" / "agent")).expanduser().resolve()
    one_shot = args.prompt is not None or not sys.stdin.isatty()
    options = CliOptions(
        workspace=workspace,
        inbox=inbox,
        system_prompt=args.system,
        temperature=args.temperature,
        max_turns=args.max_turns,
        thinking=args.thinking,
        show_reasoning=not args.hide_reasoning,
        json_mode=args.json,
        color=not args.no_color,
        # 单轮/管道模式不打印 banner，保持输出干净（JSON 模式本来也不打印）
        show_banner=not one_shot,
        enable_core_tools=not args.no_core_tools,
        tools=_parse_tools(args.tools),
        enable_memory=not args.no_memory,
        enable_skills=not args.no_skills,
        enable_todo=not args.no_todo,
        enable_subagents=not args.no_subagents,
        memory_dir=args.memory_dir,
        skills_dir=args.skills_dir,
        embed_model=args.embed_model,
        embed_base_url=args.embed_base_url,
        embed_key=args.embed_key,
        enable_vectors=not args.no_vectors,
        enable_extraction=not args.no_extraction,
        models_dev_cache=_models_dev_cache(args.models_dev_cache),
        env_report=env_report or None,
    )
    session = CliSession(
        registry,
        resolved,
        options,
        input_fn=input_fn,
        model_factory=model_factory,
    )

    try:
        await session.start()
    except ProviderConfigError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 启动失败要显式，不要静默
        print(f"启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("LIORIUM_CLI_DEBUG"):
            traceback.print_exc()
        return 2

    previous_handler = None
    if install_signal:
        def _sigint(signum: int, frame: Any) -> None:  # noqa: ARG001
            if session.busy:
                session.request_interrupt()
            else:
                raise KeyboardInterrupt

        try:
            previous_handler = signal.signal(signal.SIGINT, _sigint)
        except ValueError:
            previous_handler = None  # 非主线程（测试）：不装信号处理器

    code = 0
    try:
        prompt = args.prompt
        if prompt is None and not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
            if not prompt:
                prompt = None
        if prompt is not None:
            code = 0 if await session.run_turn(prompt) else 1
        else:
            if options.json_mode:
                print("--json 需要配合 -p/管道输入使用", file=sys.stderr)
                code = 2
            else:
                code = await session.repl()
    finally:
        await session.close()
        if install_signal and previous_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except ValueError:
                pass
    return code


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run_cli(argv))
    except KeyboardInterrupt:
        print()
        return 130


__all__ = ["build_parser", "main", "run_cli"]
