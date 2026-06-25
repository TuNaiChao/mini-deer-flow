"""Lead agent factory——LangGraph 图入口（langgraph.json 注册的就是它）。

``make_lead_agent(config) → CompiledStateGraph``。config 驱动：从运行时配置解析模型 /
agent 名 / plan_mode / subagent，按 [ALIGNMENT_OUTLINE.md](../../../../../../../docs/ALIGNMENT_OUTLINE.md)
M17 装配。

不变量——tracing 回调挂载位置
==============================

tracing 回调（Langfuse / LangSmith）挂在**图调用根**（见 ``_make_lead_agent`` 里
``build_tracing_callbacks()`` 往 ``config["callbacks"]`` append 的那段）。本模块里**每个**
``create_chat_model(...)`` 调用——以及从本图可达的任何中间件（如 TitleMiddleware）里的——
**必须**传 ``attach_tracing=False``。

漏传这个 flag 会发重复 span（图根一个、模型一个），还会让 Langfuse handler 的
``propagate_attributes`` 路径失效，``session_id`` / ``user_id`` 永远到不了 trace。当前四个点：
bootstrap agent、默认 agent、summarization 中间件、TitleMiddleware 里的异步路径。任何新的
图内 ``create_chat_model`` 调用都得加进这个清单并传该 flag。
"""

from __future__ import annotations

import logging

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig

from deerflow.agents.lead_agent.prompt import apply_prompt_template
from deerflow.agents.middlewares import build_middlewares
from deerflow.agents.thread_state import ThreadState
from deerflow.config import get_app_config
from deerflow.config.agents_config import load_agent_config, validate_agent_name
from deerflow.models import create_chat_model
from deerflow.skills.tool_policy import filter_tools_by_skill_allowed_tools
from deerflow.skills.types import Skill
from deerflow.tracing import build_tracing_callbacks

logger = logging.getLogger(__name__)

# bootstrap agent 的技能白名单故意收窄——agent 创建流程在自定义 agent 自己的 config 存在
# 之前必须保持确定性。
_BOOTSTRAP_SKILL_NAMES = {"bootstrap"}


def _get_runtime_config(config: RunnableConfig) -> dict:
    """合并 legacy configurable 选项与 LangGraph runtime context。"""
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg


def _resolve_model_name(requested_model_name: str | None = None, *, app_config=None) -> str:
    """安全解析运行时模型名，非法时回退默认。无模型配置则抛错。"""

    app_config = app_config or get_app_config()
    default_model_name = app_config.models[0].name if app_config.models else None
    if default_model_name is None:
        raise ValueError("No chat models are configured. Please configure at least one model in config.yaml.")

    if requested_model_name and app_config.get_model_config(requested_model_name):
        return requested_model_name

    if requested_model_name and requested_model_name != default_model_name:
        logger.warning(f"Model '{requested_model_name}' not found in config; fallback to default model '{default_model_name}'.")
    return default_model_name


def _available_skill_names(agent_config, is_bootstrap: bool) -> set[str] | None:
    """解析当前 agent 可见的技能白名单。

    - bootstrap：固定 ``{"bootstrap"}``（创建流程要确定性）；
    - 自定义 agent 配了 ``skills``：用它的白名单；
    - 否则 ``None`` = 全部启用的技能。
    """
    if is_bootstrap:
        return set(_BOOTSTRAP_SKILL_NAMES)
    if agent_config and agent_config.skills is not None:
        return set(agent_config.skills)
    return None


def _load_enabled_skills_for_tool_policy(available_skills: set[str] | None, *, app_config) -> list[Skill]:
    """取 enabled skills（按白名单过滤），供工具策略过滤用。

    工具策略（allowed-tools 白名单收紧）依赖技能列表——技能没加载就过滤不了。加载失败直接抛
    （技能是安全相关，不能静默放行全部工具）。
    """
    from deerflow.agents.lead_agent.prompt import get_enabled_skills_for_config

    skills = get_enabled_skills_for_config(app_config)

    if available_skills is None:
        return skills
    return [skill for skill in skills if skill.name in available_skills]


def make_lead_agent(config: RunnableConfig):
    """LangGraph 图工厂；签名保持与 LangGraph Server 兼容。"""
    runtime_config = _get_runtime_config(config)
    runtime_app_config = runtime_config.get("app_config")
    return _make_lead_agent(config, app_config=runtime_app_config or get_app_config())


def _make_lead_agent(config: RunnableConfig, *, app_config):
    """真正的装配逻辑——从 config 解析一切，组装出 CompiledStateGraph。"""
    # 延迟导入避免循环依赖
    from deerflow.tools import get_available_tools
    from deerflow.tools.builtins import setup_agent, update_agent
    from deerflow.tools.builtins.tool_search import assemble_deferred_tools

    cfg = _get_runtime_config(config)
    resolved_app_config = app_config

    thinking_enabled = cfg.get("thinking_enabled", True)
    reasoning_effort = cfg.get("reasoning_effort", None)
    requested_model_name: str | None = cfg.get("model_name") or cfg.get("model")
    is_plan_mode = cfg.get("is_plan_mode", False)
    subagent_enabled = cfg.get("subagent_enabled", False)
    max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
    is_bootstrap = cfg.get("is_bootstrap", False)
    agent_name = validate_agent_name(cfg.get("agent_name"))

    agent_config = load_agent_config(agent_name) if not is_bootstrap else None
    available_skills = _available_skill_names(agent_config, is_bootstrap)
    # 自定义 agent 的模型覆盖（若有）；None 让 _resolve_model_name 选默认
    agent_model_name = agent_config.model if agent_config and agent_config.model else None

    # 最终模型名解析：请求 → agent 配置 → 全局默认，未知名回退
    model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)

    model_config = resolved_app_config.get_model_config(model_name)

    if model_config is None:
        raise ValueError("No chat model could be resolved. Please configure at least one model in config.yaml or provide a valid 'model_name'/'model' in the request.")
    if thinking_enabled and not model_config.supports_thinking:
        logger.warning(f"Thinking mode is enabled but model '{model_name}' does not support it; fallback to non-thinking mode.")
        thinking_enabled = False

    logger.info(
        "Create Agent(%s) -> thinking_enabled: %s, reasoning_effort: %s, model_name: %s, is_plan_mode: %s, subagent_enabled: %s, max_concurrent_subagents: %s",
        agent_name or "default",
        thinking_enabled,
        reasoning_effort,
        model_name,
        is_plan_mode,
        subagent_enabled,
        max_concurrent_subagents,
    )

    # 注入 run metadata（LangSmith trace 打标）
    if "metadata" not in config:
        config["metadata"] = {}

    config["metadata"].update(
        {
            "agent_name": agent_name or "default",
            "model_name": model_name or "default",
            "thinking_enabled": thinking_enabled,
            "reasoning_effort": reasoning_effort,
            "is_plan_mode": is_plan_mode,
            "subagent_enabled": subagent_enabled,
            "tool_groups": agent_config.tool_groups if agent_config else None,
            "available_skills": sorted(available_skills) if available_skills is not None else None,
        }
    )

    # 在图调用根注入 tracing 回调——让一次 LangGraph run 产生一条 trace（所有节点 / LLM /
    # 工具调用作为子 span），且让 Langfuse handler 看到 ``on_chain_start(parent_run_id=None)``
    # 真正把 ``langfuse_session_id`` / ``langfuse_user_id`` 从 ``config["metadata"]`` 提到 trace 上。
    # 不在根挂的话模型是嵌套观测，handler 会剥掉 ``langfuse_*`` 键。
    tracing_callbacks = build_tracing_callbacks()
    if tracing_callbacks:
        existing = config.get("callbacks") or []
        if not isinstance(existing, list):
            existing = list(existing)
        config["callbacks"] = [*existing, *tracing_callbacks]

    skills_for_tool_policy = _load_enabled_skills_for_tool_policy(available_skills, app_config=resolved_app_config)

    if is_bootstrap:
        # 特殊 bootstrap agent——用最小 prompt 走初始自定义 agent 创建流程。
        # 技能集故意收窄，让 agent 创建在自定义 agent 自己的 config 存在之前保持确定性。
        raw_tools = get_available_tools(model_name=model_name, subagent_enabled=subagent_enabled, app_config=resolved_app_config) + [setup_agent]
        filtered = filter_tools_by_skill_allowed_tools(raw_tools, skills_for_tool_policy)
        final_tools, setup = assemble_deferred_tools(filtered, enabled=resolved_app_config.tool_search.enabled)
        return create_agent(
            model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled, app_config=resolved_app_config, attach_tracing=False),
            tools=final_tools,
            middleware=build_middlewares(
                config,
                model_name=model_name,
                available_skills=set(_BOOTSTRAP_SKILL_NAMES),
                app_config=resolved_app_config,
                deferred_setup=setup,
            ),
            system_prompt=apply_prompt_template(
                subagent_enabled=subagent_enabled,
                max_concurrent_subagents=max_concurrent_subagents,
                available_skills=set(_BOOTSTRAP_SKILL_NAMES),
                app_config=resolved_app_config,
                deferred_names=setup.deferred_names,
            ),
            state_schema=ThreadState,
        )

    # 自定义 agent 能经 update_agent 自更新 SOUL.md / config。默认 agent（无 agent_name）看不到这个工具。
    extra_tools = [update_agent] if agent_name else []
    # 默认 lead agent（行为不变）
    raw_tools = get_available_tools(model_name=model_name, groups=agent_config.tool_groups if agent_config else None, subagent_enabled=subagent_enabled, app_config=resolved_app_config)
    filtered = filter_tools_by_skill_allowed_tools(raw_tools + extra_tools, skills_for_tool_policy)
    final_tools, setup = assemble_deferred_tools(filtered, enabled=resolved_app_config.tool_search.enabled)
    return create_agent(
        model=create_chat_model(name=model_name, thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort, app_config=resolved_app_config, attach_tracing=False),
        tools=final_tools,
        middleware=build_middlewares(
            config,
            model_name=model_name,
            agent_name=agent_name,
            available_skills=available_skills,
            app_config=resolved_app_config,
            deferred_setup=setup,
        ),
        system_prompt=apply_prompt_template(
            subagent_enabled=subagent_enabled,
            max_concurrent_subagents=max_concurrent_subagents,
            agent_name=agent_name,
            available_skills=available_skills,
            app_config=resolved_app_config,
            deferred_names=setup.deferred_names,
        ),
        state_schema=ThreadState,
    )
