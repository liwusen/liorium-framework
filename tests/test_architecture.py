"""架构守卫：把「包间依赖方向 + 禁止项」钉进 CI（计划 §8 不变量 / INTERFACES §2）。

全部用**静态 AST / 文本分析**，不 import 被测模块（跑一次几毫秒，也不会因为
某包依赖未安装而 skip）。

守的规则：

1. ``liorium-core`` 只能 import 标准库与 ``liorium.core``；
   ``liorium-vfs`` 只能 import 标准库 / ``liorium.core`` / ``liorium.vfs``
   （计划 §3.8：二者零第三方依赖）；
   ``liorium-realm-base``（2026-11-07）只许 ``liorium.core`` / ``liorium.vfs`` / 自己，
   且**模块级不许 import langchain / langgraph**（它是给 realm 作者的轻量契约）；
2. 能力包（``liorium-core-tool`` / ``liorium-memory`` / ``liorium-skills`` / ``liorium-mcp``）
   **允许** import ``liorium.agent``（2026-11-06 决策：取消反向依赖禁令）；能力包之间
   仍然互不依赖，也不许 import 旧后端 / 旧插件系统。``liorium-todo-realm``（2026-11-07）
   是另一条更轻的路：只 import ``liorium.realm_base``，连 ``liorium.agent`` 都不需要；
3. 任何包不许 import 旧式 hook 插件框架 ``pluggy``（只有一个扩展机制：realm）；
4. 全局禁止裸 ``asyncio.create_task``，唯一白名单
   ``packages/liorium-realm-base/src/liorium/realm_base/base.py``（§8.4）；
5. 不许存在 ``packages/*/src/liorium/__init__.py``（命名空间包约定）；
6. 源码里不许出现 ``pluggy``（禁止把 hook 插件系统重新引进来）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
"""仓库根（``liorium/``）。"""

PACKAGES = REPO_ROOT / "packages"

#: 每个包允许 import 的 liorium 内部前缀；未列出的包（例如 liorium-agent）不做方向约束。
LIORIUM_ALLOWED: dict[str, set[str]] = {
    "liorium-core": {"liorium.core"},
    "liorium-vfs": {"liorium.core", "liorium.vfs"},
    # realm 契约与写作 API：只认 core / vfs（不依赖 agent，否则 agent→它→agent 成环）。
    "liorium-realm-base": {"liorium.core", "liorium.vfs", "liorium.realm_base"},
    "liorium-core-tool": {
        "liorium.core",
        "liorium.vfs",
        "liorium.core_tool",
        "liorium.agent",
        "liorium.realm_base",
    },
    "liorium-memory": {
        "liorium.core",
        "liorium.vfs",
        "liorium.memory",
        "liorium.agent",
        "liorium.realm_base",
    },
    "liorium-skills": {
        "liorium.core",
        "liorium.vfs",
        "liorium.skills",
        "liorium.agent",
        "liorium.realm_base",
    },
    "liorium-mcp": {"liorium.core", "liorium.vfs", "liorium.mcp", "liorium.agent", "liorium.realm_base"},
    # 只写 realm 的能力包：core + 契约包就够了，**不**需要 agent / vfs。
    "liorium-todo-realm": {"liorium.core", "liorium.realm_base"},
    # 核心工具的 realm 外壳：调 core_tool 的 ToolRuntime + 契约包，**不**背 agent。
    "liorium-core-tool-realm": {
        "liorium.core",
        "liorium.vfs",
        "liorium.core_tool",
        "liorium.realm_base",
    },
    "liorium-provider-manager": {"liorium.core", "liorium.provider"},
}

#: 零第三方依赖的叶子包（计划 §3.8）：import 只能是标准库或白名单里的 liorium 包。
ZERO_DEPENDENCY_PACKAGES = ("liorium-core", "liorium-vfs")

#: 允许调用 ``asyncio.create_task`` 的白名单（相对仓库根）。
#: - ``liorium/realm_base/base.py``：realm 的 ``ctx.spawn`` 实现（2026-11-07 从 agent 搬来）；
#: - ``cli/subagents.py``：CLI 是宿主，事件监控任务由宿主自己托管。
ASYNCIO_CREATE_TASK_WHITELIST = frozenset(
    {
        "packages/liorium-realm-base/src/liorium/realm_base/base.py",
        "packages/liorium-cli/src/liorium/cli/subagents.py",
    }
)

#: 源码里不许出现的字符串（旧式 hook 插件框架）。
FORBIDDEN_TEXT = ("pluggy",)

#: 文本扫描时纳入的文件后缀（其余视为二进制，跳过）。
TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".json", ".txt", ".yaml", ".yml", ".cfg", ".ini", ".rst"}
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def package_dirs() -> list[Path]:
    """``packages/<name>``（只保留带 ``src`` 的，按名字排序保证消息稳定）。"""
    return sorted(
        path
        for path in PACKAGES.iterdir()
        if path.is_dir() and (path / "src").is_dir() and not path.name.startswith(".")
    )


def python_files(package: Path) -> list[Path]:
    return sorted(
        path for path in (package / "src").rglob("*.py") if "__pycache__" not in path.parts
    )


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def imports_of(path: Path) -> list[str]:
    """文件里**所有** import 目标（含函数内延迟导入；相对导入不跨包，直接跳过）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入 = 同包内
                continue
            if node.module:
                found.append(node.module)
                # ``from x import a`` 里 a 可能是子模块，也可能是符号；两种都要能匹配
                found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def is_liorium(module: str) -> bool:
    return module == "liorium" or module.startswith("liorium.")


def allowed_liorium_prefix(module: str, allowed: set[str]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in allowed)


def create_task_calls(path: Path) -> list[int]:
    """返回文件里 ``asyncio.create_task(...)`` / 裸 ``create_task(...)`` 的行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "create_task":
            if isinstance(func.value, ast.Name) and func.value.id == "asyncio":
                lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id == "create_task":
            lines.append(node.lineno)
    return sorted(lines)


def _docstring_ids(tree: ast.AST) -> set[int]:
    """收集模块/类/函数 docstring 的 ``id()``（说明性文字不算耦合）。"""
    found: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def code_string_literals(path: Path) -> list[tuple[int, str]]:
    """返回文件里**代码形态**的字符串常量（``(行号, 内容)``，跳过 docstring）。

    为什么跳过 docstring：文档里"历史说明"类内容不是耦合，
    真正要防的是拿它当路径/依赖去读去 import。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


# ---------------------------------------------------------------------------
# 0. 自检：守卫真的扫到了东西
# ---------------------------------------------------------------------------


def test_packages_are_discovered() -> None:
    """守卫必须真的扫到文件，否则"全绿"是假绿。"""
    names = {package.name for package in package_dirs()}
    assert {
        "liorium-core",
        "liorium-vfs",
        "liorium-realm-base",
        "liorium-agent",
        "liorium-core-tool",
        "liorium-todo-realm",
    } <= names
    for package in package_dirs():
        assert python_files(package), f"{package.name} 没有扫到任何 .py 文件"


def test_core_and_vfs_have_expected_modules() -> None:
    core = {path.name for path in python_files(PACKAGES / "liorium-core")}
    vfs = {path.name for path in python_files(PACKAGES / "liorium-vfs")}
    assert {"types.py", "uri.py", "canonical.py", "errors.py", "paths.py", "ports.py"} <= core
    assert {"fs.py", "node.py", "locks.py"} <= vfs


# ---------------------------------------------------------------------------
# 1. 包间依赖方向
# ---------------------------------------------------------------------------


def test_liorium_dependency_direction() -> None:
    """core / vfs / core-tool / memory / skills / mcp 的 liorium 内部依赖不得越界。"""
    offenders: list[str] = []
    for package in package_dirs():
        allowed = LIORIUM_ALLOWED.get(package.name)
        if allowed is None:
            continue
        for path in python_files(package):
            for module in imports_of(path):
                if not is_liorium(module):
                    continue
                if allowed_liorium_prefix(module, allowed):
                    continue
                offenders.append(
                    f"{rel(path)} -> {module}（{package.name} 只许 {sorted(allowed)}）"
                )
    assert not offenders, "包间依赖方向被破坏：\n" + "\n".join(offenders)


def test_zero_dependency_packages_import_only_stdlib() -> None:
    """``liorium-core`` / ``liorium-vfs`` 零第三方依赖（计划 §3.8：让"只想要 VFS"成立）。"""
    offenders: list[str] = []
    for name in ZERO_DEPENDENCY_PACKAGES:
        for path in python_files(PACKAGES / name):
            for module in imports_of(path):
                top = module.split(".")[0]
                if top in sys.stdlib_module_names or top == "liorium":
                    continue
                offenders.append(f"{rel(path)} -> {module}")
    assert not offenders, (
        "零依赖包出现了第三方 import（需要新增依赖请先改 pyproject.toml 并说明理由）：\n"
        + "\n".join(offenders)
    )


def test_realm_base_stays_light() -> None:
    """``liorium-realm-base`` 模块级不许 import langchain / langgraph。

    它是"写 realm 只需 core + vfs"的承诺：``@realm_tool`` 只在 ``tools()`` 里**延迟**
    import ``langchain_core``，所以模块顶层的 import 必须干净。
    """
    offenders: list[str] = []
    for path in python_files(PACKAGES / "liorium-realm-base"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # 只看模块顶层；函数内延迟导入是允许的
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"langchain", "langchain_core", "langgraph"}:
                    offenders.append(f"{rel(path)}:{node.lineno} -> {name}")
    assert not offenders, (
        "liorium-realm-base 模块级不许 import langchain / langgraph"
        "（必须在函数里延迟导入）：\n" + "\n".join(offenders)
    )


def test_capability_packages_may_import_agent() -> None:
    """2026-11-06 决策：能力包允许 import ``liorium.agent``。

    2026-11-07 起还多了一条更轻的路：``liorium.realm_base``（契约与写作 API，不背
    langchain / langgraph），能力包写 realm 时应优先用它。
    """
    for name in ("liorium-core-tool", "liorium-memory", "liorium-skills", "liorium-mcp"):
        assert "liorium.agent" in LIORIUM_ALLOWED[name], f"{name} 不允许 import liorium.agent"
        assert "liorium.realm_base" in LIORIUM_ALLOWED[name], f"{name} 不允许 import liorium.realm_base"
    # 只写 realm 的包不需要 agent：todo-realm 的允许面就只有 core + realm_base。
    assert LIORIUM_ALLOWED["liorium-todo-realm"] == {"liorium.core", "liorium.realm_base"}


# 2026-11-06：能力包（core-tool / memory / skills / mcp）允许 import ``liorium.agent``。
# 允许面由上面的 ``LIORIUM_ALLOWED`` 表达；原先的两条反向依赖禁令已删除。


# ---------------------------------------------------------------------------
# 3. 禁止裸 asyncio.create_task（计划 §8.4）
# ---------------------------------------------------------------------------


def test_no_bare_asyncio_create_task() -> None:
    """后台任务必须走 ``ctx.spawn`` / 宿主托管；唯一白名单是 realm/base.py。"""
    offenders: list[str] = []
    for package in package_dirs():
        for path in python_files(package):
            hits = create_task_calls(path)
            if hits and rel(path) not in ASYNCIO_CREATE_TASK_WHITELIST:
                offenders.extend(f"{rel(path)}:{line}" for line in hits)
    assert not offenders, (
        "禁止裸 asyncio.create_task（计划 §8.4：一律 ctx.spawn / 宿主托管）；"
        f"唯一白名单 {sorted(ASYNCIO_CREATE_TASK_WHITELIST)}：\n" + "\n".join(offenders)
    )


def test_create_task_whitelist_is_still_the_only_place() -> None:
    """白名单文件本身必须还在用它（否则白名单该删了，守卫不该无限期豁免）。"""
    whitelist = [REPO_ROOT / item for item in ASYNCIO_CREATE_TASK_WHITELIST]
    for path in whitelist:
        assert path.is_file(), f"白名单文件不存在: {path}"
        assert create_task_calls(path), f"{rel(path)} 已经不再用 create_task，请从白名单删除"


# ---------------------------------------------------------------------------
# 4. 命名空间包（不许 src/liorium/__init__.py）
# ---------------------------------------------------------------------------


def test_namespace_package_has_no_liorium_init() -> None:
    """每个包只有 ``src/liorium/<sub>/__init__.py``；``src/liorium/__init__.py`` 会毁掉命名空间。"""
    offenders = sorted(
        rel(path)
        for path in PACKAGES.glob("*/src/liorium/__init__.py")
        if path.is_file()
    )
    assert not offenders, (
        "不许创建 src/liorium/__init__.py（多个包会互相覆盖，见 INTERFACES §1）：\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 5. 配置读取守卫（不许出现产品后端/插件系统的痕迹）
# ---------------------------------------------------------------------------


def test_no_product_config_reads() -> None:
    """代码里的字符串常量与随包文本资源不许出现产品配置/旧后端/旧插件系统。

    docstring 与注释里的**历史说明**（例如"原型见旧后端"）不算耦合，故这里只扫
    代码形态的字符串常量 + 非 ``.py`` 文本资源；真正的依赖耦合由 import 规则守。
    """
    offenders: list[str] = []
    for package in package_dirs():
        for path in python_files(package):
            for lineno, value in code_string_literals(path):
                for needle in FORBIDDEN_TEXT:
                    if needle in value:
                        offenders.append(f"{rel(path)}:{lineno}: {needle}")
        for path in sorted((package / "src").rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in TEXT_SUFFIXES or path.suffix == ".py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in FORBIDDEN_TEXT:
                if needle in text:
                    offenders.append(f"{rel(path)}: {needle}")
    assert not offenders, (
        "源码里不许出现 pluggy（库只有 realm 一种扩展机制）：\n" + "\n".join(offenders)
    )


def test_no_plugin_framework_imports() -> None:
    """``pluggy`` 连 import 都不许出现（含函数内延迟导入）。"""
    offenders = [
        f"{rel(path)} -> {module}"
        for package in package_dirs()
        for path in python_files(package)
        for module in imports_of(path)
        for needle in ("pluggy",)
        if module == needle or module.startswith(needle + ".")
    ]
    assert not offenders, (
        "不许 import pluggy（扩展机制只有 realm，见决策记录 §A2）：\n" + "\n".join(offenders)
    )
