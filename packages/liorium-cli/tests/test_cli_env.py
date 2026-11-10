"""``.env`` 加载：语法、优先级、与 CLI 的接线。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from liorium.cli.app import _scan_env_args, build_parser, run_cli
from liorium.cli.env import (
    EnvFileError,
    default_env_candidates,
    env_bool,
    load_env,
    parse_env_file,
    write_env_file,
)
from liorium.cli.providers import ProviderRegistry


@pytest.fixture
def preserve_env():
    """``load_env`` 会真写 os.environ，用例跑完要还回去。"""
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_supports_quotes_export_and_expansion(tmp_path: Path) -> None:
    path = write(
        tmp_path / ".env",
        "\n".join(
            [
                "# 注释",
                "",
                'LIORIUM_WORKSPACE="D:\\\\dev\\\\agent\\\\liorium"',
                "LIORIUM_INBOX=${LIORIUM_WORKSPACE}/.liorium-cli/agent",
                "export LIORIUM_MODEL=Go::deepseek-v4.1-flash",
                "LIORIUM_EMBED_KEY='sk-ab#c'",
                "LIORIUM_TEMPERATURE=0.3  # 行尾注释",
                "OTHER_TOOL=x",
            ]
        ),
    )
    values = parse_env_file(path)
    assert values["LIORIUM_WORKSPACE"] == "D:\\dev\\agent\\liorium"
    assert values["LIORIUM_INBOX"].endswith("/.liorium-cli/agent")
    assert values["LIORIUM_MODEL"] == "Go::deepseek-v4.1-flash"
    assert values["LIORIUM_EMBED_KEY"] == "sk-ab#c"  # 单引号里的 # 不是注释
    assert values["LIORIUM_TEMPERATURE"] == "0.3"  # 未加引号的行尾注释被剥掉
    assert values["OTHER_TOOL"] == "x"


@pytest.mark.parametrize(
    "text, match",
    [
        ("LIORIUM A=1\n", "键名不合法"),
        ("JUST_A_WORD\n", "缺少 '='"),
        ('LIORIUM_A="unterminated\n', "引号没有闭合"),
        ('LIORIUM_A="closed" 多余内容\n', "引号后面还有内容"),
    ],
)
def test_parse_errors_point_at_the_line(tmp_path: Path, text: str, match: str) -> None:
    path = write(tmp_path / "bad.env", text)
    with pytest.raises(EnvFileError, match=match):
        parse_env_file(path)
    with pytest.raises(EnvFileError, match=":1:"):
        parse_env_file(path)


def test_load_env_never_overrides_real_environment(tmp_path: Path, preserve_env) -> None:
    path = write(tmp_path / ".env", "LIORIUM_MODEL=from-file\nLIORIUM_TEMPERATURE=0.1\n")
    os.environ["LIORIUM_MODEL"] = "from-shell"
    report = load_env(path)
    assert os.environ["LIORIUM_MODEL"] == "from-shell"
    assert os.environ["LIORIUM_TEMPERATURE"] == "0.1"
    assert list(report.skipped) == ["LIORIUM_MODEL"]
    assert report.loaded and report.path == path
    assert "1 项被外部环境覆盖" in report.describe()

    load_env(path, override=True)
    assert os.environ["LIORIUM_MODEL"] == "from-file"


def test_load_env_reports_foreign_keys(tmp_path: Path, preserve_env) -> None:
    path = write(tmp_path / ".env", "PATH_LIKE=x\nLIORIUM_OK=1\n")
    report = load_env(path)
    assert report.unknown == ["PATH_LIKE"]
    assert report.warnings and "LIORIUM_" in report.warnings[0]
    assert os.environ["LIORIUM_OK"] == "1"


def test_env_file_selection(tmp_path: Path, monkeypatch, preserve_env) -> None:
    first = write(tmp_path / "one.env", "LIORIUM_A=1\n")
    second = write(tmp_path / "two.env", "LIORIUM_A=2\n")

    monkeypatch.setenv("LIORIUM_ENV_FILE", str(second))
    assert default_env_candidates() == [second]
    assert load_env().path == second

    # explicit 覆盖候选顺序；文件不存在要报错（不静默换源）
    assert load_env(first).path == first
    with pytest.raises(EnvFileError, match="不存在"):
        load_env(tmp_path / "missing.env")

    # 空串 = 显式关闭自动加载
    monkeypatch.setenv("LIORIUM_ENV_FILE", "")
    assert default_env_candidates() == []
    assert load_env().loaded is False


def test_write_env_file_roundtrip(tmp_path: Path) -> None:
    target = write_env_file(
        tmp_path / "out.env",
        {"LIORIUM_A": "x y", "LIORIUM_B": "", "LIORIUM_C": "plain", "LIORIUM_D": 'q"q'},
        header="# 头部",
    )
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 头部\n")
    assert parse_env_file(target) == {
        "LIORIUM_A": "x y",
        "LIORIUM_B": "",
        "LIORIUM_C": "plain",
        "LIORIUM_D": 'q"q',
    }


def test_env_bool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIORIUM_FLAG", "yes")
    assert env_bool("LIORIUM_FLAG") is True
    monkeypatch.setenv("LIORIUM_FLAG", "off")
    assert env_bool("LIORIUM_FLAG", True) is False
    monkeypatch.setenv("LIORIUM_FLAG", "maybe")
    with pytest.raises(EnvFileError, match="布尔"):
        env_bool("LIORIUM_FLAG")


def test_cli_defaults_read_env(monkeypatch) -> None:
    """命令行优先于环境变量（argparse 的 default 只在没给参数时生效）。"""
    monkeypatch.setenv("LIORIUM_MODEL", "Go::from-env")
    monkeypatch.setenv("LIORIUM_MEMORY_DIR", "D:/mem")
    monkeypatch.setenv("LIORIUM_NO_COLOR", "1")
    args = build_parser().parse_args([])
    assert args.model == "Go::from-env"
    assert Path(args.memory_dir) == Path("D:/mem")
    assert args.no_color is True

    args = build_parser().parse_args(["--model", "x::from-cli"])
    assert args.model == "x::from-cli"


def test_cli_subagents_flag(monkeypatch) -> None:
    assert build_parser().parse_args([]).no_subagents is False
    assert build_parser().parse_args(["--no-subagents"]).no_subagents is True
    monkeypatch.setenv("LIORIUM_NO_SUBAGENTS", "1")
    assert build_parser().parse_args([]).no_subagents is True


def test_scan_env_args() -> None:
    assert _scan_env_args(["-p", "hi"]) == (None, True)
    assert _scan_env_args(["--no-env"]) == (None, False)
    assert _scan_env_args(["--env-file", "a.env"]) == (Path("a.env"), True)
    assert _scan_env_args(["--env-file=b.env", "--no-env"]) == (Path("b.env"), False)


def test_provider_registry_from_env(monkeypatch) -> None:
    for key in ("LIORIUM_BASE_URL", "LIORIUM_CHAT_KEY", "LIORIUM_MODEL", "LIORIUM_API_FORMAT"):
        monkeypatch.delenv(key, raising=False)
    assert ProviderRegistry.from_env() is None

    monkeypatch.setenv("LIORIUM_BASE_URL", "https://stub.invalid/v1/")
    monkeypatch.setenv("LIORIUM_CHAT_KEY", "sk-stub")
    monkeypatch.setenv("LIORIUM_MODEL", "ignored::demo")
    monkeypatch.setenv("LIORIUM_CONTEXT_WINDOW", "32000")
    registry = ProviderRegistry.from_env()
    assert registry is not None
    assert registry.main_model == "env::demo"
    resolved = registry.resolve("env::demo")
    assert resolved.base_url == "https://stub.invalid/v1"
    assert resolved.provider.api_key == "sk-stub"
    assert resolved.context_window == 32_000
    assert resolved.api_format == "openai_chat"


CONFIG = {
    "main_model": "stub::demo",
    "providers": [
        {
            "name": "stub",
            "base_url": "https://stub.invalid/v1",
            "key": "sk-stub",
            "models": ["demo", "other"],
            "model_context_lengths": {"demo": 64_000, "other": 32_000},
        }
    ],
}


@pytest.mark.asyncio
async def test_run_cli_loads_env_file(tmp_path: Path, capsys, preserve_env) -> None:
    """``--env-file`` 里的 LIORIUM_MODEL 真的会被用上。"""
    from test_cli_agent import stub_factory

    config = tmp_path / "providers.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    env_file = write(tmp_path / "args.env", "LIORIUM_MODEL=stub::other\nLIORIUM_NO_COLOR=1\n")
    labels: list[str] = []

    def factory(label: str):
        labels.append(label)
        return stub_factory(["env 生效"])(label)

    code = await run_cli(
        [
            "--config",
            str(config),
            "--env-file",
            str(env_file),
            "-p",
            "你好",
            "--json",
            "--workspace",
            str(tmp_path / "ws"),
            "--inbox",
            str(tmp_path / "inbox"),
        ],
        model_factory=factory,
        install_signal=False,
    )
    capsys.readouterr()
    assert code == 0
    assert labels and labels[0] == "stub::other"


@pytest.mark.asyncio
async def test_run_cli_rejects_broken_env_file(tmp_path: Path, capsys, preserve_env) -> None:
    env_file = write(tmp_path / "bad.env", "LIORIUM A=1\n")
    code = await run_cli(["--env-file", str(env_file), "-p", "x"], install_signal=False)
    err = capsys.readouterr().err
    assert code == 2
    assert ".env 错误" in err and ":1:" in err


@pytest.mark.asyncio
async def test_run_cli_no_env_ignores_file(tmp_path: Path, capsys, preserve_env) -> None:
    from test_cli_agent import stub_factory

    config = tmp_path / "providers.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    env_file = write(tmp_path / "args.env", "LIORIUM_MODEL=stub::other\nLIORIUM_NO_MEMORY=1\n")
    code = await run_cli(
        [
            "--config",
            str(config),
            "--env-file",
            str(env_file),
            "--no-env",
            "-p",
            "你好",
            "--json",
            "--workspace",
            str(tmp_path / "ws"),
            "--inbox",
            str(tmp_path / "inbox"),
        ],
        model_factory=stub_factory(["ok"]),
        install_signal=False,
    )
    assert code == 0
    # --no-env 时 LIORIUM_NO_MEMORY 不该生效 → 记忆 realm 目录被建出来
    assert (tmp_path / "inbox" / "memory" / "memory.sqlite").is_file()
