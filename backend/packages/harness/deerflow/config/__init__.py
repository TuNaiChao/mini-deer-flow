"""
配置模块

提供统一的配置访问接口
"""
import os

from .app_config import AppConfig, get_app_config, reload_config
from .model_config import ModelConfig
from .paths import PROJECT_ROOT, get_config_file, get_env_file


def get_enabled_tracing_providers() -> list[str]:
    """返回显式启用的追踪 provider 列表。

    通过环境变量 LANGSMITH_TRACING / LANGFUSE_TRACING 控制。
    返回空列表表示无追踪。
    """
    providers = []
    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        providers.append("langsmith")
    if os.getenv("LANGFUSE_TRACING", "").lower() == "true":
        providers.append("langfuse")
    return providers


def validate_enabled_tracing_providers() -> None:
    """验证启用的追踪 provider 配置完整。"""
    providers = get_enabled_tracing_providers()
    for p in providers:
        if p == "langsmith" and not os.getenv("LANGSMITH_API_KEY"):
            raise ValueError("LANGSMITH_TRACING=true 但未设置 LANGSMITH_API_KEY")
        if p == "langfuse":
            if not os.getenv("LANGFUSE_SECRET_KEY") or not os.getenv("LANGFUSE_PUBLIC_KEY"):
                raise ValueError(
                    "LANGFUSE_TRACING=true 但未设置 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY"
                )


def get_tracing_config():
    """获取追踪配置（从环境变量读取各 provider 的连接参数）。

    返回一个简单命名空间对象，字段为 langsmith / langfuse。
    """
    from types import SimpleNamespace

    langsmith = SimpleNamespace(
        project=os.getenv("LANGSMITH_PROJECT", "deerflow"),
        api_key=os.getenv("LANGSMITH_API_KEY", ""),
    )
    langfuse = SimpleNamespace(
        secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
        host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )
    return SimpleNamespace(langsmith=langsmith, langfuse=langfuse)


__all__ = [
    "AppConfig",
    "get_app_config",
    "reload_config",
    "ModelConfig",
    "PROJECT_ROOT",
    "get_config_file",
    "get_env_file",
    "get_enabled_tracing_providers",
    "get_tracing_config",
    "validate_enabled_tracing_providers",
]