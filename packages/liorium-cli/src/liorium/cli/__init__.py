"""``liorium-cli``：真实模型 + 六个核心工具 + 记忆 / 技能 / 子代理的终端 Agent。"""

from __future__ import annotations

from typing import Sequence

__version__ = "0.1.1"


def main(argv: Sequence[str] | None = None) -> int:
    """控制台入口（也等价于 ``python -m liorium.cli``）。"""
    from .app import main as _main

    return _main(argv)


__all__ = ["__version__", "main"]
