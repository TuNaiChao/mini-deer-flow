"""把 LangChain 消息对象转成 OpenAI Chat Completions 格式的纯函数。

LangChain 消息类型 → OpenAI 兼容 dict 的翻译工具。当前**未**接入 RunJournal
（RunJournal 直接用 ``message.model_dump()``），但供需要 OpenAI 线协议格式的消费方
（未来的 REST 端点、worker）使用。

outline 标注本模块「可后补，先占位」；此处按 deer 参考实现完整移植（纯函数、无依赖、
低风险），供后续端点直接用。
"""

from __future__ import annotations

import json
from typing import Any

_ROLE_MAP = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
}


def langchain_to_openai_message(message: Any) -> dict:
    """把单条 LangChain BaseMessage 转成 OpenAI message dict。

    处理：
    - HumanMessage → ``{"role": "user", "content": "..."}``
    - AIMessage（纯文本）→ ``{"role": "assistant", "content": "..."}``
    - AIMessage（带 tool_calls）→ ``{"role": "assistant", "content": null, "tool_calls": [...]}``
    - AIMessage（文本 + tool_calls）→ content 与 tool_calls 都在
    - AIMessage（list content / 多模态）→ content 保留为 list
    - SystemMessage → ``{"role": "system", "content": "..."}``
    - ToolMessage → ``{"role": "tool", "tool_call_id": "...", "content": "..."}``
    """
    msg_type = getattr(message, "type", "")
    role = _ROLE_MAP.get(msg_type, msg_type)
    content = getattr(message, "content", "")

    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": getattr(message, "tool_call_id", ""),
            "content": content,
        }

    if role == "assistant":
        tool_calls = getattr(message, "tool_calls", None) or []
        result: dict = {"role": "assistant"}

        if tool_calls:
            openai_tool_calls = []
            for tc in tool_calls:
                args = tc.get("args", {})
                openai_tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(args) if not isinstance(args, str) else args,
                        },
                    }
                )
            # 无文本内容时按 OpenAI 规范 content 设 null
            result["content"] = content if (isinstance(content, list) and content) or (isinstance(content, str) and content) else None
            result["tool_calls"] = openai_tool_calls
        else:
            result["content"] = content

        return result

    # user / system / unknown
    return {"role": role, "content": content}


def _infer_finish_reason(message: Any) -> str:
    """从 AIMessage 推断 OpenAI finish_reason。

    有 tool_calls 返回 "tool_calls"；否则查 response_metadata.finish_reason；都没有返回 "stop"。
    """
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        return "tool_calls"
    resp_meta = getattr(message, "response_metadata", None) or {}
    if isinstance(resp_meta, dict):
        finish = resp_meta.get("finish_reason")
        if finish:
            return finish
    return "stop"


def langchain_to_openai_completion(message: Any) -> dict:
    """把一条 AIMessage 及其元数据转成 OpenAI completion 响应 dict。

    Returns:
        ``{"id", "model", "choices": [{"index": 0, "message": <openai_message>, "finish_reason": <inferred>}], "usage": ... | None}``
    """
    resp_meta = getattr(message, "response_metadata", None) or {}
    model_name = resp_meta.get("model_name") if isinstance(resp_meta, dict) else None

    openai_msg = langchain_to_openai_message(message)
    finish_reason = _infer_finish_reason(message)

    usage_metadata = getattr(message, "usage_metadata", None)
    if usage_metadata is not None:
        input_tokens = usage_metadata.get("input_tokens", 0) or 0
        output_tokens = usage_metadata.get("output_tokens", 0) or 0
        usage: dict | None = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    else:
        usage = None

    return {
        "id": getattr(message, "id", None),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": openai_msg,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def langchain_messages_to_openai(messages: list) -> list[dict]:
    """把一组 LangChain BaseMessage 转成 OpenAI message dict 列表。"""
    return [langchain_to_openai_message(m) for m in messages]
