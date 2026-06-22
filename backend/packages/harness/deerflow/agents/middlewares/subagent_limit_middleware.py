"""``SubagentLimitMiddleware``：截断单次模型响应里超额的 ``task`` 工具调用（M16，接 M11）。

LLM 可能在一条响应里并行发 > ``max_concurrent`` 个 ``task``（子代理委派）调用。子代理执行
贵且受 scheduler pool 槽位（M11 ``MAX_CONCURRENT_SUBAGENTS=3``）限制，超额调用会排队甚至
拖垮。本中间件在 ``after_model`` 保留前 ``max_concurrent`` 个 task 调用、丢弃其余，比 prompt
里「最多 N 个」的软约束可靠得多。

clamp 到 ``[2, 4]``：低于 2 无并行收益，高于 4 超过 scheduler pool 上限（3）意义不大且增风险。
"""

from __future__ import annotations

import logging
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.tool_call_metadata import clone_ai_message_with_tool_calls
from deerflow.subagents.executor import MAX_CONCURRENT_SUBAGENTS

logger = logging.getLogger(__name__)

MIN_SUBAGENT_LIMIT = 2
MAX_SUBAGENT_LIMIT = 4


def _clamp_subagent_limit(value: int) -> int:
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """截断单次模型响应里超额的 ``task`` 工具调用。

    Args:
        max_concurrent: 最大并发子代理调用数。默认 ``MAX_CONCURRENT_SUBAGENTS``(3)，clamp 到 [2,4]。
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_SUBAGENTS):
        super().__init__()
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)

    def _truncate_task_calls(self, state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
        if len(task_indices) <= self.max_concurrent:
            return None

        indices_to_drop = set(task_indices[self.max_concurrent :])
        truncated_tool_calls = [tc for i, tc in enumerate(tool_calls) if i not in indices_to_drop]

        dropped_count = len(indices_to_drop)
        logger.warning(
            "Truncated %d excess task tool call(s) from model response (limit: %d)",
            dropped_count,
            self.max_concurrent,
        )

        # clone 同步结构化 / raw tool_calls / finish_reason（同一 id 触发替换）。
        updated_msg = clone_ai_message_with_tool_calls(last_msg, truncated_tool_calls)
        return {"messages": [updated_msg]}

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._truncate_task_calls(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._truncate_task_calls(state)
