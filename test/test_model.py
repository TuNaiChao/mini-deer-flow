"""模型工厂测试。

两类：
- **单元测试**（``test_*``）：不依赖 config.yaml / 网络。用 ``app_config`` 显式注入
  + patch ``resolve_class`` 返回记录器类，验证 thinking/stream/reasoning_effort 等逻辑。
- **集成冒烟**（``test_model_factory_integration``）：需 config.yaml + 真实 API key，
  无则自动跳过。
"""

from __future__ import annotations

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models import create_chat_model, get_default_model
from deerflow.models import factory as factory_module


class _Recorder:
    """记录构造参数的假模型类。

    不继承 BaseChatModel——patch ``resolve_class`` 跳过 issubclass 校验，
    使测试无需引入真实 provider。
    """

    callbacks = None  # 模拟 langchain 模型的 callbacks 属性

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_config(**model_fields) -> AppConfig:
    """构造单模型的 AppConfig。默认 use=fake:Recorder。"""
    base = {"name": "t", "use": "fake:Recorder", "model": "m"}
    base.update(model_fields)
    return AppConfig(models=[ModelConfig(**base)])


# ---------------------------------------------------------------------------
# 单元测试（hermetic）
# ---------------------------------------------------------------------------


def test_basic_creation(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(temperature=0.5, api_key="k")

    model = create_chat_model(app_config=cfg)

    assert isinstance(model, _Recorder)
    # use/name 是元数据，不透传；model/temperature/api_key 是构造参数，透传
    assert model.kwargs["model"] == "m"
    assert model.kwargs["temperature"] == 0.5
    assert model.kwargs["api_key"] == "k"
    assert "use" not in model.kwargs
    assert "name" not in model.kwargs


def test_default_model_uses_first(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = AppConfig(models=[ModelConfig(name="first", use="x:F", model="mf"), ModelConfig(name="second", use="x:S", model="ms")])

    model = create_chat_model(app_config=cfg)

    assert model.kwargs["model"] == "mf"  # name=None → 第一个


def test_thinking_enabled_merge(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(
        supports_thinking=True,
        when_thinking_enabled={"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
    )

    model = create_chat_model("t", thinking_enabled=True, app_config=cfg)

    assert model.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_thinking_enabled_but_unsupported_raises(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(supports_thinking=False, when_thinking_enabled={"x": 1})

    with pytest.raises(ValueError, match="不支持 thinking"):
        create_chat_model("t", thinking_enabled=True, app_config=cfg)


def test_thinking_disabled_vllm_path(monkeypatch):
    """not thinking_enabled + vLLM chat_template_kwargs → 构造 disable 负载。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(
        supports_thinking=True,
        when_thinking_enabled={"extra_body": {"chat_template_kwargs": {"enable_thinking": True, "thinking": True}}},
    )

    model = create_chat_model("t", thinking_enabled=False, app_config=cfg)

    ct = model.kwargs["extra_body"]["chat_template_kwargs"]
    assert ct["enable_thinking"] is False
    assert ct["thinking"] is False


def test_thinking_disabled_openai_path(monkeypatch):
    """not thinking_enabled + OpenAI extra_body.thinking.type → disabled + reasoning_effort=minimal。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    # supports_reasoning_effort=True：否则 reasoning_effort 门控会把 OpenAI 关闭
    # 路径设的 reasoning_effort="minimal" 弹掉（factory 行为与 deer 一致）。
    cfg = _make_config(
        supports_thinking=True,
        supports_reasoning_effort=True,
        when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
    )

    model = create_chat_model("t", thinking_enabled=False, app_config=cfg)

    assert model.kwargs["extra_body"]["thinking"]["type"] == "disabled"
    assert model.kwargs["reasoning_effort"] == "minimal"


def test_when_thinking_disabled_explicit(monkeypatch):
    """显式 when_thinking_disabled 优先于其它关闭路径。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(
        supports_thinking=True,
        supports_reasoning_effort=True,
        when_thinking_enabled={"extra_body": {"thinking": {"type": "enabled"}}},
        when_thinking_disabled={"reasoning_effort": "low"},
    )

    model = create_chat_model("t", thinking_enabled=False, app_config=cfg)

    assert model.kwargs["reasoning_effort"] == "low"


def test_reasoning_effort_popped_when_unsupported(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(supports_reasoning_effort=False)

    model = create_chat_model("t", reasoning_effort="high", app_config=cfg)

    assert "reasoning_effort" not in model.kwargs


def test_reasoning_effort_kept_when_supported(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(supports_reasoning_effort=True)

    model = create_chat_model("t", reasoning_effort="high", app_config=cfg)

    assert model.kwargs["reasoning_effort"] == "high"


def test_stream_defaults_for_openai_compatible(monkeypatch):
    """OpenAI 兼容 + base_url → stream_usage=True + stream_chunk_timeout=240。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(use="langchain_openai:ChatOpenAI", base_url="http://x")

    model = create_chat_model("t", app_config=cfg)

    assert model.kwargs["stream_usage"] is True
    assert model.kwargs["stream_chunk_timeout"] == 240.0


def test_stream_chunk_timeout_dropped_for_non_openai(monkeypatch):
    """非 ChatOpenAI provider → stream_chunk_timeout 必须剔除防 TypeError。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config(use="langchain_deepseek:ChatDeepSeek")

    model = create_chat_model("t", app_config=cfg)

    assert "stream_chunk_timeout" not in model.kwargs


def test_unknown_model_raises(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config()

    with pytest.raises(ValueError, match="找不到模型"):
        create_chat_model("nope", app_config=cfg)


def test_no_models_raises(monkeypatch):
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = AppConfig(models=[])

    with pytest.raises(ValueError, match="未配置任何模型"):
        create_chat_model(app_config=cfg)


def test_attach_tracing_without_tracing_module(monkeypatch):
    """tracing 模块（M12）未落地时，attach_tracing=True 不报错、不挂回调。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config()

    model = create_chat_model("t", app_config=cfg, attach_tracing=True)

    # tracing 不存在 → _maybe_build_tracing_callbacks 返回 [] → 不修改 callbacks
    assert model.callbacks is None


def test_attach_tracing_false_skips(monkeypatch):
    """attach_tracing=False 完全跳过回调逻辑。"""
    monkeypatch.setattr(factory_module, "resolve_class", lambda path, base=None: _Recorder)
    cfg = _make_config()

    model = create_chat_model("t", app_config=cfg, attach_tracing=False)

    assert model.callbacks is None


def test_get_model_config_on_app_config():
    """AppConfig.get_model_config 的契约：None→首个、命名查找、找不到返回 None。"""
    cfg = AppConfig(models=[ModelConfig(name="a", use="x:A", model="ma"), ModelConfig(name="b", use="x:B", model="mb")])

    assert cfg.get_model_config(None).name == "a"
    assert cfg.get_model_config("b").model == "mb"
    assert cfg.get_model_config("missing") is None

    empty = AppConfig(models=[])
    assert empty.get_model_config(None) is None


# ---------------------------------------------------------------------------
# 集成冒烟（需 config.yaml + API key，无则跳过）
# ---------------------------------------------------------------------------


def test_model_factory_integration():
    """端到端冒烟：需 config.yaml + 真实 API key。无则跳过。"""
    try:
        model = get_default_model()
    except Exception:
        pytest.skip("无可用 config.yaml 或模型配置，跳过集成冒烟")

    try:
        response = model.invoke("用一句话介绍你自己")
    except Exception:
        pytest.skip("模型调用失败（缺 API key / 网络），跳过集成冒烟")

    assert response.content
