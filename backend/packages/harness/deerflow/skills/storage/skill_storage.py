"""``SkillStorage`` 抽象基类 + 模板方法流（M14 skills）。

对齐 deer ``skills/storage/skill_storage.py``。子类实现少量存储介质相关的原子操作；本基类
提供最终的模板方法流（``load_skills`` / 路径 helper / 校验）组合它们。

关键不变量：
- ``load_skills`` 每次重读 extensions_config 的 enabled 状态（他进程改动立即生效）；
- ``validate_relative_path`` / ``ensure_safe_support_path`` 防穿越（resolve + relative_to）；
- 名称校验 ``validate_skill_name``（hyphen-case，≤64 字符）。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from deerflow.skills.types import SKILL_MD_FILE, Skill, SkillCategory  # noqa: F401  # re-export

logger = logging.getLogger(__name__)

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillStorage(ABC):
    """技能存储后端抽象基类。

    子类实现少量存储介质特定的原子操作；本基类提供最终的模板方法流（load_skills、
    历史序列化、路径 helper、校验）。
    """

    def __init__(self, container_path: str = "/mnt/skills") -> None:
        self._container_root = container_path

    # ------------------------------------------------------------------
    # 静态协议 helper（非存储特定）
    # ------------------------------------------------------------------

    @staticmethod
    def validate_skill_name(name: str) -> str:
        """校验并归一化技能名；返回归一化形式。"""
        normalized = name.strip()
        if not _SKILL_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("Skill name must be hyphen-case using lowercase letters, digits, and hyphens only.")
        if len(normalized) > 64:
            raise ValueError("Skill name must be 64 characters or fewer.")
        return normalized

    @staticmethod
    def validate_relative_path(relative_path: str, base_dir: Path) -> Path:
        """校验 relative_path 相对 base_dir，返回 resolve 后的目标。穿越则抛 ValueError。"""
        if not relative_path:
            raise ValueError("relative_path must not be empty.")
        resolved_base = base_dir.resolve()
        target = (resolved_base / relative_path).resolve()
        try:
            target.relative_to(resolved_base)
        except ValueError as exc:
            raise ValueError("relative_path must resolve within the skill directory.") from exc
        return target

    @staticmethod
    def validate_skill_markdown_content(name: str, content: str) -> None:
        """校验 SKILL.md 内容：解析 frontmatter 并校验 name 匹配。"""
        import tempfile

        from deerflow.skills.validation import _validate_skill_frontmatter

        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_skill_dir = Path(tmp_dir) / SkillStorage.validate_skill_name(name)
            temp_skill_dir.mkdir(parents=True, exist_ok=True)
            (temp_skill_dir / SKILL_MD_FILE).write_text(content, encoding="utf-8")
            is_valid, message, parsed_name = _validate_skill_frontmatter(temp_skill_dir)
            if not is_valid:
                raise ValueError(message)
            if parsed_name != name:
                raise ValueError(f"Frontmatter name '{parsed_name}' must match requested skill name '{name}'.")

    def ensure_safe_support_path(self, name: str, relative_path: str) -> Path:
        """校验并返回支持文件的 resolve 后绝对路径。穿越 / 非白名单子目录则抛。"""
        _ALLOWED_SUPPORT_SUBDIRS = {"references", "templates", "scripts", "assets"}
        skill_dir = self.get_custom_skill_dir(self.validate_skill_name(name)).resolve()
        if not relative_path or relative_path.endswith("/"):
            raise ValueError("Supporting file path must include a filename.")
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ValueError("Supporting file path must be relative.")
        if any(part in {"..", ""} for part in relative.parts):
            raise ValueError("Supporting file path must not contain parent-directory traversal.")
        top_level = relative.parts[0] if relative.parts else ""
        if top_level not in _ALLOWED_SUPPORT_SUBDIRS:
            raise ValueError(f"Supporting files must live under one of: {', '.join(sorted(_ALLOWED_SUPPORT_SUBDIRS))}.")
        target = (skill_dir / relative).resolve()
        allowed_root = (skill_dir / top_level).resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("Supporting file path must stay within the selected support directory.") from exc
        return target

    # ------------------------------------------------------------------
    # 抽象原子操作（存储介质特定）
    # ------------------------------------------------------------------

    @abstractmethod
    def get_skills_root_path(self) -> Path:
        """技能根的绝对宿主路径（沙箱挂载用）。"""

    @abstractmethod
    def _iter_skill_files(self) -> Iterable[tuple[SkillCategory, Path, Path]]:
        """为每个 SKILL.md yield ``(category, category_root, skill_md_path)``。"""

    @abstractmethod
    def read_custom_skill(self, name: str) -> str:
        """读自定义技能的 SKILL.md 内容。"""

    @abstractmethod
    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        """原子写文本文件到 ``custom/<name>/<relative_path>``。"""

    @abstractmethod
    async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
        """从 ``.skill`` ZIP 异步安装。"""

    def install_skill_from_archive(self, archive_path: str | Path) -> dict:
        """同步包装——委托 :meth:`ainstall_skill_from_archive`。"""
        from deerflow.skills.installer import _run_async_install

        return _run_async_install(self.ainstall_skill_from_archive(archive_path))

    @abstractmethod
    def delete_custom_skill(self, name: str, *, history_meta: dict | None = None) -> None:
        """删自定义技能（校验 + 可选历史 + 目录移除）。"""

    @abstractmethod
    def custom_skill_exists(self, name: str) -> bool:
        """自定义技能是否存在。"""

    @abstractmethod
    def public_skill_exists(self, name: str) -> bool:
        """公共技能是否存在。"""

    @abstractmethod
    def append_history(self, name: str, record: dict) -> None:
        """为 name 追加一条 JSONL 历史记录。"""

    @abstractmethod
    def read_history(self, name: str) -> list[dict]:
        """返回 name 的全部历史记录（最旧在前）。"""

    # ------------------------------------------------------------------
    # 具体路径 helper（布局是 SKILL.md 协议的一部分）
    # ------------------------------------------------------------------

    def get_container_root(self) -> str:
        """容器内技能根路径。"""
        return self._container_root

    def get_custom_skill_dir(self, name: str) -> Path:
        """``custom/<name>`` 路径（不创建）。"""
        normalized_name = self.validate_skill_name(name)
        return self.get_skills_root_path() / SkillCategory.CUSTOM.value / normalized_name

    def get_custom_skill_file(self, name: str) -> Path:
        """``custom/<name>/SKILL.md`` 路径。"""
        normalized_name = self.validate_skill_name(name)
        return self.get_custom_skill_dir(normalized_name) / SKILL_MD_FILE

    def get_skill_history_file(self, name: str) -> Path:
        """``custom/.history/<name>.jsonl`` 路径（不创建父目录）。"""
        normalized_name = self.validate_skill_name(name)
        return self.get_skills_root_path() / SkillCategory.CUSTOM.value / ".history" / f"{normalized_name}.jsonl"

    # ------------------------------------------------------------------
    # 最终模板方法流
    # ------------------------------------------------------------------

    def load_skills(self, *, enabled_only: bool = False) -> list[Skill]:
        """发现全部技能、合并 enabled 状态、排序、可选过滤。

        enabled 状态每次重读 extensions config（他进程改动立即生效）。
        """
        from deerflow.skills.parser import parse_skill_file

        skills_by_name: dict[str, Skill] = {}
        for category, category_root, md_path in self._iter_skill_files():
            skill = parse_skill_file(
                md_path,
                category=category,
                relative_path=md_path.parent.relative_to(category_root),
            )
            if skill:
                skills_by_name[skill.name] = skill

        skills = list(skills_by_name.values())

        # 从 extensions config 合并 enabled 状态（每次重读，他进程改动立即生效）。
        try:
            from deerflow.config.extensions_config import ExtensionsConfig

            extensions_config = ExtensionsConfig.from_file()
            for skill in skills:
                skill.enabled = extensions_config.is_skill_enabled(skill.name, skill.category)
        except Exception as e:
            logger.warning("Failed to load extensions config: %s", e)

        if enabled_only:
            skills = [s for s in skills if s.enabled]

        skills.sort(key=lambda s: s.name)
        return skills

    def ensure_custom_skill_is_editable(self, name: str) -> None:
        """自定义技能可编辑性校验：存在 OK；是公共技能抛「需新建覆盖」；不存在抛 FileNotFoundError。"""
        if self.custom_skill_exists(name):
            return
        if self.public_skill_exists(name):
            raise ValueError(f"'{name}' is a built-in skill. To customise it, create a new skill with the same name under skills/custom/.")
        raise FileNotFoundError(f"Custom skill '{name}' not found.")
