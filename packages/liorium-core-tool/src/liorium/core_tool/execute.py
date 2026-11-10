"""``execute`` 工具：在子进程里跑一条系统命令。

计划依据 §F3（v1 不做权限系统，execute 直接跑）与
``docs/design/vfs-output-mw.md`` §3（进程语义 / 返回文本）：
返回文本按 ``stdout`` → ``[stderr]`` → ``[exit code: N]`` 分段，**工具内不截断**
（截断由 Agent 的 OutputStore 包装层负责，§4.6）。

平台选择（2026-11-08 修订）：``asyncio.create_subprocess_shell`` 在 POSIX 走
``/bin/sh -c``，在 Windows 走 ``%COMSPEC% /c``（默认 ``cmd.exe``）。

**为什么多行命令改走 PowerShell**：``cmd.exe`` 的命令行解析在**第一个换行处终止**，
换行之后的内容被静默丢弃，并被"就地补齐"成合法命令。实测（Windows 11 / py3.11）::

    python.exe -c "⏎print(1)⏎print(2)"   → 子进程 argv = ['-c', '']  → 空程序、exit 0
    echo A⏎ echo B                        → 只输出 A，第二行消失，exit 0

即调用方以为跑了一整段脚本，实际只跑了第一行的残骸、还拿不到任何错误。
落成 ``.cmd`` 批处理再执行同样被截断（批处理解析器也在换行处断），所以
"写临时脚本"是用户唯一能绕开的路 —— 这里把多行命令改交给
``powershell.exe -NoProfile -NonInteractive -Command``（用 ``exec`` 传参，不经 cmd），
实测 argv 完整到达、输出正常。单行命令仍走 cmd：``;`` 不是 cmd 的分隔符，
换 shell 会改变语义。

命令内容的编码在 Windows 上可能是 OEM 代码页（如 cp936），因此输出先用 UTF-8
解码、失败再退回本地区域编码，避免中文输出变成乱码。
``[Shell: ...]`` 首行始终披露这次实际交给谁执行（2026-11-08，宿主反馈：
分不清某条命令走的是 cmd 还是 PATH 里的 MSYS 工具）。
"""

from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from langchain_core.tools import tool

from liorium.core.log import get_logger

from .runtime import ToolRuntime

logger = get_logger("core_tool.execute")

_MAX_TIMEOUT = 3600.0
"""超时上限（秒）。超过就按上限跑，避免模型传一个天文数字把回合挂死。"""

SHELL_CHOICES = ("auto", "cmd", "powershell", "sh")
"""``execute(shell=...)`` 的合法取值。"""

_POWERSHELL_EXIT_WRAP = "$global:LASTEXITCODE = 0\n{command}\nexit $LASTEXITCODE"
"""PowerShell 的 ``-Command`` **不传播原生命令的退出码**（``python -c "sys.exit(3)"``
会以 0 结束），所以先归零、跑完再 ``exit $LASTEXITCODE``。实测 3 / 0 / 7 均正确。"""


@dataclass(frozen=True, slots=True)
class ShellPlan:
    """一条命令的执计划：交给谁、怎么交、展不展示。"""

    label: str
    """``[Shell: ...]`` 里那一行（面向模型的可读描述）。"""
    argv: tuple[str, ...] | None
    """非空 ⇒ 用 ``create_subprocess_exec``（不经过 shell）；空 ⇒ ``create_subprocess_shell``。"""
    text: str
    """真正要执行的文本（PowerShell 路径会包一层退出码收尾）。"""


def _has_newline(command: str) -> bool:
    return "\n" in command or "\r" in command


def _comspec_name() -> str:
    """``%COMSPEC%`` 的文件名部分（通常是 ``cmd.exe``）。"""
    raw = os.environ.get("COMSPEC") or "cmd.exe"
    return Path(raw).name or "cmd.exe"


def _powershell_command(command: str) -> str:
    """包装成 PowerShell 命令行：补 ``&`` 调用符 + 收尾退出码。

    PowerShell 里 ``"C:\\x.exe" -c "..."`` 是一个**字符串字面量**，不是命令调用
    （实测会抛 ``ParserError: UnexpectedToken``）；以引号开头的命令行必须写成
    ``& "C:\\x.exe" ...``。模型给带空格路径加引号是常态，所以这里要补。
    """
    body = command.lstrip()
    if body[:1] in ("\"", "'"):
        body = "& " + body
    return _POWERSHELL_EXIT_WRAP.format(command=body)


def shell_plan(command: str, shell: str = "auto") -> ShellPlan:
    """决定这条命令交给谁执行；``execute`` 的 ``[Shell: ...]`` 与真正的执行共用它。

    - POSIX：恒为 ``sh -c``；
    - Windows ``cmd``：``%COMSPEC% /c``（单行默认）；
    - Windows ``powershell`` / ``auto`` + 多行：``powershell.exe -Command``（exec 传参）。
    """
    if os.name != "nt":
        return ShellPlan(label="sh -c", argv=None, text=command)
    if shell == "powershell" or (shell != "cmd" and _has_newline(command)):
        return ShellPlan(
            label="powershell.exe -NoProfile -Command",
            argv=("powershell.exe", "-NoProfile", "-NonInteractive", "-Command"),
            text=_powershell_command(command),
        )
    return ShellPlan(label=f"{_comspec_name()} /c", argv=None, text=command)


def _decode_output(raw: bytes) -> str:
    """解码子进程输出：优先 UTF-8，失败退回本地区域编码，最后才 replace。"""
    if not raw:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """杀掉整个进程树并回收。

    Windows 用 ``taskkill /T /F``（``/T`` 包含子进程树，``shell=True`` 的孙进程
    否则会变成孤儿）；POSIX 因为 ``start_new_session=True``，整棵树在同一个
    进程组里，直接 ``os.killpg``。
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt" and proc.pid:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError):
                killer.kill()
                await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                proc.kill()
    except Exception as exc:  # pragma: no cover - 终止失败只能记录，不能改变超时语义
        logger.warning("终止子进程失败: %s", exc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("子进程回收超时（pid=%s）", proc.pid)


class DefaultProcessRunner:
    """``liorium.core.ports.ProcessRunner`` 的内置实现。

    ``env`` 语义：``os.environ`` 为底，``env`` 逐项覆盖（不丢弃 PATH 等基础变量）。
    ``shell`` 语义见 :func:`shell_plan`（``auto`` 时多行命令走 PowerShell）。
    超时抛 ``TimeoutError``（工具层翻译成 ``命令超时 (...)``）。
    """

    def __init__(self, *, env: Mapping[str, str] | None = None, shell: str = "auto") -> None:
        self._base_env = dict(env or {})
        self._shell = shell if shell in SHELL_CHOICES else "auto"

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        merged_env = dict(os.environ)
        merged_env.update(self._base_env)
        if env:
            merged_env.update(env)

        plan = shell_plan(command, self._shell)
        kwargs: dict[str, object] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
            "env": merged_env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        if plan.argv is None:
            proc = await asyncio.create_subprocess_shell(plan.text, **kwargs)  # type: ignore[arg-type]
        else:
            # 不经 shell：多行参数才能原样到达子进程（cmd 会在换行处截断）。
            proc = await asyncio.create_subprocess_exec(*plan.argv, plan.text, **kwargs)  # type: ignore[arg-type]
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            await _terminate(proc)
            raise TimeoutError(f"命令执行超时（{timeout}s）") from None
        return proc.returncode or 0, _decode_output(stdout or b""), _decode_output(stderr or b"")


def _format_result(returncode: int, stdout: str, stderr: str, *, shell_label: str) -> str:
    """§3「返回文本」的逐字分段，首行始终是 ``[Shell: ...]``。

    ``[Shell: ...]`` 是 2026-11-08 追加的披露行（宿主分不清命令走了哪个 shell）；
    ``stdout`` → ``[stderr]`` → ``[exit code: N]`` 三段本身未变。
    """
    parts: list[str] = [f"[Shell: {shell_label}]"]
    out = stdout.strip()
    err = stderr.strip()
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    if returncode != 0:
        parts.append(f"[exit code: {returncode}]")
    if len(parts) == 1:
        parts.append("(无输出)")
    return "\n".join(parts)


def _clamp_timeout(value: float | None, default: float) -> float:
    """超时归一：``None``/非正数 → 默认值；超过上限截到 3600s。"""
    try:
        timeout = default if value is None else float(value)
    except (TypeError, ValueError):
        timeout = default
    if timeout <= 0:
        timeout = default
    return min(timeout, _MAX_TIMEOUT)


def _format_seconds(value: float) -> str:
    """``120.0`` → ``120``、``0.5`` → ``0.5``（冻结文案是 ``命令超时 ({timeout}s)``）。"""
    return f"{value:g}"


def _cmd_newline_warning(label: str) -> str:
    """``shell="cmd"`` 且命令含换行：**拒绝执行**，否则会静默跑半条命令。"""
    return (
        f"[Shell: {label}]\n"
        "错误: 命令里含换行，cmd.exe 会在第一个换行处截断（后面的内容被静默丢弃，"
        "退出码仍是 0）。三种改法：改成单行并用 `&&` 串联；传 `shell=\"powershell\"`；"
        "或把脚本写进文件再执行。"
    )


def make_execute_tool(runtime: ToolRuntime):
    """构造绑定 ``runtime`` 的 ``execute`` 工具。"""

    @tool
    async def execute(
        command: str, cwd: str = "", timeout: float | None = None, shell: str = "auto"
    ) -> str:
        """执行一条系统命令（shell），返回 stdout / stderr / 退出码。

        适合：跑测试、构建、git、装好的命令行工具、查看系统信息、临时脚本。
        不适合：需要交互输入的命令（会一直等到超时）；长期驻留的服务（用后台任务，别用这个）。

        语义：
        - 一次调用只跑一条命令；多条命令用 `&&` 串联（Windows 的 cmd 里 `;` **不是**分隔符）。
        - 结果首行总是 `[Shell: ...]`，披露这条命令实际交给了谁（cmd.exe / powershell / sh）。
        - Windows 上命令含换行时**自动走 PowerShell**（cmd.exe 会在第一个换行处截断、
          静默丢掉后面的内容）；单行仍走 cmd。可用 `shell` 强制指定。
        - 默认工作目录是 Agent 的工作区，`cwd` 可指定相对/绝对目录。
        - 超时会**杀掉整个进程树**（含子进程），结果只有 `命令超时 (...)`。
        - 非零退出码不会让工具失败，结果里会有一段 `[exit code: N]`，要自己判断成败。
        - 输出不截断；太长的结果由外层统一存成 `artifact://<id>`，按需用 read 取全文。

        Args:
            command (str): 要执行的命令行（原样交给 shell，不做二次转义）。
            cwd (str): 工作目录；默认 Agent 工作区。
            timeout (float): 超时秒数；省略用运行时默认值（120s），上限 3600s。
            shell (str): `auto`（默认）/ `cmd` / `powershell` / `sh`；POSIX 上恒为 sh。
        """
        text = str(command or "").strip()
        if not text:
            return "错误: command 不能为空"

        chosen = str(shell or "auto").strip().lower() or "auto"
        if chosen not in SHELL_CHOICES:
            return f"错误: shell 只能是 {'/'.join(SHELL_CHOICES)}，收到 {shell!r}"
        plan = shell_plan(text, chosen)
        if os.name == "nt" and chosen == "cmd" and _has_newline(text):
            return _cmd_newline_warning(plan.label)

        limit = _clamp_timeout(timeout, runtime.default_timeout)
        workdir = Path(str(cwd).strip()) if str(cwd or "").strip() else runtime.workspace_root
        if not workdir.is_absolute():
            workdir = runtime.workspace_root / workdir
        if not workdir.is_dir():
            return f"工作目录不存在: {workdir}"

        try:
            returncode, stdout, stderr = await runtime.run_process(
                text, cwd=workdir, timeout=limit, shell=chosen
            )
        except (TimeoutError, asyncio.TimeoutError):
            return f"[Shell: {plan.label}]\n命令超时 ({_format_seconds(limit)}s)"
        except FileNotFoundError as exc:
            return f"[Shell: {plan.label}]\n找不到命令解释器: {exc}"
        except OSError as exc:
            return f"[Shell: {plan.label}]\n命令执行失败: {exc}"
        return _format_result(returncode, stdout, stderr, shell_label=plan.label)

    return execute


__all__ = [
    "DefaultProcessRunner",
    "SHELL_CHOICES",
    "ShellPlan",
    "make_execute_tool",
    "shell_plan",
]
