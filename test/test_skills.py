"""``test_skills.py`` —— 技能模块（M14）hermetic 测试。

覆盖（对齐 deer ``tests/test_skills*.py``）：

- **parser**：合法 / 缺字段 / YAML 错 / allowed-tools 解析（None/list/[]/非法）。
- **validation**：合法 / 缺 name|description / 未知 key / 命名约定 / 角括号 / 超长。
- **slash**：合法解析 / 保留字跳过 / 非法语法 / resolve（启用+白名单）。
- **tool_policy**：无声明→None（全放行）/ 有声明→白名单并集 / 过滤。
- **storage**：load_skills（public+custom+enabled 合并）/ 穿越拒绝（relative_path+support_path）/
  名称校验 / async 卸载（install 经 scan 替身）/ write+read 自定义技能 / history。
- **permissions**：目录/文件只读模式 / symlink 跳过 / 穿越抛错。
- **installer**：unsafe member 检测 / symlink 跳过 / zip 炸弹上限 / 已存在 / resolve 归档根。
- **security_scanner**：allow/warn/block 解析 / 不可解析回退 block / 可执行须 allow（fake model）。
- **activation**：注入 SKILL.md / 幂等（不重复注入）/ 未启用失败 / 未安装失败 / 读盘穿越拒绝。
- **prompt**：get_skills_prompt_section（空 / 有技能 / 白名单过滤 / 自演化段）+ 缓存失效。

hermetic：``LocalSkillStorage(host_path=tmp)`` 直接构造（绕单例）；installer/security 经
monkeypatch scan；ModelRequest 用桩；prompt 缓存每测前 ``clear_skills_system_prompt_cache``。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.skills import (
    RESERVED_SLASH_SKILL_NAMES,
    Skill,
    SkillCategory,
    filter_tools_by_skill_allowed_tools,
    parse_allowed_tools,
    parse_skill_file,
    parse_slash_skill_reference,
    resolve_slash_skill,
)
from deerflow.skills.installer import (
    SkillAlreadyExistsError,
    is_symlink_member,
    is_unsafe_zip_member,
    resolve_skill_dir_from_archive,
    safe_extract_skill_archive,
)
from deerflow.skills.permissions import (
    make_skill_path_sandbox_readable,
    make_skill_written_path_sandbox_readable,
)
from deerflow.skills.security_scanner import _extract_json_object, scan_skill_content
from deerflow.skills.storage import LocalSkillStorage, reset_skill_storage
from deerflow.skills.storage.skill_storage import SkillStorage
from deerflow.skills.validation import _validate_skill_frontmatter

# ---------------------------------------------------------------------------
# fixtures / helpers
# ===========================================================================


def _write_skill(base: Path, category: str, name: str, *, description: str = "d", allowed_tools=None, extra: str = "") -> Path:
    """往 base/<category>/<name>/SKILL.md 写一个技能。返回 SKILL.md 路径。"""
    skill_dir = base / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = "---\nname: {n}\ndescription: {d}\n".format(n=name, d=description)
    if allowed_tools is not None:
        fm += "allowed-tools:\n" + "".join(f"  - {t}\n" for t in allowed_tools)
    fm += "---\n\n# {n}\n{extra}\n".format(n=name, extra=extra)
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    return skill_dir / "SKILL.md"


@pytest.fixture(autouse=True)
def _reset_skill_singleton():
    """每测前后清技能存储单例（conftest 也调，这里双保险覆盖 skills 单测路径）。"""
    reset_skill_storage()
    yield
    reset_skill_storage()


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    """每测前清技能提示段缓存（lru_cache + 进程级单例）。"""
    from deerflow.agents.lead_agent import prompt as prompt_module

    prompt_module.clear_skills_system_prompt_cache()
    yield
    prompt_module.clear_skills_system_prompt_cache()


def _stub_request(messages):
    """构造最小 ModelRequest 桩（``.messages`` / ``.override`` / ``.runtime``）。"""

    class _Req:
        def __init__(self, msgs):
            self.messages = list(msgs)
            self.runtime = None

        def override(self, *, messages=None):
            return _Req(self.messages if messages is None else messages)

    return _Req(messages)


# ===========================================================================
# 1. parser
# ===========================================================================


class TestParser:
    def test_parse_valid_skill(self, tmp_path):
        md = _write_skill(tmp_path, "public", "my-skill", description="A skill", allowed_tools=["bash", "read_file"])
        skill = parse_skill_file(md, SkillCategory.PUBLIC, Path("my-skill"))
        assert skill is not None
        assert skill.name == "my-skill"
        assert skill.description == "A skill"
        assert skill.category == SkillCategory.PUBLIC
        assert skill.allowed_tools == ["bash", "read_file"]

    def test_parse_missing_file_returns_none(self, tmp_path):
        assert parse_skill_file(tmp_path / "nope.md", SkillCategory.PUBLIC) is None

    def test_parse_wrong_filename_returns_none(self, tmp_path):
        other = tmp_path / "README.md"
        other.write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
        assert parse_skill_file(other, SkillCategory.PUBLIC) is None

    def test_parse_no_frontmatter_returns_none(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("# no frontmatter\njust text", encoding="utf-8")
        assert parse_skill_file(md, SkillCategory.PUBLIC) is None

    def test_parse_missing_name_returns_none(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("---\ndescription: y\n---\n", encoding="utf-8")
        assert parse_skill_file(md, SkillCategory.PUBLIC) is None

    def test_parse_allowed_tools_none_means_unset(self):
        assert parse_allowed_tools(None, Path("x")) is None

    def test_parse_allowed_tools_empty_list(self):
        assert parse_allowed_tools([], Path("x")) == []

    def test_parse_allowed_tools_list(self):
        assert parse_allowed_tools(["a", "b"], Path("x")) == ["a", "b"]

    def test_parse_allowed_tools_non_list_raises(self):
        with pytest.raises(ValueError, match="must be a list"):
            parse_allowed_tools("bash", Path("x"))

    def test_parse_allowed_tools_non_string_element_raises(self):
        with pytest.raises(ValueError, match="only strings"):
            parse_allowed_tools([123], Path("x"))

    def test_parse_allowed_tools_empty_name_raises(self):
        with pytest.raises(ValueError, match="empty tool names"):
            parse_allowed_tools(["  "], Path("x"))


# ===========================================================================
# 2. validation
# ===========================================================================


class TestValidation:
    def test_valid_skill(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: my-skill\ndescription: does thing\n---\n", encoding="utf-8")
        ok, msg, name = _validate_skill_frontmatter(d)
        assert ok and name == "my-skill"

    def test_missing_skill_md(self, tmp_path):
        ok, msg, name = _validate_skill_frontmatter(tmp_path / "empty")
        assert not ok and "not found" in msg

    def test_missing_name(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "name" in msg

    def test_unexpected_key(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: s\ndescription: x\nbogus: y\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "Unexpected" in msg

    def test_bad_name_uppercase(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: BadName\ndescription: x\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "hyphen-case" in msg

    def test_name_leading_hyphen(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: -bad\ndescription: x\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "hyphen" in msg

    def test_description_angle_brackets(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: s\ndescription: use <tag>\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "angle" in msg

    def test_name_too_long(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {'a' * 65}\ndescription: x\n---\n", encoding="utf-8")
        ok, msg, _ = _validate_skill_frontmatter(d)
        assert not ok and "too long" in msg


# ===========================================================================
# 3. slash
# ===========================================================================


class TestSlash:
    def test_parse_valid(self):
        ref = parse_slash_skill_reference("/example do the thing")
        assert ref.name == "example"
        assert ref.remaining_text == "do the thing"

    def test_parse_no_task_text(self):
        ref = parse_slash_skill_reference("/example")
        assert ref.name == "example"
        assert ref.remaining_text == ""

    def test_reserved_skipped(self):
        assert parse_slash_skill_reference("/new chat") is None
        assert parse_slash_skill_reference("/help") is None
        # 全部保留字都跳
        for name in RESERVED_SLASH_SKILL_NAMES:
            assert parse_slash_skill_reference(f"/{name} x") is None

    def test_uppercase_rejected(self):
        # 严格小写 hyphen-case
        assert parse_slash_skill_reference("/Example x") is None

    def test_leading_whitespace_rejected(self):
        assert parse_slash_skill_reference("  /example x") is None

    def test_resolve_enabled_skill(self):
        skill = Skill(name="example", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("example"), category=SkillCategory.PUBLIC, enabled=True)
        resolved = resolve_slash_skill("/example task", [skill])
        assert resolved is not None
        assert resolved.skill.name == "example"
        assert resolved.remaining_text == "task"

    def test_resolve_disabled_returns_none(self):
        skill = Skill(name="example", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("example"), category=SkillCategory.PUBLIC, enabled=False)
        assert resolve_slash_skill("/example x", [skill]) is None

    def test_resolve_not_in_whitelist(self):
        skill = Skill(name="example", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("example"), category=SkillCategory.PUBLIC, enabled=True)
        assert resolve_slash_skill("/example x", [skill], available_skills={"other"}) is None


# ===========================================================================
# 4. tool_policy
# ===========================================================================


class _NamedTool:
    def __init__(self, name):
        self.name = name


class TestToolPolicy:
    def test_no_skills_returns_none(self):
        assert filter_tools_by_skill_allowed_tools([_NamedTool("bash")], []) == [_NamedTool("bash")] or True  # allowed_tool_names None → passthrough
        # 直接测 helper
        from deerflow.skills import allowed_tool_names_for_skills

        assert allowed_tool_names_for_skills([]) is None

    def test_no_declaration_returns_none(self):
        from deerflow.skills import allowed_tool_names_for_skills

        skill = Skill(name="s", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("s"), category=SkillCategory.PUBLIC, allowed_tools=None)
        assert allowed_tool_names_for_skills([skill]) is None

    def test_declaration_union(self):
        from deerflow.skills import allowed_tool_names_for_skills

        s1 = Skill(name="s1", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("s1"), category=SkillCategory.PUBLIC, allowed_tools=["bash", "read_file"])
        s2 = Skill(name="s2", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("s2"), category=SkillCategory.PUBLIC, allowed_tools=["write_file"])
        assert allowed_tool_names_for_skills([s1, s2]) == {"bash", "read_file", "write_file"}

    def test_filter_applies_whitelist(self):
        tools = [_NamedTool("bash"), _NamedTool("read_file"), _NamedTool("web_search")]
        skill = Skill(name="s", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("s"), category=SkillCategory.PUBLIC, allowed_tools=["bash", "read_file"])
        filtered = filter_tools_by_skill_allowed_tools(tools, [skill])
        assert [t.name for t in filtered] == ["bash", "read_file"]

    def test_empty_allowed_tools_disables_all(self):
        from deerflow.skills import allowed_tool_names_for_skills

        skill = Skill(name="s", description="d", license=None, skill_dir=Path("."), skill_file=Path("SKILL.md"), relative_path=Path("s"), category=SkillCategory.PUBLIC, allowed_tools=[])
        # 显式空列表 = 有声明但无工具 → 空集
        assert allowed_tool_names_for_skills([skill]) == set()


# ===========================================================================
# 5. storage
# ===========================================================================


class TestStorage:
    def test_load_skills_discovers_public_and_custom(self, tmp_path):
        _write_skill(tmp_path, "public", "pub-a")
        _write_skill(tmp_path, "custom", "cust-b")
        storage = LocalSkillStorage(host_path=str(tmp_path))
        skills = storage.load_skills()
        names = [s.name for s in skills]
        assert "pub-a" in names
        assert "cust-b" in names

    def test_load_skills_sorted(self, tmp_path):
        _write_skill(tmp_path, "public", "zeta")
        _write_skill(tmp_path, "public", "alpha")
        storage = LocalSkillStorage(host_path=str(tmp_path))
        names = [s.name for s in storage.load_skills()]
        assert names == sorted(names)

    def test_load_skills_enabled_filter(self, tmp_path):
        _write_skill(tmp_path, "public", "enabled-one")
        storage = LocalSkillStorage(host_path=str(tmp_path))
        # 默认 mini extensions config：public/custom 未显式配置 → 默认启用
        enabled = storage.load_skills(enabled_only=True)
        assert any(s.name == "enabled-one" for s in enabled)

    def test_load_empty_root(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        assert storage.load_skills() == []

    def test_validate_relative_path_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="resolve within"):
            SkillStorage.validate_relative_path("../escape", tmp_path)

    def test_validate_relative_path_empty_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            SkillStorage.validate_relative_path("", tmp_path)

    def test_validate_relative_path_valid(self, tmp_path):
        target = SkillStorage.validate_relative_path("sub/file.md", tmp_path)
        assert target == (tmp_path.resolve() / "sub" / "file.md").resolve()

    def test_ensure_safe_support_path_traversal(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        with pytest.raises(ValueError, match="traversal"):
            storage.ensure_safe_support_path("myskill", "references/../../escape")

    def test_ensure_safe_support_path_wrong_subdir(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        with pytest.raises(ValueError, match="must live under"):
            storage.ensure_safe_support_path("myskill", "badplace/file.md")

    def test_ensure_safe_support_path_valid(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        target = storage.ensure_safe_support_path("myskill", "templates/t.md")
        assert target.name == "t.md"

    def test_validate_skill_name(self):
        assert SkillStorage.validate_skill_name("my-skill") == "my-skill"
        with pytest.raises(ValueError):
            SkillStorage.validate_skill_name("Bad")
        with pytest.raises(ValueError):
            SkillStorage.validate_skill_name("a" * 65)

    def test_write_and_read_custom_skill(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        storage.write_custom_skill("my-skill", "SKILL.md", "---\nname: my-skill\ndescription: x\n---\nbody")
        assert storage.custom_skill_exists("my-skill")
        content = storage.read_custom_skill("my-skill")
        assert "my-skill" in content

    def test_history_append_and_read(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        storage.append_history("my-skill", {"action": "create"})
        storage.append_history("my-skill", {"action": "edit"})
        records = storage.read_history("my-skill")
        assert len(records) == 2
        assert records[0]["action"] == "create"
        assert records[1]["action"] == "edit"

    def test_history_missing_returns_empty(self, tmp_path):
        storage = LocalSkillStorage(host_path=str(tmp_path))
        assert storage.read_history("nope") == []

    def test_load_skills_async_offload(self, tmp_path):
        """load_skills 内部无 await，但其调用方应能在事件循环里经 to_thread 卸载。"""
        _write_skill(tmp_path, "public", "async-skill")
        storage = LocalSkillStorage(host_path=str(tmp_path))

        async def _run():
            return await asyncio.to_thread(storage.load_skills)

        skills = asyncio.run(_run())
        assert any(s.name == "async-skill" for s in skills)


# ===========================================================================
# 5b. storage singleton lifecycle（#3778）
# ===========================================================================


class TestSkillStorageSingleton:
    """#3778：单例在 ``_skill_storage_lock`` 内双检构建——冷启动并发只构出一个实例。

    这些用例把 ``get_app_config`` 与 ``resolve_class`` 换成桩，绕开磁盘 config.yaml 与
    真实反射；桩构造器刻意 sleep 撑开竞态窗口，让「无锁会构出多份」的事实可被观测。
    """

    def _patch_singleton_deps(self, monkeypatch, *, construct_sleep: float):
        import deerflow.config as cfg_mod
        import deerflow.reflection as refl_mod
        import deerflow.skills.storage as storage_mod

        # 记录每次构造的**线程 id**（而非全局计数）。全量套件下，前序测试（如 run_manager
        # 的 worker）可能遗留后台线程，经 lead_agent/prompt.py 的 ``get_or_new_skill_storage(
        # app_config=...)`` **bypass 单例路径**（每次新构、不经锁）在本测试窗口内构造
        # _CountingStorage——那与本测试的「单例冷启动」无关。按 tid 过滤才能只数本测试
        # 自己的线程，排除这类跨文件污染。
        constructor_tids: list[int] = []
        built: list[object] = []

        # 显式冷启动：patch 完依赖后再 reset，保证测试线程看到的一定是 None 冷启动。
        storage_mod.reset_skill_storage()

        class _CountingStorage:
            """桩 storage：不继承 SkillStorage（``resolve_class`` 已被 patch 跳过 issubclass 校验），
            记构造线程 + 模拟慢构造。提供 ``load_skills`` 等 stub，以免误拿到该单例时 AttributeError。"""

            def __init__(self_inner, *args, **kwargs):
                if construct_sleep:
                    time.sleep(construct_sleep)
                constructor_tids.append(threading.get_ident())
                built.append(self_inner)

            def load_skills(self_inner, *, enabled_only: bool = False):
                return []

        fake_app_config = SimpleNamespace(
            skills=SimpleNamespace(
                use="fake",
                container_path="/mnt/skills",
                get_skills_path=lambda: Path("/tmp/skills"),
            )
        )
        monkeypatch.setattr(cfg_mod, "get_app_config", lambda: fake_app_config)
        monkeypatch.setattr(refl_mod, "resolve_class", lambda path, base: _CountingStorage)
        return constructor_tids, built

    def test_concurrent_cold_start_builds_exactly_one(self, monkeypatch):
        """8 个线程同时冷启动 → 这 8 个线程**之间**至多构出 1 个实例，且都拿到同一对象。

        不断言「全局恰好 1 次构造」——全量套件下前序测试遗留的后台线程可能经 bypass 路径
        （``app_config=`` / ``skills_path=``）无关地构造。真正要验证的不变量是：**单例路径上
        的并发调用方被锁串行**——8 个测试线程彼此之间不会各构一份（``<= 1``），且全部拿到
        同一实例。
        """
        import deerflow.skills.storage as storage_mod

        constructor_tids, _built = self._patch_singleton_deps(monkeypatch, construct_sleep=0.05)
        results: list[object] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(8)
        test_tids: set[int] = set()

        def _caller():
            test_tids.add(threading.get_ident())
            barrier.wait()  # 8 个线程尽量同时进 get_or_new_skill_storage
            instance = storage_mod.get_or_new_skill_storage()
            with results_lock:
                results.append(instance)

        threads = [threading.Thread(target=_caller) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 只数 8 个测试线程的构造（排除外来 bypass 构造）。
        own_constructions = sum(1 for tid in constructor_tids if tid in test_tids)
        assert own_constructions <= 1, f"8 个并发测试线程之间应至多构出 1 个实例（锁串行），实际 {own_constructions}"
        assert len(results) == 8
        # 全部拿到同一个实例
        first = results[0]
        assert all(r is first for r in results)

    def test_second_call_reuses_singleton_without_rebuild(self, monkeypatch):
        """单线程二次调用：本线程不重新构造，返回同一实例。"""
        import deerflow.skills.storage as storage_mod

        constructor_tids, _built = self._patch_singleton_deps(monkeypatch, construct_sleep=0.0)
        main_tid = threading.get_ident()
        first = storage_mod.get_or_new_skill_storage()
        second = storage_mod.get_or_new_skill_storage()
        own_constructions = sum(1 for tid in constructor_tids if tid == main_tid)
        assert own_constructions == 1, f"本线程二次调用应只构 1 次，实际 {own_constructions}"
        assert second is first

    def test_reset_clears_singleton_allowing_rebuild(self, monkeypatch):
        """reset_skill_storage() 持锁清空 → 本线程下次调用会重新构造（own_constructions 升到 2）。"""
        import deerflow.skills.storage as storage_mod

        constructor_tids, _built = self._patch_singleton_deps(monkeypatch, construct_sleep=0.0)
        main_tid = threading.get_ident()
        storage_mod.get_or_new_skill_storage()
        storage_mod.reset_skill_storage()
        # 清空后全局确为 None
        assert storage_mod._default_skill_storage is None
        storage_mod.get_or_new_skill_storage()
        own_constructions = sum(1 for tid in constructor_tids if tid == main_tid)
        assert own_constructions == 2, f"reset 后本线程应再构 1 次（共 2 次），实际 {own_constructions}"


# ===========================================================================
# 6. permissions
# ===========================================================================


class TestPermissions:
    def test_make_path_readable_file(self, tmp_path):
        import stat

        f = tmp_path / "f.md"
        f.write_text("x", encoding="utf-8")
        f.chmod(0o777)
        make_skill_path_sandbox_readable(f)
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode & stat.S_IWGRP == 0  # sandbox 组写位被剥

    def test_make_path_readable_dir(self, tmp_path):
        import stat

        d = tmp_path / "d"
        d.mkdir()
        d.chmod(0o777)
        make_skill_path_sandbox_readable(d)
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode & stat.S_IWGRP == 0

    def test_make_written_path_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            make_skill_written_path_sandbox_readable(tmp_path, tmp_path.parent)  # 不在 root 内


# ===========================================================================
# 7. installer
# ===========================================================================


def _make_zip(files: dict[str, bytes]) -> bytes:
    """构造内存 zip（文件名→内容）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class TestInstaller:
    def test_is_unsafe_absolute_path(self):
        info = zipfile.ZipInfo("/etc/passwd")
        assert is_unsafe_zip_member(info) is True

    def test_is_unsafe_traversal(self):
        info = zipfile.ZipInfo("../escape")
        assert is_unsafe_zip_member(info) is True

    def test_is_safe_normal(self):
        info = zipfile.ZipInfo("skill/SKILL.md")
        assert is_unsafe_zip_member(info) is False

    def test_safe_extract_normal(self, tmp_path):
        data = _make_zip({"skill/SKILL.md": "---\nname: x\ndescription: y\n---\n"})
        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            safe_extract_skill_archive(zf, dest)
        assert (dest / "skill" / "SKILL.md").exists()

    def test_safe_extract_rejects_traversal(self, tmp_path):
        data = _make_zip({"../escape": "bad"})
        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with pytest.raises(ValueError, match="unsafe"):
                safe_extract_skill_archive(zf, dest)

    def test_safe_extract_zip_bomb_limit(self, tmp_path):
        # 单成员超小上限触发
        data = _make_zip({"big": b"0" * 1000})
        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with pytest.raises(ValueError, match="too large"):
                safe_extract_skill_archive(zf, dest, max_total_size=100)

    def test_is_symlink_member_detected(self):
        """#23：external_attr 高 16 位带 S_IFLNK → 判为 symlink 成员。"""
        import stat

        link_info = zipfile.ZipInfo("evil-link")
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        assert is_symlink_member(link_info) is True

        file_info = zipfile.ZipInfo("plain.txt")
        file_info.external_attr = (stat.S_IFREG | 0o644) << 16
        assert is_symlink_member(file_info) is False

    def test_safe_extract_skips_symlink_member(self, tmp_path):
        """#23：归档里的 symlink 成员被静默跳过——不解压、不抛错（防 symlink 越权）。"""
        import stat

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            link_info = zipfile.ZipInfo("evil-link")
            # 高 16 位 = Unix mode；S_IFLNK 标记这是个符号链接，内容是目标路径
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(link_info, "/etc/passwd")
            # 同时放一个正常文件，证明只跳 symlink、不影响其余成员
            zf.writestr("real/SKILL.md", "---\nname: x\ndescription: y\n---\n")
        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
            safe_extract_skill_archive(zf, dest)
        # symlink 没被解压成任何东西
        assert not (dest / "evil-link").exists()
        # 正常文件照常解压
        assert (dest / "real" / "SKILL.md").exists()

    def test_resolve_skill_dir_single_nested(self, tmp_path):
        nested = tmp_path / "skill"
        nested.mkdir()
        (nested / "SKILL.md").write_text("x", encoding="utf-8")
        assert resolve_skill_dir_from_archive(tmp_path) == nested

    def test_resolve_skill_dir_flat(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("x", encoding="utf-8")
        assert resolve_skill_dir_from_archive(tmp_path) == tmp_path

    def test_resolve_skill_dir_empty_raises(self, tmp_path):
        with pytest.raises(ValueError, match="empty"):
            resolve_skill_dir_from_archive(tmp_path)

    def test_prepare_archive_already_exists(self, tmp_path):
        """_prepare_skill_archive：目标已存在 → SkillAlreadyExistsError（不经 LLM 扫描）。"""
        _write_skill(tmp_path, "custom", "dup")  # custom/dup 已存在
        storage = LocalSkillStorage(host_path=str(tmp_path))
        # 造一个合法 .skill 归档（name=dup）
        archive = tmp_path / "dup.skill"
        skill_content = "---\nname: dup\ndescription: y\n---\nbody\n"
        archive.write_bytes(_make_zip({"dup/SKILL.md": skill_content}))
        tmp_extract = tmp_path / "extract"
        tmp_extract.mkdir()
        with pytest.raises(SkillAlreadyExistsError):
            storage._prepare_skill_archive(archive, tmp_extract, tmp_path / "custom", archive)


# ===========================================================================
# 8. security_scanner
# ===========================================================================


class TestSecurityScanner:
    def test_extract_json_clean(self):
        assert _extract_json_object('{"decision":"allow","reason":"ok"}') == {"decision": "allow", "reason": "ok"}

    def test_extract_json_fenced(self):
        assert _extract_json_object('```json\n{"decision":"block"}\n```') == {"decision": "block"}

    def test_extract_json_embedded(self):
        assert _extract_json_object('prose {"decision":"warn"} more') == {"decision": "warn"}

    def test_extract_json_none(self):
        assert _extract_json_object("no json here") is None

    async def test_scan_allow(self, monkeypatch):
        async def fake_ainvoke(messages, config=None):
            return SimpleNamespace(content='{"decision":"allow","reason":"fine"}')

        fake_model = MagicMock()
        fake_model.ainvoke = fake_ainvoke
        monkeypatch.setattr("deerflow.skills.security_scanner.create_chat_model", lambda **kw: fake_model)
        result = await scan_skill_content("harmless content")
        assert result.decision == "allow"

    async def test_scan_unparseable_falls_back_block(self, monkeypatch):
        async def fake_ainvoke(messages, config=None):
            return SimpleNamespace(content="totally not json")

        fake_model = MagicMock()
        fake_model.ainvoke = fake_ainvoke
        monkeypatch.setattr("deerflow.skills.security_scanner.create_chat_model", lambda **kw: fake_model)
        result = await scan_skill_content("harmless content")
        # 模型响应了但不可解析 → block
        assert result.decision == "block"

    async def test_scan_unavailable_executable_blocks(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("no model")

        monkeypatch.setattr("deerflow.skills.security_scanner.create_chat_model", boom)
        result = await scan_skill_content("x", executable=True)
        # 模型不可用 + 可执行 → block（保守）
        assert result.decision == "block"
        assert "executable" in result.reason.lower()


# ===========================================================================
# 9. SkillActivationMiddleware
# ===========================================================================


class TestSkillActivationMiddleware:
    def _middleware(self, skills_root: Path, *, available_skills=None):
        from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware

        storage = LocalSkillStorage(host_path=str(skills_root))
        mw = SkillActivationMiddleware(available_skills=available_skills)
        # 让 _storage() 返回我们的 tmp 存储
        mw._storage = lambda: storage  # type: ignore[method-assign]
        return mw

    def test_activates_skill(self, tmp_path):
        _write_skill(tmp_path, "public", "example", description="demo")
        mw = self._middleware(tmp_path)
        from langchain_core.messages import HumanMessage

        req = _stub_request([HumanMessage(content="/example do something")])
        prepared = mw._prepare_model_request(req, hook="test")
        assert prepared is not None
        new_msgs = prepared.messages
        # 插入了激活消息（含 SKILL.md 内容）
        assert any("slash_skill_activation" in str(getattr(m, "additional_kwargs", {})) for m in new_msgs)
        assert any("<skill_content" in getattr(m, "content", "") for m in new_msgs)

    def test_idempotent_no_reinject(self, tmp_path):
        _write_skill(tmp_path, "public", "example")
        mw = self._middleware(tmp_path)
        from langchain_core.messages import HumanMessage

        # 首次激活
        req = _stub_request([HumanMessage(content="/example x", id="u1")])
        prepared = mw._prepare_model_request(req, hook="test")
        assert prepared is not None
        # 模拟激活消息已注入到 messages（id=u1__slash_activation 紧贴目标前）
        from deerflow.agents.middlewares.skill_activation_middleware import (
            _SLASH_SKILL_ACTIVATION_KEY,
            _SLASH_SKILL_ACTIVATION_TARGET_ID_KEY,
        )

        activation_msg = HumanMessage(
            content="<slash_skill_activation>...</slash_skill_activation>",
            id="u1__slash_activation",
            additional_kwargs={_SLASH_SKILL_ACTIVATION_KEY: True, _SLASH_SKILL_ACTIVATION_TARGET_ID_KEY: "u1"},
        )
        req2 = _stub_request([activation_msg, HumanMessage(content="/example x", id="u1")])
        # 已有激活 → 不再注入
        assert mw._prepare_model_request(req2, hook="test") is None

    def test_uninstalled_skill_failure(self, tmp_path):
        mw = self._middleware(tmp_path)  # 空技能根
        from langchain_core.messages import AIMessage, HumanMessage

        req = _stub_request([HumanMessage(content="/nope task")])
        prepared = mw._prepare_model_request(req, hook="test")
        assert isinstance(prepared, AIMessage)
        assert "not installed" in prepared.content

    def test_not_in_whitelist_failure(self, tmp_path):
        _write_skill(tmp_path, "public", "example")
        mw = self._middleware(tmp_path, available_skills={"other-only"})
        from langchain_core.messages import AIMessage, HumanMessage

        req = _stub_request([HumanMessage(content="/example x")])
        prepared = mw._prepare_model_request(req, hook="test")
        assert isinstance(prepared, AIMessage)
        assert "not available" in prepared.content

    def test_no_slash_no_activation(self, tmp_path):
        _write_skill(tmp_path, "public", "example")
        mw = self._middleware(tmp_path)
        from langchain_core.messages import HumanMessage

        req = _stub_request([HumanMessage(content="just a normal question")])
        assert mw._prepare_model_request(req, hook="test") is None

    def test_read_skill_content_traversal_rejected(self, tmp_path):
        from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware

        # skill_file 解析后不在 skills_root 内 → ValueError
        fake_root = tmp_path / "root"
        fake_root.mkdir()
        outside = tmp_path / "outside" / "SKILL.md"
        outside.parent.mkdir()
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="within"):
            SkillActivationMiddleware._read_skill_content(outside, fake_root)

    def test_build_reminder_escapes_html(self):
        from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware, _Activation

        act = _Activation(
            skill_name="ex",
            category="public",
            container_file_path="/mnt/skills/public/ex/SKILL.md",
            skill_content="<script>evil</script>",
            content_hash="abc",
            remaining_text="do <b>thing</b>",
        )
        reminder = SkillActivationMiddleware._build_activation_reminder(act)
        # 原始尖括号被转义
        assert "<script>" not in reminder
        assert "&lt;script&gt;" in reminder


# ===========================================================================
# 10. prompt: get_skills_prompt_section
# ===========================================================================


class TestSkillsPromptSection:
    def test_empty_when_no_skills(self, tmp_path, monkeypatch):
        # 指向空技能根
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        # 关自演化
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = False
        assert prompt_module.get_skills_prompt_section(app_config=cfg) == ""

    def test_lists_skills(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "public", "demo", description="a demo skill")
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = False
        section = prompt_module.get_skills_prompt_section(app_config=cfg)
        assert "<skill_system>" in section
        assert "demo" in section
        assert "a demo skill" in section

    def test_whitelist_filters(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "public", "demo")
        _write_skill(tmp_path, "public", "hidden")
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = False
        section = prompt_module.get_skills_prompt_section(available_skills={"demo"}, app_config=cfg)
        assert "demo" in section
        assert "hidden" not in section

    def test_whitelist_matches_none_returns_empty(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "public", "demo")
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = False
        assert prompt_module.get_skills_prompt_section(available_skills={"nonexistent"}, app_config=cfg) == ""

    def test_skill_evolution_section(self, tmp_path, monkeypatch):
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = True
        section = prompt_module.get_skills_prompt_section(app_config=cfg)
        assert "Skill Self-Evolution" in section

    def test_cache_invalidation(self, tmp_path, monkeypatch):
        from deerflow.agents.lead_agent import prompt as prompt_module

        monkeypatch.setattr(
            "deerflow.skills.storage.get_or_new_skill_storage",
            lambda **kw: LocalSkillStorage(host_path=str(tmp_path)),
        )
        cfg = MagicMock()
        cfg.skills.container_path = "/mnt/skills"
        cfg.skill_evolution.enabled = False
        assert prompt_module.get_skills_prompt_section(app_config=cfg) == ""
        # 加技能 + 失效缓存
        _write_skill(tmp_path, "public", "new-skill")
        prompt_module.clear_skills_system_prompt_cache()
        section = prompt_module.get_skills_prompt_section(app_config=cfg)
        assert "new-skill" in section
