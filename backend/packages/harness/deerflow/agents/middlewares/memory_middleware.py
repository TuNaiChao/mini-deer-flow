"""
记忆中间件

在每个 Agent 轮次完成后，将对话内容加入记忆更新队列。
实际的记忆提取在后台异步执行。
"""
import logging

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)


class MemoryMiddleware(AgentMiddleware):
    """
    队列化对话用于异步记忆更新。

    Hook 使用: after_agent
    执行顺序: 较后（在标题生成之后）

    收集用户消息和最终的 AI 回复，加入记忆更新队列。
    实际的 LLM 调用进行记忆提取是异步的（在后台线程中）。
    """

    def __init__(self, agent_name: str = "default"):
        super().__init__()
        self.agent_name = agent_name
        self._update_queue = []  # 简化：实际使用线程安全的队列

    def after_agent(self, state, runtime):
        """
        收集本轮的对话内容，加入记忆更新队列。

        筛选条件：只收集用户消息和最终的 AI 回复
        （跳过中间的工具调用和思考过程）
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # 提取用户消息和 AI 最终回复
        conversation_pair = []
        for msg in messages[-4:]:  # 只看最近几条消息
            msg_type = getattr(msg, "type", "")
            if msg_type == "human":
                conversation_pair.append({
                    "role": "user",
                    "content": getattr(msg, "content", ""),
                })
            elif msg_type == "ai" and conversation_pair:
                # 只收集有历史上下文的 AI 回复
                conversation_pair.append({
                    "role": "assistant",
                    "content": getattr(msg, "content", ""),
                })

        if len(conversation_pair) >= 2:
            self._update_queue.append({
                "agent_name": self.agent_name,
                "messages": conversation_pair,
            })
            logger.debug(f"对话已加入记忆队列 (队列长度: {len(self._update_queue)})")

        return None

    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)