"""多模态桥：把消息里的 ``artifact://<id>`` 引用还原成图片块。

计划 §4.1：位于基座链最外层（紧贴 realm），**永远打开**。
详细规格见 ``docs/design/vfs-output-mw.md`` §5。

三条硬约束（其中前两条是历史事故的回归）：

1. **永不删除历史消息**：不产生 ``RemoveMessage``、不动源 ``ToolMessage``。
   删掉 ``ToolMessage`` 会让它的 ``AIMessage(tool_calls)`` 悬空，上游立刻
   400（``An assistant message with 'tool_calls' must be followed by tool
   messages``）。图片收敛交给压缩中间件连 tool 组一起整理。
2. **注入幂等**：桥接消息带 ``_mm_bridge_source`` 标记（源 ``ToolMessage`` 的
   稳定 key），同一源最多一条桥接消息；进程重启、重复扫描、压缩后重注入都
   不会重复。
3. **扫描范围 = 请求全量消息**（没有「回看 N 条」上限），只处理
   ``ToolMessage``。

与既有实现的设计差异：缩放阈值不再从产品配置读，改为构造参数
``max_pixels``（默认 ``2_000_000``，``0`` 关闭缩放）；Pillow 是可选依赖，
不可用或解码失败时原样透传，绝不因此报错。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage

#: 桥接消息上标记源 ``ToolMessage`` 的 ``additional_kwargs`` 键（幂等去重依据）。
SOURCE_KEY_FIELD = "_mm_bridge_source"
#: 桥接消息自身的标记（测试 / 诊断用）。
GENERATED_FIELD = "_mm_bridge_generated"
#: 缩放阈值默认值（``vfs-output-mw.md`` §5.1：2_000_000 像素）。
DEFAULT_MAX_PIXELS = 2_000_000
#: 图片未声明 MIME 时的回退值。
DEFAULT_IMAGE_MIME = "image/png"


def _collect_urls(images: Any) -> list[str]:
    """把 ``images`` 字段规范成 data/URL 字符串列表。

    支持三种元素形态：非空字符串、``{"url": ...}``、
    ``{"data": bytes, "mime": ...}``（Liorium 的规范形态）。
    """
    urls: list[str] = []
    for image in images or []:
        if isinstance(image, str) and image:
            urls.append(image)
        elif isinstance(image, Mapping):
            url = image.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
                continue
            data = image.get("data")
            if isinstance(data, (bytes, bytearray)):
                mime = str(image.get("mime") or DEFAULT_IMAGE_MIME)
                encoded = base64.b64encode(bytes(data)).decode("ascii")
                urls.append(f"data:{mime};base64,{encoded}")
    return urls


class MultimodalBridgeMiddleware(AgentMiddleware):
    """把工具结果里的 artifact 引用还原成 ``image_url`` 块。"""

    def __init__(self, artifacts: Any, *, max_pixels: int = DEFAULT_MAX_PIXELS) -> None:
        super().__init__()
        self.artifacts = artifacts
        self.max_pixels = max(0, int(max_pixels))

    # ── 入口（同步 / 异步都支持；异步直接委托同步实现）─────────────────

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        if not messages:
            return None

        bridged = self._bridged_source_keys(messages)
        additions: list[HumanMessage] = []

        # 始终扫描全量消息：较早的图片也要有机会进入模型上下文。
        # 幂等去重让已桥接的源几乎零成本跳过，昂贵的取图/缩放只发生在未桥接项上。
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                continue
            source_key = self._tool_message_key(message)
            if source_key in bridged:
                continue
            payload = self._parse_tool_payload(message)
            if payload is None:
                continue
            bridged_message = self._payload_to_mm_message(payload, source_key)
            if bridged_message is None:
                continue
            bridged.add(source_key)
            additions.append(bridged_message)

        if not additions:
            return None
        # reversed 扫描得到的是时间倒序，这里恢复成时间顺序。
        additions.reverse()
        return {"messages": additions}

    async def abefore_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    # ── 幂等键 / payload 识别 ────────────────────────────────────────────

    @staticmethod
    def _bridged_source_keys(messages: list[Any]) -> set[str]:
        """上下文里已存在的桥接消息对应的源 key（遍历全部消息、不限类型）。"""
        keys: set[str] = set()
        for message in messages:
            kwargs = getattr(message, "additional_kwargs", None) or {}
            source = kwargs.get(SOURCE_KEY_FIELD)
            if source:
                keys.add(str(source))
        return keys

    @staticmethod
    def _parse_tool_payload(message: ToolMessage) -> dict[str, Any] | None:
        """工具消息内容 → 多模态 payload；不是该形态返回 ``None``。"""
        content = message.content
        if isinstance(content, dict):
            data = content
        elif isinstance(content, str):
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return None
        else:
            return None
        if not isinstance(data, dict) or data.get("kind") != "multimodal_tool_result":
            return None
        return data

    @staticmethod
    def _tool_message_key(message: ToolMessage) -> str:
        """源 ``ToolMessage`` 的稳定 key（幂等去重依据）。

        优先用消息 id；缺 id 时用 ``tool_call_id + 内容 sha256``（跨进程稳定，
        因此进程重启后仍能识别「这张图已经桥接过了」）。
        """
        if message.id is not None:
            return str(message.id)
        digest = hashlib.sha256(str(message.content).encode("utf-8")).hexdigest()[:16]
        return f"fallback:{message.tool_call_id}:{digest}"

    # ── payload → HumanMessage ──────────────────────────────────────────

    def _payload_to_mm_message(
        self, payload: dict[str, Any], source_key: str
    ) -> HumanMessage | None:
        blocks: list[dict[str, Any]] = []
        if payload.get("text"):
            blocks.append({"type": "text", "text": str(payload["text"])})
        for url in self._image_urls(payload):
            resized = self._maybe_resize_image_url(url)
            if resized:
                blocks.append({"type": "image_url", "image_url": {"url": resized}})
        if not any(block["type"] == "image_url" for block in blocks):
            # 一张可用图片都没有 → 不注入（ToolMessage 里已有描述文本）。
            return None
        return HumanMessage(
            content=blocks,
            additional_kwargs={
                GENERATED_FIELD: True,
                SOURCE_KEY_FIELD: source_key,
            },
        )

    def _image_urls(self, payload: dict[str, Any]) -> list[str]:
        """收集 payload 里的图片 URL；工具只带引用时从 store 取回图片本体。

        ``artifact`` 字段既接受裸 id（``read`` 的产物），也接受
        ``artifact://<id>`` / ``artifacts://<id>`` 两种写法。
        """
        urls = _collect_urls(payload.get("images"))
        if urls:
            return urls
        artifact_id = str(payload.get("artifact") or "").strip()
        for prefix in ("artifacts://", "artifact://"):
            if artifact_id.startswith(prefix):
                artifact_id = artifact_id[len(prefix) :]
                break
        artifact_id = artifact_id.strip("/")
        if not artifact_id:
            return []
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            # artifact 已被清理（新对话/重启）：安静跳过，Agent 仍可 read 复看。
            return []
        for image in artifact.images:
            data = image.get("data")
            if isinstance(data, (bytes, bytearray)):
                mime = str(image.get("mime") or DEFAULT_IMAGE_MIME)
                urls.append(
                    f"data:{mime};base64,{base64.b64encode(bytes(data)).decode('ascii')}"
                )
        if urls:
            return urls
        if artifact.content_base64:
            mime = artifact.mime_type or DEFAULT_IMAGE_MIME
            return [f"data:{mime};base64,{artifact.content_base64}"]
        return []

    def _maybe_resize_image_url(self, url: str) -> str | None:
        """``data:`` URL 超过 ``max_pixels`` 时缩放；其它情况原样返回。

        只支持 ``data:;base64`` URL；``http``/``file`` 一律透传。Pillow 不可用
        或任何解码/缩放异常 → 原样返回，绝不让桥接失败。
        """
        if not url or self.max_pixels <= 0:
            return url or None
        if not url.startswith("data:"):
            return url
        try:
            header, separator, encoded = url.partition(",")
            if not separator or ";base64" not in header:
                return url
            from PIL import Image  # noqa: PLC0415 - 可选依赖，用到才导入

            raw = base64.b64decode(encoded)
            image = Image.open(io.BytesIO(raw))
            width, height = image.size
            if width * height <= self.max_pixels:
                return url
            scale = math.sqrt(self.max_pixels / float(width * height))
            new_width = max(1, int(width * scale))
            new_height = max(1, int(height * scale))
            resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image_format = resized.format or "PNG"
            if image_format.upper() not in ("PNG", "JPEG", "WEBP"):
                image_format = "PNG"
            resized.save(buffer, format=image_format)
            mime = header[len("data:") :].replace(";base64", "")
            return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
        except Exception:  # noqa: BLE001 - 缺 Pillow / 坏图片都原样透传
            return url


__all__ = [
    "DEFAULT_MAX_PIXELS",
    "GENERATED_FIELD",
    "SOURCE_KEY_FIELD",
    "MultimodalBridgeMiddleware",
]
