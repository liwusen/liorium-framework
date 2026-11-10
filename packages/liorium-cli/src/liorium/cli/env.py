"""``.env`` 加载：让 CLI 的所有配置都能写进一个文件。

不引 ``python-dotenv``（CLI 不想为一个几十行的解析器多背一个依赖）。支持的语法刚好够用：

```dotenv
# 注释；空行忽略
LIORIUM_MODEL=Go::deepseek-v4.1-flash
export LIORIUM_WORKSPACE="D:\\dev\\agent\\liorium"   # 允许 export 前缀与双引号
LIORIUM_EMBED_KEY='sk-...'                              # 单引号 = 原样
LIORIUM_INBOX=${LIORIUM_WORKSPACE}/.liorium-cli/agent     # ${VAR} 展开（先本次、再环境变量）
```

规则：

- ``KEY=VALUE``，键名不合法（含空格/连字符）直接报错，不静默忽略——``.env`` 写错的
  代价是"配置看起来没生效"，比启动失败更难查；
- 值两端的引号会被剥掉；双引号里支持 ``\\n`` 与 ``\\"``，单引号原样；
- **不覆盖**已经存在的环境变量（``override=False`` 默认），这样 ``$env:LIORIUM_MODEL=...``
  或命令行临时变量永远优先；
- 文件里不认识的键（不带 ``LIORIUM_`` 前缀）只警告一次，不报错（可能有人顺手放了别的工具变量）。

优先级：**命令行参数 > 进程环境变量 > .env > 内置默认**。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

ENV_PREFIX = "LIORIUM_"
"""CLI 只认这个前缀的键（``LIORIUM_MODEL`` → ``--model``）。"""

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPAND_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class EnvFileError(RuntimeError):
    """``.env`` 语法错误（行号会带上，别让用户猜）。"""


@dataclass
class EnvReport:
    """一次加载的结果（给 UI 显示"配置来自哪、生效了几项"）。"""

    path: Path | None = None
    values: dict[str, str] = field(default_factory=dict)
    applied: dict[str, str] = field(default_factory=dict)
    """真正写进 ``os.environ`` 的键（已被外部环境覆盖的不算）。"""
    skipped: dict[str, str] = field(default_factory=dict)
    """文件里有、但环境变量已经设置了（因此保留外部值）的键。"""
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return self.path is not None

    def describe(self) -> str:
        if self.path is None:
            return "env: 未找到 .env"
        extra = f"，{len(self.skipped)} 项被外部环境覆盖" if self.skipped else ""
        return f"env: {self.path}（生效 {len(self.applied)} 项{extra}）"


def default_env_candidates() -> list[Path]:
    """默认查找顺序：显式环境变量 → 当前目录的 ``.env`` / ``.env.local``。

    ``LIORIUM_ENV_FILE`` 设成空串（或 ``none``/``0``）表示**不自动加载**——测试与
    "我只想用真实环境变量"的场景靠它关掉，否则开发机上的 ``.env`` 会污染测试。
    """
    candidates: list[Path] = []
    explicit = os.environ.get("LIORIUM_ENV_FILE")
    if explicit is not None:
        if explicit.strip().lower() in ("", "none", "0", "off"):
            return []
        return [Path(explicit).expanduser()]
    cwd = Path.cwd()
    for name in (".env", ".env.local"):
        candidates.append(cwd / name)
    return candidates


def load_env(
    path: Path | None = None,
    *,
    override: bool = False,
    candidates: Iterable[Path] | None = None,
) -> EnvReport:
    """加载 ``.env``（``path`` 为空时按 :func:`default_env_candidates` 找）。"""
    report = EnvReport()
    target = _pick_file(path, candidates)
    if target is None:
        return report
    report.path = target
    report.values = parse_env_file(target)
    for key, value in report.values.items():
        if not key.startswith(ENV_PREFIX):
            report.unknown.append(key)
            continue
        if not override and key in os.environ:
            report.skipped[key] = value
            continue
        os.environ[key] = value
        report.applied[key] = value
    if report.unknown:
        report.warnings.append(
            "以下键没有 LIORIUM_ 前缀，已忽略: " + ", ".join(report.unknown[:8])
        )
    return report


def _pick_file(path: Path | None, candidates: Iterable[Path] | None) -> Path | None:
    if path is not None:
        target = Path(path).expanduser()
        if not target.is_file():
            raise EnvFileError(f"--env-file 指向的文件不存在: {target}")
        return target
    for candidate in candidates if candidates is not None else default_env_candidates():
        if candidate.is_file():
            return candidate
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 ``.env`` 返回 ``{key: value}``（按文件顺序，``${VAR}`` 可引用前面的键）。"""
    global _CURRENT
    target = Path(path)
    previous, _CURRENT = _CURRENT, {}
    try:
        text = target.read_text(encoding="utf-8-sig")
        values: dict[str, str] = {}
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                raise EnvFileError(f"{target}:{lineno}: 缺少 '='（{raw.strip()!r}）")
            key = key.strip()
            if not _KEY_RE.match(key):
                raise EnvFileError(f"{target}:{lineno}: 键名不合法 {key!r}")
            parsed = _parse_value(value.strip(), path=target, lineno=lineno)
            values[key] = parsed
            _CURRENT[key] = parsed
        return values
    finally:
        _CURRENT = previous


def _parse_value(raw: str, *, path: Path, lineno: int) -> str:
    if raw.startswith('"'):
        body = _read_quoted(raw, '"', path=path, lineno=lineno)
        body = body.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        return _expand(body)
    if raw.startswith("'"):
        return _read_quoted(raw, "'", path=path, lineno=lineno)
    # 未加引号：允许行尾 ` # 注释`
    if " #" in raw:
        raw = raw.split(" #", 1)[0]
    return _expand(raw.strip())


def _read_quoted(raw: str, quote: str, *, path: Path, lineno: int) -> str:
    end = raw.find(quote, 1)
    while end > 0 and quote == '"' and raw[end - 1] == "\\":
        end = raw.find(quote, end + 1)
    if end < 0:
        raise EnvFileError(f"{path}:{lineno}: 引号没有闭合")
    rest = raw[end + 1 :].strip()
    if rest and not rest.startswith("#"):
        raise EnvFileError(f"{path}:{lineno}: 引号后面还有内容 {rest!r}")
    return raw[1:end]


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _CURRENT:
            return _CURRENT[name]
        return os.environ.get(name, "")

    return _EXPAND_RE.sub(replace, value)


_CURRENT: dict[str, str] = {}
"""本次解析出来的值（``${VAR}`` 优先用同文件里先定义的那个）。

模块级可变状态只用于"一次解析"，加载发生在启动期、单线程，不参与并发。
"""


def env_bool(name: str, default: bool = False) -> bool:
    """读一个布尔开关（``1/true/yes/on`` 真，``0/false/no/off`` 假）。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise EnvFileError(f"{name}={raw!r} 不是合法布尔值（用 1/0/true/false）")


def env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip().replace(",", "").replace("_", ""))
    except ValueError as exc:
        raise EnvFileError(f"{name}={raw!r} 不是整数") from exc


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise EnvFileError(f"{name}={raw!r} 不是数字") from exc


def env_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw.strip()).expanduser()


def as_dict() -> dict[str, str]:
    """当前进程里所有 ``LIORIUM_*``（给 ``/status`` 或调试用，**不要打印值**）。"""
    return {key: value for key, value in os.environ.items() if key.startswith(ENV_PREFIX)}


def write_env_file(path: Path, values: Mapping[str, str], *, header: str = "") -> Path:
    """写一个 ``.env``（键排序、值按需加引号）；已存在则覆盖。"""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if header:
        for line in header.splitlines():
            stripped = line.strip()
            if not stripped:
                lines.append("#")
            elif stripped.startswith("#"):
                lines.append(stripped)
            else:
                lines.append(f"# {stripped}")
    for key in sorted(values):
        lines.append(f"{key}={_quote(values[key])}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _quote(value: str) -> str:
    text = str(value)
    if not text:
        return ""
    if any(char in text for char in ' #"\n\t') or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
        return f'"{escaped}"'
    return text


__all__ = [
    "ENV_PREFIX",
    "EnvFileError",
    "EnvReport",
    "as_dict",
    "default_env_candidates",
    "env_bool",
    "env_float",
    "env_int",
    "env_path",
    "env_str",
    "load_env",
    "parse_env_file",
    "write_env_file",
]
