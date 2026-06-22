"""子代理注册表——按名解析 ``SubagentConfig``，应用 config.yaml 覆盖。

合并优先级（镜像 Codex 的 config 分层）：

1. **内置子代理**（general-purpose / bash）—— 见 [builtins/__init__.py](builtins/__init__.py)。
2. **自定义子代理**—— config.yaml ``subagents.custom_agents.<name>``。
3. **per-agent 覆盖**—— config.yaml ``subagents.agents.<name>`` 的 timeout/max_turns/model/skills。

``get_subagent_config`` 按上面顺序找到基线 config，再压 per-agent 覆盖层。全局默认
（``subagents.timeout_seconds`` / ``max_turns``）**只覆盖内置子代理**，不覆盖自定义子代理
（自定义子代理自带默认）——见 [config/subagents_config.py](../config/subagents_config.py) docstring。
"""

import logging
from dataclasses import replace
from typing import Any

from deerflow.sandbox.security import is_host_bash_allowed
from deerflow.subagents.builtins import BUILTIN_SUBAGENTS
from deerflow.subagents.config import SubagentConfig

logger = logging.getLogger(__name__)


def _resolve_subagents_app_config(app_config: Any | None = None):
    """从 AppConfig 取 ``subagents`` 段；传 None 则用进程级单例。"""
    if app_config is None:
        from deerflow.config.subagents_config import get_subagents_app_config

        return get_subagents_app_config()
    return getattr(app_config, "subagents", app_config)


def _build_custom_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """从 config.yaml ``custom_agents`` 段构造一个 SubagentConfig。

    Args:
        name: 自定义子代理名。
        app_config: 可选 AppConfig 或 SubagentsAppConfig。

    Returns:
        在 custom_agents 里找到就返回 SubagentConfig，否则 None。
    """
    subagents_config = _resolve_subagents_app_config(app_config)
    custom = subagents_config.custom_agents.get(name)
    if custom is None:
        return None

    return SubagentConfig(
        name=name,
        description=custom.description,
        system_prompt=custom.system_prompt,
        tools=custom.tools,
        disallowed_tools=custom.disallowed_tools,
        skills=custom.skills,
        model=custom.model,
        max_turns=custom.max_turns,
        timeout_seconds=custom.timeout_seconds,
    )


def get_subagent_config(name: str, *, app_config: Any | None = None) -> SubagentConfig | None:
    """按名取子代理配置，应用 config.yaml 覆盖。

    解析顺序：
    1. 内置子代理（general-purpose / bash），找不到回退 custom_agents。
    2. config.yaml custom_agents 段的自定义子代理。
    3. config.yaml agents 段的 per-agent 覆盖（timeout/max_turns/model/skills）。

    Args:
        name: 子代理名。
        app_config: 可选 AppConfig 或 SubagentsAppConfig，用来解析覆盖。

    Returns:
        找到（含任意 config.yaml 覆盖）返回 SubagentConfig，否则 None。
    """
    # Step 1: 先查内置，再回退 custom_agents
    config = BUILTIN_SUBAGENTS.get(name)
    if config is None:
        config = _build_custom_subagent_config(name, app_config=app_config)
    if config is None:
        return None

    # Step 2: 应用 config.yaml agents 段的 per-agent 覆盖。
    # 这里只应用显式的 per-agent 覆盖。全局默认（顶层 timeout_seconds / max_turns）
    # 适用于内置子代理，但**不得**覆盖自定义子代理自身的值——自定义子代理在
    # custom_agents 段定义自己的默认。
    subagents_config = _resolve_subagents_app_config(app_config)
    is_builtin = name in BUILTIN_SUBAGENTS
    agent_override = subagents_config.agents.get(name)

    overrides = {}

    # timeout：per-agent 覆盖 > 全局默认（仅内置）> config 自身值
    if agent_override is not None and agent_override.timeout_seconds is not None:
        if agent_override.timeout_seconds != config.timeout_seconds:
            logger.debug("Subagent '%s': timeout overridden (%ss -> %ss)", name, config.timeout_seconds, agent_override.timeout_seconds)
            overrides["timeout_seconds"] = agent_override.timeout_seconds
    elif is_builtin and subagents_config.timeout_seconds != config.timeout_seconds:
        logger.debug("Subagent '%s': timeout from global default (%ss -> %ss)", name, config.timeout_seconds, subagents_config.timeout_seconds)
        overrides["timeout_seconds"] = subagents_config.timeout_seconds

    # max_turns：per-agent 覆盖 > 全局默认（仅内置）> config 自身值
    if agent_override is not None and agent_override.max_turns is not None:
        if agent_override.max_turns != config.max_turns:
            logger.debug("Subagent '%s': max_turns overridden (%s -> %s)", name, config.max_turns, agent_override.max_turns)
            overrides["max_turns"] = agent_override.max_turns
    elif is_builtin and subagents_config.max_turns is not None and subagents_config.max_turns != config.max_turns:
        logger.debug("Subagent '%s': max_turns from global default (%s -> %s)", name, config.max_turns, subagents_config.max_turns)
        overrides["max_turns"] = subagents_config.max_turns

    # model：仅 per-agent 覆盖（无全局默认）
    effective_model = subagents_config.get_model_for(name)
    if effective_model is not None and effective_model != config.model:
        logger.debug("Subagent '%s': model overridden (%s -> %s)", name, config.model, effective_model)
        overrides["model"] = effective_model

    # skills：仅 per-agent 覆盖（无全局默认）
    effective_skills = subagents_config.get_skills_for(name)
    if effective_skills is not None and effective_skills != config.skills:
        logger.debug("Subagent '%s': skills overridden (%s -> %s)", name, config.skills, effective_skills)
        overrides["skills"] = effective_skills

    if overrides:
        config = replace(config, **overrides)

    return config


def list_subagents(*, app_config: Any | None = None) -> list[SubagentConfig]:
    """列出全部可用子代理配置（含 config.yaml 覆盖）。"""
    configs = []
    for name in get_subagent_names(app_config=app_config):
        config = get_subagent_config(name, app_config=app_config)
        if config is not None:
            configs.append(config)
    return configs


def get_subagent_names(*, app_config: Any | None = None) -> list[str]:
    """取全部可用子代理名（内置 + 自定义）。"""
    names = list(BUILTIN_SUBAGENTS.keys())

    # 合并 config.yaml 的 custom_agents
    subagents_config = _resolve_subagents_app_config(app_config)
    for custom_name in subagents_config.custom_agents:
        if custom_name not in names:
            names.append(custom_name)

    return names


def get_available_subagent_names(*, app_config: Any | None = None) -> list[str]:
    """取当前运行时应暴露给 LLM 的子代理名。

    按沙箱配置过滤：host bash 未被放行（``is_host_bash_allowed()=False``）时隐藏
    ``bash`` 子代理（LocalSandboxProvider 非 AIO 时 host bash 不安全）。
    """
    names = get_subagent_names(app_config=app_config)
    try:
        host_bash_allowed = is_host_bash_allowed(app_config) if hasattr(app_config, "sandbox") else is_host_bash_allowed()
    except Exception:
        logger.debug("Could not determine host bash availability; exposing all subagents")
        return names

    if not host_bash_allowed:
        names = [name for name in names if name != "bash"]
    return names
