"""宿主机本地沙箱实现：``LocalSandbox``。

``LocalSandbox`` 是本仓库的本地沙箱（Docker / AIO provisioner 是 M10b，独立模块）。
它**不是**真正的安全边界——bash 和文件操作直接作用在**宿主机进程**上，隔离完全靠：

1. **虚拟路径翻译**：agent 只看到 ``/mnt/user-data/{workspace,uploads,outputs}`` 和
   ``/mnt/skills``，provider 把这些前缀翻译成按 ``(user_id, thread_id)`` 隔离的宿主目录。
2. **路径穿越防御**：拒绝 ``..`` 段、拒绝越界绝对路径（工具层 ``validate_local_*``）。
3. **host bash 默认禁用**：``bash`` 工具需 ``sandbox.allow_host_bash: true`` 才放行
   （见 ``sandbox/security.py``）。

虚拟路径映射表（``PathMapping``）是核心数据结构：每条把「容器路径前缀」↔「宿主本地路径」
对应起来，带 ``read_only`` 标志（skills 目录只读）。``_find_path_mapping`` 按最长前缀匹配，
``_resolve_path`` 把容器路径翻成宿主路径，``_reverse_resolve_path`` 反过来——后者用于把
命令 / 文件内容 / 输出里的**宿主绝对路径**「洗回」虚拟路径，避免向 agent 泄露宿主目录布局。

provider（``LocalSandboxProvider``）在 :mod:`deerflow.sandbox.local.local_sandbox_provider`。
"""

from __future__ import annotations

import errno
import logging
import ntpath
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.local.list_dir import list_dir
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, find_glob_matches, find_grep_matches

logger = logging.getLogger(__name__)

# 虚拟前缀常量（与 config.paths.VIRTUAL_PATH_PREFIX 一致；download_file 用）。
_USER_DATA_VIRTUAL_PREFIX = VIRTUAL_PATH_PREFIX

# 模块级别名，向后兼容直接摸 ``local_sandbox_provider._singleton`` 的旧调用方 / 测试。
# 新代码读 provider 实例属性（``_generic_sandbox`` / ``_thread_sandboxes``）。
_singleton: "LocalSandbox | None" = None

# per-thread LocalSandbox 缓存上限（LRU）。长跑 gateway 里 thread_id 无界增长，
# 缓存对象本身很轻（一组 PathMapping + 一个 agent-written 集合），但还是要封顶防泄漏。
DEFAULT_MAX_CACHED_THREAD_SANDBOXES = 256


# ---------------------------------------------------------------------------
# 路径映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathMapping:
    """一条「容器路径 ↔ 宿主本地路径」映射，带可选只读标志。"""

    container_path: str
    local_path: str
    read_only: bool = False


class ResolvedPath(NamedTuple):
    path: str
    mapping: PathMapping | None


# ---------------------------------------------------------------------------
# LocalSandbox
# ---------------------------------------------------------------------------


class LocalSandbox(Sandbox):
    """宿主机本地沙箱。

    通过 ``path_mappings`` 把容器路径翻译成宿主路径。所有公开方法接收的都是**容器视角**
    路径（如 ``/mnt/user-data/workspace/a.py``），内部解析后操作宿主文件系统。
    输出 / 命令里的宿主绝对路径会被「反解析」回容器路径，避免泄露宿主布局。
    """

    # ------------------------------------------------------------------
    # shell 检测（execute_command 用）
    # ------------------------------------------------------------------

    @staticmethod
    def _shell_name(shell: str) -> str:
        return shell.replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _is_powershell(shell: str) -> bool:
        return LocalSandbox._shell_name(shell) in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}

    @staticmethod
    def _is_cmd_shell(shell: str) -> bool:
        return LocalSandbox._shell_name(shell) in {"cmd", "cmd.exe"}

    @staticmethod
    def _is_msys_shell(shell: str) -> bool:
        normalized = shell.replace("\\", "/").lower()
        shell_name = LocalSandbox._shell_name(shell)
        return shell_name in {"sh.exe", "bash.exe"} and any(part in normalized for part in ("/git/", "/mingw", "/msys"))

    @staticmethod
    def _find_first_available_shell(candidates: tuple[str, ...]) -> str | None:
        for shell in candidates:
            if os.path.isabs(shell):
                if os.path.isfile(shell) and os.access(shell, os.X_OK):
                    return shell
                continue
            shell_from_path = shutil.which(shell)
            if shell_from_path is not None:
                return shell_from_path
        return None

    def __init__(self, id: str, path_mappings: list[PathMapping] | None = None):
        """
        Args:
            id: 沙箱 id（如 ``"local:abc"``）。
            path_mappings: 路径映射列表（skills 默认只读）。
        """
        super().__init__(id)
        self.path_mappings = path_mappings or []
        # 记录经 write_file 写入的路径，让 read_file 只对「agent 自写内容」做反解析——
        # 用户上传文件 / 外部工具产物不该被悄悄改写。
        self._agent_written_paths: set[str] = set()

    # ------------------------------------------------------------------
    # 路径解析（容器 ↔ 宿主）
    # ------------------------------------------------------------------

    def _is_read_only_path(self, resolved_path: str) -> bool:
        """解析后的宿主路径是否落在某个只读挂载下（多映射嵌套时取最具体的一条）。"""
        resolved = str(Path(resolved_path).resolve())

        best_mapping: PathMapping | None = None
        best_prefix_len = -1

        for mapping in self.path_mappings:
            local_resolved = str(Path(mapping.local_path).resolve())
            if resolved == local_resolved or resolved.startswith(local_resolved + os.sep):
                prefix_len = len(local_resolved)
                if prefix_len > best_prefix_len:
                    best_prefix_len = prefix_len
                    best_mapping = mapping

        if best_mapping is None:
            return False
        return best_mapping.read_only

    def _find_path_mapping(self, path: str) -> tuple[PathMapping, str] | None:
        path_str = str(path)
        # 最长容器前缀优先匹配，避免 /mnt/user-data 抢了 /mnt/user-data/workspace。
        for mapping in sorted(self.path_mappings, key=lambda m: len(m.container_path.rstrip("/") or "/"), reverse=True):
            container_path = mapping.container_path.rstrip("/") or "/"
            if container_path == "/":
                if path_str.startswith("/"):
                    return mapping, path_str.lstrip("/")
                continue
            if path_str == container_path or path_str.startswith(container_path + "/"):
                relative = path_str[len(container_path) :].lstrip("/")
                return mapping, relative
        return None

    def _resolve_path_with_mapping(self, path: str) -> ResolvedPath:
        mapping_match = self._find_path_mapping(path)
        if mapping_match is None:
            return ResolvedPath(path, None)

        mapping, relative = mapping_match
        local_root = Path(mapping.local_path).resolve()
        resolved_path = (local_root / relative).resolve() if relative else local_root

        try:
            resolved_path.relative_to(local_root)
        except ValueError as exc:
            # 翻译后路径逃出了挂载根——穿越，拒绝。
            raise PermissionError(errno.EACCES, "Access denied: path escapes mounted directory", path) from exc

        return ResolvedPath(str(resolved_path), mapping)

    def _resolve_path(self, path: str) -> str:
        return self._resolve_path_with_mapping(path).path

    def _is_resolved_path_read_only(self, resolved: ResolvedPath) -> bool:
        return bool(resolved.mapping and resolved.mapping.read_only) or self._is_read_only_path(resolved.path)

    def _reverse_resolve_path(self, path: str) -> str:
        """把宿主本地路径反解析回容器路径（无映射则原样返回）。"""
        normalized_path = path.replace("\\", "/")
        path_str = str(Path(normalized_path).resolve())

        # 最长 local_path 优先，保证更具体的映射胜出。
        for mapping in sorted(self.path_mappings, key=lambda m: len(m.local_path), reverse=True):
            local_path_resolved = str(Path(mapping.local_path).resolve())
            if path_str == local_path_resolved or path_str.startswith(local_path_resolved + "/"):
                relative = path_str[len(local_path_resolved) :].lstrip("/")
                return f"{mapping.container_path}/{relative}" if relative else mapping.container_path
        return path_str

    def _reverse_resolve_paths_in_output(self, output: str) -> str:
        """把输出串里出现的宿主绝对路径批量洗回容器路径。"""
        import re

        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.local_path), reverse=True)
        if not sorted_mappings:
            return output

        result = output
        for mapping in sorted_mappings:
            escaped_local = re.escape(str(Path(mapping.local_path).resolve()))
            pattern = re.compile(escaped_local + r"(?:[/\\][^\s\"';&|<>()]*)?")

            def replace_match(match: re.Match) -> str:
                return self._reverse_resolve_path(match.group(0))

            result = pattern.sub(replace_match, result)
        return result

    def _resolve_paths_in_command(self, command: str) -> str:
        """把 bash 命令串里的容器路径翻译成宿主路径（按 shell 边界字符切分）。"""
        import re

        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)
        if not sorted_mappings:
            return command

        # 前瞻断言确保只在路径段边界匹配，避免 /mnt/user-data 误匹配 /mnt/user-data-extra。
        patterns = [re.escape(m.container_path) + r"(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?" for m in sorted_mappings]
        pattern = re.compile("|".join(f"({p})" for p in patterns))

        def replace_match(match: re.Match) -> str:
            return self._resolve_path(match.group(0))

        return pattern.sub(replace_match, command)

    def _resolve_paths_in_content(self, content: str) -> str:
        """把文件内容里的容器路径翻译成宿主路径（纯文本，无 shell 语义）。

        翻译结果归一为正斜杠，避免 Windows 反斜杠在源码里变成非法转义。
        """
        import re

        sorted_mappings = sorted(self.path_mappings, key=lambda m: len(m.container_path), reverse=True)
        if not sorted_mappings:
            return content

        patterns = [re.escape(m.container_path) + r"(?=/|$|[^\w./-])(?:/[^\s\"';&|<>()]*)?" for m in sorted_mappings]
        pattern = re.compile("|".join(f"({p})" for p in patterns))

        def replace_match(match: re.Match) -> str:
            resolved = self._resolve_path(match.group(0))
            return resolved.replace("\\", "/")

        return pattern.sub(replace_match, content)

    # ------------------------------------------------------------------
    # Sandbox 接口实现
    # ------------------------------------------------------------------

    @staticmethod
    def _get_shell() -> str:
        """探测可用 shell（zsh → bash → sh → `sh` on PATH → Windows fallback）。"""
        shell = LocalSandbox._find_first_available_shell(("/bin/zsh", "/bin/bash", "/bin/sh", "sh"))
        if shell is not None:
            return shell

        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            shell = LocalSandbox._find_first_available_shell(
                (
                    "pwsh",
                    "pwsh.exe",
                    "powershell",
                    "powershell.exe",
                    ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                    "cmd.exe",
                )
            )
            if shell is not None:
                return shell
            raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, `sh` on PATH, then PowerShell and cmd.exe fallbacks for Windows.")

        raise RuntimeError("No suitable shell executable found. Tried /bin/zsh, /bin/bash, /bin/sh, and `sh` on PATH.")

    def execute_command(self, command: str) -> str:
        # 先翻译命令里的容器路径，再交给真实 shell。
        resolved_command = self._resolve_paths_in_command(command)
        shell = self._get_shell()

        if os.name == "nt":
            env = None
            if self._is_powershell(shell):
                args = [shell, "-NoProfile", "-Command", resolved_command]
            elif self._is_cmd_shell(shell):
                args = [shell, "/c", resolved_command]
            else:
                args = [shell, "-c", resolved_command]
                if self._is_msys_shell(shell):
                    env = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}
            result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=600, env=env)
        else:
            args = [shell, "-c", resolved_command]
            result = subprocess.run(args, shell=False, capture_output=True, text=True, timeout=600)

        output = result.stdout
        if result.stderr:
            output += f"\nStd Error:\n{result.stderr}" if output else result.stderr
        if result.returncode != 0:
            output += f"\nExit Code: {result.returncode}"

        final_output = output if output else "(no output)"
        # 把输出里的宿主路径洗回容器路径，不泄露宿主布局。
        return self._reverse_resolve_paths_in_output(final_output)

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        resolved_path = self._resolve_path(path)
        entries = list_dir(resolved_path, max_depth)
        result: list[str] = []
        for entry in entries:
            is_dir = entry.endswith(("/", "\\"))
            reversed_entry = self._reverse_resolve_path(entry.rstrip("/\\")) if is_dir else self._reverse_resolve_path(entry)
            result.append(f"{reversed_entry}/" if is_dir and not reversed_entry.endswith("/") else reversed_entry)
        return result

    def read_file(self, path: str) -> str:
        resolved_path = self._resolve_path(path)
        try:
            with open(resolved_path, encoding="utf-8") as f:
                content = f.read()
            # 只对 agent 自写文件做反解析；用户上传 / 外部产物原样返回。
            if resolved_path in self._agent_written_paths:
                content = self._reverse_resolve_paths_in_output(content)
            return content
        except OSError as e:
            # 用原始（容器）路径重抛，隐藏内部宿主路径，错误信息更清晰。
            raise type(e)(e.errno, e.strerror, path) from None

    def download_file(self, path: str) -> bytes:
        """下载文件二进制内容（供 view_image 等读二进制场景）。

        仅允许 ``/mnt/user-data`` 下的路径（防越界下载宿主任意文件）；上限 100MB。
        """
        normalised = path.replace("\\", "/")
        stripped_path = normalised.lstrip("/")
        allowed_prefix = _USER_DATA_VIRTUAL_PREFIX.lstrip("/")
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            logger.error("Refused download outside allowed directory: path=%s, allowed_prefix=%s", path, _USER_DATA_VIRTUAL_PREFIX)
            raise PermissionError(errno.EACCES, f"Access denied: path must be under '{_USER_DATA_VIRTUAL_PREFIX}'", path)

        resolved_path = self._resolve_path(path)
        max_download_size = 100 * 1024 * 1024
        try:
            file_size = os.path.getsize(resolved_path)
            if file_size > max_download_size:
                raise OSError(errno.EFBIG, f"File exceeds maximum download size of {max_download_size} bytes", path)
            # TOCTOU：getsize 与 read 间文件可能变大；沙箱受控环境下可接受。
            with open(resolved_path, "rb") as f:
                return f.read()
        except OSError as e:
            raise type(e)(e.errno, e.strerror, path) from None

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            resolved_content = self._resolve_paths_in_content(content)
            mode = "a" if append else "w"
            with open(resolved_path, mode, encoding="utf-8") as f:
                f.write(resolved_content)
            self._agent_written_paths.add(resolved_path)
        except OSError as e:
            raise type(e)(e.errno, e.strerror, path) from None

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """在 root 下按 glob 模式找文件/目录，结果路径反解析回容器视角。"""
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_glob_matches(resolved_path, pattern, include_dirs=include_dirs, max_results=max_results)
        return [self._reverse_resolve_path(match) for match in matches], truncated

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        """在 root 下的文本文件里搜匹配行，命中路径反解析回容器视角。"""
        resolved_path = Path(self._resolve_path(path))
        matches, truncated = find_grep_matches(
            resolved_path,
            pattern,
            glob_pattern=glob,
            literal=literal,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
        return [
            GrepMatch(
                path=self._reverse_resolve_path(match.path),
                line_number=match.line_number,
                line=match.line,
            )
            for match in matches
        ], truncated

    def update_file(self, path: str, content: bytes) -> None:
        resolved = self._resolve_path_with_mapping(path)
        resolved_path = resolved.path
        if self._is_resolved_path_read_only(resolved):
            raise OSError(errno.EROFS, "Read-only file system", path)
        try:
            dir_path = os.path.dirname(resolved_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            with open(resolved_path, "wb") as f:
                f.write(content)
        except OSError as e:
            raise type(e)(e.errno, e.strerror, path) from None


# ---------------------------------------------------------------------------
# 本地路径布局 helper（provider 用；tools.py 从 thread_data 反推 thread_id 也依赖）
# ---------------------------------------------------------------------------


def _thread_user_data_root(thread_id: str, user_id: str) -> Path:
    """某线程的用户数据根目录：``{base_dir}/users/{user_id}/threads/{thread_id}/user-data``。

    与 deer 的 ``paths.sandbox_user_data_dir`` 等价。布局的**唯一真相源**是
    :class:`deerflow.config.paths.Paths.thread_user_data_dir`——本函数委托它，避免
    uploads（M23）/ sandbox 各拼一份造成漂移。``tools.py`` 从
    ``thread_data['workspace_path']`` 反推 thread_id 时依赖本布局：
    ``Path(workspace_path).parent.parent.name == thread_id``。
    """
    from deerflow.config.paths import get_paths

    return get_paths().thread_user_data_dir(user_id, thread_id)


def ensure_thread_dirs(thread_id: str, *, user_id: str) -> Path:
    """确保某线程的 workspace/uploads/outputs 三个目录存在，返回用户数据根。

    委托 :meth:`Paths.ensure_thread_dirs`（M16 起建目录动作的唯一真相源），保持
    sandbox / ThreadDataMiddleware 两处 mkdir 不漂移。
    """
    from deerflow.config.paths import get_paths

    return get_paths().ensure_thread_dirs(thread_id, user_id=user_id)
