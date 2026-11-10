"""跨模块常量与请求头。

显式设置 User-Agent：不设置时 openai-python / httpx 会以自身 UA 发请求，部分网关
会据此做来源识别或拦截。OpenCode Go 还要求每个会话带稳定的 ``x-opencode-session``
头（用于路由与 prompt 缓存），进程生命周期内保持不变即可。
"""

from __future__ import annotations

import uuid
from typing import Mapping

DEFAULT_USER_AGENT = "liorium-provider-manager/0.1 (langchain)"

_OPENCODE_SESSION_ID = uuid.uuid4().hex


def opencode_headers(
    extra: Mapping[str, str] | None = None,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, str]:
    """返回带稳定 session 头的请求头；``extra`` 覆盖默认值。"""
    headers = {
        "x-opencode-session": _OPENCODE_SESSION_ID,
        "User-Agent": user_agent,
    }
    if extra:
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def common_headers(
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    opencode_go: bool = False,
) -> dict[str, str]:
    """所有格式共用的请求头（UA + 可选 OpenCode session）。"""
    if opencode_go:
        return opencode_headers(user_agent=user_agent)
    return {"User-Agent": user_agent}


__all__ = [
    "DEFAULT_USER_AGENT",
    "common_headers",
    "opencode_headers",
]
