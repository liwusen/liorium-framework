"""``liorium-cli`` 入口：参数解析、``.env`` 装载、单轮 / REPL 分发。

配置文件分两层（都可以不存在）：

- ``providers.json``：provider 与密钥（由 :mod:`liorium.cli.setup` 写）；
- ``config.json``：非密钥设置（默认模型 / 思考 / 记忆 / 技能 / 界面）。

优先级：命令行 > 进程环境变量 > ``.env`` > ``config.json`` > 内置默认。
新机器上第一次运行会建空骨架并进入「未配置」模式，而不是在这里追问 API key。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from liorium.provider import catalog_status, describe_thinking, ensure_catalog

from . import config as cli_config
from . import setup as cli_setup
from .config import ConfigError
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
    ensure_registry,
    normalize_format,
)
from .session import CliOptions, CliSession
from .ui import Ui, configure_streams

THINKING_CHOICES = cli_config.THINKING_LEVELS
MODELS_DEV_CHOICES = ("off", "cache", "refresh")

UNCONFIGURED_COMMANDS = [
    ("/setup [providers|memory]", "交互式配置 provider / 记忆系统"),
    ("/show", "查看当前配置（路径、provider、记忆、设置）"),
    ("/doctor", "体检：文件 / 密钥 / 目录 / 依赖"),
    ("/init", "重建空的配置骨架（已存在的不动）"),
    ("/path", "打印各配置路径"),
    ("/help", "显示这段帮助"),
    ("/exit", "退出"),
]


def _opt_str(
    config: Mapping[str, Any], key: str, env_name: str, fallback: str | None = None
) -> str | None:
    """字符串默认值：环境变量 > ``config.json`` > ``fallback``。"""
    env_value = env_str(env_name)
    if env_value:
        return env_value
    value = cli_config.get_value(config, key)
    if value not in (None, ""):
        return str(value)
    return fallback


def _opt_path(
    config: Mapping[str, Any], key: str, env_name: str
) -> Path | None:
    env_value = env_path(env_name)
    if env_value is not None:
        return env_value
    value = cli_config.get_value(config, key)
    return Path(str(value)).expanduser() if value else None


def _opt_bool(
    config: Mapping[str, Any], key: str, env_name: str, fallback: bool
) -> bool:
    if os.environ.get(env_name) is not None:
        return env_bool(env_name, fallback)
    value = cli_config.get_value(config, key)
    return fallback if value is None else bool(value)


def _opt_disabled(
    config: Mapping[str, Any], key: str, env_name: str, *, fallback_enabled: bool = True
) -> bool:
    """``--no-xxx`` 这类否定开关的默认值：``config`` 里 enabled=False 就是 True。"""
    if os.environ.get(env_name) is not None:
        return env_bool(env_name, False)
    enabled = cli_config.get_value(config, key)
    return not (fallback_enabled if enabled is None else bool(enabled))


def _thinking_default(config: Mapping[str, Any]) -> str:
    """``--thinking`` 的默认值；环境变量 / 配置非法时明确报错。"""
    if env_bool("LIORIUM_NO_THINKING"):
        return "off"
    raw = (
        env_str("LIORIUM_THINKING")
        or str(cli_config.get_value(config, "thinking") or "medium")
    ).strip().lower()
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
    """models.dev 缓存候选：显式 → 环境变量 → ``<home>/cache/models_dev/api.json``。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path_value = env_path("LIORIUM_MODELS_DEV_CACHE")
    if env_path_value is not None:
        return env_path_value
    return cli_config.cache_dir() / "models_dev" / "api.json"


def build_parser(*, config: Mapping[str, Any] | None = None) -> argparse.ArgumentParser:
    """建参数解析器。

    ``config`` 是 ``config.json`` 的内容（``None`` = 不读配置文件）。默认值优先级：
    环境变量 > ``config`` > 内置默认——argparse 的 ``default=`` 只在没给参数时生效，
    所以命令行天然最高。
    """
    settings: Mapping[str, Any] = config or {}
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

配置（不用进 CLI，也不需要 API key 就能跑）:
  liorium-cli setup                      # 交互式向导：provider / 记忆系统
  liorium-cli setup providers add deepseek --base-url https://api.deepseek.com \\
      --model deepseek-chat --api-format openai_chat
  liorium-cli setup providers list|show|edit|rm|default|test|set-key
  liorium-cli setup memory show|set|enable|disable|init
  liorium-cli setup show | doctor | init | path

配置文件（都可缺省，缺省时用内置默认）:
  ~/.liorium/providers.json   provider 与密钥（0600）
  ~/.liorium/config.json      非密钥设置；命令行参数与环境变量仍然优先
  LIORIUM_HOME                改配置根目录

进入 CLI 后也可以 /setup 交互式配置；全空机器上第一次运行会建空骨架并进入「未配置」模式。

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
    parser.add_argument("--model", default=_opt_str(settings, "model", "LIORIUM_MODEL"), help="模型名或 Provider::model")
    parser.add_argument(
        "--api-format", default=env_str("LIORIUM_API_FORMAT") or None,
        help="强制 api 格式（openai_chat/openai_responses/anthropic_messages/google_gemini）",
    )
    parser.add_argument(
        "--workspace", type=Path, default=_opt_path(settings, "workspace", "LIORIUM_WORKSPACE"),
        help="工作区根（裸路径与 execute 的 cwd），默认当前目录",
    )
    parser.add_argument(
        "--inbox", type=Path, default=_opt_path(settings, "inbox", "LIORIUM_INBOX"),
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
    parser.add_argument(
        "--no-memory",
        action="store_true",
        default=_opt_disabled(settings, "memory.enabled", "LIORIUM_NO_MEMORY"),
        help="不启用长期记忆（memories://）",
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        default=_opt_disabled(settings, "skills.enabled", "LIORIUM_NO_SKILLS"),
        help="不启用技能库（skills://）",
    )
    parser.add_argument(
        "--no-todo",
        action="store_true",
        default=_opt_disabled(settings, "todo.enabled", "LIORIUM_NO_TODO"),
        help="不启用待办（todo / todo_list 工具，及每 50 回合的待办提醒）",
    )
    parser.add_argument(
        "--no-subagents",
        action="store_true",
        default=_opt_disabled(settings, "subagents.enabled", "LIORIUM_NO_SUBAGENTS"),
        help="不启用子代理（newSubagent / invokeSubagent）",
    )
    parser.add_argument(
        "--memory-dir", type=Path, default=_opt_path(settings, "memory.dir", "LIORIUM_MEMORY_DIR"),
        help="记忆库目录（默认 <inbox>/memory）",
    )
    parser.add_argument(
        "--embed-model",
        default=_opt_str(settings, "memory.embed_model", "LIORIUM_EMBED_MODEL", ""),
        help="embedding 模型（如 text-embedding-3-small）；给了才启用语义检索",
    )
    parser.add_argument(
        "--embed-base-url",
        default=_opt_str(settings, "memory.embed_base_url", "LIORIUM_EMBED_BASE_URL", ""),
        help="embedding 的 OpenAI 兼容 base_url（默认跟随当前 provider）",
    )
    parser.add_argument(
        "--embed-key",
        default=_opt_str(settings, "memory.embed_key", "LIORIUM_EMBED_KEY", ""),
        help="embedding 的 API key（默认跟随当前 provider）",
    )
    parser.add_argument(
        "--no-vectors",
        action="store_true",
        default=_opt_disabled(settings, "memory.vectors", "LIORIUM_NO_VECTORS"),
        help="不建向量索引（只跑关键词检索）",
    )
    parser.add_argument(
        "--no-extraction",
        action="store_true",
        default=_opt_disabled(settings, "memory.extraction", "LIORIUM_NO_EXTRACTION"),
        help="写记忆后不自动抽实体/关系",
    )
    parser.add_argument(
        "--skills-dir", type=Path, default=_opt_path(settings, "skills.dir", "LIORIUM_SKILLS_DIR"),
        help="技能目录（默认 <inbox>/skill.d；示例: examples/skills）",
    )
    parser.add_argument("-p", "--prompt", default=env_str("LIORIUM_PROMPT") or None, help="单轮模式：执行一次后退出")
    parser.add_argument("--json", action="store_true", default=env_bool("LIORIUM_JSON"), help="事件以 JSON Lines 输出（不渲染）")
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=_opt_disabled(settings, "ui.color", "LIORIUM_NO_COLOR"),
        help="关闭 ANSI 颜色",
    )

    parser.add_argument(
        "--thinking",
        choices=THINKING_CHOICES,
        default=_thinking_default(settings),
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
        default=_opt_bool(settings, "ui.hide_reasoning", "LIORIUM_HIDE_REASONING", False),
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


def _print_cold_start_banner(
    ui: Ui, *, tried: Sequence[Path], created: Mapping[str, Any]
) -> None:
    ui.writeln()
    ui.writeln(ui.mint("Liorium CLI") + ui.dim("  还没有配置 provider"))
    ui.writeln(ui.dim("  配置根目录: ") + str(cli_config.home_dir()))
    for path in tried:
        ui.writeln(ui.dim("  查找过: ") + str(path))
    made = {key: value for key, value in created.items() if value}
    if made:
        ui.writeln(ui.dim("  已建: ") + ", ".join(f"{key}={value}" for key, value in made.items()))
    ui.writeln(ui.dim("  下一步（任选）:"))
    ui.writeln("    liorium-cli setup")
    ui.writeln(
        "    liorium-cli setup providers add deepseek"
        " --base-url https://api.deepseek.com --model deepseek-chat"
    )
    ui.writeln("    liorium-cli setup memory show")
    ui.writeln(ui.dim("  或在这里输入 /setup 直接配置（/help 看命令，/exit 退出）"))
    ui.writeln()


def _usable_registry(ctx: cli_setup.Ctx) -> ProviderRegistry | None:
    """配置向导返回后重新读一次 providers.json（可能刚被写好）。"""
    try:
        providers = cli_setup.read_providers(ctx)
    except ConfigError:
        return None
    if not providers.providers:
        return None
    try:
        return ProviderRegistry(providers, source=Path(ctx.providers))
    except ProviderConfigError:
        return None


async def _unconfigured_repl(
    args: argparse.Namespace, *, input_fn: Callable[[str], str]
) -> ProviderRegistry | int:
    """「未配置」模式的 REPL：只处理配置类命令，配好 provider 就返回 registry。

    返回 ``int`` = 直接退出的退出码；返回 ``ProviderRegistry`` = 可以启动会话了。
    """
    ui = Ui(color=not args.no_color)
    ctx = cli_setup.make_ctx(input_fn=input_fn, print_fn=ui.writeln)
    while True:
        try:
            line = await asyncio.to_thread(input_fn, ui.mint("liorium[未配置]> "))
        except (EOFError, KeyboardInterrupt):
            ui.writeln()
            return 0
        text = (line or "").strip()
        if not text:
            continue
        if not text.startswith("/"):
            ui.warn(
                "还没有配置 provider：输入 /setup 开始配置，"
                "或退出后运行 liorium-cli setup …"
            )
            continue
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command in ("/exit", "/quit", "/q"):
            return 0
        if command == "/help":
            for name, description in UNCONFIGURED_COMMANDS:
                ui.writeln(f"  {ui.mint(name.ljust(28))}{description}")
            continue
        result = None
        try:
            if command in ("/setup", "/config", "/configure"):
                result = cli_setup.wizard(ctx, argument or None)
            elif command == "/show":
                cli_setup.print_overview(ctx)
            elif command == "/doctor":
                cli_setup.doctor(ctx)
            elif command == "/init":
                cli_setup.setup_init(ctx)
            elif command == "/path":
                ui.writeln(f"  home       {cli_config.home_dir()}")
                ui.writeln(f"  providers  {ctx.providers}")
                ui.writeln(f"  config     {ctx.config_file}")
            else:
                ui.warn(f"未知命令: {command}（/help 查看全部）")
                continue
        except (ConfigError, ProviderConfigError) as exc:
            ui.error(str(exc))
        registry = _usable_registry(ctx)
        if registry is not None:
            ui.info("provider 已配置，启动会话…")
            return registry
        if result is not None and result.memory_changed:
            ui.info("记忆设置已保存；还差 provider 才能开始对话（/setup providers）")


async def _cold_start(
    args: argparse.Namespace,
    *,
    tried: Sequence[Path],
    created: Mapping[str, Any],
    input_fn: Callable[[str], str],
    interactive: bool,
) -> ProviderRegistry | int:
    """没有可用 provider 时的启动路径：建骨架 → 交互式配置，或明确退出。"""
    ui = Ui(color=not args.no_color)
    _print_cold_start_banner(ui, tried=tried, created=created)
    sys.stdout.flush()  # 让 banner 先于 stderr 提示出现（重定向时缓冲区顺序不同）
    if args.list_models:
        print("没有可列出的模型：先运行 liorium-cli setup …", file=sys.stderr)
        return 2
    if args.json or args.prompt is not None or not interactive:
        print(
            "尚未配置 provider：先运行 liorium-cli setup …"
            "（或 liorium-cli setup providers add …），"
            f"配置目录 {cli_config.home_dir()}",
            file=sys.stderr,
        )
        return 2
    return await _unconfigured_repl(args, input_fn=input_fn)


async def run_cli(
    argv: Sequence[str] | None = None,
    *,
    registry: ProviderRegistry | None = None,
    model_factory: Callable[[str], Any] | None = None,
    input_fn: Callable[[str], str] = input,
    install_signal: bool = True,
    interactive: bool | None = None,
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

    # ``config.json`` 必须在建 parser 之前读：argparse 的 ``default=`` 建 parser 时求值。
    try:
        settings = cli_config.load_config()
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    try:
        parser = build_parser(config=settings)
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

    interactive_mode = sys.stdin.isatty() if interactive is None else interactive

    # 新机器：先把配置骨架建出来（已存在的不动），再判断能不能起会话。
    created: dict[str, Any] = {}
    if args.config is None:
        try:
            created = cli_config.ensure_home()
        except OSError as exc:
            print(f"配置目录创建失败: {exc}", file=sys.stderr)
            return 2

    loaded = registry is None
    tried: list[Path] = []
    try:
        if loaded:
            registry, tried, registry_created = ensure_registry(config=args.config)
            created = {**created, **registry_created}
    except ProviderConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if registry is None:
        outcome = await _cold_start(
            args,
            tried=tried,
            created=created,
            input_fn=input_fn,
            interactive=interactive_mode,
        )
        if isinstance(outcome, int):
            return outcome
        registry = outcome

    if loaded:
        try:
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
    one_shot = args.prompt is not None or not interactive_mode
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
        config_file=cli_config.config_path(),
        providers_file=args.config or cli_config.providers_path(),
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
        if prompt is None and not interactive_mode:
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
    """命令入口：``setup …`` 走配置子命令，其余走正常的 CLI 会话。"""
    raw = list(argv) if argv is not None else sys.argv[1:]
    try:
        if raw and raw[0] == "setup":
            return cli_setup.main(raw[1:])
        return asyncio.run(run_cli(raw))
    except KeyboardInterrupt:
        print()
        return 130


__all__ = ["build_parser", "main", "run_cli"]

