# liorium-skills

Liorium 技能能力包（`[skills]` extra）。挂载点 `/skills` ⇔ `skills://`（只读）。

## 组成

| 模块 | 职责 |
|---|---|
| `catalog.py` | `discover_skills()`：扫描 `<skills_dir>/<slug>/SKILL.md`；`catalog_signature()` 用 mtime 指纹判断装/卸/改 |
| `frontmatter.py` | `SKILL.md` frontmatter 解析（有 PyYAML 用 PyYAML，没有就退化成 `key: value`）；`_meta.json` 优先 |
| `realm.py` | `SkillsRealm`：`/skills` 只读挂载 + 状态 + prompt 片段 + `dirty()` |
| `tools.py` | `list_skills`（计划里的 `listSkills`） |

技能清单（`skills()`）会预载进 system prompt 的「技能清单」段（计划 §F2），
所以装/卸技能 ⇒ 提示词变化 ⇒ `CacheEpoch +1`。清单最多
`RealmSpec.params["max_prompt_skills"]` 条（默认 20），超出部分给一句提示。

## 用法

```python
from liorium.agent import AgentSpec, RealmSpec, LioriumAgent
from liorium.skills import SkillsRealm

agent = LioriumAgent(
    spec=AgentSpec(
        id="main",
        inbox_dir=Path("agents/main"),
        model="deepseek::chat",
        prompt_sources=("AGENT.md",),
        realms=[RealmSpec(id="skills")],           # 默认读 <inbox>/skill.d
    ),
    model_factory=make_chat_model,
    realm_factories={"skills": SkillsRealm},
)
```

自定义目录：`RealmSpec(id="skills", params={"skills_dir": "elsewhere"})`。

模型侧：

- `read skills://` → 列技能（目录名 + 说明）
- `read skills://<slug>/SKILL.md` → 读技能说明；技能内其它文件同样可读
- `list_skills()` → JSON（slug / name / version / description / `skills://<slug>/SKILL.md`）

## 安装 / 卸载

安装是**宿主**的事（下载、校验、解压由产品侧决定），本包只负责发现与暴露。
把目录放进 `<inbox>/skill.d/` 后，下一个回合边界 `dirty()` 会让 Agent 自动重挂载并刷新清单。

```python
from liorium.skills import discover_skills

for skill in discover_skills(Path("agents/main/skill.d")):
    print(skill.slug, skill.version, skill.description)
```
