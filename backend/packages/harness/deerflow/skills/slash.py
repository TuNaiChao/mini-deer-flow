"""``/skill-name task`` slash 激活解析（M14 skills）。

对齐 deer ``skills/slash.py``。严格语法 + 保留字过滤（红线：``RESERVED_SLASH_SKILL_NAMES``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from deerflow.skills.types import Skill

#: 保留的 slash 命令名——这些是控制命令，不是技能，激活时跳过。
RESERVED_SLASH_SKILL_NAMES = frozenset({"bootstrap", "help", "memory", "models", "new", "status"})
#: 严格 slash 技能语法：``/skill-name<空白或行尾>``，name 是 hyphen-case。
_SLASH_SKILL_RE = re.compile(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)")


@dataclass(frozen=True, slots=True)
class SlashSkillReference:
    """解析出的 slash-skill 命令：技能名 + 剩余任务文本。"""

    name: str
    remaining_text: str


@dataclass(frozen=True, slots=True)
class ResolvedSlashSkill:
    """对启用且白名单内的技能解析出的激活结果。"""

    skill: Skill
    remaining_text: str
    container_file_path: str


def parse_slash_skill_reference(text: str) -> SlashSkillReference | None:
    """解析严格 ``/skill-name task`` 语法，忽略保留控制命令。

    拒绝：前导空白、缺分隔、保留字（``/new`` ``/help`` 等）。
    """
    match = _SLASH_SKILL_RE.match(text)
    if not match:
        return None
    name = match.group(1)
    if name in RESERVED_SLASH_SKILL_NAMES:
        return None
    return SlashSkillReference(
        name=name,
        remaining_text=text[match.end() :].lstrip(),
    )


def resolve_slash_skill(
    text: str,
    skills: list[Skill],
    *,
    available_skills: set[str] | None = None,
    container_base_path: str = "/mnt/skills",
) -> ResolvedSlashSkill | None:
    """把文本解析成一个启用、白名单内的技能激活（若可能）。"""
    reference = parse_slash_skill_reference(text)
    if reference is None:
        return None
    if available_skills is not None and reference.name not in available_skills:
        return None

    skill = next((candidate for candidate in skills if candidate.name == reference.name and candidate.enabled), None)
    if skill is None:
        return None

    return ResolvedSlashSkill(
        skill=skill,
        remaining_text=reference.remaining_text,
        container_file_path=skill.get_container_file_path(container_base_path),
    )
