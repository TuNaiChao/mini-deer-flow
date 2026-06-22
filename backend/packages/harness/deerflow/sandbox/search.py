"""文件搜索：glob 模式匹配 + grep 内容搜索。

两个工具（``glob`` / ``grep``）的「在目录树下找文件 / 找内容」核心逻辑都在这里。把它从
工具层（``tools.py``）与沙箱实现（``local_sandbox.py``）里抽出来，是因为：

- ``LocalSandbox.glob`` / ``LocalSandbox.grep`` 直接复用本模块的 ``find_glob_matches`` /
  ``find_grep_matches``，AIO 沙箱（M10b）则是远端搜本端滤——两端共用过滤/截断语义。
- 工具层只负责「解析虚拟路径 → 调 sandbox.glob/grep → 把宿主路径洗回虚拟路径」，不关心
  搜索算法本身。

设计要点：
- **忽略模式**：57 个常见噪音目录/文件名（.git / __pycache__ / .venv / node_modules …），
  避免把版本控制、缓存、构建产物塞进 agent 视野。
- **二进制检测**：grep 跳过含 NUL 字节的文件（二进制文件 grep 无意义且可能卡住）。
- **上限截断**：``max_results`` 防一次搜出几万条把上下文撑爆；超限返回 ``truncated=True``，
  工具层提示 agent 收窄 pattern。
- **防 ReDoS**：grep 跳过过长的行（``line_summary_length * 10``），避免在压缩/无换行文件上
  被正则回溯拖死。
- **符号链接**：grep 用 ``is_relative_to(root)`` 校验，防 symlink 逃出搜索根。
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# glob / grep / ls 通用忽略的目录/文件名（fnmatch 模式）。
# 命中即跳过，避免把版本控制、虚拟环境、缓存、构建产物等噪音塞进 agent 视野。
IGNORE_PATTERNS = [
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "env",
    ".tox",
    ".nox",
    ".eggs",
    "*.egg-info",
    "site-packages",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    "target",
    "out",
    ".idea",
    ".vscode",
    "*.swp",
    "*.swo",
    "*~",
    ".project",
    ".classpath",
    ".settings",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "*.lnk",
    "*.log",
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.cache",
    ".cache",
    "logs",
    ".coverage",
    "coverage",
    ".nyc_output",
    "htmlcov",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
]

DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000
DEFAULT_LINE_SUMMARY_LENGTH = 200


@dataclass(frozen=True)
class GrepMatch:
    """一条 grep 命中：文件路径 + 行号 + 该行内容（已截断）。"""

    path: str
    line_number: int
    line: str


def should_ignore_name(name: str) -> bool:
    """名字是否命中任一忽略模式（fnmatch）。"""
    for pattern in IGNORE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def should_ignore_path(path: str) -> bool:
    """路径里任一段命中忽略模式即忽略（跨平台正反斜杠归一）。"""
    return any(should_ignore_name(segment) for segment in path.replace("\\", "/").split("/") if segment)


def path_matches(pattern: str, rel_path: str) -> bool:
    """单个 glob 模式是否匹配某相对路径。

    支持 ``**/`` 前缀（递归任意层）：``**/foo.py`` 既匹配 ``a/foo.py`` 也匹配 ``foo.py``。
    """
    path = PurePosixPath(rel_path)
    if path.match(pattern):
        return True
    if pattern.startswith("**/"):
        return path.match(pattern[3:])
    return False


def truncate_line(line: str, max_chars: int = DEFAULT_LINE_SUMMARY_LENGTH) -> str:
    """截断过长行，尾部加 ``...``（避免一行就把上下文撑爆）。"""
    line = line.rstrip("\n\r")
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 3] + "..."


def is_binary_file(path: Path, sample_size: int = 8192) -> bool:
    """用 NUL 字节启发式判定二进制文件（读不到也当二进制跳过）。"""
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(sample_size)
    except OSError:
        return True


def find_glob_matches(root: Path, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
    """在 root 下按 glob 模式找文件（可选目录）路径。

    Args:
        root: 搜索根（会被 resolve()）。
        pattern: 相对 root 的 glob 模式。
        include_dirs: 是否返回匹配的目录（默认仅文件）。
        max_results: 最多返回多少条。

    Returns:
        ``(绝对路径列表, 是否截断)``。

    Raises:
        FileNotFoundError: root 不存在。
        NotADirectoryError: root 是文件不是目录。
    """
    matches: list[str] = []
    truncated = False
    root = root.resolve()

    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    for current_root, dirs, files in os.walk(root):
        # 原地改 dirs 剪枝：命中的目录不递归进去。
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        # root 已 resolve；os.walk 在其下拼接 current_root，relative_to 无需额外 stat/resolve。
        rel_dir = Path(current_root).relative_to(root)

        if include_dirs:
            for name in dirs:
                rel_path = (rel_dir / name).as_posix()
                if path_matches(pattern, rel_path):
                    matches.append(str(Path(current_root) / name))
                    if len(matches) >= max_results:
                        truncated = True
                        return matches, truncated

        for name in files:
            if should_ignore_name(name):
                continue
            rel_path = (rel_dir / name).as_posix()
            if path_matches(pattern, rel_path):
                matches.append(str(Path(current_root) / name))
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated

    return matches, truncated


def find_grep_matches(
    root: Path,
    pattern: str,
    *,
    glob_pattern: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    line_summary_length: int = DEFAULT_LINE_SUMMARY_LENGTH,
) -> tuple[list[GrepMatch], bool]:
    """在 root 下的文本文件里搜匹配行。

    Args:
        root: 搜索根（会被 resolve()）。
        pattern: 字符串或正则。
        glob_pattern: 可选的候选文件 glob 过滤（如 ``**/*.py``）。
        literal: True 则把 pattern 当纯字符串（re.escape）。
        case_sensitive: 大小写敏感（默认 False，即忽略大小写）。
        max_results: 最多返回多少条命中。
        max_file_size: 超过此字节数的文件跳过（防读巨文件卡住）。
        line_summary_length: 单行摘要长度上限（命中行按此截断）。

    Returns:
        ``(GrepMatch 列表, 是否截断)``。

    Raises:
        FileNotFoundError: root 不存在。
        NotADirectoryError: root 是文件不是目录。
    """
    matches: list[GrepMatch] = []
    truncated = False
    root = root.resolve()

    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    regex_source = re.escape(pattern) if literal else pattern
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(regex_source, flags)

    # 跳过过长的行，防在压缩 / 无换行文件上被正则回溯拖死（ReDoS）。
    _max_line_chars = line_summary_length * 10

    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not should_ignore_name(name)]
        rel_dir = Path(current_root).relative_to(root)

        for name in files:
            if should_ignore_name(name):
                continue

            candidate_path = Path(current_root) / name
            rel_path = (rel_dir / name).as_posix()

            if glob_pattern is not None and not path_matches(glob_pattern, rel_path):
                continue

            try:
                # 跳过 symlink：resolve 后可能逃出 root，防穿越。
                if candidate_path.is_symlink():
                    continue
                file_path = candidate_path.resolve()
                if not file_path.is_relative_to(root):
                    continue
                if file_path.stat().st_size > max_file_size or is_binary_file(file_path):
                    continue
                with file_path.open(encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if len(line) > _max_line_chars:
                            continue
                        if regex.search(line):
                            matches.append(
                                GrepMatch(
                                    path=str(file_path),
                                    line_number=line_number,
                                    line=truncate_line(line, line_summary_length),
                                )
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                return matches, truncated
            except OSError:
                continue

    return matches, truncated
