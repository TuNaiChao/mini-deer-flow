"""沙箱工具：bash / ls / glob / grep / read_file / write_file / str_replace。

这七个工具是 agent 操作文件系统与执行命令的唯一入口。它们都通过注入的 ``runtime``
读取图状态里的 ``sandbox``（沙箱 id）和 ``thread_data``（线程目录），把 agent 看到的
**虚拟路径**（``/mnt/user-data/...``、``/mnt/skills``）翻译成宿主真实路径后再交给
``Sandbox`` 实例执行。``glob`` / ``grep`` 的搜索算法在 :mod:`deerflow.sandbox.search`，
``LocalSandbox`` 复用同一份，AIO 沙箱（M10b）也复用——两端输出格式一致。

两层防御：
- **provider 层**（``LocalSandbox``）：``path_mappings`` 翻译虚拟路径、反解析输出、只读挂载拒绝。
- **工具层**（本文件）：``validate_local_tool_path`` / ``validate_local_bash_command_paths``
  做 defense-in-depth——即便 provider 翻译出问题，``..`` 穿越、越界绝对路径、不安全的 ``cd``
  目标也会被拦下。本层是 best-effort 守卫，**不是**安全沙箱边界；真正的隔离靠 AIO 容器（M10b）。

同路径写串行化在 :mod:`deerflow.sandbox.file_operation_lock`（``write_file`` / ``str_replace``
按 ``(sandbox_id, path)`` 取锁，防并发读-改-写丢数据）。
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from langchain.tools import tool

from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.exceptions import SandboxError, SandboxNotFoundError, SandboxRuntimeError
from deerflow.sandbox.file_operation_lock import get_file_operation_lock
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.sandbox.search import GrepMatch
from deerflow.sandbox.security import LOCAL_HOST_BASH_DISABLED_MESSAGE, is_host_bash_allowed
from deerflow.tools.types import Runtime

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------


class ThreadDataState(TypedDict, total=False):
    """线程数据目录（由未来的 ThreadDataMiddleware / M16 写入图状态）。

    运行时是普通 dict；这里仅作文档与键提示。
    """

    workspace_path: str
    uploads_path: str
    outputs_path: str


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_SKILLS_CONTAINER_PATH = "/mnt/skills"
_DEFAULT_WRITE_FILE_ERROR_MAX_CHARS = 2000

# glob / grep 工具的返回条数默认值与硬上限（防一次搜出几万条把上下文撑爆）。
# 模型可传 max_results，但会被钳制到 [default, 上限] 之内。
_DEFAULT_GLOB_MAX_RESULTS = 200
_MAX_GLOB_MAX_RESULTS = 1000
_DEFAULT_GREP_MAX_RESULTS = 100
_MAX_GREP_MAX_RESULTS = 500

# 单次非追加 write_file 的字节上限（issue #3189）：过大的单次写与 LLM 流式 chunk 超时相关。
# 80KB ≈ 20K token，在默认 240s stream_chunk_timeout 下留足余量。环境变量
# DEERFLOW_WRITE_FILE_MAX_BYTES 可覆盖；设 0 或负数禁用。
_WRITE_FILE_CONTENT_MAX_BYTES = 80 * 1024
_WRITE_FILE_MAX_BYTES_ENV = "DEERFLOW_WRITE_FILE_MAX_BYTES"

# bash 命令路径扫描用的正则。
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![:\w])(?<!:/)/(?:[^\s\"'`;&|<>()]+)")
_FILE_URL_PATTERN = re.compile(r"\bfile://\S+", re.IGNORECASE)
_URL_WITH_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_URL_IN_COMMAND_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'`;&|<>()]+", re.IGNORECASE)
_DOTDOT_PATH_SEGMENT_PATTERN = re.compile(r"(?:^|[/\\=])\.\.(?:$|[/\\])")

# host bash 放行时允许的系统路径前缀（可执行 / 设备引用，如 /bin/sh、/dev/null）。
_LOCAL_BASH_SYSTEM_PATH_PREFIXES = ("/bin/", "/usr/bin/", "/usr/sbin/", "/sbin/", "/opt/homebrew/bin/", "/dev/")
_LOCAL_BASH_CWD_COMMANDS = {"cd", "pushd"}
_LOCAL_BASH_COMMAND_WRAPPERS = {"command", "builtin"}
_LOCAL_BASH_COMMAND_PREFIX_KEYWORDS = {"!", "{", "case", "do", "elif", "else", "for", "if", "select", "then", "time", "until", "while"}
_LOCAL_BASH_COMMAND_END_KEYWORDS = {"}", "done", "esac", "fi"}
_LOCAL_BASH_ROOT_PATH_COMMANDS = {"awk", "cat", "cp", "du", "find", "grep", "head", "less", "ln", "ls", "more", "mv", "rm", "sed", "tail", "tar"}
_SHELL_COMMAND_SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")"}
_SHELL_REDIRECTION_OPERATORS = {"<", ">", "<<", ">>", "<<<", "<>", ">&", "<&", "&>", "&>>", ">|"}


# ---------------------------------------------------------------------------
# 同路径写串行化：见 deerflow.sandbox.file_operation_lock.get_file_operation_lock
# （write_file / str_replace 调用，按 (sandbox_id, path) 取锁，隔离沙箱互不争用同一虚拟路径）。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# skills 路径解析（缓存）
# ---------------------------------------------------------------------------


def _get_skills_container_path() -> str:
    """从 config 取 skills 容器路径（带默认 /mnt/skills 兜底，成功后缓存）。"""
    cached = getattr(_get_skills_container_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        value = get_app_config().skills.container_path
        _get_skills_container_path._cached = value  # type: ignore[attr-defined]
        return value
    except Exception:
        return _DEFAULT_SKILLS_CONTAINER_PATH


def _get_skills_host_path() -> str | None:
    """skills 宿主路径（目录不存在 / 配置缺失返回 None；成功后缓存）。"""
    cached = getattr(_get_skills_host_path, "_cached", None)
    if cached is not None:
        return cached
    try:
        skills_path = get_app_config().skills.get_skills_path()
        if skills_path.exists():
            value = str(skills_path)
            _get_skills_host_path._cached = value  # type: ignore[attr-defined]
            return value
    except Exception:
        pass
    return None


def _is_skills_path(path: str) -> bool:
    skills_prefix = _get_skills_container_path()
    return path == skills_prefix or path.startswith(f"{skills_prefix}/")


def _resolve_skills_path(path: str) -> str:
    """把虚拟 skills 路径翻成宿主路径。skills 目录未配置 / 不存在则 raise。"""
    skills_container = _get_skills_container_path()
    skills_host = _get_skills_host_path()
    if skills_host is None:
        raise FileNotFoundError(f"Skills directory not available for path: {path}")
    if path == skills_container:
        return skills_host
    relative = path[len(skills_container) :].lstrip("/")
    return _join_path_preserving_style(skills_host, relative)


def _get_custom_mounts() -> list:
    """读 ``config.sandbox.mounts`` 的自定义卷挂载（成功后缓存）。

    host_path 不存在的条目被滤掉（与 ``LocalSandboxProvider._setup_path_mappings`` 一致——
    不存在的源目录挂不进去）。配置加载失败返空列表**不缓存**，让后续调用能在配置就绪后重试。
    """
    cached = getattr(_get_custom_mounts, "_cached", None)
    if cached is not None:
        return cached
    try:
        from pathlib import Path

        config = get_app_config()
        mounts = []
        sandbox_config = getattr(config, "sandbox", None)
        raw_mounts = getattr(sandbox_config, "mounts", None) if sandbox_config else None
        if raw_mounts:
            mounts = [m for m in raw_mounts if Path(m.host_path).exists()]
        _get_custom_mounts._cached = mounts  # type: ignore[attr-defined]
        return mounts
    except Exception:
        return []


def _is_custom_mount_path(path: str) -> bool:
    """path 是否落在某个自定义挂载的 container_path 下。"""
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            return True
    return False


def _get_custom_mount_for_path(path: str):
    """匹配 path 的挂载配置（最长前缀优先）。无匹配返回 None。"""
    best = None
    for mount in _get_custom_mounts():
        if path == mount.container_path or path.startswith(f"{mount.container_path}/"):
            if best is None or len(mount.container_path) > len(best.container_path):
                best = mount
    return best


def _resolve_custom_mount_path(path: str) -> str:
    """把自定义挂载的 container_path 翻成宿主路径。无匹配 raise。"""
    mount = _get_custom_mount_for_path(path)
    if mount is None:
        raise FileNotFoundError(f"Path is not under a custom mount: {path}")
    if path == mount.container_path:
        return mount.host_path
    relative = path[len(mount.container_path) :].lstrip("/")
    return _join_path_preserving_style(mount.host_path, relative)


# ---------------------------------------------------------------------------
# 路径拼接 / 风格保持
# ---------------------------------------------------------------------------


def _path_variants(path: str) -> set[str]:
    return {path, path.replace("\\", "/"), path.replace("/", "\\")}


def _path_separator_for_style(path: str) -> str:
    return "\\" if "\\" in path and "/" not in path else "/"


def _join_path_preserving_style(base: str, relative: str) -> str:
    if not relative:
        return base
    separator = _path_separator_for_style(base)
    normalized_relative = relative.replace("\\" if separator == "/" else "/", separator).lstrip("/\\")
    stripped_base = base.rstrip("/\\")
    return f"{stripped_base}{separator}{normalized_relative}"


# ---------------------------------------------------------------------------
# 虚拟路径翻译（/mnt/user-data/* → 宿主）
# ---------------------------------------------------------------------------


def _thread_virtual_to_actual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    """由 thread_data 建「虚拟前缀 → 宿主目录」映射。"""
    mappings: dict[str, str] = {}
    workspace = thread_data.get("workspace_path")
    uploads = thread_data.get("uploads_path")
    outputs = thread_data.get("outputs_path")
    if workspace:
        mappings[f"{VIRTUAL_PATH_PREFIX}/workspace"] = workspace
    if uploads:
        mappings[f"{VIRTUAL_PATH_PREFIX}/uploads"] = uploads
    if outputs:
        mappings[f"{VIRTUAL_PATH_PREFIX}/outputs"] = outputs
    # 三个目录共享同一父级时，额外映射虚拟根，让 ls /mnt/user-data 也能工作。
    actual_dirs = [Path(p) for p in (workspace, uploads, outputs) if p]
    if actual_dirs:
        common_parent = str(Path(actual_dirs[0]).parent)
        if all(str(path.parent) == common_parent for path in actual_dirs):
            mappings[VIRTUAL_PATH_PREFIX] = common_parent
    return mappings


def _thread_actual_to_virtual_mappings(thread_data: ThreadDataState) -> dict[str, str]:
    return {actual: virtual for virtual, actual in _thread_virtual_to_actual_mappings(thread_data).items()}


def replace_virtual_path(path: str, thread_data: ThreadDataState | None) -> str:
    """把单个虚拟 ``/mnt/user-data`` 路径替换成实际线程目录路径。

    映射：
        /mnt/user-data/workspace/* -> thread_data['workspace_path']/*
        /mnt/user-data/uploads/*   -> thread_data['uploads_path']/*
        /mnt/user-data/outputs/*   -> thread_data['outputs_path']/*
    """
    if thread_data is None:
        return path

    mappings = _thread_virtual_to_actual_mappings(thread_data)
    if not mappings:
        return path

    # 最长前缀优先 + 段边界检查。
    for virtual_base, actual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        if path == virtual_base:
            return actual_base
        if path.startswith(f"{virtual_base}/"):
            rest = path[len(virtual_base) :].lstrip("/")
            result = _join_path_preserving_style(actual_base, rest)
            if path.endswith("/") and not result.endswith(("/", "\\")):
                result += _path_separator_for_style(actual_base)
            return result
    return path


def replace_virtual_paths_in_command(command: str, thread_data: ThreadDataState | None) -> str:
    """把命令串里所有虚拟路径（/mnt/user-data、/mnt/skills）替换成宿主路径。"""
    result = command

    # skills 路径
    skills_container = _get_skills_container_path()
    skills_host = _get_skills_host_path()
    if skills_host and skills_container in result:
        skills_pattern = re.compile(rf"{re.escape(skills_container)}(/[^\s\"';&|<>()]*)?")

        def replace_skills_match(match: re.Match) -> str:
            return _resolve_skills_path(match.group(0))

        result = skills_pattern.sub(replace_skills_match, result)

    # user-data 路径
    if VIRTUAL_PATH_PREFIX in result and thread_data is not None:
        pattern = re.compile(rf"{re.escape(VIRTUAL_PATH_PREFIX)}(/[^\s\"';&|<>()]*)?")

        def replace_user_data_match(match: re.Match) -> str:
            return replace_virtual_path(match.group(0), thread_data)

        result = pattern.sub(replace_user_data_match, result)

    return result


def mask_local_paths_in_output(output: str, thread_data: ThreadDataState | None) -> str:
    """把输出里的宿主绝对路径「洗回」虚拟路径（skills + user-data），不泄露宿主布局。"""
    result = output

    # skills 宿主路径
    skills_host = _get_skills_host_path()
    skills_container = _get_skills_container_path()
    if skills_host:
        raw_base = str(Path(skills_host))
        resolved_base = str(Path(skills_host).resolve())
        for base in _path_variants(raw_base) | _path_variants(resolved_base):
            escaped = re.escape(base).replace(r"\\", r"[/\\]")
            pattern = re.compile(escaped + r"(?:[/\\][^\s\"';&|<>()]*)?")

            def replace_skills(match: re.Match, _base: str = base) -> str:
                matched_path = match.group(0)
                if matched_path == _base:
                    return skills_container
                relative = matched_path[len(_base) :].lstrip("/\\")
                return f"{skills_container}/{relative}" if relative else skills_container

            result = pattern.sub(replace_skills, result)

    if thread_data is None:
        return result

    # user-data 宿主路径
    mappings = _thread_actual_to_virtual_mappings(thread_data)
    if not mappings:
        return result

    for actual_base, virtual_base in sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True):
        raw_base = str(Path(actual_base))
        resolved_base = str(Path(actual_base).resolve())
        for base in _path_variants(raw_base) | _path_variants(resolved_base):
            escaped_actual = re.escape(base).replace(r"\\", r"[/\\]")
            pattern = re.compile(escaped_actual + r"(?:[/\\][^\s\"';&|<>()]*)?")

            def replace_match(match: re.Match, _base: str = base, _virtual: str = virtual_base) -> str:
                matched_path = match.group(0)
                if matched_path == _base:
                    return _virtual
                relative = matched_path[len(_base) :].lstrip("/\\")
                return f"{_virtual}/{relative}" if relative else _virtual

            result = pattern.sub(replace_match, result)

    return result


# ---------------------------------------------------------------------------
# 路径穿越防御 / 校验
# ---------------------------------------------------------------------------


def _reject_path_traversal(path: str) -> None:
    """拒绝含 ``..`` 段的路径。"""
    normalised = path.replace("\\", "/")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PermissionError("Access denied: path traversal detected")


def validate_local_tool_path(path: str, thread_data: ThreadDataState | None, *, read_only: bool = False) -> None:
    """安全闸：检查虚拟路径是否允许 local-sandbox 访问。**不**解析路径，只判定 + raise。

    允许的虚拟路径族：
      - ``/mnt/user-data/*`` —— 总是允许（读 + 写）。
      - ``/mnt/skills/*``    —— 仅 read_only 时允许。
      - 自定义卷挂载（``sandbox.mounts``）的 container_path —— 遵循各挂载的 read_only。

    Args:
        path: 虚拟路径。
        thread_data: 线程数据（local 沙箱必须有）。
        read_only: True 时 skills 路径放行。

    Raises:
        SandboxRuntimeError: thread_data 缺失。
        PermissionError: 路径不允许或含穿越。
    """

    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for local sandbox")

    _reject_path_traversal(path)

    if _is_skills_path(path):
        if not read_only:
            raise PermissionError(f"Write access to skills path is not allowed: {path}")
        return

    # 自定义卷挂载：遵循各挂载的 read_only 设置（operator 配的可写挂载允许写）。
    custom_mount = _get_custom_mount_for_path(path)
    if custom_mount is not None:
        if not read_only and getattr(custom_mount, "read_only", False):
            raise PermissionError(f"Write access to read-only custom mount is not allowed: {path}")
        return

    if path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        return

    raise PermissionError(f"Only paths under {VIRTUAL_PATH_PREFIX}/ or {_get_skills_container_path()}/ are allowed")


def _validate_resolved_user_data_path(resolved: Path, thread_data: ThreadDataState) -> None:
    """解析后的宿主路径必须落在 workspace/uploads/outputs 之一内。"""
    allowed_roots = [Path(p).resolve() for p in (thread_data.get("workspace_path"), thread_data.get("uploads_path"), thread_data.get("outputs_path")) if p is not None]
    if not allowed_roots:
        raise SandboxRuntimeError("No allowed local sandbox directories configured")
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise PermissionError("Access denied: path traversal detected")


def _resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """解析 ``/mnt/user-data`` 虚拟路径并校验在界内，返回宿主路径串。"""
    resolved_str = replace_virtual_path(path, thread_data)
    resolved = Path(resolved_str).resolve()
    _validate_resolved_user_data_path(resolved, thread_data)
    return str(resolved)


def resolve_and_validate_user_data_path(path: str, thread_data: ThreadDataState) -> str:
    """``_resolve_and_validate_user_data_path`` 的公开别名。"""
    return _resolve_and_validate_user_data_path(path, thread_data)


def _resolve_local_read_path(path: str, thread_data: ThreadDataState) -> str:
    """读路径统一解析：skills 优先，其次自定义挂载，否则 user-data。"""
    validate_local_tool_path(path, thread_data, read_only=True)
    if _is_skills_path(path):
        return _resolve_skills_path(path)
    if _is_custom_mount_path(path):
        return _resolve_custom_mount_path(path)
    return _resolve_and_validate_user_data_path(path, thread_data)


# ---------------------------------------------------------------------------
# glob / grep 结果上限钳制与格式化
# ---------------------------------------------------------------------------


def _clamp_max_results(value: int, *, default: int, upper_bound: int) -> int:
    """把 max_results 钳到 [default, upper_bound]：<=0 用默认，否则取 min(值, 上限)。"""
    if value <= 0:
        return default
    return min(value, upper_bound)


def _resolve_max_results(requested: int, *, default: int, upper_bound: int) -> int:
    """解析 max_results：模型请求值与默认值都各自钳制后取 min（取更严的那个）。

    mini 暂未实现 ``config.tools[]`` 的 ``max_results`` 覆盖（deer 的 ``_get_tool_config_int``），
    所以这里只钳制模型请求值；该覆盖可在 M15 tools 模块补全后接入。
    """
    requested_clamped = _clamp_max_results(requested, default=default, upper_bound=upper_bound)
    default_clamped = _clamp_max_results(default, default=default, upper_bound=upper_bound)
    return min(requested_clamped, default_clamped)


def _format_glob_results(root_path: str, matches: list[str], truncated: bool) -> str:
    """格式化 glob 结果：首行计数（截断则注明），随后逐条编号路径。"""
    if not matches:
        return f"No files matched under {root_path}"

    lines = [f"Found {len(matches)} paths under {root_path}"]
    if truncated:
        lines[0] += f" (showing first {len(matches)})"
    lines.extend(f"{index}. {path}" for index, path in enumerate(matches, start=1))
    if truncated:
        lines.append("Results truncated. Narrow the path or pattern to see fewer matches.")
    return "\n".join(lines)


def _format_grep_results(root_path: str, matches: list[GrepMatch], truncated: bool) -> str:
    """格式化 grep 结果：首行命中数（截断则注明），随后每条 ``path:line: 内容``。"""
    if not matches:
        return f"No matches found under {root_path}"

    lines = [f"Found {len(matches)} matches under {root_path}"]
    if truncated:
        lines[0] += f" (showing first {len(matches)})"
    lines.extend(f"{match.path}:{match.line_number}: {match.line}" for match in matches)
    if truncated:
        lines.append("Results truncated. Narrow the path or add a glob filter.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# host bash 命令路径校验（allow_host_bash=true 时的 defense-in-depth）
# ---------------------------------------------------------------------------


def _is_non_file_url_token(token: str) -> bool:
    values = [token]
    if "=" in token:
        values.append(token.split("=", 1)[1])
    for value in values:
        match = _URL_WITH_SCHEME_PATTERN.match(value)
        if match and not value.lower().startswith("file://"):
            return True
    return False


def _non_file_url_spans(command: str) -> list[tuple[int, int]]:
    spans = []
    for match in _URL_IN_COMMAND_PATTERN.finditer(command):
        if not match.group().lower().startswith("file://"):
            spans.append(match.span())
    return spans


def _is_in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _has_dotdot_path_segment(token: str) -> bool:
    if _is_non_file_url_token(token):
        return False
    return bool(_DOTDOT_PATH_SEGMENT_PATTERN.search(token))


def _split_shell_tokens(command: str) -> list[str]:
    try:
        normalized = command.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ; ")
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        # 语法错误交给真实 shell 拒；这里 best-effort 不把语法错变成安全误报。
        return command.split()


def _is_shell_command_separator(token: str) -> bool:
    return token in _SHELL_COMMAND_SEPARATORS


def _is_shell_redirection_operator(token: str) -> bool:
    return token in _SHELL_REDIRECTION_OPERATORS


def _is_shell_assignment(token: str) -> bool:
    name, separator, _ = token.partition("=")
    if not separator or not name:
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def _is_allowed_local_bash_absolute_path(path: str, *, allow_system_paths: bool) -> bool:
    if path == VIRTUAL_PATH_PREFIX or path.startswith(f"{VIRTUAL_PATH_PREFIX}/"):
        _reject_path_traversal(path)
        return True
    if _is_skills_path(path):
        _reject_path_traversal(path)
        return True
    if allow_system_paths and any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _LOCAL_BASH_SYSTEM_PATH_PREFIXES):
        return True
    return False


def _next_cd_target(tokens: list[str], start_index: int) -> tuple[str | None, int]:
    index = start_index
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_command_separator(token):
            return None, index
        if _is_shell_redirection_operator(token):
            index += 2
            continue
        if token == "--":
            index += 1
            continue
        if token in {"-L", "-P", "-e", "-@"}:
            index += 1
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        return token, index + 1
    return None, index


def _validate_local_bash_cwd_target(command_name: str, target: str | None) -> None:
    if target is None or target == "-":
        raise PermissionError(f"Unsafe working directory change in command: {command_name}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith(("$", "`")):
        raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith("~"):
        raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")
    if target.startswith("/"):
        _reject_path_traversal(target)
        if not _is_allowed_local_bash_absolute_path(target, allow_system_paths=False):
            raise PermissionError(f"Unsafe working directory change in command: {command_name} {target}. Use paths under {VIRTUAL_PATH_PREFIX}")


def _validate_local_bash_root_path_args(command_name: str, tokens: list[str], start_index: int) -> None:
    if command_name not in _LOCAL_BASH_ROOT_PATH_COMMANDS:
        return
    index = start_index
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_command_separator(token):
            return
        if _is_shell_redirection_operator(token):
            index += 2
            continue
        if token == "/" and not _is_non_file_url_token(token):
            raise PermissionError(f"Unsafe absolute paths in command: /. Use paths under {VIRTUAL_PATH_PREFIX}")
        index += 1


def _validate_local_bash_shell_tokens(command: str) -> None:
    """保守地拒绝绝对路径扫描漏掉的相对路径逃逸。"""
    if re.search(r"\$\([^)]*\b(?:cd|pushd)\b", command):
        raise PermissionError(f"Unsafe working directory change in command substitution. Use paths under {VIRTUAL_PATH_PREFIX}")

    tokens = _split_shell_tokens(command)
    for token in tokens:
        if _is_shell_command_separator(token) or _is_shell_redirection_operator(token):
            continue
        if _has_dotdot_path_segment(token):
            raise PermissionError("Access denied: path traversal detected")

    at_command_start = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_command_separator(token):
            at_command_start = True
            index += 1
            continue
        if _is_shell_redirection_operator(token):
            index += 1
            continue
        if at_command_start and _is_shell_assignment(token):
            index += 1
            continue

        command_name = token.rsplit("/", 1)[-1]
        if at_command_start and command_name in _LOCAL_BASH_COMMAND_PREFIX_KEYWORDS | _LOCAL_BASH_COMMAND_END_KEYWORDS:
            index += 1
            continue

        if not at_command_start:
            index += 1
            continue

        at_command_start = False
        if command_name in _LOCAL_BASH_COMMAND_WRAPPERS and index + 1 < len(tokens):
            wrapped_name = tokens[index + 1].rsplit("/", 1)[-1]
            if wrapped_name in _LOCAL_BASH_CWD_COMMANDS:
                target, next_index = _next_cd_target(tokens, index + 2)
                _validate_local_bash_cwd_target(wrapped_name, target)
                index = next_index
                continue
            _validate_local_bash_root_path_args(wrapped_name, tokens, index + 2)

        if command_name not in _LOCAL_BASH_CWD_COMMANDS:
            _validate_local_bash_root_path_args(command_name, tokens, index + 1)
            index += 1
            continue

        target, next_index = _next_cd_target(tokens, index + 1)
        _validate_local_bash_cwd_target(command_name, target)
        index = next_index


def validate_local_bash_command_paths(command: str, thread_data: ThreadDataState | None) -> None:
    """校验 local-sandbox bash 命令里的绝对路径（``allow_host_bash: true`` 显式 opt-in 时的守卫）。

    **仅是 best-effort 守卫，不是安全沙箱边界**，不得当作与宿主文件系统的隔离。
    local 模式要求用 ``/mnt/user-data`` 下的虚拟路径访问用户数据；``/mnt/skills`` 路径
    允许（仅做穿越检查）；一小撮系统路径前缀（/bin/、/dev/ 等）保留给可执行/设备引用。
    """

    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for local sandbox")

    # 拦 file:// URL：绕过绝对路径正则却能本地读文件。
    file_url_match = _FILE_URL_PATTERN.search(command)
    if file_url_match:
        raise PermissionError(f"Unsafe file:// URL in command: {file_url_match.group()}. Use paths under {VIRTUAL_PATH_PREFIX}")

    unsafe_paths: list[str] = []
    _validate_local_bash_shell_tokens(command)
    url_spans = _non_file_url_spans(command)

    for match in _ABSOLUTE_PATH_PATTERN.finditer(command):
        if _is_in_spans(match.start(), url_spans):
            continue
        absolute_path = match.group()
        if _is_allowed_local_bash_absolute_path(absolute_path, allow_system_paths=True):
            continue
        unsafe_paths.append(absolute_path)

    if unsafe_paths:
        unsafe = ", ".join(sorted(dict.fromkeys(unsafe_paths)))
        raise PermissionError(f"Unsafe absolute paths in command: {unsafe}. Use paths under {VIRTUAL_PATH_PREFIX}")


def _apply_cwd_prefix(command: str, thread_data: ThreadDataState | None) -> str:
    """前缀 ``cd <workspace> &&`` 让相对路径锚定到线程 workspace。"""
    if thread_data and (workspace := thread_data.get("workspace_path")):
        return f"cd {shlex.quote(workspace)} && {command}"
    return command


# ---------------------------------------------------------------------------
# runtime 取沙箱 / 线程数据
# ---------------------------------------------------------------------------


def get_thread_data(runtime: Runtime | None) -> ThreadDataState | None:
    """从 runtime.state 取 thread_data。"""
    if runtime is None or runtime.state is None:
        return None
    return runtime.state.get("thread_data")


def is_local_sandbox(runtime: Runtime | None) -> bool:
    """当前沙箱是否是 local（认 ``"local"`` 与 ``"local:{thread_id}"`` 两种 id）。"""
    if runtime is None or runtime.state is None:
        return False
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is None:
        return False
    sandbox_id = sandbox_state.get("sandbox_id")
    if not isinstance(sandbox_id, str):
        return False
    return sandbox_id == "local" or sandbox_id.startswith("local:")


def _sanitize_error(error: Exception, runtime: Runtime | None = None) -> str:
    """把错误信息里的宿主路径洗回虚拟路径（local 模式），不泄露布局。"""
    msg = f"{type(error).__name__}: {error}"
    if runtime is not None and is_local_sandbox(runtime):
        thread_data = get_thread_data(runtime)
        msg = mask_local_paths_in_output(msg, thread_data)
    return msg


def sandbox_from_runtime(runtime: Runtime | None = None) -> Sandbox:
    """从 runtime 取沙箱实例（**已弃用**，优先用 ``ensure_sandbox_initialized``）。

    假设沙箱已初始化，未初始化则 raise。
    """

    if runtime is None:
        raise SandboxRuntimeError("Tool runtime not available")
    if runtime.state is None:
        raise SandboxRuntimeError("Tool runtime state not available")
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is None:
        raise SandboxRuntimeError("Sandbox state not initialized in runtime")
    sandbox_id = sandbox_state.get("sandbox_id")
    if sandbox_id is None:
        raise SandboxRuntimeError("Sandbox ID not found in state")
    sandbox = get_sandbox_provider().get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError(f"Sandbox with ID '{sandbox_id}' not found", sandbox_id=sandbox_id)
    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id
    return sandbox


def ensure_sandbox_initialized(runtime: Runtime | None = None) -> Sandbox:
    """确保沙箱已初始化（首次调用按需 acquire，后续返回已有实例）。

    线程安全由 provider 内部锁保证。
    """

    if runtime is None or runtime.state is None:
        raise SandboxRuntimeError("Tool runtime not available")

    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id
                return sandbox
            # 沙箱已释放，落到下面重新 acquire。

    # 懒 acquire：取 thread_id 并获取沙箱。
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = provider.acquire(thread_id)
    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)
    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id
    return sandbox


async def ensure_sandbox_initialized_async(runtime: Runtime | None = None) -> Sandbox:
    """``ensure_sandbox_initialized`` 的 async 版：走 provider 的 async acquire。"""

    if runtime is None or runtime.state is None:
        raise SandboxRuntimeError("Tool runtime not available")

    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state is not None:
        sandbox_id = sandbox_state.get("sandbox_id")
        if sandbox_id is not None:
            sandbox = get_sandbox_provider().get(sandbox_id)
            if sandbox is not None:
                if runtime.context is not None:
                    runtime.context["sandbox_id"] = sandbox_id
                return sandbox

    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id is None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id") if runtime.config else None
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = await provider.acquire_async(thread_id)
    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}

    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)
    if runtime.context is not None:
        runtime.context["sandbox_id"] = sandbox_id
    return sandbox


async def _run_sync_tool_after_async_sandbox_init(func: Callable[..., str] | None, runtime: Runtime, *args: object) -> str:
    """先用 async provider 懒初始化沙箱，再把同步工具体卸载到线程跑。"""

    try:
        await ensure_sandbox_initialized_async(runtime)
    except SandboxError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: Unexpected error initializing sandbox: {_sanitize_error(e, runtime)}"

    if func is None:
        return "Error: Tool implementation not available"
    return await asyncio.to_thread(func, runtime, *args)


def ensure_thread_directories_exist(runtime: Runtime | None) -> None:
    """确保线程的 workspace/uploads/outputs 目录存在（懒创建，仅 local 沙箱需要）。

    local 沙箱在宿主上按需建目录；容器沙箱（AIO）这些目录已 bind-mount，无需建。
    """
    if runtime is None or not is_local_sandbox(runtime):
        return
    thread_data = get_thread_data(runtime)
    if thread_data is None:
        return
    if runtime.state.get("thread_directories_created"):
        return
    for key in ("workspace_path", "uploads_path", "outputs_path"):
        path = thread_data.get(key)
        if path:
            os.makedirs(path, exist_ok=True)
    runtime.state["thread_directories_created"] = True


# ---------------------------------------------------------------------------
# 输出截断
# ---------------------------------------------------------------------------


def _truncate_bash_output(output: str, max_chars: int) -> str:
    """中间截断 bash 输出，首尾各保一半（stderr/stdout 顺序不确定，两端都可能有错）。"""
    if max_chars == 0 or len(output) <= max_chars:
        return output
    total_len = len(output)
    marker_max_len = len(f"\n... [middle truncated: {total_len} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total_len - kept
    marker = f"\n... [middle truncated: {skipped} chars skipped] ...\n"
    return f"{output[:head_len]}{marker}{output[-tail_len:] if tail_len > 0 else ''}"


def _truncate_read_file_output(output: str, max_chars: int) -> str:
    """头部截断 read_file 输出（源码/文档从头读最有上下文）。"""
    if max_chars == 0 or len(output) <= max_chars:
        return output
    total = len(output)
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. Use start_line/end_line to read a specific range] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. Use start_line/end_line to read a specific range] ..."
    return f"{output[:kept]}{marker}"


def _truncate_ls_output(output: str, max_chars: int) -> str:
    """头部截断 ls 输出（目录列表从头看结构最相关）。"""
    if max_chars == 0 or len(output) <= max_chars:
        return output
    total = len(output)
    marker_max_len = len(f"\n... [truncated: showing first {total} of {total} chars. Use a more specific path to see fewer results] ...")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return output[:max_chars]
    marker = f"\n... [truncated: showing first {kept} of {total} chars. Use a more specific path to see fewer results] ..."
    return f"{output[:kept]}{marker}"


def _truncate_write_file_error_detail(detail: str, max_chars: int) -> str:
    """中间截断 write_file 错误详情，保留首尾。"""
    if max_chars == 0 or len(detail) <= max_chars:
        return detail
    total = len(detail)
    marker_max_len = len(f"\n... [write_file error truncated: {total} chars skipped] ...\n")
    kept = max(0, max_chars - marker_max_len)
    if kept == 0:
        return detail[:max_chars]
    head_len = kept // 2
    tail_len = kept - head_len
    skipped = total - kept
    marker = f"\n... [write_file error truncated: {skipped} chars skipped] ...\n"
    return f"{detail[:head_len]}{marker}{detail[-tail_len:] if tail_len > 0 else ''}"


def _format_write_file_error(requested_path: str, error: Exception, runtime: Runtime | None = None, *, max_chars: int = _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS) -> str:
    """有界、脱敏的 write_file 错误串。"""
    header = f"Error: Failed to write file '{requested_path}'"
    detail = _sanitize_error(error, runtime)
    if max_chars == 0:
        return f"{header}: {detail}"
    detail_budget = max_chars - len(header) - 2
    if detail_budget <= 0:
        return _truncate_write_file_error_detail(f"{header}: {detail}", max_chars)
    return f"{header}: {_truncate_write_file_error_detail(detail, detail_budget)}"


def _effective_write_file_max_bytes() -> int:
    """调时读 ``DEERFLOW_WRITE_FILE_MAX_BYTES``（非导入时），便于测试 / 运行时调整。"""
    raw = os.environ.get(_WRITE_FILE_MAX_BYTES_ENV)
    if raw is None:
        return _WRITE_FILE_CONTENT_MAX_BYTES
    try:
        return int(raw)
    except ValueError:
        return _WRITE_FILE_CONTENT_MAX_BYTES


def _sandbox_output_max_chars(attr: str, default: int) -> int:
    """从 config.sandbox 取某输出截断上限，配置读失败兜底 default。"""
    try:
        sandbox_cfg = get_app_config().sandbox
        return getattr(sandbox_cfg, attr) if sandbox_cfg else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 七个工具（bash / ls / glob / grep / read_file / write_file / str_replace）
# ---------------------------------------------------------------------------


@tool("bash", parse_docstring=True)
def bash_tool(runtime: Runtime, description: str, command: str) -> str:
    """Execute a bash command in a Linux environment.

    - Use `python` to run Python code.
    - Prefer a thread-local virtual environment in `/mnt/user-data/workspace/.venv`.
    - Use `python -m pip` (inside the virtual environment) to install Python packages.

    Args:
        description: Explain why you are running this command in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        command: The bash command to execute. Always use absolute paths for files and directories.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        if is_local_sandbox(runtime):
            # host bash 准入闸：默认禁用（非安全边界）。
            if not is_host_bash_allowed():
                return f"Error: {LOCAL_HOST_BASH_DISABLED_MESSAGE}"
            ensure_thread_directories_exist(runtime)
            thread_data = get_thread_data(runtime)
            validate_local_bash_command_paths(command, thread_data)
            command = replace_virtual_paths_in_command(command, thread_data)
            command = _apply_cwd_prefix(command, thread_data)
            output = sandbox.execute_command(command)
            max_chars = _sandbox_output_max_chars("bash_output_max_chars", 20000)
            return _truncate_bash_output(mask_local_paths_in_output(output, thread_data), max_chars)
        # 非 local provider（未来 Docker）：直接执行。
        ensure_thread_directories_exist(runtime)
        max_chars = _sandbox_output_max_chars("bash_output_max_chars", 20000)
        return _truncate_bash_output(sandbox.execute_command(command), max_chars)
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        if isinstance(e, SandboxError):
            return f"Error: {e}"
        return f"Error: Unexpected error executing command: {_sanitize_error(e, runtime)}"


async def _bash_tool_async(runtime: Runtime, description: str, command: str) -> str:
    return await _run_sync_tool_after_async_sandbox_init(bash_tool.func, runtime, description, command)


bash_tool.coroutine = _bash_tool_async  # type: ignore[attr-defined]


@tool("ls", parse_docstring=True)
def ls_tool(runtime: Runtime, description: str, path: str) -> str:
    """List the contents of a directory up to 2 levels deep in tree format.

    Args:
        description: Explain why you are listing this directory in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the directory to list.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data, read_only=True)
            if _is_skills_path(path):
                path = _resolve_skills_path(path)
            else:
                path = _resolve_and_validate_user_data_path(path, thread_data)
        children = sandbox.list_dir(path)
        if not children:
            return "(empty)"
        output = "\n".join(children)
        if thread_data is not None:
            output = mask_local_paths_in_output(output, thread_data)
        max_chars = _sandbox_output_max_chars("ls_output_max_chars", 20000)
        return _truncate_ls_output(output, max_chars)
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied: {requested_path}"
    except Exception as e:
        if isinstance(e, SandboxError):
            return f"Error: {e}"
        return f"Error: Unexpected error listing directory: {_sanitize_error(e, runtime)}"


async def _ls_tool_async(runtime: Runtime, description: str, path: str) -> str:
    return await _run_sync_tool_after_async_sandbox_init(ls_tool.func, runtime, description, path)


ls_tool.coroutine = _ls_tool_async  # type: ignore[attr-defined]


@tool("glob", parse_docstring=True)
def glob_tool(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    include_dirs: bool = False,
    max_results: int = _DEFAULT_GLOB_MAX_RESULTS,
) -> str:
    """Find files or directories that match a glob pattern under a root directory.

    Args:
        description: Explain why you are searching for these paths in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        pattern: The glob pattern to match relative to the root path, for example `**/*.py`.
        path: The **absolute** root directory to search under.
        include_dirs: Whether matching directories should also be returned. Default is False.
        max_results: Maximum number of paths to return. Default is 200.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        effective_max_results = _resolve_max_results(max_results, default=_DEFAULT_GLOB_MAX_RESULTS, upper_bound=_MAX_GLOB_MAX_RESULTS)
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            if thread_data is None:
                raise SandboxRuntimeError("Thread data not available for local sandbox")
            path = _resolve_local_read_path(path, thread_data)
        matches, truncated = sandbox.glob(path, pattern, include_dirs=include_dirs, max_results=effective_max_results)
        if thread_data is not None:
            matches = [mask_local_paths_in_output(match, thread_data) for match in matches]
        return _format_glob_results(requested_path, matches, truncated)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except NotADirectoryError:
        return f"Error: Path is not a directory: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error searching paths: {_sanitize_error(e, runtime)}"


async def _glob_tool_async(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    include_dirs: bool = False,
    max_results: int = _DEFAULT_GLOB_MAX_RESULTS,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        glob_tool.func,
        runtime,
        description,
        pattern,
        path,
        include_dirs,
        max_results,
    )


glob_tool.coroutine = _glob_tool_async  # type: ignore[attr-defined]


@tool("grep", parse_docstring=True)
def grep_tool(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = _DEFAULT_GREP_MAX_RESULTS,
) -> str:
    """Search for matching lines inside text files under a root directory.

    Args:
        description: Explain why you are searching file contents in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        pattern: The string or regex pattern to search for.
        path: The **absolute** root directory to search under.
        glob: Optional glob filter for candidate files, for example `**/*.py`.
        literal: Whether to treat `pattern` as a plain string. Default is False.
        case_sensitive: Whether matching is case-sensitive. Default is False.
        max_results: Maximum number of matching lines to return. Default is 100.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        effective_max_results = _resolve_max_results(max_results, default=_DEFAULT_GREP_MAX_RESULTS, upper_bound=_MAX_GREP_MAX_RESULTS)
        thread_data = None
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            if thread_data is None:
                raise SandboxRuntimeError("Thread data not available for local sandbox")
            path = _resolve_local_read_path(path, thread_data)
        matches, truncated = sandbox.grep(
            path,
            pattern,
            glob=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=effective_max_results,
        )
        if thread_data is not None:
            matches = [
                GrepMatch(
                    path=mask_local_paths_in_output(match.path, thread_data),
                    line_number=match.line_number,
                    line=match.line,
                )
                for match in matches
            ]
        return _format_grep_results(requested_path, matches, truncated)
    except SandboxError as e:
        return f"Error: {e}"
    except FileNotFoundError:
        return f"Error: Directory not found: {requested_path}"
    except NotADirectoryError:
        return f"Error: Path is not a directory: {requested_path}"
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"
    except PermissionError:
        return f"Error: Permission denied: {requested_path}"
    except Exception as e:
        return f"Error: Unexpected error searching file contents: {_sanitize_error(e, runtime)}"


async def _grep_tool_async(
    runtime: Runtime,
    description: str,
    pattern: str,
    path: str,
    glob: str | None = None,
    literal: bool = False,
    case_sensitive: bool = False,
    max_results: int = _DEFAULT_GREP_MAX_RESULTS,
) -> str:
    return await _run_sync_tool_after_async_sandbox_init(
        grep_tool.func,
        runtime,
        description,
        pattern,
        path,
        glob,
        literal,
        case_sensitive,
        max_results,
    )


grep_tool.coroutine = _grep_tool_async  # type: ignore[attr-defined]


@tool("read_file", parse_docstring=True)
def read_file_tool(runtime: Runtime, description: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read the contents of a text file. Use this to examine source code, configuration files, logs, or any text-based file.

    Args:
        description: Explain why you are reading this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to read.
        start_line: Optional starting line number (1-indexed, inclusive). Use with end_line to read a specific range.
        end_line: Optional ending line number (1-indexed, inclusive). Use with start_line to read a specific range.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data, read_only=True)
            if _is_skills_path(path):
                path = _resolve_skills_path(path)
            else:
                path = _resolve_and_validate_user_data_path(path, thread_data)
        content = sandbox.read_file(path)
        if not content:
            return "(empty)"
        if start_line is not None and end_line is not None:
            content = "\n".join(content.splitlines()[start_line - 1 : end_line])
        max_chars = _sandbox_output_max_chars("read_file_output_max_chars", 50000)
        return _truncate_read_file_output(content, max_chars)
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied reading file: {requested_path}"
    except IsADirectoryError:
        return f"Error: Path is a directory, not a file: {requested_path}"
    except Exception as e:
        if isinstance(e, SandboxError):
            return f"Error: {e}"
        return f"Error: Unexpected error reading file: {_sanitize_error(e, runtime)}"


async def _read_file_tool_async(runtime: Runtime, description: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    return await _run_sync_tool_after_async_sandbox_init(read_file_tool.func, runtime, description, path, start_line, end_line)


read_file_tool.coroutine = _read_file_tool_async  # type: ignore[attr-defined]


@tool("write_file", parse_docstring=True)
def write_file_tool(runtime: Runtime, description: str, path: str, content: str, append: bool = False) -> str:
    """Write text content to a file. By default this overwrites the target file; set append=True to add content to the end without replacing existing content.

    SIZE POLICY (issue #3189):
    A single non-append write_file call must not exceed 80 KB of UTF-8 content.
    Oversized single-shot writes correlate with LLM streaming chunk-gap timeouts.
    For larger documents, use ONE of these strategies (write_file rejects oversized payloads):

      1. INCREMENTAL EDIT (preferred for revisions): after the initial write, use `str_replace`
         to surgically update sections.
      2. APPEND-IN-CHUNKS (for new long-form content): split into sections, each well under 80 KB.
         First call uses append=False; subsequent calls use append=True. The 80 KB cap does NOT
         apply to append=True calls.

    Operators can override via env var `DEERFLOW_WRITE_FILE_MAX_BYTES` (0 disables the guard).

    Args:
        description: Explain why you are writing to this file in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to write to. ALWAYS PROVIDE THIS PARAMETER SECOND.
        content: The content to write to the file. ALWAYS PROVIDE THIS PARAMETER THIRD.
        append: Whether to append content to the end of the file instead of overwriting it. Defaults to False.
    """
    if not append:
        max_bytes = _effective_write_file_max_bytes()
        if max_bytes > 0:
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > max_bytes:
                return (
                    f"Error: write_file content ({content_bytes} bytes) exceeds the "
                    f"{max_bytes}-byte single-call limit. Split the content into smaller "
                    "pieces: either (a) write the first section now, then use `str_replace` "
                    "for further edits, or (b) call write_file again with append=True "
                    "carrying the next section. See SIZE POLICY in the tool docstring "
                    "or issue #3189 for the rationale."
                )
    try:
        requested_path = path
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            path = _resolve_and_validate_user_data_path(path, thread_data)
        # 同 (沙箱, 路径) 写串行化，隔离沙箱互不争用同一虚拟路径。
        with get_file_operation_lock(sandbox, path):
            sandbox.write_file(path, content, append)
        return "OK"
    except PermissionError:
        return _truncate_write_file_error_detail(f"Error: Permission denied writing to file: {requested_path}", _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS)
    except IsADirectoryError:
        return _truncate_write_file_error_detail(f"Error: Path is a directory, not a file: {requested_path}", _DEFAULT_WRITE_FILE_ERROR_MAX_CHARS)
    except OSError as e:
        return _format_write_file_error(requested_path, e, runtime)
    except Exception as e:
        if isinstance(e, SandboxError):
            return _format_write_file_error(requested_path, e, runtime)
        return _format_write_file_error(requested_path, e, runtime)


async def _write_file_tool_async(runtime: Runtime, description: str, path: str, content: str, append: bool = False) -> str:
    return await _run_sync_tool_after_async_sandbox_init(write_file_tool.func, runtime, description, path, content, append)


write_file_tool.coroutine = _write_file_tool_async  # type: ignore[attr-defined]


@tool("str_replace", parse_docstring=True)
def str_replace_tool(runtime: Runtime, description: str, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    """Replace a substring in a file with another substring.
    If `replace_all` is False (default), the substring to replace must appear **exactly once** in the file.

    Args:
        description: Explain why you are replacing the substring in short words. ALWAYS PROVIDE THIS PARAMETER FIRST.
        path: The **absolute** path to the file to replace the substring in. ALWAYS PROVIDE THIS PARAMETER SECOND.
        old_str: The substring to replace. ALWAYS PROVIDE THIS PARAMETER THIRD.
        new_str: The new substring. ALWAYS PROVIDE THIS PARAMETER FOURTH.
        replace_all: Whether to replace all occurrences of the substring. If False, only the first occurrence will be replaced. Default is False.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        ensure_thread_directories_exist(runtime)
        requested_path = path
        if is_local_sandbox(runtime):
            thread_data = get_thread_data(runtime)
            validate_local_tool_path(path, thread_data)
            path = _resolve_and_validate_user_data_path(path, thread_data)
        with get_file_operation_lock(sandbox, path):
            content = sandbox.read_file(path)
            if not content:
                return "OK"
            if old_str not in content:
                return f"Error: String to replace not found in file: {requested_path}"
            if replace_all:
                content = content.replace(old_str, new_str)
            else:
                content = content.replace(old_str, new_str, 1)
            sandbox.write_file(path, content)
        return "OK"
    except FileNotFoundError:
        return f"Error: File not found: {requested_path}"
    except PermissionError:
        return f"Error: Permission denied accessing file: {requested_path}"
    except Exception as e:
        if isinstance(e, SandboxError):
            return f"Error: {e}"
        return f"Error: Unexpected error replacing string: {_sanitize_error(e, runtime)}"


async def _str_replace_tool_async(runtime: Runtime, description: str, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    return await _run_sync_tool_after_async_sandbox_init(str_replace_tool.func, runtime, description, path, old_str, new_str, replace_all)


str_replace_tool.coroutine = _str_replace_tool_async  # type: ignore[attr-defined]
