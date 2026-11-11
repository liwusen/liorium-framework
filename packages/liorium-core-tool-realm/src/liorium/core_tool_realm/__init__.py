"""liorium.core_tool_realm —— 六个核心工具的能力包。

工具实现仍在 ``liorium-core-tool``（``ToolRuntime`` + ``build_core_tools``）；本包只负责
把它装配成 realm：由用户在 ``AgentSpec.realms`` 里声明 ``RealmSpec(id="core")``、
在 ``realm_factories`` 里注册 ``CoreToolRealm``，Agent 自己不再隐式添加任何工具。
"""

from __future__ import annotations

__version__ = "0.1.1"

from .realm import CORE_REALM_ID, CoreToolRealm

__all__ = [
    "CORE_REALM_ID",
    "CoreToolRealm",
    "__version__",
]
