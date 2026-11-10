"""Liorium：可嵌入的 LLM Agent 框架。

一个发行版（``pip install liorium``）内含全部模块：

- ``liorium.core`` —— 类型 / URI / 指纹 / 日志（代码层面零第三方依赖）
- ``liorium.vfs`` —— 虚拟文件系统（挂载树 / 读写锁，代码层面零第三方依赖）
- ``liorium.realm_base`` —— realm 契约与写作 API（``BaseRealm`` / ``@realm_tool``）
- ``liorium.core_tool`` / ``liorium.core_tool_realm`` —— 六个核心工具与其 realm 外壳
- ``liorium.agent`` —— Agent 运行时（turn loop / scheduler / middlewares / realms）
- ``liorium.memory`` / ``liorium.skills`` / ``liorium.mcp`` / ``liorium.todo_realm`` —— 能力包
- ``liorium.provider`` —— LLM provider 管理（schema / 四种 API 格式 / models.dev）
- ``liorium.cli`` —— 命令行 Agent（``liorium-cli`` 入口）

依赖分档（只有第三方库才需要选装）：

- 基础安装：``pip install liorium`` —— langchain / langgraph / pydantic / httpx
- 全量能力：``pip install "liorium[all]"`` —— 再加 MCP / 向量记忆 / 三家 provider 适配 / 图片
- 单项：``[mcp]`` / ``[vector]`` / ``[openai]`` / ``[anthropic]`` / ``[google]`` / ``[images]``

``core`` 与 ``vfs`` 的**代码**仍然零第三方依赖（有架构守卫测试钉着），
所以只想要 URI / VFS 时也可以把这两个目录单独 vendored 走。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
