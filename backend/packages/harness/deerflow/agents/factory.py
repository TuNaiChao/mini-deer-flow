"""纯参数化的 Agent 工厂——SDK 级入口。

``create_deerflow_agent`` 接受纯 Python 参数——不读 YAML、不碰全局单例。它夹在底层
``langchain.agents.create_agent`` 原语和 config 驱动的应用工厂 [make_lead_agent](lead_agent/agent.py)
之间。

两种装配模式：

- **features 模式**（默认）：传 ``RuntimeFeatures`` 声明要开哪些行为，工厂按固定顺序
  装出一条中间件链（``_assemble_from_features``）。可用 ``extra_middleware`` + ``@Next``/``@Prev``
  把自定义中间件插到锚点旁边（``_insert_extra``）。
- **middleware 全接管**：直接传 ``middleware=[...]``，工厂原样用——不能与 features / extra_middleware 同用。

注意：工厂装配本身不读 config，但某些注入的运行时组件（如 ``task_tool``）在调用时
仍可能读全局 config（Phase 2 目标才是完全 config-free 运行时）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.middlewares.clarification_middleware import ClarificationMiddleware
from deerflow.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from deerflow.agents.middlewares.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from deerflow.agents.thread_state import ThreadState
from deerflow.tools.builtins import ask_clarification_tool

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TodoMiddleware prompts（SDK 最小版）
# ---------------------------------------------------------------------------

_TODO_SYSTEM_PROMPT = """
<todo_list_system>
You have access to the `write_todos` tool to help you manage and track complex multi-step objectives.

**CRITICAL RULES:**
- Mark todos as completed IMMEDIATELY after finishing each step - do NOT batch completions
- Keep EXACTLY ONE task as `in_progress` at any time (unless tasks can run in parallel)
- Update the todo list in REAL-TIME as you work - gives users visibility into your progress
- DO NOT use this tool for simple tasks (< 3 steps) - just complete them directly
</todo_list_system>
"""

_TODO_TOOL_DESCRIPTION = "Use this tool to create and manage a structured task list for complex work sessions.  Only use for complex tasks (3+ steps)."


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def create_deerflow_agent(
    model: "BaseChatModel",
    tools: "list[BaseTool] | None" = None,
    *,
    system_prompt: str | None = None,
    middleware: list[AgentMiddleware] | None = None,
    features: RuntimeFeatures | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
    plan_mode: bool = False,
    state_schema: type | None = None,
    checkpointer: "BaseCheckpointSaver | None" = None,
    name: str = "default",
) -> "CompiledStateGraph":
    """从纯 Python 参数创建一个 DeerFlow agent。

    工厂装配本身不读 config 文件。某些注入的运行时组件（如 ``task_tool``）在调用时
    仍可能依赖全局 config——完全 config-free 运行时见 Phase 2 路线图。

    Args:
        model: 聊天模型实例（必传）。
        tools: 用户提供的工具。feature 注入的工具会自动追加（按 name 去重，用户工具优先）。
        system_prompt: 系统提示词。``None`` 用最小默认。
        middleware: **全接管**——给了就直接用这条链。不能与 *features* / *extra_middleware* 同用。
        features: 声明式 feature flag。不能与 *middleware* 同用。
        extra_middleware: 经 ``@Next``/``@Prev`` 定位插入到自动装配链里的额外中间件。
            不能与 *middleware* 同用。
        plan_mode: 开 TodoMiddleware（任务跟踪）。
        state_schema: LangGraph 状态类型。默认 ``ThreadState``。
        checkpointer: 可选持久化后端。
        name: agent 名（传给在意的中间件，如 ``MemoryMiddleware``）。

    Raises:
        ValueError: 同时给了 *middleware* 和 *features*/*extra_middleware*。
    """
    if middleware is not None and features is not None:
        raise ValueError("Cannot specify both 'middleware' and 'features'.  Use one or the other.")
    if middleware is not None and extra_middleware:
        raise ValueError("Cannot use 'extra_middleware' with 'middleware' (full takeover).")
    if extra_middleware:
        for mw in extra_middleware:
            if not isinstance(mw, AgentMiddleware):
                raise TypeError(f"extra_middleware items must be AgentMiddleware instances, got {type(mw).__name__}")

    effective_tools: list[BaseTool] = list(tools or [])
    effective_state = state_schema or ThreadState

    if middleware is not None:
        effective_middleware = list(middleware)
    else:
        feat = features or RuntimeFeatures()
        effective_middleware, extra_tools = _assemble_from_features(
            feat,
            name=name,
            plan_mode=plan_mode,
            extra_middleware=extra_middleware or [],
        )
        # 按 tool name 去重——用户提供的工具优先
        existing_names = {t.name for t in effective_tools}
        for t in extra_tools:
            if t.name not in existing_names:
                effective_tools.append(t)
                existing_names.add(t.name)

    return create_agent(
        model=model,
        tools=effective_tools or None,
        middleware=effective_middleware,
        system_prompt=system_prompt,
        state_schema=effective_state,
        checkpointer=checkpointer,
        name=name,
    )


# ---------------------------------------------------------------------------
# 内部：feature 驱动的中间件装配
# ---------------------------------------------------------------------------


def _assemble_from_features(
    feat: RuntimeFeatures,
    *,
    name: str = "default",
    plan_mode: bool = False,
    extra_middleware: list[AgentMiddleware] | None = None,
) -> "tuple[list[AgentMiddleware], list[BaseTool]]":
    """从 *feat* 构建有序中间件链 + 额外工具。

    中间件顺序与 ``make_lead_agent`` 对齐（14 个中间件，SDK 精简链）：

      0-2. Sandbox 基础设施（ThreadData → Uploads → Sandbox）
      3.   DanglingToolCallMiddleware（恒定）
      4.   GuardrailMiddleware（guardrail feature）
      5.   ToolErrorHandlingMiddleware（恒定）
      6.   SummarizationMiddleware（summarization feature）
      7.   TodoMiddleware（plan_mode 参数）
      8.   TitleMiddleware（auto_title feature）
      9.   MemoryMiddleware（memory feature）
      10.  ViewImageMiddleware（vision feature）
      11.  SubagentLimitMiddleware（subagent feature）
      12.  LoopDetectionMiddleware（loop_detection feature）
      13.  TokenBudgetMiddleware（token_budget feature）
      14.  ClarificationMiddleware（恒定末位）

    两阶段排序：
      1. 内置链——固定顺序 append；
      2. 额外中间件——经 ``@Next``/``@Prev`` 插入。

    每个 feature 值的处理：
      - ``False``：跳过；
      - ``True``：用内置默认中间件（``summarization`` / ``guardrail`` 不可——这俩要自定义实例）；
      - ``AgentMiddleware`` 实例：直接用（自定义替换）。
    """
    chain: list[AgentMiddleware] = []
    extra_tools: list[BaseTool] = []

    # --- [0-2] Sandbox 基础设施 ---
    if feat.sandbox is not False:
        if isinstance(feat.sandbox, AgentMiddleware):
            chain.append(feat.sandbox)
        else:
            from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
            from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware
            from deerflow.sandbox.middleware import SandboxMiddleware

            chain.append(ThreadDataMiddleware(lazy_init=True))
            chain.append(UploadsMiddleware())
            chain.append(SandboxMiddleware(lazy_init=True))

    # --- [3] DanglingToolCall（恒定）---
    chain.append(DanglingToolCallMiddleware())

    # --- [4] Guardrail ---
    if feat.guardrail is not False:
        if isinstance(feat.guardrail, AgentMiddleware):
            chain.append(feat.guardrail)
        else:
            raise ValueError("guardrail=True requires a custom AgentMiddleware instance (no built-in GuardrailMiddleware yet)")

    # --- [5] ToolErrorHandling（恒定）---
    chain.append(ToolErrorHandlingMiddleware())

    # --- [6] Summarization ---
    if feat.summarization is not False:
        if isinstance(feat.summarization, AgentMiddleware):
            chain.append(feat.summarization)
        else:
            raise ValueError("summarization=True requires a custom AgentMiddleware instance (SummarizationMiddleware needs a model argument)")

    # --- [7] TodoMiddleware（plan_mode）---
    if plan_mode:
        from deerflow.agents.middlewares.todo_middleware import TodoMiddleware

        chain.append(TodoMiddleware(system_prompt=_TODO_SYSTEM_PROMPT, tool_description=_TODO_TOOL_DESCRIPTION))

    # --- [8] Auto Title ---
    if feat.auto_title is not False:
        if isinstance(feat.auto_title, AgentMiddleware):
            chain.append(feat.auto_title)
        else:
            from deerflow.agents.middlewares.title_middleware import TitleMiddleware

            chain.append(TitleMiddleware())

    # --- [9] Memory ---
    if feat.memory is not False:
        if isinstance(feat.memory, AgentMiddleware):
            chain.append(feat.memory)
        else:
            from deerflow.agents.middlewares.memory_middleware import MemoryMiddleware

            chain.append(MemoryMiddleware(agent_name=name))

    # --- [10] Vision ---
    if feat.vision is not False:
        if isinstance(feat.vision, AgentMiddleware):
            chain.append(feat.vision)
        else:
            from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

            chain.append(ViewImageMiddleware())

        if feat.sandbox is not False:
            from deerflow.tools.builtins import view_image_tool

            extra_tools.append(view_image_tool)

    # --- [11] Subagent ---
    if feat.subagent is not False:
        if isinstance(feat.subagent, AgentMiddleware):
            chain.append(feat.subagent)
        else:
            from deerflow.agents.middlewares.subagent_limit_middleware import SubagentLimitMiddleware

            chain.append(SubagentLimitMiddleware())
        from deerflow.tools.builtins import task_tool

        extra_tools.append(task_tool)

    # --- [12] LoopDetection ---
    if feat.loop_detection is not False:
        if isinstance(feat.loop_detection, AgentMiddleware):
            chain.append(feat.loop_detection)
        else:
            from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware
            from deerflow.config.loop_detection_config import LoopDetectionConfig

            chain.append(LoopDetectionMiddleware.from_config(LoopDetectionConfig()))

    # --- [13] TokenBudget ---
    if feat.token_budget is not False:
        if isinstance(feat.token_budget, AgentMiddleware):
            chain.append(feat.token_budget)
        else:
            from deerflow.agents.middlewares.token_budget_middleware import TokenBudgetMiddleware
            from deerflow.config.token_budget_config import TokenBudgetConfig

            chain.append(TokenBudgetMiddleware.from_config(TokenBudgetConfig()))

    # --- [14] Clarification（内置链恒定末位）---
    chain.append(ClarificationMiddleware())
    extra_tools.append(ask_clarification_tool)

    # --- 经 @Next/@Prev 插入 extra_middleware ---
    if extra_middleware:
        _insert_extra(chain, extra_middleware)
        # 不变量：ClarificationMiddleware 必须永远末位。
        # @Next(ClarificationMiddleware) 可能把它顶离尾部。
        clar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
        if clar_idx != len(chain) - 1:
            chain.append(chain.pop(clar_idx))

    return chain, extra_tools


# ---------------------------------------------------------------------------
# 内部：经 @Next/@Prev 插入额外中间件
# ---------------------------------------------------------------------------


def _insert_extra(chain: list[AgentMiddleware], extras: list[AgentMiddleware]) -> None:
    """用 ``@Next``/``@Prev`` 锚点把额外中间件插入 *chain*。

    算法：
      1. 校验：没有中间件同时有 @Next 和 @Prev；
      2. 冲突检测：两个 extra 瞄准同一个锚点（同向或反向）→ 报错；
      3. 无锚点的 extra 插在 ClarificationMiddleware 之前；
      4. 有锚点的 extra 迭代插入（支持 extra 之间互相锚定）；
      5. 所有轮次后仍解析不了的锚点 → 报错。
    """
    next_targets: dict[type, type] = {}
    prev_targets: dict[type, type] = {}

    anchored: list[tuple[AgentMiddleware, str, type]] = []
    unanchored: list[AgentMiddleware] = []

    for mw in extras:
        next_anchor = getattr(type(mw), "_next_anchor", None)
        prev_anchor = getattr(type(mw), "_prev_anchor", None)

        if next_anchor and prev_anchor:
            raise ValueError(f"{type(mw).__name__} cannot have both @Next and @Prev")

        if next_anchor:
            if next_anchor in next_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} and {next_targets[next_anchor].__name__} both @Next({next_anchor.__name__})")
            if next_anchor in prev_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} @Next({next_anchor.__name__}) and {prev_targets[next_anchor].__name__} @Prev({next_anchor.__name__}) — use cross-anchoring between extras instead")
            next_targets[next_anchor] = type(mw)
            anchored.append((mw, "next", next_anchor))
        elif prev_anchor:
            if prev_anchor in prev_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} and {prev_targets[prev_anchor].__name__} both @Prev({prev_anchor.__name__})")
            if prev_anchor in next_targets:
                raise ValueError(f"Conflict: {type(mw).__name__} @Prev({prev_anchor.__name__}) and {next_targets[prev_anchor].__name__} @Next({prev_anchor.__name__}) — use cross-anchoring between extras instead")
            prev_targets[prev_anchor] = type(mw)
            anchored.append((mw, "prev", prev_anchor))
        else:
            unanchored.append(mw)

    # 无锚点 → 插在 ClarificationMiddleware 之前
    clarification_idx = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
    for mw in unanchored:
        chain.insert(clarification_idx, mw)
        clarification_idx += 1

    # 有锚点 → 迭代插入（支持 extra 锚定另一个 extra）
    pending = list(anchored)
    max_rounds = len(pending) + 1
    for _ in range(max_rounds):
        if not pending:
            break
        remaining = []
        for mw, direction, anchor in pending:
            idx = next(
                (i for i, m in enumerate(chain) if isinstance(m, anchor)),
                None,
            )
            if idx is None:
                remaining.append((mw, direction, anchor))
                continue
            if direction == "next":
                chain.insert(idx + 1, mw)
            else:
                chain.insert(idx, mw)
        if len(remaining) == len(pending):
            names = [type(m).__name__ for m, _, _ in remaining]
            anchor_types = {a for _, _, a in remaining}
            remaining_types = {type(m) for m, _, _ in remaining}
            circular = anchor_types & remaining_types
            if circular:
                raise ValueError(f"Circular dependency among extra middlewares: {', '.join(t.__name__ for t in circular)}")
            raise ValueError(f"Cannot resolve positions for {', '.join(names)} — anchors {', '.join(a.__name__ for _, _, a in remaining)} not found in chain")
        pending = remaining
