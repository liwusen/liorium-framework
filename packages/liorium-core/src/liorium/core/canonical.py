"""结构指纹：canonical JSON + sha256。

计划依据 §4.2：指纹只覆盖**结构**（realm 列表 / 工具集 / 模型 / prompt 来源 / 参数），
不覆盖 realm 内部状态。同一结构必须产生同一指纹（CI 断言"重复 apply 零变更"）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

_UNSUPPORTED = object()


def canonicalize(value: Any) -> Any:
    """把任意结构压成 canonical 可序列化形态。

    规则（顺序即优先级）：

    - dataclass → ``{"__type__": 类名, "fields": {...}}``（字段排序）
    - ``Path`` → 字符串（posix 风格）
    - ``frozenset`` / ``set`` → 排序后的列表（集合无序，必须稳定）
    - ``tuple`` / ``list`` → 列表
    - ``dict`` → 键排序的 dict
    - ``Enum`` → ``value``
    - 其它 → 原样

    不可序列化对象（模型实例、回调、websocket）应当**不进指纹**；
    若混进来会抛 ``TypeError``，而不是被 ``repr()`` 悄悄吞掉（``repr`` 里含 id 会让
    同一结构产生不同指纹）。
    """
    from enum import Enum

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes_sha256__": hashlib.sha256(bytes(value)).hexdigest()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {}
        for f in dataclasses.fields(value):
            fields[f.name] = canonicalize(getattr(value, f.name))
        return {"__type__": type(value).__name__, "fields": fields}
    if isinstance(value, (frozenset, set)):
        items = [canonicalize(v) for v in value]
        items.sort(key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
        return items
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): canonicalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    raise TypeError(
        f"无法进入指纹的类型: {type(value).__name__}；"
        "不可序列化对象（模型/回调/连接）必须走 ports，不进 AgentSpec"
    )


def fingerprint(value: Any) -> str:
    """sha256(canonical JSON)，hex 摘要。"""
    payload = canonicalize(value)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def short_fingerprint(value: Any, length: int = 12) -> str:
    """短指纹，仅用于日志。"""
    return fingerprint(value)[:length]


__all__ = ["canonicalize", "fingerprint", "short_fingerprint"]
