"""``liorium.realm_base`` —— realm 契约与写作 API。

刻意**不**依赖 langchain / langgraph：写一个 realm 只需要 ``liorium-core`` + ``liorium-vfs``；
用 ``@realm_tool`` 提供工具时再装 ``liorium-realm-base[tools]``（langchain-core）。

- 契约：``Realm`` / ``BaseRealm`` / ``RealmContext`` / ``TurnInfo``
- 状态：``JsonStateStore`` / ``RealmTaskScope``
- 写作 API：``@realm_tool`` / ``BaseRealm.save()`` / ``on_start`` / ``on_stop``

``liorium.agent`` 依赖本包并原样 re-export 这些名字（旧导入路径仍然可用）。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .base import (
    BaseRealm,
    ContextSink,
    JsonStateStore,
    Realm,
    RealmContext,
    RealmRecord,
    RealmTaskScope,
    TurnInfo,
    utc_stamp,
)
from .tools import build_realm_tools, marked_methods, realm_tool

__all__ = [
    "BaseRealm",
    "ContextSink",
    "JsonStateStore",
    "Realm",
    "RealmContext",
    "RealmRecord",
    "RealmTaskScope",
    "TurnInfo",
    "build_realm_tools",
    "marked_methods",
    "realm_tool",
    "utc_stamp",
]
