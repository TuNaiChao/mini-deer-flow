"""SKILL.md 解析器（M14 skills）。

对齐 deer ``skills/parser.py``。解析 YAML frontmatter（``---`` 围栏）抽出技能元数据
（name/description/license/allowed-tools），正文留给激活时读。
"""

import logging
import re
from pathlib import Path

import yaml

from .types import SKILL_MD_FILE, Skill, SkillCategory

logger = logging.getLogger(__name__)


def _format_yaml_error(skill_file: Path, exc: yaml.YAMLError, source: str) -> str:
    """渲染开发者友好的 YAML frontmatter 错误说明。"""
    lines = [f"Invalid YAML front-matter in {skill_file}: {exc}"]

    mark = getattr(exc, "problem_mark", None)
    source_lines = source.splitlines()
    if mark is not None and 0 <= mark.line < len(source_lines):
        offending = source_lines[mark.line]
        # mark.line 在 frontmatter 正文体里 0 基；+1 转 1 基，再 +1 算 leading ``---`` 围栏。
        file_line_number = mark.line + 2
        lines.append(f"  line {file_line_number}: {offending}")

        # 最常见作者错误的针对性提示：未加引号的标量值含 ``: ``。
        if getattr(exc, "problem", "") == "mapping values are not allowed here" and ":" in offending:
            key, _, value = offending.partition(":")
            value = value.strip()
            if value and value[0] not in {'"', "'", "|", ">", "[", "{"}:
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'  hint: values containing ":" must be quoted, e.g. {key}: "{escaped}"')

    return "\n".join(lines)


def parse_allowed_tools(raw: object, skill_file: Path) -> list[str] | None:
    """解析可选的 allowed-tools frontmatter 字段。

    ``None`` = 字段缺省（不限制）。``list`` = 字符串序列（含空列表=显式无工具）。
    非法值抛 ``ValueError``。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"allowed-tools in {skill_file} must be a list of strings")

    allowed_tools: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"allowed-tools in {skill_file} must contain only strings")
        tool_name = item.strip()
        if not tool_name:
            raise ValueError(f"allowed-tools in {skill_file} cannot contain empty tool names")
        allowed_tools.append(tool_name)
    return allowed_tools


def parse_skill_file(skill_file: Path, category: SkillCategory, relative_path: Path | None = None) -> Skill | None:
    """解析 SKILL.md 抽元数据。解析失败 / 缺必填字段返回 ``None``（记 error，不抛）。"""
    if not skill_file.exists() or skill_file.name != SKILL_MD_FILE:
        return None

    try:
        content = skill_file.read_text(encoding="utf-8")

        # 抽 leading ``---`` 围栏之间的 YAML frontmatter 块。
        front_matter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not front_matter_match:
            return None

        front_matter_text = front_matter_match.group(1)

        try:
            metadata = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as exc:
            logger.error("%s", _format_yaml_error(skill_file, exc, front_matter_text))
            return None

        if not isinstance(metadata, dict):
            logger.error("Front-matter in %s is not a YAML mapping", skill_file)
            return None

        # 必填字段：name + description，都须非空字符串。
        name = metadata.get("name")
        description = metadata.get("description")

        if not name or not isinstance(name, str):
            return None
        if not description or not isinstance(description, str):
            return None

        # 归一化：剥 YAML 可能保留的周边空白。
        name = name.strip()
        description = description.strip()

        if not name or not description:
            return None

        license_text = metadata.get("license")
        if license_text is not None:
            license_text = str(license_text).strip() or None

        try:
            allowed_tools = parse_allowed_tools(metadata.get("allowed-tools"), skill_file)
        except ValueError as exc:
            logger.error("Invalid allowed-tools in %s: %s", skill_file, exc)
            return None

        return Skill(
            name=name,
            description=description,
            license=license_text,
            skill_dir=skill_file.parent,
            skill_file=skill_file,
            relative_path=relative_path or Path(skill_file.parent.name),
            category=category,
            allowed_tools=allowed_tools,
            enabled=True,  # 实际状态来自 extensions config
        )

    except Exception:
        logger.exception("Unexpected error parsing skill file %s", skill_file)
        return None
