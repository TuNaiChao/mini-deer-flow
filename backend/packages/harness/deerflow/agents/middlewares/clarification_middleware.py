"""澄清拦截中间件（M16 重做）。

拦截 ``ask_clarification`` 工具调用 → 把问题 + 选项 + 上下文格式化成友好用户消息 →
``Command(goto=END)`` 中断执行，等用户回复后继续。

为何**永远排最后**（红线 #14）：所有其它中间件须先处理完毕，且本中间件能中断整次执行——
排在最后保证它在工具路由的最后一跳生效，不被后续中间件覆盖。

相较 v1.1 教学版，本次补全：options（JSON 字符串归一）/ context（背景）/ clarification_type
（含图标）/ 确定性 message id（重试的澄清**替换**而非追加，靠 ``clarification:{tool_call_id}``）。
红线 #15：``wrap_tool_call`` 里非 ask_clarification 的调用原样 ``handler(request)``，
GraphBubbleUp 若被 handler 抛出自然透传（不吞）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from hashlib import sha256
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


class ClarificationMiddlewareState(AgentState):
    """兼容 ``ThreadState``。"""

    pass


class ClarificationMiddleware(AgentMiddleware[ClarificationMiddlewareState]):
    """拦截 ``ask_clarification`` 工具调用并中断执行把问题呈现给用户。

    模型调 ``ask_clarification`` 时本中间件：① 拦截工具调用；② 抽问题 + 元数据；③ 格式化
    友好消息；④ 返回 ``Command`` 中断执行呈现问题；⑤ 等用户回复后继续。
    """

    state_schema = ClarificationMiddlewareState

    def _stable_message_id(self, tool_call_id: str, formatted_message: str) -> str:
        """确定性 message id：重试的澄清替换而非追加。"""
        if tool_call_id:
            return f"clarification:{tool_call_id}"
        digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
        return f"clarification:{digest}"

    def _is_chinese(self, text: str) -> bool:
        return any("一" <= char <= "鿿" for char in text)

    def _format_clarification_message(self, args: dict) -> str:
        """把澄清参数格式化成用户友好消息。

        部分模型（如 Qwen3-Max）把数组参数序列化成 JSON 字符串而非原生数组——归一化让
        ``options`` 恒为 list。
        """
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (json.JSONDecodeError, TypeError):
                options = [options]

        if options is None:
            options = []
        elif not isinstance(options, list):
            options = [options]

        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }
        icon = type_icons.get(clarification_type, "❓")

        message_parts = []
        if context:
            # 有背景 → 先呈现背景再问。
            message_parts.append(f"{icon} {context}")
            message_parts.append(f"\n{question}")
        else:
            message_parts.append(f"{icon} {question}")

        if options:
            message_parts.append("")
            for i, option in enumerate(options, 1):
                message_parts.append(f"  {i}. {option}")

        return "\n".join(message_parts)

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        args = request.tool_call.get("args", {})
        question = args.get("question", "")

        logger.info("Intercepted clarification request")
        logger.debug("Clarification question: %s", question)

        formatted_message = self._format_clarification_message(args)
        tool_call_id = request.tool_call.get("id", "")

        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        # Command：① 加格式化 ToolMessage；② goto=END 中断执行。
        # 不额外加 AIMessage——前端会直接检测并展示 ask_clarification ToolMessage。
        return Command(update={"messages": [tool_message]}, goto=END)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            # 非澄清调用 → 正常执行（GraphBubbleUp 透传）。
            return handler(request)
        return self._handle_clarification(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            return await handler(request)
        return self._handle_clarification(request)
