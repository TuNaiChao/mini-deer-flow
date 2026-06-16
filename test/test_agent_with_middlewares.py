"""带中间件的 Agent 装配测试。

hermetic：验证 ``create_deerflow_agent`` 能接收 ``build_middlewares`` 产出的真实
中间件链并原样传递给 langchain 的 ``create_agent``（后者桩化为记录器），不真正
编译图。聚焦"中间件链 → agent 工厂"的衔接，与 test_agent.py 的工厂逻辑互补。
"""

from __future__ import annotations

from deerflow.agents import create_deerflow_agent
from deerflow.agents import factory as factory_module
from deerflow.agents.middlewares import build_middlewares
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.config import AppConfig


def test_create_deerflow_agent_accepts_real_middleware_chain(monkeypatch):
    """build_middlewares 的产出能直接作为 middleware 传给 create_deerflow_agent。"""
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")

    mws = build_middlewares(app_config=AppConfig())
    create_deerflow_agent(model=object(), middleware=mws)

    assert captured["middleware"] is mws
    assert isinstance(captured["middleware"][-1], ClarificationMiddleware)


def test_create_deerflow_agent_preserves_middleware_order(monkeypatch):
    """中间件顺序在传递过程中保持不变。"""
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")

    mws = build_middlewares(app_config=AppConfig())
    names_before = [type(m).__name__ for m in mws]
    create_deerflow_agent(model=object(), middleware=mws)
    names_after = [type(m).__name__ for m in captured["middleware"]]

    assert names_before == names_after
