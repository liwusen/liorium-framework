"""``tests/`` 目录的夹具（``agent_workspace`` 在仓库根 conftest 里）。

假模型等工具函数在 ``tests/liorium_testkit.py``（避免多个同名 ``conftest`` 模块冲突）。
真模型用例统一走 ``tests/test_live_*.py`` 并打 ``@pytest.mark.live``。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage


@pytest.fixture
def tool_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read", "args": {"uri": "notes.txt"}, "id": "call_1", "type": "tool_call"}
        ],
    )
