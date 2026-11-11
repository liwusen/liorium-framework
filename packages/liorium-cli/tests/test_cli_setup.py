"""``setup`` 子命令、``config.json`` 与「未配置」冷启动。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from liorium.cli import config as cli_config
from liorium.cli import setup as cli_setup
from liorium.cli.app import build_parser, run_cli
from liorium.cli.providers import ProviderRegistry, load_registry


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """隔离的配置根目录：所有 setup / 启动路径都落在 tmp 里。"""
    target = tmp_path / "home"
    monkeypatch.setenv(cli_config.HOME_ENV, str(target))
    monkeypatch.delenv(cli_config.PROVIDERS_ENV, raising=False)
    monkeypatch.delenv(cli_config.CONFIG_ENV, raising=False)
    return target


def make_ctx(home: Path, answers: list[str] | None = None) -> tuple[cli_setup.Ctx, list[str]]:
    lines: list[str] = []
    queue = list(answers or [])

    def ask(_prompt: str) -> str:
        if not queue:
            raise AssertionError("向导问了比预期更多的问题")
        return queue.pop(0)

    ctx = cli_setup.Ctx(
        providers=home / "providers.json",
        config_file=home / "config.json",
        input_fn=ask,
        print_fn=lines.append,
    )
    return ctx, lines


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def test_config_defaults_and_coercion(home: Path) -> None:
    config = cli_config.load_config()
    assert config["thinking"] == "medium"
    assert config["memory"]["enabled"] is True
    assert config["ui"]["color"] is True

    assert cli_config.set_value(config, "thinking", "low") == "low"
    assert cli_config.set_value(config, "memory.enabled", "no") is False
    assert cli_config.set_value(config, "memory.dir", " D:/mem ") == "D:/mem"
    assert cli_config.set_value(config, "memory.vectors", "1") is True
    assert cli_config.set_value(config, "memory.embed_key", "") is None

    with pytest.raises(cli_config.ConfigError, match="未知配置键"):
        cli_config.set_value(config, "memory.nope", 1)
    with pytest.raises(cli_config.ConfigError, match="off/low/medium/high"):
        cli_config.set_value(config, "thinking", "turbo")
    with pytest.raises(cli_config.ConfigError, match="布尔"):
        cli_config.set_value(config, "memory.enabled", "maybe")


def test_config_roundtrip_preserves_unknown_keys(home: Path) -> None:
    path = cli_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"thinking": "high", "未来字段": 42}), encoding="utf-8")

    config = cli_config.load_config()
    assert config["thinking"] == "high"
    assert config["未来字段"] == 42
    assert config["memory"]["enabled"] is True  # 缺的键用默认补齐

    cli_config.set_value(config, "model", "p::m")
    cli_config.save_config(config)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["model"] == "p::m"
    assert saved["未来字段"] == 42


def test_config_rejects_broken_files(home: Path) -> None:
    path = cli_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cli_config.ConfigError, match="JSON"):
        cli_config.load_config()

    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(cli_config.ConfigError, match="顶层"):
        cli_config.load_config()

    assert cli_config.load_config(home / "missing.json")["thinking"] == "medium"
    with pytest.raises(cli_config.ConfigError, match="不存在"):
        cli_config.load_config(home / "missing.json", missing_ok=False)


def test_ensure_home_creates_empty_skeleton(home: Path) -> None:
    created = cli_config.ensure_home()
    assert created["config"] == cli_config.config_path()
    assert created["providers"] == cli_config.providers_path()
    assert json.loads(created["providers"].read_text(encoding="utf-8")) == {"providers": []}
    assert json.loads(created["config"].read_text(encoding="utf-8"))["thinking"] == "medium"

    again = cli_config.ensure_home()
    assert again["config"] is None and again["providers"] is None  # 不覆盖已有文件


def test_mask_secret() -> None:
    assert cli_config.mask_secret("") == "(未设置)"
    assert cli_config.mask_secret("sk-1234567890") == "sk-1…7890"
    assert cli_config.describe_value("memory.embed_key", "sk-1234567890").endswith("7890")


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def test_providers_crud(home: Path) -> None:
    ctx, lines = make_ctx(home)
    assert cli_setup.providers_add(
        ctx,
        "deepseek",
        base_url="https://api.deepseek.com/",
        key="sk-1",
        models=["m1", "m2"],
        api_format="openai_chat",
        context=64_000,
        thinking="deepseek",
    ) == 0
    providers = cli_setup.read_providers(ctx)
    provider = providers.find("deepseek")
    assert provider is not None
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.models == ["m1", "m2"]
    assert provider.model_context_lengths == {"m1": 64_000, "m2": 64_000}
    assert provider.model_api_formats == {"m1": "openai_chat", "m2": "openai_chat"}
    assert provider.thinking_type == "deepseek"
    assert providers.main_model == "deepseek::m1"

    # 追加 / 删除模型 + 改 key
    assert cli_setup.providers_edit(
        ctx, "deepseek", key="sk-2", models=["m3"], remove_models="m2"
    ) == 0
    provider = cli_setup.read_providers(ctx).find("deepseek")
    assert provider is not None
    assert provider.models == ["m1", "m3"]
    assert provider.key == "sk-2"
    assert "m2" not in provider.model_context_lengths

    assert cli_setup.providers_default(ctx, "deepseek::m3") == 0
    assert cli_setup.providers_list(ctx) == 0
    assert cli_setup.providers_show(ctx, "deepseek") == 0
    assert any("m3" in line for line in lines)

    assert cli_setup.providers_remove(ctx, "deepseek") == 0
    assert cli_setup.read_providers(ctx).providers == []
    assert not any(line.startswith("→") for line in lines)


def test_providers_validation(home: Path) -> None:
    ctx, _ = make_ctx(home)
    with pytest.raises(cli_config.ConfigError, match="不能为空"):
        cli_setup.providers_add(ctx, "", base_url="https://a.test")
    with pytest.raises(cli_config.ConfigError, match="::"):
        cli_setup.providers_add(ctx, "a::b", base_url="https://a.test")
    with pytest.raises(cli_config.ConfigError, match="http"):
        cli_setup.providers_add(ctx, "p", base_url="ftp://a.test")
    with pytest.raises(cli_config.ConfigError, match="api 格式"):
        cli_setup.providers_add(ctx, "p", base_url="https://a.test", api_format="nope")
    with pytest.raises(cli_config.ConfigError, match="整数"):
        cli_setup.providers_add(ctx, "p", base_url="https://a.test", context="abc")
    with pytest.raises(cli_config.ConfigError, match="思考方言"):
        cli_setup.providers_add(ctx, "p", base_url="https://a.test", thinking="nope")

    cli_setup.providers_add(ctx, "p", base_url="https://a.test")
    with pytest.raises(cli_config.ConfigError, match="已存在"):
        cli_setup.providers_add(ctx, "p", base_url="https://b.test")
    with pytest.raises(cli_config.ConfigError, match="没有这个 provider"):
        cli_setup.providers_show(ctx, "ghost")
    with pytest.raises(cli_config.ConfigError, match="没有这个 provider"):
        cli_setup.providers_remove(ctx, "ghost")


def test_providers_default_validation(home: Path) -> None:
    ctx, _ = make_ctx(home)
    cli_setup.providers_add(ctx, "a", base_url="https://a.test", models=["m"])
    cli_setup.providers_add(ctx, "b", base_url="https://b.test", models=["m"])
    with pytest.raises(cli_config.ConfigError, match="多个 provider"):
        cli_setup.providers_default(ctx, "m")
    with pytest.raises(cli_config.ConfigError, match="找不到模型"):
        cli_setup.providers_default(ctx, "nope")
    assert cli_setup.providers_default(ctx, "b::m") == 0
    assert cli_setup.read_providers(ctx).main_model == "b::m"


def test_providers_test_requires_key(home: Path) -> None:
    ctx, lines = make_ctx(home)
    cli_setup.providers_add(ctx, "p", base_url="https://p.test", models=["m"])
    assert cli_setup.providers_test(ctx, "p::m") == 1
    assert any("没有 API key" in line for line in lines)

    cli_setup.providers_set_key(ctx, "p", "sk-test-1234")
    assert cli_setup.providers_test(ctx, "p::m") == 0
    assert cli_setup.read_providers(ctx).find("p").key == "sk-test-1234"  # type: ignore[union-attr]


def test_providers_list_and_empty_show(home: Path) -> None:
    ctx, lines = make_ctx(home)
    assert cli_setup.providers_list(ctx) == 0
    assert any("还没有 provider" in line for line in lines)
    with pytest.raises(cli_config.ConfigError):
        cli_setup.providers_test(ctx, "p::m")


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


def test_memory_commands(home: Path, tmp_path: Path) -> None:
    ctx, lines = make_ctx(home)
    target = tmp_path / "mem"
    assert cli_setup.memory_set(ctx, "dir", str(target)) == 0
    assert cli_setup.memory_set(ctx, "embed-model", "text-embedding-3-small") == 0
    assert cli_setup.memory_set(ctx, "vectors", "off") == 0
    assert cli_setup.memory_enable(ctx, False) == 0

    config = cli_config.load_config(ctx.config_file)
    assert config["memory"]["dir"] == str(target)
    assert config["memory"]["embed_model"] == "text-embedding-3-small"
    assert config["memory"]["enabled"] is False
    assert config["memory"]["vectors"] is False

    assert cli_setup.memory_show(ctx) == 0
    assert any("text-embedding-3-small" in line for line in lines)

    assert cli_setup.memory_path(ctx) == 0
    assert str(target) in lines[-1]

    assert cli_setup.memory_init(ctx) == 0
    assert (target / "memory.sqlite").is_file()
    assert (target / "content").is_dir()

    assert cli_setup.memory_enable(ctx, True) == 0
    assert cli_config.load_config(ctx.config_file)["memory"]["enabled"] is True


def test_memory_set_rejects_other_keys(home: Path) -> None:
    ctx, _ = make_ctx(home)
    with pytest.raises(cli_config.ConfigError, match="memory."):
        cli_setup.memory_set(ctx, "thinking", "low")


# ---------------------------------------------------------------------------
# 体检 / 总览 / 命令行入口
# ---------------------------------------------------------------------------


def test_doctor_and_overview(home: Path) -> None:
    ctx, lines = make_ctx(home)
    assert cli_setup.doctor(ctx) == 0
    assert any("providers.json" in line for line in lines)
    assert any("还没有 provider" in line for line in lines) or any(
        "没有 provider" in line for line in lines
    )
    assert cli_setup.print_overview(ctx) == 0
    assert any("config.json" in line for line in lines)


def test_setup_main_init_show_json(home: Path, capsys) -> None:
    assert cli_setup.main(["init"]) == 0
    assert (home / "providers.json").is_file()
    assert (home / "config.json").is_file()

    assert cli_setup.main(["show"]) == 0
    assert "provider" in capsys.readouterr().out

    assert cli_setup.main(["--json", "path"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["providers"].endswith("providers.json")
    assert payload["config"].endswith("config.json")


def test_setup_main_providers_add_and_list(home: Path, capsys) -> None:
    assert (
        cli_setup.main(
            [
                "providers",
                "add",
                "p",
                "--base-url",
                "https://p.test",
                "--key",
                "sk-123456",
                "--model",
                "m1,m2",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli_setup.main(["providers", "list"]) == 0
    out = capsys.readouterr().out
    assert "p" in out and "2 个模型" in out and "有 key" in out


def test_setup_main_json_flag_position(home: Path, capsys) -> None:
    assert cli_setup.main(["providers", "add", "p", "--base-url", "https://p.test", "--model", "m"]) == 0
    capsys.readouterr()
    assert cli_setup.main(["--json", "providers", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["providers"][0]["name"] == "p"
    assert payload["main_model"] == "p::m"


def test_setup_main_errors_return_2(home: Path, capsys) -> None:
    assert cli_setup.main(["providers", "show", "ghost"]) == 2
    assert "没有这个 provider" in capsys.readouterr().err

    assert cli_setup.main(["set", "nope", "1"]) == 2
    assert "未知配置键" in capsys.readouterr().err


def test_setup_config_set_get_unset(home: Path, capsys) -> None:
    assert cli_setup.main(["set", "thinking", "low"]) == 0
    capsys.readouterr()
    assert cli_config.load_config()["thinking"] == "low"
    assert cli_setup.main(["get", "thinking"]) == 0
    assert capsys.readouterr().out.strip().endswith("low")
    assert cli_setup.main(["unset", "thinking"]) == 0
    assert cli_config.load_config()["thinking"] == "medium"


def test_app_main_dispatches_setup(home: Path, capsys) -> None:
    from liorium.cli.app import main

    assert main(["setup", "path"]) == 0
    out = capsys.readouterr().out
    assert "providers" in out


# ---------------------------------------------------------------------------
# 空配置启动
# ---------------------------------------------------------------------------


def test_build_parser_reads_config_defaults(home: Path) -> None:
    config = cli_config.default_config()
    cli_config.set_value(config, "thinking", "low")
    cli_config.set_value(config, "memory.enabled", False)
    cli_config.set_value(config, "memory.dir", "D:/mem")
    cli_config.set_value(config, "model", "p::m")
    cli_config.set_value(config, "workspace", "D:/ws")
    cli_config.set_value(config, "ui.color", False)

    args = build_parser(config=config).parse_args([])
    assert args.thinking == "low"
    assert args.no_memory is True
    assert Path(args.memory_dir) == Path("D:/mem")
    assert args.model == "p::m"
    assert Path(args.workspace) == Path("D:/ws")
    assert args.no_color is True

    # 命令行仍然最高
    assert build_parser(config=config).parse_args(["--thinking", "high"]).thinking == "high"


def test_load_registry_treats_empty_config_as_unconfigured(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "providers.json").write_text('{"providers": []}', encoding="utf-8")
    registry, tried = load_registry(None, candidates=[home / "providers.json"])
    assert registry is None
    assert tried == [home / "providers.json"]

    registry, tried = load_registry(home / "providers.json")
    assert registry is None


@pytest.mark.asyncio
async def test_cold_start_creates_files_and_repl_works(home: Path, capsys) -> None:
    answers = iter(["/help", "/show", "/exit"])
    code = await run_cli(
        [],
        input_fn=lambda _prompt: next(answers),
        install_signal=False,
        interactive=True,
    )
    assert code == 0
    assert json.loads((home / "providers.json").read_text(encoding="utf-8")) == {
        "providers": []
    }
    assert json.loads((home / "config.json").read_text(encoding="utf-8"))["version"] == 1
    out = capsys.readouterr().out
    assert "还没有配置 provider" in out
    assert "/setup" in out


@pytest.mark.asyncio
async def test_cold_start_non_interactive_returns_2(home: Path, capsys) -> None:
    code = await run_cli(["-p", "你好"], install_signal=False, interactive=False)
    assert code == 2
    assert "尚未配置 provider" in capsys.readouterr().err
    assert (home / "providers.json").is_file()  # 骨架仍然建出来了


@pytest.mark.asyncio
async def test_cold_start_setup_then_starts_session(home: Path, tmp_path: Path, capsys) -> None:
    from test_cli_agent import stub_factory

    answers = [
        "/setup",           # 进向导
        "1",                # provider
        "a",                # 新增
        "stub",             # 名字
        "https://stub.invalid/v1",  # base_url
        "sk-test",          # key
        "demo",             # 模型
        "",                 # api 格式 = 自动
        "",                 # 上下文 = 不设置
        "",                 # 思考方言 = auto
        "y",                # 设为默认
        "",                 # 返回 provider 菜单
        "0",                # 退出向导
        "/exit",            # 会话里退出
    ]
    code = await run_cli(
        ["--workspace", str(tmp_path / "ws"), "--inbox", str(tmp_path / "inbox")],
        input_fn=lambda _prompt: answers.pop(0),
        install_signal=False,
        interactive=True,
        model_factory=stub_factory(["ok"]),
    )
    assert code == 0
    saved = json.loads((home / "providers.json").read_text(encoding="utf-8"))
    assert saved["providers"][0]["name"] == "stub"
    assert saved["main_model"] == "stub::demo"
    out = capsys.readouterr().out
    assert "provider 已配置，启动会话" in out


@pytest.mark.asyncio
async def test_wizard_provider_edit_and_remove(home: Path) -> None:
    ctx, lines = make_ctx(
        home,
        [
            "e",                       # 编辑
            "p",                       # provider 名
            "3",                       # 追加模型
            "m2",
            "0",                       # 结束编辑
            "d",                       # 删除
            "p",
            "y",
            "",                        # 返回
        ],
    )
    cli_setup.providers_add(ctx, "p", base_url="https://p.test", models=["m1"])
    result = cli_setup.wizard_providers(ctx)
    assert result.providers_changed is True
    assert cli_setup.read_providers(ctx).providers == []
    assert any("已删除 provider p" in line for line in lines)


def test_wizard_memory_toggles(home: Path, tmp_path: Path) -> None:
    ctx, lines = make_ctx(
        home,
        [
            "1",                       # 设置目录
            str(tmp_path / "mem"),
            "2",                       # embed 模型
            "text-embedding-3-small",
            "5",                       # 开关记忆
            "0",                       # 返回
        ],
    )
    result = cli_setup.wizard_memory(ctx)
    assert result.memory_changed is True
    assert result.memory_dir_changed is True
    assert result.memory_embed_changed is True
    assert result.memory_enabled is False
    config = cli_config.load_config(ctx.config_file)
    assert config["memory"]["dir"] == str(tmp_path / "mem")
    assert config["memory"]["embed_model"] == "text-embedding-3-small"
    assert config["memory"]["enabled"] is False


@pytest.mark.asyncio
async def test_setup_command_in_session_adds_provider_and_switches(
    home: Path, tmp_path: Path
) -> None:
    """会话内 /setup 新增 provider 后热切换，不用重启。"""
    from liorium.cli.session import CliOptions, CliSession
    from liorium_testkit import write_workspace
    from test_cli_agent import stub_factory

    ctx, _ = make_ctx(home)
    cli_setup.providers_add(ctx, "a", base_url="https://a.test", models=["m1"])
    answers = iter(
        [
            "a",                      # 新增 provider
            "b",                      # 名字
            "https://b.test",         # base_url
            "sk-b",                   # key
            "m2",                     # 模型
            "",                       # api 格式 = 自动
            "",                       # 上下文 = 不设置
            "",                       # 思考方言 = auto
            "y",                      # 设为默认
            "",                       # 返回
            "0",                      # /setup memory 直接返回
        ]
    )
    registry = ProviderRegistry.load(home / "providers.json")
    options = CliOptions(
        workspace=write_workspace(tmp_path / "ws"),
        inbox=tmp_path / "inbox",
        color=False,
        config_file=home / "config.json",
        providers_file=home / "providers.json",
        models_dev_cache=tmp_path / "cache" / "api.json",
    )
    session = CliSession(
        registry,
        registry.resolve("a::m1"),
        options,
        input_fn=lambda _prompt: next(answers),
        model_factory=stub_factory(["ok"]),
    )
    await session.start()
    try:
        await session.handle_command("/setup providers")
        assert session.resolved.label == "b::m2"
        saved = json.loads((home / "providers.json").read_text(encoding="utf-8"))
        assert [p["name"] for p in saved["providers"]] == ["a", "b"]
        await session.handle_command("/setup show")
        await session.handle_command("/setup memory")
    finally:
        await session.close()


def test_overview_uses_session_inbox(home: Path, tmp_path: Path) -> None:
    """会话内 /setup 的路径展示要用当前会话的 inbox，而不是 cwd 的默认值。"""
    ctx, _ = make_ctx(home)
    ctx.inbox = tmp_path / "inbox"
    data = cli_setup.overview(ctx)
    assert data["paths"]["inbox"] == str(tmp_path / "inbox")
    assert data["paths"]["memory"] == str(tmp_path / "inbox" / "memory")
    assert data["paths"]["skills"] == str(tmp_path / "inbox" / "skill.d")

    assert cli_setup.memory_path(ctx) == 0
    assert cli_setup.memory_show(ctx) == 0


def test_setup_provider_file_write_keeps_key(home: Path) -> None:
    """key 只落在 providers.json 里，config.json 不出现任何密钥。"""
    ctx, _ = make_ctx(home)
    cli_setup.providers_add(ctx, "p", base_url="https://p.test", models=["m"], key="sk-x")
    payload = json.loads((home / "providers.json").read_text(encoding="utf-8"))
    assert payload["providers"][0]["key"] == "sk-x"
    config_payload = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert "sk-x" not in json.dumps(config_payload, ensure_ascii=False)
