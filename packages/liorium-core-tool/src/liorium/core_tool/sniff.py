"""字节 → 文本的编码嗅探（``read`` / ``search`` 共用）。

**为什么需要**（2026-11-08，宿主实测）：本地编码的**纯文本**被整份判成"二进制"。
``read`` 原来的判定是「前 8KiB 有 NUL **或**整份不是合法 UTF-8 ⇒ 二进制」，而输出里
``类型:`` 来自 ``mimetypes.guess_type(文件名)``（只看扩展名），两条判据来源不同，于是
``类型: text/plain`` 与 ``[二进制: xxx.txt]`` 同时出现 —— 自相矛盾，而且模型因此读不到
pytest 的 traceback，只能退回 shell 的 ``type``。

三种高频触发源：

- ``>`` 重定向出来的 **UTF-16LE**（ASCII 字符后面跟一个 ``\\x00``，几乎必然是
  "含 NUL"的那一类）；
- 中文 Windows 下 **GBK/CP936** 的报错文本（pytest 输出里夹着的系统消息）；
- 日志/抓屏里零散的 NUL 或非法字节。

**判定顺序**：BOM → （含 NUL）UTF-16 → UTF-8 → GB18030 → 带替换的 UTF-8 → 二进制。
GB18030 是 CP936/GBK 的超集、对纯 ASCII 也成立，所以必须排在 UTF-8 之后、
二进制之前；它能"解"出几乎任意字节序列，因此还要过一道"像不像文本"的检查
（控制符比例、私用区字符）。
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

_STRICT_CONTROL_RATIO = 0.02
"""控制符占比上限（2%）：超过就不像文本。"""

_LOOSE_CONTROL_RATIO = 0.05
"""``errors="replace"`` 兜底时的上限（宽松一点，已经明确是"有坏字节的文本"）。"""

_CONTROL = frozenset(range(0x00, 0x20)) - {0x09, 0x0A, 0x0C, 0x0D, 0x1B}
"""被允许的控制符：``\\t`` / ``\\n`` / ``\\f`` / ``\\r`` / ``ESC``（ANSI 颜色）。"""

_BOMS: tuple[tuple[bytes, str, str], ...] = (
    # UTF-32 的 BOM 以 UTF-16 的 BOM 开头，必须先判
    (codecs.BOM_UTF32_LE, "utf-32", "UTF-32LE"),
    (codecs.BOM_UTF32_BE, "utf-32", "UTF-32BE"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16", "UTF-16LE"),
    (codecs.BOM_UTF16_BE, "utf-16", "UTF-16BE"),
)


@dataclass(frozen=True, slots=True)
class TextSniff:
    """一次嗅探的结果。``text is None`` ⇒ 判定为二进制。"""

    encoding: str
    """命中的编码名（``utf-8`` / ``gb18030`` / ``utf-16`` …）；二进制时是 MIME。"""
    text: str | None
    note: str = ""
    """面向模型的说明；空串 ⇒ 就是普通 UTF-8，不需要任何提示。"""

    @property
    def is_binary(self) -> bool:
        return self.text is None

    @property
    def is_plain_utf8(self) -> bool:
        return self.encoding == "utf-8" and not self.note


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ord(ch) in _CONTROL or ch == "\x7f")
    return bad / len(text)


def _has_private_use(text: str) -> bool:
    """私用区字符（U+E000–U+F8FF）：随机字节被 GB18030"解"出来时几乎必现。"""
    return any(0xE000 <= ord(ch) <= 0xF8FF for ch in text[:4096])


def _looks_like_text(text: str, *, ratio: float = _STRICT_CONTROL_RATIO) -> bool:
    return _control_ratio(text) <= ratio


def _try_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _decode_bom(raw: bytes) -> TextSniff | None:
    for bom, encoding, label in _BOMS:
        if not raw.startswith(bom):
            continue
        text = _try_decode(raw, encoding)
        if text is None:
            return None  # BOM 对得上但内容解不开：交给后面的探测
        return TextSniff(encoding, text, f"{label} 文本（按 BOM 判定）")
    return None


def sniff_text(raw: bytes) -> TextSniff:
    """把文件字节嗅探成文本；判不出来才返回二进制。"""
    if not raw:
        return TextSniff("utf-8", "")

    by_bom = _decode_bom(raw)
    if by_bom is not None:
        return by_bom

    if b"\x00" in raw[:8192]:
        # UTF-16 的 ASCII 字符后面必跟一个 NUL：没 BOM 时按端序试一遍
        for encoding, label in (("utf-16-le", "UTF-16LE"), ("utf-16-be", "UTF-16BE")):
            text = _try_decode(raw, encoding)
            if text is not None and _looks_like_text(text):
                return TextSniff(
                    encoding, text, f"{label} 文本（文件含 NUL 字节，按 NUL 分布判定）"
                )
        return TextSniff("application/octet-stream", None)

    text = _try_decode(raw, "utf-8")
    if text is not None and _looks_like_text(text):
        return TextSniff("utf-8", text)

    text = _try_decode(raw, "gb18030")
    if text is not None and _looks_like_text(text) and not _has_private_use(text):
        return TextSniff("gb18030", text, "GB18030 文本（非 UTF-8，已按 GB18030 解码）")

    loose = raw.decode("utf-8", errors="replace")
    if _looks_like_text(loose, ratio=_LOOSE_CONTROL_RATIO):
        return TextSniff("utf-8", loose, "含少量非法字节，已用 U+FFFD 替换")

    return TextSniff("application/octet-stream", None)


__all__ = ["TextSniff", "sniff_text"]
