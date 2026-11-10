"""``liorium.core.canonical`` 的回归测试（计划 §4.2 指纹只覆盖结构）。

要点：

- 同一结构必须产生同一指纹（dict 顺序 / set 顺序 / tuple-vs-list 都不许影响）；
- dataclass / ``Path`` / 集合 / 元组都有稳定的 canonical 形态；
- 不可序列化对象（lambda、连接、回调）**抛 TypeError**，绝不用 ``repr()`` 静默吞掉
  （``repr`` 里含 ``id`` 会让同一结构每次指纹都不同）；
- ``bytes`` 走 sha256，不把二进制内容当字符串塞进指纹。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from liorium.core.canonical import canonicalize, fingerprint, short_fingerprint

# ---------------------------------------------------------------------------
# 结构稳定：dict / set / tuple
# ---------------------------------------------------------------------------


def test_dict_order_does_not_change_fingerprint() -> None:
    """同一结构不同 dict 插入顺序 → 同一指纹（CI 断言"重复 apply 零变更"）。"""
    left = {"model": "m", "limits": {"max_turns": 300, "ratio": 0.8}, "realms": ["a", "b"]}
    right = {"realms": ["a", "b"], "limits": {"ratio": 0.8, "max_turns": 300}, "model": "m"}
    assert fingerprint(left) == fingerprint(right)


def test_dict_keys_are_sorted_in_canonical_form() -> None:
    assert list(canonicalize({"b": 1, "a": 2, "c": 3})) == ["a", "b", "c"]


def test_dict_keys_are_stringified() -> None:
    """非字符串键统一 ``str()``，否则 JSON 序列化会炸。"""
    assert canonicalize({1: "a", "2": "b"}) == {"1": "a", "2": "b"}


def test_tuple_and_list_normalize_to_the_same_shape() -> None:
    """``tuple`` / ``list`` 都压成列表（spec 里两者混用不该产生两个指纹）。"""
    assert canonicalize((1, "a")) == [1, "a"]
    assert canonicalize(("only",)) == ["only"]
    assert fingerprint([1, 2, 3]) == fingerprint((1, 2, 3))


def test_set_order_is_not_part_of_fingerprint() -> None:
    """集合无序，必须排序后才能进指纹。"""
    assert canonicalize({3, 1, 2}) == [1, 2, 3]
    assert canonicalize(frozenset({"b", "a"})) == ["a", "b"]
    assert fingerprint({3, 1, 2}) == fingerprint({1, 2, 3})
    assert fingerprint({"a", "b"}) == fingerprint(frozenset({"b", "a"}))


def test_nested_structures_are_stable() -> None:
    left = {"outer": [{"b": {2, 1}, "a": (1, 2)}]}
    right = {"outer": [{"a": [1, 2], "b": {1, 2}}]}
    assert fingerprint(left) == fingerprint(right)


# ---------------------------------------------------------------------------
# dataclass / Path / Enum
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class OtherPoint:
    x: int
    y: int


def test_dataclass_has_stable_canonical_form() -> None:
    assert canonicalize(Point(1, 2)) == {
        "__type__": "Point",
        "fields": {"x": 1, "y": 2},
    }
    assert fingerprint(Point(1, 2)) == fingerprint(Point(1, 2))
    assert fingerprint(Point(1, 2)) != fingerprint(Point(2, 1))


def test_dataclass_type_name_is_part_of_fingerprint() -> None:
    """只比字段会撞车：两个不同 dataclass 同字段必须指纹不同。"""
    assert canonicalize(Point(1, 2)) != canonicalize(OtherPoint(1, 2))
    assert fingerprint(Point(1, 2)) != fingerprint(OtherPoint(1, 2))


def test_dataclass_class_object_is_not_treated_as_instance() -> None:
    """``dataclasses.is_dataclass(类)`` 也是 True；类对象本身必须走 TypeError 分支。"""
    with pytest.raises(TypeError):
        canonicalize(Point)


def test_path_is_posix_string() -> None:
    assert canonicalize(Path("a/b")) == "a/b"
    assert canonicalize(Path("/x/y")) == "/x/y"
    assert fingerprint(Path("a") / "b") == fingerprint("a/b")


def test_enum_uses_value() -> None:
    class Color(Enum):
        RED = "red"

    assert canonicalize(Color.RED) == "red"
    assert fingerprint({"c": Color.RED}) == fingerprint({"c": "red"})


def test_bool_is_not_confused_with_int() -> None:
    """``True`` 与 ``1`` 在 JSON 里不同形，指纹必须不同。"""
    assert fingerprint(True) != fingerprint(1)


# ---------------------------------------------------------------------------
# bytes 走 sha256
# ---------------------------------------------------------------------------


def test_bytes_are_hashed_not_embedded() -> None:
    assert canonicalize(b"abc") == {"__bytes_sha256__": hashlib.sha256(b"abc").hexdigest()}


def test_bytes_fingerprint_is_deterministic_and_discriminating() -> None:
    assert fingerprint(b"abc") == fingerprint(b"abc")
    assert fingerprint(b"abc") != fingerprint(b"abd")
    assert fingerprint(b"") == fingerprint(b"")


def test_bytes_like_objects_share_fingerprint() -> None:
    """``bytearray`` / ``memoryview`` 与 ``bytes`` 等价（按内容哈希）。"""
    assert fingerprint(bytearray(b"abc")) == fingerprint(b"abc")
    assert fingerprint(memoryview(b"abc")) == fingerprint(b"abc")


def test_bytes_and_str_do_not_collide() -> None:
    """二进制内容不许被当成文本：``b"abc"`` 与 ``"abc"`` 指纹必须不同。"""
    assert fingerprint(b"abc") != fingerprint("abc")


def test_nested_bytes_use_sha256() -> None:
    payload = {"blob": b"abc"}
    assert canonicalize(payload) == {"blob": {"__bytes_sha256__": hashlib.sha256(b"abc").hexdigest()}}


# ---------------------------------------------------------------------------
# 不可序列化对象 → TypeError（不许静默 repr）
# ---------------------------------------------------------------------------


def test_lambda_raises_type_error() -> None:
    with pytest.raises(TypeError):
        canonicalize(lambda value: value)


def test_arbitrary_object_raises_type_error() -> None:
    with pytest.raises(TypeError):
        fingerprint(object())


def test_nested_unserializable_object_raises_type_error() -> None:
    """深层混进来的回调也要炸，不许"部分序列化成功"。"""
    with pytest.raises(TypeError):
        fingerprint({"deep": [{"cb": lambda: None}]})


def test_error_message_names_the_type_but_never_uses_repr() -> None:
    """报错要指出类型名；正文里不许出现 ``repr`` 的 ``id``（否则指纹会漂移）。"""

    class FakeConnection:
        def __repr__(self) -> str:  # pragma: no cover - 只用于断言“没被调用”
            return "<FakeConnection id=0xdeadbeef>"

    with pytest.raises(TypeError) as excinfo:
        fingerprint(FakeConnection())
    message = str(excinfo.value)
    assert "FakeConnection" in message
    assert "0xdeadbeef" not in message


# ---------------------------------------------------------------------------
# 摘要形态
# ---------------------------------------------------------------------------


def test_fingerprint_is_sha256_hex() -> None:
    digest = fingerprint({"a": 1})
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_fingerprint_is_order_independent_for_nested_containers() -> None:
    assert fingerprint({"a": [1, 2], "b": {"c": 3}}) == fingerprint({"b": {"c": 3}, "a": [1, 2]})


def test_short_fingerprint_is_a_prefix() -> None:
    value = {"a": [1, 2, 3]}
    assert short_fingerprint(value) == fingerprint(value)[:12]
    assert len(short_fingerprint(value, length=8)) == 8


def test_different_structures_differ() -> None:
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert fingerprint([1, 2]) != fingerprint([2, 1])
