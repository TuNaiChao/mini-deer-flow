"""配置模块测试。

hermetic：不读 config.yaml，直接构造 AppConfig/ModelConfig 并用 monkeypatch 控制
环境变量，验证默认值（空配置可启动，红线 #25）、环境变量展开、追踪 provider 解析。
"""

from __future__ import annotations

import pytest

from deerflow.config import (
    AppConfig,
    ModelConfig,
    get_enabled_tracing_providers,
    get_tracing_config,
    validate_enabled_tracing_providers,
)
from deerflow.config.app_config import _expand_env_vars

# ---------------------------------------------------------------------------
# AppConfig 默认值（空配置必须可启动）
# ---------------------------------------------------------------------------


def test_app_config_defaults_are_safe():
    """空 AppConfig 必须能构造，且关键字段有安全默认。"""
    cfg = AppConfig()
    assert cfg.log_level == "info"
    assert cfg.models == []
    assert cfg.memory["enabled"] is True
    assert cfg.sandbox["use"].endswith("LocalSandboxProvider")
    assert cfg.database["backend"] == "sqlite"


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
    """extra='allow' → 可传 provider 特定字段（api_key/temperature 等）。"""
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
