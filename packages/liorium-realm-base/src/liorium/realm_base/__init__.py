"""``liorium.realm_base`` —— realm 契约与写作 API。

刻意**不**依赖 langchain / langgraph：写一个 realm 只需要 ``liorium.core`` + ``liorium.vfs``；
用 ``@realm_tool`` 提供工具时才碰 langchain-core（基础依赖，``pip install liorium`` 自带）。

- 契约：``Realm`` / ``BaseRealm`` / ``RealmContext`` / ``TurnInfo``
- 状态：``JsonStateStore`` / ``RealmTaskScope``
- 写作 API：``@realm_tool`` / ``BaseRealm.save()`` / ``on_start`` / ``on_stop``
- 主动推送（2026-11-14）：``RealmHost`` 端口 + ``ctx.emit/push/interrupt/turn``
- 只读钩子（2026-11-14）：``ModelCall`` / ``ToolCall`` / ``HookOutcome`` / ``TurnOutcome``
- 宿主消息（2026-11-14）：``RealmMessage`` + ``BaseRealm.on_message()``

``liorium.agent`` 依赖本包并原样 re-export 这些名字（旧导入路径仍然可用）。
"""

from __future__ import annotations

__version__ = "0.1.1"

from .base import (
    BaseRealm,
    ContextSink,
    HookOutcome,
    JsonStateStore,
    ModelCall,
    Realm,
    RealmContext,
    RealmHost,
    RealmMessage,
    RealmRecord,
    RealmTaskScope,
    ToolCall,
    TurnInfo,
    TurnOutcome,
    overrides_hook,
    utc_stamp,
)
from .tools import build_realm_tools, marked_methods, realm_tool

__all__ = [
    "BaseRealm",
    "ContextSink",
    "HookOutcome",
    "JsonStateStore",
    "ModelCall",
    "Realm",
    "RealmContext",
    "RealmHost",
    "RealmMessage",
    "RealmRecord",
    "RealmTaskScope",
    "ToolCall",
    "TurnInfo",
    "TurnOutcome",
    "overrides_hook",
    "build_realm_tools",
    "marked_methods",
    "realm_tool",
    "utc_stamp",
]
