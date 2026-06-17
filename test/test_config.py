"""配置模块测试（M0 类型化后）。

hermetic：不读 config.yaml，直接构造 AppConfig / 各子配置，用 monkeypatch 控制
环境变量。覆盖：空配置可启动（红线 #25）、子配置默认值、database 派生路径、
config_version、reload_boundary、paths、env 展开、model_config、is_skill_enabled、
loop_detection validator。
"""

from __future__ import annotations

import pytest

from deerflow.config import (
    AppConfig,
    ContextSize,
    DatabaseConfig,
    ExtensionsConfig,
    LoopDetectionConfig,
    MemoryConfig,
    ModelConfig,
    SummarizationConfig,
    format_field_description,
    get_enabled_tracing_providers,
    get_paths,
    get_tracing_config,
    is_startup_only_field,
    resolve_path,
    runtime_home,
    validate_enabled_tracing_providers,
)
from deerflow.config.app_config import _expand_env_vars

# ---------------------------------------------------------------------------
# AppConfig 默认值（空配置必须可启动，红线 #25）
# ---------------------------------------------------------------------------


def test_app_config_defaults_are_safe():
    """空 AppConfig 必须能构造，且关键字段有安全默认（memory 后端、各开关默认开）。"""
    cfg = AppConfig()
    assert cfg.log_level == "info"
    assert cfg.config_version == 0
    assert cfg.models == []
    # 子配置是类型化对象，不再是 dict
    assert isinstance(cfg.memory, MemoryConfig)
    assert cfg.memory.enabled is True
    assert cfg.database.backend == "memory"  # 红线 #25：默认 memory
    assert cfg.sandbox.use.endswith("LocalSandboxProvider")  # 空配置默认 provider
    assert cfg.checkpointer is None  # 未配置 → None（用 database 派生）
    assert cfg.stream_bridge is None


def test_app_config_accepts_models():
    """能接收 models 列表。"""
    cfg = AppConfig(models=[ModelConfig(name="a", use="x:A", model="ma")])
    assert len(cfg.models) == 1
    assert cfg.models[0].name == "a"


def test_app_config_get_model_config_contract():
    """get_model_config：None→首个、命名查找、找不到/空→None。"""
    cfg = AppConfig(
        models=[
            ModelConfig(name="first", use="x:F", model="mf"),
            ModelConfig(name="second", use="x:S", model="ms"),
        ]
    )
    assert cfg.get_model_config(None).name == "first"
    assert cfg.get_model_config("second").model == "ms"
    assert cfg.get_model_config("missing") is None
    assert AppConfig().get_model_config(None) is None


def test_app_config_coerces_null_list_sections():
    """yaml 里全注释的列表节解析成 None，validator 归一为 []（红线 #25 配套）。"""
    cfg = AppConfig(models=None, tools=None, tool_groups=None)
    assert cfg.models == []
    assert cfg.tools == []
    assert cfg.tool_groups == []


def test_app_config_constructs_subconfigs_from_dict():
    """AppConfig 从 dict 构造子配置（pydantic 自动转换）。"""
    cfg = AppConfig(
        memory={"enabled": False, "max_facts": 50},
        database={"backend": "sqlite", "sqlite_dir": "/tmp/db"},
        loop_detection={"warn_threshold": 2, "hard_limit": 4},
    )
    assert cfg.memory.enabled is False
    assert cfg.memory.max_facts == 50
    assert cfg.database.backend == "sqlite"
    assert cfg.loop_detection.warn_threshold == 2


def test_app_config_allows_unknown_fields():
    """extra='allow' → 未知字段不崩（容错）。"""
    cfg = AppConfig(unknown_section={"x": 1})  # type: ignore[call-arg]
    assert cfg.log_level == "info"


# ---------------------------------------------------------------------------
# 子配置默认值
# ---------------------------------------------------------------------------


def test_database_config_defaults_and_derived():
    """DatabaseConfig 默认 memory；派生 sqlite_path / app_sqlalchemy_url。"""
    db = DatabaseConfig(backend="sqlite", sqlite_dir="/tmp/data")
    assert db.sqlite_path.endswith("deerflow.db")
    assert "/tmp/data" in db.sqlite_path
    # 派生别名
    assert db.checkpointer_sqlite_path == db.sqlite_path
    assert db.app_sqlite_path == db.sqlite_path
    # sqlalchemy url
    assert db.app_sqlalchemy_url.startswith("sqlite+aiosqlite:///")
    assert "deerflow.db" in db.app_sqlalchemy_url


def test_database_config_postgres_url_rewrite():
    """postgres 的 postgresql:// → postgresql+asyncpg://。"""
    db = DatabaseConfig(backend="postgres", postgres_url="postgresql://u:p@h:5432/d")
    assert db.app_sqlalchemy_url == "postgresql+asyncpg://u:p@h:5432/d"


def test_database_config_memory_has_no_sqlalchemy_url():
    """memory 后端没有 sqlalchemy url，查询应 raise。"""
    db = DatabaseConfig(backend="memory")
    with pytest.raises(ValueError, match="No SQLAlchemy URL"):
        _ = db.app_sqlalchemy_url


def test_memory_config_defaults():
    m = MemoryConfig()
    assert m.enabled is True
    assert m.max_facts == 100
    assert m.debounce_seconds == 30
    assert m.fact_confidence_threshold == 0.7
    assert m.injection_enabled is True
    assert m.token_counting == "tiktoken"


def test_summarization_config_defaults():
    s = SummarizationConfig()
    assert s.enabled is False
    assert isinstance(s.keep, ContextSize)
    assert s.keep.type == "messages"
    assert s.keep.value == 20
    assert s.keep.to_tuple() == ("messages", 20)


def test_loop_detection_config_defaults_and_validator():
    """默认通过；hard_limit < warn_threshold 时 validator 报错。"""
    ld = LoopDetectionConfig()
    assert ld.enabled is True
    assert ld.warn_threshold == 3
    assert ld.hard_limit == 5
    with pytest.raises(ValueError, match="hard_limit"):
        LoopDetectionConfig(warn_threshold=5, hard_limit=3)


# ---------------------------------------------------------------------------
# config_version
# ---------------------------------------------------------------------------


def test_config_version_defaults_zero():
    assert AppConfig().config_version == 0
    assert AppConfig(config_version=7).config_version == 7


# ---------------------------------------------------------------------------
# reload_boundary（热重载边界）
# ---------------------------------------------------------------------------


def test_is_startup_only_field():
    """基础设施字段需重启；业务字段不需要。"""
    assert is_startup_only_field("database") is True
    assert is_startup_only_field("sandbox") is True
    assert is_startup_only_field("checkpointer") is True
    assert is_startup_only_field("models") is False
    assert is_startup_only_field("memory") is False


def test_format_field_description_registered_field():
    """已登记字段描述以 startup-only: 开头，含字段文档。"""
    desc = format_field_description("log_level", field_doc="日志级别")
    assert desc.startswith("startup-only:")
    assert "日志级别" in desc


def test_format_field_description_unregistered_raises():
    """未登记字段 raise KeyError（防笔误绕过漂移覆盖）。"""
    with pytest.raises(KeyError):
        format_field_description("not_a_real_field")


def test_app_config_startup_only_fields_carry_marker():
    """AppConfig 的基础设施字段描述带 startup-only 标记。"""
    fields = AppConfig.model_fields
    assert fields["database"].description.startswith("startup-only:")
    assert fields["sandbox"].description.startswith("startup-only:")


# ---------------------------------------------------------------------------
# paths（resolve_path / runtime_home / get_paths）
# ---------------------------------------------------------------------------


def test_resolve_path_absolute_passthrough(tmp_path):
    """绝对路径原样（resolve 后）返回。"""
    assert resolve_path(str(tmp_path)) == tmp_path


def test_resolve_path_relative_against_project_root(monkeypatch, tmp_path):
    """相对路径相对 project_root 解析。"""
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", str(tmp_path))
    assert resolve_path("sub/file") == (tmp_path / "sub" / "file").resolve()


def test_runtime_home_env_override(monkeypatch, tmp_path):
    """DEER_FLOW_HOME 覆盖 base_dir。"""
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path / "home"))
    assert runtime_home() == (tmp_path / "home").resolve()


def test_get_paths_base_dir_matches_runtime_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DEER_FLOW_HOME", str(tmp_path))
    paths = get_paths()
    assert paths.base_dir == tmp_path.resolve()
    assert paths.users_dir == (tmp_path / "users").resolve()


def test_project_root_env_must_exist(monkeypatch):
    """DEER_FLOW_PROJECT_ROOT 指向不存在的路径应报错。"""
    monkeypatch.setenv("DEER_FLOW_PROJECT_ROOT", "/no/such/dir/xyz")
    with pytest.raises(ValueError, match="DEER_FLOW_PROJECT_ROOT"):
        resolve_path("rel")


# ---------------------------------------------------------------------------
# extensions_config.is_skill_enabled
# ---------------------------------------------------------------------------


def test_is_skill_enabled_explicit_list():
    assert ExtensionsConfig(enabled_skills=["alpha"]).is_skill_enabled("alpha") is True


def test_is_skill_enabled_default_public_custom_enabled():
    """未显式配置的 public/custom 技能默认启用（对齐 deer）。"""
    cfg = ExtensionsConfig()
    assert cfg.is_skill_enabled("anything", "public") is True
    assert cfg.is_skill_enabled("anything", "custom") is True


def test_is_skill_enabled_unknown_category_disabled():
    cfg = ExtensionsConfig()
    assert cfg.is_skill_enabled("anything", "system") is False


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------


def test_model_config_required_fields_and_defaults():
    """name/use/model 必填，能力字段默认 False。"""
    m = ModelConfig(name="t", use="x:T", model="mt")
    assert m.name == "t"
    assert m.supports_thinking is False
    assert m.supports_vision is False
    assert m.supports_reasoning_effort is False


def test_model_config_allows_extra_provider_fields():
    """extra='allow' → 可传 provider 特定字段。"""
    m = ModelConfig(name="t", use="x:T", model="mt", api_key="k", temperature=0.5)
    assert m.api_key == "k"  # type: ignore[attr-defined]
    assert m.temperature == 0.5  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 环境变量展开（$VAR / ${VAR}）
# ---------------------------------------------------------------------------


def test_expand_env_vars_dollar(monkeypatch):
    monkeypatch.setenv("DF_TEST_VAR", "expanded")
    assert _expand_env_vars("$DF_TEST_VAR") == "expanded"


def test_expand_env_vars_braces(monkeypatch):
    monkeypatch.setenv("DF_TEST_VAR", "braced")
    assert _expand_env_vars("${DF_TEST_VAR}") == "braced"


def test_expand_env_vars_recurses_into_containers(monkeypatch):
    """dict / list 递归展开。"""
    monkeypatch.setenv("DF_K", "v")
    assert _expand_env_vars({"a": "$DF_K", "b": ["${DF_K}", "plain"]}) == {"a": "v", "b": ["v", "plain"]}


def test_expand_env_vars_unset_keeps_placeholder(monkeypatch):
    """未设置的环境变量保留原占位文本。"""
    monkeypatch.delenv("DF_UNSET", raising=False)
    assert _expand_env_vars("$DF_UNSET") == "$DF_UNSET"


# ---------------------------------------------------------------------------
# 追踪 provider（环境驱动）
# ---------------------------------------------------------------------------


def test_enabled_tracing_providers_empty_by_default(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGFUSE_TRACING", raising=False)
    assert get_enabled_tracing_providers() == []


def test_enabled_tracing_providers_langsmith(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGFUSE_TRACING", raising=False)
    assert get_enabled_tracing_providers() == ["langsmith"]


def test_validate_tracing_providers_missing_key_raises(monkeypatch):
    """启用 langsmith 但缺 API key → ValueError。"""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        validate_enabled_tracing_providers()


def test_get_tracing_config_reads_env(monkeypatch):
    monkeypatch.setenv("LANGSMITH_PROJECT", "proj-x")
    monkeypatch.setenv("LANGSMITH_API_KEY", "key-x")
    tracing = get_tracing_config()
    assert tracing.langsmith.project == "proj-x"
    assert tracing.langsmith.api_key == "key-x"
