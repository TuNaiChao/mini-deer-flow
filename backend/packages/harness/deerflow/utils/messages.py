"""消息内容抽取工具。

LangChain 消息的 ``content`` 字段有三种形态：纯字符串、字符串列表、
``{"type": ..., "text": ...}`` 字典列表（多模态块）。这里提供把它们统一抽成
纯文本的辅助，以及取出「中间件介入前的原始用户文本」。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ORIGINAL_USER_CONTENT_KEY = "original_user_content"


def message_content_to_text(content: Any) -> str:
    """从 LangChain 消息 content 的各种形态抽取纯文本。

    - 纯字符串：直接返回；
    - 列表：逐项取字符串，或取字典项的 ``"text"`` 字段（多模态文本块），用换行拼接，
      跳过非文本块（如 ``image_url``）；
    - 其他类型：兜底 ``str(content)``。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content)


def get_original_user_content_text(content: Any, additional_kwargs: Mapping[str, Any] | None) -> str:
    """优先返回中间件介入前的原始用户文本，否则返回 content 抽取的文本。

    中间件（如 DynamicContext 注入记忆 / 日期）可能改写 ``content``，同时把
    「改写前的原始用户输入」存进 ``additional_kwargs["original_user_content"]``。
    本函数优先取这份未被改写的文本（用于记忆抽取等需要原始用户原话的场景），
    若不存在或不是字符串则回退到从当前 ``content`` 抽取的文本。
    """
    original_content = (additional_kwargs or {}).get(ORIGINAL_USER_CONTENT_KEY)
    if isinstance(original_content, str):
        return original_content
    return message_content_to_text(content)
