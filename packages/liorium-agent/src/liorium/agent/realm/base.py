"""realm 契约的**旧入口**：实体已搬到 :mod:`liorium.realm_base`（2026-11-07）。

保留本模块是为了让 ``from liorium.agent.realm.base import JsonStateStore`` 这类历史导入
继续可用（能力包的测试就是这样导入的）。新代码请直接 ``from liorium.realm_base import ...``。
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
    utc_stamp,
)

__all__ = [
    "BaseRealm",
    "ContextSink",
    "JsonStateStore",
    "Realm",
    "RealmContext",
    "RealmRecord",
    "RealmTaskScope",
    "TurnInfo",
    "utc_stamp",
]
