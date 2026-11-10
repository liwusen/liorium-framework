# Liorium Provider 管理迁移与思考接入（决策记录）

日期：2026-11-07。状态：已实施。入口代码：`packages/liorium-provider-manager`。

## 1. 目标

把既有实现 `backend/llm/` 的 LLM Provider 管理完整迁到 Liorium：

- 配置 schema（`providers.json` 兼容）与持久化；
- 四种 API 格式：`openai_chat` / `openai_responses` / `anthropic_messages` / `google_gemini`；
- 格式优先级、端点归一化、多格式 Provider 回退；
- Provider `/models` 自动发现与元数据补全；
- models.dev 目录下载/缓存/匹配/路由/enrich；
- 思考能力解析与四种格式的思考参数；
- `liorium-cli` 接入，并支持运行中切换思考。

## 2. 已确认决策

| 决策 | 结论 |
|---|---|
| 思考默认值 | 默认 `medium` 开启；`--thinking off|low|medium|high`；`--no-thinking` = off |
| 思考显示 | `--hide-reasoning` 只隐藏 `reasoning_delta`，不改变是否请求 |
| 运行中切换 | `/thinking` + `LioriumAgent.apply(force=True)` |
| models.dev | 启动只读本地缓存；`--models-dev refresh` 才联网；只改内存、不回写配置 |
| 包路径 | 发行名 `liorium-provider-manager`，模块 `liorium.provider` |
| 数据模型 | Pydantic v2；未知字段保留；`key` 字段名不变 |
| 默认方言 | `thinking_type="auto"`，按 provider/model 名字推断，回落 `openai` |

## 3. 包结构

```
packages/liorium-provider-manager/src/liorium/provider/
├── schema.py          # ModelProvider / ModelProviders / loads / dumps / 选择辅助
├── discovery.py       # /models 拉取、元数据提取、auto_load
├── formats.py         # 格式检测/路由/端点归一化/模型构建/服务端工具
├── thinking.py        # 预设、按格式参数、ReasoningChatOpenAI、方言
├── thinking_caps.py   # 能力解析/优先级/档位/预算
├── catalog.py         # models.dev 下载/缓存/匹配/路由/enrich
├── registry.py        # ProviderRegistry / ResolvedModel
└── constants.py       # 默认 UA / OpenCode session 头
```

依赖：`liorium-core`、`pydantic>=2`、`httpx`、`langchain-core`；模型 SDK 走
`[openai]` / `[anthropic]` / `[google]` / `[all]` 可选依赖。

## 4. 关键行为

### 格式优先级

`model_api_formats` > `provider.api_format` > `model_catalog_routes` > URL 路径 >
多格式 Provider 的模型族 > `openai_chat` fallback。

端点：`format_base_urls[format]` > 目录路由（仅 host 匹配且格式一致）>
`provider.base_url`。显式格式无端点时报错。

### 思考

- 优先级：手动 `model_thinking_modes` > `/models` 自报 > models.dev > 启发式。
- `reasoning is False` 时不发送参数。
- OpenAI Chat 按方言预设；Responses 用 `reasoning.effort`；Anthropic 用
  `thinking.budget_tokens`；Gemini 用 `thinking_budget` 或 `reasoning_effort` +
  `include_thoughts`。
- 思考开启时不发送 `temperature`；`aux` / `compact` 永远不思考。
- `ReasoningChatOpenAI` 保留 `reasoning_content` 到 `additional_kwargs`。

### models.dev

- 缓存默认 `~/.liorium/cache/models_dev/api.json`，TTL 24h；下载失败保留旧缓存。
- 匹配：pin 优先；同 host 强匹配；不同 host 只按模型名覆盖率（≥0.8 且 ≥2 个模型）
  采用格式，端点仍用用户 `base_url`。
- `enrich_provider` 整表重算路由，只填空缺的 context/output，不覆盖用户值。

## 5. CLI 接入

- `liorium/cli/providers.py` 只保留宿主策略（配置搜索、环境变量、向导、保存）。
- 新参数：`--thinking`、`--no-thinking`、`--hide-reasoning`、`--models-dev`、
  `--models-dev-cache`、`--refresh-models`。
- 新命令：`/thinking [off|low|medium|high]`、`/models-dev [status|refresh]`。
- `LioriumAgent` 配套：`apply(force=True)`、`ApplyResult.forced`、
  `compact_model_factory`（摘要调用不带思考参数）。

## 6. 验证

- `uv run pytest -q -p no:cacheprovider`：1106 passed, 3 skipped。
- 新包测试：195 passed。
- `uv run liorium-cli --list-models` 对既有实现配置可输出格式/上下文/输出/思考形状。
- 架构守卫：新包只允许 `liorium.core` / `liorium.provider`，不 import `liorium.agent`。
