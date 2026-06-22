"""本地容器 backend：用 Docker / Apple Container 管沙箱容器生命周期。

在 macOS 上优先 Apple Container（``container`` CLI），没有则回退 Docker；其它平台用 Docker。
负责：确定性容器命名（跨进程发现）、线程安全端口分配、容器 start/stop（``--rm``）、
卷挂载 + 环境变量注入、健康检查、批量 ``docker inspect`` 枚举运行中容器。

关键设计：
- **确定性命名** ``{prefix}-{sandbox_id}``：任何进程都能凭 sandbox_id（= thread_id 哈希）推出
  同一容器名，从而 ``docker inspect`` 发现 / 复用别的进程起的容器。
- **端口冲突重试**：``get_free_port`` 的 socket bind 检查镜像 Docker 的 0.0.0.0 绑定，但 Docker
  释端口有微秒级异步，故 ``create`` 里若 Docker 报「port already allocated」就换下一个端口重试。
- **容器名冲突 → 发现**：若报「container name already in use」，说明另一进程已起同名容器，
  走 ``discover`` 收养而不是报错。
- **``list_running`` 用 2 次 subprocess**（``ps`` + 批量 ``inspect``），而非每容器 2N+1 次。
- **环境变量脱敏**：日志里 ``-e KEY=value`` 的 value 脱敏，防密钥泄露。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime

from deerflow.community.aio_sandbox.backend import SandboxBackend, wait_for_sandbox_ready
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.utils.network import get_free_port, release_port

logger = logging.getLogger(__name__)


def _parse_docker_timestamp(raw: str) -> float:
    """把 Docker 的 ISO 8601 纳秒时间戳解析成 Unix epoch float。

    Docker 返回纳秒精度 + 尾部 ``Z``（如 ``2026-04-08T01:22:50.123456789Z``）。
    ``fromisoformat`` 最多接微秒且（3.11 前）不接 ``Z``，故先归一再解析。
    空 / 解析失败返回 ``0.0``（调用方以此作「未知 age」哨兵）。
    """
    if not raw:
        return 0.0
    try:
        s = raw.strip()
        if "." in s:
            dot_pos = s.index(".")
            tz_start = dot_pos + 1
            while tz_start < len(s) and s[tz_start].isdigit():
                tz_start += 1
            frac = s[dot_pos + 1 : tz_start][:6]  # 截到微秒
            tz_suffix = s[tz_start:]
            s = s[: dot_pos + 1] + frac + tz_suffix
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError) as e:
        logger.debug("Could not parse docker timestamp %r: %s", raw, e)
        return 0.0


def _extract_host_port(inspect_entry: dict, container_port: int) -> int | None:
    """从 docker inspect 条目取映射到 ``container_port/tcp`` 的宿主端口。无映射返回 None。"""
    try:
        ports = (inspect_entry.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{container_port}/tcp") or []
        if bindings:
            host_port = bindings[0].get("HostPort")
            if host_port:
                return int(host_port)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _format_container_mount(runtime: str, host_path: str, container_path: str, read_only: bool) -> list[str]:
    """按 runtime 格式化一个 bind-mount 参数。

    Docker 用 ``--mount type=bind,...``（避免 Windows 盘符 ``D:/..`` 里 ``:`` 的歧义）；
    Apple Container 用 ``-v host:container[:ro]``。
    """
    if runtime == "docker":
        mount_spec = f"type=bind,src={host_path},dst={container_path}"
        if read_only:
            mount_spec += ",readonly"
        return ["--mount", mount_spec]

    mount_spec = f"{host_path}:{container_path}"
    if read_only:
        mount_spec += ":ro"
    return ["-v", mount_spec]


def _redact_container_command_for_log(cmd: list[str]) -> list[str]:
    """把 Docker 命令里的环境变量 value 脱敏（``-e KEY=value`` → ``-e KEY=<redacted>``）。"""
    redacted: list[str] = []
    redact_next_env = False

    for arg in cmd:
        if redact_next_env:
            if "=" in arg:
                key = arg.split("=", 1)[0]
                redacted.append(f"{key}=<redacted>" if key else "<redacted>")
            else:
                redacted.append(arg)
            redact_next_env = False
            continue

        if arg in {"-e", "--env"}:
            redacted.append(arg)
            redact_next_env = True
            continue

        if arg.startswith("--env="):
            value = arg.removeprefix("--env=")
            if "=" in value:
                key = value.split("=", 1)[0]
                redacted.append(f"--env={key}=<redacted>" if key else "--env=<redacted>")
            else:
                redacted.append(arg)
            continue

        redacted.append(arg)

    return redacted


def _format_container_command_for_log(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _normalize_sandbox_host(host: str) -> str:
    return host.strip().lower()


def _is_ipv6_loopback_sandbox_host(host: str) -> bool:
    return _normalize_sandbox_host(host) in {"::1", "[::1]"}


def _is_loopback_sandbox_host(host: str) -> bool:
    return _normalize_sandbox_host(host) in {"", "localhost", "127.0.0.1", "::1", "[::1]"}


def _resolve_docker_bind_host(sandbox_host: str | None = None, bind_host: str | None = None) -> str:
    """选 legacy Docker ``-p`` 端口发布绑定的宿主网卡。

    裸机/本地经 localhost 访问沙箱，不应把沙箱 HTTP API 暴露到所有网卡。
    DooD（Docker-outside-of-Docker）常从另一容器经 ``host.docker.internal`` 访问，保留其
    宽绑定除非用 ``DEER_FLOW_SANDBOX_BIND_HOST`` 收窄。IPv6 loopback 沙箱 host → 也绑 IPv6 loopback。
    """
    explicit_bind = bind_host if bind_host is not None else os.environ.get("DEER_FLOW_SANDBOX_BIND_HOST")
    if explicit_bind is not None:
        explicit_bind = explicit_bind.strip()
        if explicit_bind:
            logger.debug("Docker sandbox bind: %s (explicit bind host override)", explicit_bind)
            return explicit_bind

    host = sandbox_host if sandbox_host is not None else os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
    if _is_ipv6_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: [::1] (IPv6 loopback sandbox host)")
        return "[::1]"
    if _is_loopback_sandbox_host(host):
        logger.debug("Docker sandbox bind: 127.0.0.1 (loopback default)")
        return "127.0.0.1"

    logger.debug("Docker sandbox bind: 0.0.0.0 (non-loopback sandbox host compatibility)")
    return "0.0.0.0"


def _is_no_such_container_error(stderr: str, container_name: str) -> bool:
    """仅当 stderr 明确说容器不存在才返回 True。

    Docker 报 ``No such object`` / ``No such container``；Apple Container 报通用 ``not found``，
    故 ``not found`` 仅在消息同时含容器名 / container / object 时才信——瞬时故障（``command not found``
    等）文本里恰好含 ``not found`` 的不能误读成「容器已死」。
    """
    message = stderr.lower()
    if "no such object" in message or "no such container" in message:
        return True
    if "not found" not in message:
        return False
    return container_name.lower() in message or "container" in message or "object" in message


class LocalContainerBackend(SandboxBackend):
    """本机用 Docker / Apple Container 管沙箱容器的 backend。

    macOS 优先 Apple Container，否则回退 Docker；其它平台用 Docker。

    能力：确定性容器命名（跨进程发现）、线程安全端口分配、容器生命周期（``--rm`` start/stop）、
    卷挂载 + 环境变量、健康检查。
    """

    def __init__(
        self,
        *,
        image: str,
        base_port: int,
        container_prefix: str,
        config_mounts: list,
        environment: dict[str, str],
    ):
        """
        Args:
            image: 容器镜像。
            base_port: 起始搜索空闲端口的基准端口。
            container_prefix: 容器名前缀（如 ``deer-flow-sandbox``）。
            config_mounts: config 来的卷挂载配置（VolumeMountConfig 列表）。
            environment: 注入容器的环境变量。
        """
        self._image = image
        self._base_port = base_port
        self._container_prefix = container_prefix
        self._config_mounts = config_mounts
        self._environment = environment
        self._runtime = self._detect_runtime()

    @property
    def runtime(self) -> str:
        """检测到的容器 runtime（``docker`` 或 ``container``）。"""
        return self._runtime

    def _detect_runtime(self) -> str:
        """探测容器 runtime：macOS 优先 Apple Container，否则 Docker。"""
        import platform

        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["container", "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                logger.info("Detected Apple Container: %s", result.stdout.strip())
                return "container"
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.info("Apple Container not available, falling back to Docker")

        return "docker"

    # ── SandboxBackend interface ──────────────────────────────────────────

    def create(self, thread_id: str | None, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """起一个新容器并返回连接信息。

        端口冲突时换下一个端口重试（Docker 释端口有微秒级异步）；容器名冲突时走 ``discover``
        收养已存在的同名容器。
        """
        container_name = f"{self._container_prefix}-{sandbox_id}"

        _next_start = self._base_port
        container_id: str | None = None
        port: int = 0
        for _attempt in range(10):
            port = get_free_port(start_port=_next_start)
            try:
                container_id = self._start_container(container_name, port, extra_mounts)
                break
            except RuntimeError as exc:
                release_port(port)
                err = str(exc)
                err_lower = err.lower()
                # 端口已被 Docker 占（释端口异步）→ 换下一个重试。
                if "port is already allocated" in err or "address already in use" in err_lower:
                    logger.warning("Port %s rejected by Docker (already allocated), retrying with next port", port)
                    _next_start = port + 1
                    continue
                # 容器名冲突 → 另一进程已起同名容器，发现并收养。
                if "is already in use by container" in err_lower or "conflict. the container name" in err_lower:
                    logger.warning("Container name %s already in use, attempting to discover existing sandbox instance", container_name)
                    existing = self.discover(sandbox_id)
                    if existing is not None:
                        return existing
                raise
        else:
            raise RuntimeError("Could not start sandbox container: all candidate ports are already allocated by Docker")

        # DooD 时沙箱容器经 host.docker.internal 可达（它们跑在宿主 daemon 上）。
        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=f"http://{sandbox_host}:{port}",
            container_name=container_name,
            container_id=container_id,
        )

    def destroy(self, info: SandboxInfo) -> None:
        """停容器并释放端口。优先 container_id，回退 container_name。"""
        stop_target = info.container_id or info.container_name
        if stop_target:
            self._stop_container(stop_target)
        # 从 sandbox_url 抽端口释放。
        try:
            from urllib.parse import urlparse

            port = urlparse(info.sandbox_url).port
            if port:
                release_port(port)
        except Exception:
            pass

    def is_alive(self, info: SandboxInfo) -> bool:
        """容器是否还在跑（轻量 container inspect，不打 HTTP）。"""
        if info.container_name:
            return self._is_container_running(info.container_name)
        return False

    def discover(self, info_or_sandbox_id, *args, **kwargs):
        """按确定性名发现已存在的容器。

        可按 ``sandbox_id``（str）发现，也兼容旧签名 ``discover(sandbox_id)``。
        检查同名容器是否在跑、取端口、健康检查通过才返回。
        """
        # 统一成 sandbox_id: str 调用（基类签名）。
        if isinstance(info_or_sandbox_id, str):
            sandbox_id = info_or_sandbox_id
        else:
            sandbox_id = info_or_sandbox_id.sandbox_id

        container_name = f"{self._container_prefix}-{sandbox_id}"

        try:
            running = self._is_container_running(container_name)
        except RuntimeError as e:
            logger.warning("Could not verify container %s during discovery; not adopting it: %s", container_name, e)
            return None

        if not running:
            return None

        port = self._get_container_port(container_name)
        if port is None:
            return None

        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        sandbox_url = f"http://{sandbox_host}:{port}"
        if not wait_for_sandbox_ready(sandbox_url, timeout=5):
            return None

        return SandboxInfo(
            sandbox_id=sandbox_id,
            sandbox_url=sandbox_url,
            container_name=container_name,
        )

    def list_running(self) -> list[SandboxInfo]:
        """枚举所有匹配前缀的运行中容器（启动 reconcile 用）。

        单次 ``docker ps`` 列名 + 单次批量 ``docker inspect`` 取创建时间与端口映射，
        共 2 次 subprocess（取代朴素的 2N+1 次）。``--filter name=`` 是子串匹配，故二次
        ``startswith`` 精确过滤前缀。无端口映射的容器也纳入（sandbox_url 空），让 reconcile
        不管端口状态都能收养孤儿。
        """
        # Step 1: docker ps 枚举容器名。
        try:
            result = subprocess.run(
                [
                    self._runtime,
                    "ps",
                    "--filter",
                    f"name={self._container_prefix}-",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                logger.warning(
                    "Failed to list running containers with %s ps (returncode=%s, stderr=%s)",
                    self._runtime,
                    result.returncode,
                    stderr or "<empty>",
                )
                return []
            if not result.stdout.strip():
                return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Failed to list running containers: %s", e)
            return []

        # docker filter 是子串匹配，二次 startswith 精确过滤前缀。
        container_names = [name.strip() for name in result.stdout.strip().splitlines() if name.strip().startswith(self._container_prefix + "-")]
        if not container_names:
            return []

        # Step 2: 批量 docker inspect（单次 subprocess）。
        inspections = self._batch_inspect(container_names)

        infos: list[SandboxInfo] = []
        sandbox_host = os.environ.get("DEER_FLOW_SANDBOX_HOST", "localhost")
        for container_name in container_names:
            data = inspections.get(container_name)
            if data is None:
                # ps 与 inspect 间容器消失了，或 inspect 失败。
                continue
            created_at, host_port = data
            sandbox_id = container_name[len(self._container_prefix) + 1 :]
            sandbox_url = f"http://{sandbox_host}:{host_port}" if host_port else ""

            infos.append(
                SandboxInfo(
                    sandbox_id=sandbox_id,
                    sandbox_url=sandbox_url,
                    container_name=container_name,
                    created_at=created_at,
                )
            )

        logger.info("Found %d running sandbox container(s)", len(infos))
        return infos

    def _batch_inspect(self, container_names: list[str]) -> dict[str, tuple[float, int | None]]:
        """单次 subprocess 批量 inspect，返回 ``{container_name: (created_at, host_port)}``。

        缺失 / 解析失败的容器静默丢弃。
        """
        if not container_names:
            return {}
        try:
            result = subprocess.run(
                [self._runtime, "inspect", *container_names],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Failed to batch-inspect containers: %s", e)
            return {}

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning(
                "Failed to batch-inspect containers with %s inspect (returncode=%s, stderr=%s)",
                self._runtime,
                result.returncode,
                stderr or "<empty>",
            )
            return {}

        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse docker inspect output as JSON: %s", e)
            return {}

        out: dict[str, tuple[float, int | None]] = {}
        for entry in payload:
            # ``Name`` 在 docker inspect 响应里带前缀 ``/``。
            name = (entry.get("Name") or "").lstrip("/")
            if not name:
                continue
            created_at = _parse_docker_timestamp(entry.get("Created", ""))
            host_port = _extract_host_port(entry, 8080)
            out[name] = (created_at, host_port)
        return out

    # ── 容器操作 ─────────────────────────────────────────────────────────

    def _start_container(
        self,
        container_name: str,
        port: int,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> str:
        """起一个新容器，返回容器 id。失败 raise RuntimeError。"""
        cmd = [self._runtime, "run"]

        # Docker 专属安全选项。
        if self._runtime == "docker":
            cmd.extend(["--security-opt", "seccomp=unconfined"])

        if self._runtime == "docker":
            port_mapping = f"{_resolve_docker_bind_host()}:{port}:8080"
        else:
            port_mapping = f"{port}:8080"

        cmd.extend(
            [
                "--rm",
                "-d",
                "-p",
                port_mapping,
                "--name",
                container_name,
            ]
        )

        # 环境变量。
        for key, value in self._environment.items():
            cmd.extend(["-e", f"{key}={value}"])

        # config 级卷挂载。
        for mount in self._config_mounts:
            cmd.extend(
                _format_container_mount(
                    self._runtime,
                    mount.host_path,
                    mount.container_path,
                    mount.read_only,
                )
            )

        # 额外挂载（线程级、skills 等）。
        if extra_mounts:
            for host_path, container_path, read_only in extra_mounts:
                cmd.extend(
                    _format_container_mount(
                        self._runtime,
                        host_path,
                        container_path,
                        read_only,
                    )
                )

        cmd.append(self._image)

        log_cmd = _format_container_command_for_log(_redact_container_command_for_log(cmd))
        logger.info("Starting container using %s: %s", self._runtime, log_cmd)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            container_id = result.stdout.strip()
            logger.info("Started container %s (ID: %s) using %s", container_name, container_id, self._runtime)
            return container_id
        except subprocess.CalledProcessError as e:
            logger.error("Failed to start container using %s: %s", self._runtime, e.stderr)
            raise RuntimeError(f"Failed to start sandbox container: {e.stderr}")

    def _stop_container(self, container_id: str) -> None:
        """停容器（``--rm`` 确保自动移除）。"""
        try:
            subprocess.run(
                [self._runtime, "stop", container_id],
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("Stopped container %s using %s", container_id, self._runtime)
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to stop container %s: %s", container_id, e.stderr)

    def _is_container_running(self, container_name: str) -> bool:
        """命名容器是否在跑（跨进程容器发现的基础）。

        Raises:
            RuntimeError: runtime 答不上 inspect 查询。故意区分「答不上」与「明确不存在」，
                免得瞬时 Docker/Container daemon 故障时误杀健康容器。
        """
        try:
            result = subprocess.run(
                [self._runtime, "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Timed out checking container {container_name}") from exc

        if result.returncode == 0:
            return result.stdout.strip().lower() == "true"
        if _is_no_such_container_error(result.stderr, container_name):
            return False
        raise RuntimeError(f"Failed to inspect container {container_name}: {result.stderr.strip()}")

    def _get_container_port(self, container_name: str) -> int | None:
        """取运行中容器的宿主端口（映射到容器 8080）。找不到返回 None。"""
        try:
            result = subprocess.run(
                [self._runtime, "port", container_name, "8080"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # 输出形如 ``0.0.0.0:PORT`` 或 ``:::PORT``。
                port_str = result.stdout.strip().split(":")[-1]
                return int(port_str)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None
