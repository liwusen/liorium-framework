"""路径工具：裸路径解析与目录准备。

计划依据 §3.6 规则 4：裸路径 = 真实文件系统（相对 ``AgentSpec.workspace_root``）；
``file://`` = 真实文件系统绝对路径。

``__file__`` 锚点只允许出现在本包（架构守卫）。
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import LioriumError

PACKAGE_ROOT = Path(__file__).resolve().parent
"""``liorium/core`` 目录。库内唯一的 ``__file__`` 锚点之一。"""

_EXTENDED_PREFIX = "\\\\?\\"
"""Windows 扩展长度路径前缀。"""


def normalize_fs_path(path: str | Path) -> str:
    """去掉 Windows 的 ``\\\\?\\`` 扩展长度前缀（``\\\\?\\UNC\\server\\x`` → ``\\\\server\\x``）。

    2026-11-08：``write`` 回显的路径时而是 ``D:\\dev\\x``、时而是 ``\\\\?\\D:\\dev\\x``。
    ``Path.resolve()`` 自己**不会**加这个前缀（四种输入实测过），但只要上游
    （宿主配的 ``workspace_root``、模型回传的上次输出）带了它，``resolve`` 会原样保留，
    之后**所有派生路径**都跟着带。后果是 ``edit`` / ``read`` 里按字符串比较工作区相对
    路径时失配（``Path.relative_to`` 抛 ``ValueError``，退化成裸文件名，``artifacts://``
    引用跟着错）。归一化放在解析入口，一处修完所有工具。

    POSIX 上原样返回（``\\\\?\\`` 是合法的文件名片段，不能动）。
    """
    text = os.fspath(path)
    if os.name != "nt" or not text.startswith(_EXTENDED_PREFIX):
        return text
    rest = text[len(_EXTENDED_PREFIX):]
    if rest.upper().startswith("UNC\\"):
        return "\\\\" + rest[len("UNC\\"):]
    return rest


def ensure_dir(path: Path) -> Path:
    """确保目录存在（``parents=True, exist_ok=True``），返回原路径。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_workspace_path(raw: str, workspace_root: Path) -> Path:
    """把裸路径解析成真实文件系统路径。

    - 绝对路径（``C:\\x`` / ``/x``）→ 原样；
    - 相对路径 → ``workspace_root / raw``；
    - 不含 ``..`` 越界检查：裸路径本来就是真实文件系统，宿主给的工作区根就是边界。
    """
    text = os.path.expanduser(raw)
    p = Path(text)
    if not p.is_absolute():
        p = workspace_root / p
    return Path(normalize_fs_path(p))


def resolve_file_uri(raw: str) -> Path:
    """``file://`` → 绝对路径。

    支持 ``file:///C:/x/y.txt``、``file:////server/share/x``（Windows）与
    ``file:///home/x``、``file:////home/x``（POSIX，多重前导斜杠等价）；
    ``file://relative`` 视为错误 —— ``file://`` 的语义就是绝对路径。
    """
    rest = raw[len("file://"):] if raw.lower().startswith("file://") else raw
    if os.name == "nt":
        if rest.startswith("//"):
            # file:////server/share/x → \\server\share\x（UNC）
            rest = "\\\\" + rest[2:].replace("/", "\\")
        elif rest.startswith("/"):
            # file:///C:/x → C:/x
            rest = rest[1:]
    elif rest.startswith("/"):
        # POSIX：file:////tmp/x 与 file:///tmp/x 等价（折叠多余前导斜杠）
        rest = "/" + rest.lstrip("/")
    p = Path(rest)
    if not p.is_absolute():
        raise LioriumError(f"file:// 必须是绝对路径: {raw!r}")
    return Path(normalize_fs_path(p))


__all__ = [
    "PACKAGE_ROOT",
    "ensure_dir",
    "normalize_fs_path",
    "resolve_file_uri",
    "resolve_workspace_path",
]
