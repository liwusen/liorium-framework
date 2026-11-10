"""``CoreToolRealm`` —— 六个核心工具（read / write / edit / search / find / execute）的 realm。

分工（两个包都保留）：

- ``liorium-core-tool``：工具实现、``ToolRuntime``（路径解析 / VFS 读写 / 进程端口）；
- 本包：把同一批工具装成**能力包**。工具不再由 Agent 隐式添加，用户写
  ``RealmSpec(id="core")`` 才装、``/realm disable core`` 就撤。

实例作用域 ``instance_scope="agent"``：本 realm 没有挂载、没有外部连接、没有状态，
每个 Agent（含子代理）各建一份是安全的；子代理因此可以用 ``toolsets`` 拿到不同的子集。

工具子集的来源（先到先得）：

1. ``RealmSpec.params["tools"]``（显式配置，可序列化，进指纹）；
2. ``RealmContext.agent_spec.toolsets["default"]``（``AgentSpec`` 未改动时的既有语义）；
3. 都没有 ⇒ 六个全给。

未知工具名由 ``build_core_tools`` 抛 ``ValueError``（不静默跳过）：realm 会标 ``failed``，
原因留在 ``status().realms[*].error`` 里，CLI 的 ``/realm`` 能看到。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from liorium.core.errors import RealmError
from liorium.core_tool.tools import CORE_TOOL_NAMES, build_core_tools
from liorium.realm_base import BaseRealm

CORE_REALM_ID = "core"
"""realm id（固定：``RealmManager`` 要求工厂返回的实例 id 与 spec 一致）。"""

DEFAULT_MAX_OUTPUT_CHARS = 120
"""``ToolRuntime.max_output_chars`` 的默认值（与 ``Limits.max_tool_output_chars`` 一致）。"""


class CoreToolRealm(BaseRealm):
    """六个核心工具的能力包（无挂载、无状态、无周期上下文）。"""

    id = CORE_REALM_ID
    instance_scope = "agent"
    state_scope = "agent"
    context_every = 0

    def __init__(self, spec: Any = None, *, artifacts: Any = None) -> None:
        super().__init__(spec)
        # 独立使用（没有 Agent 的 OutputStore）时可由宿主直接给一个 ArtifactStore。
        self._artifacts_override = artifacts
        self._tools: list[Any] = []
        self._names: tuple[str, ...] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        self._names = self._resolve_names()
        self._tools = self._build(self._names)

    async def on_stop(self) -> None:
        self._tools = []
        self._names = None

    # ------------------------------------------------------------------
    # 四件套
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        """已构造的工具（未 ``start()`` 时为空；``stop()`` 后清空 ⇒ 停用即撤工具）。"""
        return list(self._tools)

    def prompt_fragment(self) -> str:
        """工具调用规则；只描述本实例实际挂上的工具（空子集 / 已停用 ⇒ 空串）。"""
        names = [getattr(tool, "name", "") for tool in self._tools]
        if not names and self.ctx is None:  # 还没 start：按"六个全给"描述
            names = list(CORE_TOOL_NAMES)
        if not names:
            return ""
        lines = [
            f"工具调用规则（本 realm 提供：{' / '.join(names)}）：",
            "- 裸路径相对工作区根（workspace root）；`file://` 是绝对路径；"
            "`<scheme>://` 是 VFS 挂载点（`agent://` = Agent 自身根）。",
        ]
        if "read" in names:
            lines.append(
                "- `read` 支持行选择器：`path:50-100`、`path:raw`；大文件先看结构再取片段。"
            )
        if "write" in names or "edit" in names:
            lines.append("- 写文件用 `write` / `edit`，别用 `execute` 绕道；VFS 挂载点也支持写。")
        if "search" in names or "find" in names:
            lines.append("- `search` 按内容找、`find` 按名字找；都在目录树上递归。")
        if "execute" in names:
            lines.append("- `execute` 会真的在本机 shell 执行命令；破坏性操作前先说清楚要做什么。")
        lines.append("- 长输出会被截断成 `artifact://<id>` 引用；需要全文时用行选择器读那个引用。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_names(self) -> tuple[str, ...] | None:
        """``None`` = 六个全给；否则是显式子集（保持给出顺序、去重）。"""
        raw = self.params.get("tools")
        if raw is not None:
            return _parse_names(raw, source="RealmSpec.params['tools']")
        spec = getattr(self.ctx, "agent_spec", None) if self.ctx is not None else None
        toolsets = getattr(spec, "toolsets", None) or {}
        if "default" in toolsets:
            return _parse_names(toolsets["default"], source="AgentSpec.toolsets['default']")
        return None

    def _build(self, names: tuple[str, ...] | None) -> list[Any]:
        from liorium.core_tool.runtime import ToolRuntime

        ctx = self.ctx
        if ctx is None:  # pragma: no cover - BaseRealm.start() 保证已有 ctx
            raise RealmError("core realm 还没 start()，不能构造工具")
        runtime = ToolRuntime(
            vfs=ctx.vfs,
            workspace_root=self._workspace_root(),
            artifacts=self._artifacts(),
            max_output_chars=self._max_output_chars(),
        )
        return build_core_tools(runtime, names=list(names) if names is not None else None)

    def _workspace_root(self) -> Path:
        ctx = self.ctx
        for candidate in (
            getattr(ctx, "workspace_root", None),
            getattr(ctx, "inbox_dir", None),
        ):
            if candidate is not None:
                return Path(candidate)
        return Path.cwd()

    def _artifacts(self) -> Any:
        # 注意不能用 `a or b`：OutputStore 定义了 __len__，空库时是 falsy。
        store = getattr(self.ctx, "artifacts", None)
        if store is None:
            store = self._artifacts_override
        if store is None:
            raise RealmError(
                "core realm 需要 artifacts 端口（装配进 Agent 时由框架提供；"
                "独立使用时用 CoreToolRealm(spec, artifacts=...) 传入一个 ArtifactStore）"
            )
        return store

    def _max_output_chars(self) -> int:
        raw = self.params.get("max_tool_output_chars", DEFAULT_MAX_OUTPUT_CHARS)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise RealmError(
                f"core realm 的 max_tool_output_chars 非法: {raw!r}（需要 >= 0 的整数）"
            )
        return raw

    def __repr__(self) -> str:  # pragma: no cover - 日志可读性
        return f"<CoreToolRealm id={self.id!r} tools={len(self._tools)}>"


def _parse_names(raw: Any, *, source: str) -> tuple[str, ...]:
    """把配置里的工具名归一化成去重元组；类型不对直接报错（不猜）。"""
    if isinstance(raw, str):
        items: Sequence[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = raw
    else:
        raise RealmError(f"{source} 需要字符串或字符串序列，收到 {type(raw).__name__}")
    names: list[str] = []
    for item in items:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


__all__ = ["CORE_REALM_ID", "DEFAULT_MAX_OUTPUT_CHARS", "CoreToolRealm"]
