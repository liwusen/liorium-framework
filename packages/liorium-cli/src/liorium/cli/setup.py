"""``liorium-cli setup``：provider 与记忆系统的命令行 / 交互式配置。

设计约束：

- **不联网也能跑完**（``providers test --network`` 是唯一例外）；
- **任何一步失败都要说清楚**：配置没落盘时绝不假装成功；
- **空机器可用**：文件不存在时按"空配置"处理，第一次写入自动建目录；
- **命令行与交互式共用同一套实现**：``setup providers add …`` 与 REPL 里的
  ``/setup providers`` 调同一批函数，避免两套行为漂移。

密钥只写 ``providers.json``（0600）；``config.json`` 只放非密钥设置，
``memory.embed_key`` 是唯一例外（显示时打码）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from liorium.provider import (
    SUPPORTED_FORMATS,
    ModelProvider,
    ModelProviders,
    ProviderConfigError,
    ProviderRegistry,
    normalize_format,
)
from liorium.provider.schema import dumps as dumps_providers
from liorium.provider.schema import loads as loads_providers

from . import config as cfg
from .config import ConfigError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CONTEXT = 128_000
PROVIDER_THINKING_DIALECTS = (
    "auto",
    "openai",
    "qwen",
    "deepseek",
    "glm",
    "mimo",
    "minimax",
    "none",
)

MEMORY_SHORTCUTS = {
    "enabled": "memory.enabled",
    "dir": "memory.dir",
    "embed-model": "memory.embed_model",
    "embed_model": "memory.embed_model",
    "embed-base-url": "memory.embed_base_url",
    "embed_base_url": "memory.embed_base_url",
    "embed-key": "memory.embed_key",
    "embed_key": "memory.embed_key",
    "vectors": "memory.vectors",
    "extraction": "memory.extraction",
}


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    """一次 setup 调用的全部外部依赖（测试可直接替换 IO）。"""

    providers: Path
    config_file: Path
    input_fn: Callable[[str], str] = input
    print_fn: Callable[[str], None] = print
    json_mode: bool = False
    inbox: Path | None = None
    """宿主已知的 Agent 根目录（会话内 ``/setup`` 传 ``options.inbox``；CLI 里为 None）。"""

    def out(self, text: str = "") -> None:
        self.print_fn(text)

    def ask(self, prompt: str) -> str:
        return str(self.input_fn(prompt) or "")

    def load_config(self) -> dict[str, Any]:
        return cfg.load_config(self.config_file)

    def save_config(self, config: Mapping[str, Any]) -> Path:
        return cfg.save_config(config, self.config_file)

    def emit_json(self, payload: Any) -> None:
        self.out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def make_ctx(
    *,
    home: Path | str | None = None,
    config_file: Path | str | None = None,
    providers: Path | str | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    json_mode: bool = False,
) -> Ctx:
    """按"显式参数 → 环境变量 → 默认位置"解析路径。"""
    if home is not None:
        os.environ[cfg.HOME_ENV] = str(Path(home).expanduser())
    if config_file is not None:
        os.environ[cfg.CONFIG_ENV] = str(Path(config_file).expanduser())
    if providers is not None:
        os.environ[cfg.PROVIDERS_ENV] = str(Path(providers).expanduser())
    return Ctx(
        providers=cfg.providers_path(),
        config_file=cfg.config_path(),
        input_fn=input_fn,
        print_fn=print_fn,
        json_mode=json_mode,
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _ok(ctx: Ctx, text: str) -> None:
    ctx.out(f"✓ {text}")


def _warn(ctx: Ctx, text: str) -> None:
    ctx.out(f"! {text}")


def _fail(ctx: Ctx, text: str) -> None:
    ctx.out(f"✗ {text}")


def _display_width(text: str) -> int:
    """终端显示宽度（CJK 算 2 列；``ljust`` 只数字符，会让中文表格歪）。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _kv(ctx: Ctx, rows: Iterable[tuple[str, str]], *, indent: int = 2) -> None:
    pad = " " * indent
    for key, value in rows:
        ctx.out(f"{pad}{_pad(key, 12)}{value}")


def _provider_options(providers: ModelProviders) -> str:
    return ", ".join(p.name for p in providers.providers) or "（空）"


def _all_labels(providers: ModelProviders) -> list[str]:
    return [f"{p.name}::{m}" for p in providers.providers for m in p.models]


def _fmt_model_count(provider: ModelProvider) -> str:
    return f"{len(provider.models)} 个模型" if provider.models else "（还没有模型）"


def _secret_input(ctx: Ctx, prompt: str) -> str:
    """秘密输入：真终端里用 getpass（不回显），测试/管道走注入的 input_fn。"""
    if ctx.input_fn is input and sys.stdin.isatty():
        try:
            import getpass

            return str(getpass.getpass(prompt) or "")
        except (EOFError, KeyboardInterrupt):
            return ""
    return ctx.ask(prompt)


# ---------------------------------------------------------------------------
# providers.json 读写
# ---------------------------------------------------------------------------


def read_providers(ctx: Ctx) -> ModelProviders:
    """读 providers.json；不存在返回空集合（第一次配置就是这种情况）。"""
    path = Path(ctx.providers)
    if not path.is_file():
        return ModelProviders()
    try:
        return loads_providers(path)
    except ProviderConfigError as exc:
        raise ConfigError(str(exc)) from exc


def write_providers(ctx: Ctx, providers: ModelProviders) -> Path:
    """原子写回 providers.json（0600），返回路径。"""
    try:
        return dumps_providers(providers, ctx.providers)
    except OSError as exc:
        raise ConfigError(f"写入 provider 配置失败: {ctx.providers}（{exc}）") from exc


def current_registry(ctx: Ctx) -> ProviderRegistry | None:
    """按 providers.json 构造 registry；一个 provider 都没有时返回 None。"""
    providers = read_providers(ctx)
    if not providers.providers:
        return None
    return ProviderRegistry(providers, source=Path(ctx.providers))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _check_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise ConfigError("provider 名不能为空")
    if "::" in text:
        raise ConfigError("provider 名里不能有 '::'（那是 Provider::model 的分隔符）")
    if any(char.isspace() for char in text):
        raise ConfigError(f"provider 名不能含空格: {text!r}")
    return text


def _check_base_url(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    if not text:
        raise ConfigError("base_url 不能为空")
    if not text.startswith(("http://", "https://")):
        raise ConfigError(f"base_url 必须以 http:// 或 https:// 开头: {text!r}")
    return text


def _check_models(models: Iterable[str]) -> list[str]:
    if isinstance(models, str):  # 单独传一个模型名也当列表
        models = [models]
    cleaned: list[str] = []
    for raw in models:
        for item in str(raw or "").replace(",", " ").split():
            name = item.strip()
            if name and name not in cleaned:
                cleaned.append(name)
    return cleaned


def _check_format(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    fmt = normalize_format(str(raw))
    if fmt is None:
        raise ConfigError(f"未知 api 格式: {raw!r}（可用: {', '.join(SUPPORTED_FORMATS)}）")
    return fmt


def _check_context(raw: Any) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).replace(",", "").replace("_", "").strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise ConfigError(f"上下文窗口必须是整数: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"上下文窗口必须大于 0: {value}")
    return value


def _check_thinking(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip().lower()
    if text not in PROVIDER_THINKING_DIALECTS:
        raise ConfigError(
            f"未知思考方言: {raw!r}（可用: {', '.join(PROVIDER_THINKING_DIALECTS)}）"
        )
    return text


def _ensure_files(ctx: Ctx) -> None:
    """写之前保证两个配置文件都在：新机器上一条命令就拿到完整骨架。"""
    try:
        ensure_layout(ctx)
    except OSError as exc:
        raise ConfigError(f"配置目录创建失败: {exc}") from exc


def _fallback_main_model(providers: ModelProviders) -> str | None:
    for provider in providers.providers:
        if provider.models:
            return f"{provider.name}::{provider.models[0]}"
    return None


# ---------------------------------------------------------------------------
# providers：查询
# ---------------------------------------------------------------------------


def providers_list(ctx: Ctx) -> int:
    providers = read_providers(ctx)
    if ctx.json_mode:
        ctx.emit_json(
            {
                "file": str(ctx.providers),
                "main_model": providers.main_model,
                "providers": [
                    {
                        "name": item.name,
                        "base_url": item.base_url,
                        "has_key": bool(item.key),
                        "models": list(item.models),
                        "api_format": item.api_format,
                        "thinking_type": item.thinking_type,
                    }
                    for item in providers.providers
                ],
            }
        )
        return 0
    if not providers.providers:
        _warn(ctx, f"{ctx.providers} 里还没有 provider")
        ctx.out("  新增: liorium-cli setup providers add <名字> --base-url <URL> --model <模型>")
        ctx.out("  或启动 liorium-cli 后执行 /setup")
        return 0
    ctx.out(f"providers.json: {ctx.providers}")
    for provider in providers.providers:
        marker = (
            "→"
            if providers.main_model
            and providers.main_model.startswith(f"{provider.name}::")
            else " "
        )
        key_state = "有 key" if provider.key else "无 key"
        ctx.out(
            f" {marker} {provider.name.ljust(14)}{provider.base_url.ljust(42)}"
            f"{_fmt_model_count(provider)}  {key_state}"
        )
    ctx.out(f"  默认模型: {providers.main_model or '（未设置）'}")
    ctx.out("  详情: setup providers show <名字>；新增: setup providers add …")
    return 0


def providers_show(ctx: Ctx, name: str, *, reveal_key: bool = False) -> int:
    providers = read_providers(ctx)
    provider = providers.find(_check_name(name))
    if provider is None:
        raise ConfigError(
            f"没有这个 provider: {name!r}（可用: {_provider_options(providers)}）"
        )
    is_default = bool(
        providers.main_model and providers.main_model.startswith(f"{provider.name}::")
    )
    if ctx.json_mode:
        ctx.emit_json(
            {
                "name": provider.name,
                "base_url": provider.base_url,
                "key": provider.key if reveal_key else cfg.mask_secret(provider.key),
                "has_key": bool(provider.key),
                "models": list(provider.models),
                "model_context_lengths": dict(provider.model_context_lengths),
                "model_api_formats": dict(provider.model_api_formats),
                "api_format": provider.api_format,
                "thinking_type": provider.thinking_type,
                "opencode_go": provider.opencode_go,
                "is_default": is_default,
                "file": str(ctx.providers),
            }
        )
        return 0
    _kv(
        ctx,
        [
            ("名字", provider.name),
            ("base_url", provider.base_url),
            (
                "key",
                (provider.key or "(未设置)") if reveal_key else cfg.mask_secret(provider.key),
            ),
            ("api 格式", provider.api_format),
            ("思考方言", provider.thinking_type),
            ("opencode_go", "是" if provider.opencode_go else "否"),
            ("默认模型", "是" if is_default else "否"),
        ],
    )
    ctx.out("  模型:")
    if not provider.models:
        ctx.out(
            f"    （还没有模型：setup providers edit {provider.name} --model <模型>）"
        )
    for model in provider.models:
        window = provider.model_context_lengths.get(model)
        fmt = provider.model_api_formats.get(model)
        extra = "  ".join(
            part
            for part in (
                f"{window:,} tokens" if isinstance(window, int) else "",
                fmt or "",
            )
            if part
        )
        ctx.out(f"    {model.ljust(36)}{extra}")
    return 0


# ---------------------------------------------------------------------------
# providers：增删改
# ---------------------------------------------------------------------------


def providers_add(
    ctx: Ctx,
    name: str,
    *,
    base_url: str,
    key: str | None = None,
    models: Sequence[str] = (),
    api_format: str | None = None,
    context: Any = None,
    thinking: str | None = None,
    make_default: bool = False,
) -> int:
    _ensure_files(ctx)
    providers = read_providers(ctx)
    provider_name = _check_name(name)
    if providers.find(provider_name) is not None:
        raise ConfigError(
            f"provider {provider_name!r} 已存在（用 setup providers edit {provider_name} 修改）"
        )
    fmt = _check_format(api_format)
    window = _check_context(context)
    dialect = _check_thinking(thinking)
    model_names = _check_models(models)

    provider = ModelProvider(
        name=provider_name,
        base_url=_check_base_url(base_url),
        key=(key or None),
        models=model_names,
        model_context_lengths={model: window for model in model_names} if window else {},
        model_api_formats={model: fmt for model in model_names} if fmt else {},
        api_format=fmt or "auto",
        thinking_type=dialect or "auto",
    )
    providers.providers.append(provider)
    first_provider = len(providers.providers) == 1
    if make_default or first_provider or not providers.main_model:
        providers.main_model = (
            f"{provider_name}::{model_names[0]}" if model_names else None
        )
    write_providers(ctx, providers)

    if ctx.json_mode:
        ctx.emit_json(
            {
                "added": provider_name,
                "file": str(ctx.providers),
                "main_model": providers.main_model,
            }
        )
        return 0
    _ok(ctx, f"已写入 {ctx.providers}：provider {provider_name}")
    if not key:
        _warn(ctx, f"{provider_name} 还没有 API key：setup providers set-key {provider_name}")
    if not model_names:
        _warn(
            ctx,
            f"{provider_name} 还没有模型：setup providers edit {provider_name} --model <模型>",
        )
    if providers.main_model:
        ctx.out(f"  默认模型: {providers.main_model}")
    return 0


def providers_edit(
    ctx: Ctx,
    name: str,
    *,
    base_url: str | None = None,
    key: str | None = None,
    models: Sequence[str] = (),
    remove_models: Sequence[str] = (),
    clear_models: bool = False,
    api_format: str | None = None,
    context: Any = None,
    thinking: str | None = None,
    opencode_go: bool | None = None,
) -> int:
    _ensure_files(ctx)
    providers = read_providers(ctx)
    provider = providers.find(_check_name(name))
    if provider is None:
        raise ConfigError(
            f"没有这个 provider: {name!r}（可用: {_provider_options(providers)}）"
        )

    changes: list[str] = []
    if base_url is not None:
        provider.base_url = _check_base_url(base_url)
        changes.append("base_url")
    if key is not None:
        provider.key = key or None
        changes.append("key")
    if clear_models:
        provider.models = []
        changes.append("models(清空)")
    for model in _check_models(models):
        if model not in provider.models:
            provider.models.append(model)
            changes.append(f"+model {model}")
    for model in _check_models(remove_models):
        if model in provider.models:
            provider.models.remove(model)
            provider.model_context_lengths.pop(model, None)
            provider.model_api_formats.pop(model, None)
            changes.append(f"-model {model}")
    if api_format is not None:
        fmt = _check_format(api_format)
        provider.api_format = fmt or "auto"
        if fmt:
            for model in provider.models:
                provider.model_api_formats[model] = fmt
        changes.append("api 格式")
    window = _check_context(context)
    if window:
        for model in provider.models:
            provider.model_context_lengths[model] = window
        changes.append(f"上下文 {window:,}")
    dialect = _check_thinking(thinking)
    if dialect:
        provider.thinking_type = dialect
        changes.append("思考方言")
    if opencode_go is not None:
        provider.opencode_go = bool(opencode_go)
        changes.append("opencode_go")

    if not changes:
        _warn(
            ctx,
            "没有给任何修改项（--base-url / --key / --model / --rm-model / --api-format …）",
        )
        return 0

    if providers.main_model and not any(
        providers.main_model.startswith(f"{p.name}::") for p in providers.providers
    ):
        providers.main_model = _fallback_main_model(providers)
    write_providers(ctx, providers)
    if ctx.json_mode:
        ctx.emit_json({"edited": provider.name, "changes": changes})
        return 0
    _ok(ctx, f"已更新 {provider.name}: {', '.join(changes)}")
    return 0


def providers_remove(ctx: Ctx, name: str) -> int:
    _ensure_files(ctx)
    providers = read_providers(ctx)
    target = providers.find(_check_name(name))
    if target is None:
        raise ConfigError(
            f"没有这个 provider: {name!r}（可用: {_provider_options(providers)}）"
        )
    providers.providers = [p for p in providers.providers if p.name != target.name]
    if providers.main_model and providers.main_model.startswith(f"{target.name}::"):
        providers.main_model = _fallback_main_model(providers)
    write_providers(ctx, providers)
    if ctx.json_mode:
        ctx.emit_json(
            {
                "removed": target.name,
                "main_model": providers.main_model,
                "file": str(ctx.providers),
            }
        )
        return 0
    _ok(ctx, f"已删除 provider {target.name}")
    if providers.main_model:
        ctx.out(f"  默认模型改为: {providers.main_model}")
    else:
        _warn(ctx, "现在没有任何可用模型（providers.json 是空的）")
    return 0


def providers_default(ctx: Ctx, label: str | None = None) -> int:
    _ensure_files(ctx)
    providers = read_providers(ctx)
    if not providers.providers:
        raise ConfigError("providers.json 里还没有 provider，先 setup providers add …")
    if label is None:
        if ctx.json_mode:
            ctx.emit_json({"main_model": providers.main_model, "labels": _all_labels(providers)})
        else:
            ctx.out(f"默认模型: {providers.main_model or '（未设置）'}")
            ctx.out(f"  可设: {', '.join(_all_labels(providers)) or '（没有模型）'}")
        return 0

    text = label.strip()
    if "::" in text:
        provider_name, model = text.split("::", 1)
        provider = providers.find(_check_name(provider_name))
        if provider is None:
            raise ConfigError(
                f"没有这个 provider: {provider_name.strip()!r}"
                f"（可用: {_provider_options(providers)}）"
            )
        if provider.models and model.strip() not in provider.models:
            raise ConfigError(
                f"{provider.name} 没有模型 {model.strip()!r}（可用: {', '.join(provider.models)}）"
            )
        chosen = f"{provider.name}::{model.strip()}"
    else:
        matches = [p for p in providers.providers if text in p.models]
        if not matches:
            raise ConfigError(
                f"找不到模型 {text!r}；用 Provider::model 形式，或 setup providers list"
            )
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise ConfigError(
                f"模型 {text!r} 在多个 provider 里都有（{names}），请写 Provider::model"
            )
        chosen = f"{matches[0].name}::{text}"

    providers.main_model = chosen
    write_providers(ctx, providers)
    if ctx.json_mode:
        ctx.emit_json({"main_model": chosen})
        return 0
    _ok(ctx, f"默认模型已设为 {chosen}")
    return 0


def providers_test(ctx: Ctx, label: str, *, network: bool = False) -> int:
    registry = current_registry(ctx)
    if registry is None:
        raise ConfigError("providers.json 里还没有 provider，先 setup providers add …")
    resolved = registry.resolve(label)
    rows: list[tuple[str, str]] = [
        ("模型", resolved.label),
        ("格式", f"{resolved.api_format}（来源 {resolved.format_source}）"),
        ("base_url", resolved.base_url),
        ("上下文", f"{resolved.context_window:,} tokens" if resolved.context_window else "未知"),
        ("输出上限", f"{resolved.max_output_tokens:,} tokens"),
        ("key", cfg.mask_secret(resolved.provider.api_key)),
    ]
    ok = True
    detail = ""
    if network:
        ok, detail = _probe_models(resolved)
    if ctx.json_mode:
        ctx.emit_json(
            {
                "label": resolved.label,
                "ok": ok,
                "api_format": resolved.api_format,
                "format_source": resolved.format_source,
                "base_url": resolved.base_url,
                "context_window": resolved.context_window,
                "max_output_tokens": resolved.max_output_tokens,
                "has_key": bool(resolved.provider.api_key),
                "network": {"checked": network, "ok": ok, "detail": detail},
            }
        )
        return 0 if ok else 1
    _kv(ctx, rows)
    if not resolved.provider.api_key:
        _fail(ctx, "没有 API key：setup providers set-key " + resolved.provider.name)
        return 1
    if network:
        if ok:
            _ok(ctx, detail)
        else:
            _fail(ctx, detail)
            return 1
    else:
        _ok(ctx, "本地解析通过（未联网；加 --network 请求一次 /models）")
    return 0


def _probe_models(resolved: Any) -> tuple[bool, str]:
    """``GET {base_url}/models``（只对 OpenAI 兼容格式有意义）。"""
    if not str(resolved.api_format).startswith("openai"):
        return True, f"跳过：{resolved.api_format} 的 /models 需要专用鉴权"
    try:
        import httpx
    except ImportError:
        return False, "没装 httpx，跳过网络检查"
    url = str(resolved.base_url).rstrip("/") + "/models"
    headers = {}
    if resolved.provider.api_key:
        headers["Authorization"] = f"Bearer {resolved.provider.api_key}"
    try:
        response = httpx.get(url, headers=headers, timeout=8.0)
    except Exception as exc:  # noqa: BLE001 - 网络错误原样展示
        return False, f"{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    try:
        payload = response.json()
    except ValueError:
        return True, "HTTP 200（返回不是 JSON）"
    data = payload.get("data") if isinstance(payload, dict) else None
    count = len(data) if isinstance(data, list) else 0
    return True, f"HTTP 200，{count} 个模型"


def providers_set_key(ctx: Ctx, name: str, key: str | None = None) -> int:
    _ensure_files(ctx)
    providers = read_providers(ctx)
    provider = providers.find(_check_name(name))
    if provider is None:
        raise ConfigError(
            f"没有这个 provider: {name!r}（可用: {_provider_options(providers)}）"
        )
    if key is None:
        key = _secret_input(ctx, f"{provider.name} 的 API key: ").strip()
    if not key or not str(key).strip():
        raise ConfigError("API key 不能为空")
    provider.key = str(key).strip()
    write_providers(ctx, providers)
    if ctx.json_mode:
        ctx.emit_json({"name": provider.name, "key": cfg.mask_secret(provider.key)})
        return 0
    _ok(ctx, f"已更新 {provider.name} 的 API key: {cfg.mask_secret(provider.key)}")
    return 0


# ---------------------------------------------------------------------------
# 记忆系统
# ---------------------------------------------------------------------------


def memory_dir_of(config: Mapping[str, Any]) -> Path | None:
    raw = cfg.get_value(config, "memory.dir")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def effective_inbox(
    config: Mapping[str, Any],
    *,
    workspace: Path | None = None,
    default: Path | None = None,
) -> Path:
    raw = cfg.get_value(config, "inbox")
    if raw:
        return Path(str(raw)).expanduser()
    if default is not None:
        return Path(default)
    root = workspace or Path.cwd()
    return Path(root) / ".liorium-cli" / "agent"


def effective_memory_dir(
    config: Mapping[str, Any], *, inbox: Path | None = None
) -> Path:
    configured = memory_dir_of(config)
    if configured is not None:
        return configured
    return effective_inbox(config) / "memory" if inbox is None else inbox / "memory"


def effective_skills_dir(
    config: Mapping[str, Any], *, inbox: Path | None = None
) -> Path:
    raw = cfg.get_value(config, "skills.dir")
    if raw:
        return Path(str(raw)).expanduser()
    return effective_inbox(config) / "skill.d" if inbox is None else inbox / "skill.d"


def _inbox_of(ctx: Ctx, config: Mapping[str, Any]) -> Path:
    """生效的 Agent 根目录：宿主给了就用宿主的（会话内 /setup），否则按配置/当前目录推。"""
    return effective_inbox(config, default=ctx.inbox)


def memory_dir_for(ctx: Ctx, config: Mapping[str, Any]) -> Path:
    return effective_memory_dir(config, inbox=_inbox_of(ctx, config))


def skills_dir_for(ctx: Ctx, config: Mapping[str, Any]) -> Path:
    return effective_skills_dir(config, inbox=_inbox_of(ctx, config))


def memory_show(ctx: Ctx) -> int:
    config = ctx.load_config()
    directory = memory_dir_for(ctx, config)
    configured = memory_dir_of(config)
    store = directory / "memory.sqlite"
    payload = {
        "enabled": bool(cfg.get_value(config, "memory.enabled", True)),
        "dir": str(directory),
        "dir_configured": str(configured) if configured else None,
        "store_exists": store.is_file(),
        "store_bytes": store.stat().st_size if store.is_file() else 0,
        "embed_model": cfg.get_value(config, "memory.embed_model", "") or "",
        "embed_base_url": cfg.get_value(config, "memory.embed_base_url", "") or "",
        "embed_key": cfg.mask_secret(cfg.get_value(config, "memory.embed_key", "")),
        "vectors": bool(cfg.get_value(config, "memory.vectors", True)),
        "extraction": bool(cfg.get_value(config, "memory.extraction", True)),
        "file": str(ctx.config_file),
    }
    if ctx.json_mode:
        ctx.emit_json(payload)
        return 0
    _kv(
        ctx,
        [
            ("开关", "开" if payload["enabled"] else "关"),
            ("目录", payload["dir"] + ("（默认）" if configured is None else "（已配置）")),
            ("记忆库", f"{store}（{'已存在' if payload['store_exists'] else '还没建'}）"),
            ("embed", payload["embed_model"] or "（未设置：只跑关键词检索）"),
            ("embed 地址", payload["embed_base_url"] or "（跟随 provider）"),
            ("embed key", payload["embed_key"]),
            ("向量索引", "开" if payload["vectors"] else "关"),
            ("实体抽取", "开" if payload["extraction"] else "关"),
        ],
    )
    ctx.out("  用法: setup memory set dir <路径> / set embed-model <模型> / enable|disable")
    return 0


def memory_set(ctx: Ctx, key: str, value: str) -> int:
    _ensure_files(ctx)
    full = MEMORY_SHORTCUTS.get(str(key).strip().lower(), str(key).strip())
    if not full.startswith("memory."):
        raise ConfigError(
            "用法: setup memory set <dir|embed-model|embed-base-url|embed-key"
            "|vectors|extraction|enabled> <值>"
        )
    config = ctx.load_config()
    parsed = cfg.set_value(config, full, value)
    ctx.save_config(config)
    if ctx.json_mode:
        ctx.emit_json({full: parsed})
        return 0
    _ok(ctx, f"{full} = {cfg.describe_value(full, parsed)}")
    return 0


def memory_enable(ctx: Ctx, enabled: bool = True) -> int:
    _ensure_files(ctx)
    config = ctx.load_config()
    cfg.set_value(config, "memory.enabled", enabled)
    ctx.save_config(config)
    if ctx.json_mode:
        ctx.emit_json({"memory.enabled": enabled})
        return 0
    _ok(ctx, "记忆已启用" if enabled else "记忆已停用")
    return 0


def memory_path(ctx: Ctx) -> int:
    directory = memory_dir_for(ctx, ctx.load_config())
    if ctx.json_mode:
        ctx.emit_json({"dir": str(directory)})
        return 0
    ctx.out(str(directory))
    return 0


def memory_init(ctx: Ctx, directory: str | Path | None = None) -> int:
    _ensure_files(ctx)
    config = ctx.load_config()
    if directory is not None:
        cfg.set_value(config, "memory.dir", str(Path(directory).expanduser()))
        ctx.save_config(config)
    target = memory_dir_for(ctx, config)
    target.mkdir(parents=True, exist_ok=True)
    (target / "content").mkdir(parents=True, exist_ok=True)
    store_state = _init_store(target)
    if ctx.json_mode:
        ctx.emit_json({"dir": str(target), "store": store_state})
        return 0
    _ok(ctx, f"记忆目录已就绪: {target}")
    ctx.out(f"  {store_state}")
    return 0


def _init_store(directory: Path) -> str:
    """尽力把 ``memory.sqlite`` 建出来；缺 [vector] 之类的依赖不影响目录创建。"""
    store = Path(directory) / "memory.sqlite"
    if store.is_file():
        return f"记忆库已存在: {store}"
    try:
        from liorium.memory.store import SqliteMemoryStore

        instance = SqliteMemoryStore(store)
        instance.close()
    except Exception as exc:  # noqa: BLE001 - 初始化失败要让用户看到原因
        return f"记忆库暂未初始化（{type(exc).__name__}: {exc}）；首次写入时会自动建"
    return f"已初始化记忆库: {store}"


# ---------------------------------------------------------------------------
# 总览 / 体检
# ---------------------------------------------------------------------------


def overview(ctx: Ctx) -> dict[str, Any]:
    config = ctx.load_config()
    providers = read_providers(ctx)
    inbox = _inbox_of(ctx, config)
    memory = effective_memory_dir(config, inbox=inbox)
    skills = effective_skills_dir(config, inbox=inbox)
    registry: ProviderRegistry | None
    try:
        registry = current_registry(ctx)
    except ConfigError:
        registry = None
    model = cfg.get_value(config, "model") or providers.main_model
    if not model and providers.providers:
        model = _fallback_main_model(providers)
    return {
        "paths": {
            "home": str(cfg.home_dir()),
            "providers": str(ctx.providers),
            "config": str(ctx.config_file),
            "cache": str(cfg.cache_dir()),
            "inbox": str(inbox),
            "memory": str(memory),
            "skills": str(skills),
        },
        "files": {
            "providers_exists": Path(ctx.providers).is_file(),
            "config_exists": Path(ctx.config_file).is_file(),
            "memory_store_exists": (memory / "memory.sqlite").is_file(),
        },
        "model": model,
        "usable": registry is not None,
        "providers": {
            "count": len(providers.providers),
            "names": [p.name for p in providers.providers],
            "main_model": providers.main_model,
        },
        "config": {
            key: cfg.describe_value(key, cfg.get_value(config, key)) for key in cfg.KEYS
        },
    }


def print_overview(ctx: Ctx) -> int:
    data = overview(ctx)
    if ctx.json_mode:
        ctx.emit_json(data)
        return 0
    paths = data["paths"]
    ctx.out("路径")
    _kv(
        ctx,
        [
            ("home", paths["home"]),
            ("providers", paths["providers"]),
            ("config", paths["config"]),
            ("cache", paths["cache"]),
            ("inbox", paths["inbox"]),
            ("memory", paths["memory"]),
            ("skills", paths["skills"]),
        ],
    )
    ctx.out("")
    ctx.out("provider")
    providers = data["providers"]
    if not providers["count"]:
        _warn(ctx, "还没有 provider（setup providers add … 或 /setup）")
    else:
        _kv(
            ctx,
            [
                ("数量", f"{providers['count']}（{', '.join(providers['names'])}）"),
                ("默认模型", providers["main_model"] or "（未设置）"),
                ("当前模型", str(data["model"] or "（无）")),
            ],
        )
    ctx.out("")
    ctx.out("config.json")
    for key, value in data["config"].items():
        ctx.out(f"  {key.ljust(24)}{value}")
    ctx.out("")
    ctx.out("  设置: setup set <键> <值>；交互式: setup（无子命令）或 CLI 里 /setup")
    return 0


@dataclass
class Check:
    level: str
    """ok / warn / error。"""
    name: str
    detail: str


def run_checks(ctx: Ctx) -> list[Check]:
    checks: list[Check] = []

    home = cfg.home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(home), prefix=".probe-", delete=True):
            pass
        checks.append(Check("ok", "home 可写", str(home)))
    except OSError as exc:
        checks.append(Check("error", "home 不可写", f"{home}（{exc}）"))

    try:
        config = ctx.load_config()
        checks.append(Check("ok", "config.json", str(ctx.config_file)))
    except ConfigError as exc:
        config = cfg.default_config()
        checks.append(Check("error", "config.json 非法", str(exc)))

    try:
        providers = read_providers(ctx)
        checks.append(
            Check("ok", "providers.json", f"{ctx.providers}（{len(providers.providers)} 个）")
        )
    except ConfigError as exc:
        providers = ModelProviders()
        checks.append(Check("error", "providers.json 非法", str(exc)))

    if not providers.providers:
        checks.append(
            Check("warn", "没有 provider", "setup providers add <名字> --base-url <URL> --model <模型>")
        )
    else:
        try:
            registry = ProviderRegistry(providers)
            label = registry.default_label()
            resolved = registry.resolve(label)
            checks.append(Check("ok", "默认模型", resolved.describe()))
            if resolved.provider.api_key:
                checks.append(Check("ok", "API key", "已配置"))
            else:
                checks.append(
                    Check(
                        "warn",
                        "默认 provider 没有 API key",
                        f"setup providers set-key {resolved.provider.name}",
                    )
                )
        except ProviderConfigError as exc:
            checks.append(Check("warn", "默认模型不可用", str(exc)))

    configured_memory = cfg.get_value(config, "memory.dir")
    memory = memory_dir_for(ctx, config)
    if configured_memory:
        try:
            memory.mkdir(parents=True, exist_ok=True)
            (memory / "content").mkdir(parents=True, exist_ok=True)
            checks.append(Check("ok", "记忆目录可写", str(memory)))
        except OSError as exc:
            checks.append(Check("warn", "记忆目录不可写", f"{memory}（{exc}）"))
    else:
        checks.append(
            Check("ok", "记忆目录", f"{memory}（默认位置，首次写入时自动创建）")
        )

    skills = skills_dir_for(ctx, config)
    checks.append(
        Check(
            "ok" if skills.is_dir() else "warn",
            "技能目录",
            str(skills) if skills.is_dir() else f"{skills}（还没建；放 <slug>/SKILL.md 进去）",
        )
    )

    for module, label in (
        ("langchain_openai", "openai 格式"),
        ("langchain_anthropic", "anthropic 格式"),
        ("langchain_google_genai", "google 格式"),
    ):
        try:
            __import__(module)
            checks.append(Check("ok", f"依赖 {label}", module))
        except Exception as exc:  # noqa: BLE001 - 缺 extra 只算提示
            checks.append(
                Check("warn", f"依赖 {label} 不可用", f"{module}（{type(exc).__name__}）")
            )
    return checks


def doctor(ctx: Ctx) -> int:
    checks = run_checks(ctx)
    if ctx.json_mode:
        ctx.emit_json(
            {
                "checks": [
                    {"level": item.level, "name": item.name, "detail": item.detail}
                    for item in checks
                ]
            }
        )
    else:
        marks = {"ok": "✓", "warn": "!", "error": "✗"}
        for item in checks:
            ctx.out(f" {marks.get(item.level, '?')} {_pad(item.name, 22)}{item.detail}")
    return 1 if any(item.level == "error" for item in checks) else 0


# ---------------------------------------------------------------------------
# 骨架
# ---------------------------------------------------------------------------


def ensure_layout(ctx: Ctx) -> dict[str, Any]:
    """建目录与空文件（已存在的不动）。

    只碰 ``~/.liorium``：记忆库 / 技能目录由会话启动或 ``setup memory init`` 负责，
    这里顺手在用户 cwd 里建目录会让人莫名其妙。
    """
    created: dict[str, Any] = {
        "home": None,
        "config": None,
        "providers": None,
        "cache": None,
    }
    home_existed = cfg.home_dir().is_dir()
    Path(ctx.config_file).parent.mkdir(parents=True, exist_ok=True)
    if not home_existed:
        created["home"] = cfg.home_dir()
    cache = cfg.cache_dir()
    if not cache.is_dir():
        cache.mkdir(parents=True, exist_ok=True)
        created["cache"] = cache
    if not Path(ctx.config_file).is_file():
        created["config"] = ctx.save_config(cfg.default_config())
    if not Path(ctx.providers).is_file():
        created["providers"] = cfg.write_json(ctx.providers, {"providers": []})
    return created


def setup_init(ctx: Ctx) -> int:
    created = ensure_layout(ctx)
    if ctx.json_mode:
        ctx.emit_json({key: (str(value) if value else None) for key, value in created.items()})
        return 0
    for key, value in created.items():
        if value:
            _ok(ctx, f"{key}: 已创建 {value}")
    if not any(created.values()):
        ctx.out("配置骨架已存在，没有改动")
    ctx.out(f"  providers: {ctx.providers}")
    ctx.out(f"  config   : {ctx.config_file}")
    return 0


# ---------------------------------------------------------------------------
# 交互式
# ---------------------------------------------------------------------------


@dataclass
class WizardResult:
    """交互式改了什么（会话据此决定要不要热切换模型 / 重建记忆 realm）。"""

    providers_changed: bool = False
    provider_label: str | None = None
    memory_changed: bool = False
    memory_enabled: bool | None = None
    """None = 开关没改；True/False = 新状态。"""
    memory_embed_changed: bool = False
    memory_dir_changed: bool = False


def _merge_result(target: WizardResult, part: WizardResult) -> WizardResult:
    target.providers_changed = target.providers_changed or part.providers_changed
    target.provider_label = part.provider_label or target.provider_label
    target.memory_changed = target.memory_changed or part.memory_changed
    if part.memory_enabled is not None:
        target.memory_enabled = part.memory_enabled
    target.memory_embed_changed = target.memory_embed_changed or part.memory_embed_changed
    target.memory_dir_changed = target.memory_dir_changed or part.memory_dir_changed
    return target


def _print_header(ctx: Ctx) -> None:
    try:
        config = ctx.load_config()
        providers = read_providers(ctx)
    except ConfigError as exc:
        _fail(ctx, str(exc))
        return
    memory_on = "开" if cfg.get_value(config, "memory.enabled", True) else "关"
    ctx.out("")
    ctx.out("Liorium 配置")
    ctx.out(f"  provider  {len(providers.providers)} 个  默认 {providers.main_model or '（未设置）'}")
    ctx.out(f"  记忆      {memory_on}  {memory_dir_for(ctx, config)}")
    ctx.out(f"  文件      {ctx.providers}")
    ctx.out(f"            {ctx.config_file}")


def wizard(ctx: Ctx, topic: str | None = None) -> WizardResult:
    """顶层菜单；``topic`` 给定时直接进某个子向导。"""
    result = WizardResult()
    try:
        ensure_layout(ctx)  # 第一次运行就把空骨架建出来（已存在的不动）
    except OSError as exc:
        _fail(ctx, f"配置目录创建失败: {exc}")
    key = (topic or "").strip().lower()
    if key in ("provider", "providers"):
        return wizard_providers(ctx)
    if key in ("memory", "mem"):
        return wizard_memory(ctx)
    while True:
        _print_header(ctx)
        ctx.out("  1) provider  2) 记忆系统  3) 查看全部  4) 体检  5) 建空配置  0) 完成")
        choice = ctx.ask("选择 [1]: ").strip().lower() or "0"
        try:
            if choice in ("0", "q", "quit", "exit", "done"):
                return result
            if choice in ("1", "p", "provider", "providers"):
                _merge_result(result, wizard_providers(ctx))
            elif choice in ("2", "m", "memory"):
                _merge_result(result, wizard_memory(ctx))
            elif choice in ("3", "s", "show"):
                print_overview(ctx)
            elif choice in ("4", "d", "doctor"):
                doctor(ctx)
            elif choice in ("5", "init"):
                setup_init(ctx)
            else:
                _warn(ctx, f"没有这个选项: {choice!r}")
        except (ConfigError, ProviderConfigError) as exc:
            _fail(ctx, str(exc))


def _print_providers_short(ctx: Ctx, providers: ModelProviders) -> None:
    if not providers.providers:
        ctx.out("  （还没有 provider）")
        return
    for index, provider in enumerate(providers.providers, 1):
        mark = "→" if providers.main_model and providers.main_model.startswith(
            f"{provider.name}::"
        ) else " "
        ctx.out(
            f"  {mark}{index}) {provider.name.ljust(12)}{provider.base_url.ljust(38)}"
            f"{_fmt_model_count(provider)}  {'有 key' if provider.key else '无 key'}"
        )
    ctx.out(f"   默认模型: {providers.main_model or '（未设置）'}")


def wizard_providers(ctx: Ctx) -> WizardResult:
    result = WizardResult()
    while True:
        providers = read_providers(ctx)
        ctx.out("")
        ctx.out(f"provider 配置  {ctx.providers}")
        _print_providers_short(ctx, providers)
        ctx.out("  [a]新增 [e]编辑 [d]删除 [s]设为默认 [t]测试 [k]改 key [Enter]返回")
        choice = ctx.ask("操作: ").strip().lower()
        if choice in ("", "b", "back", "q", "quit"):
            result.provider_label = read_providers(ctx).main_model
            return result
        try:
            if choice in ("a", "add"):
                _wizard_add_provider(ctx)
                result.providers_changed = True
            elif choice in ("e", "edit"):
                _wizard_edit_provider(ctx)
                result.providers_changed = True
            elif choice in ("d", "delete", "rm"):
                if _wizard_remove_provider(ctx):
                    result.providers_changed = True
            elif choice in ("s", "default"):
                label = ctx.ask("设为默认的模型（Provider::model 或模型名）: ").strip()
                if label:
                    providers_default(ctx, label)
                    result.providers_changed = True
            elif choice in ("t", "test"):
                label = ctx.ask("测试哪个模型（Provider::model）: ").strip()
                if label:
                    network = ctx.ask("联网请求 /models？[y/N]: ").strip().lower() in (
                        "y",
                        "yes",
                    )
                    providers_test(ctx, label, network=network)
            elif choice in ("k", "key"):
                name = ctx.ask("哪个 provider: ").strip()
                key = _secret_input(ctx, f"{name} 的 API key: ").strip()
                if name and key:
                    providers_set_key(ctx, name, key)
                    result.providers_changed = True
            else:
                _warn(ctx, f"没有这个选项: {choice!r}")
        except (ConfigError, ProviderConfigError) as exc:
            _fail(ctx, str(exc))
        result.provider_label = read_providers(ctx).main_model


def _wizard_add_provider(ctx: Ctx) -> None:
    name = ctx.ask("provider 名（例如 deepseek / openrouter / local）: ").strip()
    base_url = ctx.ask(f"base_url [{DEFAULT_BASE_URL}]: ").strip() or DEFAULT_BASE_URL
    key = _secret_input(ctx, "API key（可留空，之后再填）: ").strip()
    models = ctx.ask("模型名（逗号分隔，例如 deepseek-chat, deepseek-reasoner）: ").strip()
    fmt = ctx.ask(f"api 格式（{'/'.join(SUPPORTED_FORMATS)}）[自动]: ").strip()
    window = ctx.ask(f"上下文窗口（tokens）[{DEFAULT_CONTEXT}]: ").strip()
    dialect = ctx.ask(f"思考方言（{'/'.join(PROVIDER_THINKING_DIALECTS)}）[auto]: ").strip()
    make_default = ctx.ask("设为默认模型？[Y/n]: ").strip().lower() not in ("n", "no")
    providers_add(
        ctx,
        name,
        base_url=base_url,
        key=key or None,
        models=[models] if models else [],
        api_format=fmt or None,
        context=window or None,
        thinking=dialect or None,
        make_default=make_default,
    )


def _wizard_edit_provider(ctx: Ctx) -> None:
    name = ctx.ask("要编辑哪个 provider: ").strip()
    providers = read_providers(ctx)
    provider = providers.find(_check_name(name))
    if provider is None:
        raise ConfigError(
            f"没有这个 provider: {name!r}（可用: {_provider_options(providers)}）"
        )
    providers_show(ctx, provider.name)
    ctx.out(
        "  1) base_url  2) API key  3) 追加模型  4) 删除模型  5) api 格式"
        "  6) 上下文  7) 思考方言  8) opencode_go  0) 返回"
    )
    while True:
        choice = ctx.ask("改哪一项 [0]: ").strip() or "0"
        if choice in ("0", "q", ""):
            return
        if choice == "1":
            providers_edit(ctx, provider.name, base_url=ctx.ask("新 base_url: ").strip())
        elif choice == "2":
            key = _secret_input(ctx, "新 API key: ").strip()
            if key:
                providers_edit(ctx, provider.name, key=key)
        elif choice == "3":
            models = ctx.ask("追加模型（逗号分隔）: ").strip()
            if models:
                providers_edit(ctx, provider.name, models=[models])
        elif choice == "4":
            models = ctx.ask("删除模型（逗号分隔）: ").strip()
            if models:
                providers_edit(ctx, provider.name, remove_models=[models])
        elif choice == "5":
            fmt = ctx.ask(f"api 格式（{'/'.join(SUPPORTED_FORMATS)}）: ").strip()
            if fmt:
                providers_edit(ctx, provider.name, api_format=fmt)
        elif choice == "6":
            window = ctx.ask("上下文窗口（tokens）: ").strip()
            if window:
                providers_edit(ctx, provider.name, context=window)
        elif choice == "7":
            dialect = ctx.ask(
                f"思考方言（{'/'.join(PROVIDER_THINKING_DIALECTS)}）: "
            ).strip()
            if dialect:
                providers_edit(ctx, provider.name, thinking=dialect)
        elif choice == "8":
            raw = ctx.ask("启用 opencode_go 适配？[y/N]: ").strip().lower()
            providers_edit(ctx, provider.name, opencode_go=raw in ("y", "yes"))
        else:
            _warn(ctx, f"没有这个选项: {choice!r}")


def _wizard_remove_provider(ctx: Ctx) -> bool:
    name = ctx.ask("要删除哪个 provider: ").strip()
    if not name:
        return False
    answer = ctx.ask(f"确认删除 {name}？[y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        _warn(ctx, "已取消")
        return False
    providers_remove(ctx, name)
    return True


def _print_memory(ctx: Ctx, config: Mapping[str, Any]) -> None:
    enabled = cfg.get_value(config, "memory.enabled", True)
    directory = memory_dir_for(ctx, config)
    ctx.out("")
    ctx.out("记忆系统")
    _kv(
        ctx,
        [
            ("开关", "开" if enabled else "关"),
            ("目录", str(directory)),
            ("embed", str(cfg.get_value(config, "memory.embed_model", "") or "（未设置）")),
            ("向量索引", "开" if cfg.get_value(config, "memory.vectors", True) else "关"),
            ("实体抽取", "开" if cfg.get_value(config, "memory.extraction", True) else "关"),
        ],
    )
    ctx.out(
        "  1) 设置目录  2) embed 模型  3) embed base_url  4) embed key"
        "  5) 开/关记忆  6) 开/关向量  7) 开/关抽取  8) 初始化目录  0) 返回"
    )


def wizard_memory(ctx: Ctx) -> WizardResult:
    result = WizardResult()
    while True:
        config = ctx.load_config()
        _print_memory(ctx, config)
        choice = ctx.ask("操作 [0]: ").strip() or "0"
        if choice in ("0", "q", ""):
            return result
        try:
            if choice == "1":
                raw = ctx.ask("记忆库目录（留空 = 用默认 <inbox>/memory）: ").strip()
                if raw:
                    cfg.set_value(config, "memory.dir", raw)
                    ctx.save_config(config)
                    result.memory_dir_changed = True
                    result.memory_changed = True
                    _ok(ctx, f"memory.dir = {raw}")
                else:
                    cfg.unset_value(config, "memory.dir")
                    ctx.save_config(config)
                    result.memory_dir_changed = True
                    result.memory_changed = True
                    _ok(ctx, "memory.dir 已恢复默认")
            elif choice == "2":
                raw = ctx.ask("embedding 模型（留空 = 关闭语义检索）: ").strip()
                cfg.set_value(config, "memory.embed_model", raw)
                ctx.save_config(config)
                result.memory_embed_changed = True
                result.memory_changed = True
                _ok(ctx, f"memory.embed_model = {raw or '(空)'}")
            elif choice == "3":
                raw = ctx.ask("embedding base_url（留空 = 跟随 provider）: ").strip()
                cfg.set_value(config, "memory.embed_base_url", raw)
                ctx.save_config(config)
                result.memory_embed_changed = True
                result.memory_changed = True
                _ok(ctx, f"memory.embed_base_url = {raw or '(空)'}")
            elif choice == "4":
                raw = _secret_input(ctx, "embedding API key（留空 = 跟随 provider）: ").strip()
                cfg.set_value(config, "memory.embed_key", raw)
                ctx.save_config(config)
                result.memory_embed_changed = True
                result.memory_changed = True
                _ok(ctx, f"memory.embed_key = {cfg.mask_secret(raw)}")
            elif choice == "5":
                enabled = not bool(cfg.get_value(config, "memory.enabled", True))
                cfg.set_value(config, "memory.enabled", enabled)
                ctx.save_config(config)
                result.memory_enabled = enabled
                result.memory_changed = True
                _ok(ctx, "记忆已启用" if enabled else "记忆已停用")
            elif choice == "6":
                checked = not bool(cfg.get_value(config, "memory.vectors", True))
                cfg.set_value(config, "memory.vectors", checked)
                ctx.save_config(config)
                result.memory_embed_changed = True
                result.memory_changed = True
                _ok(ctx, "向量索引已开" if checked else "向量索引已关（只跑关键词）")
            elif choice == "7":
                checked = not bool(cfg.get_value(config, "memory.extraction", True))
                cfg.set_value(config, "memory.extraction", checked)
                ctx.save_config(config)
                result.memory_embed_changed = True
                result.memory_changed = True
                _ok(ctx, "实体抽取已开" if checked else "实体抽取已关")
            elif choice == "8":
                directory = ctx.ask(
                    f"初始化哪个目录 [{memory_dir_for(ctx, config)}]: "
                ).strip()
                memory_init(ctx, directory or None)
            else:
                _warn(ctx, f"没有这个选项: {choice!r}")
        except (ConfigError, OSError) as exc:
            _fail(ctx, str(exc))


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liorium-cli setup",
        description="配置 Liorium CLI：provider、记忆系统与本地设置（不联网）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  liorium-cli setup                        # 交互式菜单
  liorium-cli setup show                   # 看当前全部配置（密钥打码）
  liorium-cli setup providers list
  liorium-cli setup providers add deepseek --base-url https://api.deepseek.com \\
      --model deepseek-chat --api-format openai_chat --context 128000
  liorium-cli setup providers set-key deepseek
  liorium-cli setup providers default deepseek::deepseek-chat
  liorium-cli setup memory show
  liorium-cli setup memory set dir D:/data/liorium-memory
  liorium-cli setup memory set embed-model text-embedding-3-small
  liorium-cli setup doctor                 # 体检：文件 / 密钥 / 目录 / 依赖
  liorium-cli setup init                   # 只建空配置骨架

文件:
  ~/.liorium/providers.json   provider 与密钥（0600）
  ~/.liorium/config.json      非密钥设置（默认模型 / 思考 / 记忆 / 技能 / UI）
  LIORIUM_HOME                改配置根目录（多套配置 / 测试）
""",
    )
    parser.add_argument("--home", type=Path, default=None, help="配置根目录（默认 ~/.liorium）")
    parser.add_argument("--config-file", type=Path, default=None, help="config.json 路径")
    parser.add_argument("--providers", type=Path, default=None, help="providers.json 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON（脚本用）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="创建配置目录与空文件（已存在的不动）")
    sub.add_parser("path", help="打印各配置路径")
    sub.add_parser("show", help="查看全部配置（密钥打码）")
    sub.add_parser("doctor", help="体检：文件 / 密钥 / 目录 / 依赖")
    sub.add_parser("wizard", help="交互式配置菜单")

    get_p = sub.add_parser("get", help="读取配置键（不带键 = 全部）")
    get_p.add_argument("key", nargs="?")
    get_p.add_argument("--reveal", action="store_true", help="显示密钥原文")
    set_p = sub.add_parser("set", help="写配置键")
    set_p.add_argument("key")
    set_p.add_argument("value")
    unset_p = sub.add_parser("unset", help="恢复配置键的默认值")
    unset_p.add_argument("key")

    prov = sub.add_parser("providers", help="provider 管理")
    psub = prov.add_subparsers(dest="provider_command")

    psub.add_parser("list", help="列出全部 provider（默认）")
    show_p = psub.add_parser("show", help="查看一个 provider")
    show_p.add_argument("name")
    show_p.add_argument("--reveal-key", action="store_true", help="显示 key 原文")

    add_p = psub.add_parser("add", help="新增 provider")
    add_p.add_argument("name")
    add_p.add_argument("--base-url", dest="base_url", required=True, help="API base_url")
    add_p.add_argument("--key", default=None, help="API key（不传就留空，之后 set-key）")
    add_p.add_argument("--model", action="append", default=[], help="模型名（可重复 / 逗号分隔）")
    add_p.add_argument("--api-format", dest="api_format", default=None, help="api 格式（默认自动）")
    add_p.add_argument("--context", default=None, help="上下文窗口 tokens（应用到全部模型）")
    add_p.add_argument("--thinking", default=None, help="思考方言（默认 auto）")
    add_p.add_argument("--default", dest="make_default", action="store_true", help="设为默认模型")

    edit_p = psub.add_parser("edit", help="修改 provider")
    edit_p.add_argument("name")
    edit_p.add_argument("--base-url", dest="base_url", default=None)
    edit_p.add_argument("--key", default=None)
    edit_p.add_argument("--model", action="append", default=[], help="追加模型（可重复）")
    edit_p.add_argument("--rm-model", dest="rm_model", action="append", default=[], help="删除模型")
    edit_p.add_argument("--clear-models", dest="clear_models", action="store_true")
    edit_p.add_argument("--api-format", dest="api_format", default=None)
    edit_p.add_argument("--context", default=None)
    edit_p.add_argument("--thinking", default=None)
    edit_p.add_argument(
        "--opencode-go",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="OpenCode Go 订阅适配（--no-opencode-go 关掉）",
    )

    rm_p = psub.add_parser("rm", help="删除 provider")
    rm_p.add_argument("name")

    default_p = psub.add_parser("default", help="查看 / 设置默认模型")
    default_p.add_argument("label", nargs="?")

    test_p = psub.add_parser("test", help="校验一个模型配置（默认不联网）")
    test_p.add_argument("label")
    test_p.add_argument("--network", action="store_true", help="请求一次 <base_url>/models")

    key_p = psub.add_parser("set-key", help="设置 / 更新 API key")
    key_p.add_argument("name")
    key_p.add_argument("key", nargs="?", default=None)

    mem = sub.add_parser("memory", help="记忆系统")
    msub = mem.add_subparsers(dest="memory_command")
    msub.add_parser("show", help="查看记忆配置（默认）")
    msub.add_parser("path", help="打印记忆库目录")
    msub.add_parser("enable", help="启用记忆")
    msub.add_parser("disable", help="停用记忆")
    mset = msub.add_parser("set", help="设置记忆配置项")
    mset.add_argument("key", help="dir / embed-model / embed-base-url / embed-key / vectors / extraction / enabled")
    mset.add_argument("value")
    minit = msub.add_parser("init", help="初始化记忆目录与记忆库")
    minit.add_argument("--dir", dest="directory", default=None, help="同时把目录写进配置")
    return parser


def _dispatch(ctx: Ctx, args: argparse.Namespace) -> int:
    command = args.command
    if command == "path":
        data = {
            "home": str(cfg.home_dir()),
            "providers": str(ctx.providers),
            "config": str(ctx.config_file),
            "cache": str(cfg.cache_dir()),
            "memory": str(memory_dir_for(ctx, ctx.load_config())),
            "skills": str(skills_dir_for(ctx, ctx.load_config())),
        }
        if ctx.json_mode:
            ctx.emit_json(data)
        else:
            for key, value in data.items():
                ctx.out(f"{key.ljust(10)}{value}")
        return 0
    if command == "show":
        return print_overview(ctx)
    if command == "doctor":
        return doctor(ctx)
    if command == "init":
        return setup_init(ctx)
    if command == "get":
        return _command_get(ctx, getattr(args, "key", None), reveal=bool(args.reveal))
    if command == "set":
        return _command_set(ctx, args.key, args.value)
    if command == "unset":
        return _command_unset(ctx, args.key)
    if command == "wizard":
        wizard(ctx)
        return 0

    if command == "providers":
        action = getattr(args, "provider_command", None) or "list"
        if action == "list":
            return providers_list(ctx)
        if action == "show":
            return providers_show(ctx, args.name, reveal_key=bool(args.reveal_key))
        if action == "add":
            return providers_add(
                ctx,
                args.name,
                base_url=args.base_url,
                key=args.key,
                models=args.model,
                api_format=args.api_format,
                context=args.context,
                thinking=args.thinking,
                make_default=bool(args.make_default),
            )
        if action == "edit":
            return providers_edit(
                ctx,
                args.name,
                base_url=args.base_url,
                key=args.key,
                models=args.model,
                remove_models=args.rm_model,
                clear_models=bool(args.clear_models),
                api_format=args.api_format,
                context=args.context,
                thinking=args.thinking,
                opencode_go=args.opencode_go,
            )
        if action == "rm":
            return providers_remove(ctx, args.name)
        if action == "default":
            return providers_default(ctx, args.label)
        if action == "test":
            return providers_test(ctx, args.label, network=bool(args.network))
        if action == "set-key":
            return providers_set_key(ctx, args.name, args.key)
        raise ConfigError(f"未知 providers 子命令: {action!r}")

    if command == "memory":
        action = getattr(args, "memory_command", None) or "show"
        if action == "show":
            return memory_show(ctx)
        if action == "path":
            return memory_path(ctx)
        if action == "enable":
            return memory_enable(ctx, True)
        if action == "disable":
            return memory_enable(ctx, False)
        if action == "set":
            return memory_set(ctx, args.key, args.value)
        if action == "init":
            return memory_init(ctx, args.directory)
        raise ConfigError(f"未知 memory 子命令: {action!r}")

    raise ConfigError(f"未知子命令: {command!r}")


def _command_get(ctx: Ctx, key: str | None, *, reveal: bool = False) -> int:
    config = ctx.load_config()
    if key:
        name = str(key).strip()
        if name not in cfg.KEYS:
            known = ", ".join(sorted(cfg.KEYS))
            raise ConfigError(f"未知配置键: {name!r}（可用: {known}）")
        value = cfg.get_value(config, name)
        if ctx.json_mode:
            ctx.emit_json({name: value if reveal else cfg.describe_value(name, value)})
        else:
            ctx.out(str(value if reveal else cfg.describe_value(name, value)))
        return 0
    if ctx.json_mode:
        ctx.emit_json(
            {name: cfg.describe_value(name, cfg.get_value(config, name)) for name in cfg.KEYS}
        )
        return 0
    for name in cfg.KEYS:
        ctx.out(f"{name.ljust(24)}{cfg.describe_value(name, cfg.get_value(config, name))}")
    return 0


def _command_set(ctx: Ctx, key: str, value: str) -> int:
    config = ctx.load_config()
    parsed = cfg.set_value(config, key, value)
    ctx.save_config(config)
    if ctx.json_mode:
        ctx.emit_json({key: parsed})
        return 0
    _ok(ctx, f"{key} = {cfg.describe_value(key, parsed)}")
    ctx.out(f"  （已写入 {ctx.config_file}；命令行参数与环境变量仍然优先）")
    return 0


def _command_unset(ctx: Ctx, key: str) -> int:
    config = ctx.load_config()
    parsed = cfg.unset_value(config, key)
    ctx.save_config(config)
    if ctx.json_mode:
        ctx.emit_json({key: parsed})
        return 0
    _ok(ctx, f"{key} 已恢复默认: {cfg.describe_value(key, parsed)}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    from .ui import configure_streams

    configure_streams()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    ctx = make_ctx(
        home=args.home,
        config_file=args.config_file,
        providers=args.providers,
        input_fn=input_fn,
        print_fn=print_fn,
        json_mode=bool(args.json),
    )
    try:
        if args.command is None:
            if sys.stdin.isatty():
                wizard(ctx)
                return 0
            print_fn(
                "用法: liorium-cli setup <init|path|show|doctor|get|set|unset|"
                "providers|memory|wizard>"
            )
            print_fn("  交互式: 直接运行 liorium-cli setup（终端里）或在 CLI 里输入 /setup")
            return 0
        return _dispatch(ctx, args)
    except (ConfigError, ProviderConfigError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"文件操作失败: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "Check",
    "Ctx",
    "MEMORY_SHORTCUTS",
    "PROVIDER_THINKING_DIALECTS",
    "WizardResult",
    "build_parser",
    "current_registry",
    "doctor",
    "effective_inbox",
    "effective_memory_dir",
    "effective_skills_dir",
    "ensure_layout",
    "main",
    "make_ctx",
    "memory_dir_of",
    "memory_enable",
    "memory_init",
    "memory_path",
    "memory_set",
    "memory_show",
    "overview",
    "print_overview",
    "providers_add",
    "providers_default",
    "providers_edit",
    "providers_list",
    "providers_remove",
    "providers_set_key",
    "providers_show",
    "providers_test",
    "read_providers",
    "run_checks",
    "setup_init",
    "wizard",
    "wizard_memory",
    "wizard_providers",
    "write_providers",
]
