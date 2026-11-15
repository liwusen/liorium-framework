"""``MemoryRealm.Config``：配置驱动自组装（2026-11-14 决策 C1/C5）。

字段与语义见 ``docs/plans/2026-11-14-agent-profile.md`` §4.2：

- ``dir``：记忆目录（相对 ``ctx.inbox_dir``），默认 ``<inbox>/memory``；
  ``db_path`` / ``content_dir`` 是旧宿主的兼容入口，不能与 ``dir`` 同时给；
- ``embed``：语义检索的凭据（**不跟随主 provider**，realm 看不到 registry）；
- ``vectors`` / ``extraction`` / ``keyword_index`` / ``list_limit`` 与旧 ``params`` 同名。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbedConfig(BaseModel):
    """OpenAI 兼容 embedding 端点（``OpenAICompatibleEmbeddings`` 的三个必填入参）。

    ``key`` 是 0.3.0 的**历史误写字段**：``setup memory set embed-key`` 把 ``embed_key``
    截成 ``key`` 写进 profile，而这里的字段叫 ``api_key`` ⇒ 写过一次就再也 load 不起来
    （``extra="forbid"`` 报 "Extra inputs are not permitted"）。0.3.1 修好了写入端，这里
    继续接受旧文件里的 ``key`` 并映射到 ``api_key``：严格性（决策 D2）针对的是"用户拼错的
    键"，不该让我们自己的历史 bug 把旧 profile 全部打死。
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_key(cls, data: Any) -> Any:
        """``key`` → ``api_key``（``api_key`` 已有非空值时以它为准，``key`` 直接丢弃）。"""
        if not isinstance(data, Mapping) or "key" not in data:
            return data
        payload = {name: value for name, value in data.items() if name != "key"}
        if not payload.get("api_key"):
            payload["api_key"] = data["key"]
        return payload


class MemoryConfig(BaseModel):
    """``MemoryRealm`` 的配置：所有字段都有默认值，"最少配置"必须能通过校验。"""

    model_config = ConfigDict(extra="forbid")

    dir: str | None = None
    db_path: str | None = None
    content_dir: str | None = None
    vectors: bool = True
    embed: EmbedConfig | None = None
    extraction: bool = True
    list_limit: int = Field(default=200, ge=1)
    keyword_index: bool = True

    @model_validator(mode="after")
    def _check_layout(self) -> "MemoryConfig":
        if self.dir is not None and (self.db_path is not None or self.content_dir is not None):
            raise ValueError("dir 不能与 db_path / content_dir 同时给出（二选一）")
        return self


__all__ = ["EmbedConfig", "MemoryConfig"]
