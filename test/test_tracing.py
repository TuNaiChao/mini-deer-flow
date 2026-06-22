"""``test_tracing.py`` —— 链路追踪（M12）hermetic 测试。

覆盖：
- ``build_tracing_callbacks``：未配置返回空、langsmith/langfuse provider 构造、validate 缺凭证报错、
  构造异常包 RuntimeError、双 provider。
- ``build_langfuse_trace_metadata``：langfuse 未启用返回 {}、启用时字段映射、DEFAULT_USER_ID 兜底、
  environment/model_name tags。
- ``inject_langfuse_metadata``：就地 merge、setdefault 调用方优先、未启用 no-op。
- models ``attach_tracing`` 联动：``_maybe_build_tracing_callbacks`` 懒导入 tracing、
  ``create_chat_model(attach_tracing=False)`` 不挂回调、``True`` 挂回调。

hermetic：langfuse SDK 非依赖，用 ``sys.modules`` 注入 fake；langsmith tracer 构造经
monkeypatch 替身；环境变量经 monkeypatch.setenv 控制；models 联动用 ``_FakeModelClass`` +
resolve_class/app_config 替身 + ``_maybe_build_tracing_callbacks`` spy，跑真 create_chat_model
到「挂回调」那步而不碰真模型 provider。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.tracing import (
    build_langfuse_trace_metadata,
    build_tracing_callbacks,
    inject_langfuse_metadata,
)
from deerflow.tracing import factory as factory_module

# ---------------------------------------------------------------------------
# factory：build_tracing_callbacks
# ===========================================================================


@pytest.fixture
def _no_tracing_env(monkeypatch):
    """清掉所有 tracing 环境变量，回到「未配置」基线。"""
    for var in (
        "LANGSMITH_TRACING",
        "LANGFUSE_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(var, raising=False)


class TestBuildTracingCallbacks:
    def test_no_providers_returns_empty(self, _no_tracing_env):
        assert build_tracing_callbacks() == []

    def test_validate_raises_when_langsmith_key_missing(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        # 未设 LANGSMITH_API_KEY -> validate ValueError（在构造前抛）
        with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
            build_tracing_callbacks()

    def test_validate_raises_when_langfuse_keys_missing(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGFUSE_TRACING", "true")
        with pytest.raises(ValueError, match="LANGFUSE_SECRET_KEY"):
            build_tracing_callbacks()

    def test_langsmith_provider_built(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
        monkeypatch.setenv("LANGSMITH_PROJECT", "my-proj")
        # 替身构造器，避免真 LangChainTracer
        captured = {}

        def fake_ls_tracer(cfg):
            captured["project"] = cfg.project
            return MagicMock(name="langsmith-tracer")

        monkeypatch.setattr(factory_module, "_create_langsmith_tracer", fake_ls_tracer)
        cbs = build_tracing_callbacks()
        assert len(cbs) == 1
        assert captured["project"] == "my-proj"

    def test_langfuse_provider_built(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGFUSE_TRACING", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pub")
        monkeypatch.setenv("LANGFUSE_HOST", "https://lf.example.com")
        captured = {}

        def fake_lf_handler(cfg):
            captured["secret"] = cfg.secret_key
            captured["public"] = cfg.public_key
            captured["host"] = cfg.host
            return MagicMock(name="langfuse-handler")

        monkeypatch.setattr(factory_module, "_create_langfuse_handler", fake_lf_handler)
        cbs = build_tracing_callbacks()
        assert len(cbs) == 1
        assert captured["secret"] == "secret"
        assert captured["public"] == "pub"
        assert captured["host"] == "https://lf.example.com"

    def test_both_providers(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
        monkeypatch.setenv("LANGFUSE_TRACING", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "s")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "p")
        monkeypatch.setattr(factory_module, "_create_langsmith_tracer", lambda cfg: MagicMock())
        monkeypatch.setattr(factory_module, "_create_langfuse_handler", lambda cfg: MagicMock())
        cbs = build_tracing_callbacks()
        assert len(cbs) == 2

    def test_construction_error_wrapped_runtimeerror(self, _no_tracing_env, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")

        def boom(cfg):
            raise OSError("network down")

        monkeypatch.setattr(factory_module, "_create_langsmith_tracer", boom)
        with pytest.raises(RuntimeError, match="LangSmith tracing initialization failed"):
            build_tracing_callbacks()

    def test_real_langsmith_tracer_construction(self, _no_tracing_env, monkeypatch):
        # 真 _create_langsmith_tracer：langchain_core 可用，构造不发网络
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
        monkeypatch.setenv("LANGSMITH_PROJECT", "real-proj")
        from langchain_core.tracers.langchain import LangChainTracer

        tracer = factory_module._create_langsmith_tracer(SimpleNamespace(project="real-proj"))
        assert isinstance(tracer, LangChainTracer)

    def test_real_langfuse_handler_with_fake_sdk(self, _no_tracing_env, monkeypatch):
        # langfuse 非 mini 依赖 -> 注入 fake 模块测真 _create_langfuse_handler
        monkeypatch.setenv("LANGFUSE_TRACING", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "s")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "p")
        monkeypatch.setenv("LANGFUSE_HOST", "https://lf")

        fake_langfuse_mod = MagicMock()
        fake_langfuse_mod.Langfuse = MagicMock()
        fake_cb_mod = MagicMock()
        fake_handler = MagicMock(name="LangfuseCallbackHandler")
        fake_cb_mod.CallbackHandler = MagicMock(return_value=fake_handler)
        monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse_mod)
        monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_cb_mod)

        cfg = SimpleNamespace(secret_key="s", public_key="p", host="https://lf")
        handler = factory_module._create_langfuse_handler(cfg)
        assert handler is fake_handler
        # Langfuse 单例用配置初始化
        fake_langfuse_mod.Langfuse.assert_called_once_with(secret_key="s", public_key="p", host="https://lf")
        fake_cb_mod.CallbackHandler.assert_called_once_with(public_key="p")


# ---------------------------------------------------------------------------
# metadata：build_langfuse_trace_metadata + inject_langfuse_metadata
# ===========================================================================


@pytest.fixture
def _langfuse_enabled(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "s")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "p")


class TestLangfuseMetadata:
    def test_empty_when_langfuse_disabled(self, _no_tracing_env):
        # langfuse 未启用 -> {} （不影响 langsmith 等其它 tracer）
        out = build_langfuse_trace_metadata(thread_id="t1", user_id="u1")
        assert out == {}

    def test_field_mapping(self, _langfuse_enabled):
        out = build_langfuse_trace_metadata(
            thread_id="thread-42",
            user_id="alice",
            assistant_id="research-agent",
        )
        assert out["langfuse_session_id"] == "thread-42"
        assert out["langfuse_user_id"] == "alice"
        assert out["langfuse_trace_name"] == "research-agent"
        # 无 env/model -> 不带 tags
        assert "langfuse_tags" not in out

    def test_default_trace_name_when_no_assistant(self, _langfuse_enabled):
        out = build_langfuse_trace_metadata(thread_id="t1")
        assert out["langfuse_trace_name"] == "lead-agent"

    def test_default_user_id_fallback(self, _langfuse_enabled):
        # user_id=None -> DEFAULT_USER_ID（"default"），让无鉴权模式 Users 页仍可用
        from deerflow.runtime.user_context import DEFAULT_USER_ID

        out = build_langfuse_trace_metadata(thread_id="t1", user_id=None)
        assert out["langfuse_user_id"] == DEFAULT_USER_ID == "default"

    def test_tags_from_environment_and_model(self, _langfuse_enabled):
        out = build_langfuse_trace_metadata(
            thread_id="t1",
            model_name="gpt-4o",
            environment="production",
        )
        assert out["langfuse_tags"] == ["env:production", "model:gpt-4o"]

    def test_tags_partial(self, _langfuse_enabled):
        only_model = build_langfuse_trace_metadata(thread_id="t1", model_name="claude-3")
        assert only_model["langfuse_tags"] == ["model:claude-3"]
        only_env = build_langfuse_trace_metadata(thread_id="t1", environment="staging")
        assert only_env["langfuse_tags"] == ["env:staging"]


class TestInjectLangfuseMetadata:
    def test_noop_when_disabled(self, _no_tracing_env):
        config = {"metadata": {"existing": "kept"}}
        inject_langfuse_metadata(config, thread_id="t1")
        # 未启用 -> 不改 config
        assert config == {"metadata": {"existing": "kept"}}

    def test_merges_into_metadata(self, _langfuse_enabled):
        config: dict = {}
        inject_langfuse_metadata(config, thread_id="t1", user_id="u", assistant_id="a")
        assert config["metadata"]["langfuse_session_id"] == "t1"
        assert config["metadata"]["langfuse_user_id"] == "u"
        assert config["metadata"]["langfuse_trace_name"] == "a"

    def test_caller_supplied_metadata_wins(self, _langfuse_enabled):
        # setdefault：调用方已有的 key 不被覆盖（前端设的 session_id 留住）
        config = {"metadata": {"langfuse_session_id": "frontend-set"}}
        inject_langfuse_metadata(config, thread_id="backend-tid", user_id="u")
        assert config["metadata"]["langfuse_session_id"] == "frontend-set"
        # 其它 key 仍注入
        assert config["metadata"]["langfuse_user_id"] == "u"

    def test_preserves_existing_unrelated_metadata(self, _langfuse_enabled):
        config = {"metadata": {"custom": "x"}}
        inject_langfuse_metadata(config, thread_id="t1")
        assert config["metadata"]["custom"] == "x"
        assert config["metadata"]["langfuse_session_id"] == "t1"

    def test_creates_metadata_key_if_absent(self, _langfuse_enabled):
        config: dict = {"other": 1}
        inject_langfuse_metadata(config, thread_id="t1")
        assert "metadata" in config
        assert config["other"] == 1


# ---------------------------------------------------------------------------
# models attach_tracing 联动
# ===========================================================================


class _FakeModelClass:
    """替身模型类：吞任意构造参数，允许事后设 .callbacks 属性。"""

    def __init__(self, **kwargs):
        self.callbacks = kwargs.get("callbacks")  # 通常不在 kwargs；保持 None


@pytest.fixture
def _scaffolded_factory(monkeypatch):
    """搭好让真 create_chat_model 能跑到「挂回调」那一步的脚手架。

    - resolve_class 替成返回 _FakeModelClass（不真 import 模型 provider）。
    - app_config 返回一个 model_dump()={} 的 model_config（无构造参数）。
    - _maybe_build_tracing_callbacks 替成 spy（可控制返回值 + 计数）。

    返回 SimpleNamespace(spy_calls, set_spy_return, last_model_holder)。
    """
    from deerflow.models import factory as models_factory

    # model_config 替身：model_dump 返回空 dict（无构造参数）
    model_config = MagicMock()
    model_config.name = "m"
    model_config.use = "fake:FakeModel"
    model_config.supports_thinking = False
    model_config.supports_reasoning_effort = False
    model_config.when_thinking_enabled = None
    model_config.thinking = None
    model_config.when_thinking_disabled = None
    model_config.model_dump = MagicMock(return_value={})

    app_config = MagicMock()
    app_config.get_model_config = MagicMock(return_value=model_config)

    monkeypatch.setattr(models_factory, "resolve_class", lambda use, base: _FakeModelClass)

    state = SimpleNamespace(spy_calls=0, spy_return=[], last_model=None)

    def spy():
        state.spy_calls += 1
        return state.spy_return

    monkeypatch.setattr(models_factory, "_maybe_build_tracing_callbacks", spy)
    return state, app_config


class TestModelsAttachTracing:
    def test_maybe_build_lazy_imports_tracing(self, _no_tracing_env):
        # tracing 包已落地 -> _maybe_build_tracing_callbacks 不再 ImportError，返回 []
        from deerflow.models.factory import _maybe_build_tracing_callbacks

        assert _maybe_build_tracing_callbacks() == []

    def test_maybe_build_returns_callbacks_when_enabled(self, monkeypatch, _no_tracing_env):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "k")
        from deerflow.models.factory import _maybe_build_tracing_callbacks

        monkeypatch.setattr(factory_module, "_create_langsmith_tracer", lambda cfg: MagicMock(name="ls"))
        cbs = _maybe_build_tracing_callbacks()
        assert len(cbs) == 1

    def test_maybe_build_swallows_errors(self, monkeypatch, _no_tracing_env):
        # build_tracing_callbacks 抛错时 _maybe_build 不让模型创建失败，返回 []
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        # 缺 API_KEY -> validate ValueError -> 被 _maybe_build 的 except 吞成 []
        from deerflow.models.factory import _maybe_build_tracing_callbacks

        assert _maybe_build_tracing_callbacks() == []

    def test_attach_tracing_false_skips_callback_build(self, _scaffolded_factory):
        state, app_config = _scaffolded_factory
        from deerflow.models import create_chat_model

        model = create_chat_model(name="m", app_config=app_config, attach_tracing=False)
        # attach_tracing=False -> 不调 _maybe_build_tracing_callbacks
        assert state.spy_calls == 0
        assert model.callbacks is None  # 未挂

    def test_attach_tracing_true_calls_callback_build(self, _scaffolded_factory):
        state, app_config = _scaffolded_factory
        state.spy_return = [MagicMock(name="tracer")]
        from deerflow.models import create_chat_model

        model = create_chat_model(name="m", app_config=app_config, attach_tracing=True)
        assert state.spy_calls == 1
        # 回调挂到模型上
        assert model.callbacks is not None
        assert len(model.callbacks) == 1

    def test_attach_tracing_true_noop_when_callbacks_empty(self, _scaffolded_factory):
        # spy 返回 []（未配置 tracing）-> 调了但不挂
        state, app_config = _scaffolded_factory
        state.spy_return = []
        from deerflow.models import create_chat_model

        model = create_chat_model(name="m", app_config=app_config, attach_tracing=True)
        assert state.spy_calls == 1
        # callbacks 空 -> 不设 .callbacks（保持 None）
        assert model.callbacks is None
