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


def message_to_text(message: Any, *, text_attribute_fallback: bool = False) -> str:
    """从整条消息（``BaseMessage`` 或 dict 形态）抽取展示文本。

    先从属性（``BaseMessage``）或 mapping 键（``run_events`` 行是 dict）取 ``content``，
    再遍历混合的 ``content`` 形态：纯字符串；字符串 / ``{"text": ...}`` / 嵌套
    ``{"content": ...}`` 块的列表（**无分隔符**拼接）；或带 ``text``/``content`` 键的 mapping。
    传 ``text_attribute_fallback=True`` 时，content 抽不出就回退到 ``message.text``
    （对齐旧 ``RunJournal._message_text``）。

    与 :func:`message_content_to_text`（吃原始 ``content``、列表块用换行拼）不同——本函数
    保留无分隔符拼接与更宽的形态处理，是多个调用点各自重写的「整条消息→文本」逻辑的合并。
    """
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    nested = block.get("content")
                    if isinstance(nested, str):
                        parts.append(nested)
        return "".join(parts)
    if isinstance(content, Mapping):
        for key in ("text", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    if text_attribute_fallback:
        text = getattr(message, "text", None)
        if isinstance(text, str):
            return text
    return ""


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
