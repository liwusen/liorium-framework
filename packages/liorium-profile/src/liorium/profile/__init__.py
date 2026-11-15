"""``liorium.profile`` —— Agent Profile：JSON/Pydantic 装配配方。

一个 profile 描述「创建一个 Liorium Agent 的全部信息」：provider 文件、模型与思考强度、
要装的 realm 及其配置。它编译成 ``AgentSpec`` + realm 工厂表 + ``ChatModelPool``：

```python
from liorium.profile import AgentProfile

profile = AgentProfile.load("~/.liorium/profile.json")
agent = profile.build_agent()      # 未 start；模型池的关闭已挂到 agent.close()
await agent.start()
...
await agent.close()
```

设计记录：``docs/plans/2026-11-14-agent-profile.md``（唯一依据）。
"""

from __future__ import annotations

from .build import ProfileBuild, build_profile
from .errors import ProfileBuildError, ProfileError, ProfileSchemaError, ProfileSourceError
from .loader import from_dict, load
from .schema import AgentProfile, BootConfig, LimitsConfig, RealmEntry

__all__ = [
    "AgentProfile",
    "BootConfig",
    "LimitsConfig",
    "ProfileBuild",
    "ProfileBuildError",
    "ProfileError",
    "ProfileSchemaError",
    "ProfileSourceError",
    "RealmEntry",
    "build_profile",
    "from_dict",
    "load",
]
