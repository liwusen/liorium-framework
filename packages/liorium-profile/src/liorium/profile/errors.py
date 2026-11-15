"""profile 错误的统一出口：消息里带 JSON 路径，别让用户猜。"""

from __future__ import annotations


class ProfileError(RuntimeError):
    """profile 相关错误的基类。"""


class ProfileSchemaError(ProfileError):
    """schema / 取值不合法（含 ``${VAR}`` 未定义、``id`` 缺失）。"""


class ProfileSourceError(ProfileError):
    """realm 的 ``source`` 解析失败（导入 / 取属性 / 类契约）。"""


class ProfileBuildError(ProfileError):
    """``build()`` 阶段失败（provider / 模型 / 路径）。"""


__all__ = [
    "ProfileBuildError",
    "ProfileError",
    "ProfileSchemaError",
    "ProfileSourceError",
]
