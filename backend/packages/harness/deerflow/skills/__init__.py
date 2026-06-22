"""技能模块（M14 skills）。

SKILL.md 协议：一个技能 = 一个目录 + 一份 SKILL.md（YAML frontmatter + 操作指南正文）。

三流：
- **发现**：``SkillStorage.load_skills`` 扫 ``public/`` + ``custom/`` 找 SKILL.md，合并 enabled 状态。
- **激活**：用户输 ``/skill-name task`` → ``SkillActivationMiddleware`` 注入该 SKILL.md 内容。
- **安装**：``.skill`` ZIP 经安全防护（穿越/symlink/zip 炸弹）+ LLM 审查后原子搬入 ``custom/``。

对齐 deer ``skills/``。
"""

from deerflow.skills.parser import parse_allowed_tools, parse_skill_file
from deerflow.skills.slash import (
    RESERVED_SLASH_SKILL_NAMES,
    ResolvedSlashSkill,
    SlashSkillReference,
    parse_slash_skill_reference,
    resolve_slash_skill,
)
from deerflow.skills.tool_policy import allowed_tool_names_for_skills, filter_tools_by_skill_allowed_tools
from deerflow.skills.types import SKILL_MD_FILE, Skill, SkillCategory
from deerflow.skills.validation import ALLOWED_FRONTMATTER_PROPERTIES, _validate_skill_frontmatter

__all__ = [
    # types
    "SKILL_MD_FILE",
    "Skill",
    "SkillCategory",
    # parser
    "parse_skill_file",
    "parse_allowed_tools",
    # validation
    "ALLOWED_FRONTMATTER_PROPERTIES",
    "_validate_skill_frontmatter",
    # slash
    "RESERVED_SLASH_SKILL_NAMES",
    "SlashSkillReference",
    "ResolvedSlashSkill",
    "parse_slash_skill_reference",
    "resolve_slash_skill",
    # tool_policy
    "allowed_tool_names_for_skills",
    "filter_tools_by_skill_allowed_tools",
]
