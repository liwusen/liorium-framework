"""pytest 根 conftest：路径注入 + 全仓库共用夹具。

为什么要手写路径注入：

- ``.venv`` 里的 editable 安装已经能 import，但仓库也可能没装进解释器
  （带 ``python311._pth`` 的内嵌发行版会忽略 ``PYTHONPATH``），必须靠这里补。
- 各包的测试共享 ``tests/liorium_testkit.py``（假模型等），所以 ``tests/`` 也要进 path。
- 仓库里有多个同名 ``conftest.py``，pytest 在 prepend 模式下会互相覆盖 ——
  因此夹具统一放这里，包里不要再放 conftest.py。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent

# 测试一律不读开发机上的 .env：否则 LIORIUM_MODEL / LIORIUM_MEMORY_DIR 会串进断言，
# 甚至把测试写进真实的记忆库。要测 .env 的用例自己传 --env-file。
os.environ["LIORIUM_ENV_FILE"] = ""

_PATHS = [
    *sorted(
        str(pkg / "src") for pkg in (_REPO_ROOT / "packages").glob("*") if (pkg / "src").is_dir()
    ),
    str(_REPO_ROOT),
    str(_REPO_ROOT / "tests"),
]

for _path in reversed(_PATHS):
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture(autouse=True)
def _isolated_cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个测试都用临时 ``LIORIUM_HOME``：绝不读写开发机上的 ``~/.liorium``。

    2026-11-14 起 CLI 会**自动创建 / 迁移** ``profile.json``，任何走到配置路径的测试
    都会污染真实家目录（曾经把开发机的 config.json 迁走）。需要指定位置的测试可以
    在用例内部再 ``monkeypatch.setenv``，覆盖这里的默认值。
    """
    home = tmp_path / "liorium-home"
    monkeypatch.setenv("LIORIUM_HOME", str(home))
    return home


@pytest.fixture
def agent_workspace(tmp_path: Path) -> Path:
    """最小 Agent 工作区：AGENT.md / ROLE.md + 一个可读文件。"""
    from liorium_testkit import write_workspace

    return write_workspace(tmp_path)
