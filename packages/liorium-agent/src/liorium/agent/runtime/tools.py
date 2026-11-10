"""工具输出拦截：正式结果进 ``OutputStore``，模型只看摘要 / artifact 引用。

计划 §F1：工具集由 spec 决定，registry 零全局过滤；本模块只负责「输出搬运」，
不参与注册、不做 plugin hook（计划 §A2 已删除 pluggy 通道）。

行为（``docs/design/vfs-output-mw.md`` §4.6）：

- 普通结果（``str`` 原样 / ``dict``、``list`` → JSON / 其它 ``str()``）：
  长度 ``<= max_chars`` **且**不含换行 → 原样返回；否则 ``put`` + ``summary``；
- 多模态结果（``{"kind": "multimodal_tool_result"}``，dict 或 JSON 字符串）：
  图片写 ``put_multimodal``，返回引用 JSON，文本里只留
  ``[N 张图片已保存: artifact://<id>]``；已是引用形态（有 ``artifact`` 无
  ``images``）时不重复存；
- 工具抛异常 → 存错误文本 + ``工具执行出错\n[完整输出: artifact://<id>]``；
- 包装幂等（``_artifact_wrapped`` 标记），async ``@tool`` 只包 ``_arun``，
  sync ``@tool`` 包 ``_run`` 并让 ``_arun`` 走 ``asyncio.to_thread``。

本模块只依赖 ``langchain_core`` 和同目录的 ``output_store``，不 import 任何
``liorium.agent`` 之外的业务包。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from functools import wraps
from typing import Any, Sequence

from langchain_core.tools import BaseTool

from liorium.core.log import get_logger

from .output_store import OutputStore

logger = get_logger("agent.tools")

#: 多模态工具结果的 kind 标记（与 ``read`` / ``mm_bridge`` 共用）。
MULTIMODAL_KIND = "multimodal_tool_result"

#: 工具抛异常时的逐字前缀（``vfs-output-mw.md`` §4.6；``[完整输出: ...]`` 冻结）。
TOOL_ERROR_PREFIX = "工具执行出错\n[完整输出: artifact://"


def _resolve_config(config: Any) -> Any:
    """langchain 1.4+ 的 ``_run`` 期望 config；直接调用时兜底为空 dict。"""
    return config if config is not None else {}


def _summarize_result(store: OutputStore, tool_name: str, result: Any, max_chars: int) -> str:
    """把一次工具返回值搬进 store，返回给模型看的那段文本。"""
    payload = _as_multimodal_payload(result)
    if payload is not None:
        # 已经是引用形态（read artifact:// 的产物）→ 不重复存副本。
        if payload.get("artifact") and not payload.get("images"):
            return json.dumps(payload, ensure_ascii=False)
        artifact_id = store.put_multimodal(payload, tool_name=tool_name)
        text = str(payload.get("text") or "")
        count = len(payload.get("images") or [])
        return json.dumps(
            {
                "kind": MULTIMODAL_KIND,
                "text": f"{text}\n[{count} 张图片已保存: artifact://{artifact_id}]",
                "artifact": artifact_id,
            },
            ensure_ascii=False,
        )

    if isinstance(result, str):
        output = result
    elif isinstance(result, (dict, list)):
        output = json.dumps(result, ensure_ascii=False)
    else:
        output = str(result)

    # 短且单行的结果不值得为它建 artifact。
    if len(output) <= max_chars and "\n" not in output:
        return output
    artifact_id = store.put(output, tool_name=tool_name)
    return store.summary(artifact_id)


def _as_multimodal_payload(result: Any) -> dict[str, Any] | None:
    """识别 ``kind == multimodal_tool_result`` 的结果（dict 或 JSON 字符串）。"""
    data: Any
    if isinstance(result, dict):
        data = result
    elif isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        return None
    if isinstance(data, dict) and data.get("kind") == MULTIMODAL_KIND:
        return data
    return None


def _wrap_error(store: OutputStore, tool_name: str, error: BaseException) -> str:
    """把异常文本存进 store，返回逐字错误摘要。"""
    artifact_id = store.put(
        str(error),
        tool_name=tool_name,
        metadata={"status": "error"},
    )
    return f"{TOOL_ERROR_PREFIX}{artifact_id}]"


def wrap_tool_output(
    tool: BaseTool,
    *,
    artifacts: OutputStore,
    max_chars: int = 120,
) -> BaseTool:
    """包装单个工具，使它的返回值先经过 ``OutputStore``。

    同一工具对象重复包装是幂等的（``_artifact_wrapped`` 标记）：主 Agent 与
    Subagent 共享同一批工具对象，二次包装会造成重复存储与嵌套 artifact。
    """
    if getattr(tool, "_artifact_wrapped", False):
        return tool
    tool._artifact_wrapped = True  # type: ignore[attr-defined]

    tool_name = tool.name
    original_coroutine = getattr(tool, "coroutine", None)

    if original_coroutine is not None and inspect.iscoroutinefunction(original_coroutine):
        # ── async @tool：只包 _arun，直接委托原生协程 ──
        @wraps(original_coroutine)
        async def _wrapped_arun(*args: Any, **kwargs: Any) -> str:
            try:
                result = await original_coroutine(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - 工具失败要变成可读结果
                return _wrap_error(artifacts, tool_name, error)
            return _summarize_result(artifacts, tool_name, result, max_chars)

        tool._arun = _wrapped_arun  # type: ignore[method-assign]
        return tool

    original_run = getattr(tool, "_run", None)
    if original_run is not None and not inspect.iscoroutinefunction(original_run):
        # ── sync @tool：包 _run（保留原签名供 langchain 注入 config/run_manager），
        #    并让 _arun 走线程池，避免同步工具阻塞事件循环 ──
        @wraps(original_run)
        def _wrapped_run(*args: Any, config: Any = None, run_manager: Any = None, **kwargs: Any) -> str:
            try:
                result = original_run(
                    *args,
                    config=_resolve_config(config),
                    run_manager=run_manager,
                    **kwargs,
                )
            except Exception as error:  # noqa: BLE001 - 工具失败要变成可读结果
                return _wrap_error(artifacts, tool_name, error)
            return _summarize_result(artifacts, tool_name, result, max_chars)

        tool._run = _wrapped_run  # type: ignore[method-assign]

        @wraps(original_run)
        async def _wrapped_arun_sync(
            *args: Any, config: Any = None, run_manager: Any = None, **kwargs: Any
        ) -> str:
            return await asyncio.to_thread(
                _wrapped_run,
                *args,
                config=config,
                run_manager=run_manager,
                **kwargs,
            )

        tool._arun = _wrapped_arun_sync  # type: ignore[method-assign]

    return tool


def wrap_tools(
    tools: Sequence[Any],
    *,
    artifacts: OutputStore,
    max_chars: int = 120,
) -> list[BaseTool]:
    """批量包装工具；非 ``BaseTool`` 条目跳过但**留日志**。

    裸 callable 是宿主遗留的脏数据，不应让整条工具链在启动期崩掉；
    但"静默跳过"会让工具装了却没生效，所以每条都要在日志里可见。
    **工具名冲突**仍由注册表抛 ``ToolNameConflictError``，不在这里处理。
    """
    wrapped: list[BaseTool] = []
    for tool in tools:
        if not isinstance(tool, BaseTool):
            logger.warning(
                "跳过非 LangChain 工具（用 @tool 包装后再交给 Agent）: %r",
                getattr(tool, "__name__", tool),
            )
            continue
        wrapped.append(wrap_tool_output(tool, artifacts=artifacts, max_chars=max_chars))
    return wrapped


__all__ = ["MULTIMODAL_KIND", "TOOL_ERROR_PREFIX", "wrap_tool_output", "wrap_tools"]
