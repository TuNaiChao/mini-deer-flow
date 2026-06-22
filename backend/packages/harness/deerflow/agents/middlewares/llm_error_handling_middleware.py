"""LLM 错误处理中间件（M16 重做）：重试 / 退避 / 熔断 + 用户可读兜底。

包装模型调用：瞬时错误（限流 / 超时 / 过载 / 可重试状态码）指数退避重试；认证 / 配额 /
致命错误及重试耗尽 → 返回兜底 AIMessage，agent 优雅降级而非崩溃。

相较 v1.1 教学版，本次对齐 deer：
  - **错误分类细化**：quota / auth / transient / busy / generic，按类给不同用户文案。
  - **熔断器**（``CircuitBreakerConfig``）：连续失败达阈值 → 短路返回兜底（半开探测恢复），
    防持续打挂的 provider 被逐请求退避放大噪声。
  - **``GraphBubbleUp`` 透传**（红线 #15）：handler 抛 LangGraph 控制流信号必须原样上抛，
    否则 interrupt/pause/resume 失效。
  - **流断错误专门文案**（``StreamChunkTimeoutError`` 等）：单次超大 payload 卡流，引导用户
    「拆小请求」而非干等重试。
  - **Retry-After / per-exception 预算**：尊重 provider 的 retry-after 头；流超时只重试一次。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_BUSY_PATTERNS = (
    "server busy",
    "temporarily unavailable",
    "try again later",
    "please retry",
    "please try again",
    "overloaded",
    "high demand",
    "rate limit",
    "负载较高",
    "服务繁忙",
    "稍后重试",
    "请稍后重试",
)
_QUOTA_PATTERNS = (
    "insufficient_quota",
    "quota",
    "billing",
    "credit",
    "payment",
    "余额不足",
    "超出限额",
    "额度不足",
    "欠费",
)
_AUTH_PATTERNS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "permission",
    "forbidden",
    "access denied",
    "无权",
    "未授权",
)

# per-exception 重试预算覆盖。
# StreamChunkTimeoutError 在 upstream provider 已 stall ``stream_chunk_timeout`` 秒（通常
# 120-240s）后才触发；完整 3 次循环会叠 6-12 分钟死寂。保留恰好一次重试（catch 真 transient
# TCP blip）后快速失败。key 是异常类**名**（不引类），值是绝对最大尝试次数。
_RETRY_BUDGET_OVERRIDES: dict[str, int] = {"StreamChunkTimeoutError": 2}

# 指示 upstream stream-chunk 看门狗因模型中途 stall 触发的异常类名。这些值得更具体的用户文案
# （「拆小请求」而非「干等重试」）。通用连接断（httpx RemoteProtocolError/ReadError）刻意排除。
_STREAM_DROP_EXCEPTIONS: frozenset[str] = frozenset({"StreamChunkTimeoutError"})


class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """重试瞬时 LLM 错误并给优雅的助手消息。"""

    retry_max_attempts: int = 3
    retry_base_delay_ms: int = 1000
    retry_cap_delay_ms: int = 8000

    def __init__(self, *, app_config: AppConfig, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.circuit_failure_threshold = app_config.circuit_breaker.failure_threshold
        self.circuit_recovery_timeout_sec = app_config.circuit_breaker.recovery_timeout_sec

        # 熔断器状态。
        self._circuit_lock = threading.Lock()
        self._circuit_failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_state = "closed"
        self._circuit_probe_in_flight = False

    def _max_attempts_for(self, exc: BaseException) -> int:
        override = _RETRY_BUDGET_OVERRIDES.get(type(exc).__name__)
        if override is None:
            return self.retry_max_attempts
        return min(override, self.retry_max_attempts)

    def _check_circuit(self) -> bool:
        """熔断器 OPEN（快速失败）→ True。"""
        with self._circuit_lock:
            now = time.time()

            if self._circuit_state == "open":
                if now < self._circuit_open_until:
                    return True
                self._circuit_state = "half_open"
                self._circuit_probe_in_flight = False

            if self._circuit_state == "half_open":
                if self._circuit_probe_in_flight:
                    return True
                self._circuit_probe_in_flight = True
                return False

            return False

    def _record_success(self) -> None:
        with self._circuit_lock:
            if self._circuit_state != "closed" or self._circuit_failure_count > 0:
                logger.info("Circuit breaker reset (Closed). LLM service recovered.")
            self._circuit_failure_count = 0
            self._circuit_open_until = 0.0
            self._circuit_state = "closed"
            self._circuit_probe_in_flight = False

    def _record_failure(self) -> None:
        with self._circuit_lock:
            if self._circuit_state == "half_open":
                self._circuit_open_until = time.time() + self.circuit_recovery_timeout_sec
                self._circuit_state = "open"
                self._circuit_probe_in_flight = False
                logger.error("Circuit breaker probe failed (Open). Will probe again after %ds.", self.circuit_recovery_timeout_sec)
                return

            self._circuit_failure_count += 1
            if self._circuit_failure_count >= self.circuit_failure_threshold:
                self._circuit_open_until = time.time() + self.circuit_recovery_timeout_sec
                if self._circuit_state != "open":
                    self._circuit_state = "open"
                    self._circuit_probe_in_flight = False
                    logger.error(
                        "Circuit breaker tripped (Open). Threshold reached (%d). Will probe after %ds.",
                        self._circuit_failure_count,
                        self.circuit_recovery_timeout_sec,
                    )

    def _classify_error(self, exc: BaseException) -> tuple[bool, str]:
        detail = _extract_error_detail(exc)
        lowered = detail.lower()
        error_code = _extract_error_code(exc)
        status_code = _extract_status_code(exc)

        if _matches_any(lowered, _QUOTA_PATTERNS) or _matches_any(str(error_code).lower(), _QUOTA_PATTERNS):
            return False, "quota"
        if _matches_any(lowered, _AUTH_PATTERNS):
            return False, "auth"

        exc_name = exc.__class__.__name__
        if exc_name in {
            "APITimeoutError",
            "APIConnectionError",
            "InternalServerError",
            "ReadError",  # httpx.ReadError: 连接中途断
            "RemoteProtocolError",  # httpx: server 意外关连接
            "StreamChunkTimeoutError",  # langchain-openai: chunk gap 超 stream_chunk_timeout
        }:
            return True, "transient"
        if status_code in _RETRIABLE_STATUS_CODES:
            return True, "transient"
        if _matches_any(lowered, _BUSY_PATTERNS):
            return True, "busy"

        return False, "generic"

    def _build_retry_delay_ms(self, attempt: int, exc: BaseException) -> int:
        retry_after = _extract_retry_after_ms(exc)
        if retry_after is not None:
            return retry_after
        backoff = self.retry_base_delay_ms * (2 ** max(0, attempt - 1))
        return min(backoff, self.retry_cap_delay_ms)

    def _build_retry_message(self, attempt: int, wait_ms: int, reason: str) -> str:
        seconds = max(1, round(wait_ms / 1000))
        reason_text = "provider is busy" if reason == "busy" else "provider request failed temporarily"
        return f"LLM request retry {attempt}/{self.retry_max_attempts}: {reason_text}. Retrying in {seconds}s."

    def _build_circuit_breaker_message(self) -> str:
        return "The configured LLM provider is currently unavailable due to continuous failures. Circuit breaker is engaged to protect the system. Please wait a moment before trying again."

    def _build_error_fallback_message(self, content: str, *, error_type: str, reason: str, detail: str) -> AIMessage:
        return AIMessage(
            content=content,
            additional_kwargs={
                "deerflow_error_fallback": True,
                "error_type": error_type,
                "error_reason": reason,
                "error_detail": detail,
            },
        )

    def _build_user_message(self, exc: BaseException, reason: str) -> str:
        detail = _extract_error_detail(exc)
        if reason == "quota":
            return "The configured LLM provider rejected the request because the account is out of quota, billing is unavailable, or usage is restricted. Please fix the provider account and try again."
        if reason == "auth":
            return "The configured LLM provider rejected the request because authentication or access is invalid. Please check the provider credentials and try again."
        if reason in {"busy", "transient"}:
            # 流断失败（chunk-gap 超时 / peer-closed / read error）几乎都指向单次超大 tool-call
            # payload——模型序列化 JSON 参数太久，upstream 缓冲、stream gap 超 stream_chunk_timeout。
            # 给这个独立原因，让用户拆小请求而非无助重试同一 prompt。
            if type(exc).__name__ in _STREAM_DROP_EXCEPTIONS:
                return (
                    "The model's streaming response was interrupted before it could "
                    "finish. This usually happens when a single response or tool call "
                    "is very large — please ask the assistant to split the work into "
                    "smaller steps, or shorten the requested output, and try again."
                )
            return "The configured LLM provider is temporarily unavailable after multiple retries. Please wait a moment and continue the conversation."
        return f"LLM request failed: {detail}"

    def _build_user_fallback_message(self, exc: BaseException, reason: str) -> AIMessage:
        return self._build_error_fallback_message(
            self._build_user_message(exc, reason),
            error_type=type(exc).__name__,
            reason=reason,
            detail=_extract_error_detail(exc),
        )

    def _emit_retry_event(self, attempt: int, wait_ms: int, reason: str) -> None:
        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            writer(
                {
                    "type": "llm_retry",
                    "attempt": attempt,
                    "max_attempts": self.retry_max_attempts,
                    "wait_ms": wait_ms,
                    "reason": reason,
                    "message": self._build_retry_message(attempt, wait_ms, reason),
                }
            )
        except Exception:
            logger.debug("Failed to emit llm_retry event", exc_info=True)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._check_circuit():
            return self._build_error_fallback_message(
                self._build_circuit_breaker_message(),
                error_type="CircuitBreakerOpen",
                reason="circuit_open",
                detail="LLM circuit breaker is open",
            )

        attempt = 1
        while True:
            try:
                response = handler(request)
                self._record_success()
                return response
            except GraphBubbleUp:
                # 保留 LangGraph 控制流信号（interrupt/pause/resume）。
                with self._circuit_lock:
                    if self._circuit_state == "half_open":
                        self._circuit_probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                max_attempts = self._max_attempts_for(exc)
                if retriable and attempt < max_attempts:
                    wait_ms = self._build_retry_delay_ms(attempt, exc)
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: %s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        _extract_error_detail(exc),
                    )
                    self._emit_retry_event(attempt, wait_ms, reason)
                    time.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning("LLM call failed after %d attempt(s): %s", attempt, _extract_error_detail(exc), exc_info=exc)
                if retriable:
                    self._record_failure()
                return self._build_user_fallback_message(exc, reason)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._check_circuit():
            return self._build_error_fallback_message(
                self._build_circuit_breaker_message(),
                error_type="CircuitBreakerOpen",
                reason="circuit_open",
                detail="LLM circuit breaker is open",
            )

        attempt = 1
        while True:
            try:
                response = await handler(request)
                self._record_success()
                return response
            except GraphBubbleUp:
                with self._circuit_lock:
                    if self._circuit_state == "half_open":
                        self._circuit_probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)
                max_attempts = self._max_attempts_for(exc)
                if retriable and attempt < max_attempts:
                    wait_ms = self._build_retry_delay_ms(attempt, exc)
                    logger.warning(
                        "Transient LLM error on attempt %d/%d; retrying in %dms: %s",
                        attempt,
                        self.retry_max_attempts,
                        wait_ms,
                        _extract_error_detail(exc),
                    )
                    self._emit_retry_event(attempt, wait_ms, reason)
                    await asyncio.sleep(wait_ms / 1000)
                    attempt += 1
                    continue
                logger.warning("LLM call failed after %d attempt(s): %s", attempt, _extract_error_detail(exc), exc_info=exc)
                if retriable:
                    self._record_failure()
                return self._build_user_fallback_message(exc, reason)


def _matches_any(detail: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in detail for pattern in patterns)


def _extract_error_code(exc: BaseException) -> Any:
    for attr in ("code", "error_code"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            return value

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("code", "type"):
                value = error.get(key)
                if value not in (None, ""):
                    return value
    return None


def _extract_status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _extract_retry_after_ms(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    raw = None
    header_name = ""
    for key in ("retry-after-ms", "Retry-After-Ms", "retry-after", "Retry-After"):
        header_name = key
        if hasattr(headers, "get"):
            raw = headers.get(key)
        if raw:
            break
    if not raw:
        return None

    try:
        multiplier = 1 if "ms" in header_name.lower() else 1000
        return max(0, int(float(raw) * multiplier))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            delta = target.timestamp() - time.time()
            return max(0, int(delta * 1000))
        except (TypeError, ValueError, OverflowError):
            return None


def _extract_error_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return exc.__class__.__name__
