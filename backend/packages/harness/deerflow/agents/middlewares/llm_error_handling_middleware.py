"""
LLM 错误处理中间件

包装模型调用，将 API 错误转换为兜底回复，
允许 Agent 优雅降级而非崩溃。
"""
import logging
import time

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# 可重试的错误特征（瞬时错误：限流 / 超时 / 过载）
_RETRYABLE_MARKERS = ("rate limit", "429", "timeout", "timed out", "overloaded", "503")
# 认证 / 配置类错误特征（重试无意义，直接降级）
_AUTH_MARKERS = ("api key", "401", "403", "unauthorized", "forbidden")


def _classify(error: Exception) -> str:
    """根据异常信息将错误分为 auth / retryable / fatal 三类。"""
    msg = str(error).lower()
    if any(m in msg for m in _AUTH_MARKERS):
        return "auth"
    if any(m in msg for m in _RETRYABLE_MARKERS):
        return "retryable"
    return "fatal"


class LLMErrorHandlingMiddleware(AgentMiddleware):
    """
    包装模型调用，捕获 API 级错误并优雅降级。

    Hook 使用: wrap_model_call
    执行顺序: 最外层（中间件链的第一层）

    策略:
    - 可重试错误（限流 / 超时 / 过载）: 指数退避重试若干次;
    - 认证 / 配置错误、以及重试耗尽: 返回兜底 AIMessage, 避免整个 Agent 崩溃。
    """

    def __init__(self, max_retries: int = 2, base_delay: float = 1.0):
        """
        Args:
            max_retries: 最大尝试次数（含首次，即重试 max_retries - 1 次）。
            base_delay: 退避基数，第 n 次重试等待 base_delay * 2^n 秒。
        """
        super().__init__()
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _fallback(self, error: Exception) -> AIMessage:
        """构造兜底回复（wrap_model_call 支持直接返回 AIMessage）。"""
        logger.error("LLM 调用失败，已降级: %s", error)
        return AIMessage(content="抱歉，服务暂时不可用，请稍后重试。")

    def wrap_model_call(self, request, handler):
        for attempt in range(self.max_retries):
            try:
                return handler(request)
            except Exception as e:
                kind = _classify(e)
                if kind == "retryable" and attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    logger.warning(
                        "可重试错误 (%d/%d), %.1fs 后重试: %s",
                        attempt + 1, self.max_retries, delay, e,
                    )
                    time.sleep(delay)
                    continue
                # 认证 / 致命错误，或重试耗尽 → 降级
                return self._fallback(e)
        # 理论不可达（循环内必有 return）
        return self._fallback(RuntimeError("unreachable"))

    async def awrap_model_call(self, request, handler):
        import asyncio

        for attempt in range(self.max_retries):
            try:
                return await handler(request)
            except Exception as e:
                kind = _classify(e)
                if kind == "retryable" and attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    logger.warning(
                        "可重试错误 (%d/%d), %.1fs 后重试: %s",
                        attempt + 1, self.max_retries, delay, e,
                    )
                    await asyncio.sleep(delay)
                    continue
                return self._fallback(e)
        return self._fallback(RuntimeError("unreachable"))
