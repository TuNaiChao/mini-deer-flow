"""技能系统配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from deerflow.config.paths import project_root, resolve_path


class SkillsConfig(BaseModel):
    """技能系统配置。"""

    use: str = Field(
        default="deerflow.skills.storage.local_skill_storage:LocalSkillStorage",
        description="SkillStorage 实现的类路径。",
    )
    path: str | None = Field(
        default=None,
        description="技能目录路径。未指定时默认用调用方项目根下的 `skills`。",
    )
    container_path: str = Field(
        default="/mnt/skills",
        description="技能在沙箱容器内挂载的路径",
    )

    def get_skills_path(self) -> Path:
        """解析后的技能目录路径。

        解析顺序：
            1. 显式 ``path`` 字段
            2. ``DEER_FLOW_SKILLS_PATH`` 环境变量
            3. 调用方项目根（``project_root()``）下的 ``skills``
            若 (3) 不存在，仍返回项目根默认，让调用方能稳定给出「无技能」位置而不抛错。
        """
        if self.path:
            return resolve_path(self.path)
        import os

        if env_path := os.getenv("DEER_FLOW_SKILLS_PATH"):
            return resolve_path(env_path)
        return project_root() / "skills"

    def get_skill_container_path(self, skill_name: str, category: str = "public") -> str:
        """某个技能在容器内的完整路径。

        Args:
            skill_name: 技能名（目录名）
            category: 技能类别（public 或 custom）
        """
        return f"{self.container_path}/{category}/{skill_name}"
