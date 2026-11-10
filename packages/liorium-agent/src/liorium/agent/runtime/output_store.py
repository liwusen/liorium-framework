"""工具输出完整内容存储（``artifact://<id>``）。

计划 §4.5 / D8：工具结果的**图片只进 store**，消息文本里只留 ``artifact://<id>``
引用，由 ``MultimodalBridgeMiddleware`` 在请求期还原成图片块。冻结文本与行为见
``docs/design/vfs-output-mw.md`` §4。

要点：

- id 形如 ``{短名}_{递增计数}``（``shell_1`` / ``py_2`` / ``tool_3``）；
- ``summary()`` 给模型看：60000 字符 / 300 行上限 + 逐字 footer；
- 图片以 **bytes** 存内存；落盘时 base64，读回时还原成 bytes；
- ``root=None`` 时纯内存（默认）；给定 ``root`` 时写 ``<root>/artifact.json``，
  采用「同目录临时文件 + ``os.replace``」的原子写。

与既有实现的设计差异（见 ``vfs-output-mw.md`` §1.12 / §4.5）：

- 图片在内存里是 ``bytes``，不是 data URL 字符串；``metadata`` 里不再塞
  ``images``（不可 JSON 序列化），多图信息由 ``Artifact.images`` 承载；
- 不读产品配置、不做单例：作用域由构造方（Agent 实例）决定。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 摘要的字符上限（``vfs-output-mw.md`` §4.4 冻结常量）。
DEFAULT_MAX_SUMMARY = 60000
#: 摘要的行数上限（同上）。
DEFAULT_MAX_LINES = 300

#: 截断 footer（逐字冻结；半角逗号与句点，不要「修正」成中文标点）。
TRUNCATION_FOOTER = "[内容过长,已经截断.请使用行数选择器读取完整输出: artifact://{output_id}]"

#: 图片未声明 MIME 时的回退值。
DEFAULT_IMAGE_MIME = "image/png"

#: 已知工具的短名（``vfs-output-mw.md`` §4.2）。
_KNOWN_SHORT_NAMES = {
    "sys_exec": "shell",
    "python_exec": "py",
    "read_text_file": "read",
    "write_text_file": "write",
    "list_directory": "ls",
    "memory_search": "mem",
    "request_human_approval": "hil",
    "show_nimble_window": "nimble",
}

_TOOL_NAME_SUFFIXES = ("Tool", "_tool", "Func")


def _short_tool_name(tool_name: str) -> str:
    """把工具名投影成 artifact id 用的短名。

    ``strip()`` 后为空 → ``tool``；去掉 ``Tool`` / ``_tool`` / ``Func`` 后缀；
    查表；否则取小写前 12 个字符。
    """
    name = str(tool_name or "").strip() or "tool"
    for suffix in _TOOL_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    name = name.strip() or "tool"
    short = _KNOWN_SHORT_NAMES.get(name.lower())
    if short is not None:
        return short
    return name.lower()[:12] or "tool"


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    """解出 ``data:<mime>;base64,<payload>``；不是这种形态返回 ``None``。"""
    if not url.startswith("data:"):
        return None
    header, separator, payload = url.partition(",")
    if not separator or ";base64" not in header:
        return None
    mime = header[len("data:") :].replace(";base64", "").strip() or DEFAULT_IMAGE_MIME
    try:
        return base64.b64decode(payload), mime
    except (ValueError, TypeError):
        return None


def _normalize_image(image: Any) -> dict[str, Any] | None:
    """把一条图片条目规范成 ``{"data": bytes, "mime": str}``。

    规范形态是 ``{"data": bytes, "mime": "image/png"}``；同时兼容工具历史产物
    ``{"url": "data:image/png;base64,..."}``（解码一次后只留 bytes）。无法识别
    的条目返回 ``None``，由调用方跳过。
    """
    if isinstance(image, Mapping):
        mime = str(image.get("mime") or "")
        data = image.get("data")
        if isinstance(data, (bytes, bytearray)):
            return {"data": bytes(data), "mime": mime or DEFAULT_IMAGE_MIME}
        url = image.get("url")
        if isinstance(url, str):
            decoded = _decode_data_url(url)
            if decoded is not None:
                raw, url_mime = decoded
                return {"data": raw, "mime": mime or url_mime}
        return None
    if isinstance(image, str):
        decoded = _decode_data_url(image)
        if decoded is not None:
            raw, url_mime = decoded
            return {"data": raw, "mime": url_mime}
    return None


@dataclass
class Artifact:
    """一份完整的工具输出。"""

    output_id: str
    content: str = ""
    tool_name: str = ""
    content_type: str = "text"
    """``"text"`` / ``"image"`` / ``"multimodal"``，决定 ``summary()`` 分支。"""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    images: list[dict[str, Any]] = field(default_factory=list)
    """图片本体，元素为 ``{"data": bytes, "mime": str}``（内存形态）。"""

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def content_base64(self) -> str | None:
        """首张图片的 base64（不含 ``data:`` 头）；无图片时 ``None``。"""
        if not self.images:
            return None
        return base64.b64encode(self.images[0]["data"]).decode("ascii")

    @property
    def mime_type(self) -> str | None:
        """首张图片的 MIME；无图片时 ``None``。"""
        if not self.images:
            return None
        return str(self.images[0].get("mime") or DEFAULT_IMAGE_MIME)

    @property
    def size(self) -> int:
        """粗估字节数（文本长度 + 首图 base64 折算）。"""
        return len(self.content) + (len(self.content_base64 or "") // 4 * 3)

    @property
    def lines(self) -> int:
        return self.content.count("\n") + (1 if self.content else 0)

    def summary(
        self,
        max_chars: int = DEFAULT_MAX_SUMMARY,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> str:
        """给模型看的截断预览（``vfs-output-mw.md`` §4.4 逐字模板）。"""
        if self.content_type == "multimodal":
            count = self.metadata.get("image_count", 0)
            description = self.content or "图片输出"
            return f"[{description} — {count}张图片: artifact://{self.output_id}]"
        if self.content_type == "image":
            return f"[图片: artifact://{self.output_id}]"
        if not self.content:
            return "(空输出)"
        lines = self.content.split("\n")
        if len(lines) <= max_lines and len(self.content) <= max_chars:
            return self.content
        preview = "\n".join(lines[:max_lines])
        if len(preview) > max_chars:
            preview = preview[:max_chars] + "…"
        footer = TRUNCATION_FOOTER.format(output_id=self.output_id)
        return preview + "\n" + footer

    def get(self, offset: int = 0, limit: int | None = None) -> str:
        """按行取全文（``offset`` 0 起；图片不在这里还原成 data URL）。"""
        if not self.content:
            return ""
        lines = self.content.split("\n")
        if limit is None:
            return "\n".join(lines[offset:])
        return "\n".join(lines[offset : offset + limit])

    def to_dict(self) -> dict[str, Any]:
        """持久化形态：图片以 base64 落盘。"""
        return {
            "output_id": self.output_id,
            "content": self.content,
            "tool_name": self.tool_name,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "images": [
                {
                    "mime": str(image.get("mime") or DEFAULT_IMAGE_MIME),
                    "base64": base64.b64encode(image["data"]).decode("ascii"),
                }
                for image in self.images
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Artifact:
        """从持久化形态重建（图片 base64 → bytes）。"""
        images: list[dict[str, Any]] = []
        for item in list(payload.get("images") or []):
            if not isinstance(item, Mapping):
                continue
            encoded = item.get("base64")
            if not isinstance(encoded, str) or not encoded:
                continue
            try:
                raw = base64.b64decode(encoded)
            except (ValueError, TypeError):
                continue
            if not raw:
                continue
            images.append(
                {
                    "data": raw,
                    "mime": str(item.get("mime") or DEFAULT_IMAGE_MIME),
                }
            )
        return cls(
            output_id=str(payload.get("output_id") or ""),
            content=str(payload.get("content") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            content_type=str(payload.get("content_type") or "text"),
            metadata=dict(payload.get("metadata") or {}),
            created_at=float(payload.get("created_at") or time.time()),
            images=images,
        )


class OutputStore:
    """工具输出存储：实现 ``liorium.core.ports.ArtifactStore`` 协议。

    ``root=None``（默认）时纯内存；给定目录时把全部 artifact 写进
    ``<root>/artifact.json``（原子替换），构造时自动读回。

    同步工具经 ``asyncio.to_thread`` 执行，因此写入路径带 ``RLock``：多个工具
    并发落盘时不会互相踩到临时文件或迭代中的字典。
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_chars: int = DEFAULT_MAX_SUMMARY,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self._max_chars = int(max_chars)
        self._max_lines = int(max_lines)
        self._artifacts: dict[str, Artifact] = {}
        self._counter = 0
        self._lock = threading.RLock()
        self._load()

    # ── 只读视图 ──────────────────────────────────────────────────────────

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def storage_path(self) -> Path | None:
        """持久化文件路径；纯内存模式返回 ``None``。"""
        if self._root is None:
            return None
        return self._root / "artifact.json"

    @property
    def max_chars(self) -> int:
        return self._max_chars

    @property
    def max_lines(self) -> int:
        return self._max_lines

    def get(self, artifact_id: str) -> Artifact | None:
        """按 id 取回完整记录；不存在返回 ``None``。"""
        return self._artifacts.get(artifact_id)

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._artifacts

    def summary(self, artifact_id: str) -> str:
        """给模型的截断摘要；未知 id 返回冻结文本 ``[找不到 artifact: ...]``。"""
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return f"[找不到 artifact: {artifact_id}]"
        return artifact.summary(self._max_chars, self._max_lines)

    def peek(self, artifact_id: str) -> dict[str, Any] | None:
        """只含元信息（不含正文）的视图。"""
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            return None
        return {
            "output_id": artifact.output_id,
            "tool_name": artifact.tool_name,
            "size": artifact.size,
            "lines": artifact.lines,
            "created_at": artifact.created_at,
        }

    def list_ids(self) -> list[str]:
        """全部 id，按 ``created_at`` 升序（最新的在最后）。"""
        with self._lock:
            return sorted(self._artifacts, key=lambda key: self._artifacts[key].created_at)

    def __contains__(self, artifact_id: object) -> bool:
        return artifact_id in self._artifacts

    def __len__(self) -> int:
        with self._lock:
            return len(self._artifacts)

    # ── 写入 / 重置 ──────────────────────────────────────────────────────

    def put(
        self,
        text: str,
        *,
        tool_name: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """存一段文本，返回 artifact id（``{短名}_{计数}``）。"""
        with self._lock:
            artifact = Artifact(
                output_id=self._next_id(tool_name),
                content=str(text or ""),
                tool_name=str(tool_name or ""),
                metadata=dict(metadata or {}),
            )
            self._artifacts[artifact.output_id] = artifact
            self._save()
            return artifact.output_id

    def put_multimodal(self, payload: Mapping[str, Any], *, tool_name: str = "") -> str:
        """存一份多模态结果：``{"text": ..., "images": [{"data": bytes, "mime": ...}]}``。"""
        images: list[dict[str, Any]] = []
        for raw in list(payload.get("images") or []):
            normalized = _normalize_image(raw)
            if normalized is not None:
                images.append(normalized)
        metadata: dict[str, Any] = {"image_count": len(images)}
        meta = payload.get("meta")
        if isinstance(meta, Mapping):
            metadata["meta"] = dict(meta)
        with self._lock:
            artifact = Artifact(
                output_id=self._next_id(tool_name),
                content=str(payload.get("text") or ""),
                tool_name=str(tool_name or ""),
                content_type="multimodal",
                metadata=metadata,
                images=images,
            )
            self._artifacts[artifact.output_id] = artifact
            self._save()
            return artifact.output_id

    def reset(self) -> None:
        """清空全部 artifact 并把计数归零（新对话 / 清空会话）。

        持久化模式下写回空 payload（而不是删文件），使重新打开时状态一致。
        """
        with self._lock:
            self._artifacts.clear()
            self._counter = 0
            self._save()

    def clear(self) -> None:
        """``reset()`` 的别名（与既有实现的 ``clear()`` 调用点对齐）。"""
        self.reset()

    # ── 内部 ─────────────────────────────────────────────────────────────

    def _next_id(self, tool_name: str) -> str:
        self._counter += 1
        return f"{_short_tool_name(tool_name)}_{self._counter}"

    def _load(self) -> None:
        path = self.storage_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 缺失 / 解析失败都静默退回空 store（规格 §4.3）。
            return
        if not isinstance(payload, Mapping):
            return
        try:
            self._counter = int(payload.get("counter") or 0)
        except (TypeError, ValueError):
            self._counter = 0
        self._artifacts = {}
        for item in list(payload.get("artifacts") or []):
            if not isinstance(item, Mapping):
                continue
            try:
                artifact = Artifact.from_dict(item)
            except (TypeError, ValueError):
                continue
            if artifact.output_id:
                self._artifacts[artifact.output_id] = artifact

    def _save(self) -> None:
        path = self.storage_path
        if path is None:
            return
        payload = {
            "counter": self._counter,
            "artifacts": [artifact.to_dict() for artifact in self._artifacts.values()],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)


__all__ = [
    "DEFAULT_IMAGE_MIME",
    "DEFAULT_MAX_LINES",
    "DEFAULT_MAX_SUMMARY",
    "TRUNCATION_FOOTER",
    "Artifact",
    "OutputStore",
]
