# liorium-provider-manager

Liorium 的 LLM Provider 管理库（模块 `liorium.provider`）：配置 schema、四种 API 格式、
思考（reasoning）参数、模型构建，以及 models.dev 目录的下载 / 缓存 / 匹配 / 路由。

它**不读任何产品配置文件**：路径、环境变量、交互向导
都由宿主（例如 `liorium-cli`）提供。

## 安装

```powershell
# 只做配置解析 / 目录识别（不装任何模型 SDK）
uv add liorium-provider-manager

# 需要构建模型时按格式装 extra
uv add "liorium-provider-manager[openai]"
uv add "liorium-provider-manager[all]"
```

## 最小用法

```python
from liorium.provider import ProviderRegistry

registry = ProviderRegistry.load("providers.json")
resolved = registry.resolve("Go::deepseek-v4.1-flash")
print(resolved.describe())

model = registry.make_chat_model(
    resolved,
    intensity="medium",      # off / low / medium / high；None = 不启用
    temperature=0.3,         # 思考开启时不发送
)
```

`providers.json` 的 schema：

```json
{
  "main_model": "Go::deepseek-v4.1-flash",
  "providers": [
    {
      "name": "Go",
      "base_url": "https://opencode.ai/zen/go/v1",
      "key": "oc_sk_...",
      "models": ["deepseek-v4.1-flash", "glm-5.2"],
      "model_context_lengths": {"deepseek-v4.1-flash": 1048576},
      "model_api_formats": {"deepseek-v4.1-flash": "openai_responses"},
      "model_catalog_routes": {
        "deepseek-v4.1-flash": {
          "format": "openai_chat",
          "base_url": "https://opencode.ai/inference/go/openai/v1",
          "host_matched": true
        }
      },
      "thinking_type": "auto"
    }
  ]
}
```

## 四种 API 格式

`openai_chat` / `openai_responses` / `anthropic_messages` / `google_gemini`。
格式优先级（高 → 低）：

1. `model_api_formats[model]`（用户显式覆盖）
2. `provider.api_format`（非 `auto`）
3. `model_catalog_routes[model].format`（models.dev）
4. `base_url` 路径启发式
5. 多格式 Provider 的模型族（`claude*` / `gemini*` / `gpt-5|o3|...`）
6. `openai_chat` 兜底

端点：`format_base_urls[format]` > 目录路由（仅 host 相同）> `provider.base_url`。
显式格式没有可用端点时**报错**，绝不把请求发到别的主机。

## 思考

思考形状来自：手动 `model_thinking_modes` > Provider `/models` 自报 >
models.dev > 模型名启发式。

- OpenAI Chat：按 `thinking_type` 方言发送 `reasoning_effort` / `extra_body`
  （`openai` / `qwen` / `deepseek` / `glm` / `mimo` / `minimax` / `none` / `auto`）。
- OpenAI Responses：`reasoning={"effort": ...}`。
- Anthropic：`thinking={"type":"enabled","budget_tokens":N}`，预算不低于 1024，
  且 `max_tokens > budget`。
- Gemini：3+/effort 形状用 `reasoning_effort`，2.x/budget 形状用 `thinking_budget`，
  并带 `include_thoughts=True`。

`auto` 会按 provider/model 名字推断方言（`deepseek` / `qwen` / `glm` / `mimo` /
`minimax`，其余 `openai`）。`thinking_type="none"` 或 `intensity=None` 时不发送任何
思考参数；`purpose="aux"` / `"compact"` 也强制不思考。

OpenAI Chat 的 `reasoning_content` 会被 `ReasoningChatOpenAI` 保留到
`AIMessageChunk.additional_kwargs`，供 Liorium 的 `reasoning_delta` 事件读取。

## models.dev 目录

```python
import asyncio
from pathlib import Path
from liorium.provider import ProviderRegistry, ensure_catalog, enrich_all

registry = ProviderRegistry.load("providers.json")

async def main() -> None:
    # 只用本地缓存，不联网
    catalog = await ensure_catalog(allow_network=False)
    if catalog:
        enrich_all(registry.model_providers, catalog)   # 只改内存，不写配置

    # 显式联网刷新
    catalog = await ensure_catalog(force=True)
    print(catalog_status())

asyncio.run(main())
```

- 缓存默认 `~/.liorium/cache/models_dev/api.json`，可传 `cache_path=` 覆盖。
- TTL 24h；下载失败/超大/非 JSON 时保留旧缓存并记录 `last_error`。
- Provider 匹配：pin 优先；同 host 强匹配；不同 host 只按模型名重合（覆盖率 ≥ 0.8
  且至少 2 个模型）采用格式，端点仍用用户自己的 `base_url`。
- `enrich_provider` 只填空缺的 context/output，不覆盖用户手填值。

## `/models` 自动发现

```python
import asyncio
from liorium.provider import auto_load_model_for_provider, fetch_provider_model_entries

async def main() -> None:
    entries = await fetch_provider_model_entries(provider)
    # OpenAI data[] / 裸数组两种响应都支持；保留 base_url 路径前缀
    await auto_load_model_for_provider(provider, force=True)
```

自动填充 `model_context_lengths`、`model_max_output_tokens`、`model_api_capabilities`、
`model_thinking_caps`；只填空缺，force 刷新时裁剪已下线模型的陈旧元数据。

## 公开 API

按模块：

| 模块 | 主要名字 |
|---|---|
| `liorium.provider.schema` | `ModelProvider`、`ModelProviders`、`loads`、`dumps`、`parse_spec`、CRUD、选择辅助 |
| `liorium.provider.registry` | `ProviderRegistry`、`ResolvedModel` |
| `liorium.provider.formats` | `API_FORMATS`、`resolve_route`、`build_chat_model`、`build_chat_model_from_spec`、`describe_*`、`close_chat_model` |
| `liorium.provider.thinking` | `THINKING_PRESETS`、`get_format_thinking_params`、`ReasoningChatOpenAI`、`resolve_thinking_dialect` |
| `liorium.provider.thinking_caps` | `ThinkingCapability`、`ThinkingOption`、`resolve_thinking_capability`、`describe_thinking` |
| `liorium.provider.catalog` | `ensure_catalog`、`refresh_catalog`、`catalog_status`、`match_provider`、`enrich_provider` |
| `liorium.provider.discovery` | `fetch_provider_model_entries`、`auto_load_model_for_provider` |

## 设计取舍

- 不读 `~/.liorium`；缓存默认在 `~/.liorium`，由宿主决定是否指向旧缓存。
- `thinking_type` 默认 `auto`（按 provider/model 名字推断方言），显式配置仍优先。
- 不迁 `ainvoke_json` / `parse_json_object` / `forced_no_tool_choice`。
- 配置写盘改为原子写 + 0600；未知字段保留。

## 测试

```powershell
uv run pytest packages/liorium-provider-manager -q
# 可选：真实拉取 models.dev
uv run pytest packages/liorium-provider-manager -q -m live
```
