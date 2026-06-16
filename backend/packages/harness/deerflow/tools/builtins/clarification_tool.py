"""
澄清工具

当 Agent 需要向用户询问更多信息时调用。
被 ClarificationMiddleware 拦截处理（阶段3）。

注意：这个工具本身不需要 runtime 参数——它只负责"声明一次澄清请求"，
真正的中断逻辑由 ClarificationMiddleware 在阶段3实现。
"""
from typing import Literal

from langchain.tools import tool


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed.

    Use this tool when you cannot proceed without user input:
    - Missing information (file paths, URLs, specific requirements)
    - Ambiguous requirements with multiple valid interpretations
    - Approach choices needing user preference
    - Risky operations needing explicit confirmation
    - Suggestions needing user approval

    Args:
        question: The clarification question to ask the user. Be specific and clear.
        clarification_type: Type of clarification (missing_info, ambiguous_requirement,
            approach_choice, risk_confirmation, suggestion).
        context: Optional context explaining why clarification is needed.
        options: Optional list of choices (for approach_choice or suggestion).
    """
    # 占位实现。真正的逻辑由 ClarificationMiddleware 拦截此工具调用并中断执行。
    return "Clarification request processed by middleware"