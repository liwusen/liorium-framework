"""真模型支持（``@pytest.mark.live``）。

密钥不落库、不进仓库：从环境变量读，默认指向 OpenCode Go 端点。
本地跑法：

```powershell
$env:OPENCODE_GO_KEY = (Get-Content ~/.liorium/providers.json -Raw |
  ConvertFrom-Json).providers | Where-Object name -eq 'Go' | ForEach-Object key
$env:OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
$env:OPENCODE_GO_MODEL = "deepseek-v4.1-flash"
.\scripts\test.ps1 -q -m live
```
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"


def live_key() -> str:
    return os.environ.get("OPENCODE_GO_KEY", "").strip()


def live_config() -> dict[str, str]:
    return {
        "base_url": os.environ.get("OPENCODE_GO_BASE_URL", DEFAULT_BASE_URL).strip(),
        "model": os.environ.get("OPENCODE_GO_MODEL", DEFAULT_MODEL).strip(),
        "key": live_key(),
    }


def require_live() -> dict[str, str]:
    """没有 key 时跳过（不静默跑空测试）。"""
    import pytest

    cfg = live_config()
    if not cfg["key"]:
        pytest.skip("缺少 OPENCODE_GO_KEY，跳过真模型用例")
    return cfg


def make_live_factory() -> Any:
    """返回 ``model_factory``：用 OpenCode Go 的 OpenAI 兼容端点。

    OpenCode Go 要求每个会话带稳定的 ``x-opencode-session`` 头（用于路由与 prompt
    缓存），否则 400。这里一个进程内固定一个 session id，与既有行为一致。
    """
    cfg = require_live()
    from uuid import uuid4

    from langchain_openai import ChatOpenAI

    session_id = os.environ.get("OPENCODE_GO_SESSION") or uuid4().hex
    headers = {"x-opencode-session": session_id}

    def factory(label: str) -> ChatOpenAI:
        model_name = cfg["model"]
        if label and "::" in label:
            model_name = label.split("::", 1)[1]
        return ChatOpenAI(
            model=model_name,
            base_url=cfg["base_url"],
            api_key=cfg["key"],
            temperature=0.2,
            timeout=120,
            max_retries=1,
            default_headers=headers,
        )

    return factory


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "live_config", "live_key", "make_live_factory", "require_live"]
