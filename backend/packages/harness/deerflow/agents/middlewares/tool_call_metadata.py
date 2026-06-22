"""保持 ``AIMessage`` 工具调用元数据一致性的 helper（M16）。

若干中间件（SubagentLimit / Safety / LoopDetection）需要改写 ``AIMessage`` 的
``tool_calls``——但 LangChain 的 ``AIMessage`` 不止 ``tool_calls`` 一个字段携带工具
调用：原始 provider payload 也存在 ``additional_kwargs["tool_calls"]``，``function_call``
（遗留），以及 ``response_metadata["finish_reason"] == "tool_calls"``。只改结构化
``tool_calls`` 而不同步这些字段，会让 provider 序列化器 / 下游消费者看到前后不一致
的状态（残留的 raw tool_calls 被当成待执行），触发诡异 bug。

本模块提供单一入口 :func:`clone_ai_message_with_tool_calls`，**一处**把这些字段
一起同步，避免「新增截断分支忘了同步 raw payload」的漂移。是中间件层的纯函数 helper，
不是 AgentMiddleware。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


def _raw_tool_call_id(raw_tool_call: Any) -> str | None:
    """从一条 raw provider tool_call 抽 id（只接受字符串非空值）。"""
    if not isinstance(raw_tool_call, dict):
        return None

    raw_id = raw_tool_call.get("id")
    return raw_id if isinstance(raw_id, str) and raw_id else None


def clone_ai_message_with_tool_calls(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any | None = None,
) -> AIMessage:
    """克隆 AIMessage，同时保持 raw provider 工具调用元数据同步。

    Args:
        message: 原始 AIMessage。
        tool_calls: 要替换上的结构化 ``tool_calls`` 列表（**id 集合**驱动 raw 字段同步）。
        content: 可选的新 content；``None`` 表示保持原 content 不变。

    Returns:
        新的 AIMessage（``model_copy``），其 ``tool_calls`` / ``additional_kwargs`` /
        ``response_metadata`` 三者已对齐到新的工具调用集。

    同步规则：
        - 只保留 ``additional_kwargs["tool_calls"]`` 里 id 仍在新集合的条目；新集合为空时整段删除。
        - 新集合为空时同时删 ``additional_kwargs["function_call"]``（遗留字段，工具调用已清空就没意义了）。
        - ``response_metadata["finish_reason"] == "tool_calls"`` 且新集合为空 → 改成 ``"stop"``，
          否则保持原 provider finish_reason（如 ``content_filter`` / ``refusal``）让下游看到真实原因。
    """
    kept_ids = {tc["id"] for tc in tool_calls if isinstance(tc.get("id"), str) and tc["id"]}

    update: dict[str, Any] = {"tool_calls": tool_calls}
    if content is not None:
        update["content"] = content

    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        synced_raw_tool_calls = [raw_tc for raw_tc in raw_tool_calls if _raw_tool_call_id(raw_tc) in kept_ids]
        if synced_raw_tool_calls:
            additional_kwargs["tool_calls"] = synced_raw_tool_calls
        else:
            additional_kwargs.pop("tool_calls", None)

    if not tool_calls:
        additional_kwargs.pop("function_call", None)

    update["additional_kwargs"] = additional_kwargs

    response_metadata = dict(getattr(message, "response_metadata", {}) or {})
    if not tool_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata

    return message.model_copy(update=update)
