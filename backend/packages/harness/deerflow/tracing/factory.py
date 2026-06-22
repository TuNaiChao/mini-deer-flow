"""追踪回调工厂。

:func:`build_tracing_callbacks` 按当前显式启用的 provider（环境变量
``LANGSMITH_TRACING`` / ``LANGFUSE_TRACING`` 控制）构造 LangChain 回调列表。

设计要点：
- **未配置返回空列表**（零开销）：未设上述环境变量时直接返回 ``[]``，调用方无感。
- **图根注入**：回调由 lead agent 工厂 / 运行 worker 在图调用前 append 进
  ``config["callbacks"]``，让单次 run 产出一条完整 trace。**图内** 的
  :func:`deerflow.models.create_chat_model` 一律 ``attach_tracing=False``，避免重复 span
  （红线 #17）。
- **独立调用方**（如图外的 MemoryUpdater）用 ``attach_tracing=True``，模型级挂回调（见
  [models/factory.py](../models/factory.py) ``_maybe_build_tracing_callbacks`` 的懒导入）。
- 构造失败响亮报错（包成 ``RuntimeError``），不静默吞——追踪是可观测性，坏了得知道。
"""

from __future__ import annotations

from typing import Any

from deerflow.config import (
    get_enabled_tracing_providers,
    get_tracing_config,
    validate_enabled_tracing_providers,
)


def _create_langsmith_tracer(config) -> Any:
    """构造 LangSmith tracer（延迟导入 langchain_core，构造不发网络）。"""
    from langchain_core.tracers.langchain import LangChainTracer

    return LangChainTracer(project_name=config.project)


def _create_langfuse_handler(config) -> Any:
    """构造 Langfuse LangChain 回调处理器。

    langfuse>=4 经 client 单例初始化项目级凭证；LangChain 回调再挂到那个已配置的 client 上。
    """
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler as LangfuseCallbackHandler

    Langfuse(
        secret_key=config.secret_key,
        public_key=config.public_key,
        host=config.host,
    )
    return LangfuseCallbackHandler(public_key=config.public_key)


def build_tracing_callbacks() -> list[Any]:
    """为所有显式启用的追踪 provider 构造回调。

    先 :func:`validate_enabled_tracing_providers` 校验凭证齐全（缺则 ``ValueError``）；
    再逐 provider 构造，构造异常包成 ``RuntimeError``。未启用任何 provider 时返回 ``[]``。
    """
    validate_enabled_tracing_providers()
    enabled_providers = get_enabled_tracing_providers()
    if not enabled_providers:
        return []

    tracing_config = get_tracing_config()
    callbacks: list[Any] = []

    for provider in enabled_providers:
        if provider == "langsmith":
            try:
                callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
            except Exception as exc:  # pragma: no cover - hermetic 测试用 monkeypatch 覆盖
                raise RuntimeError(f"LangSmith tracing initialization failed: {exc}") from exc
        elif provider == "langfuse":
            try:
                callbacks.append(_create_langfuse_handler(tracing_config.langfuse))
            except Exception as exc:  # pragma: no cover - hermetic 测试用 monkeypatch 覆盖
                raise RuntimeError(f"Langfuse tracing initialization failed: {exc}") from exc

    return callbacks
