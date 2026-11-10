"""校验「tag ↔ 发行版版本」一致，供 publish / ci workflow 使用。

发布单元只有一个：根项目 ``liorium``（12 个 workspace 成员的源码被 force-include 打进
同一个 wheel）。因此版本号只有一个真正生效的地方 —— 根 ``pyproject.toml`` ——
外加 ``src/liorium/__init__.py`` 里的 ``__version__`` 必须与它相同。

用法::

    python scripts/check_version.py v0.1.0          # 只查「发布单元」
    python scripts/check_version.py v0.1.0 --all    # 再加查 12 个 workspace 成员是否漂移

退出码非 0 表示版本不一致（构建阶段就应该失败，绝不发一个版本号对不上的包）。
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
INIT_FILE = REPO_ROOT / "src" / "liorium" / "__init__.py"

_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def project_version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def init_version() -> str:
    match = _VERSION_RE.search(INIT_FILE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{INIT_FILE} 里找不到 __version__")
    return match.group(1)


def member_versions() -> dict[str, str]:
    return {
        path.parent.name: project_version(path)
        for path in sorted(PACKAGES_DIR.glob("*/pyproject.toml"))
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 tag 与发行版版本一致")
    parser.add_argument("tag", help="发布 tag，例如 v0.1.0")
    parser.add_argument(
        "--all",
        action="store_true",
        help="额外检查 12 个 workspace 成员包的版本（它们不发布，但保持同步更省心）",
    )
    args = parser.parse_args(argv)

    expected = args.tag.strip().lstrip("v")
    bad: list[str] = []

    published = project_version(ROOT_PYPROJECT)
    init = init_version()
    print(f"{'liorium (pyproject)':<32} {published:<10} {'ok' if published == expected else 'MISMATCH'}")
    print(f"{'liorium (__init__)':<32} {init:<10} {'ok' if init == expected else 'MISMATCH'}")
    if published != expected:
        bad.append(f"pyproject {published} != tag {expected}")
    if init != expected:
        bad.append(f"src/liorium/__init__.py {init} != tag {expected}")

    if args.all:
        for name, version in member_versions().items():
            ok = version == expected
            print(f"{name + ' (workspace)':<32} {version:<10} {'ok' if ok else 'MISMATCH'}")
            if not ok:
                bad.append(f"{name} {version} != tag {expected}")

    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
