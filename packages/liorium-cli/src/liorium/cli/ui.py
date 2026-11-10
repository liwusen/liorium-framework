"""终端渲染：颜色、缩进、JSON 模式与小工具。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Sequence


def configure_streams() -> None:
    """Windows 下 stdout/stderr 被重定向时默认编码可能是 GBK（装不下 ✗/⏺）。

    统一切到 UTF-8 + ``errors="replace"``：真控制台走 WriteConsoleW 不受影响，
    管道/文件则是合法 UTF-8，不会因一个符号把会话打崩。
    stdin 只在**非交互**时切 UTF-8（管道输入默认是 UTF-8 字节；真控制台保持原样）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


class Ui:
    """最小终端 UI：颜色、缩进、JSON 模式。

    编码兜底：Windows 下 stdout 被重定向时默认编码可能是 GBK，装不下 ✗/⏺ 这类符号。
    这里在非 UTF-8 编码时退回 ASCII 标记，避免 UnicodeEncodeError。
    """

    def __init__(self, *, color: bool = True, json_mode: bool = False) -> None:
        self.color = color and sys.stdout.isatty()
        self.json_mode = json_mode
        self._at_line_start = True
        encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
        self.unicode = "utf" in encoding
        self._glyphs = {
            "tool": "⏺" if self.unicode else ">",
            "error": "✗" if self.unicode else "x",
            "compact": "⧗" if self.unicode else "*",
            "arrow": "→" if self.unicode else "->",
            "start": "▶" if self.unicode else ">",
            "done": "✓" if self.unicode else "v",
        }

    def glyph(self, name: str) -> str:
        return self._glyphs.get(name, "")

    # -- 基础 -----------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def dim(self, text: str) -> str:
        return self._c("2", text)

    def mint(self, text: str) -> str:
        return self._c("36", text)

    def violet(self, text: str) -> str:
        return self._c("35", text)

    def yellow(self, text: str) -> str:
        return self._c("33", text)

    def red(self, text: str) -> str:
        return self._c("31", text)

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._at_line_start = text.endswith("\n")

    def writeln(self, text: str = "") -> None:
        self.write(text + "\n")

    def info(self, text: str) -> None:
        self.writeln(self.dim(text))

    def warn(self, text: str) -> None:
        self.writeln(self.yellow(f"! {text}"))

    def error(self, text: str) -> None:
        self.writeln(self.red(f"{self.glyph('error')} {text}"))

    def ensure_newline(self) -> None:
        if not self._at_line_start:
            self.writeln()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def truncate(text: str, *, limit: int = 400, max_lines: int = 5) -> str:
    if not text:
        return "(无输出)"
    lines = text.splitlines()
    trimmed = lines[:max_lines]
    out = "\n".join(trimmed)
    clipped = len(lines) > max_lines or len(text) > limit
    if len(out) > limit:
        out = out[:limit].rstrip() + "…"
    elif clipped:
        out = out.rstrip() + " …"
    if clipped:
        out += f"（共 {len(lines)} 行 / {len(text)} 字符）"
    return out


def indent(text: str, prefix: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(prefix + line for line in lines)


def short_json(value: Any, limit: int = 160) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError:
        text = str(value)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f" {text}"


def memory_meta(
    updated_at: str, tags: Sequence[str] = (), line_count: int | None = None
) -> str:
    """记忆条目的一行元信息：``2026-11-04 20:31  5 行  #人物``（本地时间）。"""
    parts = [local_time(updated_at)]
    if line_count is not None:
        parts.append(f"{line_count} 行")
    if tags:
        parts.append("#" + ", #".join(tags))
    return "  ".join(parts)


def local_time(value: str) -> str:
    """库里 ``updated_at`` 是 UTC ISO（``2026-11-04T12:51:00Z``），显示成本地时间。"""
    if not value:
        return "?"
    try:
        stamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return value.replace("T", " ")[:16]  # 非预期格式：原样显示，不静默丢弃
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def clock(timestamp: float | None) -> str:
    """Unix 时间戳 → 本地 ``HH:MM:SS``。"""
    if timestamp is None:
        return "?"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"


def duration(seconds: float | None) -> str:
    """秒数 → 紧凑的 ``1.2s`` / ``2m03s`` / ``1h02m``。"""
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def char_count(count: int) -> str:
    """字符数 → ``812`` / ``1.2k`` / ``3.4M``。"""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


# 旧名字（测试与历史代码里用得多）保留成别名，避免外部脚本 import 失效。
_configure_streams = configure_streams
_truncate = truncate
_indent = indent
_short_json = short_json
_memory_meta = memory_meta
_local_time = local_time

__all__ = [
    "Ui",
    "char_count",
    "clock",
    "configure_streams",
    "duration",
    "indent",
    "local_time",
    "memory_meta",
    "short_json",
    "truncate",
]
