"""realm source 解析：内置白名单 / 模块属性 / 文件型 realm 重载（计划 B3/B4/D4/D5）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from liorium.profile import AgentProfile, ProfileSchemaError, ProfileSourceError


def write_profile(tmp_path: Path, realms: list[dict]) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"version": 1, "id": "p", "realms": realms}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_realm_module(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


ECHO_V1 = """
from pydantic import BaseModel, ConfigDict


class EchoRealm:
    id = "echo"
    requires = ()

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        greeting: str = "hi"
        count: int = 1
"""

ECHO_V2 = ECHO_V1 + """

MARKER = "v2"
"""


def test_builtin_realm_without_source(tmp_path: Path) -> None:
    profile = AgentProfile.load(write_profile(tmp_path, [{"id": "subagents"}]))
    assert profile.realm_factories == {}
    assert profile.realm_ids() == ("subagents",)


def test_builtin_whitelist_rejects_other_ids(tmp_path: Path) -> None:
    with pytest.raises(ProfileSchemaError, match="source"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "memory"}]))


def test_module_attribute_source(tmp_path: Path) -> None:
    profile = AgentProfile.load(
        write_profile(
            tmp_path,
            [{"id": "todo", "source": "liorium.todo_realm:TodoRealm"}],
        )
    )
    factory = profile.realm_factories["todo"]
    assert factory.__name__ == "TodoRealm"
    assert getattr(factory, "id", "") == "todo"


def test_file_source_caches_and_reloads_on_mtime(tmp_path: Path) -> None:
    module_path = write_realm_module(tmp_path, "echo_realm.py", ECHO_V1)
    path = write_profile(tmp_path, [{"id": "echo", "source": "./echo_realm.py:EchoRealm"}])

    before_path = list(sys.path)
    before_modules = set(sys.modules)
    first = AgentProfile.load(path)
    first_cls = first.realm_factories["echo"]
    assert first_cls.__name__ == "EchoRealm"

    new_names = [
        name
        for name in sys.modules
        if name not in before_modules and name.startswith("liorium_profile_realm_echo_realm_")
    ]
    assert len(new_names) == 1
    module_name = new_names[0]
    first_module = sys.modules[module_name]

    second = AgentProfile.load(path)
    assert second.realm_factories["echo"] is first_cls  # 未改动：复用同一模块对象
    assert sys.modules[module_name] is first_module

    module_path.write_text(ECHO_V2, encoding="utf-8")
    third = AgentProfile.load(path)
    third_cls = third.realm_factories["echo"]
    assert third_cls is not first_cls  # 改动后重新执行
    assert sys.modules[module_name] is not first_module  # 同名条目被替换，不堆积重复模块
    assert sys.path == before_path  # 不污染 sys.path


def test_file_source_validates_declared_id(tmp_path: Path) -> None:
    write_realm_module(tmp_path, "echo_realm.py", ECHO_V1)
    with pytest.raises(ProfileSourceError, match="id"):
        AgentProfile.load(
            write_profile(tmp_path, [{"id": "other", "source": "./echo_realm.py:EchoRealm"}])
        )


def test_file_source_checks_requires_against_profile(tmp_path: Path) -> None:
    write_realm_module(
        tmp_path,
        "needy.py",
        "class Needy:\n    id = 'needy'\n    requires = ('core',)\n",
    )
    with pytest.raises(ProfileSourceError, match="requires"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "needy", "source": "./needy.py:Needy"}]))


def test_config_defaults_and_unknown_keys(tmp_path: Path) -> None:
    write_realm_module(tmp_path, "echo_realm.py", ECHO_V1)
    profile = AgentProfile.load(
        write_profile(tmp_path, [{"id": "echo", "source": "./echo_realm.py:EchoRealm"}])
    )
    assert profile.realms[0].config == {"greeting": "hi", "count": 1}

    with pytest.raises(ProfileSchemaError, match=r"realms\[0\]\.config\.nope"):
        AgentProfile.load(
            write_profile(
                tmp_path,
                [{"id": "echo", "source": "./echo_realm.py:EchoRealm", "config": {"nope": 1}}],
            )
        )


def test_dotted_attribute_and_windows_style_file_path(tmp_path: Path) -> None:
    write_realm_module(
        tmp_path,
        "dotted.py",
        "class Outer:\n    class Inner:\n        id = 'dotted'\n        requires = ()\n",
    )
    profile = AgentProfile.load(
        write_profile(tmp_path, [{"id": "dotted", "source": "./dotted.py:Outer.Inner"}])
    )
    assert profile.realm_factories["dotted"].__name__ == "Inner"

    if sys.platform == "win32":
        module_path = write_realm_module(tmp_path, "echo_realm.py", ECHO_V1)
        absolute = f"{module_path}:EchoRealm"
        profile = AgentProfile.load(write_profile(tmp_path, [{"id": "echo", "source": absolute}]))
        assert profile.realm_factories["echo"].__name__ == "EchoRealm"


def test_source_errors_are_clear(tmp_path: Path) -> None:
    with pytest.raises(ProfileSourceError, match="pkg.mod:Class"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "a", "source": "no-colon"}]))
    with pytest.raises(ProfileSourceError, match="导入"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "a", "source": "no.such.module:Cls"}]))
    with pytest.raises(ProfileSourceError, match="不存在"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "a", "source": "./nope.py:Cls"}]))
    write_realm_module(tmp_path, "echo_realm.py", ECHO_V1)
    with pytest.raises(ProfileSourceError, match="没有"):
        AgentProfile.load(write_profile(tmp_path, [{"id": "a", "source": "./echo_realm.py:Nope"}]))
