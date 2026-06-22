"""子代理配置定义（``SubagentConfig`` dataclass）。

一个 ``SubagentConfig`` 描述「一个子代理长什么样」——名字、何时委派给它、系统提示词、
工具白/黑名单、技能、模型、最大轮次、超时。内置子代理（general-purpose/bash）与
用户自定义子代理（config.yaml ``subagents.custom_agents``）都归一成这个 dataclass。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig


@dataclass
class SubagentConfig:
    """单个子代理的配置。

    Attributes:
        name: 子代理的唯一标识（如 ``general-purpose`` / ``bash`` / 自定义名）。
        description: 父 agent 何时应委派给该子代理（写进 ``task`` 工具描述供 LLM 选择）。
        system_prompt: 引导子代理行为的系统提示词。None 表示不带。
        tools: 工具名白名单。None = 继承父 agent 全部工具；给出列表则只留这些。
        disallowed_tools: 工具名黑名单（总是排除）。默认 ``["task"]`` 防子代理再嵌套。
        skills: 技能名白名单。None = 继承全部已启用技能；``[]`` = 不加载技能；列表 = 只这些。
        model: 所用模型——``'inherit'`` 用父 agent 的模型，否则用指定模型名。
        max_turns: 停止前的最大 agent 轮次。内置 general-purpose=150、bash=60；自定义=50。
        timeout_seconds: 裸的执行时间上限兜底。内置子代理的有效值由全局
            ``subagents.timeout_seconds``（默认 1800 = 30 分钟）经 registry 压一层；
            这里的 900 仅在没有不同全局值时生效。
    """

    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900


def _default_model_name(app_config: "AppConfig") -> str:
    """取 config.yaml 里第一个模型名（兜底默认模型）。"""
    if not app_config.models:
        raise ValueError("No chat models are configured. Please configure at least one model in config.yaml.")
    return app_config.models[0].name


def resolve_subagent_model_name(
    config: SubagentConfig,
    parent_model: str | None,
    *,
    app_config: "AppConfig | None" = None,
) -> str:
    """解析子代理实际要用的模型名。

    优先级：子代理显式 model（非 ``inherit``） > 父 agent 模型 > config 第一个模型。
    """
    if config.model != "inherit":
        return config.model

    if parent_model is not None:
        return parent_model

    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()
    return _default_model_name(app_config)
