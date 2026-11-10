"""``liorium.todo_realm`` —— 状态化待办能力包（``[todo]`` extra）。

- ``TodoRealm``：两个工具（``todo`` 写 / ``todo_list`` 读）+ 状态 + prompt 片段；
- 第五拍：每 50 个回合注入一次未完成清单（只在有 ``pending`` / ``processing`` 时）；
- 数据落在 ``<inbox>/realm_state/todo.json``（``state_scope="agent"``）。

用法：

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.todo_realm import TodoRealm

agent = LioriumAgent(
    spec=AgentSpec(..., realms=[RealmSpec(id="todo")]),
    realm_factories={"todo": TodoRealm},
    ...
)
```

节奏：``RealmSpec(id="todo", params={"context_every": 10})`` 可覆盖默认的 50。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .model import (
    ACTIVE_STATES,
    CONTEXT_REMINDER,
    STATES,
    Todo,
    TodoState,
    parse_state,
    render_context,
    render_list,
)
from .realm import TodoRealm

__all__ = [
    "ACTIVE_STATES",
    "CONTEXT_REMINDER",
    "STATES",
    "Todo",
    "TodoRealm",
    "TodoState",
    "__version__",
    "parse_state",
    "render_context",
    "render_list",
]
