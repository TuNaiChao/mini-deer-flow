"""
模型工厂模块

使用 resolve_class 动态加载模型类，支持任意 LangChain 兼容的模型提供者。
不硬编码任何特定模型类——所有模型通过 config.yaml 配置。
"""
"""
模型工厂模块

使用 resolve_class 动态加载模型类，支持任意 LangChain 兼容的模型提供者。
不硬编码任何特定模型类——所有模型通过 config.yaml 配置。
"""
import os
from typing import Any

from langchain_core.language_models import BaseChatModel

from ..config import get_app_config, ModelConfig
from ..reflection import resolve_class


def create_chat_model(
    name: str | None = None,
    *,
    thinking_enabled: bool = False,
    **kwargs,  
) -> BaseChatModel:
    """
    创建聊天模型实例

    通过 config.yaml 中的配置动态加载模型类并实例化。

    Args:
        name: 模型配置名称（对应 config.yaml 中 models[].name）
              为 None 时使用配置中的第一个模型
        thinking_enabled: 是否开启扩展思考模式（仅当模型 supports_thinking=True 时生效）
        **kwargs: 额外参数，会覆盖配置文件中的设置

    Returns:
        BaseChatModel 实例

    Example:
        # 使用默认模型
        model = create_chat_model()

        # 指定模型
        model = create_chat_model("deepseek-r1", thinking_enabled=True)
    """
    config = get_app_config()

    if not config.models:
        raise ValueError(
            "未配置任何模型。请在 config.yaml 中添加 models 配置。"
        )
    
    # 使用指定模型或第一个模型
    if name is None:
        model_config = config.models[0]
        print(f"未指定模型，使用默认: {model_config.name}")
    else:
        # 按名称查找模型配置
        matched = [m for m in config.models if m.name == name]
        if not matched:
            available = [m for m in config.models]
            raise ValueError(
                f"找不到模型 '{name}'。可用的模型: {available}"
            )
        model_config = matched[0]

    # --- 动态加载模型类 ---
    # 这是配置驱动的核心：通过 'use' 字段动态导入类
    # 例如 'langchain_deepseek:ChatDeepSeek' → ChatDeepSeek 类
    model_class = resolve_class(model_config.use, base_class=BaseChatModel)

    # --- 从配置中提取模型构造参数 ---
    # model_dump 导出所有字段，排除 deerflow 元数据字段
    # 剩余的（如 api_key, temperature, max_tokens 等）透传给模型构造函数
    #
    # 注意：use_responses_api / output_version / stream_chunk_timeout 这三个字段
    # 虽然在 ModelConfig 中显式声明了，但它们本身就是有效的模型构造参数
    # （OpenAI 的 Responses API、结构化输出版本、流式块超时），
    # 所以**不能**排除——必须透传给模型类。只排除纯元数据字段。
    meta_fields = {
        "use",                      # 类路径，不传给模型
        "name",                     # 业务标识，不传给模型
        "display_name",             # UI 显示名，不传给模型
        "description",              # 描述文本，不传给模型
        "supports_thinking",        # 能力声明，不传给模型
        "supports_vision",          # 能力声明，不传给模型
        "supports_reasoning_effort",# 能力声明，不传给模型
        "when_thinking_enabled",    # 思考模式开关逻辑，工厂单独处理
        "when_thinking_disabled",   # 思考模式开关逻辑，工厂单独处理
        "thinking",                 # when_thinking_enabled 的别名，工厂单独处理
    }
    model_params = model_config.model_dump(
        exclude_none=True,
        exclude=meta_fields,
    )

    # --- 处理思考模式 ---
    if thinking_enabled and model_config.supports_thinking:
        # 应用 when_thinking_enabled 或 thinking 快捷配置
        thinking_overrides = (
            model_config.when_thinking_enabled
            or model_config.thinking
            or {}
        )
        # 深度合并（简化处理：仅合并顶层 extra_body）
        for key, value in thinking_overrides.items():
            if key == "extra_body" and "extra_body" in model_params:
                model_params["extra_body"] = {
                    **model_params["extra_body"],
                    **value,
                }
            else:
                model_params[key] = value

    # 合并调用者传入的额外参数
    model_params.update(kwargs)

    # --- 实例化模型 ---
    model_instance = model_class(**model_params)
    return model_instance


def get_default_model() -> BaseChatModel:
    """
    获取默认模型（config.yaml 中第一个模型）

    适合快速开始，不关心具体模型选择时使用。
    """
    return create_chat_model()