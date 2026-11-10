"""liorium.skills —— 技能能力包（``[skills]`` extra）。

计划依据 §Phase 2 / §F2 / §3.6：

- 挂载点 ``/skills``（scheme ``skills://``），技能路径 ``skills://<slug>/SKILL.md``
- ``SkillsRealm`` 提供挂载树 + ``list_skills`` 工具 + 预载进 prompt 的技能清单
- 技能清单变化 ⇒ ``CacheEpoch +1``（清单在 system prompt 里，工具 schema 不变）

用法：

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.skills import SkillsRealm

agent = LioriumAgent(
    spec=AgentSpec(..., realms=[RealmSpec(id="skills")]),
    realm_factories={"skills": SkillsRealm},
    ...
)
```

默认技能目录是 ``<inbox_dir>/skill.d``（与既有实现的目录布局一致），
也可以在 ``RealmSpec.params["skills_dir"]`` 里指到别处。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .catalog import Skill, catalog_signature, discover_skills
from .frontmatter import read_skill_meta, split_frontmatter
from .realm import MOUNT_PREFIX, SCHEME, SKILLS_DIR_NAME, SkillsRealm

__all__ = [
    "MOUNT_PREFIX",
    "SCHEME",
    "SKILLS_DIR_NAME",
    "Skill",
    "SkillsRealm",
    "catalog_signature",
    "discover_skills",
    "read_skill_meta",
    "split_frontmatter",
    "__version__",
]
