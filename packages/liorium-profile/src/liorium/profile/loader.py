"""``load()``：读 JSON → 展开变量 → schema 校验 → 解析 realm source → 校验 Config。

校验全部在 ``load()`` 里完成（决策 D1：eager 导入 + 校验）。任何一步失败都让整个
load 失败 —— 配置错 ≠ 运行期故障；运行期 ``start()`` 失败仍按既有 C5 降级。

文件型 realm 按 ``(绝对路径, mtime_ns, size)`` 缓存：没改复用同一个模块对象，
改了重新执行并替换 ``sys.modules`` 条目。**不加 ``sys.path``**，单文件必须自包含
（相对导入明确不支持）。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .errors import ProfileSchemaError, ProfileSourceError
from .schema import BUILTIN_REALMS, AgentProfile, RealmEntry

_DOLLAR = "\x00liorium-dollar\x00"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_FILE_CACHE: dict[Path, tuple[tuple[int, int], Any]] = {}
"""``绝对路径 -> ((mtime_ns, size), 模块)``；模块只缓存，不参与指纹。"""


# ---------------------------------------------------------------------------
# 展开 ~ / ${VAR}
# ---------------------------------------------------------------------------


def _expand_string(text: str, *, env: Mapping[str, str], path: str) -> str:
    # 先保护 $$，再插值，最后还原 —— 否则 "$${HOME}" 会被二次展开。
    protected = text.replace("$$", _DOLLAR)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ProfileSchemaError(f"{path}: 未定义的环境变量 ${{{name}}}")
        return str(env[name])

    expanded = _VAR.sub(replace, protected).replace(_DOLLAR, "$")
    if expanded.startswith("~"):
        expanded = os.path.expanduser(expanded)
    return expanded


def expand_environment(value: Any, *, env: Mapping[str, str], path: str = "") -> Any:
    """递归展开所有**字符串值**（键不动）；未定义变量报错，不静默留空。"""
    if isinstance(value, str):
        return _expand_string(value, env=env, path=path or "<root>")
    if isinstance(value, Mapping):
        return {
            key: expand_environment(item, env=env, path=f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            expand_environment(item, env=env, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load(path: Path | str, *, env: Mapping[str, str] | None = None) -> AgentProfile:
    """读一个 profile 文件并完成全部加载期校验。"""
    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileSchemaError(f"profile 不存在: {target}") from exc
    except OSError as exc:
        raise ProfileSchemaError(f"profile 读不出来: {target}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ProfileSchemaError(f"profile 不是合法 JSON: {target}（{exc}）") from exc
    return from_dict(
        raw,
        base_dir=target.parent.resolve(),
        env=env,
        default_id=target.stem,
        source=str(target),
    )


def from_dict(
    raw: Mapping[str, Any],
    *,
    base_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    default_id: str | None = None,
    source: str = "<dict>",
) -> AgentProfile:
    """从已解析的 JSON 对象加载（测试 / "配置存在数据库里"的宿主用）。"""
    if not isinstance(raw, Mapping):
        raise ProfileSchemaError(f"{source}: profile 顶层必须是对象，收到 {type(raw).__name__}")
    base = Path(base_dir).expanduser().resolve() if base_dir is not None else Path.cwd()
    environment = os.environ if env is None else env

    expanded = expand_environment(raw, env=environment)
    try:
        profile = AgentProfile.model_validate(expanded)
    except ValidationError as exc:
        raise ProfileSchemaError(_format_validation(exc, source)) from exc

    if profile.id is None:
        if default_id is None:
            raise ProfileSchemaError(
                f"{source}: id 缺失（from_dict 需要显式 id 或 default_id）"
            )
        profile = profile.model_copy(update={"id": default_id})

    profile._base_dir = base  # noqa: SLF001 - loader 就是它的定稿入口
    realms, factories, requires = _resolve_realms(profile, base=base, source=source)
    profile = profile.model_copy(update={"realms": realms})
    profile._realm_factories = factories  # noqa: SLF001
    profile._realm_requires = requires  # noqa: SLF001
    return profile


# ---------------------------------------------------------------------------
# realm 解析
# ---------------------------------------------------------------------------


def _resolve_realms(
    profile: AgentProfile, *, base: Path, source: str
) -> tuple[list[RealmEntry], dict[str, Any], dict[str, tuple[str, ...]]]:
    ids = {realm.id for realm in profile.realms}
    resolved: list[RealmEntry] = []
    factories: dict[str, Any] = {}
    requires: dict[str, tuple[str, ...]] = {}

    for index, entry in enumerate(profile.realms):
        location = f"{source}: realms[{index}]"
        if entry.source is None:
            if entry.id not in BUILTIN_REALMS:
                raise ProfileSchemaError(
                    f"{location}.source: 只有内置 realm 可以省略 source"
                    f"（允许: {', '.join(sorted(BUILTIN_REALMS))}），收到 id={entry.id!r}"
                )
            resolved.append(entry)
            continue

        target = _import_source(entry.source, base=base, location=location)
        declared_id = str(getattr(target, "id", "") or "")
        if declared_id and declared_id != entry.id:
            raise ProfileSourceError(
                f"{location}.id: 与类的 id 不一致（类声明 {declared_id!r}，条目写 {entry.id!r}）"
            )
        declared_requires = tuple(str(item) for item in (getattr(target, "requires", ()) or ()))
        missing = [name for name in declared_requires if name not in ids]
        if missing:
            raise ProfileSourceError(
                f"{location}: requires 指向 profile 里不存在的 realm: {', '.join(missing)}"
            )

        config = _validate_config(target, entry.config, location=location)
        resolved.append(entry.model_copy(update={"config": config}))
        factories[entry.id] = target
        requires[entry.id] = declared_requires

    return resolved, factories, requires


def _validate_config(target: Any, raw: Mapping[str, Any], *, location: str) -> dict[str, Any]:
    config_cls = getattr(target, "Config", None)
    if config_cls is None:
        return dict(raw)
    if not (isinstance(config_cls, type) and issubclass(config_cls, BaseModel)):
        raise ProfileSourceError(
            f"{location}: 类的 Config 必须是 pydantic.BaseModel 子类，收到 {config_cls!r}"
        )
    try:
        validated = config_cls.model_validate(dict(raw))
    except ValidationError as exc:
        raise ProfileSchemaError(_format_validation(exc, f"{location}.config")) from exc
    return validated.model_dump(mode="python")


def _import_source(source: str, *, base: Path, location: str) -> Any:
    """``pkg.mod:Class`` / ``./file.py:Class`` → 类（属性支持点号链）。"""
    module_part, sep, attr_path = source.rpartition(":")
    if not sep or not module_part or not attr_path:
        raise ProfileSourceError(
            f"{location}.source: 需要 ``pkg.mod:Class`` 或 ``./file.py:Class``，收到 {source!r}"
        )
    if module_part.endswith(".py") or "/" in module_part or "\\" in module_part:
        module = _load_file(module_part, base=base, location=location)
    else:
        try:
            module = importlib.import_module(module_part)
        except Exception as exc:  # noqa: BLE001 - 导入失败统一成 ProfileSourceError
            raise ProfileSourceError(
                f"{location}.source: 导入 {module_part!r} 失败: {type(exc).__name__}: {exc}"
            ) from exc

    target: Any = module
    for part in attr_path.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            raise ProfileSourceError(
                f"{location}.source: {module_part!r} 里没有 {attr_path!r}"
            ) from exc
    if not callable(target):
        raise ProfileSourceError(f"{location}.source: {source!r} 不是可调用对象（realm 类）")
    return target


def _load_file(relative: str, *, base: Path, location: str) -> Any:
    path = Path(relative).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise ProfileSourceError(f"{location}.source: 文件不存在: {path}")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ProfileSourceError(f"{location}.source: 读不到文件 {path}（{exc}）") from exc
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _FILE_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    name = f"liorium_profile_realm_{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 平台异常
        raise ProfileSourceError(f"{location}.source: 无法加载文件 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        sys.modules.pop(name, None)
        raise ProfileSourceError(
            f"{location}.source: 执行 {path} 失败: {type(exc).__name__}: {exc}"
        ) from exc
    _FILE_CACHE[path] = (stamp, module)
    return module


# ---------------------------------------------------------------------------
# 错误格式
# ---------------------------------------------------------------------------


def _format_validation(exc: ValidationError, prefix: str) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        subject = f"{prefix}.{location}" if location else prefix
        message = "必填" if error.get("type") == "missing" else str(error.get("msg", "不合法"))
        parts.append(f"{subject}: {message}")
    return "; ".join(parts)


__all__ = ["expand_environment", "from_dict", "load"]
