"""
标题自动生成中间件

在第一个完整的对话轮次后，自动生成线程标题。
"""
import logging

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)


class TitleMiddleware(AgentMiddleware):
    """
    自动生成线程标题。

    Hook 使用: after_model
    执行顺序: 中间（在记忆注入之后，循环检测之前）

    只在首次对话后生成一次标题，之后保持不变。
    """

    def __init__(self, max_words: int = 10):
        super().__init__()
        self.max_words = max_words

    def _should_generate_title(self, state) -> bool:
        """判断是否需要生成标题"""
        # 如果已有标题，不需要
        if state.get("title"):
            return False

        # 找到第一次用户→AI 的完整对话
        messages = state.get("messages", [])
        has_user = False
        has_ai = False

        for msg in messages:
            role = getattr(msg, "type", "")
            if role == "human":
                has_user = True
            elif role == "ai" and has_user:
                has_ai = True
                break

        return has_user and has_ai

    def _generate_title(self, state) -> str | None:
        """从首次对话中生成标题"""
        messages = state.get("messages", [])

        user_msg = None
        ai_msg = None
        for msg in messages:
            if msg.type == "human" and user_msg is None:
                user_msg = msg
            elif msg.type == "ai" and user_msg is not None and ai_msg is None:
                ai_msg = msg
                break

        if not user_msg or not ai_msg:
            return None

        # 标题生成：截取 AI 回复前若干字符（按字符数计数，兼容中文）。
        # 注意：早期实现用 split()[:max_words] 按空白切分，但中文无空格 →
        # 整段中文几乎不切分 → 标题变成整句回复。故改为按字符截断。
        content = getattr(ai_msg, "content", "")
        if isinstance(content, str):
            text = content.replace("\n", " ").strip()
            if len(text) <= self.max_words:
                return text
            return text[: self.max_words].rstrip() + "…"

        return None

    def after_model(self, state, runtime):
        """在模型回复后检查是否生成标题"""
        if not self._should_generate_title(state):
            return None

        title = self._generate_title(state)
        if title:
            logger.info(f"生成标题: {title}")
            return {"title": title}

        return None

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)
