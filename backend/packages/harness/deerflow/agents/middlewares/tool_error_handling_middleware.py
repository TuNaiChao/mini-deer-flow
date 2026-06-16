"""
工具错误处理中间件

捕获工具执行中的异常，将其转换为错误 ToolMessage，
防止整个 Agent 运行因单个工具失败而中止。
"""
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"


class ToolErrorHandlingMiddleware(AgentMiddleware):
    """
    包装每个工具调用，将异常转换为友好的错误消息。

    Hook 使用: wrap_tool_call
    执行顺序: 中间位置（在沙箱审计之后，澄清拦截之前）
    """

    def _build_error_message(self, request, exc: Exception) -> ToolMessage:
        """构造错误 ToolMessage（与真实实现对齐）。"""
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = str(tool_call.get("name") or "unknown_tool")
        tool_call_id = str(tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."
        return ToolMessage(
            content=f"Error: 工具 '{tool_name}' 执行失败 ({exc.__class__.__name__}): {detail}",
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        """
        包装工具调用，捕获异常。

        Args:
            request: ToolCallRequest（包含 tool_call, runtime）
            handler: 调用下一个包装器的函数

        Returns:
            ToolMessage 或 Command
        """
        try:
            return handler(request)
        except GraphBubbleUp:
            # ⚠️ 关键：必须原样抛出 LangGraph 控制流信号（interrupt / pause / resume /
            # Command(goto=END)）。如果被下面的 except Exception 吞掉，ClarificationMiddleware
            # 的中断、subagent 的 interrupt 等都会失效。真实实现也必须保留这一分支。
            raise
        except Exception as e:
            logger.warning(
                "工具执行失败 (sync): name=%s id=%s",
                getattr(request, "tool_call", {}).get("name"),
                getattr(request, "tool_call", {}).get("id"),
            )
            return self._build_error_message(request, e)

    # 异步版本
    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except GraphBubbleUp:
            # 同上：保留控制流信号
            raise
        except Exception as e:
            logger.warning(
                "工具执行失败 (async): name=%s id=%s",
                getattr(request, "tool_call", {}).get("name"),
                getattr(request, "tool_call", {}).get("id"),
            )
            return self._build_error_message(request, e)