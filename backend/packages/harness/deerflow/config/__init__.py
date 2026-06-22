"""配置模块。

提供统一的配置访问接口。所有子配置已强类型化（pydantic BaseModel），见各 ``*_config.py``。
"""

import os

from .agents_config import (
    AGENT_NAME_PATTERN,
    SOUL_FILENAME,
    AgentConfig,
    list_custom_agents,
    load_agent_config,
    load_agent_soul,
    resolve_agent_dir,
    validate_agent_name,
)
from .app_config import AppConfig, get_app_config, load_config_from_yaml, reload_config
from .checkpointer_config import CheckpointerConfig
from .circuit_breaker_config import CircuitBreakerConfig
from .database_config import DatabaseConfig
from .extensions_config import ExtensionsConfig, McpOAuthConfig, McpServerConfig
from .loop_detection_config import LoopDetectionConfig
from .memory_config import MemoryConfig
from .model_config import ModelConfig
from .paths import (
    PROJECT_ROOT,
    VIRTUAL_PATH_PREFIX,
    Paths,
    existing_project_file,
    get_config_file,
    get_env_file,
    get_paths,
    project_root,
    resolve_path,
    runtime_home,
)
from .reload_boundary import (
    STARTUP_ONLY_FIELDS,
    format_field_description,
    is_startup_only_field,
    iter_startup_only_field_paths,
)
from .run_events_config import RunEventsConfig
from .safety_finish_reason_config import SafetyDetectorConfig, SafetyFinishReasonConfig
from .sandbox_config import SandboxConfig, VolumeMountConfig
from .skill_evolution_config import SkillEvolutionConfig
from .skills_config import SkillsConfig
from .stream_bridge_config import StreamBridgeConfig
from .subagents_config import SubagentsAppConfig
from .summarization_config import ContextSize, SummarizationConfig
from .title_config import TitleConfig
from .token_usage_config import TokenUsageConfig
from .tool_output_config import ToolOutputConfig
from .tool_search_config import ToolSearchConfig


def get_enabled_tracing_providers() -> list[str]:
    """返回显式启用的追踪 provider 列表。

    通过环境变量 LANGSMITH_TRACING / LANGFUSE_TRACING 控制。返回空列表表示无追踪。
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
                raise ValueError("LANGFUSE_TRACING=true 但未设置 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY")


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
    # app config
    "AppConfig",
    "get_app_config",
    "load_config_from_yaml",
    "reload_config",
    # agents_config（自定义 agent，M22）
    "AGENT_NAME_PATTERN",
    "SOUL_FILENAME",
    "AgentConfig",
    "list_custom_agents",
    "load_agent_config",
    "load_agent_soul",
    "resolve_agent_dir",
    "validate_agent_name",
    # model
    "ModelConfig",
    # 子配置
    "CheckpointerConfig",
    "CircuitBreakerConfig",
    "DatabaseConfig",
    "LoopDetectionConfig",
    "MemoryConfig",
    "RunEventsConfig",
    "SafetyDetectorConfig",
    "SafetyFinishReasonConfig",
    "SandboxConfig",
    "SkillEvolutionConfig",
    "SkillsConfig",
    "StreamBridgeConfig",
    "SubagentsAppConfig",
    "SummarizationConfig",
    "ContextSize",
    "TitleConfig",
    "TokenUsageConfig",
    "ToolOutputConfig",
    "ToolSearchConfig",
    "VolumeMountConfig",
    # extensions
    "ExtensionsConfig",
    "McpOAuthConfig",
    "McpServerConfig",
    # paths
    "PROJECT_ROOT",
    "VIRTUAL_PATH_PREFIX",
    "Paths",
    "existing_project_file",
    "get_config_file",
    "get_env_file",
    "get_paths",
    "project_root",
    "resolve_path",
    "runtime_home",
    # reload boundary
    "STARTUP_ONLY_FIELDS",
    "format_field_description",
    "is_startup_only_field",
    "iter_startup_only_field_paths",
    # tracing
    "get_enabled_tracing_providers",
    "get_tracing_config",
    "validate_enabled_tracing_providers",
]
