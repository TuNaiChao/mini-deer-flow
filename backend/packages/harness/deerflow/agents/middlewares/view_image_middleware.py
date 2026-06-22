"""``ViewImageMiddleware``：``view_image`` 工具完成后把图片 base64 注入模型上下文（M16）。

模型调 ``view_image`` 把图片读进 ``state["viewed_images"]``（路径 → mime + base64）。
本中间件在每次 ``before_model`` 检查：上一轮 AI 是否有 ``view_image`` 调用、是否全部
完成（每个 tool_call 都有对应 ToolMessage），若是则注入一条含 base64 图片块的
HumanMessage，让模型「看见」刚看过的图，无需用户显式提示。

``hide_from_ui`` 标记让这条上下文消息不显示在聊天 UI / IM 渠道。幂等：已注入过则不重复。
"""

from __future__ import annotations

import logging
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)


class ViewImageMiddlewareState(ThreadState):
    """复用 thread_state，reducer 标注（viewed_images）得以保留。"""


class ViewImageMiddleware(AgentMiddleware[ViewImageMiddlewareState]):
    """``view_image`` 工具完成后注入图片细节给 LLM。"""

    state_schema = ViewImageMiddlewareState

    def _get_last_assistant_message(self, messages: list) -> AIMessage | None:
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
        return None

    def _has_view_image_tool(self, message: AIMessage) -> bool:
        if not getattr(message, "tool_calls", None):
            return False
        return any(tool_call.get("name") == "view_image" for tool_call in message.tool_calls)

    def _all_tools_completed(self, messages: list, assistant_msg: AIMessage) -> bool:
        if not getattr(assistant_msg, "tool_calls", None):
            return False

        tool_call_ids = {tc.get("id") for tc in assistant_msg.tool_calls if tc.get("id")}

        try:
            assistant_idx = messages.index(assistant_msg)
        except ValueError:
            return False

        completed_tool_ids = set()
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                completed_tool_ids.add(msg.tool_call_id)

        return tool_call_ids.issubset(completed_tool_ids)

    def _create_image_details_message(self, state: ViewImageMiddlewareState) -> list[str | dict]:
        viewed_images = state.get("viewed_images", {})
        if not viewed_images:
            return [{"type": "text", "text": "No images have been viewed."}]

        content_blocks: list[str | dict] = [{"type": "text", "text": "Here are the images you've viewed:"}]

        for image_path, image_data in viewed_images.items():
            mime_type = image_data.get("mime_type", "unknown")
            base64_data = image_data.get("base64", "")

            content_blocks.append({"type": "text", "text": f"\n- **{image_path}** ({mime_type})"})

            if base64_data:
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_data}"},
                    }
                )

        return content_blocks

    def _should_inject_image_message(self, state: ViewImageMiddlewareState) -> bool:
        messages = state.get("messages", [])
        if not messages:
            return False

        last_assistant_msg = self._get_last_assistant_message(messages)
        if not last_assistant_msg:
            return False

        if not self._has_view_image_tool(last_assistant_msg):
            return False

        if not self._all_tools_completed(messages, last_assistant_msg):
            return False

        # 幂等：最后一条 AIMessage 之后若已有图片细节 HumanMessage，不再注入。
        assistant_idx = messages.index(last_assistant_msg)
        for msg in messages[assistant_idx + 1 :]:
            if isinstance(msg, HumanMessage):
                content_str = str(msg.content)
                if "Here are the images you've viewed" in content_str or "Here are the details of the images you've viewed" in content_str:
                    return False

        return True

    def _inject_image_message(self, state: ViewImageMiddlewareState) -> dict | None:
        if not self._should_inject_image_message(state):
            return None

        image_content = self._create_image_details_message(state)
        # 仅给模型的内部上下文 → hide_from_ui 不进 UI / IM 流。
        human_msg = HumanMessage(content=image_content, additional_kwargs={"hide_from_ui": True})

        logger.debug("Injecting image details message with images before LLM call")
        return {"messages": [human_msg]}

    @override
    def before_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._inject_image_message(state)

    @override
    async def abefore_model(self, state: ViewImageMiddlewareState, runtime: Runtime) -> dict | None:
        return self._inject_image_message(state)
