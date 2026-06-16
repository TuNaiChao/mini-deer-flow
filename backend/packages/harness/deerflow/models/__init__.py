"""模型模块：提供 LLM 模型的创建"""
from .factory import create_chat_model, get_default_model

__all__ = ["create_chat_model", "get_default_model"]