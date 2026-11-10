"""``McpRealm`` —— MCP 能力包（``[mcp]`` extra）。

计划依据：§B2（MCP 工具**不进** spec，运行时发现）、§B6（宿主 mark dirty →
回合边界 reconcile）、§Phase 2（"宿主 mark dirty"接法）。

行为：

- ``RealmSpec.params["servers"]`` 只写**要连哪些 server**（id → 配置）；
  工具在 ``start()`` 时发现，按 ``<server>_<tool>`` 暴露；
- 单个 server 连不上 ⇒ 记 ``error``、跳过它，其它 server 的工照常可用（降级但可用）；
  所有 server 都连不上且 ``strict=True`` ⇒ realm 启动失败（由 RealmManager 标 failed）；
- 宿主改了 ``servers`` 后调用 ``realm.set_servers(...)`` 或 ``realm.mark_dirty()``，
  Agent 在**下一个回合边界**重连并换工具集（工具 schema 变 ⇒ ``CacheEpoch +1``）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable, Mapping

from liorium.core.log import get_logger

from .client import McpClient, McpError, McpServerConfig, McpToolSpec

log = get_logger("liorium.mcp.realm")

MOUNT_PREFIX = ""
"""MCP 不提供 VFS 挂载（工具本身就是能力面）；保留常量让宿主知道"没有挂载"。"""


class McpRealm:
    """MCP realm：运行时发现工具 + mark dirty 重连。"""

    id = "mcp"
    requires: tuple[str, ...] = ()
    state_scope = "agent"
    instance_scope = "tree"
    """整棵 Agent 树一份：子进程连接与挂载点都不能开两份。"""

    def __init__(
        self,
        spec: Any = None,
        *,
        client_factory: Callable[[McpServerConfig], McpClient] | None = None,
    ) -> None:
        params = dict(getattr(spec, "params", {}) or {})
        self.servers: list[McpServerConfig] = _parse_servers(params.get("servers"))
        self.prefix_tools = bool(params.get("prefix_tools", True))
        self.strict = bool(params.get("strict", False))
        self.client_factory: Callable[[McpServerConfig], McpClient] = (
            client_factory or _default_client_factory()
        )

        self._clients: dict[str, McpClient] = {}
        self._configs: dict[str, McpServerConfig] = {}
        self._specs: dict[str, list[McpToolSpec]] = {}
        self._errors: dict[str, str] = {}
        self._tools: list[Any] | None = None
        self._dirty = False
        self._signature = self._desired_signature()
        self._ctx: Any = None
        # 失败退避：坏 server 不能每回合都重连；到点后 dirty() 才允许再试。
        self._retry_initial = max(0.01, float(params.get("retry_initial", 30.0)))
        self._retry_max = max(self._retry_initial, float(params.get("retry_max", 300.0)))
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._stale: set[str] = set()
        """调用失败/半死的 client：下一次 reconcile 先关掉再重连。"""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, ctx: Any) -> None:
        self._ctx = ctx
        await self._reconcile()
        if self.enabled_servers and not self._clients and self.strict:
            errors = "; ".join(f"{key}: {value}" for key, value in self._errors.items())
            raise McpError(f"所有 MCP server 都连接失败: {errors}")
        if self._errors:
            log.warning("MCP 部分 server 不可用: %s", self._errors)
        await ctx.state.update_many({"servers": self.status()})
        log.info(
            "McpRealm 启动: servers=%d connected=%d tools=%d",
            len(self.enabled_servers),
            len(self._clients),
            len(self.tool_names()),
        )

    async def stop(self, ctx: Any) -> None:
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 - 关不掉也要继续关下一个
                log.error("MCP client 关闭失败: %s: %s", getattr(client, "server_id", "?"), exc)
        self._clients.clear()
        self._specs.clear()
        self._tools = None
        self._ctx = None
        self._failures.clear()
        self._retry_at.clear()
        self._stale.clear()

    async def refresh(self) -> None:
        """重连（宿主 dirty 后由 RealmManager 在回合边界调用）。"""
        await self._reconcile()
        log.info(
            "McpRealm 刷新: servers=%d connected=%d tools=%d",
            len(self.enabled_servers),
            len(self._clients),
            len(self.tool_names()),
        )

    # ------------------------------------------------------------------
    # realm 协议
    # ------------------------------------------------------------------

    def tools(self) -> list[Any]:
        if self._tools is None:
            from .tools import build_mcp_tools

            self._tools = build_mcp_tools(self)
        return list(self._tools)

    def prompt_fragment(self) -> str:
        if not self._clients:
            return ""
        lines = ["MCP 外部工具（名字形如 `<server>_<tool>`）："]
        for server_id in sorted(self._clients):
            names = ", ".join(spec.name for spec in self._specs.get(server_id, []))
            lines.append(f"- {server_id}：{names or '（没有工具）'}")
        lines.append("调用前先读工具描述，参数按描述给；server 没连上就如实说明，不要假装调用。")
        return "\n".join(lines)

    def middleware(self) -> list[Any]:
        """MCP 不追加中间件（工具本身即能力面）。"""
        return []

    def mounts(self) -> dict[str, Any]:
        """MCP 不挂文件系统。

        必须显式返回空 dict：``RealmManager`` 装配时会无条件调 ``mounts()``，
        缺这个方法会让经 ``AgentSpec`` 装配的 MCP realm 直接标 failed。
        """
        return {}

    def skills(self) -> list[str]:
        return []

    def dirty(self) -> bool:
        if self._dirty or self._desired_signature() != self._signature:
            return True
        if not self._errors:
            return False
        now = time.monotonic()
        return any(now >= self._retry_at.get(server_id, 0.0) for server_id in self._errors)

    def mark_dirty(self, flag: bool = True) -> None:
        """宿主标记"配置变了，下个回合边界重连"（立即重试，清掉退避）。"""
        self._dirty = bool(flag)
        if flag:
            self._tools = None
            self._retry_at.clear()
            self._failures.clear()
            self._stale.clear()

    def clear_dirty(self) -> None:
        self._dirty = False
        self._signature = self._desired_signature()

    # ------------------------------------------------------------------
    # 宿主面
    # ------------------------------------------------------------------

    def set_servers(self, servers: Iterable[McpServerConfig | Mapping[str, Any]] | Mapping[str, Any]) -> None:
        """替换 server 列表并标 dirty（宿主"改配置"的唯一入口）。"""
        if isinstance(servers, Mapping):
            items: Iterable[Any] = [
                {"id": str(key), **dict(value or {})} for key, value in servers.items()
            ]
        else:
            items = servers
        parsed: list[McpServerConfig] = []
        for item in items:
            if isinstance(item, McpServerConfig):
                parsed.append(item)
            elif isinstance(item, Mapping):
                data = dict(item)
                server_id = str(data.pop("id", "") or "")
                parsed.append(McpServerConfig.from_dict(server_id, data))
            else:
                raise TypeError(f"servers 里出现不支持的类型: {type(item).__name__}")
        self.servers = parsed
        self.mark_dirty()

    @property
    def enabled_servers(self) -> list[McpServerConfig]:
        return [config for config in self.servers if config.enabled]

    @property
    def clients(self) -> dict[str, McpClient]:
        return dict(self._clients)

    @property
    def tool_specs(self) -> dict[str, list[McpToolSpec]]:
        return {key: list(value) for key, value in self._specs.items()}

    def tool_names(self) -> list[str]:
        names: list[str] = []
        for server_id in sorted(self._specs):
            for spec in self._specs[server_id]:
                names.append(self.exposed_name(server_id, spec.name))
        return sorted(names)

    def exposed_name(self, server_id: str, tool_name: str) -> str:
        if not self.prefix_tools or tool_name.startswith(f"{server_id}_"):
            return tool_name
        return f"{server_id}_{tool_name}"

    def status(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for config in self.servers:
            connected = config.id in self._clients
            out.append(
                {
                    "id": config.id,
                    "enabled": config.enabled,
                    "transport": config.transport,
                    "connected": connected,
                    "tools": [spec.name for spec in self._specs.get(config.id, [])],
                    "error": self._errors.get(config.id, ""),
                }
            )
        return out

    async def call_tool(self, server_id: str, tool_name: str, arguments: Mapping[str, Any]) -> str:
        client = self._clients.get(server_id)
        if client is None:
            raise McpError(f"MCP server 未连接: {server_id}")
        try:
            config = self._configs.get(server_id)
            timeout = float(config.call_timeout) if config is not None else 60.0
            result = await asyncio.wait_for(
                client.call_tool(tool_name, dict(arguments or {})), timeout=timeout
            )
            self._errors.pop(server_id, None)
            self._retry_at.pop(server_id, None)
            self._failures.pop(server_id, None)
            return result
        except Exception as exc:  # noqa: BLE001 - 调用失败要能被下一轮 reconcile 自愈
            self._errors[server_id] = f"{type(exc).__name__}: {exc}"
            # 标记为 stale：reconcile 会关掉这个 client 再重连，而不是继续用一个
            # 已经坏掉的 session。
            self._stale.add(server_id)
            self._record_failure(server_id)
            log.error("MCP 工具调用失败（已排入重连退避）: %s/%s: %s", server_id, tool_name, exc)
            raise

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _desired_signature(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            sorted(
                (
                    config.id,
                    config.transport,
                    config.command,
                    config.args,
                    config.url,
                    tuple(sorted(config.headers.items())),
                    config.enabled,
                    config.connect_timeout,
                    config.call_timeout,
                )
                for config in self.servers
            )
        )

    async def _reconcile(self) -> None:
        desired = {config.id: config for config in self.enabled_servers}

        # 1) 关掉不再需要 / 配置变了的 / 已判定坏掉的 client
        for server_id in list(self._clients):
            if (
                server_id not in desired
                or desired[server_id] != self._configs.get(server_id)
                or server_id in self._stale
            ):
                await self._close_client(server_id)

        # 2) 连新的 / 重连变了的
        for server_id, config in desired.items():
            if server_id in self._clients:
                continue
            client: McpClient | None = None
            try:
                client = self.client_factory(config)
                # 即使 client 实现自己没做超时（自定义端口），realm 层也兜一层，
                # 保证"连上但不回包"不会挂死 start()/整回合。
                await asyncio.wait_for(
                    client.connect(), timeout=float(config.connect_timeout)
                )
                specs = await asyncio.wait_for(
                    client.list_tools(), timeout=float(config.call_timeout)
                )
            except Exception as exc:  # noqa: BLE001 - 单 server 失败不拖垮整个 realm
                # 半开连接的 client 必须关掉，否则子进程/HTTP 连接会泄漏。
                if client is not None:
                    try:
                        await client.close()
                    except Exception as close_exc:  # noqa: BLE001
                        log.error("MCP 失败 client 关闭失败: %s: %s", server_id, close_exc)
                self._errors[server_id] = f"{type(exc).__name__}: {exc}"
                self._specs[server_id] = []
                self._clients.pop(server_id, None)
                self._configs.pop(server_id, None)
                self._record_failure(server_id)
                log.error("MCP server 连接失败: %s: %s", server_id, exc)
                continue
            self._errors.pop(server_id, None)
            self._failures.pop(server_id, None)
            self._retry_at.pop(server_id, None)
            self._clients[server_id] = client
            self._configs[server_id] = config
            self._specs[server_id] = list(specs)

        # 3) 清掉已从配置里移除的 server 的残留
        for server_id in list(self._specs):
            if server_id not in self._clients:
                self._specs.pop(server_id, None)
        for server_id in list(self._errors):
            if server_id not in desired:
                self._errors.pop(server_id, None)
                self._failures.pop(server_id, None)
                self._retry_at.pop(server_id, None)
        self._tools = None
        self._signature = self._desired_signature()
        # 配置层 dirty 清掉；失败 server 的"何时再试"由 _retry_at 控制。
        self._dirty = False

    def _record_failure(self, server_id: str) -> None:
        """指数退避：坏 server 不会每回合都被重连。"""
        count = self._failures.get(server_id, 0) + 1
        self._failures[server_id] = count
        delay = min(self._retry_max, self._retry_initial * (2 ** (count - 1)))
        self._retry_at[server_id] = time.monotonic() + delay

    async def _close_client(self, server_id: str) -> None:
        client = self._clients.pop(server_id, None)
        self._configs.pop(server_id, None)
        self._stale.discard(server_id)
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001
            log.error("MCP client 关闭失败: %s: %s", server_id, exc)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _default_client_factory() -> Callable[[McpServerConfig], McpClient]:
    from .client import SdkMcpClient

    return SdkMcpClient


def _parse_servers(raw: Any) -> list[McpServerConfig]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [McpServerConfig.from_dict(str(key), dict(value or {})) for key, value in raw.items()]
    if isinstance(raw, (list, tuple)):
        out: list[McpServerConfig] = []
        for item in raw:
            if isinstance(item, McpServerConfig):
                out.append(item)
            elif isinstance(item, Mapping):
                data = dict(item)
                server_id = str(data.pop("id", "") or "")
                out.append(McpServerConfig.from_dict(server_id, data))
            else:
                raise TypeError(f"servers 里出现不支持的类型: {type(item).__name__}")
        return out
    raise TypeError(f"servers 必须是对象或数组，得到 {type(raw).__name__}")


__all__ = ["MOUNT_PREFIX", "McpRealm"]
