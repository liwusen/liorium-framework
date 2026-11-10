"""``liorium.core.paths``：路径解析与 Windows 扩展长度前缀（``\\\\?\\``）归一化。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from liorium.core.errors import LioriumError
from liorium.core.paths import normalize_fs_path, resolve_file_uri, resolve_workspace_path

windows_only = pytest.mark.skipif(os.name != "nt", reason="只有 Windows 有 \\\\?\\ 前缀")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX 的 file:// 前导斜杠语义")


@windows_only
def test_normalize_strips_drive_prefix() -> None:
    assert normalize_fs_path(r"\\?\D:\dev\x.txt") == r"D:\dev\x.txt"


@windows_only
def test_normalize_restores_unc_prefix() -> None:
    assert normalize_fs_path(r"\\?\UNC\server\share\x") == r"\\server\share\x"


@windows_only
def test_normalize_is_idempotent() -> None:
    once = normalize_fs_path(r"\\?\D:\dev\x")
    assert normalize_fs_path(once) == once


def test_plain_path_is_untouched() -> None:
    sample = r"D:\dev\x" if os.name == "nt" else "/dev/x"
    assert normalize_fs_path(sample) == sample


def test_relative_path_joins_workspace(tmp_path: Path) -> None:
    got = resolve_workspace_path("a/b.txt", tmp_path)
    assert got == tmp_path / "a" / "b.txt"


def test_file_uri_must_be_absolute() -> None:
    with pytest.raises(LioriumError):
        resolve_file_uri("file://relative/x")


def test_file_uri_round_trips_absolute(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    assert resolve_file_uri(f"file:///{target.as_posix()}") == target


@posix_only
def test_file_uri_collapses_extra_leading_slashes(tmp_path: Path) -> None:
    """``file:////tmp/x`` 与 ``file:///tmp/x`` 等价（RFC 8089 允许空 authority）。"""
    target = tmp_path / "b.txt"
    assert resolve_file_uri(f"file:////{str(target).lstrip('/')}") == target


@windows_only
def test_file_uri_windows_drive() -> None:
    assert resolve_file_uri("file:///C:/x/y.txt") == Path("C:/x/y.txt")


@windows_only
def test_file_uri_windows_unc() -> None:
    assert str(resolve_file_uri("file:////server/share/x")) == r"\\server\share\x"
