"""
应用配置模块

从 config.yaml 加载所有配置，支持环境变量展开（$VAR 语法）
"""
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .model_config import ModelConfig
from .paths import get_config_file, get_env_file


class AppConfig(BaseModel):
    """应用总配置，对应 config.yaml 文件"""

    # --- 基本配置 ---
    log_level: str = "info"
    """日志级别: debug, info, warning, error"""

    # --- 模型配置 ---
    models: list[ModelConfig] = []
    """模型列表，至少需要配置一个"""

    # --- 工具配置 ---
    tools: list[dict[str, Any]] = []
    """工具定义列表"""

    tool_groups: list[dict[str, Any]] = []
    """工具分组"""

    # --- 记忆系统 ---
    memory: dict[str, Any] = Field(default_factory=lambda: {
        "enabled": True,
        "max_facts": 100,
        "debounce_seconds": 30,
    })
    """记忆系统配置"""

    # --- 子代理配置 ---
    subagents: dict[str, Any] = Field(default_factory=lambda: {
        "enabled": True,
        "max_concurrent": 3,
    })
    """子代理系统配置"""

    # --- 沙箱配置 ---
    sandbox: dict[str, Any] = Field(default_factory=lambda: {
        "use": "deerflow.sandbox.local:LocalSandboxProvider",
    })
    """代码执行沙箱配置 （必须配置）"""

    # --- 其他可选配置 ---
    skills: dict[str, Any] = Field(default_factory=dict)
    title: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    summarization: dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    loop_detection: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=lambda: {"backend": "sqlite"})
    checkpointer: dict[str, Any] | None = None
    tracing: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)


# --- 全局配置单例 ---

_app_config: AppConfig | None = None
_config_mtime: float | None = None


def _expand_env_vars(value: Any) -> Any:
    """递归展开 $VAR 和 ${VAR} 格式的环境变量"""
    if isinstance(value, str):
        # 匹配 $VAR 或 ${VAR}
        def replacer(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))
        return re.sub(r'\$(\w+)|\$\{(\w+)\}', replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def load_config_from_yaml(config_path: Path | None = None) -> dict[str, Any]:
    """从 YAML 文件加载配置并展开环境变量"""
    if config_path is None:
        config_path = get_config_file()

    if not config_path.exists():
        print(f"⚠ 配置文件不存在: {config_path}")
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _expand_env_vars(raw)


def get_app_config() -> AppConfig:
    """
    获取应用配置单例

    首次调用时加载配置文件和环境变量。
    后续调用时检查文件修改时间，自动热重载。

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
    """强制重新加载配置"""
    global _app_config, _config_mtime
    _app_config = None
    _config_mtime = None
    return get_app_config()