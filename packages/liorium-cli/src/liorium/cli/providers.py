"""CLI 的 Provider 宿主适配层。

真正的 Provider 管理（schema / 四种格式 / 思考 / models.dev / 模型构建）在
``liorium-provider-manager``（``liorium.provider``）。本模块只保留**宿主策略**：

- 默认配置搜索顺序（``$LIORIUM_PROVIDER_CONFIG`` → ``~/.liorium/providers.json`` → ``./providers.json``）；
- 没有配置文件时用 ``LIORIUM_*`` 环境变量拼一个 provider；
- 交互式向导；
- 保存到 ``~/.liorium/providers.json``。

命令行与环境变量永远优先；配置文件只读加载，不会写回。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from liorium.provider import (
    DEFAULT_FORMAT,
    SUPPORTED_FORMATS,
    ModelProvider,
    ModelProviders,
    ProviderConfigError,
    ProviderRegistry,
    ResolvedModel,
    build_chat_model,
    detect_url_format,
    normalize_format,
)

#: 兼容旧名字：CLI 内部大量使用 ``Provider``。
Provider = ModelProvider

THINKING_DIALECTS = (
    "auto",
    "openai",
    "qwen",
    "deepseek",
    "glm",
    "mimo",
    "minimax",
    "none",
)


def infer_format_from_url(base_url: str) -> str:
    """按 base_url 猜格式；无明确信号时用 ``openai_chat``。"""
    return detect_url_format(base_url) or DEFAULT_FORMAT


def make_chat_model(
    resolved: ResolvedModel,
    *,
    intensity: str | None = None,
    temperature: float | None = 0.3,
    timeout: float = 180.0,
    max_retries: int = 2,
    max_tokens: int = 8192,
    purpose: str = "chat",
) -> Any:
    """按解析结果构造 LangChain 模型（不联网，不校验 key 是否有效）。"""
    if not resolved.provider.api_key:
        raise ProviderConfigError(
            f"provider {resolved.provider.name!r} 没有 API key；"
            "请检查配置或删掉配置后重新填写"
        )
    return build_chat_model(
        resolved.provider,
        resolved.model,
        intensity=intensity,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
        purpose=purpose,
    )


def default_config_candidates() -> list[Path]:
    """默认搜索顺序：环境变量 → 用户主目录的配置 → 当前目录的 ``providers.json``。"""
    candidates: list[Path] = []
    env_path = os.environ.get("LIORIUM_PROVIDER_CONFIG")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(Path.home() / ".liorium" / "providers.json")
    candidates.append(Path.cwd() / "providers.json")
    return candidates


def load_registry(
    explicit: Path | None = None,
    *,
    candidates: Iterable[Path] | None = None,
) -> tuple[ProviderRegistry | None, list[Path]]:
    """按顺序找配置；找到就返回 ``(registry, 尝试过的路径)``。

    显式给了 ``explicit`` 但文件不存在时**直接报错**（用户意图明确，不能静默换源）。
    """
    tried: list[Path] = []
    if explicit is not None:
        target = Path(explicit).expanduser()
        tried.append(target)
        return ProviderRegistry.load(target), tried
    for candidate in candidates if candidates is not None else default_config_candidates():
        tried.append(candidate)
        if candidate.is_file():
            return ProviderRegistry.load(candidate), tried
    from_env = ProviderRegistry.from_env()
    if from_env is not None:
        return from_env, tried
    return None, tried


def save_registry(registry: ProviderRegistry, path: Path) -> Path:
    """把配置写到 ``path``（默认 ``~/.liorium/providers.json``），返回路径。"""
    return registry.save(path)


def prompt_for_registry(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> ProviderRegistry:
    """没有配置文件时逐项询问用户（只在这个分支里读 stdin）。"""
    print_fn("没有找到 provider 配置（~/.liorium/providers.json 或 ./providers.json）。")
    print_fn("请回答几个问题，我用它构造一个 OpenAI 兼容的 provider。")

    base_url = ""
    while not base_url:
        base_url = input_fn("API base_url [https://api.deepseek.com]: ").strip() or "https://api.deepseek.com"

    api_key = ""
    while not api_key:
        api_key = input_fn("API key（不会写入日志，也不会显示）: ").strip()
        if not api_key:
            print_fn("  key 不能为空。")

    model = ""
    while not model:
        model = input_fn("模型名（例如 deepseek-chat / deepseek-v4.1-flash）: ").strip()
        if not model:
            print_fn("  模型名不能为空。")

    print_fn("API 格式：")
    for index, name in enumerate(SUPPORTED_FORMATS, 1):
        print_fn(f"  {index}. {name}")
    raw_choice = input_fn("选择 [1]: ").strip() or "1"
    try:
        api_format = SUPPORTED_FORMATS[int(raw_choice) - 1] if raw_choice.isdigit() else normalize_format(raw_choice)
    except (IndexError, ValueError):
        api_format = None
    if api_format not in SUPPORTED_FORMATS:
        print_fn(f"  无法识别 {raw_choice!r}，使用 {DEFAULT_FORMAT}。")
        api_format = DEFAULT_FORMAT

    raw_window = input_fn("上下文窗口（tokens）[128000]: ").strip() or "128000"
    try:
        context_window = int(raw_window.replace(",", "").replace("_", ""))
    except ValueError:
        print_fn(f"  无法识别 {raw_window!r}，使用 128000。")
        context_window = 128_000

    raw_dialect = (
        input_fn(f"思考方言（{'/'.join(THINKING_DIALECTS)}）[openai]: ").strip().lower()
    )
    thinking_type = raw_dialect if raw_dialect in THINKING_DIALECTS else "openai"
    if raw_dialect and thinking_type != raw_dialect:
        print_fn(f"  无法识别 {raw_dialect!r}，使用 openai。")

    provider = ModelProvider(
        name="custom",
        base_url=base_url.rstrip("/"),
        key=api_key,
        models=[model],
        model_context_lengths={model: context_window},
        model_api_formats={model: api_format},
        api_format=api_format,
        thinking_type=thinking_type,
    )
    providers = ModelProviders(providers=[provider], main_model=f"custom::{model}")
    return ProviderRegistry(providers)


def registry_from_args_or_prompt(
    *,
    config: Path | None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    ask_save: Callable[[str], str] = input,
) -> ProviderRegistry:
    """CLI 的统一入口：``--config`` → 默认候选 → 询问用户（并可选保存）。"""
    registry, tried = load_registry(config)
    if registry is not None:
        return registry

    print_fn("已查找这些位置：")
    for path in tried:
        print_fn(f"  - {path}")
    registry = prompt_for_registry(input_fn=input_fn, print_fn=print_fn)
    answer = (ask_save("把这份配置保存到 ~/.liorium/providers.json 以便下次直接使用？[y/N]: ") or "").strip().lower()
    if answer in ("y", "yes"):
        saved = save_registry(registry, Path.home() / ".liorium" / "providers.json")
        print_fn(f"已保存: {saved}")
    return registry


__all__ = [
    "DEFAULT_FORMAT",
    "Provider",
    "ProviderConfigError",
    "ProviderRegistry",
    "ResolvedModel",
    "SUPPORTED_FORMATS",
    "THINKING_DIALECTS",
    "default_config_candidates",
    "infer_format_from_url",
    "load_registry",
    "make_chat_model",
    "normalize_format",
    "prompt_for_registry",
    "registry_from_args_or_prompt",
    "save_registry",
]
