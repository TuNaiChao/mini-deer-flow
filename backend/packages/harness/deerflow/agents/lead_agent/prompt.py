"""
提示词模板系统

支持占位符替换的模板引擎，将配置和运行时信息注入系统提示词。
"""

# 基础系统提示词模板
SYSTEM_PROMPT_TEMPLATE = """你是一个有用的 AI 助手，名叫 DeerFlow。

你的职责是：
1. 理解用户的问题和需求
2. 使用可用的工具来帮助用户
3. 提供准确、有帮助的回答

{skills_section}

请遵循以下原则：
- 用中文回答用户的问题（除非用户使用其他语言）
- 保持简洁明了，但确保回答完整
- 如果需要更多信息，请主动询问
- 使用工具时，请确保参数正确
"""

# 技能提示词段落模板
SKILLS_SECTION_TEMPLATE = """
## 可用技能

你可以使用以下技能来帮助完成任务：

{skill_list}

使用技能时，请输入 /技能名称 后跟你需要完成的任务。
"""


def apply_prompt_template(
    *,
    available_skills: set[str] | None = None,
    **kwargs,
) -> str:
    """
    生成系统提示词

    Args:
        available_skills: 当前可用的技能名称集合
        **kwargs: 其他模板变量

    Returns:
        填充后的系统提示词字符串
    """
    # 构建技能段落
    if available_skills:
        skill_lines = []
        for skill_name in sorted(available_skills):
            skill_lines.append(f"- **{skill_name}**: /{skill_name} <任务描述>")
        skills_section = SKILLS_SECTION_TEMPLATE.format(
            skill_list="\n".join(skill_lines)
        )
    else:
        skills_section = ""

    return SYSTEM_PROMPT_TEMPLATE.format(
        skills_section=skills_section,
        **kwargs,
    )


def get_default_system_prompt() -> str:
    """获取默认系统提示词（不含技能）"""
    return apply_prompt_template()