"""CLI 侧的 profile 文件：默认模板、路径发现、迁移、按 realm 粒度读写。

单一真相（计划 G1）：**装配**看 profile，**显示偏好**看 ``config.json``（只剩 ``ui``）。
这里只做"文件层"的事（读写 / 迁移 / 模板），编译交给 :class:`liorium.profile.AgentProfile`。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from liorium.profile import AgentProfile

from . import config as cfg

PROFILE_NAME = "profile.json"
"""``~/.liorium/profile.json``；当前目录的 ``.liorium/profile.json`` 优先（G2）。"""

CORE_REALM = {"id": "core", "source": "liorium.core_tool_realm:CoreToolRealm"}
MEMORY_REALM = {"id": "memory", "source": "liorium.memory:MemoryRealm"}
SKILLS_REALM = {"id": "skills", "source": "liorium.skills:SkillsRealm"}
TODO_REALM = {"id": "todo", "source": "liorium.todo_realm:TodoRealm"}
SUBAGENTS_REALM = {"id": "subagents"}

ASSEMBLY_KEYS = (
    "model",
    "thinking",
    "temperature",
    "workspace",
    "inbox",
    "prompt",
    "toolsets",
    "limits",
    "memory",
    "skills",
    "todo",
    "subagents",
)
"""旧 ``config.json`` 里属于"装配"的键；命中任意一个就值得迁移一次（G3）。"""


# ---------------------------------------------------------------------------
# 路径与模板
# ---------------------------------------------------------------------------


def profile_candidates(*, cwd: Path | None = None) -> list[Path]:
    """搜索顺序：当前目录 → 配置根目录。"""
    root = Path(cwd) if cwd is not None else Path.cwd()
    return [root / ".liorium" / PROFILE_NAME, cfg.home_dir() / PROFILE_NAME]


def resolve_profile_path(explicit: Path | str | None = None, *, cwd: Path | None = None) -> Path:
    """``--profile`` > 当前目录 > 配置根目录；都不存在时用配置根目录的路径（待生成）。"""
    if explicit is not None:
        return Path(explicit).expanduser()
    for candidate in profile_candidates(cwd=cwd):
        if candidate.is_file():
            return candidate
    return cfg.home_dir() / PROFILE_NAME


def default_profile(*, provider_file: str | None = None) -> dict[str, Any]:
    """新装默认 profile：core / memory / skills / todo / subagents 五件套。"""
    return {
        "version": 1,
        "id": "cli",
        "provider_file": provider_file,
        "model": None,
        "thinking": "medium",
        "temperature": 0.3,
        "workspace": None,
        "inbox": ".liorium-cli/agent",
        "prompt": ["AGENT.md"],
        "boot": {"message": None},
        "toolsets": {},
        "realms": [
            dict(CORE_REALM),
            dict(MEMORY_REALM),
            dict(SKILLS_REALM),
            dict(TODO_REALM),
            dict(SUBAGENTS_REALM),
        ],
    }


def load_raw(path: Path | str) -> dict[str, Any]:
    """读 profile 的原始 JSON（不校验；校验走 ``AgentProfile.load``）。"""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise cfg.ConfigError(f"profile 不存在: {target}") from exc
    except json.JSONDecodeError as exc:
        raise cfg.ConfigError(f"profile 不是合法 JSON: {target}（{exc}）") from exc
    except OSError as exc:
        raise cfg.ConfigError(f"profile 读不出来: {target}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise cfg.ConfigError(f"profile 顶层必须是对象: {target}")
    return raw


def save_raw(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """原子写入（0600）：profile 可能含明文 key（D6）。"""
    return cfg.write_json(Path(path).expanduser(), payload)


def ensure_profile(path: Path | str, *, provider_file: str | None = None) -> tuple[Path, bool]:
    """不存在就写默认模板；返回 ``(路径, 是否新建)``。已存在的不覆盖。"""
    target = Path(path).expanduser()
    if target.is_file():
        return target, False
    save_raw(target, default_profile(provider_file=provider_file))
    return target, True


def load(path: Path | str, *, env: Mapping[str, str] | None = None) -> AgentProfile:
    """加载 + 全量校验（等价 ``AgentProfile.load``，留一个 CLI 入口便于 patch）。"""
    return AgentProfile.load(path, env=env)


# ---------------------------------------------------------------------------
# 迁移：旧 config.json → profile
# ---------------------------------------------------------------------------


def has_assembly_settings(config: Mapping[str, Any]) -> bool:
    """旧 config 里是否有需要迁移的装配字段（``ui`` 之外的东西都算）。"""
    return any(key in config for key in ASSEMBLY_KEYS)


def migrate_config_to_profile(
    config: Mapping[str, Any],
    *,
    provider_file: str | None,
    provider_base_url: str = "",
    provider_key: str = "",
) -> dict[str, Any]:
    """把旧 ``config.json`` 映射成 profile（纯函数，便于测试）。"""
    profile = default_profile(provider_file=provider_file)
    for key in ("model", "thinking", "temperature", "workspace", "inbox"):
        if config.get(key) is not None:
            profile[key] = config[key]

    realms: list[dict[str, Any]] = []
    if config.get("memory", {}).get("enabled", True):
        memory = dict(MEMORY_REALM)
        memory_cfg: dict[str, Any] = {}
        if config.get("memory", {}).get("dir"):
            memory_cfg["dir"] = str(config["memory"]["dir"])
        embed_model = str(config.get("memory", {}).get("embed_model") or "")
        if embed_model:
            memory_cfg["embed"] = {
                "model": embed_model,
                "base_url": str(config["memory"].get("embed_base_url") or provider_base_url or ""),
                "api_key": str(config["memory"].get("embed_key") or provider_key or ""),
            }
        memory_cfg["vectors"] = bool(config.get("memory", {}).get("vectors", True))
        memory_cfg["extraction"] = bool(config.get("memory", {}).get("extraction", True))
        if memory_cfg:
            memory["config"] = memory_cfg
        realms.append(memory)
    if config.get("skills", {}).get("enabled", True):
        skills = dict(SKILLS_REALM)
        if config.get("skills", {}).get("dir"):
            skills["config"] = {"skills_dir": str(config["skills"]["dir"])}
        realms.append(skills)
    if config.get("todo", {}).get("enabled", True):
        realms.append(dict(TODO_REALM))
    if config.get("subagents", {}).get("enabled", True):
        realms.append(dict(SUBAGENTS_REALM))

    profile["realms"] = [dict(CORE_REALM), *realms]
    return profile


def ui_only(config: Mapping[str, Any]) -> dict[str, Any]:
    """迁移后 ``config.json`` 剩下的形状：``{version, ui}``。"""
    ui = dict(config.get("ui") or {})
    return {
        "version": cfg.CONFIG_VERSION,
        "ui": {
            "color": bool(ui.get("color", True)),
            "hide_reasoning": bool(ui.get("hide_reasoning", False)),
        },
    }


def migrate_to_profile(
    *,
    config_path: Path | str | None = None,
    profile_path: Path | str | None = None,
    provider_file: str | None = None,
    provider_base_url: str = "",
    provider_key: str = "",
) -> Path | None:
    """执行一次性迁移；返回 profile 路径（已迁移 / 不需要时返回 ``None``）。

    只在 profile 不存在时执行；原 ``config.json`` 备份成 ``config.json.bak``，
    然后重写成 UI-only。
    """
    target_config = Path(config_path).expanduser() if config_path else cfg.config_path()
    target_profile = (
        Path(profile_path).expanduser() if profile_path else cfg.home_dir() / PROFILE_NAME
    )
    if target_profile.is_file():
        return None
    if not target_config.is_file():
        return None
    try:
        raw = json.loads(target_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise cfg.ConfigError(f"配置文件读不出来，无法迁移: {target_config}（{exc}）") from exc
    if not isinstance(raw, dict) or not has_assembly_settings(raw):
        return None

    merged = cfg._deep_merge(cfg.default_config(), raw)  # noqa: SLF001 - 同包工具
    save_raw(
        target_profile,
        migrate_config_to_profile(
            merged,
            provider_file=provider_file,
            provider_base_url=provider_base_url,
            provider_key=provider_key,
        ),
    )
    backup = target_config.with_suffix(target_config.suffix + ".bak")
    backup.write_text(target_config.read_text(encoding="utf-8"), encoding="utf-8")
    cfg.save_config(ui_only(merged), target_config)
    return target_profile


# ---------------------------------------------------------------------------
# 按 realm 读写（setup memory / skills 用）
# ---------------------------------------------------------------------------


def find_realm(data: dict[str, Any], realm_id: str) -> dict[str, Any] | None:
    for entry in data.get("realms") or []:
        if isinstance(entry, dict) and entry.get("id") == realm_id:
            return entry
    return None


def ensure_realm(data: dict[str, Any], realm_id: str, template: Mapping[str, Any]) -> dict[str, Any]:
    existing = find_realm(data, realm_id)
    if existing is not None:
        return existing
    entry = dict(template)
    data.setdefault("realms", []).append(entry)
    return entry


def remove_realm(data: dict[str, Any], realm_id: str) -> bool:
    realms = data.get("realms") or []
    kept = [entry for entry in realms if not (isinstance(entry, dict) and entry.get("id") == realm_id)]
    removed = len(kept) != len(realms)
    data["realms"] = kept
    return removed


def realm_config(data: dict[str, Any], realm_id: str) -> dict[str, Any]:
    entry = find_realm(data, realm_id)
    if entry is None:
        return {}
    config = entry.get("config")
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config
    return config


def set_realm_enabled(
    data: dict[str, Any], realm_id: str, enabled: bool, template: Mapping[str, Any]
) -> None:
    if enabled:
        ensure_realm(data, realm_id, template)
    else:
        remove_realm(data, realm_id)


__all__ = [
    "ASSEMBLY_KEYS",
    "CORE_REALM",
    "MEMORY_REALM",
    "PROFILE_NAME",
    "SKILLS_REALM",
    "SUBAGENTS_REALM",
    "TODO_REALM",
    "default_profile",
    "ensure_profile",
    "ensure_realm",
    "find_realm",
    "has_assembly_settings",
    "load",
    "load_raw",
    "migrate_config_to_profile",
    "migrate_to_profile",
    "profile_candidates",
    "realm_config",
    "remove_realm",
    "resolve_profile_path",
    "save_raw",
    "set_realm_enabled",
    "ui_only",
]
