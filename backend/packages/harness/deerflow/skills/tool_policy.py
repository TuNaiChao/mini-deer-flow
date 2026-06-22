"""技能 allowed-tools 工具策略（M14 skills）。

对齐 deer ``skills/tool_policy.py``。按已加载技能声明的 ``allowed-tools`` 收紧工具集。
"""

import logging
from typing import Protocol

from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)


class NamedTool(Protocol):
    """任何带 ``name`` 属性的工具。"""

    name: str


def allowed_tool_names_for_skills(skills: list[Skill]) -> set[str] | None:
    """返回技能显式 allowed-tools 声明的并集。

    ``None`` = legacy 全部放行（无技能声明 allowed-tools 时）。一旦有技能声明该字段，
    没声明的技能不贡献工具（而非禁用其他技能的显式限制）。
    """
    if not skills:
        return None

    allowed: set[str] = set()
    has_explicit_declaration = False
    for skill in skills:
        if skill.allowed_tools is None:
            continue
        has_explicit_declaration = True
        if not skill.allowed_tools:
            logger.info("Skill %s declared empty allowed-tools", skill.name)
        allowed.update(skill.allowed_tools)

    if not has_explicit_declaration:
        return None
    return allowed


def filter_tools_by_skill_allowed_tools[ToolT: NamedTool](tools: list[ToolT], skills: list[Skill]) -> list[ToolT]:
    """按技能 allowed-tools 白名单过滤工具列表；无声明时原样返回。"""
    allowed = allowed_tool_names_for_skills(skills)
    if allowed is None:
        return tools

    return [tool for tool in tools if tool.name in allowed]
