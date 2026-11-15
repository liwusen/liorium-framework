"""CLI 的 profile 文件层：模板 / 路径 / 迁移 / realm 读写（计划 G2/G3/§6.6）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liorium.cli import config as cfg
from liorium.cli import profile as prof
from liorium.profile import AgentProfile


def test_default_profile_validates_and_lists_all_realms() -> None:
    profile = AgentProfile.from_dict(prof.default_profile(), default_id="cli")
    assert profile.id == "cli"
    assert [realm.id for realm in profile.realms] == [
        "core",
        "memory",
        "skills",
        "todo",
        "subagents",
    ]
    assert set(profile.realm_factories) == {"core", "memory", "skills", "todo"}


def test_profile_path_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cfg.HOME_ENV, str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    local = tmp_path / "proj" / ".liorium"
    local.mkdir(parents=True)

    # 都不存在 ⇒ 用 home 下的路径（待生成）
    assert prof.resolve_profile_path(cwd=tmp_path / "proj") == home / prof.PROFILE_NAME

    (home / prof.PROFILE_NAME).write_text("{}", encoding="utf-8")
    assert prof.resolve_profile_path(cwd=tmp_path / "proj") == home / prof.PROFILE_NAME

    (local / prof.PROFILE_NAME).write_text("{}", encoding="utf-8")
    assert prof.resolve_profile_path(cwd=tmp_path / "proj") == local / prof.PROFILE_NAME

    explicit = tmp_path / "custom.json"
    assert prof.resolve_profile_path(explicit, cwd=tmp_path / "proj") == explicit


def test_ensure_profile_writes_once(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    target, created = prof.ensure_profile(path, provider_file=str(tmp_path / "providers.json"))
    assert created is True
    assert target == path
    data = prof.load_raw(path)
    assert data["provider_file"] == str(tmp_path / "providers.json")

    again, created = prof.ensure_profile(path)
    assert created is False
    assert prof.load_raw(path) == data


def test_migrate_maps_old_config_to_profile() -> None:
    old = {
        "version": 1,
        "model": "P::m",
        "thinking": "high",
        "workspace": "/w",
        "inbox": "/w/agent",
        "memory": {
            "enabled": True,
            "dir": "/mem",
            "embed_model": "text-embedding-3-small",
            "embed_base_url": "",
            "embed_key": "",
            "vectors": False,
            "extraction": False,
        },
        "skills": {"enabled": False, "dir": None},
        "todo": {"enabled": True},
        "subagents": {"enabled": False},
        "ui": {"color": False, "hide_reasoning": True},
    }
    moved = prof.migrate_config_to_profile(
        old,
        provider_file="/providers.json",
        provider_base_url="https://api.test/v1",
        provider_key="sk-provider",
    )
    assert moved["model"] == "P::m"
    config = prof.find_realm(moved, "memory")["config"]
    assert config["dir"] == "/mem"
    assert config["embed"] == {
        "model": "text-embedding-3-small",
        "base_url": "https://api.test/v1",
        "api_key": "sk-provider",
    }
    assert config["vectors"] is False
    assert prof.find_realm(moved, "skills") is None  # enabled=false ⇒ 不生成
    assert prof.find_realm(moved, "todo") is not None
    assert prof.find_realm(moved, "subagents") is None

    ui = prof.ui_only(old)
    assert ui == {"version": 1, "ui": {"color": False, "hide_reasoning": True}}


def test_migrate_to_profile_writes_backup_and_ui_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cfg.HOME_ENV, str(tmp_path / "home"))
    cfg.ensure_home(home=tmp_path / "home")

    config_path = tmp_path / "home" / "config.json"
    old = cfg.default_config()
    old["model"] = "P::m"
    old["ui"] = {"color": False, "hide_reasoning": True}
    cfg.save_config(old, config_path)

    profile_path = cfg.home_dir() / prof.PROFILE_NAME
    moved = prof.migrate_to_profile(
        config_path=config_path,
        profile_path=profile_path,
        provider_file=str(tmp_path / "home" / "providers.json"),
    )

    assert moved == profile_path
    assert json.loads(profile_path.read_text(encoding="utf-8"))["model"] == "P::m"
    assert (config_path.parent / "config.json.bak").is_file()
    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(rewritten) == {"version", "ui"}
    assert rewritten["ui"] == {"color": False, "hide_reasoning": True}

    # 第二次：profile 已存在 ⇒ 不迁移
    assert prof.migrate_to_profile(config_path=config_path, profile_path=profile_path) is None


def test_realm_helpers(tmp_path: Path) -> None:
    data = prof.default_profile()
    assert prof.find_realm(data, "memory") is not None
    assert prof.remove_realm(data, "memory") is True
    assert prof.find_realm(data, "memory") is None
    assert prof.remove_realm(data, "memory") is False

    memory = prof.ensure_realm(data, "memory", prof.MEMORY_REALM)
    config = prof.realm_config(data, "memory")
    config["dir"] = "mem"
    assert prof.find_realm(data, "memory") is memory
    assert prof.realm_config(data, "memory")["dir"] == "mem"

    prof.set_realm_enabled(data, "todo", False, prof.TODO_REALM)
    assert prof.find_realm(data, "todo") is None
    prof.set_realm_enabled(data, "todo", True, prof.TODO_REALM)
    assert prof.find_realm(data, "todo") is not None
