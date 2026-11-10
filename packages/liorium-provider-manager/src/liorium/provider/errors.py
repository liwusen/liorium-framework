"""Provider 配置错误。

``ProviderConfigError`` 是宿主可直接展示给用户的异常：配置文件缺失/损坏、
模型名歧义、格式无法确定、缺少 API key 都属于它。它同时继承 ``LioriumError``
（宿主统一捕获）与 ``ValueError``（兼容既有把配置错误当 ValueError 的调用方）。
"""

from __future__ import annotations

from liorium.core.errors import LioriumError


class ProviderConfigError(LioriumError, ValueError):
    """Provider 配置缺失、不合法或无法解析模型。"""


__all__ = ["ProviderConfigError"]
