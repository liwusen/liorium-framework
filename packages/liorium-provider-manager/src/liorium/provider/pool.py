"""模型池：懒建、按用途缓存、**归它关闭**。

为什么需要它（2026-11-14 决策 E2/E3）：

- 模型能不能关，只有造它的层知道。``close_chat_model`` 只关调用方显式注入的私有
  HTTP 客户端；langchain 默认客户端在模块级 ``lru_cache`` 里，关掉会让同 base_url
  的所有模型永久不可用。所以"关模型"必须由**工厂**负责，而不是对模型实例做鸭子类型关闭。
- Agent 拿到的 ``model_factory`` / ``compact_model_factory`` 就是本池的两个绑定视图
  （:attr:`ChatModelPool.chat` / :attr:`ChatModelPool.compact`）：调用即建、``aclose``
  即关，``LioriumAgent.close()`` 只认这两个工厂的 ``aclose``。

缓存键是 ``(label, intensity, purpose)``：``/thinking`` 改 ``pool.thinking`` 后下一次
``chat`` 自然拿到新模型；旧模型仍留在池里，由 ``aclose`` 一次关干净。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from liorium.core.log import get_logger

from .errors import ProviderConfigError
from .formats import build_chat_model, close_chat_model

log = get_logger("provider.pool")


@runtime_checkable
class ModelFactoryPort(Protocol):
    """模型工厂端口：``label -> model`` + ``aclose()``（``LioriumAgent`` 只认这两件事）。"""

    def __call__(self, label: str) -> Any: ...

    async def aclose(self) -> None: ...


class _BoundFactory:
    """绑定用途的工厂视图（``pool.chat`` / ``pool.compact``）。

    每次访问都新建一个轻量包装：``aclose`` 转发给池，池负责幂等。
    """

    def __init__(self, pool: "ChatModelPool", purpose: str) -> None:
        self._pool = pool
        self._purpose = purpose

    def __call__(self, label: str) -> Any:
        return self._pool(label, purpose=self._purpose)

    async def aclose(self) -> None:
        await self._pool.aclose()

    def __repr__(self) -> str:  # pragma: no cover - 只用于日志
        return f"<ModelFactoryPort purpose={self._purpose!r}>"


class ChatModelPool:
    """按 ``(label, intensity, purpose)`` 懒建并缓存聊天模型。"""

    def __init__(
        self,
        registry: Any,
        *,
        thinking: str = "medium",
        temperature: float = 0.3,
        api_format: str | None = None,
        timeout: float = 180.0,
        max_retries: int = 2,
        max_tokens: int = 8192,
    ) -> None:
        self.registry = registry
        self.thinking = thinking
        """思考强度；``/thinking`` 直接改它（下一次 ``chat`` 建新模型）。"""
        self.temperature = temperature
        self.api_format = api_format
        """强制 API 格式（``--api-format``）；``None`` = 由 provider / 模型名推断。"""
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self._models: dict[tuple[str, str | None, str], Any] = {}
        self._closed = False

    # -- 工厂视图 -------------------------------------------------------

    @property
    def chat(self) -> ModelFactoryPort:
        """主对话工厂（带思考强度）。"""
        return _BoundFactory(self, "chat")

    @property
    def compact(self) -> ModelFactoryPort:
        """aux / compact 工厂（强制不思考）。"""
        return _BoundFactory(self, "compact")

    # -- 建模型 ---------------------------------------------------------

    def __call__(self, label: str, *, purpose: str = "chat") -> Any:
        if self._closed:
            raise RuntimeError("ChatModelPool 已关闭；模型工厂不能再用")
        intensity = self.thinking if purpose == "chat" else None
        key = (str(label), intensity, purpose)
        cached = self._models.get(key)
        if cached is not None:
            return cached
        model = self._build(label, intensity=intensity, purpose=purpose)
        self._models[key] = model
        return model

    def _build(self, label: str, *, intensity: str | None, purpose: str) -> Any:
        resolved = self.registry.resolve(label, api_format=self.api_format)
        provider = resolved.provider
        if not provider.api_key:
            raise ProviderConfigError(
                f"provider {provider.name!r} 没有 API key；请检查配置或删掉配置后重新填写"
            )
        return build_chat_model(
            provider,
            resolved.model,
            intensity=intensity,
            temperature=self.temperature,
            timeout=self.timeout,
            max_retries=self.max_retries,
            max_tokens=self.max_tokens,
            purpose=purpose,
        )

    # -- 关闭 -----------------------------------------------------------

    async def aclose(self) -> None:
        """关掉池里所有模型（幂等）。关完再用 ⇒ ``RuntimeError``。"""
        if self._closed:
            return
        self._closed = True
        models = list(self._models.values())
        self._models.clear()
        for model in models:
            try:
                await close_chat_model(model)
            except Exception as exc:  # noqa: BLE001 - 关闭失败不能拖垮退出
                log.warning("关闭模型客户端失败: %s", exc)


__all__ = ["ChatModelPool", "ModelFactoryPort"]
