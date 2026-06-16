"""Agent 工厂测试。

hermetic：用 monkeypatch 把 langchain 的 ``create_agent`` 换成记录器，验证
``create_deerflow_agent`` / ``make_lead_agent`` 的组装逻辑（默认值、参数透传、
config 驱动），不真正编译图、不调真实模型。真实模型对话留给 ``*_live`` 冒烟。
"""

from __future__ import annotations

from deerflow.agents import create_deerflow_agent
from deerflow.agents import factory as factory_module
from deerflow.agents.lead_agent import agent as agent_module
from deerflow.agents.thread_state import ThreadState
from deerflow.config import AppConfig, ModelConfig

# ---------------------------------------------------------------------------
# create_deerflow_agent（SDK 入口，纯参数化）
# ---------------------------------------------------------------------------


def test_create_deerflow_agent_passes_args_to_create_agent(monkeypatch):
    """显式 model/tools 透传给 create_agent，默认 system_prompt/state_schema 生效。"""
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "compiled-graph")

    fake_model = object()
    agent = create_deerflow_agent(model=fake_model, tools=["t1"])

    assert agent == "compiled-graph"
    assert captured["model"] is fake_model
    assert captured["tools"] == ["t1"]
    assert captured["state_schema"] is ThreadState
    assert "DeerFlow" in captured["system_prompt"]
    assert captured["middleware"] == []


def test_create_deerflow_agent_defaults_model_and_tools(monkeypatch):
    """model=None → get_default_model()；tools=None → []。"""
    fake_model = object()
    monkeypatch.setattr(factory_module, "get_default_model", lambda: fake_model)
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")

    create_deerflow_agent()

    assert captured["model"] is fake_model
    assert captured["tools"] == []


def test_create_deerflow_agent_custom_system_prompt(monkeypatch):
    """自定义 system_prompt 覆盖默认。"""
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")

    create_deerflow_agent(model=object(), system_prompt="custom-prompt")

    assert captured["system_prompt"] == "custom-prompt"


# ---------------------------------------------------------------------------
# make_lead_agent（config 驱动；全部依赖 monkeypatch 桩化）
# ---------------------------------------------------------------------------


def _stub_lead_agent_dependencies(monkeypatch, *, create_chat_model=None) -> dict:
    """桩化 make_lead_agent 的全部外部依赖，返回记录 create_agent 入参的字典。"""
    fake_cfg = AppConfig(models=[ModelConfig(name="m", use="x:M", model="mm")])
    monkeypatch.setattr(agent_module, "get_app_config", lambda: fake_cfg)
    monkeypatch.setattr(agent_module, "build_middlewares", lambda *a, **k: ["mw"])
    monkeypatch.setattr(agent_module, "apply_prompt_template", lambda **k: "sys-prompt")
    monkeypatch.setattr("deerflow.tools.get_available_tools", lambda **k: ["tool"])
    if create_chat_model is None:
        create_chat_model = lambda **k: "fake-model"  # noqa: E731
    monkeypatch.setattr(agent_module, "create_chat_model", create_chat_model)

    captured: dict = {}
    monkeypatch.setattr(agent_module, "create_agent", lambda **k: captured.update(k) or "compiled")
    return captured


def test_make_lead_agent_assembles_graph_from_config(monkeypatch):
    """make_lead_agent 从 RunnableConfig 解析参数并组装图。"""
    captured = _stub_lead_agent_dependencies(monkeypatch)

    config = {"configurable": {"model_name": "m", "thread_id": "t1"}}
    agent = agent_module.make_lead_agent(config)

    assert agent == "compiled"
    assert captured["model"] == "fake-model"
    assert captured["tools"] == ["tool"]
    assert captured["middleware"] == ["mw"]
    assert captured["system_prompt"] == "sys-prompt"


def test_make_lead_agent_forwards_thinking_flag_to_model(monkeypatch):
    """configurable.thinking_enabled 透传给 create_chat_model。"""
    seen: dict = {}
    _stub_lead_agent_dependencies(
        monkeypatch,
        create_chat_model=lambda **k: seen.update(k) or "fake-model",
    )

    config = {"configurable": {"model_name": "m", "thinking_enabled": True}}
    agent_module.make_lead_agent(config)

    assert seen["thinking_enabled"] is True
