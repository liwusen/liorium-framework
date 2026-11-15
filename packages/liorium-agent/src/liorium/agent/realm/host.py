"""``RealmHost`` 的 Agent 侧实现：realm 主动"推"的落点（2026-11-14）。

一个 realm 一份（绑定 ``realm_id``），由 :class:`~liorium.agent.realm.manager.RealmManager`
在 ``start()`` 时注入到 ``RealmContext.host``。契约在 ``liorium.realm_base.RealmHost``
（结构类型，本包不反向依赖）。

四个动作：

- ``emit``：往事件流发一帧 ``realm``。**不开回合、不是终态**、不落 checkpoint；
  从回合任务的上下文里发出时自动带 ``turn_id``（后台任务通常没有，符合"它不属于那个回合"）；
- ``push``：投一条入站条目，完全复用 ``LioriumAgent.push()``（``input.*`` 帧、timeout、
  同 id 覆盖语义全部一致），id 自动生成为 ``realm:<realm_id>:<n>``；
- ``interrupt``：叫停当前回合（``worker.interrupt``，同步）；
- ``turn_info``：当前回合身份（没有回合时为 ``None``）。

刻意**没有**任何改请求的能力：这里不碰 ``ModelRequest``，也不给 realm 暴露 graph。
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, Mapping

from liorium.core.errors import RealmError
from liorium.core.types import ev_realm

from ..runtime.inbox import Bucket
from ..runtime.state import CURRENT_TURN_ID

if TYPE_CHECKING:  # pragma: no cover
    from liorium.realm_base import TurnInfo

    from ..agent import LioriumAgent


class RealmHostPort:
    """一个 realm 的宿主端口（构造一次，随 ``RealmContext`` 存活）。"""

    def __init__(self, agent: "LioriumAgent", realm_id: str) -> None:
        self._agent = agent
        self._realm_id = realm_id
        self._seq = itertools.count(1)

    @property
    def realm_id(self) -> str:
        return self._realm_id

    # ------------------------------------------------------------------
    # RealmHost 协议
    # ------------------------------------------------------------------

    def emit(self, name: str, data: Mapping[str, Any]) -> None:
        """发一帧 ``realm`` 事件；``turn_id`` 取当前回合上下文（没有就不带）。"""
        self._agent._hub.publish(
            ev_realm(self._realm_id, name, dict(data), CURRENT_TURN_ID.get())
        )

    def push(
        self,
        text: str,
        *,
        bucket: str = "environment",
        origin: str = "",
        timeout: float | None = None,
    ) -> str:
        """投入站条目，返回自动生成的条目 id。"""
        try:
            bucket_value = Bucket(bucket)
        except ValueError as exc:  # 枚举的报错信息对 realm 作者不够直观
            allowed = " / ".join(item.value for item in Bucket)
            raise RealmError(
                f"realm {self._realm_id!r} 的 push() 收到未知 bucket {bucket!r}（允许：{allowed}）"
            ) from exc
        entry_id = f"realm:{self._realm_id}:{next(self._seq)}"
        self._agent.push(
            text,
            id=entry_id,
            bucket=bucket_value,
            origin=origin or f"realm:{self._realm_id}",
            timeout=timeout,
        )
        return entry_id

    def interrupt(self, reason: str = "realm") -> bool:
        """叫停当前回合；返回是否真的打断了。"""
        return self._agent._worker.interrupt(reason or "realm")

    def turn_info(self) -> "TurnInfo | None":
        """当前回合身份；没有回合在跑时为 ``None``。"""
        return self._agent._worker.current_turn_info


__all__ = ["RealmHostPort"]
