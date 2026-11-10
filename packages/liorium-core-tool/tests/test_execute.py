"""``execute``：正常命令 / 非零退出 / 超时杀进程树 / 中文输出 / cwd / shell 披露。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PYTHON = sys.executable


def _python(code: str) -> str:
    """拼一条跨平台的 Python 单行命令（cmd/sh 都能跑）。"""
    return f'"{PYTHON}" -c "{code}"'


def _body(output: str) -> str:
    """去掉首行 ``[Shell: ...]`` 后的结果正文（换行统一成 ``\\n``）。"""
    head, _, rest = output.partition("\n")
    assert head.startswith("[Shell: "), f"首行不是 shell 披露行: {output!r}"
    assert head.endswith("]"), f"shell 披露行没有收尾: {head!r}"
    return rest.replace("\r\n", "\n")


def _shell_label(output: str) -> str:
    return output.split("\n", 1)[0][len("[Shell: ") : -1]


async def test_execute_echo(execute_tool) -> None:
    output = await execute_tool.ainvoke({"command": "echo hello"})
    assert _body(output).strip() == "hello"


async def test_execute_shell_is_disclosed(execute_tool) -> None:
    """宿主反馈：分不清一条命令走的是 cmd 还是 PATH 里的 MSYS 工具。"""
    output = await execute_tool.ainvoke({"command": "echo hi"})
    label = _shell_label(output)
    if sys.platform == "win32":
        assert label.endswith("/c") and "cmd" in label.lower()
    else:
        assert label == "sh -c"


async def test_execute_python_stdout(execute_tool) -> None:
    output = await execute_tool.ainvoke({"command": _python("print(1 + 1)")})
    assert _body(output) == "2"


async def test_execute_nonzero_exit_has_stderr_and_code(execute_tool) -> None:
    command = _python("import sys; sys.stderr.write('boom'); sys.exit(3)")
    output = await execute_tool.ainvoke({"command": command})
    assert _body(output) == "[stderr]\nboom\n[exit code: 3]"


async def test_execute_no_output(execute_tool) -> None:
    output = await execute_tool.ainvoke({"command": _python("pass")})
    assert _body(output) == "(无输出)"


async def test_execute_multiline_python_keeps_every_line(execute_tool) -> None:
    """2026-11-08 回归：``cmd.exe`` 在第一个换行处截断，整段脚本会变成空程序。

    实测（改前）``python -c "⏎print(1)⏎print(2)"`` 的子进程 argv 是 ``['-c', '']``
    —— 退出码 0、无输出、没有任何报错。多行命令现在自动改走 PowerShell。
    """
    code = "\nprint(1)\nprint(2)"
    output = await execute_tool.ainvoke({"command": f'"{PYTHON}" -c "{code}"'})
    assert _body(output) == "1\n2"


async def test_execute_multiline_failure_propagates(execute_tool) -> None:
    """多行不只是"能跑"：非零退出码也要如实回来（PowerShell 路由的退出码收尾）。"""
    code = "\nimport sys\nsys.stderr.write('boom')\nsys.exit(4)"
    output = await execute_tool.ainvoke({"command": f'"{PYTHON}" -c "{code}"'})
    body = _body(output)
    assert "[stderr]\nboom" in body
    assert "[exit code: 4]" in body


async def test_execute_forced_cmd_refuses_multiline(execute_tool) -> None:
    """显式 ``shell="cmd"`` 时不能静默只跑第一行：直接拒绝并说明。"""
    if sys.platform != "win32":
        return
    output = await execute_tool.ainvoke({"command": "echo A\necho B", "shell": "cmd"})
    assert "换行" in output and "截断" in output
    assert "echo B" not in output


async def test_execute_unknown_shell_is_rejected(execute_tool) -> None:
    assert await execute_tool.ainvoke({"command": "echo x", "shell": "zsh"}) == (
        "错误: shell 只能是 auto/cmd/powershell/sh，收到 'zsh'"
    )


async def test_execute_timeout_kills_process_tree(execute_tool) -> None:
    started = time.monotonic()
    output = await execute_tool.ainvoke(
        {"command": _python("import time; time.sleep(30)"), "timeout": 0.5}
    )
    elapsed = time.monotonic() - started
    assert _body(output) == "命令超时 (0.5s)"
    # 整棵进程树被杀：不该等到 30 秒
    assert elapsed < 10


async def test_execute_chinese_output(execute_tool) -> None:
    output = await execute_tool.ainvoke({"command": _python("print('中文输出')")})
    assert "中文输出" in output


async def test_execute_cwd(execute_tool, workspace: Path) -> None:
    command = _python("import os; print(os.path.basename(os.getcwd()))")
    output = await execute_tool.ainvoke({"command": command, "cwd": str(workspace)})
    assert _body(output) == workspace.name


async def test_execute_missing_cwd(execute_tool, workspace: Path) -> None:
    missing = workspace / "nope"
    output = await execute_tool.ainvoke({"command": "echo hi", "cwd": str(missing)})
    assert output.startswith("工作目录不存在: ")
    assert str(missing) in output


async def test_execute_empty_command(execute_tool) -> None:
    assert await execute_tool.ainvoke({"command": "   "}) == "错误: command 不能为空"


async def test_execute_default_timeout_comes_from_runtime(
    workspace: Path, vfs, store
) -> None:
    from liorium.core_tool.runtime import ToolRuntime

    class _Runner:
        def __init__(self) -> None:
            self.timeout = None

        async def run(self, command, *, cwd=None, timeout=None, env=None):
            self.timeout = timeout
            return 0, "ok", ""

    runner = _Runner()
    runtime = ToolRuntime(
        vfs=vfs, workspace_root=workspace, artifacts=store, process_runner=runner,
        default_timeout=7.0,
    )
    from liorium.core_tool.execute import make_execute_tool

    tool = make_execute_tool(runtime)
    assert _body(await tool.ainvoke({"command": "x"})) == "ok"
    assert runner.timeout == 7.0
    assert _body(await tool.ainvoke({"command": "x", "timeout": 0})) == "ok"
    assert runner.timeout == 7.0
    assert _body(await tool.ainvoke({"command": "x", "timeout": 99999})) == "ok"
    assert runner.timeout == 3600.0
