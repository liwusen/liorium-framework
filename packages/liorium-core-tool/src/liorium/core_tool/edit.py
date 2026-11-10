"""``edit`` 工具：把文件里的一段文本精确替换成新内容。

内核是 ``replace_exact``（``docs/design/vfs-output-mw.md`` §2.3）：纯字面量、
大小写敏感、**全文件恰好 1 处**才写回；0 处 / 多处都返回可操作的错误文本、
文件一个字节都不动（§2.2 逐字文案）。

Liorium 差异：

- VFS 节点用 ``vfs.edit`` 语义（有 ``edit_handler`` 就交给它，否则替换第一处）；
  真实文件走「读 → 唯一替换 → 写回」，并按原文行尾风格写回（CRLF 文件仍是 CRLF）。
- 不再有 ``memory://`` 分支；不做「只允许项目目录内」的边界判断（v1 无权限系统）。
"""

from __future__ import annotations

from langchain_core.tools import tool

from liorium.core.errors import VfsError, VfsReadOnlyError

from .runtime import ToolRuntime


def replace_exact(content: str, old: str, new: str) -> tuple[str | None, int]:
    """精确替换内核（§2.3）：恰好 1 处才替换，返回 ``(新内容, 匹配数)``。

    0 处 / 多处时新内容为 ``None``、匹配数为实际出现次数。
    """
    count = content.count(old)
    if count != 1:
        return None, count
    return content.replace(old, new, 1), 1


def _empty_old_message() -> str:
    return (
        "edit: old_str 不能为空（空串会匹配整个文件）。\n"
        "处理: 新建文件用 write(path, content)；在文件末尾追加时，old_str 应包含"
        "文件末尾的锚点行（先用 read 确认最后一行）。"
    )


def _zero_match_message(path: str) -> str:
    return (
        f"edit: old_str 在 {path} 中匹配 0 处，文件未被修改。\n"
        "处理: 1) 用 read(\""
        + path
        + "\") 重新读取，从输出中逐字复制 old_str；"
        "2) 检查缩进、行尾空格、全角/半角字符是否一致；"
        "3) old_str 是普通字符串不是正则，不要包含 \\n 转义符——多行直接写真实换行。"
    )


def _multi_match_message(path: str, count: int) -> str:
    return (
        f"edit: old_str 在 {path} 中匹配 {count} 处（要求恰好 1 处），文件未被修改。\n"
        f"处理: 在 old_str 前后多包含几行上下文使其唯一"
        f"（可用 read(\"{path}\") 查看各处差异）；"
        f"若确实要替换所有 {count} 处相同片段，"
        f"请改为逐处编辑（每处 old_str 带不同上下文）。"
    )


def make_edit_tool(runtime: ToolRuntime):
    """构造绑定 ``runtime`` 的 ``edit`` 工具。"""

    @tool
    async def edit(path: str, old_str: str, new_str: str) -> str:
        """把文件里的一段原文精确替换成新内容（要求恰好匹配 1 处）。

        规则：
        - 逐字符匹配：缩进、行尾空格、全角/半角、引号都必须一致。
        - 从 read 输出复制的内容可能带 `行号:` 前缀，先去掉再当 old_str。
        - old_str 是普通字符串不是正则；多行就直接写真实换行，不要写 `\\n` 转义。
        - 0 处或多处匹配都会失败返回、文件不动：多处时在 old_str 前后多带几行上下文。
        - 追加内容时 old_str 要包含文件末尾的锚点行；新建文件用 write。
        - 节点编辑可能返回回执，形如 `↳ 已改为 gamma`，要当成工具结果读。

        Args:
            path (str): 文件路径（裸路径 / `file://` / `<scheme>://`）。
            old_str (str): 要被替换的原文；不能为空，必须全文件唯一。
            new_str (str): 替换后的内容；空串表示删除 old_str。
        """
        raw = str(path or "").strip()
        old = str(old_str if old_str is not None else "")
        new = str(new_str if new_str is not None else "")

        # ── 参数预检先于任何 IO（§2.2 冻结文案） ──
        if not raw:
            return "错误: path 不能为空"
        if old == "":
            return _empty_old_message()
        if old == new:
            return "edit: old_str 与 new_str 完全相同，本次编辑无任何变更，未写入。"

        parsed = runtime.resolve_path(raw)
        if parsed.uri.selector:
            return (
                f"edit: {raw} 带行选择器 {parsed.uri.selector}；"
                "edit 只接受文件路径或 `<scheme>://` URI"
            )
        resolved = parsed
        label = resolved.display

        if resolved.is_vfs:
            node = await runtime.vfs_stat(resolved.path)
            if node is None:
                return f"文档不存在: {label}"
            if node.is_directory:
                return f"无法编辑 {label}: 是目录，不是文件"
            try:
                original = await runtime.vfs_read_text(resolved.path)
            except VfsError as exc:
                return f"无法读取 {label}: {exc}"
            if original is None:
                return f"无法编辑 {label}: 不是文本节点"

            result, count = replace_exact(original, old, new)
            if result is None:
                return (
                    _zero_match_message(label)
                    if count == 0
                    else _multi_match_message(label, count)
                )
            try:
                ack = await runtime.vfs_edit_text(resolved.path, old, new)
            except VfsReadOnlyError as exc:
                raise VfsReadOnlyError(
                    f"编辑被拒绝: {label} 是只读挂载或只读节点，未做任何修改"
                ) from exc
            except VfsError as exc:
                return f"无法写入 {label}: {exc}"
            except Exception as exc:
                # edit_handler 自己抛的错：保留可读原因给模型
                return f"无法写入 {label}: {exc}"
            message = f"已编辑 {label} (1 处替换)"
            if ack:
                message += f"\n↳ {ack}"
            return message

        if await runtime.is_dir(resolved):
            return f"无法编辑 {label}: 是目录，不是文件"

        try:
            original, newline = await runtime.read_text_with_newline(resolved)
        except FileNotFoundError:
            return (
                f"文件不存在: {label}\n"
                f"建议: 先用 read(\"{raw}\") 确认路径，或 write(\"{raw}\", content) 创建文件。"
            )
        except IsADirectoryError:
            return f"无法编辑 {label}: 是目录，不是文件"
        except OSError as exc:
            return f"无法读取文件 {label}: {exc}"
        except UnicodeDecodeError as exc:
            return f"无法读取文件 {label}: {exc}"

        result, count = replace_exact(original, old, new)
        if result is None:
            return (
                _zero_match_message(label)
                if count == 0
                else _multi_match_message(label, count)
            )

        try:
            # 保留原文行尾风格：CRLF 文件读回来是 `\n`，写回时再转回 CRLF（§2.5）
            await runtime.write_text(resolved, result, newline=newline)
        except OSError as exc:
            return f"无法写入文件 {label}: {exc}"
        return f"已编辑 {label} (1 处替换)"

    return edit


__all__ = ["make_edit_tool", "replace_exact"]
