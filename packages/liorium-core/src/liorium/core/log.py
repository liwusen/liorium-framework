"""极简结构化日志。

只依赖标准库；库内所有日志走 ``liorium.*`` 命名空间，宿主可自行配置 handler。
"""

from __future__ import annotations

import logging
import os
from typing import Any

LOGGER_PREFIX = "liorium"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured = False


def configure(level: int | str | None = None) -> None:
    """给 ``liorium`` 根 logger 挂一个 stderr handler（幂等，宿主可覆盖）。

    级别优先级：显式参数 > ``LIORIUM_LOG_LEVEL`` 环境变量 > ``INFO``。
    """
    global _configured
    root = logging.getLogger(LOGGER_PREFIX)
    if _configured:
        if level is not None:
            root.setLevel(level)
        return
    if level is None:
        level = os.environ.get("LIORIUM_LOG_LEVEL", "INFO")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        root.addHandler(handler)
    root.propagate = False
    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    """取 ``liorium.<name>`` logger；``name`` 省略时返回根 logger。"""
    if not name:
        return logging.getLogger(LOGGER_PREFIX)
    if name == LOGGER_PREFIX or name.startswith(LOGGER_PREFIX + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_PREFIX}.{name}")


def kv(**fields: Any) -> str:
    """把字段拼成 ``k=v`` 串，供日志行尾使用。"""
    return " ".join(f"{k}={v!r}" for k, v in fields.items())


__all__ = ["LOGGER_PREFIX", "configure", "get_logger", "kv"]
