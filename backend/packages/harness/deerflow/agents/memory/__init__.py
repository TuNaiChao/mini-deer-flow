"""记忆模块（M13 memory）。

提供全局记忆机制：
- 把用户上下文与对话历史存进 ``memory.json``（per-user / per-agent 隔离）。
- 用 LLM 总结对话并抽取 fact。
- 把相关记忆注入系统提示实现个性化响应。

对齐 deer ``agents/memory/``。``summarization_hook``（摘要前抢拍）依赖 M16
SummarizationMiddleware（M16 ``agents/middlewares/summarization_middleware.py``）。
``memory_flush_hook`` 在摘要删消息前把对话抢拍进记忆队列，已在 M16 接入。
"""

from deerflow.agents.memory.prompt import (
    FACT_EXTRACTION_PROMPT,
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from deerflow.agents.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
    reset_memory_queue,
)
from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    create_empty_memory,
    get_memory_storage,
    utc_now_iso_z,
)
from deerflow.agents.memory.summarization_hook import memory_flush_hook
from deerflow.agents.memory.updater import (
    MemoryUpdater,
    clear_memory_data,
    create_memory_fact,
    delete_memory_fact,
    get_memory_data,
    import_memory_data,
    reload_memory_data,
    update_memory_fact,
    update_memory_from_conversation,
)

__all__ = [
    # Prompt utilities
    "MEMORY_UPDATE_PROMPT",
    "FACT_EXTRACTION_PROMPT",
    "format_memory_for_injection",
    "format_conversation_for_update",
    # Summarization hook（M13 ↔ M16）
    "memory_flush_hook",
    # Storage
    "MemoryStorage",
    "FileMemoryStorage",
    "get_memory_storage",
    "create_empty_memory",
    "utc_now_iso_z",
    # Queue
    "ConversationContext",
    "MemoryUpdateQueue",
    "get_memory_queue",
    "reset_memory_queue",
    # Updater
    "MemoryUpdater",
    "clear_memory_data",
    "create_memory_fact",
    "delete_memory_fact",
    "get_memory_data",
    "import_memory_data",
    "reload_memory_data",
    "update_memory_fact",
    "update_memory_from_conversation",
]
