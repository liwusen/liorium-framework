"""发布形态守卫：一个发行版 ``liorium`` 必须真的包含 12 个模块。

源码按 ``packages/<包名>/src/liorium/<子包>`` 分目录维护（方便逐个约束依赖方向），
但**只发布根项目** ``liorium``：根 ``pyproject.toml`` 用
``[tool.hatch.build.targets.wheel].force-include`` 把 12 个模块树注入同一个 wheel。

这套结构有两个静默失败模式，都靠本文件钉住：

1. 新增一个 ``packages/*/src/liorium/<子包>`` 却忘了加 force-include 映射
   → 构建成功、CI 绿，但 wheel 里少一个模块；
2. 某个成员包加了第三方依赖，根项目没同步 → 从 PyPI 装下来的包 import 就炸。

（wheel 里真实的内容由 ``scripts/check_wheel.py`` 在 CI 里对构建产物再验一次。）
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_FILE = REPO_ROOT / "src" / "liorium" / "__init__.py"

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def root_config() -> dict:
    return tomllib.loads(ROOT_PYPROJECT.read_text(encoding="utf-8"))


def force_include() -> dict[str, str]:
    """``{源目录: wheel 内目标目录}``（只取映射里指向 ``liorium/`` 的那些）。"""
    config = root_config()
    mapping: dict[str, str] = (
        config["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    )
    return {source: target for source, target in mapping.items()}


def source_modules() -> dict[str, Path]:
    """``packages/*/src/liorium/<子包>`` → 以 import 名索引。"""
    modules: dict[str, Path] = {}
    for path in sorted(PACKAGES_DIR.glob("*/src/liorium/*")):
        if path.is_dir() and (path / "__init__.py").is_file():
            modules[path.name] = path
    return modules


def requirement_names(requirements: list[str]) -> set[str]:
    """把 ``liorium-memory[vector]`` / ``nano-vectordb==0.0.4.3`` 归一成包名。"""
    names: set[str] = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(0)).lower())
    return names


def member_requirements() -> dict[str, set[str]]:
    """每个成员包声明的第三方依赖（去掉 liorium 自家包），硬依赖与 extra 合并。"""
    found: dict[str, set[str]] = {}
    for pyproject in sorted(PACKAGES_DIR.glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        declared: list[str] = list(data.get("dependencies", []))
        for extra in data.get("optional-dependencies", {}).values():
            declared.extend(extra)
        found[pyproject.parent.name] = {
            name for name in requirement_names(declared) if not name.startswith("liorium")
        }
    return found


def test_force_include_covers_every_module() -> None:
    """每个 ``packages/*/src/liorium/<子包>`` 都必须被 force-include 到 ``liorium/<子包>``。"""
    expected = {name: f"liorium/{name}" for name in source_modules()}
    mapping = {Path(source).as_posix(): target for source, target in force_include().items()}

    missing = [
        f"{path}（应为 {target}）"
        for name, target in sorted(expected.items())
        for path in [f"packages/*/src/liorium/{name}"]
        if not any(
            Path(source).name == name and target_in == target
            for source, target_in in mapping.items()
        )
    ]
    assert not missing, "force-include 漏了这些模块（wheel 里会缺）：\n" + "\n".join(missing)

    assert len(expected) == 12, f"预期 12 个模块，扫到 {len(expected)}：{sorted(expected)}"


def test_force_include_has_no_stale_entries() -> None:
    """force-include 里不许指向已经不存在的目录（改名/搬家后要同步）。"""
    stale = [
        source
        for source in force_include()
        if not (REPO_ROOT / source).is_dir()
    ]
    assert not stale, "force-include 指向了不存在的目录：\n" + "\n".join(stale)


def test_version_is_declared_once_and_matches() -> None:
    """``pyproject`` 的 version 与 ``src/liorium/__init__.py`` 的 ``__version__`` 必须一致。"""
    config = root_config()
    project_version = str(config["project"]["version"])
    match = _VERSION_LINE.search(INIT_FILE.read_text(encoding="utf-8"))
    assert match, f"{INIT_FILE} 里找不到 __version__"
    assert match.group(1) == project_version, (
        f"__version__={match.group(1)} != pyproject version={project_version}"
    )

    members = {
        path.parent.name: tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"]
        for path in PACKAGES_DIR.glob("*/pyproject.toml")
    }
    drifted = {name: version for name, version in members.items() if version != project_version}
    assert not drifted, f"成员包版本与发行版不一致（它们不发布，但别漂）：{drifted}"


def test_root_declares_every_dependency_of_the_members() -> None:
    """成员包声明的第三方依赖，根项目必须也声明（否则从 PyPI 装下来会 import 失败）。"""
    config = root_config()["project"]
    declared: list[str] = list(config.get("dependencies", []))
    for extra in config.get("optional-dependencies", {}).values():
        declared.extend(extra)
    available = requirement_names(declared)

    missing: list[str] = []
    for package, required in sorted(member_requirements().items()):
        for name in sorted(required - available):
            missing.append(f"{package} 需要 {name}，根 pyproject.toml 没声明")
    assert not missing, (
        "发布版的依赖不全（成员包有、根项目没有）：\n" + "\n".join(missing)
    )
