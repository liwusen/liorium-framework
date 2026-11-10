"""校验「tag ↔ 包版本」一致，供 publish workflow 使用。

用法::

    python scripts/check_version.py v0.1.0 liorium-core liorium-agent ...
    python scripts/check_version.py v0.1.0 --all      # 检查 workspace 里全部包

退出码非 0 表示版本不一致（构建阶段就应该失败，绝不发一个版本号对不上的包）。
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"


def package_version(package: str) -> str:
    pyproject = PACKAGES_DIR / package / "pyproject.toml"
    if not pyproject.is_file():
        raise SystemExit(f"找不到 {pyproject}")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def all_packages() -> list[str]:
    return sorted(path.parent.name for path in PACKAGES_DIR.glob("*/pyproject.toml"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 tag 与包版本一致")
    parser.add_argument("tag", help="发布 tag，例如 v0.1.0")
    parser.add_argument("packages", nargs="*", help="包目录名；配合 --all 可省略")
    parser.add_argument("--all", action="store_true", help="检查 workspace 里全部包")
    args = parser.parse_args(argv)

    expected = args.tag.strip().lstrip("v")
    packages = all_packages() if args.all else args.packages
    if not packages:
        parser.error("至少给一个包名，或使用 --all")

    bad: list[str] = []
    for package in packages:
        version = package_version(package)
        status = "ok" if version == expected else "MISMATCH"
        print(f"{package:<28} {version:<10} {status}")
        if version != expected:
            bad.append(f"{package}: pyproject {version} != tag {expected}")

    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
