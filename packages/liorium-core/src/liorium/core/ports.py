"""核心端口（Protocol）：库与工具之间唯一允许的跨包契约。

``liorium-core`` 不 import ``liorium-vfs``，所以工具需要的非核心能力都以协议在这里声明，
真实实现在各自包里（例如 ``liorium.agent.turn.output_store.OutputStore`` 实现
``ArtifactStore``）。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ArtifactStore(Protocol):
    """工具输出的完整内容存储（LLM 只看到摘要 + artifact 引用）。"""

    def put(
        self,
        text: str,
        *,
        tool_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """存一段文本，返回 artifact id。"""
        ...

    def put_multimodal(self, payload: Mapping[str, Any], *, tool_name: str = "") -> str:
        """存一份多模态结果（``{"text": ..., "images": [...]}``），返回 artifact id。"""
        ...

    def get(self, artifact_id: str) -> Any:
        """取回完整记录（不存在返回 None）。"""
        ...

    def summary(self, artifact_id: str) -> str:
        """给模型的截断摘要（含 ``artifact://<id>`` 引用）。"""
        ...

    def exists(self, artifact_id: str) -> bool:
        ...


@runtime_checkable
class Clock(Protocol):
    """时钟端口。所有与播放/时限相关的判断都在宿主侧，这里只用于时间戳。"""

    def now(self) -> float:
        ...


@runtime_checkable
class ProcessRunner(Protocol):
    """``execute`` 工具的进程执行端口（便于宿主替换沙箱/审计）。"""

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """返回 ``(returncode, stdout, stderr)``。"""
        ...


__all__ = ["ArtifactStore", "Clock", "ProcessRunner"]
