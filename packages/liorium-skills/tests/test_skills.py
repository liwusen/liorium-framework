"""``liorium.skills``：SKILL.md 发现 + 挂载树 + prompt 技能清单。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from liorium.agent.realm.base import JsonStateStore
from liorium.skills import SkillsRealm, catalog_signature, discover_skills, split_frontmatter
from liorium.skills.frontmatter import read_skill_meta
from liorium.vfs import VirtualFileSystem


def write_skill(root: Path, slug: str, *, frontmatter: str = "", body: str = "# 用法") -> Path:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n{body}\n" if frontmatter else f"{body}\n",
        encoding="utf-8",
        newline="\n",
    )
    return skill_dir


def make_ctx(tmp_path: Path, *, inbox: Path | None = None):
    state = JsonStateStore(tmp_path / "realm_state" / "skills.json", realm_id="skills")
    state.load()
    return SimpleNamespace(
        state=state,
        inbox_dir=inbox or tmp_path,
        params={},
        conversation_id="main",
        agent_id="test",
    )


# ---------------------------------------------------------------------------
# frontmatter / 发现
# ---------------------------------------------------------------------------


def test_split_frontmatter_reads_block_and_body() -> None:
    front, body = split_frontmatter("---\nname: 天气\ndescription: 查天气\n---\n# 用法\n")
    assert front == {"name": "天气", "description": "查天气"}
    assert body.strip() == "# 用法"


def test_split_frontmatter_without_block_returns_text() -> None:
    assert split_frontmatter("# 就是正文") == ({}, "# 就是正文")


def test_read_skill_meta_prefers_meta_json(tmp_path: Path) -> None:
    skill_dir = write_skill(tmp_path, "demo", frontmatter="version: 9.9.9")
    (skill_dir / "_meta.json").write_text(
        json.dumps({"slug": "demo", "version": "1.2.3", "description": "来自 meta"}),
        encoding="utf-8",
    )
    meta = read_skill_meta(skill_dir)
    assert meta["version"] == "1.2.3"
    assert meta["description"] == "来自 meta"


def test_discover_skills_skips_non_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", frontmatter="name: Alpha\ndescription: 甲的说明")
    (tmp_path / "not-a-skill").mkdir()
    (tmp_path / "not-a-skill" / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "_hidden").mkdir()
    (tmp_path / "_hidden" / "SKILL.md").write_text("x", encoding="utf-8")

    skills = discover_skills(tmp_path)
    assert [s.slug for s in skills] == ["alpha"]
    skill = skills[0]
    assert skill.name == "Alpha"
    assert skill.description == "甲的说明"
    assert skill.uri == "skills://alpha/SKILL.md"


def test_discover_skills_uses_first_line_when_no_description(tmp_path: Path) -> None:
    write_skill(tmp_path, "beta", body="# 标题\n\n做点有用的事。")
    skill = discover_skills(tmp_path)[0]
    assert skill.description == "做点有用的事。"


def test_discover_skills_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert discover_skills(tmp_path / "ghost") == []
    assert catalog_signature(tmp_path / "ghost") == ()


# ---------------------------------------------------------------------------
# realm：挂载树 / 工具 / prompt / dirty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_mounts_skill_tree(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    write_skill(skills_root, "weather", frontmatter="description: 查天气")
    (skills_root / "weather" / "utils").mkdir()
    (skills_root / "weather" / "utils" / "parse.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8", newline="\n"
    )

    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))

    mounts = realm.mounts()
    assert set(mounts) == {"/skills"}
    mount = mounts["/skills"]
    assert mount.readonly is True

    parent = VirtualFileSystem(owner_id="agent")
    parent.mount(mount)
    assert await parent.read_text("/skills/weather/SKILL.md") == (
        "---\ndescription: 查天气\n---\n# 用法\n"
    )
    assert await parent.read_text("/skills/weather/utils/parse.py") == "def f():\n    return 1\n"
    node = await parent.stat("/skills/weather")
    assert node is not None and node.description == "查天气"

    # 只读：写被拒
    from liorium.core.errors import VfsReadOnlyError
    from liorium.core.types import Text

    with pytest.raises(VfsReadOnlyError):
        await parent.write("/skills/weather/SKILL.md", Text("篡改"))


@pytest.mark.asyncio
async def test_empty_skills_dir_has_no_prompt_fragment(tmp_path: Path) -> None:
    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))
    assert realm.prompt_fragment() == ""
    assert realm.skills() == []


@pytest.mark.asyncio
async def test_prompt_fragment_and_skill_listing(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    write_skill(skills_root, "weather", frontmatter="description: 查天气")
    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))

    fragment = realm.prompt_fragment()
    assert "skills://" in fragment and "SKILL.md" in fragment
    listing = realm.skills()
    assert listing == ["weather：查天气（`skills://weather/SKILL.md`）"]


@pytest.mark.asyncio
async def test_skill_listing_is_capped(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    for index in range(5):
        write_skill(skills_root, f"s{index}", frontmatter=f"description: 说明{index}")
    realm = SkillsRealm(SimpleNamespace(params={"max_prompt_skills": 2}))
    await realm.start(make_ctx(tmp_path))
    listing = realm.skills()
    assert listing[0].startswith("s0：")
    assert "另有 3 个技能" in listing[-1]


@pytest.mark.asyncio
async def test_list_skills_tool_returns_json(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    write_skill(skills_root, "weather", frontmatter="version: 1.1.0\ndescription: 查天气")
    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))

    tools = realm.tools()
    assert [t.name for t in tools] == ["list_skills"]
    payload = json.loads(await tools[0].ainvoke({}))
    assert payload["skills"][0]["slug"] == "weather"
    assert payload["skills"][0]["version"] == "1.1.0"
    assert payload["skills"][0]["skill_md"] == "skills://weather/SKILL.md"


@pytest.mark.asyncio
async def test_dirty_tracks_installed_and_removed_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    write_skill(skills_root, "one", frontmatter="description: 一")
    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))
    assert realm.dirty() is False

    write_skill(skills_root, "two", frontmatter="description: 二")
    assert realm.dirty() is True
    # 重挂载后 dirty 清掉，新技能立刻可见
    mounts = realm.mounts()
    parent = VirtualFileSystem(owner_id="agent")
    parent.mount(mounts["/skills"])
    assert await parent.read_text("/skills/two/SKILL.md")
    realm.clear_dirty()
    assert realm.dirty() is False

    import shutil

    shutil.rmtree(skills_root / "two")
    assert realm.dirty() is True
    realm.mark_dirty()
    assert realm.dirty() is True
    realm.clear_dirty()
    assert realm.dirty() is False


@pytest.mark.asyncio
async def test_start_records_state(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    write_skill(skills_root, "one")
    ctx = make_ctx(tmp_path)
    realm = SkillsRealm()
    await realm.start(ctx)
    assert await ctx.state.get("skills") == ["one"]
    assert await ctx.state.get("count") == 1
    assert (tmp_path / "skill.d") == Path(await ctx.state.get("root"))


@pytest.mark.asyncio
async def test_symlink_escape_is_not_mounted(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    skill_dir = write_skill(skills_root, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")

    try:
        (skill_dir / "keys").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建符号链接")

    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))
    try:
        mount = realm.mounts()["/skills"]
        names = [node.name for node in await mount.vfs.list("/alpha")]
        assert "keys" not in names
        from liorium.core.errors import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            await mount.vfs.read("/alpha/keys/secret.txt")
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_skill_file_larger_than_limit_is_truncated(tmp_path: Path) -> None:
    skills_root = tmp_path / "skill.d"
    skill_dir = write_skill(skills_root, "alpha")
    (skill_dir / "big.txt").write_text("x" * 300_000, encoding="utf-8")

    realm = SkillsRealm()
    await realm.start(make_ctx(tmp_path))
    try:
        content = await realm.mounts()["/skills"].vfs.read("/alpha/big.txt")
        assert content is not None
        assert "已截断" in content.text
        assert len(content.text) < 300_000
    finally:
        await realm.stop(make_ctx(tmp_path))


@pytest.mark.asyncio
async def test_explicit_skills_dir_param(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    write_skill(custom, "custom", frontmatter="description: 自定义目录")
    realm = SkillsRealm(SimpleNamespace(params={"skills_dir": "elsewhere"}))
    await realm.start(make_ctx(tmp_path))
    assert realm.skills() == ["custom：自定义目录（`skills://custom/SKILL.md`）"]
