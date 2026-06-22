"""应用配置模块。

从 config.yaml 加载所有配置，支持环境变量展开（$VAR 语法）。所有子配置已强类型
化（pydantic BaseModel），带安全默认值——空 config.yaml 能以 memory 模式启动
（红线 #25）。需重启的基础设施字段用 ``startup-only:`` 标记（见 reload_boundary）。
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .checkpointer_config import CheckpointerConfig
from .circuit_breaker_config import CircuitBreakerConfig
from .database_config import DatabaseConfig
from .loop_detection_config import LoopDetectionConfig
from .memory_config import MemoryConfig
from .model_config import ModelConfig
from .paths import get_config_file, get_env_file
from .reload_boundary import format_field_description
from .run_events_config import RunEventsConfig
from .safety_finish_reason_config import SafetyFinishReasonConfig
from .sandbox_config import SandboxConfig
from .skill_evolution_config import SkillEvolutionConfig
from .skills_config import SkillsConfig
from .stream_bridge_config import StreamBridgeConfig
from .subagents_config import SubagentsAppConfig
from .summarization_config import SummarizationConfig
from .title_config import TitleConfig
from .token_usage_config import TokenUsageConfig
from .tool_output_config import ToolOutputConfig
from .tool_search_config import ToolSearchConfig
from .uploads_config import UploadsConfig


class AppConfig(BaseModel):
    """应用总配置，对应 config.yaml 文件。"""

    model_config = ConfigDict(extra="allow")

    # --- 元信息 ---
    config_version: int = Field(
        default=0,
        description="配置 schema 版本。低于 config.example.yaml 时启动告警；缺失视为 0。",
    )

    log_level: str = Field(
        default="info",
        description=format_field_description(
            "log_level",
            field_doc="deerflow/app 模块的日志级别（debug/info/warning/error）；不影响第三方库。",
        ),
    )

    # --- 模型 / 工具 ---
    models: list[ModelConfig] = Field(default_factory=list, description="模型列表")

    tools: list[dict[str, Any]] = Field(
        default_factory=list,
        description="工具定义列表（M15 落地时类型化为 ToolConfig）",
    )

    tool_groups: list[dict[str, Any]] = Field(
        default_factory=list,
        description="工具分组（M15 落地时类型化为 ToolGroupConfig）",
    )

    # --- 工具相关子系统 ---
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig, description="token 用量跟踪配置")
    tool_output: ToolOutputConfig = Field(default_factory=ToolOutputConfig, description="工具输出预算保护配置")
    tool_search: ToolSearchConfig = Field(default_factory=ToolSearchConfig, description="工具搜索 / 延迟加载配置")

    uploads: UploadsConfig = Field(default_factory=UploadsConfig, description="文件上传 + markitdown 转换配置（M23）")

    # --- 技能 ---
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="技能系统配置")
    skill_evolution: SkillEvolutionConfig = Field(
        default_factory=SkillEvolutionConfig,
        description="agent 自管理技能演进配置",
    )

    # --- 质量 / 辅助 ---
    title: TitleConfig = Field(default_factory=TitleConfig, description="自动标题生成配置")
    summarization: SummarizationConfig = Field(
        default_factory=SummarizationConfig,
        description="对话摘要配置",
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="记忆子系统配置")
    subagents: SubagentsAppConfig = Field(default_factory=SubagentsAppConfig, description="子代理系统配置")
    loop_detection: LoopDetectionConfig = Field(
        default_factory=LoopDetectionConfig,
        description="循环检测中间件配置",
    )
    safety_finish_reason: SafetyFinishReasonConfig = Field(
        default_factory=SafetyFinishReasonConfig,
        description="provider 安全 finish_reason 拦截中间件配置",
    )
    circuit_breaker: CircuitBreakerConfig = Field(
        default_factory=CircuitBreakerConfig,
        description="LLM 调用熔断配置（连续失败短路，M16 LLMErrorHandlingMiddleware）",
    )

    # --- 沙箱（基础设施，需重启）---
    sandbox: SandboxConfig = Field(
        default_factory=lambda: SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        description=format_field_description(
            "sandbox",
            field_doc="沙箱 provider 配置（本地文件系统或 Docker aio）。空配置默认 LocalSandboxProvider。",
        ),
    )

    # --- 持久化 / 运行时（基础设施，需重启）---
    database: DatabaseConfig = Field(
        default_factory=DatabaseConfig,
        description=format_field_description(
            "database",
            field_doc="统一数据库后端（run/反馈元数据，支持 memory/sqlite/postgres）。",
        ),
    )
    run_events: RunEventsConfig = Field(
        default_factory=RunEventsConfig,
        description=format_field_description(
            "run_events",
            field_doc="运行事件存储后端（memory 开发、db 生产、jsonl 轻量单节点）。",
        ),
    )
    checkpointer: CheckpointerConfig | None = Field(
        default=None,
        description=format_field_description(
            "checkpointer",
            field_doc="LangGraph 状态持久化 checkpointer 配置。None 表示用 database 派生的默认。",
        ),
    )
    stream_bridge: StreamBridgeConfig | None = Field(
        default=None,
        description=format_field_description(
            "stream_bridge",
            field_doc="连接 agent worker 与 SSE 端点的流桥。None 表示用内存默认。",
        ),
    )

    # --- 追踪（M12 落地，当前环境变量驱动）---
    tracing: dict[str, Any] = Field(default_factory=dict, description="追踪配置（M12 落地）")

    @field_validator("models", "tools", "tool_groups", mode="before")
    @classmethod
    def _coerce_null_list_sections(cls, value: Any) -> Any:
        """把「存在但为空」的配置节当成空列表。

        把顶层 YAML 键下全注释掉（如 ``models:`` 下面只有注释）会让 PyYAML 解析成
        ``None``。不处理的话会报晦涩的 ``Input should be a valid list``。把 ``None``
        归一成 ``[]``，与字段 ``default_factory=list`` 一致。
        """
        return [] if value is None else value

    def get_model_config(self, name: str | None) -> ModelConfig | None:
        """按名称查找模型配置。

        Args:
            name: 模型名（对应 config.yaml 中 models[].name）。为 None 时返回第一个模型（默认模型）。
        Returns:
            ModelConfig 实例；未配置任何模型或找不到该名称时返回 None。
        """
        if not self.models:
            return None
        if name is None:
            return self.models[0]
        for m in self.models:
            if m.name == name:
                return m
        return None

    def get_tool_config(self, name: str) -> dict[str, Any] | None:
        """按名称查找工具配置（对应 config.yaml 中 tools[].name）。

        mini 的 ``tools`` 当前是 ``list[dict]``（M15 落地时类型化为 ToolConfig），
        所以这里返回**原始 dict**——调用方用 ``config.get("api_key", default)``
        读「name/group/use」之外的额外字段。deer 用 pydantic ToolConfig 的
        ``model_extra`` 承载这些额外字段；mini 直接读 dict，等价。

        Args:
            name: 工具名（如 ``"web_search"`` / ``"web_fetch"`` / ``"image_search"``）。
        Returns:
            匹配的工具配置 dict；未找到时返回 None。
        """
        return next((t for t in self.tools if isinstance(t, dict) and t.get("name") == name), None)


# --- 全局配置单例 ---

_app_config: AppConfig | None = None
_config_mtime: float | None = None


def _expand_env_vars(value: Any) -> Any:
    """递归展开 $VAR 和 ${VAR} 格式的环境变量（未设置的保留占位文本）。"""
    if isinstance(value, str):
        # 匹配 $VAR 或 ${VAR}
        def replacer(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))

        return re.sub(r"\$(\w+)|\$\{(\w+)\}", replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def load_config_from_yaml(config_path: Path | None = None) -> dict[str, Any]:
    """从 YAML 文件加载配置并展开环境变量。"""
    if config_path is None:
        config_path = get_config_file()

    if not config_path.exists():
        print(f"⚠ 配置文件不存在: {config_path}")
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _expand_env_vars(raw)


def get_app_config() -> AppConfig:
    """获取应用配置单例。

    首次调用时加载配置文件和环境变量。后续调用时检查文件修改时间，自动热重载。

    Returns:
        AppConfig 实例
    """
    global _app_config, _config_mtime

    config_path = get_config_file()
    current_mtime = config_path.stat().st_mtime if config_path.exists() else None

    # 检查是否需要重新加载
    if _app_config is not None and current_mtime == _config_mtime:
        return _app_config

    # 加载环境变量
    env_file = get_env_file()
    if env_file.exists():
        load_dotenv(env_file)

    # 加载 YAML 配置
    yaml_config = load_config_from_yaml(config_path)

    # 创建配置对象
    _app_config = AppConfig(**yaml_config)
    _config_mtime = current_mtime

    # 检查模型配置
    if not _app_config.models:
        print("⚠ 警告: 未配置任何模型，请在 config.yaml 中添加 models 配置")

    return _app_config


def reload_config() -> AppConfig:
    """强制重新加载配置。"""
    global _app_config, _config_mtime
    _app_config = None
    _config_mtime = None
    return get_app_config()
