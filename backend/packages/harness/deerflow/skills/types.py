"""技能类型定义（M14 skills）。

对齐 deer ``skills/types.py``。一个 ``Skill`` 就是「一个目录 + 一份 SKILL.md」的内存表示。
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: 技能主文件名（YAML frontmatter + 操作指南正文）。
SKILL_MD_FILE = "SKILL.md"


class SkillCategory(StrEnum):
    """技能来源类别。

    - ``PUBLIC``：平台内置技能，只读。
    - ``CUSTOM``：用户自建技能，可编辑 / 删除。
    """

    PUBLIC = "public"
    CUSTOM = "custom"


@dataclass
class Skill:
    """一个技能：元数据 + 文件路径。

    ``allowed_tools`` 为 ``None`` = 不限制（legacy 全部工具）；``[]`` = 不允许任何工具；
    给列表 = 只允许这些工具（白名单，收紧该技能激活时模型的工具集）。
    """

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path  # 从类别根到本技能目录的相对路径
    category: SkillCategory  # 'public' 或 'custom'
    allowed_tools: list[str] | None = None
    enabled: bool = False  # 实际状态来自 extensions_config

    @property
    def skill_path(self) -> str:
        """从类别根（skills/{category}）到本技能目录的相对路径（posix）。"""
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        """本技能目录在沙箱容器内的完整路径。"""
        category_base = f"{container_base_path}/{self.category}"
        skill_path = self.skill_path
        if skill_path:
            return f"{category_base}/{skill_path}"
        return category_base

    def get_container_file_path(self, container_base_path: str = "/mnt/skills") -> str:
        """本技能 SKILL.md 在容器内的完整路径。"""
        return f"{self.get_container_path(container_base_path)}/{SKILL_MD_FILE}"

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, description={self.description!r}, category={self.category!r})"
