"""``test_agents_config.py`` —— 自定义 agent 配置（M22 agents_config）hermetic 测试。

覆盖（对齐 deer ``tests/test_custom_agent.py``，mini 适配 DEER_FLOW_HOME 驱动 get_paths）：

- ``AGENT_NAME_PATTERN`` / ``validate_agent_name``：合法 / 非法边界（空 / 下划线 / 空格 /
  斜杠 / 点 / 穿越 / 非字符串 / None）。
- ``AgentConfig``（pydantic）：最小 / 全量 / dict 构造 / skills 三态（None 全部 / [] 无 / 白名单）。
- ``Paths`` agent 辅助方法：``agents_dir`` / ``agent_dir`` / ``user_agents_dir`` /
  ``user_agent_dir`` / ``user_dir`` + 名称小写归一。
- ``resolve_agent_dir``：两布局都不存在 → per-user 占位；per-user 优先；legacy 回退；
  **#3390**——per-user 仅 memory.json（无 config.yaml）不算 agent 目录，回退 legacy。
- ``load_agent_config``：合法 / 缺目录 / 缺 config.yaml / 推断 name / 剥未知字段 /
  YAML 解析错 / None→None / 非法名 / per-user 优先 + legacy 回退。
- ``load_agent_soul``：读 SOUL.md（strip）/ 缺失→None / 空白→None / 默认 agent 读 base_dir /
  per-user 优先 + legacy 回退。
- ``list_custom_agents``：空 / 多发现 / 跳过无 config.yaml / 跳过非目录 / 排序 /
  per-user 覆盖 legacy / 跳过非法名。

hermetic：``DEER_FLOW_HOME`` → ``tmp_path``，agent 目录建临时盘，不碰宿主真实用户数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deerflow.config.agents_config import (
    AGENT_NAME_PATTERN,
    SOUL_FILENAME,
    AgentConfig,
    list_custom_agents,
    load_agent_config,
    load_agent_soul,
    resolve_agent_dir,
    validate_agent_name,
)
from deerflow.config.paths import Paths, get_paths

# ---------------------------------------------------------------------------
# fixtures / helpers
# ===========================================================================


@pytest.fixture()
def home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``DEER_FLOW_HOME`` → 临时目录；返回 resolve 后的 base_dir，保证路径相等断言成立。

    resolve 在前：macOS 上 tmp_path 形如 ``/var/folders/...``，resolve 成
    ``/private/var/folders/...``；先把 home resolve 再设环境变量，
    ``runtime_home()`` 的二次 resolve 才与 home 完全相等。
    """
    home = (tmp_path / "deer-flow-home").resolve()
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    return home


def _write_legacy(base: Path, name: str, config: dict, *, soul: str | None = None) -> Path:
    """往 legacy 共享布局 ``{base}/agents/{name}/`` 写一个 agent。返回目录。"""
    agent_dir = base / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = dict(config)
    cfg.setdefault("name", name)
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if soul is not None:
        (agent_dir / SOUL_FILENAME).write_text(soul, encoding="utf-8")
    return agent_dir


def _write_per_user(base: Path, user_id: str, name: str, config: dict, *, soul: str | None = None) -> Path:
    """往 per-user 布局 ``{base}/users/{user_id}/agents/{name}/`` 写一个 agent。返回目录。"""
    agent_dir = base / "users" / user_id / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = dict(config)
    cfg.setdefault("name", name)
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if soul is not None:
        (agent_dir / SOUL_FILENAME).write_text(soul, encoding="utf-8")
    return agent_dir


# ---------------------------------------------------------------------------
# 1. AGENT_NAME_PATTERN / validate_agent_name
# ===========================================================================


class TestValidateAgentName:
    def test_none_returns_none(self):
        assert validate_agent_name(None) is None

    @pytest.mark.parametrize(
        "name",
        ["my-agent", "agent123", "A", "A-B-C", "CodeReviewer", "a-1-b-2", "-leading", "trailing-"],
    )
    def test_valid_names(self, name):
        assert validate_agent_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",  # 空（+ 至少一字符）
            "agent_name",  # 下划线
            "agent name",  # 空格
            "agent/name",  # 斜杠
            "agent.name",  # 点
            "agent@x",  # 特殊符号
            "..",  # 穿越
            "../etc",  # 穿越
            "中文",  # 非 ASCII
            "agent\n",  # 换行
        ],
    )
    def test_invalid_names_raise(self, name):
        with pytest.raises(ValueError, match="Invalid agent name"):
            validate_agent_name(name)

    @pytest.mark.parametrize("bad", [123, 1.5, [], {}, object()])
    def test_non_string_raises(self, bad):
        with pytest.raises(ValueError, match="Expected a string or None"):
            validate_agent_name(bad)  # type: ignore[arg-type]

    def test_pattern_constant(self):
        # 红线 #32：pattern 形态锁定
        assert AGENT_NAME_PATTERN.pattern == r"^[A-Za-z0-9-]+$"
        # fullmatch 语义：整串必须匹配
        assert AGENT_NAME_PATTERN.fullmatch("ok") is not None
        assert AGENT_NAME_PATTERN.fullmatch("ok\n") is None  # $ 不吃换行


# ---------------------------------------------------------------------------
# 2. AgentConfig（pydantic）
# ===========================================================================


class TestAgentConfig:
    def test_minimal_defaults(self):
        cfg = AgentConfig(name="my-agent")
        assert cfg.name == "my-agent"
        assert cfg.description == ""
        assert cfg.model is None
        assert cfg.tool_groups is None
        assert cfg.skills is None

    def test_full_config(self):
        cfg = AgentConfig(
            name="code-reviewer",
            description="Specialized for code review",
            model="deepseek-v3",
            tool_groups=["file:read", "bash"],
            skills=["review", "lint"],
        )
        assert cfg.name == "code-reviewer"
        assert cfg.description == "Specialized for code review"
        assert cfg.model == "deepseek-v3"
        assert cfg.tool_groups == ["file:read", "bash"]
        assert cfg.skills == ["review", "lint"]

    def test_from_dict(self):
        data = {"name": "test-agent", "description": "A test", "model": "gpt-4"}
        cfg = AgentConfig(**data)
        assert cfg.name == "test-agent"
        assert cfg.model == "gpt-4"
        assert cfg.tool_groups is None

    def test_skills_three_states(self):
        # None（缺省）= 全部；[] = 无；白名单 = 仅指定
        assert AgentConfig(name="a").skills is None
        assert AgentConfig(name="a", skills=[]).skills == []
        assert AgentConfig(name="a", skills=["x", "y"]).skills == ["x", "y"]

    def test_unknown_fields_rejected_by_pydantic(self):
        # BaseModel 默认 extra=ignore？pydantic v2 默认 ignore，但 load_agent_config
        # 会先剥未知字段，这里直接构造时 pydantic 默认丢弃未知键。
        cfg = AgentConfig(name="a", prompt_file="system.md")  # type: ignore[call-arg]
        assert not hasattr(cfg, "prompt_file")


# ---------------------------------------------------------------------------
# 3. Paths agent 辅助方法
# ===========================================================================


class TestPathsAgentHelpers:
    def test_agents_dir(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.agents_dir == tmp_path / "agents"

    def test_agent_dir_lowercases(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.agent_dir("code-reviewer") == tmp_path / "agents" / "code-reviewer"
        # 大写归一：防大小写碰撞（macOS APFS 默认大小写不敏感）
        assert paths.agent_dir("CodeReviewer") == tmp_path / "agents" / "codereviewer"

    def test_user_dir(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.user_dir("u1") == tmp_path / "users" / "u1"

    def test_user_agents_dir(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.user_agents_dir("u1") == tmp_path / "users" / "u1" / "agents"

    def test_user_agent_dir_lowercases(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.user_agent_dir("u1", "my-agent") == tmp_path / "users" / "u1" / "agents" / "my-agent"
        assert paths.user_agent_dir("u1", "UPPER") == tmp_path / "users" / "u1" / "agents" / "upper"

    def test_lowercasing_prevents_case_collision(self, tmp_path):
        # 同一 user 下 CodeReviewer 与 codereviewer 落进同一目录（防碰撞）
        paths = Paths(base_dir=tmp_path)
        assert paths.user_agent_dir("u1", "CodeReviewer") == paths.user_agent_dir("u1", "codereviewer")
        assert paths.agent_dir("Foo") == paths.agent_dir("foo")

    def test_legacy_and_per_user_are_distinct(self, tmp_path):
        paths = Paths(base_dir=tmp_path)
        assert paths.agent_dir("x") != paths.user_agent_dir("u1", "x")


# ---------------------------------------------------------------------------
# 4. resolve_agent_dir
# ===========================================================================


class TestResolveAgentDir:
    def test_neither_exists_returns_per_user_path(self, home_env):
        # 两布局都不存在 → 返回 per-user 占位（让调用方新建到新布局）
        result = resolve_agent_dir("new-agent", user_id="u1")
        assert result == home_env / "users" / "u1" / "agents" / "new-agent"

    def test_per_user_with_config_wins(self, home_env):
        _write_legacy(home_env, "my-agent", {"name": "my-agent"})
        _write_per_user(home_env, "u1", "my-agent", {"name": "my-agent", "model": "gpt-4"})
        result = resolve_agent_dir("my-agent", user_id="u1")
        assert result == home_env / "users" / "u1" / "agents" / "my-agent"

    def test_legacy_fallback_when_no_per_user(self, home_env):
        _write_legacy(home_env, "legacy-agent", {"name": "legacy-agent"})
        result = resolve_agent_dir("legacy-agent", user_id="u1")
        assert result == home_env / "agents" / "legacy-agent"

    def test_legacy_fallback_when_per_user_memory_only(self, home_env):
        """#3390：per-user 只有 memory.json（无 config.yaml）→ 不算 agent 目录，回退 legacy。

        首轮对话给某 agent 建的 per-user 目录往往只含 memory.json；若把它当 agent 目录，
        下一回合会读到「空配置」。要求 config.yaml 才认。
        """
        _write_legacy(home_env, "my-agent", {"name": "my-agent"})
        # per-user 目录只有 memory.json
        mem_dir = home_env / "users" / "u1" / "agents" / "my-agent"
        mem_dir.mkdir(parents=True)
        (mem_dir / "memory.json").write_text("{}", encoding="utf-8")

        result = resolve_agent_dir("my-agent", user_id="u1")
        assert result == home_env / "agents" / "my-agent"  # 回退 legacy

    def test_per_user_with_config_beats_memory_only_check(self, home_env):
        # per-user 同时有 config.yaml + memory.json → per-user 胜（已迁移）
        _write_legacy(home_env, "my-agent", {"name": "my-agent"})
        per_user = _write_per_user(home_env, "u1", "my-agent", {"name": "my-agent", "model": "gpt-4"})
        (per_user / "memory.json").write_text("{}", encoding="utf-8")

        result = resolve_agent_dir("my-agent", user_id="u1")
        assert result == per_user

    def test_per_user_dir_without_config_does_not_count(self, home_env):
        # per-user 目录存在但无 config.yaml，legacy 也没有 → 返回 per-user 占位
        # （不是 legacy：legacy 也不存在）
        d = home_env / "users" / "u1" / "agents" / "x"
        d.mkdir(parents=True)
        result = resolve_agent_dir("x", user_id="u1")
        assert result == d

    def test_default_user_id_from_context(self, home_env, monkeypatch):
        # 不传 user_id → 取 get_effective_user_id()（autouse 注入了 test-user-autouse）
        from deerflow.runtime.user_context import get_effective_user_id

        effective = get_effective_user_id()
        result = resolve_agent_dir("agent-x")
        assert result == home_env / "users" / effective / "agents" / "agent-x"

    def test_user_id_param_overrides_context(self, home_env):
        # 显式 user_id 优先于上下文
        result = resolve_agent_dir("agent-x", user_id="explicit-user")
        assert result == home_env / "users" / "explicit-user" / "agents" / "agent-x"


# ---------------------------------------------------------------------------
# 5. load_agent_config
# ===========================================================================


class TestLoadAgentConfig:
    def test_none_name_returns_none(self, home_env):
        assert load_agent_config(None, user_id="u1") is None

    def test_load_valid_config(self, home_env):
        _write_per_user(home_env, "u1", "code-reviewer", {"description": "Code review agent", "model": "deepseek-v3"})
        cfg = load_agent_config("code-reviewer", user_id="u1")
        assert cfg is not None
        assert cfg.name == "code-reviewer"
        assert cfg.description == "Code review agent"
        assert cfg.model == "deepseek-v3"

    def test_load_from_legacy_when_no_per_user(self, home_env):
        _write_legacy(home_env, "legacy", {"description": "legacy agent", "model": "gpt-4"})
        cfg = load_agent_config("legacy", user_id="u1")
        assert cfg is not None
        assert cfg.name == "legacy"
        assert cfg.model == "gpt-4"

    def test_per_user_priority_over_legacy(self, home_env):
        _write_legacy(home_env, "shared", {"description": "legacy", "model": "legacy-model"})
        _write_per_user(home_env, "u1", "shared", {"description": "mine", "model": "my-model"})
        cfg = load_agent_config("shared", user_id="u1")
        assert cfg.description == "mine"
        assert cfg.model == "my-model"

    def test_load_falls_back_when_user_dir_memory_only(self, home_env):
        """#3390 端到端：per-user 只 memory.json → load 回退 legacy 成功。"""
        _write_legacy(home_env, "my-agent", {"description": "Legacy agent", "model": "deepseek-v3"})
        mem_dir = home_env / "users" / "u1" / "agents" / "my-agent"
        mem_dir.mkdir(parents=True)
        (mem_dir / "memory.json").write_text("{}", encoding="utf-8")

        cfg = load_agent_config("my-agent", user_id="u1")
        assert cfg.model == "deepseek-v3"

    def test_missing_dir_raises(self, home_env):
        with pytest.raises(FileNotFoundError, match="Agent directory not found"):
            load_agent_config("nonexistent", user_id="u1")

    def test_missing_config_yaml_raises(self, home_env):
        # 目录存在但无 config.yaml
        (home_env / "users" / "u1" / "agents" / "broken").mkdir(parents=True)
        with pytest.raises(FileNotFoundError, match="Agent config not found"):
            load_agent_config("broken", user_id="u1")

    def test_infers_name_from_dir_when_absent(self, home_env):
        # config.yaml 没有 name 字段 → 用目录名兜底
        d = home_env / "users" / "u1" / "agents" / "inferred-name"
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("description: My agent\n", encoding="utf-8")
        cfg = load_agent_config("inferred-name", user_id="u1")
        assert cfg.name == "inferred-name"

    def test_name_in_config_wins_over_dir(self, home_env):
        # config.yaml 显式 name 保留原样（含大小写）
        _write_per_user(home_env, "u1", "dir-name", {"name": "ConfigName"})
        cfg = load_agent_config("dir-name", user_id="u1")
        assert cfg.name == "ConfigName"

    def test_strips_unknown_fields(self, home_env):
        # legacy prompt_file 等未知字段被剥（向前兼容）
        d = home_env / "users" / "u1" / "agents" / "legacy-agent"
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("name: legacy-agent\nprompt_file: system.md\nunknown_key: 42\n", encoding="utf-8")
        cfg = load_agent_config("legacy-agent", user_id="u1")
        assert cfg.name == "legacy-agent"
        assert not hasattr(cfg, "prompt_file")
        assert not hasattr(cfg, "unknown_key")

    def test_tool_groups_loaded(self, home_env):
        _write_per_user(home_env, "u1", "restricted", {"tool_groups": ["file:read", "file:write"]})
        cfg = load_agent_config("restricted", user_id="u1")
        assert cfg.tool_groups == ["file:read", "file:write"]

    def test_skills_empty_list(self, home_env):
        _write_per_user(home_env, "u1", "no-skills", {"skills": []})
        cfg = load_agent_config("no-skills", user_id="u1")
        assert cfg.skills == []

    def test_skills_omitted_is_none(self, home_env):
        _write_per_user(home_env, "u1", "default-skills", {})
        cfg = load_agent_config("default-skills", user_id="u1")
        assert cfg.skills is None

    def test_yaml_parse_error_raises_value_error(self, home_env):
        d = home_env / "users" / "u1" / "agents" / "bad-yaml"
        d.mkdir(parents=True)
        # 非法 YAML（未闭合引号 + tab 缩进混合）
        (d / "config.yaml").write_text('name: "unterminated\n\tbad: indent\n', encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to parse agent config"):
            load_agent_config("bad-yaml", user_id="u1")

    def test_invalid_name_raises(self, home_env):
        with pytest.raises(ValueError, match="Invalid agent name"):
            load_agent_config("bad name", user_id="u1")

    def test_empty_yaml_file_infers_name(self, home_env):
        # 空 config.yaml → yaml.safe_load 返回 None → {} → name 由目录兜底
        d = home_env / "users" / "u1" / "agents" / "empty-cfg"
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("", encoding="utf-8")
        cfg = load_agent_config("empty-cfg", user_id="u1")
        assert cfg.name == "empty-cfg"
        assert cfg.description == ""


# ---------------------------------------------------------------------------
# 6. load_agent_soul
# ===========================================================================


class TestLoadAgentSoul:
    def test_reads_soul_from_per_user(self, home_env):
        _write_per_user(home_env, "u1", "agent", {"name": "agent"}, soul="You are a code reviewer.")
        soul = load_agent_soul("agent", user_id="u1")
        assert soul == "You are a code reviewer."

    def test_missing_soul_returns_none(self, home_env):
        _write_per_user(home_env, "u1", "no-soul", {"name": "no-soul"})
        assert load_agent_soul("no-soul", user_id="u1") is None

    def test_empty_soul_returns_none(self, home_env):
        _write_per_user(home_env, "u1", "empty-soul", {"name": "empty-soul"}, soul="   \n   ")
        assert load_agent_soul("empty-soul", user_id="u1") is None

    def test_soul_is_stripped(self, home_env):
        _write_per_user(home_env, "u1", "padded", {"name": "padded"}, soul="\n\n  hello  \n\n")
        assert load_agent_soul("padded", user_id="u1") == "hello"

    def test_default_agent_reads_base_dir_soul(self, home_env):
        # agent_name=None → 读 base_dir/SOUL.md
        (home_env / SOUL_FILENAME).write_text("Global persona", encoding="utf-8")
        assert load_agent_soul(None) == "Global persona"

    def test_default_agent_missing_returns_none(self, home_env):
        assert load_agent_soul(None) is None

    def test_soul_from_legacy_fallback(self, home_env):
        # per-user 无 → legacy 回退读 SOUL.md
        _write_legacy(home_env, "legacy-agent", {"name": "legacy-agent"}, soul="legacy soul")
        assert load_agent_soul("legacy-agent", user_id="u1") == "legacy soul"

    def test_soul_per_user_priority_over_legacy(self, home_env):
        _write_legacy(home_env, "shared", {"name": "shared"}, soul="legacy soul")
        _write_per_user(home_env, "u1", "shared", {"name": "shared"}, soul="my soul")
        assert load_agent_soul("shared", user_id="u1") == "my soul"


# ---------------------------------------------------------------------------
# 7. list_custom_agents
# ===========================================================================


class TestListCustomAgents:
    def test_empty_when_no_agents_dir(self, home_env):
        assert list_custom_agents(user_id="u1") == []

    def test_discovers_multiple_per_user(self, home_env):
        _write_per_user(home_env, "u1", "agent-a", {"name": "agent-a"})
        _write_per_user(home_env, "u1", "agent-b", {"name": "agent-b", "description": "B"})
        agents = list_custom_agents(user_id="u1")
        names = [a.name for a in agents]
        assert names == ["agent-a", "agent-b"]

    def test_discovers_legacy(self, home_env):
        _write_legacy(home_env, "legacy-a", {"name": "legacy-a"})
        agents = list_custom_agents(user_id="u1")
        assert [a.name for a in agents] == ["legacy-a"]

    def test_skips_dirs_without_config_yaml(self, home_env):
        _write_per_user(home_env, "u1", "valid", {"name": "valid"})
        # 另一个目录无 config.yaml
        (home_env / "users" / "u1" / "agents" / "invalid").mkdir(parents=True)
        agents = list_custom_agents(user_id="u1")
        assert len(agents) == 1
        assert agents[0].name == "valid"

    def test_skips_non_directory_entries(self, home_env):
        agents_root = home_env / "users" / "u1" / "agents"
        agents_root.mkdir(parents=True)
        (agents_root / "not-a-dir.txt").write_text("hello", encoding="utf-8")
        _write_per_user(home_env, "u1", "real-agent", {"name": "real-agent"})
        agents = list_custom_agents(user_id="u1")
        assert len(agents) == 1
        assert agents[0].name == "real-agent"

    def test_sorted_by_name(self, home_env):
        _write_per_user(home_env, "u1", "z-agent", {"name": "z-agent"})
        _write_per_user(home_env, "u1", "a-agent", {"name": "a-agent"})
        _write_per_user(home_env, "u1", "m-agent", {"name": "m-agent"})
        names = [a.name for a in list_custom_agents(user_id="u1")]
        assert names == ["a-agent", "m-agent", "z-agent"]

    def test_per_user_shadows_legacy_same_name(self, home_env):
        # 同名时 per-user 覆盖 legacy（并集 + per-user 先扫）
        _write_legacy(home_env, "shared", {"name": "shared", "model": "legacy-model"})
        _write_per_user(home_env, "u1", "shared", {"name": "shared", "model": "my-model"})
        agents = list_custom_agents(user_id="u1")
        assert len(agents) == 1  # 不重复
        assert agents[0].model == "my-model"  # per-user 胜

    def test_union_of_per_user_and_legacy_distinct_names(self, home_env):
        _write_per_user(home_env, "u1", "mine", {"name": "mine"})
        _write_legacy(home_env, "theirs", {"name": "theirs"})
        names = [a.name for a in list_custom_agents(user_id="u1")]
        assert set(names) == {"mine", "theirs"}

    def test_skips_dir_with_invalid_config(self, home_env, caplog):
        # config.yaml 解析失败 → 记 warning 跳过，不抛
        d = home_env / "users" / "u1" / "agents" / "broken"
        d.mkdir(parents=True)
        (d / "config.yaml").write_text('name: "unterminated\n\tbad\n', encoding="utf-8")
        _write_per_user(home_env, "u1", "good", {"name": "good"})
        import logging

        with caplog.at_level(logging.WARNING):
            agents = list_custom_agents(user_id="u1")
        names = [a.name for a in agents]
        assert "good" in names
        assert "broken" not in names  # 解析失败被跳过

    def test_default_user_id_from_context(self, home_env):
        # 不传 user_id → autouse 上下文的 user
        from deerflow.runtime.user_context import get_effective_user_id

        effective = get_effective_user_id()
        _write_per_user(home_env, effective, "ctx-agent", {"name": "ctx-agent"})
        agents = list_custom_agents()
        assert [a.name for a in agents] == ["ctx-agent"]

    def test_isolation_between_users(self, home_env):
        # u1 的 agent 不影响 u2
        _write_per_user(home_env, "u1", "u1-agent", {"name": "u1-agent"})
        _write_per_user(home_env, "u2", "u2-agent", {"name": "u2-agent"})
        assert [a.name for a in list_custom_agents(user_id="u1")] == ["u1-agent"]
        assert [a.name for a in list_custom_agents(user_id="u2")] == ["u2-agent"]


# ---------------------------------------------------------------------------
# 8. 集成：SOUL_FILENAME / get_paths 联动
# ===========================================================================


class TestConstantsAndIntegration:
    def test_soul_filename_constant(self):
        assert SOUL_FILENAME == "SOUL.md"

    def test_get_paths_base_dir_follows_home_env(self, home_env):
        # DEER_FLOW_HOME 驱动 get_paths().base_dir（hermetic 链路验证）
        assert get_paths().base_dir == home_env

    def test_full_roundtrip_per_user(self, home_env):
        """端到端：写 per-user agent → load_agent_config + load_agent_soul 都读到。"""
        _write_per_user(
            home_env,
            "u1",
            "full-agent",
            {"name": "full-agent", "description": "Full", "model": "x", "tool_groups": ["g"], "skills": ["s"]},
            soul="Be helpful.",
        )
        cfg = load_agent_config("full-agent", user_id="u1")
        soul = load_agent_soul("full-agent", user_id="u1")
        assert cfg is not None and cfg.name == "full-agent"
        assert cfg.tool_groups == ["g"]
        assert cfg.skills == ["s"]
        assert soul == "Be helpful."
        # list_custom_agents 也发现它
        listed = list_custom_agents(user_id="u1")
        assert [a.name for a in listed] == ["full-agent"]
