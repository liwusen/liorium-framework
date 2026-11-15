# liorium-profile

Agent Profile：一个 JSON（Pydantic 严格校验）描述「创建一个 Liorium Agent 的全部信息」。
它编译成三件套：

- `AgentSpec`（运行期唯一真相）
- `realm_factories`（`id -> 类`，类本身就是工厂：`RealmClass(spec)`）
- `ChatModelPool`（懒建 + 按用途缓存 + **谁造谁关**）

设计记录见 [`docs/plans/2026-11-14-agent-profile.md`](../../docs/plans/2026-11-14-agent-profile.md)。

## 最小 profile

```json
{
  "version": 1,
  "id": "cli",
  "provider_file": "providers.json",
  "model": "Go::deepseek-v4.1-flash",
  "realms": [
    {"id": "core", "source": "liorium.core_tool_realm:CoreToolRealm"},
    {"id": "subagents"}
  ]
}
```

## 用法

```python
from liorium.profile import AgentProfile

profile = AgentProfile.load("~/.liorium/profile.json")   # 加载期就校验（会 import realm 类）
agent = profile.build_agent()                             # 未 start
await agent.start()
...
await agent.close()          # worker → realms → checkpoint → 模型池 aclose
```

需要自己接管端口 / 共享 registry 时用两次调用：

```python
build = profile.build(registry=my_registry, model="Go::deepseek-v4.1-flash")
agent = build.create_agent(vfs=my_vfs, checkpoint_path=...)
...
await agent.close()
await build.close()          # 幂等兜底；只关模型池
```

## 语义要点

- **严格**：`version: 1` 必填、`extra="forbid"`；错误消息带 JSON 路径
  （`realms[1].config.vectors: ...`）。
- **路径**：`~` 与 `${VAR}` 在**所有字符串值**里展开（`$$` 转义字面 `$`，未定义变量报错）。
  `provider_file` 与文件型 realm 的 `source` 相对 profile 目录；`prompt` 相对 `inbox`；
  `workspace` 默认启动时 cwd。
- **realm**：`source` 是 `pkg.mod:Class` 或 `./file.py:Class`（`rsplit(":", 1)`，支持
  `Outer.Inner`）。内置 `subagents` 可以省略 `source`。文件型 realm 按 mtime 重载，
  单文件自包含（不支持相对导入）。
- **realm 配置**：类可选声明 `Config`（Pydantic `BaseModel`，`extra="forbid"`，全默认值）；
  声明了就由 `load()` 校验 + 填默认，没声明就原样透传给 realm。
- **模型**：`model: null` 时用 provider 的 `main_model`；aux / compact 永远不思考。
  `limits.context_window` 没显式写时从 provider 元数据补。
- **安全**：`load()` 会执行本地代码（realm 类），profile 是**受信任的本地配置**；
  `api_key` 支持 `${ENV}` 或明文（明文时请把文件权限设成 0600）。

## 测试

```bash
uv run pytest packages/liorium-profile -q
```
