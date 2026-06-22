"""``AioSandbox`` —— 经 HTTP API 连运行中的 AIO 沙箱容器的 ``Sandbox`` 实现。

它不自己起容器（那是 backend 的活），而是拿一个 ``base_url`` 连到已就绪的 AIO 容器，
调 ``agent_sandbox`` SDK（Fern 生成的 HTTP client）的 ``shell.exec_command`` / ``file.read_file``
等 API 操作容器内的 shell 与文件系统。

关键设计：
- **线程锁串行命令**（#1433）：AIO 容器维护**单个**持久 shell session，并发 ``exec_command`` 会
  把 session 搞坏（返回 ``ErrorObservation`` 而非真输出）。故 ``execute_command`` 用 ``self._lock``
  串行化；即便加了锁仍检测到 ``ErrorObservation``（如多进程共享同一沙箱），就在新 session 上重试。
- **``download_file`` 分块 100MB 上限**：流式读 + 累计字节数，超限 ``OSError(EFBIG)``；且先做
  ``..`` 穿越 + 虚拟前缀校验（AIO 把路径原样转发给容器 API，不像 LocalSandbox 经 ``_resolve_path``
  隐式防穿越）。
- **``close()`` 释放套接字**（#2872）：``agent_sandbox`` SDK 是 Fern 生成的，没暴露 ``close()`` /
  ``__exit__``，故沿属性链摸到真正的 ``httpx.Client``（socket 持有者）显式 ``close()``，防长跑
  provider 生命周期累积未回收的套接字。
- **glob/grep 远端搜本端滤**：glob 用容器 ``find_files`` / ``list_path`` API 拿候选，本端用
  ``search.py`` 的 ``should_ignore_path`` / ``path_matches`` 过滤噪音 + 匹配模式；grep 类似，
  候选文件用容器 ``search_in_file`` 搜，本端 ``truncate_line`` 截断。

soft-load：``agent_sandbox`` SDK 缺包时 ``AioSandboxClient = None``，``AioSandbox.__init__`` 抛
带可操作安装提示的 ImportError（红线 #24）。
"""

from __future__ import annotations

import base64
import errno
import logging
import shlex
import threading
import uuid

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, path_matches, should_ignore_path, truncate_line

logger = logging.getLogger(__name__)

# soft-load agent_sandbox SDK（Fern 生成的 AIO 容器 HTTP client）。缺包时回退 + 安装提示。
try:
    from agent_sandbox import Sandbox as AioSandboxClient

    _HAS_AGENT_SANDBOX = True
except ImportError:  # pragma: no cover - 缺包分支，CI 环境装了 extra
    AioSandboxClient = None  # type: ignore[assignment,misc]
    _HAS_AGENT_SANDBOX = False

_INSTALL_HINT = "AIO 沙箱需要 `agent-sandbox` SDK。安装：`uv pip install 'deerflow-harness[aio_sandbox]'`（或 `pip install agent-sandbox`）。未安装时 AioSandboxProvider 不可用，回退 LocalSandboxProvider。"

_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

# AIO 容器并发命令时返回的损坏输出签名（见 #1433）。
_ERROR_OBSERVATION_SIGNATURE = "'ErrorObservation' object has no attribute 'exit_code'"


def _require_agent_sandbox() -> None:
    """agent_sandbox 缺包时抛带安装提示的 ImportError。"""
    if not _HAS_AGENT_SANDBOX:
        raise ImportError(_INSTALL_HINT)


class AioSandbox(Sandbox):
    """用 agent-infra/sandbox Docker 容器的 ``Sandbox`` 实现。

    经 HTTP API 连运行中的 AIO 沙箱容器。线程锁串行 shell 命令，防并发请求搞坏容器的
    单持久 session（#1433）。
    """

    def __init__(self, id: str, base_url: str, home_dir: str | None = None):
        """
        Args:
            id: 本沙箱实例的唯一 id。
            base_url: 沙箱 API URL（如 ``http://localhost:8080``）。
            home_dir: 容器内 home 目录；None 则首次用时从沙箱取。

        Raises:
            ImportError: ``agent_sandbox`` SDK 未安装。
        """
        _require_agent_sandbox()
        super().__init__(id)
        self._base_url = base_url
        self._client = AioSandboxClient(base_url=base_url, timeout=600)  # type: ignore[union-attr]
        self._home_dir = home_dir
        self._lock = threading.Lock()
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        """尽力关闭本沙箱持有的宿主侧 HTTP client。

        ``agent_sandbox`` SDK 是 Fern 生成的，没暴露 ``close()`` / ``__exit__``，故沿属性链::

            Sandbox._client_wrapper        -> SyncClientWrapper
                .httpx_client              -> Fern HttpClient（wrapper，非 httpx.Client）
                    .httpx_client          -> httpx.Client     <- 真正的 socket 持有者

        关掉它释放池化 socket，防长跑 provider 生命周期累积未回收资源（#2872）。

        幂等、线程安全、非致命：拆解中的失败被记日志吞掉，不阻塞 provider/backend 清理。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client = self._client
            # 锁内丢引用，做 use-after-close 安全：后续命令会明确失败而非复用半关 client。
            self._client = None  # type: ignore[assignment]

        if client is None:
            return

        # 从真 httpx.Client 往上摸到顶层 client，取第一个真暴露 close() 的。
        wrapper = getattr(client, "_client_wrapper", None)
        fern_http = getattr(wrapper, "httpx_client", None)
        real_httpx = getattr(fern_http, "httpx_client", None)
        target = next(
            (c for c in (real_httpx, fern_http, client) if c is not None and hasattr(c, "close")),
            None,
        )
        if target is None:
            logger.debug("AioSandbox %s: no closable client found, nothing to release", self.id)
            return

        try:
            target.close()
        except Exception as e:
            logger.warning("Error closing AioSandbox client for %s: %s", self.id, e)

    @property
    def home_dir(self) -> str:
        """取容器内 home 目录（首次用时从沙箱取并缓存）。"""
        if self._home_dir is None:
            context = self._client.sandbox.get_context()  # type: ignore[union-attr]
            self._home_dir = context.home_dir
        return self._home_dir

    # exec_command 默认 no_change_timeout（秒）。对齐 client 级超时，免得长时间无输出的命令
    # 被沙箱内置 120s 默认值过早掐断。
    _DEFAULT_NO_CHANGE_TIMEOUT = 600

    def execute_command(self, command: str) -> str:
        """在沙箱里执行 shell 命令。

        用锁串行并发请求（AIO 容器维护单持久 shell session，并发会损坏，返回
        ``ErrorObservation``）。若加了锁仍检测到损坏（如多进程共享沙箱），在新 session 上重试。
        """
        with self._lock:
            try:
                result = self._client.shell.exec_command(command=command, no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)  # type: ignore[union-attr]
                output = result.data.output if result.data else ""

                if output and _ERROR_OBSERVATION_SIGNATURE in output:
                    logger.warning("ErrorObservation detected in sandbox output, retrying with a fresh session")
                    fresh_id = str(uuid.uuid4())
                    result = self._client.shell.exec_command(command=command, id=fresh_id, no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)  # type: ignore[union-attr]
                    output = result.data.output if result.data else ""

                return output if output else "(no output)"
            except Exception as e:
                logger.error("Failed to execute command in sandbox: %s", e)
                return f"Error: {e}"

    def read_file(self, path: str) -> str:
        """读容器内文件内容。"""
        try:
            result = self._client.file.read_file(file=path)  # type: ignore[union-attr]
            return result.data.content if result.data else ""
        except Exception as e:
            logger.error("Failed to read file in sandbox: %s", e)
            return f"Error: {e}"

    def download_file(self, path: str) -> bytes:
        """从容器下载文件字节。

        Raises:
            PermissionError: 路径含 ``..`` 穿越段或不在 ``VIRTUAL_PATH_PREFIX`` 内。
            OSError: 文件取不出 / 超 100MB。
        """
        # AIO 把路径原样转发给容器 API，不像 LocalSandbox 经 _resolve_path 隐式防穿越，
        # 故这里显式校验。
        normalised = path.replace("\\", "/")
        for segment in normalised.split("/"):
            if segment == "..":
                logger.error("Refused download due to path traversal: %s", path)
                raise PermissionError(f"Access denied: path traversal detected in '{path}'")

        stripped_path = normalised.lstrip("/")
        allowed_prefix = VIRTUAL_PATH_PREFIX.lstrip("/")
        if stripped_path != allowed_prefix and not stripped_path.startswith(f"{allowed_prefix}/"):
            logger.error("Refused download outside allowed directory: path=%s, allowed_prefix=%s", path, VIRTUAL_PATH_PREFIX)
            raise PermissionError(f"Access denied: path must be under '{VIRTUAL_PATH_PREFIX}': '{path}'")

        with self._lock:
            try:
                chunks: list[bytes] = []
                total = 0
                for chunk in self._client.file.download_file(path=path):  # type: ignore[union-attr]
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_SIZE:
                        raise OSError(
                            errno.EFBIG,
                            f"File exceeds maximum download size of {_MAX_DOWNLOAD_SIZE} bytes",
                            path,
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
            except OSError:
                raise
            except Exception as e:
                logger.error("Failed to download file in sandbox: %s", e)
                raise OSError(f"Failed to download file '{path}' from sandbox: {e}") from e

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        """列容器内目录内容（经 ``find -maxdepth``）。"""
        with self._lock:
            try:
                result = self._client.shell.exec_command(command=f"find {shlex.quote(path)} -maxdepth {max_depth} -type f -o -type d 2>/dev/null | head -500", no_change_timeout=self._DEFAULT_NO_CHANGE_TIMEOUT)  # type: ignore[union-attr]
                output = result.data.output if result.data else ""
                if output:
                    return [line.strip() for line in output.strip().split("\n") if line.strip()]
                return []
            except Exception as e:
                logger.error("Failed to list directory in sandbox: %s", e)
                return []

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        """写文本到容器内文件。``append=True`` 先读旧内容再拼接。"""
        with self._lock:
            try:
                if append:
                    existing = self.read_file(path)
                    if not existing.startswith("Error:"):
                        content = existing + content
                self._client.file.write_file(file=path, content=content)  # type: ignore[union-attr]
            except Exception as e:
                logger.error("Failed to write file in sandbox: %s", e)
                raise

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        """glob 模式找文件：容器 API 拿候选，本端过滤噪音 + 匹配。"""
        if not include_dirs:
            result = self._client.file.find_files(path=path, glob=pattern)  # type: ignore[union-attr]
            files = result.data.files if result.data and result.data.files else []
            filtered = [file_path for file_path in files if not should_ignore_path(file_path)]
            truncated = len(filtered) > max_results
            return filtered[:max_results], truncated

        result = self._client.file.list_path(path=path, recursive=True, show_hidden=False)  # type: ignore[union-attr]
        entries = result.data.files if result.data and result.data.files else []
        matches: list[str] = []
        root_path = path.rstrip("/") or "/"
        root_prefix = root_path if root_path == "/" else f"{root_path}/"
        for entry in entries:
            if entry.path != root_path and not entry.path.startswith(root_prefix):
                continue
            if should_ignore_path(entry.path):
                continue
            rel_path = entry.path[len(root_path) :].lstrip("/")
            if path_matches(pattern, rel_path):
                matches.append(entry.path)
                if len(matches) >= max_results:
                    return matches, True
        return matches, False

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
        """内容搜：候选文件用容器 ``search_in_file`` 搜，本端截断 + 过滤噪音。"""
        import re as _re

        regex_source = _re.escape(pattern) if literal else pattern
        # 本地编译校验 pattern，让无效正则抛 re.error（被 grep_tool 的 except re.error 接住），
        # 而非泛化的远端 API 错误。
        _re.compile(regex_source, 0 if case_sensitive else _re.IGNORECASE)
        regex = regex_source if case_sensitive else f"(?i){regex_source}"

        if glob is not None:
            find_result = self._client.file.find_files(path=path, glob=glob)  # type: ignore[union-attr]
            candidate_paths = find_result.data.files if find_result.data and find_result.data.files else []
        else:
            list_result = self._client.file.list_path(path=path, recursive=True, show_hidden=False)  # type: ignore[union-attr]
            entries = list_result.data.files if list_result.data and list_result.data.files else []
            candidate_paths = [entry.path for entry in entries if not entry.is_directory]

        matches: list[GrepMatch] = []
        truncated = False

        for file_path in candidate_paths:
            if should_ignore_path(file_path):
                continue

            search_result = self._client.file.search_in_file(file=file_path, regex=regex)  # type: ignore[union-attr]
            data = search_result.data
            if data is None:
                continue

            line_numbers = data.line_numbers or []
            matched_lines = data.matches or []
            for line_number, line in zip(line_numbers, matched_lines):
                matches.append(
                    GrepMatch(
                        path=file_path,
                        line_number=line_number if isinstance(line_number, int) else 0,
                        line=truncate_line(line),
                    )
                )
                if len(matches) >= max_results:
                    truncated = True
                    return matches, truncated

        return matches, truncated

    def update_file(self, path: str, content: bytes) -> None:
        """用二进制内容覆盖容器内文件（base64 编码写）。"""
        with self._lock:
            try:
                base64_content = base64.b64encode(content).decode("utf-8")
                self._client.file.write_file(file=path, content=base64_content, encoding="base64")  # type: ignore[union-attr]
            except Exception as e:
                logger.error("Failed to update file in sandbox: %s", e)
                raise
