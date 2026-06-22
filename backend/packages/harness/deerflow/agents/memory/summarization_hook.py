"""``before_summarization`` 钩子：摘要压缩消息前抢拍进记忆队列（M13 ↔ M16 接入）。

``SummarizationMiddleware`` 即将把旧消息压成摘要（删掉原文）。这些消息里的用户上下文
若不在压缩前抢拍进长期记忆，就会**永久丢失**（摘要里可能没保留具体偏好 / 事实）。

本钩子挂在 ``before_summarization``——被摘要的消息原样灌进记忆队列（``add_nowait`` 立即
后台处理），让 LLM 抽取 fact 存进 ``memory.json``。是「对话被压成摘要但仍记住了细节」的
关键衔接（deer ``agents/memory/summarization_hook.py`` 对齐）。

依赖 M13 的 ``filter_messages_for_memory`` / ``detect_correction`` / ``detect_reinforcement`` /
``get_memory_queue`` / ``add_nowait``，以及 M16 的 :class:`SummarizationEvent`。
"""

from __future__ import annotations

from deerflow.agents.memory.message_processing import (
    detect_correction,
    detect_reinforcement,
    filter_messages_for_memory,
)
from deerflow.agents.memory.queue import get_memory_queue
from deerflow.agents.middlewares.summarization_middleware import SummarizationEvent
from deerflow.config.memory_config import get_memory_config
from deerflow.runtime.user_context import resolve_runtime_user_id


def memory_flush_hook(event: SummarizationEvent) -> None:
    """把即将被摘要的消息抢拍进记忆队列。"""
    if not get_memory_config().enabled or not event.thread_id:
        return

    filtered_messages = filter_messages_for_memory(list(event.messages_to_summarize))
    user_messages = [message for message in filtered_messages if getattr(message, "type", None) == "human"]
    assistant_messages = [message for message in filtered_messages if getattr(message, "type", None) == "ai"]
    if not user_messages or not assistant_messages:
        return

    correction_detected = detect_correction(filtered_messages)
    reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
    user_id = resolve_runtime_user_id(event.runtime)
    queue = get_memory_queue()
    queue.add_nowait(
        thread_id=event.thread_id,
        messages=filtered_messages,
        agent_name=event.agent_name,
        user_id=user_id,
        correction_detected=correction_detected,
        reinforcement_detected=reinforcement_detected,
    )
