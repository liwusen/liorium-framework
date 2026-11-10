"""校验发布用的 wheel 里真的装了全部 12 个模块（CI / 本地都能跑）。

发布单元只有一个 ``liorium``，12 个模块靠 ``[tool.hatch.build.targets.wheel].force-include``
从各自的 ``packages/<包>/src/liorium/<子包>`` 注入。少写一条映射不会让构建失败，
只会让 wheel 里悄无声息地缺一个模块 —— 所以用一个断言把它钉住。

用法::

    python scripts/check_wheel.py dist/liorium-0.1.0-py3-none-any.whl
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"

#: 命令行入口（``[project.scripts]``）与它在 wheel 里的期望内容。
EXPECTED_SCRIPT = "liorium-cli = liorium.cli.app:main"


def expected_modules() -> dict[str, Path]:
    """``packages/*/src/liorium/<子包>`` → 以 import 名索引（``core`` / ``realm_base`` …）。"""
    modules: dict[str, Path] = {}
    for pattern in ("*/src/liorium/*",):
        for path in sorted(PACKAGES_DIR.glob(pattern)):
            if path.is_dir() and (path / "__init__.py").is_file():
                modules[path.name] = path
    return modules


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("用法: python scripts/check_wheel.py <wheel>", file=sys.stderr)
        return 2

    wheel = Path(args[0])
    if not wheel.is_file():
        print(f"找不到 wheel: {wheel}", file=sys.stderr)
        return 1

    expected = expected_modules()
    if not expected:
        print("没扫到任何 packages/*/src/liorium/<子包>，检查脚本本身", file=sys.stderr)
        return 1

    names = [name for name in zipfile.ZipFile(wheel).namelist() if not name.endswith("/")]
    files = set(names)
    problems: list[str] = []

    if "liorium/__init__.py" not in files:
        problems.append("wheel 里缺 liorium/__init__.py")

    for module in sorted(expected):
        if f"liorium/{module}/__init__.py" not in files:
            problems.append(f"wheel 里缺 liorium/{module}/__init__.py")

    leaked = [name for name in names if "/tests/" in name or name.startswith("tests/")]
    if leaked:
        problems.append(f"wheel 里混进了测试文件: {leaked[:5]}")

    dist_info = [name for name in names if name.endswith("entry_points.txt")]
    entry_points = zipfile.ZipFile(wheel).read(dist_info[0]).decode() if dist_info else ""
    if EXPECTED_SCRIPT not in entry_points:
        problems.append(f"entry_points.txt 里缺 `{EXPECTED_SCRIPT}`（实际: {entry_points!r}）")

    metadata = [name for name in names if name.endswith("METADATA")]
    if not metadata:
        problems.append("wheel 里缺 METADATA")
    else:
        text = zipfile.ZipFile(wheel).read(metadata[0]).decode()
        if "Name: liorium\n" not in text:
            problems.append("METADATA 里的 Name 不是 liorium")

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    print(f"wheel ok: {wheel.name} —— {len(expected)} 个模块 "
          f"({', '.join(sorted(expected))})，{len(names)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
