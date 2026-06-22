"""provider 安全终止时抑制工具执行（M16，issue bytedance/deer-flow#3028）。

背景：部分 provider（OpenAI ``finish_reason='content_filter'``、Anthropic
``stop_reason='refusal'``、Gemini ``finish_reason='SAFETY'``）会在流中途停掉生成，
**但仍返回半成形的 ``tool_calls``**。LangChain 的工具路由把任何带非空 ``tool_calls`` 的
AIMessage 当「去执行这些」，于是截断的参数（如 markdown ``write_file`` 写到一半）被当成完整
派发——agent 看到截断文件 → 试图修 → 又被滤 → 死循环。

本中间件在 ``after_model`` 门控：检测器命中**且** AIMessage 带 tool_calls 时，剥掉 tool_calls
（结构化 + raw provider payload）、追加用户可读说明、把可观测字段塞进
``additional_kwargs.safety_termination``，让日志 / trace / SSE 消费者看到发生了什么。

Hook 选 ``after_model``（非 ``wrap_model_call``）：响应是正常返回（非异常），要和
``LoopDetectionMiddleware`` 同处 after-model 链（共享工具调用抑制机制，但触发条件不同）。

注册位置：在 ``LoopDetectionMiddleware`` **之后**。LangChain 工厂按倒序列表序接 after_model
边——最后注册的最先观察模型输出。Safety 在 Loop 后注册 → Safety 先看原始响应 → 命中则清
tool_calls → Loop 再对清理后的消息计数，不误触警。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.safety_termination_detectors import (
    SafetyTermination,
    SafetyTerminationDetector,
    default_detectors,
)
from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls

if TYPE_CHECKING:
    from deerflow.config.safety_finish_reason_config import SafetyFinishReasonConfig

logger = logging.getLogger(__name__)

_USER_FACING_MESSAGE = (
    "The model provider stopped this response with a safety-related signal "
    "({reason_field}={reason_value!r}, detector={detector!r}). Any tool "
    "calls produced in this turn were suppressed because their arguments "
    "may be truncated and unsafe to execute. Please rephrase the request "
    "or ask for a narrower output."
)


class SafetyFinishReasonMiddleware(AgentMiddleware[AgentState]):
    """检测器命中时剥掉 AIMessage 的 tool_calls。"""

    def __init__(self, detectors: list[SafetyTerminationDetector] | None = None) -> None:
        super().__init__()
        # 拷一份，防构造后调用方改动泄漏进来。
        self._detectors: list[SafetyTerminationDetector] = list(detectors) if detectors else default_detectors()

    @classmethod
    def from_config(cls, config: SafetyFinishReasonConfig) -> SafetyFinishReasonMiddleware:
        """从已校验的 Pydantic config 构造，自定义检测器列表经 reflection 加载。

        显式空列表被拒——会静默禁检测却把中间件留在链里（最差的两头不讨好）。用
        ``enabled: false`` 关中间件。
        """
        if config.detectors is None:
            return cls()

        if not config.detectors:
            raise ValueError("safety_finish_reason.detectors must be omitted (use built-ins) or contain at least one entry; use enabled=false to disable the middleware entirely.")

        from deerflow.reflection import resolve_variable

        detectors: list[SafetyTerminationDetector] = []
        for entry in config.detectors:
            detector_cls = resolve_variable(entry.use)
            kwargs = dict(entry.config) if entry.config else {}
            detector = detector_cls(**kwargs)
            if not isinstance(detector, SafetyTerminationDetector):
                raise TypeError(f"{entry.use} did not produce a SafetyTerminationDetector (got {type(detector).__name__}); ensure it has a `name` attribute and a `detect(message)` method")
            detectors.append(detector)
        return cls(detectors=detectors)

    # ----- 检测 ----------------------------------------------------------- #

    def _detect(self, message: AIMessage) -> SafetyTermination | None:
        for detector in self._detectors:
            try:
                hit = detector.detect(message)
            except Exception:  # noqa: BLE001 - 坏检测器不能拖垮 agent run
                logger.exception("SafetyTerminationDetector %r raised; treating as no-match", getattr(detector, "name", type(detector).__name__))
                continue
            if hit is not None:
                return hit
        return None

    # ----- 消息改写 ------------------------------------------------------- #

    @staticmethod
    def _append_user_message(content: object, text: str) -> str | list:
        """把纯文本说明追加到 AIMessage content（list-content 保结构，对齐 LoopDetection）。"""
        if content is None or content == "":
            return text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": f"\n\n{text}"}]
        if isinstance(content, str):
            return content + f"\n\n{text}"
        return str(content) + f"\n\n{text}"

    def _build_suppressed_message(self, message: AIMessage, termination: SafetyTermination) -> AIMessage:
        suppressed_names = [tc.get("name") or "unknown" for tc in (message.tool_calls or [])]
        explanation = _USER_FACING_MESSAGE.format(
            reason_field=termination.reason_field,
            reason_value=termination.reason_value,
            detector=termination.detector,
        )
        new_content = self._append_user_message(message.content, explanation)

        # clone_ai_message_with_tool_calls 一次清掉结构化 tool_calls、raw additional_kwargs.tool_calls、function_call。
        # 它只在旧值是 "tool_calls" 时改 finish_reason——本场景不是（content_filter/refusal/SAFETY 保留），
        # 让下游 SSE / converter 看到真实 provider 原因。
        cleared = clone_ai_message_with_tool_calls(message, [], content=new_content)

        # 再 clone additional_kwargs 防 model_copy 引用 clone 返回的 dict；贴可观测记录。
        kwargs = dict(getattr(cleared, "additional_kwargs", None) or {})
        kwargs["safety_termination"] = {
            "detector": termination.detector,
            "reason_field": termination.reason_field,
            "reason_value": termination.reason_value,
            "suppressed_tool_call_count": len(suppressed_names),
            "suppressed_tool_call_names": suppressed_names,
            "extras": dict(termination.extras) if termination.extras else {},
        }
        return cleared.model_copy(update={"additional_kwargs": kwargs})

    # ----- 可观测性 ------------------------------------------------------- #

    def _emit_event(self, termination: SafetyTermination, suppressed_names: list[str], runtime: Runtime) -> None:
        """通知 SSE 消费者（如 web UI）某工具轮被抑制，让它对账已流的「tool starting...」占位。

        失败 debug 记录忽略——尽力而为的信号。
        """
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
        except Exception:  # noqa: BLE001
            logger.debug("get_stream_writer unavailable; skipping safety_termination event", exc_info=True)
            return

        thread_id = None
        if runtime is not None and getattr(runtime, "context", None):
            thread_id = runtime.context.get("thread_id") if isinstance(runtime.context, dict) else None

        try:
            writer(
                {
                    "type": "safety_termination",
                    "detector": termination.detector,
                    "reason_field": termination.reason_field,
                    "reason_value": termination.reason_value,
                    "suppressed_tool_call_count": len(suppressed_names),
                    "suppressed_tool_call_names": suppressed_names,
                    "thread_id": thread_id,
                }
            )
        except Exception:  # noqa: BLE001
            logger.debug("Failed to emit safety_termination stream event", exc_info=True)

    def _record_audit_event(self, termination: SafetyTermination, message, tool_calls: list[dict], runtime: Runtime) -> None:
        """写 ``middleware:safety_termination`` 记录到 RunEventStore 供事后审计。

        ``_emit_event`` 的流事件只给在线 SSE 客户端、run 后消失；本事件**持久化**，运维能一条
        SQL 查「今天哪些 run 被安全抑制」而不用 join 消息体。worker 经 ``runtime.context["__run_journal"]``
        暴露 run 级 RunJournal；单测 / 子代理 / 无事件存储路径下缺失 → 静默跳过。

        工具**参数**刻意不记——那正是 provider 滤掉的内容；记下来等于绕过安全滤镜。名字 / 数 / id
        足够审计和调试。
        """
        journal = None
        if runtime is not None and getattr(runtime, "context", None):
            context = runtime.context
            if isinstance(context, dict):
                journal = context.get("__run_journal")
        if journal is None:
            return

        suppressed_names = [tc.get("name") or "unknown" for tc in tool_calls]
        suppressed_ids = [tc.get("id") for tc in tool_calls if tc.get("id")]

        changes = {
            "detector": termination.detector,
            "reason_field": termination.reason_field,
            "reason_value": termination.reason_value,
            "suppressed_tool_call_count": len(tool_calls),
            "suppressed_tool_call_names": suppressed_names,
            "suppressed_tool_call_ids": suppressed_ids,
            "message_id": getattr(message, "id", None),
            "extras": dict(termination.extras) if termination.extras else {},
        }

        try:
            journal.record_middleware(
                tag="safety_termination",
                name=type(self).__name__,
                hook="after_model",
                action="suppress_tool_calls",
                changes=changes,
            )
        except Exception:  # noqa: BLE001
            # 审计事件持久化绝不能拖垮 agent 执行。
            logger.debug("Failed to record middleware:safety_termination event", exc_info=True)

    # ----- 主 apply ------------------------------------------------------- #

    def _apply(self, state: AgentState, runtime: Runtime) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None

        # 仅在有东西可抑制时介入。``content_filter`` 但无 tool_calls 时原样放行，让部分文本响应
        # 自然到达用户。
        tool_calls = last.tool_calls
        if not tool_calls:
            return None

        termination = self._detect(last)
        if termination is None:
            return None

        patched = self._build_suppressed_message(last, termination)

        thread_id = None
        if runtime is not None and getattr(runtime, "context", None):
            thread_id = runtime.context.get("thread_id") if isinstance(runtime.context, dict) else None

        logger.warning(
            "Provider safety termination detected — suppressed %d tool call(s)",
            len(tool_calls),
            extra={
                "thread_id": thread_id,
                "detector": termination.detector,
                "reason_field": termination.reason_field,
                "reason_value": termination.reason_value,
                "suppressed_tool_call_names": [tc.get("name") for tc in tool_calls],
            },
        )

        self._emit_event(termination, [tc.get("name") or "unknown" for tc in tool_calls], runtime)
        self._record_audit_event(termination, last, list(tool_calls), runtime)

        return {"messages": [patched]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._apply(state, runtime)
