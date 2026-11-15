"""生命周期：build → create_agent → start → close 的所有权（计划 E3 / F1 / F2）。"""

from __future__ import annotations

import json
from pathlib import Path

from liorium.agent import LioriumAgent
from liorium.profile import AgentProfile, from_dict
from liorium.provider import ModelFactoryPort, ModelProvider, ModelProviders, ProviderRegistry


def make_registry() -> ProviderRegistry:
    return ProviderRegistry(
        ModelProviders(
            providers=[
                ModelProvider(
                    name="P",
                    base_url="https://x",
                    key="sk-test",
                    models=["m"],
                    model_context_lengths={"m": 4096},
                )
            ],
            main_model="P::m",
        )
    )


def base_profile(tmp_path: Path, realms: list[dict] | None = None) -> AgentProfile:
    return from_dict(
        {"version": 1, "id": "p", "realms": realms or []},
        base_dir=tmp_path,
    )


async def test_agent_close_closes_the_pool(tmp_path: Path) -> None:
    build = base_profile(tmp_path).build(registry=make_registry())
    agent = build.create_agent()

    assert isinstance(agent.model_factory, ModelFactoryPort)
    assert isinstance(agent.compact_model_factory, ModelFactoryPort)

    await agent.start()
    assert build.pool._closed is False

    await agent.close()
    assert build.pool._closed is True

    await build.close()  # 幂等兜底
    assert build.pool._closed is True


async def test_build_agent_convenience(tmp_path: Path) -> None:
    profile = base_profile(tmp_path)
    agent = profile.build_agent(registry=make_registry())
    assert isinstance(agent, LioriumAgent)
    await agent.start()
    await agent.close()


async def test_file_realm_runs_with_config(tmp_path: Path) -> None:
    module = tmp_path / "echo_realm.py"
    module.write_text(
        "from liorium.agent import BaseRealm\n\n"
        "class EchoRealm(BaseRealm):\n"
        "    id = 'echo'\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "p",
                "realms": [{"id": "echo", "source": "./echo_realm.py:EchoRealm"}],
            }
        ),
        encoding="utf-8",
    )

    build = AgentProfile.load(profile_path).build(registry=make_registry())
    agent = build.create_agent()
    await agent.start()
    try:
        record = agent.realms.record("echo")
        assert record.state == "running"
        assert record.context is not None
        assert record.context.config == {}
    finally:
        await agent.close()


async def test_pool_is_not_closed_before_start(tmp_path: Path) -> None:
    build = base_profile(tmp_path).build(registry=make_registry())
    assert build.pool._closed is False
    await build.close()
    assert build.pool._closed is True
