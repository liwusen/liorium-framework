"""CLI 用户设置：``~/.liorium/config.json`` 与冷启动骨架。

为什么和 ``providers.json`` 分开：

- ``providers.json`` 只放 provider 与密钥（0600、由 ``liorium-provider-manager`` 校验，
  团队之间可以直接分享一份去掉 key 的版本）；
- ``config.json`` 放"怎么跑"：默认模型 / 思考强度 / 工作区 / 记忆 / 技能 / 待办 / UI。

两个文件都**可以不存在**：读不到就用内置默认，启动不会因此失败。第一次运行时
:func:`ensure_home` 会把目录和两个空骨架建出来，所以新电脑上 ``liorium-cli`` 能直接起来，
再由用户决定什么时候填 provider。

优先级（高 → 低）：命令行参数 > 进程环境变量 > ``.env`` > ``config.json`` > 内置默认。
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
    # 默认模型（Provider::model）；None = 用 providers.json 的 main_model。
    "model": None,
    "thinking": "medium",
    "workspace": None,
    "inbox": None,
    "memory": {
        "enabled": True,
        "dir": None,
        "embed_model": "",
        "embed_base_url": "",
        "embed_key": "",
        "vectors": True,
        "extraction": True,
    },
    "skills": {"enabled": True, "dir": None},
    "todo": {"enabled": True},
    "subagents": {"enabled": True},
    "ui": {"color": True, "hide_reasoning": False},
}

#: 可写入的键 → (类型, 说明)。类型决定 :func:`coerce` 怎么解析用户输入。
KEYS: dict[str, tuple[str, str]] = {
    "model": ("str", "默认模型（Provider::model）"),
    "thinking": ("choice", f"思考强度（{'/'.join(THINKING_LEVELS)}）"),
    "workspace": ("path", "工作区根目录（默认当前目录）"),
    "inbox": ("path", "Agent 根目录：prompt / checkpoint（默认 <workspace>/.liorium-cli/agent）"),
    "memory.enabled": ("bool", "是否启用长期记忆"),
    "memory.dir": ("path", "记忆库目录（默认 <inbox>/memory）"),
    "memory.embed_model": ("str", "embedding 模型（给了才启用语义检索）"),
    "memory.embed_base_url": ("str", "embedding 的 OpenAI 兼容 base_url（默认跟随 provider）"),
    "memory.embed_key": ("str", "embedding 的 API key（默认跟随 provider）"),
    "memory.vectors": ("bool", "是否建向量索引（关掉只跑关键词）"),
    "memory.extraction": ("bool", "写记忆后是否自动抽实体 / 关系"),
    "skills.enabled": ("bool", "是否启用技能库"),
    "skills.dir": ("path", "技能目录（默认 <inbox>/skill.d）"),
    "todo.enabled": ("bool", "是否启用待办"),
    "subagents.enabled": ("bool", "是否启用子代理"),
    "ui.color": ("bool", "是否用 ANSI 颜色"),
    "ui.hide_reasoning": ("bool", "是否隐藏 reasoning 增量"),
}

#: 需要打码显示的值（``setup show`` / ``/setup show``）。
SECRET_KEYS = frozenset({"memory.embed_key"})

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
# 取值 / 赋值
# ---------------------------------------------------------------------------


def get_value(config: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """读一个点分键（``memory.dir``）；不存在返回 ``default``。"""
    node: Any = config
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
    if name not in KEYS:
        known = ", ".join(sorted(KEYS))
        raise ConfigError(f"未知配置键: {name!r}（可用: {known}）")
    fallback = get_value(default_config(), name)
    return set_value(config, name, fallback)


def coerce(key: str, raw: Any) -> Any:
    """按 :data:`KEYS` 的类型解析用户输入。"""
    kind = KEYS[str(key)][0]
    if kind == "bool":
        return coerce_bool(raw, key)
    if kind == "choice":
        text = str(raw or "").strip().lower()
        if text not in THINKING_LEVELS:
            raise ConfigError(
                f"{key} 只能是 {'/'.join(THINKING_LEVELS)}，收到 {raw!r}"
            )
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
    "providers_path",
    "save_config",
    "set_value",
    "unset_value",
    "write_json",
]
