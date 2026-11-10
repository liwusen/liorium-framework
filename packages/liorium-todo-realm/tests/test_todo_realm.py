"""``TodoRealm``：两个工具 + 状态持久化 + 每 50 回合的周期上下文。

这里只用 ``RealmContext`` + ``JsonStateStore`` 起 realm，**不 import ``liorium.agent``**
—— 这也是一条证据：写一个功能完整的 realm 不需要背 Agent 运行时。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liorium.realm_base import JsonStateStore, RealmContext, TurnInfo
from liorium.todo_realm import STATES, TodoRealm, render_context


async def started(
    tmp_path: Path, *, params: dict[str, Any] | None = None
) -> tuple[TodoRealm, RealmContext]:
    realm = TodoRealm(SimpleNamespace(params=dict(params or {})))
    ctx = RealmContext(
        realm_id="todo",
        vfs=None,  # type: ignore[arg-type] - 本 realm 没有挂载，不需要真 VFS
        state=JsonStateStore(tmp_path / "realm_state" / "todo.json", realm_id="todo"),
        params=dict(params or {}),
    )
    # 与 RealmManager.start() 一致：先 load 再 start
    ctx.state.load()
    await realm.start(ctx)
    return realm, ctx


def state_file(tmp_path: Path) -> Path:
    return tmp_path / "realm_state" / "todo.json"


def tools(realm: TodoRealm) -> dict[str, Any]:
    return {tool.name: tool for tool in realm.tools()}


# ---------------------------------------------------------------------------
# 工具面
# ---------------------------------------------------------------------------


async def test_exactly_two_tools(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    assert [tool.name for tool in realm.tools()] == ["todo", "todo_list"]


async def test_add_assigns_incrementing_ids_and_persists(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]

    assert await todo.ainvoke({"action": "add", "text": "写周报"}) == "已添加 t1 [pending] 写周报"
    assert await todo.ainvoke({"action": "add", "text": "回邮件"}) == "已添加 t2 [pending] 回邮件"

    # 每次写操作立刻落盘（不靠 stop / flush）
    payload = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
    assert [item["text"] for item in payload["items"]] == ["写周报", "回邮件"]
    assert payload["next_id"] == 3
    assert [item.id for item in realm.todos()] == ["t1", "t2"]


async def test_update_text_and_status(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "写周报"})

    # id 大小写不敏感，状态大小写也收
    result = await todo.ainvoke({"action": "update", "id": "T1", "status": "PROCESSING"})
    assert result == "已更新 t1 [processing] 写周报"

    result = await todo.ainvoke({"action": "update", "id": "t1", "text": "写周报初稿"})
    assert result == "已更新 t1 [processing] 写周报初稿"

    item = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))["items"][0]
    assert item["text"] == "写周报初稿"
    assert item["state"] == "processing"
    assert item["created_at"] and item["updated_at"]


async def test_remove_never_reuses_id(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "甲"})
    await todo.ainvoke({"action": "add", "text": "乙"})

    assert await todo.ainvoke({"action": "remove", "id": "t1"}) == "已删除 t1：甲"
    assert [item.id for item in realm.todos()] == ["t2"]
    # 删掉的 id 不复用：下一条是 t3
    assert await todo.ainvoke({"action": "add", "text": "丙"}) == "已添加 t3 [pending] 丙"
    assert json.loads(state_file(tmp_path).read_text(encoding="utf-8"))["next_id"] == 4


async def test_list_hides_finished_by_default(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    listing = tools(realm)["todo_list"]
    await todo.ainvoke({"action": "add", "text": "还在做"})
    await todo.ainvoke({"action": "add", "text": "做完了"})
    await todo.ainvoke({"action": "update", "id": "t2", "status": "finished"})

    default_out = listing.invoke({})
    assert "- t1 [pending] 还在做" in default_out
    assert "t2" not in default_out
    assert "未完成 1 条" in default_out and "已完成 1 条" in default_out

    full = listing.invoke({"include_finished": True})
    assert "- t1 [pending] 还在做" in full
    assert "- t2 [finished] 做完了" in full


async def test_list_order_is_processing_pending_blocked_finished(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    listing = tools(realm)["todo_list"]
    for text in ("待做", "在做", "卡住", "做完"):
        await todo.ainvoke({"action": "add", "text": text})
    await todo.ainvoke({"action": "update", "id": "t2", "status": "processing"})
    await todo.ainvoke({"action": "update", "id": "t3", "status": "blocked"})
    await todo.ainvoke({"action": "update", "id": "t4", "status": "finished"})

    ids = [line.split()[1] for line in listing.invoke({"include_finished": True}).splitlines() if line.startswith("- ")]
    assert ids == ["t2", "t1", "t3", "t4"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"action": "add", "text": "  "}, "错误：add 需要非空 text"),
        ({"action": "add", "text": "x", "status": "bogus"}, "错误：未知状态 'bogus'"),
        ({"action": "update", "id": "t9", "text": "x"}, "错误：没有 id 为 't9' 的待办"),
        ({"action": "update", "id": "t1"}, "错误：update 至少要给 text 或 status"),
        ({"action": "remove"}, "错误：remove 需要 id"),
        ({"action": "rename", "id": "t1"}, "错误：未知 action 'rename'"),
    ],
)
async def test_bad_input_returns_error_text(
    tmp_path: Path, payload: dict[str, Any], expected: str
) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "占位"})
    out = await todo.ainvoke(payload)
    assert out.startswith("错误：")
    assert expected in out


async def test_restart_keeps_items_and_next_id(tmp_path: Path) -> None:
    realm, ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "甲"})
    await todo.ainvoke({"action": "add", "text": "乙"})
    await realm.stop(ctx)

    again, _ctx = await started(tmp_path)
    assert [item.text for item in again.todos()] == ["甲", "乙"]
    assert await tools(again)["todo"].ainvoke({"action": "add", "text": "丙"}) == (
        "已添加 t3 [pending] 丙"
    )


async def test_corrupted_state_fails_start(tmp_path: Path) -> None:
    path = state_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": [{"id": "t1", "text": "x", "state": "zzz"}]}), "utf-8")

    realm = TodoRealm()
    ctx = RealmContext(
        realm_id="todo",
        vfs=None,  # type: ignore[arg-type]
        state=JsonStateStore(path, realm_id="todo"),
    )
    ctx.state.load()
    with pytest.raises(ValueError, match="未知状态"):
        await realm.start(ctx)


# ---------------------------------------------------------------------------
# 第五拍
# ---------------------------------------------------------------------------


async def test_context_is_silent_when_nothing_actionable(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    turn = TurnInfo(index=50, id="agent#50")
    assert realm.context_items(turn) is None  # 空清单

    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "做完了"})
    await todo.ainvoke({"action": "update", "id": "t1", "status": "finished"})
    assert realm.context_items(turn) is None  # 全完成

    await todo.ainvoke({"action": "add", "text": "卡住"})
    await todo.ainvoke({"action": "update", "id": "t2", "status": "blocked"})
    assert realm.context_items(turn) is None  # 只剩 blocked


async def test_context_injects_open_items_with_reminder(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "写周报"})
    await todo.ainvoke({"action": "add", "text": "回邮件"})
    await todo.ainvoke({"action": "add", "text": "等审批"})
    await todo.ainvoke({"action": "add", "text": "已完成"})
    await todo.ainvoke({"action": "update", "id": "t2", "status": "processing"})
    await todo.ainvoke({"action": "update", "id": "t3", "status": "blocked"})
    await todo.ainvoke({"action": "update", "id": "t4", "status": "finished"})

    item = realm.context_items(TurnInfo(index=50, id="agent#50"))
    assert item is not None
    assert item.source == "realm:todo"
    assert item.meta == {"open": 3, "ids": ["t2", "t1", "t3"]}
    assert "待办提醒：3 条未完成" in item.text
    assert "- [processing] t2 回邮件" in item.text
    assert "- [pending] t1 写周报" in item.text
    assert "- [blocked] t3 等审批" in item.text
    assert "t4" not in item.text
    assert "做完 → finished" in item.text  # 提醒 AI 按状态更新


async def test_context_rendering_is_stable(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    await tools(realm)["todo"].ainvoke({"action": "add", "text": "甲"})
    item = realm.context_items(TurnInfo(index=1, id="agent#1"))
    assert item is not None
    assert item.text == render_context(realm.todos(include_finished=True))


# ---------------------------------------------------------------------------
# 合同属性
# ---------------------------------------------------------------------------


async def test_prompt_fragment_mentions_both_tools_and_states(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    fragment = realm.prompt_fragment()
    assert "`todo_list`" in fragment
    assert "`todo(" in fragment
    for state in STATES:
        assert state in fragment
    assert "realm:todo" in fragment


def test_class_contract() -> None:
    realm = TodoRealm()
    assert realm.id == "todo"
    assert realm.state_scope == "agent"
    assert realm.context_every == 50
    assert realm.context_first is True
    assert realm.mounts() == {}
    assert realm.requires == []


async def test_counts_and_summary_line(tmp_path: Path) -> None:
    realm, _ctx = await started(tmp_path)
    todo = tools(realm)["todo"]
    await todo.ainvoke({"action": "add", "text": "甲"})
    await todo.ainvoke({"action": "add", "text": "乙"})
    await todo.ainvoke({"action": "update", "id": "t2", "status": "processing"})

    assert realm.counts() == {
        "pending": 1,
        "processing": 1,
        "blocked": 0,
        "finished": 0,
    }
    line = realm.summary_line()
    assert "未完成 2 条" in line and "processing 1" in line
