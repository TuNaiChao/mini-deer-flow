"""中间件模块——Agent 的行为骨架（M16，对齐上游 26 步生产中间件链）。

``build_middlewares`` 按上游 deer-flow 的 26 步顺序装配中间件链（mini 跳过 #8 Guardrail，其余
25 步齐全）。顺序是**契约**不是建议——部分中间件有硬性先后约束（红线）：

  - **#26 ClarificationMiddleware 永远最后**（红线 #14）：它能中断整次执行（``Command(goto=END)``），
    须在所有其它中间件处理后生效。
  - **#1 InputSanitization 最外层**：排第一 → 是最外层 ``wrap_model_call`` 包装，所有内层中间件
    （含 LLMErrorHandling 的重试）看到的都是已净化消息（提示词注入标签被转义）。
  - **#4 ThreadData 先于 #5 Sandbox**：SandboxMiddleware / ToolOutputBudget 依赖 thread_data 里
    已写好的物理路径。
  - **#3 Uploads 先于 #5 Sandbox**：上传目录要在沙箱挂载前算好。
  - **#19 DeferredToolFilter 在 #21 SubagentLimit 前**：延迟过滤要在子代理截断前定 schema。
  - **#20 SystemMessageCoalescing 在请求拍平前合并所有 SystemMessage**（#3711）。
  - **#23 TokenBudget 在 #24 custom 前 / #22 LoopDetection 后**：预算硬停剥 tool_calls 后，
    custom / Safety 才看到清理过的消息。
  - **所有 ``wrap_tool_call`` / ``wrap_model_call`` 必须 ``raise GraphBubbleUp``**（红线 #15）：
    handler 抛 LangGraph 控制流信号（interrupt/pause/resume/Command goto）必须原样上抛，
    否则 Clarification 的中断、subagent interrupt 全失效。

链分两段：
  - **共享段**（``build_lead_runtime_middlewares``，步骤 1-10）：lead 与 subagent 都要——
    InputSanitization(#1) / ToolOutputBudget(#2) / Uploads(#3, 仅 lead) / ThreadData(#4) / Sandbox(#5)
    / DanglingToolCall(#6) / LLMErrorHandling(#7) / [#8 Guardrail 跳过] / SandboxAudit(#9)
    / ToolErrorHandling(#10)。
  - **lead-only 段**（``build_middlewares`` 本函数，步骤 11-26）：DynamicContext / SkillActivation
    / Summarization / Todo[plan_mode] / TokenUsage / Title / Memory / ViewImage[vision]
    / DeferredToolFilter / SystemMessageCoalescing / SubagentLimit / LoopDetection / TokenBudget
    / custom / SafetyFinishReason / Clarification。

2026-06-27 六维重审：补齐此前缺失的 InputSanitization(#1) / SystemMessageCoalescing(#20, #3711)
/ TokenBudget(#23) 三个中间件；逐文件 diff 确认 #3746（DanglingToolCall ID-swap）与 #3709
（LoopDetection 位置回退）mini 此前已含（差异仅文档措辞，无逻辑差）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅类型注解用，运行时不 import（避免循环导入 + 保持轻量）。
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.runnables import RunnableConfig

    from deerflow.config.app_config import AppConfig
    from deerflow.tools.builtins.tool_search import DeferredToolSetup


# TodoMiddleware 提示词（plan_mode 时挂载，对齐 deer 的 _create_todo_list_middleware）。
# 教模型「复杂任务才用 write_todos、实时更新状态、一次只一个 in_progress、未做完不标 completed」。
_TODO_SYSTEM_PROMPT = """
<todo_list_system>
You have access to the `write_todos` tool to help you manage and track complex multi-step objectives.

**CRITICAL RULES:**
- Mark todos as completed IMMEDIATELY after finishing each step - do NOT batch completions
- Keep EXACTLY ONE task as `in_progress` at any time (unless tasks can run in parallel)
- Update the todo list in REAL-TIME as you work - this gives users visibility into your progress
- DO NOT use this tool for simple tasks (< 3 steps) - just complete them directly

**When to Use:**
This tool is designed for complex objectives that require systematic tracking:
- Complex multi-step tasks requiring 3+ distinct steps
- Non-trivial tasks needing careful planning and execution
- User explicitly requests a todo list
- User provides multiple tasks (numbered or comma-separated list)
- The plan may need revisions based on intermediate results

**When NOT to Use:**
- Single, straightforward tasks
- Trivial tasks (< 3 steps)
- Purely conversational or informational requests
- Simple tool calls where the approach is obvious

**Best Practices:**
- Break down complex tasks into smaller, actionable steps
- Use clear, descriptive task names
- Remove tasks that become irrelevant
- Add new tasks discovered during implementation
- Don't be afraid to revise the todo list as you learn more

**Task Management:**
Writing todos takes time and tokens - use it when helpful for managing complex problems, not for simple requests.
</todo_list_system>
"""

_TODO_TOOL_DESCRIPTION = """Use this tool to create and manage a structured task list for complex work sessions.

**IMPORTANT: Only use this tool for complex tasks (3+ steps). For simple requests, just do the work directly.**

## When to Use

Use this tool in these scenarios:
1. **Complex multi-step tasks**: When a task requires 3 or more distinct steps or actions
2. **Non-trivial tasks**: Tasks requiring careful planning or multiple operations
3. **User explicitly requests todo list**: When the user directly asks you to track tasks
4. **Multiple tasks**: When users provide a list of things to do
5. **Dynamic planning**: When the plan may need updates based on intermediate results

## When NOT to Use

Skip this tool when:
1. The task is straightforward and takes less than 3 steps
2. The task is trivial and tracking provides no benefit
3. The task is purely conversational or informational
4. It's clear what needs to be done and you can just do it

## How to Use

1. **Starting a task**: Mark it as `in_progress` BEFORE beginning work
2. **Completing a task**: Mark it as `completed` IMMEDIATELY after finishing
3. **Updating the list**: Add new tasks, remove irrelevant ones, or update descriptions as needed
4. **Multiple updates**: You can make several updates at once (e.g., complete one task and start the next)

## Task States

- `pending`: Task not yet started
- `in_progress`: Currently working on (can have multiple if tasks run in parallel)
- `completed`: Task finished successfully

## Task Completion Requirements

**CRITICAL: Only mark a task as completed when you have FULLY accomplished it.**

Never mark a task as completed if:
- There are unresolved issues or errors
- Work is partial or incomplete
- You encountered blockers preventing completion
- You couldn't find necessary resources or dependencies
- Quality standards haven't been met

If blocked, keep the task as `in_progress` and create a new task describing what needs to be resolved.

## Best Practices

- Create specific, actionable items
- Break complex tasks into smaller, manageable steps
- Use clear, descriptive task names
- Update task status in real-time as you work
- Mark tasks complete IMMEDIATELY after finishing (don't batch)
- Remove tasks that are no longer relevant
- **IMPORTANT**: When you write the todo list, mark your first task(s) as `in_progress` immediately
- **IMPORTANT**: Unless all tasks are completed, always have at least one task `in_progress` to show progress

Being proactive with task management demonstrates thoroughness and ensures all requirements are completed successfully.

**Remember**: If you only need a few tool calls to complete a task and it's clear what to do, it's better to just do the task directly and NOT use this tool at all.
"""


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

    # 共享前置段（上游步骤 1-10，mini 跳过 #8 Guardrail）：由 build_lead_runtime_middlewares 装配，
    # 含 InputSanitization(#1) / ToolOutputBudget(#2) / ThreadData(#4) / Uploads(#3, 仅 lead)
    # / Sandbox(#5) / DanglingToolCall(#6) / LLMErrorHandling(#7) / SandboxAudit(#9) / ToolErrorHandling(#10)。
    middlewares = build_lead_runtime_middlewares(app_config=resolved_app_config, lazy_init=True)

    # --- 步骤 11：DynamicContextMiddleware ---
    # before_agent 里把日期 + 记忆经 ID-swap 注入首条 HumanMessage（保持系统提示静态 → prefix-cache 复用）。
    middlewares.append(DynamicContextMiddleware(agent_name=agent_name, app_config=resolved_app_config))

    # --- 步骤 12：SkillActivationMiddleware ---
    # 用户输 /skill-name 时把对应 SKILL.md 注入当次模型调用。
    middlewares.append(SkillActivationMiddleware(available_skills=available_skills, app_config=resolved_app_config))

    # --- 步骤 13：SummarizationMiddleware（可选，config 驱动）---
    summarization_middleware = _create_summarization_middleware(app_config=resolved_app_config)
    if summarization_middleware is not None:
        middlewares.append(summarization_middleware)

    # --- 步骤 14：TodoMiddleware（plan_mode）---
    cfg = _get_runtime_config(config)
    is_plan_mode = cfg.get("is_plan_mode", False)
    if is_plan_mode:
        from deerflow.agents.middlewares.todo_middleware import TodoMiddleware

        middlewares.append(TodoMiddleware(system_prompt=_TODO_SYSTEM_PROMPT, tool_description=_TODO_TOOL_DESCRIPTION))

    # --- 步骤 15：TokenUsageMiddleware（token_usage.enabled）---
    if resolved_app_config.token_usage.enabled:
        from deerflow.agents.middlewares.token_usage_middleware import TokenUsageMiddleware

        middlewares.append(TokenUsageMiddleware())

    # --- 步骤 16：TitleMiddleware（config 驱动；内部读 title.enabled）---
    middlewares.append(TitleMiddleware(app_config=resolved_app_config))

    # --- 步骤 17：MemoryMiddleware（在 Title 之后；内部读 memory.enabled）---
    middlewares.append(MemoryMiddleware(agent_name=agent_name, memory_config=resolved_app_config.memory))

    # --- 步骤 18：ViewImageMiddleware（仅模型 supports_vision）---
    model_config = resolved_app_config.get_model_config(model_name) if model_name else None
    if model_config is not None and model_config.supports_vision:
        from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

        middlewares.append(ViewImageMiddleware())

    # --- 步骤 19：DeferredToolFilterMiddleware（tool_search 启用 + 有延迟工具）---
    # 延迟名集合 + catalog_hash 来自 build 期 setup（工具策略过滤后装配）；提升状态从图状态读。
    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import DeferredToolFilterMiddleware

        middlewares.append(DeferredToolFilterMiddleware(deferred_setup.deferred_names, deferred_setup.catalog_hash))

    # --- 步骤 20：SystemMessageCoalescingMiddleware（#3711，始终挂）---
    # 严格后端（vLLM/SGLang/Qwen/Anthropic）拒收非领头的 SystemMessage；lead agent 会动态注入
    # 多条 SystemMessage（DynamicContext 的日期提醒、跨午夜更新）。在出站请求里把它们合并成一条
    # 领头的 SystemMessage。只改请求、不动 checkpoint。无开关（provider 兼容修复，始终生效）。
    from deerflow.agents.middlewares.system_message_coalescing_middleware import SystemMessageCoalescingMiddleware

    middlewares.append(SystemMessageCoalescingMiddleware())

    # --- 步骤 21：SubagentLimitMiddleware（subagent_enabled）---
    subagent_enabled = cfg.get("subagent_enabled", False)
    if subagent_enabled:
        from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

        max_concurrent_subagents = cfg.get("max_concurrent_subagents", 3)
        middlewares.append(SubagentLimitMiddleware(max_concurrent=max_concurrent_subagents))

    # --- 步骤 22：LoopDetectionMiddleware（loop_detection.enabled，from_config）---
    loop_detection_config = resolved_app_config.loop_detection
    if loop_detection_config.enabled:
        middlewares.append(LoopDetectionMiddleware.from_config(loop_detection_config))

    # --- 步骤 23：TokenBudgetMiddleware（token_budget.enabled，from_config）---
    # 单 run token 用量上限：到软阈值提醒、到硬上限剥 tool_calls 强制收尾。after_model 记账 +
    # wrap_model_call 注入提醒（同 LoopDetection 的延迟注入套路，保 AIMessage→ToolMessage 配对）。
    token_budget_config = resolved_app_config.token_budget
    if token_budget_config.enabled:
        from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware

        middlewares.append(TokenBudgetMiddleware.from_config(token_budget_config))

    # --- 步骤 24：custom_middlewares（插在 Clarification 之前）---
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    # --- 步骤 25：SafetyFinishReasonMiddleware（safety_finish_reason.enabled，from_config）---
    # 注册在 custom 之后：LangChain 的 after_model 按倒列表序分发，最后注册的最先观察模型输出。
    # Safety 先看原始响应、命中则清 tool_calls，Loop / Subagent 再对清理后的消息计数不误触警。
    safety_config = resolved_app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import SafetyFinishReasonMiddleware

        middlewares.append(SafetyFinishReasonMiddleware.from_config(safety_config))

    # --- 步骤 26：ClarificationMiddleware（永远最后，红线 #14）---
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
