"""OutputStore：id 生成 / 摘要截断 / 多模态存取 / 持久化，以及工具输出包装。

规格：``docs/design/vfs-output-mw.md`` §4（逐字冻结文本在 §4.4）。
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path

from langchain_core.tools import tool

from liorium.agent.runtime.output_store import (
    DEFAULT_MAX_LINES,
    DEFAULT_MAX_SUMMARY,
    TRUNCATION_FOOTER,
    Artifact,
    OutputStore,
)
from liorium.agent.runtime.tools import wrap_tool_output, wrap_tools

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16


# ── id 生成 ──────────────────────────────────────────────────────────────


def test_id_generation_uses_short_tool_name() -> None:
    store = OutputStore()

    assert store.put("a", tool_name="sys_exec") == "shell_1"
    assert store.put("b", tool_name="python_exec") == "py_2"
    assert store.put("c", tool_name="list_directoryTool") == "ls_3"
    assert store.put("d") == "tool_4"
    assert store.put("e", tool_name="   ") == "tool_5"
    assert store.put("f", tool_name="VeryLongToolNameHere") == "verylongtool_6"


def test_id_generation_strips_known_suffixes() -> None:
    store = OutputStore()

    assert store.put("x", tool_name="read_text_file_tool") == "read_1"
    assert store.put("y", tool_name="someFunc") == "some_2"


def test_put_get_exists_and_contains() -> None:
    store = OutputStore()
    artifact_id = store.put("完整内容", tool_name="read")

    artifact = store.get(artifact_id)
    assert artifact is not None
    assert artifact.output_id == artifact_id
    assert artifact.content == "完整内容"
    assert artifact.tool_name == "read"
    assert artifact.content_type == "text"
    assert store.exists(artifact_id)
    assert artifact_id in store
    assert store.get("missing") is None
    assert not store.exists("missing")


def test_list_ids_oldest_first_and_peek() -> None:
    store = OutputStore()
    first = store.put("一", tool_name="shell")
    second = store.put("二", tool_name="shell")

    assert store.list_ids() == [first, second]
    peek = store.peek(first)
    assert peek is not None
    assert peek["output_id"] == first
    assert peek["tool_name"] == "shell"
    assert peek["lines"] == 1
    assert peek["size"] == 1
    assert store.peek("missing") is None


def test_pagination_is_zero_based() -> None:
    store = OutputStore()
    artifact_id = store.put("l0\nl1\nl2\nl3", tool_name="read")
    artifact = store.get(artifact_id)

    assert artifact is not None
    assert artifact.get() == "l0\nl1\nl2\nl3"
    assert artifact.get(offset=1, limit=2) == "l1\nl2"
    assert artifact.get(offset=3) == "l3"
    assert artifact.lines == 4


# ── 摘要 ─────────────────────────────────────────────────────────────────


def test_short_content_summary_is_verbatim() -> None:
    store = OutputStore()
    artifact_id = store.put("一行结果", tool_name="shell")

    assert store.summary(artifact_id) == "一行结果"


def test_empty_output_summary() -> None:
    store = OutputStore()
    artifact_id = store.put("", tool_name="shell")

    assert store.summary(artifact_id) == "(空输出)"


def test_summary_truncated_by_line_limit_with_frozen_footer() -> None:
    store = OutputStore()
    content = "\n".join(f"line{i}" for i in range(DEFAULT_MAX_LINES + 1))
    artifact_id = store.put(content, tool_name="shell")

    summary = store.summary(artifact_id)
    footer = TRUNCATION_FOOTER.format(output_id=artifact_id)
    assert summary.endswith(footer)
    assert summary.split("\n")[0] == "line0"
    # 300 行预览 + 1 行 footer
    assert len(summary.split("\n")) == DEFAULT_MAX_LINES + 1
    # footer 逐字（半角逗号与句点）
    assert (
        footer
        == "[内容过长,已经截断.请使用行数选择器读取完整输出: artifact://" + artifact_id + "]"
    )


def test_summary_truncated_by_char_limit_with_ellipsis() -> None:
    store = OutputStore()
    content = "x" * (DEFAULT_MAX_SUMMARY + 1)
    artifact_id = store.put(content, tool_name="shell")

    summary = store.summary(artifact_id)
    footer = TRUNCATION_FOOTER.format(output_id=artifact_id)
    assert summary.endswith(footer)
    preview = summary[: -len(footer) - 1]
    assert len(preview) == DEFAULT_MAX_SUMMARY + 1
    assert preview.endswith("…")


def test_summary_of_unknown_id() -> None:
    store = OutputStore()

    assert store.summary("ghost_9") == "[找不到 artifact: ghost_9]"


def test_summary_respects_custom_limits() -> None:
    store = OutputStore(max_chars=10, max_lines=2)
    artifact_id = store.put("0123456789abc\ndef\nghi", tool_name="shell")

    summary = store.summary(artifact_id)
    assert summary.startswith("0123456789…")
    assert summary.endswith(TRUNCATION_FOOTER.format(output_id=artifact_id))


# ── 多模态 ───────────────────────────────────────────────────────────────


def test_multimodal_roundtrip_in_memory() -> None:
    store = OutputStore()
    artifact_id = store.put_multimodal(
        {
            "kind": "multimodal_tool_result",
            "text": "截图",
            "images": [
                {"data": PNG_BYTES, "mime": "image/png"},
                {"data": JPEG_BYTES, "mime": "image/jpeg"},
            ],
        },
        tool_name="read",
    )

    artifact = store.get(artifact_id)
    assert artifact is not None
    assert artifact.output_id == "read_1"
    assert artifact.content_type == "multimodal"
    assert artifact.content == "截图"
    assert artifact.image_count == 2
    assert artifact.metadata["image_count"] == 2
    assert artifact.images[0] == {"data": PNG_BYTES, "mime": "image/png"}
    assert artifact.images[1] == {"data": JPEG_BYTES, "mime": "image/jpeg"}
    assert artifact.content_base64 == base64.b64encode(PNG_BYTES).decode("ascii")
    assert artifact.mime_type == "image/png"
    assert store.summary(artifact_id) == "[截图 — 2张图片: artifact://read_1]"


def test_multimodal_default_description_when_text_empty() -> None:
    store = OutputStore()
    artifact_id = store.put_multimodal({"images": [{"data": PNG_BYTES}]}, tool_name="read")
    artifact = store.get(artifact_id)

    assert artifact is not None
    assert artifact.mime_type == "image/png"
    assert store.summary(artifact_id) == "[图片输出 — 1张图片: artifact://read_1]"


def test_multimodal_accepts_data_url_images() -> None:
    store = OutputStore()
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    artifact_id = store.put_multimodal(
        {"text": "图", "images": [{"url": f"data:image/png;base64,{encoded}"}]},
        tool_name="read",
    )
    artifact = store.get(artifact_id)

    assert artifact is not None
    assert artifact.images[0]["data"] == PNG_BYTES
    assert artifact.images[0]["mime"] == "image/png"


def test_multimodal_skips_unusable_images() -> None:
    store = OutputStore()
    artifact_id = store.put_multimodal(
        {"text": "图", "images": [{"data": "not-bytes"}, None]},
        tool_name="read",
    )
    artifact = store.get(artifact_id)

    assert artifact is not None
    assert artifact.image_count == 0
    assert artifact.metadata["image_count"] == 0


# ── 重置 / 持久化 ────────────────────────────────────────────────────────


def test_reset_clears_artifacts_and_counter() -> None:
    store = OutputStore()
    artifact_id = store.put("x", tool_name="shell")

    store.reset()

    assert store.list_ids() == []
    assert not store.exists(artifact_id)
    assert store.put("y", tool_name="shell") == "shell_1"


def test_persistence_roundtrip(tmp_path: Path) -> None:
    store = OutputStore(root=tmp_path)
    text_id = store.put("hello\nworld", tool_name="python_exec")
    image_id = store.put_multimodal(
        {"text": "截图", "images": [{"data": PNG_BYTES, "mime": "image/png"}]},
        tool_name="read",
    )

    assert (tmp_path / "artifact.json").is_file()

    reopened = OutputStore(root=tmp_path)
    assert reopened.exists(text_id) and reopened.exists(image_id)
    text_artifact = reopened.get(text_id)
    assert text_artifact is not None
    assert text_artifact.get() == "hello\nworld"
    image_artifact = reopened.get(image_id)
    assert image_artifact is not None
    assert image_artifact.images[0]["data"] == PNG_BYTES
    assert image_artifact.images[0]["mime"] == "image/png"
    assert image_artifact.metadata["image_count"] == 1
    assert reopened.summary(image_id) == "[截图 — 1张图片: artifact://read_2]"


def test_persisted_counter_continues_after_reopen(tmp_path: Path) -> None:
    first = OutputStore(root=tmp_path)
    first.put("a", tool_name="shell")
    first.put("b", tool_name="shell")

    reopened = OutputStore(root=tmp_path)
    assert reopened.put("c", tool_name="shell") == "shell_3"


def test_reset_persists_empty_state(tmp_path: Path) -> None:
    store = OutputStore(root=tmp_path)
    store.put("a", tool_name="shell")

    store.reset()

    payload = json.loads((tmp_path / "artifact.json").read_text(encoding="utf-8"))
    assert payload == {"counter": 0, "artifacts": []}
    reopened = OutputStore(root=tmp_path)
    assert reopened.list_ids() == []
    assert reopened.put("b", tool_name="shell") == "shell_1"


def test_persistence_write_is_atomic(tmp_path: Path) -> None:
    store = OutputStore(root=tmp_path)
    store.put("a", tool_name="shell")
    store.put("b", tool_name="shell")

    assert [path.name for path in tmp_path.iterdir()] == ["artifact.json"]
    payload = json.loads((tmp_path / "artifact.json").read_text(encoding="utf-8"))
    assert payload["counter"] == 2
    assert [item["output_id"] for item in payload["artifacts"]] == ["shell_1", "shell_2"]
    assert payload["artifacts"][0]["images"] == []


def test_corrupt_persisted_file_falls_back_to_empty_store(tmp_path: Path) -> None:
    (tmp_path / "artifact.json").write_text("{not json", encoding="utf-8")

    store = OutputStore(root=tmp_path)

    assert store.list_ids() == []
    assert store.put("a", tool_name="shell") == "shell_1"


# ── 工具包装（wrap_tool_output / wrap_tools）─────────────────────────────
# 每个用例用工厂新建工具对象：包装是「就地标记」的，共享同一对象会让后一个
# 用例拿到前一个用例的 store。


def _make_short_tool():
    @tool
    def short_tool(query: str) -> str:
        """短结果工具。"""
        return f"ok:{query}"

    return short_tool


def _make_long_tool():
    @tool
    def long_tool(query: str) -> str:
        """长结果工具。"""
        return "x" * 500

    return long_tool


def _make_multiline_tool():
    @tool
    def multiline_tool() -> str:
        """多行短结果工具。"""
        return "a\nb"

    return multiline_tool


def _make_dict_tool():
    @tool
    def dict_tool() -> dict:
        """结构化结果工具。"""
        return {"answer": "y" * 200}

    return dict_tool


def _make_multimodal_tool():
    @tool
    def multimodal_tool() -> dict:
        """多模态结果工具。"""
        return {
            "kind": "multimodal_tool_result",
            "text": "截图",
            "images": [{"data": PNG_BYTES, "mime": "image/png"}],
        }

    return multimodal_tool


def _make_async_tool():
    @tool
    async def async_tool(query: str) -> str:
        """异步长结果工具。"""
        return f"{query}:" + "y" * 500

    return async_tool


def _make_boom_tool():
    @tool
    def boom_tool() -> str:
        """总是失败的工具。"""
        raise RuntimeError("boom")

    return boom_tool


def test_wrap_short_single_line_result_is_returned_as_is() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_short_tool(), artifacts=store)

    assert wrapped.invoke({"query": "q"}) == "ok:q"
    assert store.list_ids() == []


def test_wrap_long_result_stores_and_summarizes() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_long_tool(), artifacts=store)

    output = wrapped.invoke({"query": "q"})

    assert output == store.summary("long_1")
    assert store.get("long_1").content == "x" * 500


def test_wrap_multiline_short_result_is_stored() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_multiline_tool(), artifacts=store)

    output = wrapped.invoke({})

    ids = store.list_ids()
    assert len(ids) == 1
    assert store.get(ids[0]).content == "a\nb"
    assert output == "a\nb"


def test_wrap_dict_result_is_json_serialized() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_dict_tool(), artifacts=store)

    output = wrapped.invoke({})

    artifact = store.get(store.list_ids()[0])
    assert artifact is not None
    assert output == store.summary(artifact.output_id)
    assert json.loads(artifact.content) == {"answer": "y" * 200}


def test_wrap_multimodal_result_keeps_base64_out_of_text() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_multimodal_tool(), artifacts=store)

    output = wrapped.invoke({})
    payload = json.loads(output)

    assert payload["kind"] == "multimodal_tool_result"
    assert payload["artifact"] == "multimodal_1"
    assert payload["text"] == "截图\n[1 张图片已保存: artifact://multimodal_1]"
    assert "base64" not in output
    assert "data:image" not in output
    assert store.get("multimodal_1").images[0]["data"] == PNG_BYTES


def test_wrap_existing_reference_is_not_stored_twice() -> None:
    @tool
    def reread_tool() -> str:
        """重发已有 artifact。"""
        return json.dumps(
            {
                "kind": "multimodal_tool_result",
                "text": "已存\n[图片: artifact://read_9]",
                "artifact": "read_9",
            },
            ensure_ascii=False,
        )

    store = OutputStore()
    wrapped = wrap_tool_output(reread_tool, artifacts=store)

    output = wrapped.invoke({})

    assert json.loads(output)["artifact"] == "read_9"
    assert store.list_ids() == []


def test_wrap_reports_tool_error_as_artifact() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_boom_tool(), artifacts=store)

    output = wrapped.invoke({})

    assert output == "工具执行出错\n[完整输出: artifact://boom_1]"
    artifact = store.get("boom_1")
    assert artifact is not None
    assert artifact.content == "boom"
    assert artifact.metadata["status"] == "error"


def test_wrap_is_idempotent() -> None:
    store = OutputStore()
    first = wrap_tool_output(_make_long_tool(), artifacts=store)
    second = wrap_tool_output(first, artifacts=store)

    assert second is first
    first.invoke({"query": "q"})
    assert len(store.list_ids()) == 1


def test_wrap_async_tool() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_async_tool(), artifacts=store)

    output = asyncio.run(wrapped.ainvoke({"query": "q"}))

    artifact = store.get("async_1")
    assert artifact is not None
    assert output == store.summary("async_1")
    assert artifact.content.startswith("q:")


def test_wrap_sync_tool_async_invoke() -> None:
    store = OutputStore()
    wrapped = wrap_tool_output(_make_long_tool(), artifacts=store)

    output = asyncio.run(wrapped.ainvoke({"query": "q"}))

    assert output == store.summary("long_1")


def test_wrap_tools_skips_non_base_tool() -> None:
    store = OutputStore()
    sample_tool = _make_short_tool()
    result = wrap_tools([sample_tool, lambda: "bare callable"], artifacts=store)

    assert len(result) == 1
    assert result[0] is sample_tool


def test_tools_module_imports_no_product_or_plugin_packages() -> None:
    """计划 §A2：pluggy 通道已删除；tools.py 只依赖 stdlib + langchain_core。"""
    import liorium.agent.runtime.tools as tools_module

    tree = ast.parse(Path(tools_module.__file__).read_text(encoding="utf-8"))
    allowed_prefixes = (
        "__future__",
        "asyncio",
        "collections",
        "functools",
        "inspect",
        "json",
        "typing",
        "langchain_core",
        "liorium.core",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.startswith(allowed_prefixes), f"tools.py 不允许 import {name}"


def test_artifact_dataclass_defaults() -> None:
    artifact = Artifact(output_id="shell_1")

    assert artifact.content_type == "text"
    assert artifact.images == []
    assert artifact.metadata == {}
    assert artifact.content_base64 is None
    assert artifact.mime_type is None
    assert artifact.size == 0
    assert artifact.lines == 0
    assert artifact.get() == ""
    assert artifact.summary() == "(空输出)"
