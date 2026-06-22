"""记忆机制中间件（M13 memory，重写）。

在每个 agent 轮次完成后，把对话**过滤**（只留用户输入 + 最终 AI 回复）→ **检测**
correction/reinforcement 信号 → **捕获 user_id**（在请求上下文还活着时）→ 加入记忆更新
队列。实际的 LLM 抽取在后台**异步**经去抖队列执行。

对齐 deer ``agents/middlewares/memory_middleware.py``。mini 适配：``MemoryMiddlewareState``
省略（mini 的 ThreadState 已兼容 ``state.get("messages")``），hook 签名沿用 mini 既有
``after_agent(self, state, runtime)`` 风格。
"""

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from deerflow.agents.memory.message_processing import detect_correction, detect_reinforcement, filter_messages_for_memory
from deerflow.agents.memory.queue import get_memory_queue
from deerflow.config.memory_config import get_memory_config
from deerflow.runtime.user_context import get_effective_user_id

if TYPE_CHECKING:
    from deerflow.config.memory_config import MemoryConfig

logger = logging.getLogger(__name__)


class MemoryMiddleware(AgentMiddleware):
    """agent 执行后把对话排队等待记忆更新。

    1. agent 完成后，把对话排队等待记忆更新。
    2. 只含用户输入与最终 assistant 回复（跳过工具调用）。
    3. 队列用去抖把多次更新合并。
    4. 记忆经 LLM 总结异步更新。
    """

    def __init__(self, agent_name: str | None = None, *, memory_config: "MemoryConfig | None" = None):
        """初始化。

        Args:
            agent_name: 非 None 时存 per-agent 记忆；None 用全局记忆。
            memory_config: 显式记忆配置；省略时用全局配置兜底。
        """
        super().__init__()
        self._agent_name = agent_name
        self._memory_config = memory_config

    def _resolve_thread_id(self, runtime) -> str | None:
        """先从 runtime.context 取 thread_id，回退 LangGraph configurable 元数据。"""
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            config_data = get_config()
            thread_id = config_data.get("configurable", {}).get("thread_id")
        return thread_id

    def after_agent(self, state, runtime):
        """agent 完成后把对话排队等待记忆更新。

        Returns:
            None（本中间件不改 state）。
        """
        config = self._memory_config or get_memory_config()
        if not config.enabled:
            return None

        thread_id = self._resolve_thread_id(runtime)
        if not thread_id:
            logger.debug("No thread_id in context, skipping memory update")
            return None

        messages = state.get("messages", [])
        if not messages:
            logger.debug("No messages in state, skipping memory update")
            return None

        # 过滤：只留用户输入与最终 assistant 回复
        filtered_messages = filter_messages_for_memory(messages)

        # 至少需要一条用户消息和一条 assistant 回复
        user_messages = [m for m in filtered_messages if getattr(m, "type", None) == "human"]
        assistant_messages = [m for m in filtered_messages if getattr(m, "type", None) == "ai"]

        if not user_messages or not assistant_messages:
            return None

        # 排队
        correction_detected = detect_correction(filtered_messages)
        reinforcement_detected = not correction_detected and detect_reinforcement(filtered_messages)
        # 在请求上下文还活着时捕获 user_id。threading.Timer 在另一线程触发，ContextVar 值
        # 不跨裸线程传播，故必须把 user_id 显式存进 ConversationContext。
        user_id = get_effective_user_id()
        queue = get_memory_queue()
        queue.add(
            thread_id=thread_id,
            messages=filtered_messages,
            agent_name=self._agent_name,
            user_id=user_id,
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
        )

        return None

    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)
