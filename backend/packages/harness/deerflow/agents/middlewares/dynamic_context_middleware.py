"""
动态上下文中间件

在每个模型调用前，注入动态信息：
- 当前日期时间
- 记忆内容（来自 MemoryMiddleware，阶段4实现）

以 SystemMessage 注入到对话消息最前面。每轮基于原始 request 重新构造，
不会累积；且 SystemMessage 语义清晰，便于模型识别为系统提醒而非用户输入。

基础系统提示词（system_message）保持不变，仍可被 LLM 缓存复用。
"""

from datetime import datetime

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


class DynamicContextMiddleware(AgentMiddleware):
    """
    在每轮模型调用前注入动态上下文。

    Hook 使用: wrap_model_call
    执行顺序: 较早（在模型实际调用之前）

    设计要点:
    - 用 wrap_model_call 而非 before_agent / before_model, 保证每轮注入且不累积
      （注入的提醒消息不写回 state, 下一轮 request.messages 不含它）;
    - 用 SystemMessage 注入, 不与用户消息混淆;
    - 仅 override messages, 不改 system_message, 保留缓存友好性。
    """

    def _build_reminder(self) -> SystemMessage:
        """构造动态上下文提醒。"""
        today = datetime.now().strftime("%Y年%m月%d日")
        # 记忆内容（来自 MemoryMiddleware）——阶段4实现，此处留空。
        parts = [
            "<system-reminder>",
            f"今天的日期是 {today}。",
            "</system-reminder>",
        ]
        return SystemMessage(content="\n".join(parts))

    def _inject(self, request):
        """在对话消息最前面注入动态提醒。"""
        return request.override(messages=[self._build_reminder()] + request.messages)

    def wrap_model_call(self, request, handler):
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._inject(request))
