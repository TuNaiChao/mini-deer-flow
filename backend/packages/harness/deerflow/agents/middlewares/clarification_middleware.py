"""
澄清拦截中间件

拦截 ask_clarification 工具调用，中断 Agent 执行，
将问题返回给用户并等待回复。

这是必须排在最后的中间件——确保其他中间件已经处理完毕。
"""

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# LangGraph 控制流：导入 Command 与 END 终点常量（如果可用）
try:
    from langgraph.types import END, Command

    _HAS_LANGGRAPH_COMMAND = True
except ImportError:
    Command = None
    END = "__end__"  # 回退值（理论上 langgraph 已安装）
    _HAS_LANGGRAPH_COMMAND = False


class ClarificationMiddleware(AgentMiddleware):
    """
    拦截 ask_clarification 工具调用。

    Hook 使用: wrap_tool_call
    执行顺序: ⚠️ 必须排在中间件链的最后！

    当 Agent 调用 ask_clarification 时：
    1. 格式化问题为友好的用户消息
    2. 使用 Command(goto=END) 中断当前执行
    3. 等待用户回复后继续
    """

    def wrap_tool_call(self, request, handler):
        """拦截 ask_clarification 调用"""
        tool_name = getattr(request, "tool_name", "")
        if hasattr(request, "tool_call"):
            tool_name = request.tool_call.get("name", "")

        if tool_name == "ask_clarification":
            # 获取问题内容
            args = request.tool_call.get("args", {})
            question = args.get("question", "请提供更多信息")

            logger.info(f"Agent 请求澄清: {question}")

            if _HAS_LANGGRAPH_COMMAND:
                # 使用 Command 中断执行（END 为 langgraph 终点常量）
                return Command(
                    goto=END,
                    update={
                        "messages": [
                            ToolMessage(
                                content=f"[需要更多信息] {question}",
                                tool_call_id=request.tool_call["id"],
                                name="ask_clarification",
                            )
                        ]
                    },
                )
            else:
                # 回退方案：返回 ToolMessage（不会中断）
                return ToolMessage(
                    content=f"[需要更多信息] {question}",
                    tool_call_id=request.tool_call["id"],
                    name="ask_clarification",
                )

        # 非 ask_clarification，正常执行
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        """异步版本"""
        tool_name = request.tool_call.get("name", "")
        if tool_name == "ask_clarification":
            return self.wrap_tool_call(request, handler)
        return await handler(request)
