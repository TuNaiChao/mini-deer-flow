"""Langfuse trace 属性元数据构造器。

Langfuse v4 的 ``langchain.CallbackHandler`` 从 ``RunnableConfig.metadata`` 里取一组**保留键**
提升到根 trace 上：

- ``langfuse_session_id`` → 分组（LangGraph thread → Langfuse Session）
- ``langfuse_user_id``    → trace 的 user_id（驱动 Users 页）
- ``langfuse_trace_name`` → 人可读的 trace 名
- ``langfuse_tags``       → trace 标签

见 ``langfuse/langchain/CallbackHandler.py::_parse_langfuse_trace_attributes`` 与
https://langfuse.com/docs/observability/features/sessions 的契约。本模块的构造器让
gateway / 运行 worker 能注入正确的 metadata，而不把 Langfuse 内部细节漏到调用点。
"""

from __future__ import annotations

from typing import Any

from deerflow.config import get_enabled_tracing_providers

# 默认 trace 名（无 agent 标识时）。
_DEFAULT_TRACE_NAME = "lead-agent"


def build_langfuse_trace_metadata(
    *,
    thread_id: str | None,
    user_id: str | None = None,
    assistant_id: str | None = None,
    model_name: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """构造供 ``RunnableConfig.metadata`` 用的 Langfuse trace 属性元数据。

    Langfuse 不在启用 provider 里时返回 ``{}``，让调用方可以无条件 merge 结果而不影响
    LangSmith 或其它 tracer。

    Args:
        thread_id: LangGraph thread id；映射到 ``langfuse_session_id``。
        user_id: 有效 user id；``None`` 时回退 ``DEFAULT_USER_ID``，让无鉴权模式下
            Langfuse Users 页仍可用。
        assistant_id: 可选 agent 标识；默认 ``"lead-agent"``。
        model_name: 模型名；以 ``model:<name>`` 进 ``langfuse_tags``。
        environment: 部署环境（如 ``"production"``）；以 ``env:<value>`` 进 ``langfuse_tags``。
    """
    if "langfuse" not in get_enabled_tracing_providers():
        return {}

    # 延迟导入避免循环：deerflow.runtime 急切导入运行 worker，后者需要 deerflow.tracing。
    from deerflow.runtime.user_context import DEFAULT_USER_ID

    metadata: dict[str, Any] = {
        "langfuse_session_id": thread_id,
        "langfuse_user_id": user_id or DEFAULT_USER_ID,
        "langfuse_trace_name": assistant_id or _DEFAULT_TRACE_NAME,
    }

    tags: list[str] = []
    if environment:
        tags.append(f"env:{environment}")
    if model_name:
        tags.append(f"model:{model_name}")
    if tags:
        metadata["langfuse_tags"] = tags

    return metadata


def inject_langfuse_metadata(
    config: dict,
    *,
    thread_id: str | None,
    user_id: str | None = None,
    assistant_id: str | None = None,
    model_name: str | None = None,
    environment: str | None = None,
) -> None:
    """把 Langfuse trace 属性元数据 merge 进 ``config["metadata"]``。

    gateway worker（``runtime/runs/worker.py``）与嵌入式 client（``client.py``）共用，让两条
    路径不漂移。

    调用方提供的 metadata 经 ``setdefault`` 优先——例如前端设的 ``langfuse_session_id`` 不被覆盖。
    ``config`` 字典就地修改；Langfuse 不在启用 provider 时是 no-op。
    """
    langfuse_metadata = build_langfuse_trace_metadata(
        thread_id=thread_id,
        user_id=user_id,
        assistant_id=assistant_id,
        model_name=model_name,
        environment=environment,
    )
    if not langfuse_metadata:
        return

    merged_metadata = dict(config.get("metadata") or {})
    for key, value in langfuse_metadata.items():
        merged_metadata.setdefault(key, value)
    config["metadata"] = merged_metadata
