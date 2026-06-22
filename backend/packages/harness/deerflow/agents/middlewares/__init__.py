"""中间件模块——Agent 的行为骨架（M16，23 步生产中间件链）。

``build_middlewares`` 按 ALIGNMENT_OUTLINE Part D 的 23 步顺序装配中间件链。顺序是**契约**
不是建议——部分中间件有硬性先后约束（红线）：

  - **#14 ClarificationMiddleware 永远最后**：它能中断整次执行，须在所有其它中间件处理后生效。
  - **#2 ThreadData 先于 #4 Sandbox**：SandboxMiddleware / ToolOutputBudget 依赖 thread_data 里
    已写好的路径。
  - **#3 Uploads 先于 #4 Sandbox**：上传目录要在沙箱挂载前算好。
  - **#18 DeferredToolFilter 在 #19 SubagentLimit 前**：延迟过滤要在子代理截断前定 schema。
  - **所有 ``wrap_tool_call`` / ``wrap_model_call`` 必须 ``raise GraphBubbleUp``**（红线 #15）：
    handler 抛 LangGraph 控制流信号（interrupt/pause/resume/Command goto）必须原样上抛，
    否则 Clarification 的中断、subagent interrupt 全失效。

链分两段：
  - **共享段**（``build_lead_runtime_middlewares``，步骤 1-9）：lead 与 subagent 都要——
    ToolOutputBudget / ThreadData / Uploads[仅 lead] / Sandbox / DanglingToolCall / LLMErrorHandling
    / [Guardrail 跳过] / SandboxAudit / ToolErrorHandling。
  - **lead-only 段**（``build_middlewares`` 本函数，步骤 10-23）：DynamicContext / SkillActivation
    / Summarization / Todo[plan_mode] / TokenUsage / Title / Memory / ViewImage[vision]
    / DeferredToolFilter / SubagentLimit / LoopDetection / custom / SafetyFinishReason / Clarification。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅类型注解用，运行时不 import（避免循环导入 + 保持轻量）。
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.runnables import RunnableConfig

    from deerflow.config.app_config import AppConfig
    from deerflow.tools.builtins.tool_search import DeferredToolSetup


def build_middlewares(
    config: RunnableConfig | None = None,
    model_name: str | None = None,
    agent_name: str | None = None,
    custom_middlewares: list[AgentMiddleware] | None = None,
    *,
    app_config: AppConfig | None = None,
    available_skills: set[str] | None = None,
    deferred_setup: DeferredToolSetup | None = None,
) -> list[AgentMiddleware]:
    """按 Part D 的 23 步严格顺序装配中间件链。

    Args:
        config: LangGraph 运行时配置（含 configurable：is_plan_mode / subagent_enabled /
            max_concurrent_subagents）。
        model_name: 解析出的模型名（按 supports_vision 决定是否挂 ViewImageMiddleware）。
        agent_name: 自定义 agent 名；MemoryMiddleware / DynamicContextMiddleware 用它做
            per-agent 记忆 / 上下文。
        custom_middlewares: 额外自定义中间件（插在 LoopDetection 与 SafetyFinishReason 之间，
            Clarification 之前）。
        app_config: 显式配置；None 用 ``get_app_config()``。
        available_skills: 当前 agent 可见的技能白名单（SkillActivationMiddleware 据此过滤）。
        deferred_setup: 延迟 MCP 工具装配（tool_search 启用才有）；非空则挂 DeferredToolFilter。

    Returns:
        按顺序排列的中间件列表（Clarification 永远末位）。
    """
    from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
    from deerflow.agents.middlewares.dynamic_context_middleware import DynamicContextMiddleware
    from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
    from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware
    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware
    from deerflow.agents.middlewares.summarization_middleware import _create_summarization_middleware
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware
    from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares
    from deerflow.config import get_app_config

    resolved_app_config = app_config or get_app_config()

    # 步骤 1-9：lead/subagent 共享前置中间件（含 Guardrail 跳过位）。
    middlewares = build_lead_runtime_middlewares(app_config=resolved_app_config, lazy_init=True)

    # --- 步骤 10：DynamicContextMiddleware ---
    # before_agent 里把日期 + 记忆经 ID-swap 注入首条 HumanMessage（保持系统提示静态 → prefix-cache 复用）。
    middlewares.append(DynamicContextMiddleware(agent_name=agent_name, app_config=resolved_app_config))

    # --- 步骤 11：SkillActivationMiddleware ---
    # 用户输 /skill-name 时把对应 SKILL.md 注入当次模型调用。
    middlewares.append(SkillActivationMiddleware(available_skills=available_skills, app_config=resolved_app_config))

    # --- 步骤 12：SummarizationMiddleware（可选，config 驱动）---
    summarization_middleware = _create_summarization_middleware(app_config=resolved_app_config)
    if summarization_middleware is not None:
        middlewares.append(summarization_middleware)

    # --- 步骤 13：TodoMiddleware（plan_mode）---
    cfg = _get_runtime_config(config)
    is_plan_mode = cfg.get("is_plan_mode", False)
    if is_plan_mode:
        from deerflow.agents.middlewares.todo_middleware import TodoMiddleware

        middlewares.append(TodoMiddleware())

    # --- 步骤 14：TokenUsageMiddleware（token_usage.enabled）---
    if resolved_app_config.token_usage.enabled:
        from deerflow.agents.middlewares.token_usage_middleware import TokenUsageMiddleware

        middlewares.append(TokenUsageMiddleware())

    # --- 步骤 15：TitleMiddleware（config 驱动；内部读 title.enabled）---
    middlewares.append(TitleMiddleware(app_config=resolved_app_config))

    # --- 步骤 16：MemoryMiddleware（在 Title 之后；内部读 memory.enabled）---
    middlewares.append(MemoryMiddleware(agent_name=agent_name, memory_config=resolved_app_config.memory))

    # --- 步骤 17：ViewImageMiddleware（仅模型 supports_vision）---
    model_config = resolved_app_config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

        middlewares.append(ViewImageMiddleware())

    # --- 步骤 18：DeferredToolFilterMiddleware（tool_search 启用 + 有延迟工具）---
    # 延迟名集合 + catalog_hash 来自 build 期 setup（工具策略过滤后装配）；提升状态从图状态读。
    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

        middlewares.append(DeferredToolFilterMiddleware(deferred_setup.deferred_names, deferred_setup.catalog_hash))

    # --- 步骤 19：SubagentLimitMiddleware（subagent_enabled）---
    subagent_enabled = cfg.get("subagent_enabled", False)
    if subagent_enabled:
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
        middlewares.append(SubagentLimitMiddleware(max_concurrent=max_concurrent_subagents))

    # --- 步骤 20：LoopDetectionMiddleware（loop_detection.enabled，from_config）---
    loop_detection_config = resolved_app_config.loop_detection
    if loop_detection_config.enabled:
        middlewares.append(LoopDetectionMiddleware.from_config(loop_detection_config))

    # --- 步骤 21：custom_middlewares（插在 Clarification 之前）---
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    # --- 步骤 22：SafetyFinishReasonMiddleware（safety_finish_reason.enabled，from_config）---
    # 注册在 custom 之后：LangChain 的 after_model 按倒列表序分发，最后注册的最先观察模型输出。
    # Safety 先看原始响应、命中则清 tool_calls，Loop / Subagent 再对清理后的消息计数不误触警。
    safety_config = resolved_app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware

        middlewares.append(SafetyFinishReasonMiddleware.from_config(safety_config))

    # --- 步骤 23：ClarificationMiddleware（永远最后，红线 #14）---
    middlewares.append(ClarificationMiddleware())

    return middlewares


def _get_runtime_config(config: RunnableConfig | None) -> dict:
    """从 RunnableConfig 抽 configurable dict（可能为空）。"""
    if not config:
        return {}
    configurable = config.get("configurable")
    if isinstance(configurable, dict):
        return configurable
    return {}
