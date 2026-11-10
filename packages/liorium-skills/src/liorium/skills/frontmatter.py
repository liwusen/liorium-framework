"""``SKILL.md`` 的 YAML frontmatter 解析。

技能包的标准格式是在 ``SKILL.md`` 顶部写一段 ``---`` 包起来的 YAML，声明
``name`` / ``description`` / ``version`` 等字段。这里**不强制**依赖 PyYAML：
装了就 `yaml.safe_load`，没装就退化成 ``key: value`` 逐行解析（够用，且不静默
丢整块——解析失败只是少几个字段，正文照读）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SKILL_FILE = "SKILL.md"
META_FILE = "_meta.json"
_MAX_META_BYTES = 64 * 1024
"""元数据读取上限：技能元信息不需要整篇大文件，避免一次读爆内存/上下文。"""


def _read_text(path: Path, *, limit: int = _MAX_META_BYTES) -> str:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    return raw[:limit].decode("utf-8", errors="replace")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """把文本拆成 ``(frontmatter dict, 正文)``；没有 frontmatter 时返回 ``({}, text)``。"""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, stripped
    lines = stripped.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, stripped

    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return _parse_block(block), body.lstrip("\n")


def _parse_block(block: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:  # 可选依赖：解析器存在就用它（能处理引号 / 列表 / 多行）
        import yaml

        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            data = {str(k): v for k, v in parsed.items()}
    except Exception:  # noqa: BLE001 - 退化解析是设计好的降级路径
        data = {}
    if data:
        return data
    for line in block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def read_skill_meta(skill_dir: Path) -> dict[str, Any]:
    """读技能元数据：优先 ``_meta.json``，否则 ``SKILL.md`` 的 frontmatter。

    返回的 dict 一定带 ``slug``（目录名），缺 ``version`` 时补 ``"0.0.0"``。
    """
    skill_dir = Path(skill_dir)
    meta_file = skill_dir / META_FILE
    if meta_file.is_file() and not meta_file.is_symlink():
        try:
            raw = json.loads(_read_text(meta_file))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            meta = {str(k): v for k, v in raw.items()}
            meta.setdefault("slug", skill_dir.name)
            meta.setdefault("version", "0.0.0")
            return meta

    meta = {}
    skill_md = skill_dir / SKILL_FILE
    if skill_md.is_file() and not skill_md.is_symlink():
        try:
            front, _body = split_frontmatter(_read_text(skill_md))
        except OSError:
            front = {}
        meta = dict(front)
    meta.setdefault("slug", skill_dir.name)
    meta.setdefault("version", "0.0.0")
    return meta


def summarize_skill_md(skill_dir: Path, *, limit: int = 200) -> str:
    """从 ``SKILL.md`` 里取一句说明（跳过标题与空行）。"""
    skill_md = Path(skill_dir) / SKILL_FILE
    try:
        if skill_md.is_symlink():
            return ""
        text = _read_text(skill_md)
    except OSError:
        return ""
    _front, body = split_frontmatter(text)
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line[:limit]
    return ""


__all__ = ["META_FILE", "SKILL_FILE", "read_skill_meta", "split_frontmatter", "summarize_skill_md"]
