"""CLI 用户设置：``~/.liorium/config.json``（只剩 UI 偏好）与冷启动骨架。

2026-11-14 起（计划 G1 / A5）：**装配**看 ``profile.json``（Agent Profile），
``config.json`` 只放"怎么显示"——颜色、是否隐藏 reasoning。这样 Agent 语义只有一份真相，
UI 偏好也不会污染库的 schema。

为什么 provider 仍单独一个文件：``providers.json`` 只放 provider 与密钥（0600、
由 ``liorium-provider-manager`` 校验，团队之间可以直接分享一份去掉 key 的版本）。

三个文件都**可以不存在**：读不到就用内置默认，启动不会因此失败。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HOME_ENV = "LIORIUM_HOME"
"""``~/.liorium`` 的替代位置（测试与多套配置并存用）。"""

CONFIG_ENV = "LIORIUM_CONFIG_FILE"
"""``config.json`` 的替代位置。"""

PROVIDERS_ENV = "LIORIUM_PROVIDER_CONFIG"
"""``providers.json`` 的替代位置（与 :mod:`liorium.cli.providers` 共用同一个变量）。"""

THINKING_LEVELS = ("off", "low", "medium", "high")

CONFIG_VERSION = 1
"""配置文件版本；只做前向兼容提示，不参与校验。"""

DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "ui": {"color": True, "hide_reasoning": False},
}

#: 可写入的键 → (类型, 说明)。只剩 UI；Agent 装配键已经迁到 profile（见 migrate_to_profile）。
KEYS: dict[str, tuple[str, str]] = {
    "ui.color": ("bool", "是否用 ANSI 颜色"),
    "ui.hide_reasoning": ("bool", "是否隐藏 reasoning 增量"),
}

#: 旧 ``config.json`` 里属于装配的键：命中这些键时给出"去改 profile"的明确指引。
LEGACY_KEYS: dict[str, str] = {
    "model": "profile 的 model",
    "thinking": "profile 的 thinking",
    "temperature": "profile 的 temperature",
    "workspace": "profile 的 workspace",
    "inbox": "profile 的 inbox",
    "prompt": "profile 的 prompt",
    "toolsets": "profile 的 toolsets",
    "limits": "profile 的 limits",
    "memory": "profile 里 memory realm 的 config",
    "skills": "profile 里 skills realm 的 config",
    "todo": "profile 的 realms",
    "subagents": "profile 的 realms",
}

#: 需要打码显示的值（``setup show`` / ``/setup show``）。
SECRET_KEYS: frozenset[str] = frozenset()

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


class ConfigError(RuntimeError):
    """``config.json`` 非法或取值不合法（消息里带路径 / 键名，别让用户猜）。"""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def home_dir() -> Path:
    """配置根目录：``$LIORIUM_HOME`` → ``~/.liorium``。"""
    raw = os.environ.get(HOME_ENV)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".liorium"


def config_path() -> Path:
    """``config.json`` 路径：``$LIORIUM_CONFIG_FILE`` → ``<home>/config.json``。"""
    raw = os.environ.get(CONFIG_ENV)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    return home_dir() / "config.json"


def providers_path() -> Path:
    """**写** provider 配置的目标路径：环境变量 → 当前目录已有 → ``<home>/providers.json``。

    读取顺序在 :func:`liorium.cli.providers.default_config_candidates`，两边保持一致：
    当前目录里已经有一份 ``providers.json`` 时，``setup`` 就改那一份，不另起炉灶。
    """
    raw = os.environ.get(PROVIDERS_ENV)
    if raw is not None and raw.strip():
        return Path(raw.strip()).expanduser()
    local = Path.cwd() / "providers.json"
    if local.is_file():
        return local
    return home_dir() / "providers.json"


def cache_dir() -> Path:
    return home_dir() / "cache"


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------


def default_config() -> dict[str, Any]:
    """内置默认配置（深拷贝，调用方随便改）。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config(path: Path | str | None = None, *, missing_ok: bool = True) -> dict[str, Any]:
    """读取 ``config.json`` 并与内置默认做深合并。

    文件不存在：``missing_ok`` 时返回默认值，否则抛 :class:`ConfigError`。
    JSON / 顶层结构非法一律抛错——"配置写了但没生效"比启动失败更难查。
    """
    target = Path(path).expanduser() if path is not None else config_path()
    if not target.is_file():
        if missing_ok:
            return default_config()
        raise ConfigError(f"配置文件不存在: {target}")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {target}（{exc}）") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读不出来: {target}（{exc}）") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"配置文件顶层必须是对象: {target}")
    merged = _deep_merge(default_config(), raw)
    version = merged.get("version")
    if not isinstance(version, int):
        merged["version"] = CONFIG_VERSION
    return merged


def save_config(config: Mapping[str, Any], path: Path | str | None = None) -> Path:
    """原子写入 ``config.json``（临时文件 + ``os.replace``，权限 0600）。"""
    target = Path(path).expanduser() if path is not None else config_path()
    return write_json(target, config)


def write_json(path: Path, payload: Mapping[str, Any] | list[Any]) -> Path:
    """原子写 JSON（UTF-8、缩进 2、末尾换行、0600）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def ensure_home(
    *,
    home: Path | None = None,
    create_providers: bool = True,
    create_config: bool = True,
) -> dict[str, Any]:
    """建出配置骨架，返回 ``{路径名: 路径或 None（已存在）}``。

    已存在的文件**不覆盖**：这个方法会在每次正常启动时调用，改配置不能靠它。
    """
    base = Path(home).expanduser() if home is not None else home_dir()
    created: dict[str, Any] = {"home": None, "config": None, "providers": None, "cache": None}
    if not base.is_dir():
        created["home"] = base
    base.mkdir(parents=True, exist_ok=True)

    cache = base / "cache"
    if not cache.is_dir():
        cache.mkdir(parents=True, exist_ok=True)
        created["cache"] = cache

    if create_config:
        target = config_path() if home is None else base / "config.json"
        if not target.is_file():
            created["config"] = save_config(default_config(), target)

    if create_providers:
        target = providers_path() if home is None else base / "providers.json"
        if not target.is_file():
            # 空骨架必须是合法 JSON，且能让 loader 判定"还没配置"。
            created["providers"] = write_json(target, {"providers": []})
    return created


# ---------------------------------------------------------------------------
# 迁移（旧 config.json → profile.json）
# ---------------------------------------------------------------------------


def migrate_to_profile(**kwargs: Any) -> Path | None:
    """把旧 ``config.json`` 的装配字段迁到 profile（一次性；实现在 ``cli.profile``）。

    返回 profile 路径；不需要迁移（profile 已存在 / 旧配置没有装配字段）时返回 ``None``。
    """
    from . import profile as profile_module

    return profile_module.migrate_to_profile(**kwargs)


# ---------------------------------------------------------------------------
# 取值 / 赋值
# ---------------------------------------------------------------------------


def get_value(config: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """读一个点分键（``ui.color``）；不存在返回 ``default``。"""
    node = config
    for part in str(key).split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def set_value(config: dict[str, Any], key: str, raw: Any) -> Any:
    """把用户输入写进 ``config``（就地修改），返回规范化后的值。

    未知键 / 类型不合法抛 :class:`ConfigError`——静默接受一个拼错的键，
    用户会以为配置生效了。
    """
    name = str(key).strip()
    _reject_legacy(name)
    if name not in KEYS:
        known = ", ".join(sorted(KEYS))
        raise ConfigError(f"未知配置键: {name!r}（可用: {known}）")
    value = coerce(name, raw)
    parts = name.split(".")
    node = config
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    return value


def unset_value(config: dict[str, Any], key: str) -> Any:
    """恢复内置默认值（就地修改），返回值。"""
    name = str(key).strip()
    _reject_legacy(name)
    if name not in KEYS:
        known = ", ".join(sorted(KEYS))
        raise ConfigError(f"未知配置键: {name!r}（可用: {known}）")
    fallback = get_value(default_config(), name)
    return set_value(config, name, fallback)


def _reject_legacy(name: str) -> None:
    top = name.split(".", 1)[0]
    if top in LEGACY_KEYS:
        raise ConfigError(
            f"{name!r} 是 Agent 装配设置，已经迁到 profile（{LEGACY_KEYS[top]}）；"
            "请用 `liorium-cli setup profile edit` 或直接改 profile.json"
        )


def coerce(key: str, raw: Any) -> Any:
    """按 :data:`KEYS` 的类型解析用户输入。"""
    kind = KEYS[str(key)][0]
    if kind == "bool":
        return coerce_bool(raw, key)
    if kind == "choice":
        text = str(raw or "").strip().lower()
        if text not in THINKING_LEVELS:
            raise ConfigError(f"{key} 只能是 {'/'.join(THINKING_LEVELS)}，收到 {raw!r}")
        return text
    if kind == "path":
        text = str(raw or "").strip()
        return text or None
    text = str(raw if raw is not None else "").strip()
    return text or None


def coerce_bool(raw: Any, key: str = "") -> bool:
    """``1/true/yes/on`` 真、``0/false/no/off`` 假，其它报错。"""
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{key or '值'} 不是合法布尔值: {raw!r}（用 1/0/true/false）")


def mask_secret(value: Any, *, keep: int = 4) -> str:
    """``sk-abcdef123456`` → ``sk-a…3456``；空值显示 ``(未设置)``。"""
    text = str(value or "")
    if not text:
        return "(未设置)"
    if len(text) <= keep + 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]}"


def describe_value(key: str, value: Any) -> str:
    """给 ``setup show`` 用的一行展示（密钥打码、空值说清楚）。"""
    if key in SECRET_KEYS:
        return mask_secret(value)
    if value is None or value == "":
        return "(未设置)"
    if isinstance(value, bool):
        return "开" if value else "关"
    return str(value)


def _deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``extra`` 叠到 ``base`` 上：字典递归合并，未知键原样保留。"""
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


__all__ = [
    "CONFIG_ENV",
    "CONFIG_VERSION",
    "ConfigError",
    "DEFAULT_CONFIG",
    "HOME_ENV",
    "KEYS",
    "LEGACY_KEYS",
    "PROVIDERS_ENV",
    "SECRET_KEYS",
    "THINKING_LEVELS",
    "cache_dir",
    "coerce",
    "coerce_bool",
    "config_path",
    "default_config",
    "describe_value",
    "ensure_home",
    "get_value",
    "home_dir",
    "load_config",
    "mask_secret",
    "migrate_to_profile",
    "providers_path",
    "save_config",
    "set_value",
    "unset_value",
    "write_json",
]
