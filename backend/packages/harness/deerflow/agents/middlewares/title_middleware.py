"""标题自动生成中间件（M16 重做，config 驱动 + 结构化内容归一）。

首个完整对话轮次后，用配置的标题模型生成线程标题，写进 ``state["title"]``。已生成则不再
覆盖（幂等）。

相较 v1.1 教学版，本次对齐 deer：① config 驱动（``title.enabled`` / ``prompt_template`` /
``max_words`` / ``max_chars`` / ``model_name``）；② 结构化消息内容归一（list / dict content
也能抽文本，不再 ``str()`` 强转）；③ 同步路径给本地兜底标题（截首条用户消息），异步路径调
LLM 生成、失败回退兜底；④ 继承父 RunnableConfig + 贴 ``middleware:title`` tag 供 RunJournal
归因；⑤ 过滤 dynamic-context reminder（不把它当首条用户消息）。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.dynamic_context_middleware import is_dynamic_context_reminder
from deerflow.models import create_chat_model

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.config.title_config import TitleConfig

logger = logging.getLogger(__name__)


class TitleMiddlewareState(AgentState):
    title: NotRequired[str | None]


class TitleMiddleware(AgentMiddleware[TitleMiddlewareState]):
    """首个用户消息后自动生成线程标题。"""

    state_schema = TitleMiddlewareState

    def __init__(self, *, app_config: AppConfig | None = None):
        super().__init__()
        self._app_config = app_config

    def _get_title_config(self) -> TitleConfig:
        # mini 用 ``app_config.title`` 访问（不维护独立 get_title_config 单例）。
        if self._app_config is not None:
            return self._app_config.title
        from deerflow.config import get_app_config

        return get_app_config().title

    def _normalize_content(self, content: object) -> str:
        """从 str / list / dict content 抽纯文本。"""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = [self._normalize_content(item) for item in content]
            return "\n".join(part for part in parts if part)

        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value

            nested_content = content.get("content")
            if nested_content is not None:
                return self._normalize_content(nested_content)

        return ""

    @staticmethod
    def _is_user_message_for_title(message: object) -> bool:
        # 排除 dynamic-context reminder（带日期 / 记忆的隐藏消息），别把它当首条用户消息。
        return getattr(message, "type", None) == "human" and not is_dynamic_context_reminder(message)

    def _should_generate_title(self, state: TitleMiddlewareState) -> bool:
        config = self._get_title_config()
        if not config.enabled:
            return False

        if state.get("title"):
            return False

        messages = state.get("messages", [])
        if len(messages) < 2:
            return False

        user_messages = [m for m in messages if self._is_user_message_for_title(m)]
        assistant_messages = [m for m in messages if m.type == "ai"]

        # 首个完整对话轮次后生成（恰好一条用户消息 + 至少一条 AI 回复）。
        return len(user_messages) == 1 and len(assistant_messages) >= 1

    def _build_title_prompt(self, state: TitleMiddlewareState) -> tuple[str, str]:
        """抽用户 / AI 消息构造标题提示。返回 (prompt, user_msg) 供调用方兜底用。"""
        config = self._get_title_config()
        messages = state.get("messages", [])

        user_msg_content = next((m.content for m in messages if self._is_user_message_for_title(m)), "")
        assistant_msg_content = next((m.content for m in messages if m.type == "ai"), "")

        user_msg = self._normalize_content(user_msg_content)
        assistant_msg = self._strip_think_tags(self._normalize_content(assistant_msg_content))

        prompt = config.prompt_template.format(
            max_words=config.max_words,
            user_msg=user_msg[:500],
            assistant_msg=assistant_msg[:500],
        )
        return prompt, user_msg

    def _strip_think_tags(self, text: str) -> str:
        """去掉推理模型（minimax / DeepSeek-R1）的 ``<think>...</think>`` 块。"""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    def _parse_title(self, content: object) -> str:
        config = self._get_title_config()
        title_content = self._normalize_content(content)
        title_content = self._strip_think_tags(title_content)
        title = title_content.strip().strip('"').strip("'")
        return title[: config.max_chars] if len(title) > config.max_chars else title

    def _fallback_title(self, user_msg: str) -> str:
        config = self._get_title_config()
        fallback_chars = min(config.max_chars, 50)
        if len(user_msg) > fallback_chars:
            return user_msg[:fallback_chars].rstrip() + "..."
        return user_msg if user_msg else "New Conversation"

    def _get_runnable_config(self) -> dict[str, Any]:
        """继承父 RunnableConfig 并贴 middleware tag。

        让 RunJournal 把本中间件的 LLM 调用识别为 ``middleware:title`` 而非 ``lead_agent``。
        """
        try:
            parent = get_config()
        except Exception:
            parent = {}
        config = {**parent}
        config["run_name"] = "title_agent"
        config["tags"] = [*(config.get("tags") or []), "middleware:title"]
        return config

    def _generate_title_result(self, state: TitleMiddlewareState) -> dict | None:
        """同步路径：给本地兜底标题（不阻塞 LLM 调用）。"""
        if not self._should_generate_title(state):
            return None

        _, user_msg = self._build_title_prompt(state)
        return {"title": self._fallback_title(user_msg)}

    async def _agenerate_title_result(self, state: TitleMiddlewareState) -> dict | None:
        """异步路径：调 LLM 生成，失败回退本地兜底。"""
        if not self._should_generate_title(state):
            return None

        config = self._get_title_config()
        prompt, user_msg = self._build_title_prompt(state)

        try:
            # attach_tracing=False：_get_runnable_config 继承图级 RunnableConfig（make_lead_agent
            # 设的），其 callbacks 已带追踪 handler；模型级再挂会发重复 span。
            model_kwargs: dict[str, Any] = {"thinking_enabled": False, "attach_tracing": False}
            if self._app_config is not None:
                model_kwargs["app_config"] = self._app_config
            if config.model_name:
                model = create_chat_model(name=config.model_name, **model_kwargs)
            else:
                model = create_chat_model(**model_kwargs)
            response = await model.ainvoke(prompt, config=self._get_runnable_config())
            title = self._parse_title(response.content)
            if title:
                return {"title": title}
        except Exception:
            logger.debug("Failed to generate async title; falling back to local title", exc_info=True)
        return {"title": self._fallback_title(user_msg)}

    @override
    def after_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return self._generate_title_result(state)

    @override
    async def aafter_model(self, state: TitleMiddlewareState, runtime: Runtime) -> dict | None:
        return await self._agenerate_title_result(state)
