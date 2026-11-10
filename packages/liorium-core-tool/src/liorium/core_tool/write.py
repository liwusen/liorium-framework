"""``write`` 工具：创建或覆盖真实文件 / 可写 VFS 节点。

返回文本冻结自 ``docs/design/vfs-output-mw.md`` §2.1：

- 真实文件：``已写入 {file_path} ({size} bytes)``
- VFS 节点：``已写入 {scheme}://{path} ({size} bytes)``，handler 返回非空字符串时
  追加 ``\\n↳ {ack}``（ACK 通道，§9.5）
- ``size`` 恒为 ``len(content.encode("utf-8"))``（按内容算，不按磁盘字节算）

Liorium 差异：不再有 ``memory://`` 分支（记忆是独立 realm 的工具），也不做
「只允许写项目目录内」的边界判断（v1 无权限系统，§F3）。只读挂载 / 只读节点
**抛错**而不是返回一段看起来像成功的文本，避免静默丢掉写入。
"""

from __future__ import annotations

from langchain_core.tools import tool

from liorium.core.errors import VfsError, VfsReadOnlyError

from .runtime import ToolRuntime


def make_write_tool(runtime: ToolRuntime):
    """构造绑定 ``runtime`` 的 ``write`` 工具。"""

    @tool
    async def write(path: str, content: str) -> str:
        """创建或整份覆盖一个文件 / 资源节点。

        怎么选：
        - 裸路径或 `file://` → 真实文件系统。相对路径基于 Agent 工作区，父目录自动创建。
        - `<scheme>://` → 虚拟文件系统里可写的节点（例如 `agent://notes/todo.md`）。
          只读挂载会直接报错（不会假装写入成功）。

        何时用：新建文件、整份替换、写配置/脚本。只想改文件里的一小段时用 edit；
        只想看内容用 read。写入是覆盖式的，不追加。

        注意：节点写入可能带一行回执，形如 `↳ 已提交 task_7`——那是插件/节点返回的
        结果（例如任务 id），要当成工具结果读，不是错误。

        Args:
            path (str): 目标路径（裸路径 / `file://` / `<scheme>://`）。
            content (str): 完整的新内容（纯文本，覆盖式写入）。
        """
        raw = str(path or "").strip()
        if not raw:
            return "错误: path 不能为空"

        text = str(content or "")
        size = len(text.encode("utf-8"))
        resolved = runtime.resolve_path(raw)

        if resolved.uri.selector:
            return (
                f"错误: {raw} 带行选择器 {resolved.uri.selector}；"
                "write 只接受文件路径或 `<scheme>://` URI"
            )

        if resolved.is_vfs:
            if resolved.path == "/":
                return f"错误: {resolved.scheme}:// 路径不能为空"
            try:
                ack = await runtime.vfs_write_text(resolved.path, text)
            except VfsReadOnlyError as exc:
                raise VfsReadOnlyError(
                    f"写入被拒绝: {resolved.display} 是只读挂载或只读节点，未做任何修改"
                ) from exc
            except VfsError as exc:
                raise VfsError(f"写入 {resolved.display} 失败: {exc}") from exc
            except Exception as exc:
                # handler 自己抛的错（例如节点拒绝非法信封）：保留可读原因给模型
                return f"写入 {resolved.display} 出错: {exc}"
            result = f"已写入 {resolved.display} ({size} bytes)"
            if ack:
                # ACK 通道冻结格式：换行 + `↳ ` + handler 原文（多行保留）
                result += f"\n↳ {ack}"
            return result

        try:
            await runtime.write_bytes(resolved, text.encode("utf-8"))
        except OSError as exc:
            return f"写入文件出错: {exc}"
        return f"已写入 {resolved.path} ({size} bytes)"

    return write


__all__ = ["make_write_tool"]
