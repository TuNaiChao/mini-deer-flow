"""技能 ``.skill`` ZIP 安装的共享业务逻辑（M14 skills）。

对齐 deer ``skills/installer.py``。纯业务逻辑，无 FastAPI / HTTP 依赖。Gateway 与 Client
都委托给这些函数。

安全防护：拒绝绝对路径 / 穿越成员、跳过 symlink、512MB 总解压上限（zip 炸弹防御）、
macOS ``__MACOSX`` / dotfile 过滤、预占目标原子搬入。
"""

import asyncio
import concurrent.futures
import logging
import posixpath
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from deerflow.skills.permissions import make_skill_tree_sandbox_readable
from deerflow.skills.security_scanner import scan_skill_content

logger = logging.getLogger(__name__)

_PROMPT_INPUT_DIRS = {"references", "templates"}
_PROMPT_INPUT_SUFFIXES = frozenset({".json", ".markdown", ".md", ".rst", ".txt", ".yaml", ".yml"})


class SkillAlreadyExistsError(ValueError):
    """同名技能已安装。"""


class SkillSecurityScanError(ValueError):
    """技能归档未通过安全审查。"""


def is_unsafe_zip_member(info: zipfile.ZipInfo) -> bool:
    """zip 成员路径是绝对路径或试图穿越目录则返回 True。"""
    name = info.filename
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        return True
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return True
    if PureWindowsPath(name).is_absolute():
        return True
    if ".." in path.parts:
        return True
    return False


def is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """据 ZipInfo 外部属性检测 symlink。"""
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def should_ignore_archive_entry(path: Path) -> bool:
    """macOS 元数据目录与 dotfile 返回 True。"""
    return path.name.startswith(".") or path.name == "__MACOSX"


def resolve_skill_dir_from_archive(temp_path: Path) -> Path:
    """从解压内容里定位技能根目录（滤掉 macOS 元数据 / dotfile）。空则抛 ValueError。"""
    items = [p for p in temp_path.iterdir() if not should_ignore_archive_entry(p)]
    if not items:
        raise ValueError("Skill archive is empty")
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return temp_path


def safe_extract_skill_archive(
    zip_ref: zipfile.ZipFile,
    dest_path: Path,
    max_total_size: int = 512 * 1024 * 1024,
) -> None:
    """安全解压技能归档。

    防护：拒绝绝对路径与穿越（``..``）；跳过 symlink；强制总解压上限（zip 炸弹防御）。
    """
    dest_root = dest_path.resolve()
    total_written = 0

    for info in zip_ref.infolist():
        if is_unsafe_zip_member(info):
            raise ValueError(f"Archive contains unsafe member path: {info.filename!r}")

        if is_symlink_member(info):
            logger.warning("Skipping symlink entry in skill archive: %s", info.filename)
            continue

        normalized_name = posixpath.normpath(info.filename.replace("\\", "/"))
        member_path = dest_root.joinpath(*PurePosixPath(normalized_name).parts)
        if not member_path.resolve().is_relative_to(dest_root):
            raise ValueError(f"Zip entry escapes destination: {info.filename!r}")
        member_path.parent.mkdir(parents=True, exist_ok=True)

        if info.is_dir():
            member_path.mkdir(parents=True, exist_ok=True)
            continue

        with zip_ref.open(info) as src, member_path.open("wb") as dst:
            while chunk := src.read(65536):
                total_written += len(chunk)
                if total_written > max_total_size:
                    raise ValueError("Skill archive is too large or appears highly compressed.")
                dst.write(chunk)


def _is_script_support_file(rel_path: Path) -> bool:
    return bool(rel_path.parts) and rel_path.parts[0] == "scripts"


def _should_scan_support_file(rel_path: Path) -> bool:
    if _is_script_support_file(rel_path):
        return True
    return bool(rel_path.parts) and rel_path.parts[0] in _PROMPT_INPUT_DIRS and rel_path.suffix.lower() in _PROMPT_INPUT_SUFFIXES


def _move_staged_skill_into_reserved_target(staging_target: Path, target: Path) -> None:
    """预占目标目录（0o700）后把暂存内容搬入；失败回滚清理。"""
    installed = False
    reserved = False
    try:
        target.mkdir(mode=0o700)
        reserved = True
        for child in staging_target.iterdir():
            shutil.move(str(child), target / child.name)
        make_skill_tree_sandbox_readable(target)
        installed = True
    except FileExistsError as e:
        raise SkillAlreadyExistsError(f"Skill '{target.name}' already exists") from e
    finally:
        if reserved and not installed and target.exists():
            shutil.rmtree(target)


async def _scan_skill_file_or_raise(skill_dir: Path, path: Path, skill_name: str, *, executable: bool) -> None:
    """审一个技能文件；block / 可执行非 allow / 非法决定 → 抛 SkillSecurityScanError。"""
    rel_path = path.relative_to(skill_dir).as_posix()
    location = f"{skill_name}/{rel_path}"
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except UnicodeDecodeError as e:
        raise SkillSecurityScanError(f"Security scan failed for skill '{skill_name}': {location} must be valid UTF-8") from e

    try:
        result = await scan_skill_content(content, executable=executable, location=location)
    except Exception as e:
        raise SkillSecurityScanError(f"Security scan failed for {location}: {e}") from e

    decision = getattr(result, "decision", None)
    reason = str(getattr(result, "reason", "") or "No reason provided.")
    if decision == "block":
        if rel_path == "SKILL.md":
            raise SkillSecurityScanError(f"Security scan blocked skill '{skill_name}': {reason}")
        raise SkillSecurityScanError(f"Security scan blocked {location}: {reason}")
    if executable and decision != "allow":
        raise SkillSecurityScanError(f"Security scan rejected executable {location}: {reason}")
    if decision not in {"allow", "warn"}:
        raise SkillSecurityScanError(f"Security scan failed for {location}: invalid scanner decision {decision!r}")


def _collect_scannable_files(skill_dir: Path) -> list[Path]:
    """枚举归档文件供审查（阻塞；离事件循环跑）。"""
    return [candidate for candidate in sorted(skill_dir.rglob("*")) if candidate.is_file()]


async def _scan_skill_archive_contents_or_raise(skill_dir: Path, skill_name: str) -> None:
    """对所有可安装的文本与脚本文件跑安全审查。嵌套 SKILL.md 禁止。"""
    skill_md = skill_dir / "SKILL.md"
    await _scan_skill_file_or_raise(skill_dir, skill_md, skill_name, executable=False)

    for path in await asyncio.to_thread(_collect_scannable_files, skill_dir):
        rel_path = path.relative_to(skill_dir)
        if rel_path == Path("SKILL.md"):
            continue
        if path.name == "SKILL.md":
            raise SkillSecurityScanError(f"Security scan failed for skill '{skill_name}': nested SKILL.md is not allowed at {skill_name}/{rel_path.as_posix()}")
        if not _should_scan_support_file(rel_path):
            continue

        await _scan_skill_file_or_raise(skill_dir, path, skill_name, executable=_is_script_support_file(rel_path))


def _run_async_install(coro):
    """在已有事件循环里跑 async 安装：用一次性线程池跑 ``asyncio.run``。否则直接 ``asyncio.run``。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
