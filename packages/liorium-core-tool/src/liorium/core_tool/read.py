"""``read`` —— 一个入口吃下全部可读后端。

计划依据（``docs/plans/2026-11-04-liorium-agent-core.md``）：

- §3.6 **scheme 与挂载点同名**：``<scheme>://a/b`` ≡ ``/<scheme>/a/b``；裸路径是
  真实文件系统（相对 ``ToolRuntime.workspace_root``）；``file://`` 是真实文件系统
  绝对路径；只有 ``<scheme>://`` 走 VFS。``agent://`` 落在 VFS 根 ``/``。
- §4.5 / §D8 **输出形态由节点内容类型决定，与 scheme 无关**：
  ``Text`` → 文本（可选行选择器 / ``:raw`` / 结构摘要）；``Image`` → 多模态 JSON
  （图片交 agent 侧 OutputStore，文本里只留 ``artifacts://<id>`` 引用）；``Binary``
  → 文本说明。**绝不把 base64 拼进给人看的文本**。
- ``docs/design/read-tool.md`` 冻结逐字模板：分发顺序、目录列举、选择器、行号、
  结构摘要、300 行截断、元数据、缺失文案。

兼容：``artifact://<id>``（单数，冻结的工具输出文本）与 ``artifacts://<id>``
（挂载点）都落在 VFS 的 ``/artifacts``。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence

from langchain_core.tools import BaseTool, tool

from liorium.core.log import get_logger
from liorium.core.types import Binary, Image, Text, VfsContent
from liorium.core.uri import SCHEME_FILE, ParsedUri, normalize_vfs_path, parse
from liorium.vfs import VirtualFileSystem, VfsNode

from .format import (
    SOURCE_EXTENSIONS,
    apply_selector,
    format_file_metadata,
    format_memory_metadata,
    format_structure_summary,
    truncate_300,
)
from .sniff import sniff_text

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免无运行时依赖
    from .runtime import ToolRuntime

log = get_logger("core_tool.read")

# ---------------------------------------------------------------------------
# scheme 常量与挂载点
# ---------------------------------------------------------------------------

SCHEME_ARTIFACT = "artifact"
SCHEME_ARTIFACTS = "artifacts"
SCHEME_MEMORY = "memory"
SCHEME_MEMORIES = "memories"
SCHEME_SKILL = "skill"
SCHEME_SKILLS = "skills"
SCHEME_AGENT = "agent"
SCHEME_IMG_SOURCE = "img_source"
SCHEME_CODE = "code"
SCHEME_SOURCE_CODE = "sourcecode"

ARTIFACT_ROOT = "/artifacts"
"""工具输出挂载点：``artifacts://`` ⇔ ``/artifacts``。"""

MEMORY_ROOT = "/memories"
"""记忆挂载点：``memories://``（兼容单数 ``memory://``）⇔ ``/memories``。"""

SKILL_ROOT = "/skills"
"""技能挂载点：``skills://``（兼容单数 ``skill://``）⇔ ``/skills``。"""

CODE_ROOT = "/code"
"""只读代码树挂载点：``code://``（兼容 ``sourceCode://``）⇔ ``/code``。"""

AGENT_BASE = ""
"""``agent://`` 落在 VFS 根（计划 §3.6 挂载表：``/`` ⇔ ``agent://``）。"""

_MOUNT_BASE: dict[str, str] = {
    SCHEME_ARTIFACT: ARTIFACT_ROOT,
    SCHEME_ARTIFACTS: ARTIFACT_ROOT,
    SCHEME_MEMORY: MEMORY_ROOT,
    SCHEME_MEMORIES: MEMORY_ROOT,
    SCHEME_SKILL: SKILL_ROOT,
    SCHEME_SKILLS: SKILL_ROOT,
    SCHEME_CODE: CODE_ROOT,
    SCHEME_SOURCE_CODE: CODE_ROOT,
    SCHEME_AGENT: AGENT_BASE,
}
"""``scheme -> 挂载点基路径``；不在表里的 scheme 走 ``ParsedUri.vfs_path()`` 的通用规则。"""

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
"""图片文件后缀（``.svg`` 也算图片，与既有实现一致）。"""

IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

MAX_METADATA_LINE_SCAN = 2 * 1024 * 1024
"""超过该大小不统计行数（避免列举时读大文件，§6.1）。"""

MAX_ARTIFACT_LIST = 20
"""``artifact://`` 列举只给最近 20 个（§5.4）。"""

MULTIMODAL_KIND = "multimodal_tool_result"
"""多模态结果 JSON 的 ``kind``（冻结的桥接协议，mm_bridge 只认它）。"""

IMG_SOURCE_MENU = (
    "img_source:// 可用资源:\n"
    "  img_source://screenshot\n"
    "  img_source://camera_0\n"
    '使用 read("img_source://screenshot?grid=true&scale=0.5") 截图，'
    '使用 read("img_source://camera_0") 访问摄像头。'
)
"""``img_source://`` 的固定三行菜单（逐字，read-tool.md §5.9）。"""

_SCHEME_SPELLING_RE = re.compile(r"^(?P<scheme>[a-zA-Z][\w+.-]*)://")

_HandlerMode = Literal["file", "resource"]
"""``file`` = 文件语义（行选择器 / ``:raw`` / 结构摘要 / 截断）；
``resource`` = 资源语义（只做行选择器与编号，不摘要不截断）。"""


# ---------------------------------------------------------------------------
# 工具装配
# ---------------------------------------------------------------------------


def build_read_tool(runtime: "ToolRuntime") -> BaseTool:
    """按运行时依赖装配 ``read`` 工具。

    只依赖运行时上两个成员（详见 ``docs/design/INTERFACES.md`` 与 ``runtime.py``）：

    - ``runtime.vfs``：``VirtualFileSystem``，所有 ``<scheme>://`` 的读取面；
    - ``runtime.resolve_path(uri)``：裸路径 / ``file://`` 的真实文件解析
      （内部即 ``workspace_root``，与 write/edit/search/find 共用同一套语义）。

    返回的 ``BaseTool`` 名字恒为 ``read``（artifact id 前缀 ``read_N``、prompt 与
    测试都依赖它）。
    """

    @tool
    async def read(
        uri: str,
        *,
        force_plain_text: bool = False,
        show_line_number: bool = False,
        with_metadata: bool = True,
    ) -> str:
        r"""读取文件/目录/记忆/技能/代码树/工具输出/系统资源的统一入口；uri 决定读哪个后端。

        选择器（追加在 uri 后面）：
        - `:50-100` / `:42` — 按绝对行号读。源码文件默认只给结构化摘要（def/class/import 行），
          摘要里看到的声明，用行号范围去读它的本体。
        - `:-3` — 倒数第 3 行；`:-20:-10` — 倒数第 20 行到倒数第 10 行；`:50+20` — 从第 50 行起 20 行。
        - `:raw` — 关闭结构摘要返回原文（长于 300 行仍会截断，剩余用行号读）。

        目录：目录一律自动列举（结尾 `/` 可省）。每一条附 `[大小, 行数, 日期]`
        （VFS 节点则是 `[用途描述]`，记忆节点是 `[日期, 行数, #标签]`）；不需要这行
        噪音时用 with_metadata=False。

        图片：图片会作为图像块附进对话（只有视觉模型看得见）；非视觉模型必须
        force_plain_text=True。工具结果里只留 artifacts:// 引用，需要时再 read 它。

        二进制：只返回类型/大小/引用说明，不内联内容。

        后端（与挂载点同名，任意新挂载自动获得一个 scheme）：
        - 裸路径 — 工作区里的真实文件（目录自动列出）；`file:///C:/x.txt` — 真实绝对路径
        - agent:// — Agent 自身的资源与投影节点（落在 VFS 根）
        - skills:// — 技能文件，必须显式写 `SKILL.md`（`skills://` 是列技能）
        - memories:// — 记忆文档；`read("memories://")` 是列目录
        - code:// — 只读代码树（目录自动列出）
        - subagents:// / realms:// — 子代理与 realm 的投影
        - artifacts://<id> — 某次工具输出的完整内容（输出被截断时用；`artifact://<id>` 同样接受）
        - img_source:// — 图片源菜单（截屏/摄像头由产品侧的 screen:// / camera:// 提供）

        Args:
            uri (str): 路径或 URI，可带 `:N` / `:N-M` / `:raw` 选择器。
            force_plain_text (bool): 图片只返回元数据，不附加图像块。
            show_line_number (bool): 每行前置绝对行号（配合 edit 用；总是文件里的绝对行号）。
            with_metadata (bool): 列举是否带大小/行数/日期，默认 True。
        """
        parsed = parse(uri)
        log.info(
            "read INPUT uri=%s force_plain_text=%s show_line_number=%s with_metadata=%s",
            uri,
            force_plain_text,
            show_line_number,
            with_metadata,
        )
        log.debug(
            "read parsed: scheme=%s path=%r selector=%r force_plain_text=%r",
            parsed.scheme,
            parsed.path,
            parsed.selector,
            force_plain_text,
        )
        result = await _dispatch(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
        log.info("read OUTPUT len=%d", len(result))
        return result

    return read


# ---------------------------------------------------------------------------
# 分发（严格顺序，命中即返回）
# ---------------------------------------------------------------------------


async def _dispatch(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool,
    show_line_number: bool,
    with_metadata: bool,
) -> str:
    """按 scheme 分发：artifact → memory → skill → agent → img_source → code → file。"""
    scheme = parsed.scheme
    if scheme in (SCHEME_ARTIFACT, SCHEME_ARTIFACTS):
        return await _read_artifact(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    if scheme in (SCHEME_MEMORY, SCHEME_MEMORIES):
        return await _read_memory(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    if scheme in (SCHEME_SKILL, SCHEME_SKILLS):
        return await _read_skill(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    if scheme == SCHEME_AGENT:
        return await _read_agent(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    if scheme == SCHEME_IMG_SOURCE:
        return _read_img_source(parsed)
    if scheme in (SCHEME_CODE, SCHEME_SOURCE_CODE):
        return await _read_code(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    if scheme == SCHEME_FILE:
        return _read_file(
            runtime,
            parsed,
            force_plain_text=force_plain_text,
            show_line_number=show_line_number,
            with_metadata=with_metadata,
        )
    return await _read_vfs_resource(
        runtime,
        parsed,
        force_plain_text=force_plain_text,
        show_line_number=show_line_number,
        with_metadata=with_metadata,
    )


# ---------------------------------------------------------------------------
# artifact:// / artifacts://
# ---------------------------------------------------------------------------


async def _read_artifact(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """读工具输出：``artifact://<id>``（单数，冻结文本）与 ``artifacts://<id>`` 等价。"""
    vfs = runtime.vfs
    output_id = str(parsed.path or "").strip("/")
    vpath = _join(ARTIFACT_ROOT, output_id)
    node = await vfs.stat(vpath)

    if node is None:
        if not output_id:
            return "(没有可用的 artifact)"
        return f"[找不到 artifact: {output_id}]"

    if node.is_directory:
        children = await vfs.list(vpath)
        if not children:
            return "(没有可用的 artifact)"
        names = [child.name for child in children][-MAX_ARTIFACT_LIST:]
        return "可用的 artifact:\n" + "\n".join(f"  artifact://{name}" for name in names)

    content = await vfs.read(vpath)
    if content is None:
        return f"[找不到 artifact: {output_id}]"

    if isinstance(content, Image):
        description = (node.description or "").strip() or f"图片 artifact: {output_id}"
        if force_plain_text:
            return description
        # 引用形态：图片已在 OutputStore 里，包装层不会重复存一份（vfs-output-mw.md §4.6）
        return _json(
            {
                "kind": MULTIMODAL_KIND,
                "text": f"{description}\n[图片: artifact://{output_id}]",
                "artifact": output_id,
            }
        )
    if isinstance(content, Binary):
        return _binary_result(content, name=node.name, reference=f"artifacts://{output_id}")

    text = content.text
    if parsed.selector_lines or show_line_number:
        return apply_selector(text, parsed.selector, show_line_number=show_line_number)
    return text


# ---------------------------------------------------------------------------
# memory:// / memories://
# ---------------------------------------------------------------------------


async def _read_memory(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """读记忆库：目录列举（一层树）/ 文档 / 图片附件。"""
    vfs = runtime.vfs
    if await vfs.stat(MEMORY_ROOT) is None:
        return "(记忆模块不可用)"

    path = str(parsed.path or "").strip("/")
    vpath = _join(MEMORY_ROOT, path)
    node = await vfs.stat(vpath)

    if not path or (node is not None and node.is_directory):
        return await _memory_tree(vfs, parsed, vpath, with_metadata=with_metadata)

    if node is None:
        return f"[记忆文档不存在: {path}]"

    content = await vfs.read(vpath)
    if content is None:
        return f"[记忆文档不存在: {path}]"

    if isinstance(content, Image):
        description = (node.description or "").strip() or f"记忆图片: {path}"
        if force_plain_text:
            return f"[图片附件: {path}]\n描述: {description}\n类型: {content.mime}"
        return _json(
            {
                "kind": MULTIMODAL_KIND,
                "text": description,
                "images": [_image_block(content)],
            }
        )
    if isinstance(content, Binary):
        return _binary_result(content, name=node.name, reference=f"artifacts://memories/{path}")

    text = content.text
    if parsed.selector_lines or show_line_number:
        return apply_selector(text, parsed.selector, show_line_number=show_line_number)
    return text


async def _memory_tree(
    vfs: VirtualFileSystem,
    parsed: ParsedUri,
    vpath: str,
    *,
    with_metadata: bool,
) -> str:
    """记忆目录列举：根行 + 一层子项（目录优先），只有文件带元数据（§5.5）。"""
    root = await vfs.stat(vpath)
    if root is None or not root.is_directory:
        return f"[记忆文档不存在: {str(parsed.path or '').strip('/')}]"

    children = await vfs.list(vpath)
    name = str(parsed.path or "").strip("/").rsplit("/", 1)[-1] or "/"
    lines = [f"{name}/"]
    for child in sorted(children, key=lambda n: (0 if n.is_directory else 1, n.name.lower())):
        if child.is_directory:
            lines.append(f"  {child.name}/")
            continue
        lines.append(f"  {child.name}")
        if with_metadata:
            meta = format_memory_metadata(
                updated_at=str(child.meta.get("updated_at") or ""),
                line_count=child.meta.get("line_count"),
                tags=child.meta.get("tags") or (),
            )
            if meta:
                lines.append(f"    [{meta}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# skill:// / skills://
# ---------------------------------------------------------------------------


async def _read_skill(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """读技能：空路径列技能清单；目录列举；文件走文件语义（摘要 / ``:raw`` / 行范围）。"""
    vfs = runtime.vfs
    label = _scheme_spelling(parsed)
    raw_path = str(parsed.path or "").strip("/")

    if not raw_path:
        if await vfs.stat(SKILL_ROOT) is None:
            return "(没有可用的 skill)"
        skills = [
            node
            for node in await vfs.list(SKILL_ROOT)
            if node.is_directory and not node.name.startswith(".")
        ]
        if not skills:
            return "(没有可用的 skill)"
        lines = [f"{label}:// 可用 skill:"]
        for node in sorted(skills, key=lambda n: n.name):
            lines.append(f"  {label}://{node.name}/")
            if with_metadata:
                description = (node.description or "").strip()
                if description:
                    lines.append(f"    [{description}]")
        return "\n".join(lines)

    parts = [part for part in raw_path.split("/") if part]
    skill_name = parts[0] if parts else ""
    if not skill_name:
        return "[skill 名称不能为空]"
    relative = parts[1:]
    if any(part in (".", "..") for part in relative):
        return "[不允许越界访问 skill 目录]"

    vpath = _join(SKILL_ROOT, raw_path)
    node = await vfs.stat(vpath)
    if node is None:
        if not relative:
            return f"[skill 不存在: {skill_name}]"
        return f"[skill 文件不存在: {skill_name}/{'/'.join(relative)}]"

    if node.is_directory:
        dir_label = f"{skill_name}/{'/'.join(relative)}" if relative else skill_name
        lines = [f"{label}://{dir_label}/ 内容:"]
        lines += _entry_lines(
            await vfs.list(vpath),
            prefix=f"{label}://{dir_label}/",
            with_metadata=with_metadata,
            dirs_first=True,
            skip_hidden=True,
        )
        return "\n".join(lines)

    content = await vfs.read(vpath)
    if content is None:
        return f"[skill 文件不存在: {skill_name}/{'/'.join(relative)}]"
    return _content_result(
        content,
        name=node.name,
        uri_label=f"{label}://{raw_path}",
        binary_reference=f"artifacts://skills/{raw_path}",
        force_plain_text=force_plain_text,
        parsed=parsed,
        show_line_number=show_line_number,
        mode="file",
    )


# ---------------------------------------------------------------------------
# sourceCode:// / code://
# ---------------------------------------------------------------------------


async def _read_code(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """只读代码树：根是**单一有序列表**（不目录优先），子目录才目录优先（§5.8）。"""
    vfs = runtime.vfs
    label = _scheme_spelling(parsed)
    raw_path = str(parsed.path or "").strip("/")

    if not raw_path:
        lines = [f"{label}:// 仓库根 内容:"]
        if await vfs.stat(CODE_ROOT) is not None:
            lines += _entry_lines(
                await vfs.list(CODE_ROOT),
                prefix=f"{label}://",
                with_metadata=with_metadata,
                dirs_first=False,
                skip_hidden=True,
            )
        return "\n".join(lines)

    parts = [part for part in raw_path.split("/") if part]
    if any(part in (".", "..") for part in parts):
        return "[不允许越界访问源码目录]"

    vpath = _join(CODE_ROOT, raw_path)
    node = await vfs.stat(vpath)
    if node is None:
        return f"[源码文件不存在: {raw_path}]"

    if node.is_directory:
        lines = [f"{label}://{raw_path}/ 内容:"]
        lines += _entry_lines(
            await vfs.list(vpath),
            prefix=f"{label}://{raw_path}/",
            with_metadata=with_metadata,
            dirs_first=True,
            skip_hidden=True,
        )
        return "\n".join(lines)

    content = await vfs.read(vpath)
    if content is None:
        return f"[源码文件不存在: {raw_path}]"
    return _content_result(
        content,
        name=node.name,
        uri_label=f"{label}://{raw_path}",
        binary_reference=f"artifacts://code/{raw_path}",
        force_plain_text=force_plain_text,
        parsed=parsed,
        show_line_number=show_line_number,
        mode="file",
    )


# ---------------------------------------------------------------------------
# agent:// 与其它挂载点（通用 VFS 资源语义）
# ---------------------------------------------------------------------------


async def _read_agent(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """``agent://`` = Agent 自身的 VFS 根；空内容节点与不存在同文案（冻结）。"""
    return await _read_vfs_tree(
        runtime,
        parsed,
        base=AGENT_BASE,
        label=SCHEME_AGENT,
        root_title="agent:// 可用资源:",
        dir_title="agent://{path}/ 内容:",
        missing="[未知 agent 资源: {path}]",
        mode="resource",
        empty_is_missing=True,
        force_plain_text=force_plain_text,
        show_line_number=show_line_number,
        with_metadata=with_metadata,
    )


async def _read_vfs_resource(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """未知 scheme = 一个尚未认识的挂载点：按通用 VFS 语义读，缺失给 ``[文件不存在: 原串]``。"""
    label = _scheme_spelling(parsed)
    return await _read_vfs_tree(
        runtime,
        parsed,
        base="/" + parsed.scheme,
        label=label,
        root_title=f"{label}:// 可用资源:",
        dir_title=f"{label}://{{path}}/ 内容:",
        missing="[文件不存在: {raw}]",
        mode="resource",
        empty_is_missing=False,
        force_plain_text=force_plain_text,
        show_line_number=show_line_number,
        with_metadata=with_metadata,
    )


async def _read_vfs_tree(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    base: str,
    label: str,
    root_title: str,
    dir_title: str,
    missing: str,
    mode: _HandlerMode,
    empty_is_missing: bool,
    force_plain_text: bool,
    show_line_number: bool,
    with_metadata: bool,
) -> str:
    """通用 VFS 后端：根列举 / 目录列举 / 文件读取（内容类型决定输出形态）。"""
    vfs = runtime.vfs
    path = str(parsed.path or "").strip("/")
    vpath = _join(base, path)
    node = await vfs.stat(vpath)

    if node is None:
        return missing.format(path=path, raw=parsed.raw)

    if node.is_directory:
        children = await vfs.list(vpath)
        if not path:
            lines = [root_title]
            lines += _entry_lines(
                children,
                prefix=f"{label}://",
                with_metadata=with_metadata,
                dirs_first=False,
            )
        else:
            lines = [dir_title.format(path=path, raw=parsed.raw)]
            lines += _entry_lines(
                children,
                prefix=f"{label}://{path}/",
                with_metadata=with_metadata,
                dirs_first=True,
            )
        return "\n".join(lines)

    content = await vfs.read(vpath)
    if content is None or (empty_is_missing and isinstance(content, Text) and not content.text):
        return missing.format(path=path, raw=parsed.raw)
    return _content_result(
        content,
        name=node.name,
        uri_label=f"{label}://{path}",
        binary_reference=f"artifacts://{vpath.lstrip('/')}",
        force_plain_text=force_plain_text,
        parsed=parsed,
        show_line_number=show_line_number,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# img_source://
# ---------------------------------------------------------------------------


def _read_img_source(parsed: ParsedUri) -> str:
    """``img_source://``：core 只保留菜单与"未知资源"文案（抓屏属产品侧）。"""
    path = str(parsed.path or "").strip("/")
    if not path or parsed.is_dir:
        return IMG_SOURCE_MENU
    return f"[未知 img_source 资源: {path}]"


# ---------------------------------------------------------------------------
# 真实文件系统（裸路径 / file://）
# ---------------------------------------------------------------------------


def _read_file(
    runtime: "ToolRuntime",
    parsed: ParsedUri,
    *,
    force_plain_text: bool = False,
    show_line_number: bool = False,
    with_metadata: bool = True,
) -> str:
    """裸路径 = ``workspace_root`` 下的真实文件；``file://`` = 绝对路径。"""
    # 与 write/edit/search/find 同一条解析路径（相对 workspace_root、file:// 去盘符前斜杠）
    target = runtime.resolve_path(parsed.raw)
    fs_path = Path(target.path)
    from_file_url = (parsed.raw or "").lower().startswith("file://")
    path_label = target.display if from_file_url else (parsed.path or ".")

    if parsed.is_dir or fs_path.is_dir():
        return _list_directory(fs_path, with_metadata=with_metadata)

    if not fs_path.exists():
        return f"[文件不存在: {path_label}]"

    if fs_path.suffix.lower() in IMAGE_EXTENSIONS:
        return _read_image_file(fs_path, force_plain_text=force_plain_text)

    try:
        raw = fs_path.read_bytes()
    except OSError as exc:
        return f"读取文件出错: {exc}"

    # Liorium 修正（read-tool.md §11.2 + 2026-11-08 编码嗅探）：真实文件也做探测，
    # 但"是不是文本"与"是哪种文本"由 sniff_text 一次判定 —— 本地编码（UTF-16 / GBK）
    # 的纯文本不再被误判成二进制，`类型:` 也不会再和内容判定自相矛盾。
    sniff = sniff_text(raw)
    if sniff.text is None:
        guessed = mimetypes.guess_type(fs_path.name)[0]
        return _binary_result(
            Binary(data=raw, mime="application/octet-stream"),
            name=fs_path.name,
            reference=f"artifacts://{_workspace_label(runtime, fs_path)}",
            guessed=guessed,
        )

    # 等价文本模式的通用换行（`\r\n` / 裸 `\r` → `\n`），行号与 edit 才对得上
    content = sniff.text.replace("\r\n", "\n").replace("\r", "\n")
    rendered = _render_file_text(
        content,
        parsed=parsed,
        path_label=str(fs_path),
        suffix=fs_path.suffix,
        show_line_number=show_line_number,
    )
    if sniff.note:
        # 只在"不是普通 UTF-8"时披露编码，既有输出逐字不变
        return f"[编码: {sniff.note}]\n{rendered}"
    return rendered


def _workspace_label(runtime: "ToolRuntime", fs_path: Path) -> str:
    """文件在 workspace 内用相对路径，否则退化成文件名（只用于 ``artifacts://`` 占位）。"""
    try:
        return fs_path.resolve().relative_to(Path(runtime.workspace_root)).as_posix()
    except ValueError:
        return fs_path.name


def _list_directory(dir_path: Path, *, with_metadata: bool) -> str:
    """FS 目录列举：目录先、文件后（各自小写升序）；元数据 4 空格 + 方括号（§6.2）。"""
    try:
        entries = sorted(dir_path.iterdir(), key=lambda entry: (entry.is_file(), entry.name.lower()))
    except OSError as exc:
        return f"列出目录出错: {exc}"

    lines: list[str] = []
    for entry in entries:
        is_dir = entry.is_dir()
        lines.append(f"  {entry.name}{'/' if is_dir else ''}")
        if with_metadata and not is_dir:
            meta = _file_metadata_line(entry)
            if meta:
                lines.append(f"    [{meta}]")
    return "\n".join(lines)


def _file_metadata_line(path: Path) -> str:
    """FS 条目元数据 ``[大小, N lines, MM/DD]``；``stat()`` 失败返回 ``""``。"""
    try:
        st = path.stat()
    except OSError:
        return ""
    return format_file_metadata(
        size=st.st_size,
        mtime=st.st_mtime,
        line_count=_count_source_lines(path),
    )


def _count_source_lines(path: Path) -> int | None:
    """白名单源码文件（≤2MiB 且 UTF-8 可解码）返回行数，否则 ``None``（§6.1）。"""
    if path.suffix.lower() not in SOURCE_EXTENSIONS:
        return None
    try:
        if path.stat().st_size > MAX_METADATA_LINE_SCAN:
            return None
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return None


def _read_image_file(path: Path, *, force_plain_text: bool) -> str:
    """真实图片文件：多模态 JSON 或纯文本元数据（``类型`` 是原始后缀，§5.1）。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"读取文件出错: {exc}"
    if force_plain_text:
        return f"[图片文件: {path.name}]\n大小: {len(raw)} bytes\n类型: {path.suffix}"
    mime = IMAGE_MIME.get(path.suffix.lower(), "image/png")
    return _json(
        {
            "kind": MULTIMODAL_KIND,
            "text": f"图片文件: {path.name} ({len(raw)} bytes)",
            "images": [{"url": _data_url(raw, mime)}],
        }
    )


# ---------------------------------------------------------------------------
# 内容类型分派（Text / Image / Binary）
# ---------------------------------------------------------------------------


def _content_result(
    content: VfsContent,
    *,
    name: str,
    uri_label: str,
    binary_reference: str,
    force_plain_text: bool,
    parsed: ParsedUri,
    show_line_number: bool,
    mode: _HandlerMode,
) -> str:
    """按**内容类型**产出结果（计划 §4.5：与 scheme 无关）。"""
    if isinstance(content, Image):
        return _image_result(content, name=name, force_plain_text=force_plain_text)
    if isinstance(content, Binary):
        return _binary_result(content, name=name, reference=binary_reference)
    text = content.text
    if mode == "resource":
        return apply_selector(text, parsed.selector, show_line_number=show_line_number)
    return _render_file_text(
        text,
        parsed=parsed,
        path_label=uri_label,
        suffix=Path(name).suffix,
        show_line_number=show_line_number,
    )


def _render_file_text(
    content: str,
    *,
    parsed: ParsedUri,
    path_label: str,
    suffix: str,
    show_line_number: bool,
) -> str:
    """文件语义的文本渲染（选择器 → ``:raw`` → 结构摘要 → 截断，顺序即语义）。"""
    if parsed.selector_lines:
        return apply_selector(content, parsed.selector, show_line_number=show_line_number)
    if parsed.is_raw:
        # 先编号再截断：截断提示行不带行号（§4.4）
        return truncate_300(
            apply_selector(content, None, show_line_number=show_line_number)
        )[0]
    if suffix.lower() in SOURCE_EXTENSIONS:
        return format_structure_summary(content, path_label)
    return truncate_300(content)[0]


def _image_result(content: Image, *, name: str, force_plain_text: bool) -> str:
    """``Image`` 节点 → 多模态 JSON（绝不把 base64 放进给人看的文本）。"""
    size = len(content.data)
    if force_plain_text:
        return f"[图片: {name}]\n大小: {size} bytes\n类型: {content.mime}"
    return _json(
        {
            "kind": MULTIMODAL_KIND,
            "text": f"图片: {name} ({size} bytes)",
            "images": [_image_block(content)],
        }
    )


def _binary_result(
    content: Binary, *, name: str, reference: str, guessed: str | None = None
) -> str:
    """``Binary`` 节点 → 文本说明（类型 + 大小 + ``artifacts://`` 引用占位）。

    ``reference`` 只是**占位**：真正的 artifact id 由 agent 侧包装层落盘后生成
    （``read`` 不碰 OutputStore，计划 §4.5）。

    ``guessed`` 是扩展名猜到的类型。2026-11-08 之前 ``类型:`` 填的就是它，于是
    纯文本（UTF-16 / GBK）被内容判定为二进制时会出现 ``类型: text/plain`` 与
    ``[二进制: xxx.txt]`` 并存的矛盾。现在 ``类型:`` 恒为内容类型
    （``application/octet-stream``），扩展名猜测降级成一句注脚。
    """
    lines = [f"[二进制: {name}]", f"类型: {content.mime}"]
    if guessed and guessed != content.mime:
        lines.append(f"扩展名: 猜到的类型是 {guessed}，但内容不是文本")
    lines.append(f"大小: {len(content.data)} bytes")
    lines.append(f"引用: {reference}")
    return "\n".join(lines)


def _image_block(content: Image) -> dict[str, str]:
    """多模态 payload 的单个图片块（生产者形状见 vfs-output-mw.md §4.6）。"""
    return {"url": _data_url(content.data, content.mime)}


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _json(payload: dict) -> str:
    """多模态结果 JSON：``ensure_ascii=False``（模型看见中文而不是转义）。"""
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 路径 / 列举小工具
# ---------------------------------------------------------------------------


def _scheme_spelling(parsed: ParsedUri) -> str:
    """回显用户写的 scheme 拼写（``sourceCode://`` 不该被压成 ``sourcecode://``）。"""
    match = _SCHEME_SPELLING_RE.match(parsed.raw or "")
    return match.group("scheme") if match else parsed.scheme


def _join(base: str, path: str) -> str:
    """``(挂载点基路径, scheme 内相对路径)`` → VFS 绝对路径（含 ``..`` 越界检查）。"""
    clean = str(path or "").strip("/")
    if not clean:
        return normalize_vfs_path(base or "/")
    return normalize_vfs_path(f"{base}/{clean}" if base else f"/{clean}")


def _entry_lines(
    nodes: Sequence[VfsNode],
    *,
    prefix: str,
    with_metadata: bool,
    dirs_first: bool,
    skip_hidden: bool = False,
) -> list[str]:
    """渲染 VFS 列举条目：``  {prefix}{name}{/}`` + 可选 ``    [描述]``。"""
    ordered = list(nodes)
    if dirs_first:
        ordered.sort(key=lambda node: (0 if node.is_directory else 1, node.name.lower()))
    if skip_hidden:
        ordered = [node for node in ordered if not node.name.startswith(".")]

    lines: list[str] = []
    for node in ordered:
        lines.append(f"  {prefix}{node.name}{'/' if node.is_directory else ''}")
        if with_metadata:
            description = (node.description or "").strip()
            if description:
                lines.append(f"    [{description}]")
    return lines


__all__ = [
    "ARTIFACT_ROOT",
    "CODE_ROOT",
    "AGENT_BASE",
    "IMAGE_EXTENSIONS",
    "IMAGE_MIME",
    "IMG_SOURCE_MENU",
    "MAX_ARTIFACT_LIST",
    "MAX_METADATA_LINE_SCAN",
    "MEMORY_ROOT",
    "MULTIMODAL_KIND",
    "SKILL_ROOT",
    "build_read_tool",
]
