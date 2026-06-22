"""子代理系统配置（从 config.yaml 加载）。

对齐 deer-flow ``config/subagents_config.py``，全面对标（v1.2）：

- ``custom_agents``：用户在 config.yaml 里**自定义的子代理类型**（description/
  system_prompt/tools/disallowed_tools/skills/model/max_turns/timeout_seconds）。
  ``task`` 工具的 ``subagent_type`` 既可填内置名（``general-purpose`` / ``bash``），
  也可填任意自定义名。
- ``agents``：**按名的 per-agent 覆盖**（timeout_seconds/max_turns/model/skills）。
  合并优先级（见 [registry.py](../subagents/registry.py) ``get_subagent_config``）：
  built-in → custom → per-agent override。
- helper：``get_timeout_for``/``get_model_for``/``get_max_turns_for``/``get_skills_for``。

mini 额外保留两个开关（deer 把它们放在别处）：``enabled``（主开关，task 工具据此挂载）
与 ``max_concurrent``（与 ``SubagentLimitMiddleware`` 共同保证的并发上限，默认 3）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SubagentOverrideConfig(BaseModel):
    """单个子代理的 per-agent 覆盖（config.yaml ``subagents.agents.<name>``）。"""

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="该子代理的超时秒数（None = 用全局默认）",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="该子代理的最大轮次（None = 用全局或内置默认）",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        description="该子代理用的模型名（None = 继承父 agent 的模型）",
    )
    skills: list[str] | None = Field(
        default=None,
        description="该子代理的技能白名单（None = 继承全部已启用技能，[] = 不加载技能）",
    )


class CustomSubagentConfig(BaseModel):
    """用户在 config.yaml 里声明的自定义子代理类型（``subagents.custom_agents.<name>``）。"""

    description: str = Field(
        description="父 agent 何时应委派给该子代理",
    )
    system_prompt: str = Field(
        description="引导该子代理行为的系统提示词",
    )
    tools: list[str] | None = Field(
        default=None,
        description="工具名白名单（None = 继承父 agent 全部工具）",
    )
    disallowed_tools: list[str] | None = Field(
        default_factory=lambda: ["task", "ask_clarification", "present_files"],
        description="禁用的工具名",
    )
    skills: list[str] | None = Field(
        default=None,
        description="技能名白名单（None = 继承全部已启用技能，[] = 不加载技能）",
    )
    model: str = Field(
        default="inherit",
        description="所用模型——'inherit' 用父 agent 的模型",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        description="停止前的最大 agent 轮次",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="最大执行秒数",
    )


class SubagentsAppConfig(BaseModel):
    """子代理系统配置（``config.yaml`` 的 ``subagents`` 段）。

    合并语义（详见 [registry.py](../subagents/registry.py)）：

    - **内置子代理**（general-purpose/bash）用内置的 max_turns/timeout，但被这里的
      **全局** ``timeout_seconds`` / ``max_turns`` 覆盖（仅当全局值与内置值不同时）。
    - **自定义子代理**用自身在 ``custom_agents`` 里声明的值，全局默认**不**覆盖它们
      （自定义子代理自带默认）。
    - 两类都可被 ``agents.<name>`` 的 per-agent 覆盖再压一层。
    """

    enabled: bool = Field(
        default=True,
        description="是否启用子代理委派（task 工具）",
    )
    max_concurrent: int = Field(
        default=3,
        ge=1,
        description="最大并发子代理数（与 SubagentLimitMiddleware 共同保证）",
    )
    timeout_seconds: int = Field(
        default=1800,
        ge=1,
        description="内置子代理的默认超时秒数（默认 1800 = 30 分钟）；自定义子代理用自身 timeout_seconds，除非给了 per-agent 覆盖",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="所有子代理的可选默认最大轮次覆盖（None = 保持内置默认）",
    )
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="按名的 per-agent 覆盖",
    )
    custom_agents: dict[str, CustomSubagentConfig] = Field(
        default_factory=dict,
        description="用户自定义的子代理类型（按名）",
    )

    def get_timeout_for(self, agent_name: str) -> int:
        """取某子代理的有效超时：per-agent 覆盖 > 全局默认。

        注意：仅当 per-agent 覆盖设了才返回它；否则返回全局 ``timeout_seconds``。
        registry 会判断这个全局值是否真的要覆盖内置/自定义子代理自身的值。
        """
        override = self.agents.get(agent_name)
        if override is not None and override.timeout_seconds is not None:
            return override.timeout_seconds
        return self.timeout_seconds

    def get_model_for(self, agent_name: str) -> str | None:
        """取某子代理的模型覆盖：设了就返回，None 表示继承父 agent。"""
        override = self.agents.get(agent_name)
        if override is not None and override.model is not None:
            return override.model
        return None

    def get_max_turns_for(self, agent_name: str, builtin_default: int) -> int:
        """取某子代理的有效 max_turns：per-agent 覆盖 > 全局 max_turns > 内置默认。"""
        override = self.agents.get(agent_name)
        if override is not None and override.max_turns is not None:
            return override.max_turns
        if self.max_turns is not None:
            return self.max_turns
        return builtin_default

    def get_skills_for(self, agent_name: str) -> list[str] | None:
        """取某子代理的技能覆盖：设了就返回，None 表示继承全部已启用技能。"""
        override = self.agents.get(agent_name)
        if override is not None and override.skills is not None:
            return override.skills
        return None


_subagents_config: SubagentsAppConfig = SubagentsAppConfig()


def get_subagents_app_config() -> SubagentsAppConfig:
    """取当前子代理配置（进程级单例）。"""
    return _subagents_config


def load_subagents_config_from_dict(config_dict: dict) -> None:
    """从字典加载子代理配置（config.yaml 解析后调用）。"""
    global _subagents_config
    _subagents_config = SubagentsAppConfig(**config_dict)

    overrides_summary = {}
    for name, override in _subagents_config.agents.items():
        parts = []
        if override.timeout_seconds is not None:
            parts.append(f"timeout={override.timeout_seconds}s")
        if override.max_turns is not None:
            parts.append(f"max_turns={override.max_turns}")
        if override.model is not None:
            parts.append(f"model={override.model}")
        if override.skills is not None:
            parts.append(f"skills={override.skills}")
        if parts:
            overrides_summary[name] = ", ".join(parts)

    custom_agents_names = list(_subagents_config.custom_agents.keys())

    if overrides_summary or custom_agents_names:
        logger.info(
            "Subagents config loaded: default timeout=%ss, default max_turns=%s, per-agent overrides=%s, custom_agents=%s",
            _subagents_config.timeout_seconds,
            _subagents_config.max_turns,
            overrides_summary or "none",
            custom_agents_names or "none",
        )
