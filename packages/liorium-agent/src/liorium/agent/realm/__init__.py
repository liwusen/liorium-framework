"""realm 子系统。

- ``Realm`` / ``BaseRealm`` / ``RealmContext`` / ``JsonStateStore`` / ``TurnInfo`` /
  ``realm_tool`` —— 契约与写作 API，实体在 ``liorium.realm_base``（本包 re-export）；
- ``RealmManager`` —— 装配、拓扑排序、start/stop、冲突报错、失败降级（本包实现）。
"""

from __future__ import annotations

from liorium.realm_base import (
    BaseRealm,
    ContextSink,
    JsonStateStore,
    Realm,
    RealmContext,
    RealmRecord,
    RealmTaskScope,
    TurnInfo,
    realm_tool,
)

from .manager import RealmManager

__all__ = [
    "BaseRealm",
    "ContextSink",
    "JsonStateStore",
    "Realm",
    "RealmContext",
    "RealmManager",
    "RealmRecord",
    "RealmTaskScope",
    "TurnInfo",
    "realm_tool",
]
