"""工具错误处理中间件 + 共享运行时中间件构造器（M16）。

两件事：

1. :class:`ToolErrorHandlingMiddleware`：``wrap_tool_call`` 捕工具异常 → 错误 ToolMessage，
   run 不因单个工具失败中止。同时给 ``task`` 工具返回贴结构化子代理状态
   （``additional_kwargs.subagent_status``，issue bytedance/deer-flow#3146）——前端从结构化
   字段读状态而非解析 task 返回串的前缀，契约在此处一处落实，防「新增返回路径忘贴」漂移。

2. 三个工厂：``_build_runtime_middlewares``（lead/subagent 共享前置段）/
   ``build_lead_runtime_middlewares`` / ``build_subagent_runtime_middlewares``。把 lead 和
   subagent 都需要的中间件（InputSanitization / ToolOutputBudget / Uploads[仅 lead] / ThreadData
   / Sandbox / DanglingToolCall / LLMErrorHandling / SandboxAudit / ToolErrorHandling）集中装配。

红线 #15：``wrap_tool_call`` 里 ``handler`` 抛 ``GraphBubbleUp`` 必须 ``raise`` 原样上抛——
否则 ClarificationMiddleware 的中断、subagent 的 interrupt 都会被下面的 ``except Exception`` 吞掉。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.config.app_config import AppConfig
from deerflow.subagents.status_contract import (
    extract_subagent_status,
    make_subagent_additional_kwargs,
)

if TYPE_CHECKING:
    from deerflow.tools.builtins.tool_search import DeferredToolSetup

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_TASK_TOOL_NAME = "task"


def _stamp_task_subagent_status(message: ToolMessage, *, tool_name: str, error: str | None = None) -> ToolMessage:
    """集中给 ``additional_kwargs.subagent_status`` 贴标。

    issue #3146：前端从结构化字段读子代理状态而非解析 task 返回串前缀。契约在此处一处落实
    （每条 task 结果都过这里），而非散在 task_tool.py 的 5 个正常返回 + 3 个 ``Error:`` 分支里。

    非 task 工具 no-op，不碰别的工具的 additional_kwargs 约定。
    """
    if tool_name != _TASK_TOOL_NAME:
        return message
    content = message.content if isinstance(message.content, str) else ""
    status = extract_subagent_status(content)
    if status is None:
        # 非终态流块 / 未识别形状 → 不设字段，前端保留进行中占位直到真终态帧。
        return message
    stamp = make_subagent_additional_kwargs(status, error=error)
    existing = dict(message.additional_kwargs or {})
    existing.update(stamp)
    message.additional_kwargs = existing
    return message


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """工具异常 → 错误 ToolMessage，run 继续。"""

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."

        content = f"Error: Tool '{tool_name}' failed with {exc.__class__.__name__}: {detail}. Continue with available context, or choose an alternative tool."
        message = ToolMessage(content=content, tool_call_id=tool_call_id, name=tool_name, status="error")
        # 包装器也贴结构化子代理状态：否则前端得回退到前缀匹配 ``Error: Tool 'task' failed ...``。
        structured_error = f"{exc.__class__.__name__}: {detail}"
        return _stamp_task_subagent_status(message, tool_name=tool_name, error=structured_error)

    @staticmethod
    def _maybe_stamp(result: ToolMessage | Command, request: ToolCallRequest) -> ToolMessage | Command:
        """给成功的 task 工具返回贴子代理状态标。

        ``Command`` 结果跳过——它编码 LangGraph 控制流而非用户可见工具输出。
        """
        if not isinstance(result, ToolMessage):
            return result
        tool_name = str(request.tool_call.get("name") or "")
        return _stamp_task_subagent_status(result, tool_name=tool_name)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            result = handler(request)
        except GraphBubbleUp:
            # 保留 LangGraph 控制流信号（interrupt/pause/resume）。
            raise
        except Exception as exc:
            logger.exception("Tool execution failed (sync): name=%s id=%s", request.tool_call.get("name"), request.tool_call.get("id"))
            return self._build_error_message(request, exc)
        return self._maybe_stamp(result, request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            result = await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception("Tool execution failed (async): name=%s id=%s", request.tool_call.get("name"), request.tool_call.get("id"))
            return self._build_error_message(request, exc)
        return self._maybe_stamp(result, request)


def _build_runtime_middlewares(
    *,
    app_config: AppConfig,
    include_uploads: bool,
    include_dangling_tool_call_patch: bool,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """构造 lead/subagent 共享的前 9 步中间件。

    Guardrail（``app_config.guardrails``）mini 标真正可选未做——此处不留分支，跳过该步。
    """
    from deerflow.agents.middlewares.input_sanitization_middleware import InputSanitizationMiddleware
    from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
    from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
    from deerflow.agents.middlewares.tool_output_budget_middleware import ToolOutputBudgetMiddleware
    from deerflow.sandbox.middleware import SandboxMiddleware

    # 顺序对齐上游 build_lead_runtime_middlewares（mini 跳过 #8 Guardrail）：
    #   InputSanitization(#1, 最外层 wrap_model_call) → ToolOutputBudget(#2) → Uploads(#3, 仅 lead)
    #   → ThreadData(#4) → Sandbox(#5) → DanglingToolCall(#6) → LLMErrorHandling(#7)
    #   → [Guardrail(#8) 跳过] → SandboxAudit(#9) → ToolErrorHandling(#10)。
    #
    # InputSanitization 必须第一：它是包在最外层的 wrap_model_call，所有内层中间件（含
    # LLMErrorHandling 的重试）看到的都是已净化消息（提示词注入标签被转义）。
    # Uploads 从 runtime.context 取 thread_id、自己解析 uploads_dir（不读 thread_data state），
    # 故与 ThreadData 无硬先后依赖——Uploads 提前到 ThreadData 之前，对齐上游顺序。
    middlewares: list[AgentMiddleware] = [InputSanitizationMiddleware(), ToolOutputBudgetMiddleware.from_app_config(app_config)]

    if include_uploads:
        from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware

        # 排在 ThreadData 之前、ToolOutputBudget 之后（对齐上游）。
        middlewares.append(UploadsMiddleware())

    middlewares.append(ThreadDataMiddleware(lazy_init=lazy_init))

    middlewares.append(SandboxMiddleware(lazy_init=lazy_init))

    if include_dangling_tool_call_patch:
        from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware

        middlewares.append(DanglingToolCallMiddleware())

    middlewares.append(LLMErrorHandlingMiddleware(app_config=app_config))

    # Guardrail 中间件（真正可选）：mini 未引入 guardrails 独立模块，此处跳过（#8）。

    from deerflow.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware

    middlewares.append(SandboxAuditMiddleware())
    middlewares.append(ToolErrorHandlingMiddleware())
    return middlewares


def build_lead_runtime_middlewares(*, app_config: AppConfig, lazy_init: bool = True) -> list[AgentMiddleware]:
    """lead agent 与 lead-only 中间件共享的前置中间件。"""
    return _build_runtime_middlewares(
        app_config=app_config,
        include_uploads=True,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )


def build_subagent_runtime_middlewares(
    *,
    app_config: AppConfig | None = None,
    model_name: str | None = None,
    lazy_init: bool = True,
    deferred_setup: DeferredToolSetup | None = None,
) -> list[AgentMiddleware]:
    """subagent 运行时共享的前置中间件 + subagent 专属（vision / deferred / safety）。"""
    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()

    middlewares = _build_runtime_middlewares(
        app_config=app_config,
        include_uploads=False,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )

    if model_name is None and app_config.models:
        model_name = app_config.models[0].name

    model_config = app_config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

        middlewares.append(ViewImageMiddleware())

    # subagent 也隐藏延迟 MCP 工具 schema 直到 tool_search 提升（同 lead）。空 setup（延迟
    # 未启用 / 无 MCP 工具存活）纯 no-op。
    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

        middlewares.append(DeferredToolFilterMiddleware(deferred_setup.deferred_names, deferred_setup.catalog_hash))

    # subagent 同样暴露在 provider 安全终止（content_filter 等）下，坏调用会经 task 结果传回 lead。
    safety_config = app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware

        middlewares.append(SafetyFinishReasonMiddleware.from_config(safety_config))

    return middlewares
