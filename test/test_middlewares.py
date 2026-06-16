"""中间件装配测试。

hermetic：``build_middlewares(app_config=...)`` 接受显式配置，注入 AppConfig 即可
不读全局 config.yaml。验证装配顺序、条件开关、自定义中间件插入位置。
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.middlewares import build_middlewares
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware
from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.config import AppConfig


def _names(mws):
    return [type(m).__name__ for m in mws]


def test_default_assembly_has_core_middlewares():
    """默认装配包含核心中间件。"""
    mws = build_middlewares(app_config=AppConfig())
    names = _names(mws)
    assert "LLMErrorHandlingMiddleware" in names
    assert "DynamicContextMiddleware" in names
    assert "ToolErrorHandlingMiddleware" in names
    # 默认开关全开
    assert "TitleMiddleware" in names
    assert "MemoryMiddleware" in names
    assert "LoopDetectionMiddleware" in names


def test_clarification_always_last():
    """ClarificationMiddleware 必须排在末位（红线 #14）。"""
    mws = build_middlewares(app_config=AppConfig())
    assert isinstance(mws[-1], ClarificationMiddleware)


def test_custom_middlewares_inserted_before_clarification():
    """custom_middlewares 插在 Clarification 之前。"""

    class MyMiddleware(AgentMiddleware):
        pass

    mws = build_middlewares(app_config=AppConfig(), custom_middlewares=[MyMiddleware()])
    assert isinstance(mws[-1], ClarificationMiddleware)
    assert isinstance(mws[-2], MyMiddleware)


def test_title_disabled_omits_title_middleware():
    """title.enabled=False → 不挂 TitleMiddleware。"""
    cfg = AppConfig(title={"enabled": False})
    mws = build_middlewares(app_config=cfg)
    assert not any(isinstance(m, TitleMiddleware) for m in mws)
    assert isinstance(mws[-1], ClarificationMiddleware)


def test_memory_disabled_omits_memory_middleware():
    """memory.enabled=False → 不挂 MemoryMiddleware。"""
    cfg = AppConfig(memory={"enabled": False})
    mws = build_middlewares(app_config=cfg)
    assert not any(isinstance(m, MemoryMiddleware) for m in mws)


def test_loop_detection_disabled_omits_loop_middleware():
    """loop_detection.enabled=False → 不挂 LoopDetectionMiddleware。"""
    cfg = AppConfig(loop_detection={"enabled": False})
    mws = build_middlewares(app_config=cfg)
    assert not any(isinstance(m, LoopDetectionMiddleware) for m in mws)
