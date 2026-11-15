"""事件流示例：四个场景跑通，且关键帧 / 注入结果与文档描述一致。"""

from __future__ import annotations

import pytest

from examples.event_stream import main


@pytest.mark.asyncio
async def test_event_stream_example_runs(capsys: pytest.CaptureFixture[str]) -> None:
    await main()
    out = capsys.readouterr().out

    for header in ("=== ①", "=== ②", "=== ③", "=== ④"):
        assert header in out

    # ① 一个回合：AUTO 插话与环境注入都在当前回合内物化，没有开第二个回合。
    assert "inputs=['q1']" in out
    assert '<context source="user"' in out
    assert "观众数 42" in out
    assert out.count("turn_id=demo#1") >= 2 and "turn_id=demo#2" not in out.split("=== ②")[0]

    # ② 节流：env1 不进第一个回合，搭第二个回合的车。
    assert "inputs=['q1', 'env0']" in out
    assert "inputs=['q2', 'env1']" in out

    # ③ 合批：一个回合代表整批两条 id。
    assert "inputs=['b1', 'b2']" in out

    # ④ 中断：以 interrupted 收尾（终态帧）。
    assert "interrupted turn_id=demo#1 reason=user" in out
