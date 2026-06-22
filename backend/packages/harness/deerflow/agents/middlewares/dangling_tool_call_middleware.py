"""补 dangling tool call 的占位响应（M16）。

**dangling tool call**：某条 ``AIMessage`` 带了 ``tool_calls``，但历史里没有对应的
``ToolMessage``（用户中断 / 请求取消导致）。OpenAI 兼容 provider 的校验器要求
assistant 的每个 tool_call 紧跟 tool 响应，缺了就 400，agent 卡死。

本中间件用 ``wrap_model_call``（而非 ``before_model``）扫描历史，给每个悬空 tool call
插一条合成错误 ``ToolMessage``（紧跟在那条 AIMessage 后），保证消息顺序合法。

为何用 ``wrap_model_call`` 而非 ``before_model``：``before_model`` + ``add_messages``
reducer 会把补的消息追加到末尾，而非紧跟在 AIMessage 后；``wrap_model_call`` 能
``request.override(messages=...)`` 重建完整顺序。

无效 / malformed tool call（``invalid_tool_calls`` / ``additional_kwargs["tool_calls"]``）
也算 dangling——provider adapter 可能仍把它们的 id/name 带进下个请求，校验器照样要配对。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# issue #2894：malformed write_file 的 tool-call args 可能带超大 Markdown payload。
# 复错误说明截短，避免把大 / 畸形内容回灌给模型。
_MAX_RECOVERY_ERROR_DETAIL_LEN = 500


class DanglingToolCallMiddleware(AgentMiddleware[AgentState]):
    """模型调用前给悬空 tool call 补占位 ToolMessage。"""

    @staticmethod
    def _message_tool_calls(msg) -> list[dict]:
        """从结构化字段或 raw provider payload 归一出 tool calls。

        LangChain 把 malformed provider function call 存进 ``invalid_tool_calls``——它们
        不执行，但 provider adapter 可能仍把 id/name 带进下个请求，严格 OpenAI 校验器
        仍要配对 ToolMessage。把它们也当 dangling，保下个模型请求格式合法。
        """
        normalized: list[dict] = []

        tool_calls = getattr(msg, "tool_calls", None) or []
        normalized.extend(list(tool_calls))

        raw_tool_calls = (getattr(msg, "additional_kwargs", None) or {}).get("tool_calls") or []
        if not tool_calls:
            for raw_tc in raw_tool_calls:
                if not isinstance(raw_tc, dict):
                    continue

                function = raw_tc.get("function")
                name = raw_tc.get("name")
                if not name and isinstance(function, dict):
                    name = function.get("name")

                args = raw_tc.get("args", {})
                if not args and isinstance(function, dict):
                    raw_args = function.get("arguments")
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            parsed_args = {}
                        args = parsed_args if isinstance(parsed_args, dict) else {}

                normalized.append(
                    {
                        "id": raw_tc.get("id"),
                        "name": name or "unknown",
                        "args": args if isinstance(args, dict) else {},
                    }
                )

        for invalid_tc in getattr(msg, "invalid_tool_calls", None) or []:
            if not isinstance(invalid_tc, dict):
                continue
            normalized.append(
                {
                    "id": invalid_tc.get("id"),
                    "name": invalid_tc.get("name") or "unknown",
                    "args": {},
                    "invalid": True,
                    "error": invalid_tc.get("error"),
                }
            )

        return normalized

    @staticmethod
    def _synthetic_tool_message_content(tool_call: dict) -> str:
        if tool_call.get("invalid"):
            name = tool_call.get("name")
            error = tool_call.get("error")
            error_text = error[:_MAX_RECOVERY_ERROR_DETAIL_LEN] if isinstance(error, str) and error else ""
            # issue #2894：malformed write_file 常因超大单次 payload，引导模型改用正文输出 / 拆段。
            if name == "write_file":
                details = f" Parser error: {error_text}" if error_text else ""
                return (
                    "[write_file failed before execution: the tool-call arguments were not valid JSON, "
                    "so no file was written. This often happens when the model tries to write a very "
                    "large Markdown file in a single tool call, especially when `content` contains "
                    "unescaped quotes, inline JSON, backslashes, or code fences. Do not retry the same "
                    "large `write_file` payload for this artifact; provide the report/content directly "
                    "as normal assistant text in your next response. If a file write is still needed "
                    f"later, split the file into smaller sections instead of one large payload.{details}]"
                )
            if error_text:
                return f"[Tool call could not be executed because its arguments were invalid: {error_text}]"
            return "[Tool call could not be executed because its arguments were invalid.]"
        return "[Tool call was interrupted and did not return a result.]"

    def _build_patched_messages(self, messages: list) -> list | None:
        """把 tool 响应重新分组到对应 AIMessage 之后；已合法的历史原样不动。"""
        tool_messages_by_id: dict[str, deque[ToolMessage]] = defaultdict(deque)
        for msg in messages:
            if isinstance(msg, ToolMessage):
                tool_messages_by_id[msg.tool_call_id].append(msg)

        tool_call_ids: set[str] = set()
        for msg in messages:
            if getattr(msg, "type", None) != "ai":
                continue
            for tc in self._message_tool_calls(msg):
                tc_id = tc.get("id")
                if tc_id:
                    tool_call_ids.add(tc_id)

        patched: list = []
        patch_count = 0
        for msg in messages:
            # 跳过原位置的 ToolMessage（后面会重新插到对应 AIMessage 后）。
            if isinstance(msg, ToolMessage) and msg.tool_call_id in tool_call_ids:
                continue

            patched.append(msg)
            if getattr(msg, "type", None) != "ai":
                continue

            for tc in self._message_tool_calls(msg):
                tc_id = tc.get("id")
                if not tc_id:
                    continue

                tool_msg_queue = tool_messages_by_id.get(tc_id)
                existing_tool_msg = tool_msg_queue.popleft() if tool_msg_queue else None
                if existing_tool_msg is not None:
                    patched.append(existing_tool_msg)
                else:
                    patched.append(
                        ToolMessage(
                            content=self._synthetic_tool_message_content(tc),
                            tool_call_id=tc_id,
                            name=tc.get("name", "unknown"),
                            status="error",
                        )
                    )
                    patch_count += 1

        if patched == messages:
            return None

        if patch_count:
            logger.warning("Injecting %d placeholder ToolMessage(s) for dangling tool calls", patch_count)
        return patched

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return await handler(request)
