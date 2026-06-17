"""agent 自管理技能演进配置。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillEvolutionConfig(BaseModel):
    """agent 自管理技能演进配置。"""

    enabled: bool = Field(
        default=False,
        description="agent 是否可在 skills/custom 下创建和修改技能",
    )
    moderation_model_name: str | None = Field(
        default=None,
        description="技能安全审核用的可选模型名。默认用主对话模型。",
    )
