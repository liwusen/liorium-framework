"""核心工具的共享运行时（路径解析 + 文件/VFS 读写 + 进程端口）。

计划依据：§F1 / §F3、§3.6 URI 规则、§4.5 内容模型，
``docs/design/vfs-output-mw.md`` §1 / §2 / §3 / §4.6 / §9.5。

硬约束：

- 只与 ``VirtualFileSystem`` 和 ``liorium.core`` 里声明的端口打交道：
  **不 import ``liorium.agent``**、不读产品配置、不做权限判断（v1 没有权限系统）；
- 裸路径 = 真实文件系统（相对 ``ToolRuntime.workspace_root``）；``file://`` = 绝对路径；
  ``<scheme>://`` = VFS（``liorium.core.uri.parse`` 负责拆 scheme / selector / query）；
- ``agent://`` 是 Agent 自身 VFS 根 ``/`` 的 scheme（``agent://a/b`` ≡ ``/a/b``，
  见 vfs-output-mw §1.12）；其余 scheme 与挂载点同名（``skills://a`` ≡ ``/skills/a``）；
- VFS 写入统一走本模块的 helper：先做只读判断，再分派 ``write_handler`` / ``edit_handler``，
  保证 ``↳ ack`` 契约（§1.12 / §9.5）不会被静默丢掉。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from liorium.core.errors import (
    ConfigError,
    NodeNotFoundError,
    VfsError,
    VfsReadOnlyError,
)
from liorium.core.paths import normalize_fs_path
from liorium.core.ports import ArtifactStore, ProcessRunner
from liorium.core.types import Text, content_to_text
from liorium.core.uri import ParsedUri, normalize_vfs_path, parse
from liorium.vfs import VirtualFileSystem, VfsNode
from liorium.vfs.node import maybe_await

ROOT_SCHEME = "agent"
"""Agent 自身 VFS 根（``/``）的 scheme。历史名字已冻结，不要改名。"""

# ``file:///C:/x/y`` 解析出来是 ``/C:/x/y``；Windows 上要把多余的前导斜杠去掉。
_WINDOWS_DRIVE_RE = re.compile(r"^/[A-Za-z]:(?:[/\\]|$)")


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """一次路径解析的结果：VFS 节点还是真实文件。"""

    uri: ParsedUri
    is_vfs: bool
    path: str
    """VFS 绝对路径（``/`` 开头）或真实文件系统的绝对路径字符串。"""

    display: str
    """面向模型的规范写法：``scheme://path`` 或真实绝对路径。"""

    @property
    def scheme(self) -> str:
        return self.uri.scheme

    def __str__(self) -> str:  # pragma: no cover - 只用于日志/报错
        return self.display


def _fs_path(raw: str, workspace_root: Path) -> Path:
    """把 ``file`` scheme 的 path 变成真实绝对路径。"""
    text = raw
    if os.name == "nt" and _WINDOWS_DRIVE_RE.match(text):
        text = text[1:]
    candidate = Path(text) if text else Path(".")
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    # 统一去掉 Windows 的 `\\?\` 前缀：写回显 / artifacts 引用 / edit 的字符串比较
    # 必须用同一个拼写（2026-11-08 宿主反馈的路径不稳定问题）。
    return Path(normalize_fs_path(candidate.resolve()))


def _vfs_path(parsed: ParsedUri) -> str:
    """``<scheme>://path`` → VFS 绝对路径。"""
    if parsed.scheme == ROOT_SCHEME:
        return normalize_vfs_path("/" + parsed.path)
    return parsed.vfs_path()


def _vfs_display(parsed: ParsedUri) -> str:
    """VFS URI 的规范展示形态（去掉多余前导斜杠）。"""
    return f"{parsed.scheme}://{parsed.path.lstrip('/')}"


def vfs_display(vfs_path: str, scheme: str = ROOT_SCHEME) -> str:
    """VFS 绝对路径 → 能直接回传给工具解析的 URI。

    - ``agent://``（根 scheme）：相对 VFS 根 ``/`` 展示，``/notes/a.md`` →
      ``agent://notes/a.md``；
    - 其它 scheme 与挂载点同名：``/skills/demo/a.md`` → ``skills://demo/a.md``。
    """
    if scheme == ROOT_SCHEME:
        relative = vfs_path.lstrip("/")
    else:
        base = "/" + scheme
        if vfs_path == base:
            relative = ""
        elif vfs_path.startswith(base + "/"):
            relative = vfs_path[len(base):].lstrip("/")
        else:
            relative = vfs_path.lstrip("/")
    return f"{scheme}://{relative}"


def detect_newline(raw: bytes) -> str:
    """从原文判断行尾风格：有 ``\\r\\n`` 就按 CRLF，否则 LF（§2.5 / 任务要求保留行尾）。"""
    return "\r\n" if b"\r\n" in raw else "\n"


def decode_text(raw: bytes) -> str:
    """把真实文件字节解码成文本，并把换行统一成 ``\\n``（等价通用换行模式）。

    ``edit`` 的逐字符契约依赖「读回的 ``\\n`` 与模型写的 ``\\n`` 一致」（§2.5）。
    """
    text = raw.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode_text(text: str, newline: str = "\n") -> bytes:
    """按指定行尾风格编码成 UTF-8 字节。``newline="\\r\\n"`` 时把裸 ``\\n`` 转成 CRLF。"""
    if newline == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    elif newline == "\n":
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("utf-8")


@dataclass
class ToolRuntime:
    """核心工具（``read`` + 本包五个）共享的运行时。

    ``artifacts`` 只用到 ``liorium.core.ports.ArtifactStore`` 协议：
    工具本身不写 store（截断由 Agent 的包装层负责，§4.6），
    这里持有实例是为了让 ``read`` 之类的多模态工具能拿到同一个 store。
    """

    vfs: VirtualFileSystem
    workspace_root: Path
    artifacts: ArtifactStore
    max_output_chars: int = 120
    default_timeout: float = 120.0
    env: Mapping[str, str] | None = None
    process_runner: ProcessRunner | None = None
    """None → 用 ``execute.DefaultProcessRunner``（宿主可换成沙箱/审计实现）。"""

    def __post_init__(self) -> None:
        if not isinstance(self.vfs, VirtualFileSystem):
            raise ConfigError(
                f"ToolRuntime.vfs 必须是 VirtualFileSystem: {type(self.vfs).__name__}"
            )
        self.workspace_root = Path(
            normalize_fs_path(Path(self.workspace_root).expanduser().resolve())
        )
        if not isinstance(self.artifacts, ArtifactStore):
            raise ConfigError(
                "ToolRuntime.artifacts 必须实现 ArtifactStore 协议"
                f"（缺少 put/put_multimodal/get/summary/exists）: {type(self.artifacts).__name__}"
            )
        if self.max_output_chars < 0:
            raise ConfigError(f"max_output_chars 不能为负: {self.max_output_chars}")
        if self.default_timeout <= 0:
            raise ConfigError(f"default_timeout 必须为正数: {self.default_timeout}")

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    def resolve_path(self, uri: str) -> ResolvedPath:
        """解析任意输入：裸路径 / ``file://`` → 真实文件；``<scheme>://`` → VFS。"""
        parsed = parse(str(uri or ""))
        if parsed.is_vfs:
            return ResolvedPath(
                uri=parsed,
                is_vfs=True,
                path=_vfs_path(parsed),
                display=_vfs_display(parsed),
            )
        return ResolvedPath(
            uri=parsed,
            is_vfs=False,
            path=str(_fs_path(parsed.path, self.workspace_root)),
            display=str(_fs_path(parsed.path, self.workspace_root)),
        )

    # ------------------------------------------------------------------
    # 真实文件系统 helper
    # ------------------------------------------------------------------

    @staticmethod
    def _file_path(target: ResolvedPath) -> Path:
        if not isinstance(target, ResolvedPath):
            raise ConfigError(f"需要 ResolvedPath，收到 {type(target).__name__}")
        if target.is_vfs:
            raise VfsError(f"{target.display} 是 VFS 节点，不能用文件系统 helper")
        return Path(target.path)

    async def exists(self, target: ResolvedPath) -> bool:
        path = self._file_path(target)
        return await asyncio.to_thread(path.exists)

    async def is_dir(self, target: ResolvedPath) -> bool:
        path = self._file_path(target)
        return await asyncio.to_thread(path.is_dir)

    async def read_bytes(self, target: ResolvedPath) -> bytes:
        """读真实文件字节（不存在抛 ``FileNotFoundError``）。"""
        path = self._file_path(target)
        return await asyncio.to_thread(path.read_bytes)

    async def write_bytes(self, target: ResolvedPath, data: bytes) -> None:
        """写真实文件字节；父目录自动创建（§2.1）。"""
        path = self._file_path(target)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def read_text(self, target: ResolvedPath) -> str:
        """按 UTF-8 读文本，换行统一成 ``\\n``。"""
        return decode_text(await self.read_bytes(target))

    async def read_text_with_newline(self, target: ResolvedPath) -> tuple[str, str]:
        """读文本并同时返回行尾风格（``edit`` 用它保留 CRLF）。"""
        raw = await self.read_bytes(target)
        return decode_text(raw), detect_newline(raw)

    async def write_text(
        self, target: ResolvedPath, text: str, *, newline: str = "\n"
    ) -> None:
        """按指定行尾风格写文本（``newline="\\r\\n"`` 保留 CRLF）。"""
        await self.write_bytes(target, encode_text(text, newline))

    # ------------------------------------------------------------------
    # VFS helper
    # ------------------------------------------------------------------

    async def vfs_stat(self, path: str) -> VfsNode | None:
        return await self.vfs.stat(path)

    async def vfs_read_text(self, path: str) -> str | None:
        """读 VFS 文本节点；不存在 / 非文本返回 None。"""
        content = await self.vfs.read(path)
        if content is None:
            return None
        return content_to_text(content)

    async def vfs_write_text(self, path: str, text: str) -> str | None:
        """写 VFS 文本节点，返回 handler 的 ACK（仅非空 ``str``，见 §9.5）。

        只读挂载 / 只读节点抛 ``VfsReadOnlyError``；投影节点、挂载子树内新建节点
        等写失败抛 ``VfsError``（不静默丢内容）。
        """
        content = Text(text)
        try:
            found = self.vfs.resolve(path)
        except NodeNotFoundError:
            await self._assert_creatable(path)
            # 新建路径的 ACK 由目录节点的 create_handler 返回（v1.1 投影目录）
            ack = await self.vfs.write(path, content)
            return ack if isinstance(ack, str) and ack else None
        node = found.node
        if node.is_directory:
            raise VfsError(f"{path} 是目录，不能写入")
        if found.readonly or not node.writable:
            raise VfsReadOnlyError(f"{path} 是只读节点")
        if node.has_write_handler:
            ack = await maybe_await(node.write_handler(node, content))  # type: ignore[misc]
            return ack if isinstance(ack, str) and ack else None
        ack = await self.vfs.write(path, content)
        return ack if isinstance(ack, str) and ack else None

    async def _assert_creatable(self, path: str) -> None:
        """新建节点前检查父链是否只读。

        跨挂载的写入由 ``VirtualFileSystem.write`` 自己委托给被挂载的 VFS
        （``_delegate_to_mount``），所以这里**不再拒绝**挂载子树内新建节点，
        只提前把只读父链说清楚（错误比"写进挂载点读不到"更可诊断）。
        """
        parts = [part for part in normalize_vfs_path(path).split("/") if part]
        prefix = ""
        for part in parts[:-1]:
            prefix += "/" + part
            try:
                found = self.vfs.resolve(prefix)
            except NodeNotFoundError:
                return  # 父链从这里开始缺失：vfs.write 会在本源树上补齐
            if found.readonly or not found.node.writable:
                raise VfsReadOnlyError(f"{prefix} 是只读节点")

    async def vfs_edit_text(self, path: str, old: str, new: str) -> str | None:
        """按字符串替换编辑 VFS 节点，返回 handler 的 ACK（仅非空 ``str``）。

        有 ``edit_handler`` 时把 ``(old, {"new": new})`` 交给它（diff 形态），
        否则落到 ``vfs.edit`` 的「替换第一处」。唯一匹配校验由调用方先做。
        """
        found = self.vfs.resolve(path)
        node = found.node
        if node.is_directory:
            raise VfsError(f"{path} 是目录，不能编辑")
        if found.readonly or not node.writable:
            raise VfsReadOnlyError(f"{path} 是只读节点")
        if node.has_edit_handler:
            ack = await maybe_await(
                node.edit_handler(node, old, {"new": new})  # type: ignore[misc]
            )
            return ack if isinstance(ack, str) and ack else None
        await self.vfs.edit(path, old, new)
        return None

    # ------------------------------------------------------------------
    # 进程端口
    # ------------------------------------------------------------------

    async def run_process(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        shell: str = "auto",
    ) -> tuple[int, str, str]:
        """跑一条命令，返回 ``(returncode, stdout, stderr)``（已是文本）。

        ``process_runner`` 为空时用 ``execute.DefaultProcessRunner``（``shell`` 生效）；
        宿主给了自定义 runner 时 ``shell`` 由它自己解释 —— 协议
        （``liorium.core.ports.ProcessRunner``）不因 shell 选择而变。
        超时抛 ``TimeoutError``，由 ``execute`` 工具翻译成冻结文案。
        """
        merged: dict[str, str] | None = None
        if self.env or env:
            merged = dict(self.env or {})
            merged.update(env or {})
        runner = self.process_runner
        if runner is None:
            from .execute import DefaultProcessRunner

            runner = DefaultProcessRunner(shell=shell)
        return await runner.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            env=merged,
        )


__all__ = [
    "ROOT_SCHEME",
    "ResolvedPath",
    "ToolRuntime",
    "decode_text",
    "detect_newline",
    "encode_text",
    "vfs_display",
]
