"""``AioSandboxProvider`` —— 用可插拔 backend 编排沙箱生命周期。

provider 组合一个 ``SandboxBackend``（怎么供给沙箱：本地容器 vs 远端 K8s），自己管：

- **进程内缓存**：同 thread 重复访问秒级命中。
- **暖池（warm pool）**：release 的沙箱容器**还跑着**，下次同 thread 复用免冷启动；超 ``replicas``
  软上限时淘汰暖池里最老的腾位。
- **跨进程发现**：sandbox_id 由 thread_id 哈希派生（确定性），任何进程都能推出同一容器名 / id，
  经 backend ``discover`` 复用别的进程起的容器；建容器前用 ``{thread_dir}/{sandbox_id}.lock``
  文件锁串行化跨进程竞争（防容器名冲突）。
- **idle 超时回收**：后台线程定期查 ``_last_activity`` / 暖池释放时间，超 ``idle_timeout`` 销毁。
- **启动收养孤儿**：进程重启时 ``list_running`` 枚举本 backend 的运行中容器，全收养进暖池，让
  idle 检查器决定（避免进程崩溃后容器永远跑着）。
- **优雅关闭**：注册 ``SIGTERM`` / ``SIGINT`` / ``SIGHUP`` + ``atexit``，关时先停 idle 检查线程，
  再逐个 destroy 活跃 + 暖池沙箱（红线 #33）。

缓存层级（acquire 依次试）：① 进程内活跃缓存 → ② 暖池复用 → ③ 跨进程文件锁内 backend discover →
④ 都没有才 create。

soft-load：``agent_sandbox`` 缺包时 ``AioSandbox.__init__`` 抛 ImportError；本 provider 在
``_create_backend`` / ``acquire`` 时才会触发，故模块导入本身不炸。``is_host_bash_allowed`` 对
非 local provider 返回 True（AIO 有真隔离，host bash 放行）。
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import logging
import os
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    import msvcrt

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.community.aio_sandbox.backend import SandboxBackend, wait_for_sandbox_ready, wait_for_sandbox_ready_async
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend
from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.local.local_sandbox import ensure_thread_dirs
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_IMAGE = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
DEFAULT_PORT = 8080
DEFAULT_CONTAINER_PREFIX = "deer-flow-sandbox"
DEFAULT_IDLE_TIMEOUT = 600  # 10 分钟
DEFAULT_REPLICAS = 3  # 最大并发沙箱容器
IDLE_CHECK_INTERVAL = 60  # 每 60s 查一次
THREAD_LOCK_EXECUTOR_WORKERS = min(32, (os.cpu_count() or 1) + 4)
_THREAD_LOCK_EXECUTOR = ThreadPoolExecutor(max_workers=THREAD_LOCK_EXECUTOR_WORKERS, thread_name_prefix="sandbox-lock-wait")
atexit.register(_THREAD_LOCK_EXECUTOR.shutdown, wait=False, cancel_futures=True)


def _lock_file_exclusive(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(lock_file) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return

    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _open_lock_file(lock_path):
    return open(lock_path, "a", encoding="utf-8")


async def _acquire_thread_lock_async(lock: threading.Lock) -> None:
    """在不轮询、不用默认 executor 的前提下 await 获取一把 threading.Lock。"""
    loop = asyncio.get_running_loop()
    acquire_future = loop.run_in_executor(_THREAD_LOCK_EXECUTOR, lock.acquire, True)

    try:
        acquired = await asyncio.shield(acquire_future)
    except asyncio.CancelledError:
        acquire_future.add_done_callback(lambda task: _release_cancelled_lock_acquire(lock, task))
        raise

    if not acquired:
        raise RuntimeError("Failed to acquire sandbox thread lock")


def _release_cancelled_lock_acquire(lock: threading.Lock, task: asyncio.Future[bool]) -> None:
    """释放一把在其 await 协程被取消后才真正拿到手的锁。"""
    if task.cancelled():
        return

    try:
        acquired = task.result()
    except Exception as e:
        logger.warning("Cancelled sandbox lock acquisition finished with error: %s", e)
        return

    if acquired:
        lock.release()


class AioSandboxProvider(SandboxProvider):
    """管 AIO 沙箱容器的 provider。

    组合一个 ``SandboxBackend``（怎么供给）：本地 Docker/Apple Container 模式（自起容器）或
    远端/K8s 模式（连预置 URL）。

    config.yaml 的 ``sandbox`` 段::

        use: deerflow.community.aio_sandbox:AioSandboxProvider
        image: <容器镜像>
        port: 8080                      # 本地容器基准端口
        container_prefix: deer-flow-sandbox
        idle_timeout: 600               # 空闲秒数（0 禁用）
        replicas: 3                     # 最大并发容器（超限 LRU 淘汰暖池）
        provisioner_url: http://provisioner:8002  # 设了用远端 K8s backend
        mounts:                         # 本地容器卷挂载
          - host_path: /path/on/host
            container_path: /path/in/container
            read_only: false
        environment:                    # 容器环境变量
          NODE_ENV: production
          API_KEY: $MY_API_KEY
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sandboxes: dict[str, AioSandbox] = {}  # sandbox_id -> AioSandbox 实例
        self._sandbox_infos: dict[str, SandboxInfo] = {}  # sandbox_id -> SandboxInfo（destroy 用）
        self._thread_sandboxes: dict[str, str] = {}  # thread_id -> sandbox_id
        self._thread_locks: dict[str, threading.Lock] = {}  # thread_id -> 进程内锁
        self._last_activity: dict[str, float] = {}  # sandbox_id -> 最后活动时间戳
        # 暖池：release 的沙箱，容器还跑着。sandbox_id -> (SandboxInfo, release_timestamp)。
        # 可快速回收（免冷启动），或在 replicas 容量耗尽时销毁腾位。
        self._warm_pool: dict[str, tuple[SandboxInfo, float]] = {}
        self._shutdown_called = False
        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

        self._config = self._load_config()
        self._backend: SandboxBackend = self._create_backend()

        # 注册关闭处理
        atexit.register(self.shutdown)
        self._register_signal_handlers()

        # reconcile 上一进程生命周期遗留的孤儿容器
        self._reconcile_orphans()

        # 启用 idle 检查器
        if self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT) > 0:
            self._start_idle_checker()

    @property
    def uses_thread_data_mounts(self) -> bool:
        """是否经挂载让 thread 的 workspace/uploads/outputs 可见。

        本地容器 backend bind-mount 线程数据目录，故 gateway 写的文件沙箱启动时已可见。
        远端 backend 可能需显式文件同步。
        """
        return isinstance(self._backend, LocalContainerBackend)

    @property
    def needs_upload_permission_adjustment(self) -> bool:
        """容器模式需调上传目录权限；本地 bind-mount 不需要。"""
        return isinstance(self._backend, LocalContainerBackend)

    # ── Factory ──────────────────────────────────────────────────────────

    def _create_backend(self) -> SandboxBackend:
        """按配置选 backend：设了 ``provisioner_url`` → 远端（K8s）；否则本地 Docker/Apple。"""
        provisioner_url = self._config.get("provisioner_url")
        if provisioner_url:
            logger.info("Using remote sandbox backend with provisioner at %s", provisioner_url)
            return RemoteSandboxBackend(provisioner_url=provisioner_url)

        logger.info("Using local container sandbox backend")
        return LocalContainerBackend(
            image=self._config["image"],
            base_port=self._config["port"],
            container_prefix=self._config["container_prefix"],
            config_mounts=self._config["mounts"],
            environment=self._config["environment"],
        )

    # ── 配置 ──────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """从 app config 读沙箱配置。"""
        config = get_app_config()
        sandbox_config = config.sandbox

        idle_timeout = getattr(sandbox_config, "idle_timeout", None)
        replicas = getattr(sandbox_config, "replicas", None)

        return {
            "image": sandbox_config.image or DEFAULT_IMAGE,
            "port": sandbox_config.port or DEFAULT_PORT,
            "container_prefix": sandbox_config.container_prefix or DEFAULT_CONTAINER_PREFIX,
            "idle_timeout": idle_timeout if idle_timeout is not None else DEFAULT_IDLE_TIMEOUT,
            "replicas": replicas if replicas is not None else DEFAULT_REPLICAS,
            "mounts": sandbox_config.mounts or [],
            "environment": self._resolve_env_vars(sandbox_config.environment or {}),
            # provisioner URL（动态 Pod 管理，如 http://provisioner:8002）
            "provisioner_url": getattr(sandbox_config, "provisioner_url", None) or "",
        }

    @staticmethod
    def _resolve_env_vars(env_config: dict[str, str]) -> dict[str, str]:
        """解析环境变量引用（$ 开头的值从宿主环境取）。"""
        resolved = {}
        for key, value in env_config.items():
            if isinstance(value, str) and value.startswith("$"):
                env_name = value[1:]
                resolved[key] = os.environ.get(env_name, "")
            else:
                resolved[key] = str(value)
        return resolved

    # ── 启动 reconcile ────────────────────────────────────────────────────

    def _reconcile_orphans(self) -> None:
        """收养上一进程生命周期遗留的孤儿容器。

        启动时枚举所有匹配前缀的运行中容器，全收养进暖池。idle 检查器会回收没人再 acquire
        的容器。无条件全收养是因为光凭 age 分不清「孤儿」与「另一进程正在用」——``idle_timeout``
        表「不活跃」，非「uptime」。收养进暖池让 idle 检查器决定，避免误毁并发进程正用的容器。

        这填补了「进程内状态丢失（重启 / 崩溃 / SIGKILL）→ Docker 容器永远跑着」的根本缺口。
        """
        try:
            running = self._backend.list_running()
        except Exception as e:
            logger.warning("Failed to enumerate running containers during startup reconciliation: %s", e)
            return

        if not running:
            return

        current_time = time.time()
        adopted = 0

        for info in running:
            age = current_time - info.created_at if info.created_at > 0 else float("inf")
            # 每容器单次锁获取：原子 check-and-insert，避免「已跟踪？」检查与暖池插入间的 TOCTOU。
            with self._lock:
                if info.sandbox_id in self._sandboxes or info.sandbox_id in self._warm_pool:
                    continue
                self._warm_pool[info.sandbox_id] = (info, current_time)
            adopted += 1
            logger.info("Adopted container %s into warm pool (age: %.0fs)", info.sandbox_id, age)

        logger.info("Startup reconciliation complete: %d adopted into warm pool, %d total found", adopted, len(running))

    # ── 确定性 id ─────────────────────────────────────────────────────────

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str) -> str:
        """由 thread_id 生成确定性 sandbox_id。

        保证所有进程对同一 thread_id 派生出同一 sandbox_id，从而无需共享状态文件即可跨进程发现。
        """
        return hashlib.sha256(thread_id.encode()).hexdigest()[:8]

    # ── 挂载 helper ───────────────────────────────────────────────────────

    def _get_extra_mounts(self, thread_id: str | None) -> list[tuple[str, str, bool]]:
        """收集沙箱的所有额外挂载（线程级 + skills）。"""
        mounts: list[tuple[str, str, bool]] = []

        if thread_id:
            mounts.extend(self._get_thread_mounts(thread_id))
            logger.info("Adding thread mounts for thread %s: %s", thread_id, mounts)

        skills_mount = self._get_skills_mount()
        if skills_mount:
            mounts.append(skills_mount)
            logger.info("Adding skills mount: %s", skills_mount)

        return mounts

    @staticmethod
    def _get_thread_mounts(thread_id: str) -> list[tuple[str, str, bool]]:
        """取线程数据目录的卷挂载（复用 M10 local_sandbox.ensure_thread_dirs 建目录）。

        挂载源用宿主路径，让 DooD（容器内 gateway + 宿主 Docker socket）时宿主 Docker daemon
        能解析路径。acp-workspace 只读（lead agent 读结果；ACP 子进程从宿主侧写，不从容器内写）。
        """
        user_id = get_effective_user_id()
        root = ensure_thread_dirs(thread_id, user_id=user_id)  # 建 workspace/uploads/outputs
        acp_dir = root.parent / "acp-workspace"
        acp_dir.mkdir(parents=True, exist_ok=True)

        return [
            (str(root / "workspace"), f"{VIRTUAL_PATH_PREFIX}/workspace", False),
            (str(root / "uploads"), f"{VIRTUAL_PATH_PREFIX}/uploads", False),
            (str(root / "outputs"), f"{VIRTUAL_PATH_PREFIX}/outputs", False),
            # ACP workspace：容器内只读。
            (str(acp_dir), "/mnt/acp-workspace", True),
        ]

    @staticmethod
    def _get_skills_mount() -> tuple[str, str, bool] | None:
        """取 skills 目录挂载（只读）。DooD 时用宿主侧 skills 路径。"""
        try:
            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path

            if skills_path.exists():
                # DooD 时用宿主侧 skills 路径，让宿主 Docker daemon 能解析。
                host_skills = os.environ.get("DEER_FLOW_HOST_SKILLS_PATH") or str(skills_path)
                return (host_skills, container_path, True)  # 只读，安全
        except Exception as e:
            logger.warning("Could not setup skills mount: %s", e)
        return None

    # ── idle 超时管理 ─────────────────────────────────────────────────────

    def _start_idle_checker(self) -> None:
        """启动查空闲沙箱的后台线程。"""
        self._idle_checker_thread = threading.Thread(
            target=self._idle_checker_loop,
            name="sandbox-idle-checker",
            daemon=True,
        )
        self._idle_checker_thread.start()
        logger.info("Started idle checker thread (timeout: %ss)", self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))

    def _idle_checker_loop(self) -> None:
        idle_timeout = self._config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)
        while not self._idle_checker_stop.wait(timeout=IDLE_CHECK_INTERVAL):
            try:
                self._cleanup_idle_sandboxes(idle_timeout)
            except Exception as e:
                logger.error("Error in idle checker loop: %s", e)

    def _cleanup_idle_sandboxes(self, idle_timeout: float) -> None:
        current_time = time.time()
        active_to_destroy = []
        warm_to_destroy: list[tuple[str, SandboxInfo]] = []

        with self._lock:
            # 活跃沙箱：按 _last_activity 跟踪。
            for sandbox_id, last_activity in self._last_activity.items():
                idle_duration = current_time - last_activity
                if idle_duration > idle_timeout:
                    active_to_destroy.append(sandbox_id)
                    logger.info("Sandbox %s idle for %.1fs, marking for destroy", sandbox_id, idle_duration)

            # 暖池：按 _warm_pool 里的 release_timestamp 跟踪。
            for sandbox_id, (info, release_ts) in list(self._warm_pool.items()):
                warm_duration = current_time - release_ts
                if warm_duration > idle_timeout:
                    warm_to_destroy.append((sandbox_id, info))
                    del self._warm_pool[sandbox_id]
                    logger.info("Warm-pool sandbox %s idle for %.1fs, marking for destroy", sandbox_id, warm_duration)

        # 销毁活跃沙箱（动手前在锁内再验一次仍空闲）。
        for sandbox_id in active_to_destroy:
            try:
                # 锁内再验：快照到现在期间，沙箱可能已被 re-acquire（last_activity 更新）或已 release/destroy。
                with self._lock:
                    last_activity = self._last_activity.get(sandbox_id)
                    if last_activity is None:
                        logger.info("Sandbox %s already gone before idle destroy, skipping", sandbox_id)
                        continue
                    if (time.time() - last_activity) < idle_timeout:
                        logger.info("Sandbox %s was re-acquired before idle destroy, skipping", sandbox_id)
                        continue
                logger.info("Destroying idle sandbox %s", sandbox_id)
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error("Failed to destroy idle sandbox %s: %s", sandbox_id, e)

        # 销毁暖池沙箱（上面已在锁内从 _warm_pool 移除）。
        for sandbox_id, info in warm_to_destroy:
            try:
                self._backend.destroy(info)
                logger.info("Destroyed idle warm-pool sandbox %s", sandbox_id)
            except Exception as e:
                logger.error("Failed to destroy idle warm-pool sandbox %s: %s", sandbox_id, e)

    # ── 信号处理 ──────────────────────────────────────────────────────────

    def _register_signal_handlers(self) -> None:
        """注册优雅关闭的信号处理器（SIGTERM / SIGINT / SIGHUP）。

        确保用户关终端（SIGHUP）时沙箱容器也被清掉。
        """
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None

        def signal_handler(signum, frame):
            self.shutdown()
            if signum == signal.SIGTERM:
                original = self._original_sigterm
            elif hasattr(signal, "SIGHUP") and signum == signal.SIGHUP:
                original = self._original_sighup
            else:
                original = self._original_sigint
            if callable(original):
                original(signum, frame)
            elif original == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, signal_handler)
        except ValueError:
            logger.debug("Could not register signal handlers (not main thread)")

    # ── 线程锁（进程内）──────────────────────────────────────────────────

    def _get_thread_lock(self, thread_id: str) -> threading.Lock:
        """取 / 建某 thread_id 的进程内锁。"""
        with self._lock:
            if thread_id not in self._thread_locks:
                self._thread_locks[thread_id] = threading.Lock()
            return self._thread_locks[thread_id]

    def _sandbox_id_for_thread(self, thread_id: str | None) -> str:
        """有 thread_id 用确定性 id，否则随机 id。"""
        return self._deterministic_sandbox_id(thread_id) if thread_id else str(uuid.uuid4())[:8]

    def _reuse_in_process_sandbox(self, thread_id: str | None, *, post_lock: bool = False) -> str | None:
        """若线程仍有一个被跟踪的活跃进程内沙箱，则复用它。"""
        if thread_id is None:
            return None

        with self._lock:
            if thread_id not in self._thread_sandboxes:
                return None

            existing_id = self._thread_sandboxes[thread_id]
            if existing_id in self._sandboxes:
                info = self._sandbox_infos.get(existing_id)
            else:
                del self._thread_sandboxes[thread_id]
                return None

        alive = self._check_tracked_sandbox_alive(existing_id, info) if info is not None else True
        if alive is False:
            self._drop_unhealthy_sandbox(
                existing_id,
                "in-process cache failed health check",
                expected_info=info,
            )
            return None

        with self._lock:
            if self._thread_sandboxes.get(thread_id) != existing_id:
                return None
            if existing_id not in self._sandboxes:
                self._thread_sandboxes.pop(thread_id, None)
                return None

            suffix = " (post-lock check)" if post_lock else ""
            logger.info("Reusing in-process sandbox %s for thread %s%s", existing_id, thread_id, suffix)
            self._last_activity[existing_id] = time.time()
            return existing_id

    def _reclaim_warm_pool_sandbox(self, thread_id: str | None, sandbox_id: str, *, post_lock: bool = False) -> str | None:
        """若暖池里有该沙箱，提升回活跃跟踪。"""
        if thread_id is None:
            return None

        with self._lock:
            if sandbox_id not in self._warm_pool:
                return None

            info, _ = self._warm_pool[sandbox_id]

        alive = self._check_tracked_sandbox_alive(sandbox_id, info)
        if alive is False:
            self._drop_unhealthy_sandbox(
                sandbox_id,
                "warm-pool cache failed health check",
                expected_info=info,
            )
            return None

        with self._lock:
            warm_item = self._warm_pool.pop(sandbox_id, None)
            if warm_item is None:
                return None
            info, _ = warm_item
            sandbox = AioSandbox(id=sandbox_id, base_url=info.sandbox_url)
            self._sandboxes[sandbox_id] = sandbox
            self._sandbox_infos[sandbox_id] = info
            self._last_activity[sandbox_id] = time.time()
            self._thread_sandboxes[thread_id] = sandbox_id

        suffix = " (post-lock check)" if post_lock else f" at {info.sandbox_url}"
        logger.info("Reclaimed warm-pool sandbox %s for thread %s%s", sandbox_id, thread_id, suffix)
        return sandbox_id

    def _recheck_cached_sandbox(self, thread_id: str, sandbox_id: str) -> str | None:
        """拿到跨进程文件锁后再查一次进程内缓存。"""
        return self._reuse_in_process_sandbox(thread_id, post_lock=True) or self._reclaim_warm_pool_sandbox(thread_id, sandbox_id, post_lock=True)

    def _register_discovered_sandbox(self, thread_id: str, info: SandboxInfo) -> str:
        """跟踪一个经 backend 发现的沙箱。"""
        sandbox = AioSandbox(id=info.sandbox_id, base_url=info.sandbox_url)
        with self._lock:
            self._sandboxes[info.sandbox_id] = sandbox
            self._sandbox_infos[info.sandbox_id] = info
            self._last_activity[info.sandbox_id] = time.time()
            self._thread_sandboxes[thread_id] = info.sandbox_id

        logger.info("Discovered existing sandbox %s for thread %s at %s", info.sandbox_id, thread_id, info.sandbox_url)
        return info.sandbox_id

    def _register_created_sandbox(self, thread_id: str | None, sandbox_id: str, info: SandboxInfo) -> str:
        """把新建的沙箱纳入活跃 map。"""
        sandbox = AioSandbox(id=sandbox_id, base_url=info.sandbox_url)
        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
            self._sandbox_infos[sandbox_id] = info
            self._last_activity[sandbox_id] = time.time()
            if thread_id:
                self._thread_sandboxes[thread_id] = sandbox_id

        logger.info("Created sandbox %s for thread %s at %s", sandbox_id, thread_id, info.sandbox_url)
        return sandbox_id

    def _check_tracked_sandbox_alive(self, sandbox_id: str, info: SandboxInfo) -> bool | None:
        """返回被跟踪沙箱是否看着活着，无法判定时 None。"""
        try:
            return self._backend.is_alive(info)
        except Exception as e:
            logger.warning("Failed to check sandbox %s health: %s", sandbox_id, e)
            return None

    def _remove_tracked_sandbox(
        self,
        sandbox_id: str,
        *,
        expected_info: SandboxInfo | None = None,
    ) -> tuple[Sandbox | None, SandboxInfo | None, bool]:
        """从进程内跟踪 map 移除一个沙箱。

        传了 ``expected_info`` 时，仅当当前跟踪的活跃 / 暖池条目正是被检查的那个 info 对象才移除——
        防陈旧健康检查结果删掉一个刚重建的同 id 沙箱。
        """
        thread_ids_to_remove: list[str] = []

        with self._lock:
            active_info = self._sandbox_infos.get(sandbox_id)
            warm_item = self._warm_pool.get(sandbox_id)
            warm_info = warm_item[0] if warm_item is not None else None
            if expected_info is not None and active_info is not expected_info and warm_info is not expected_info:
                return None, None, False

            sandbox = self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            if info is None and sandbox_id in self._warm_pool:
                info, _ = self._warm_pool.pop(sandbox_id)
            else:
                self._warm_pool.pop(sandbox_id, None)

        return sandbox, info, True

    def _drop_unhealthy_sandbox(self, sandbox_id: str, reason: str, *, expected_info: SandboxInfo | None = None) -> None:
        """健康检查明确失败后移除并销毁沙箱。"""
        sandbox, info, removed = self._remove_tracked_sandbox(sandbox_id, expected_info=expected_info)
        if not removed:
            logger.info("Skipped dropping sandbox %s: tracked info changed after health check", sandbox_id)
            return

        if sandbox is not None:
            try:
                sandbox.close()
            except Exception as e:
                logger.warning("Error closing unhealthy sandbox %s: %s", sandbox_id, e)

        if info is not None:
            try:
                self._backend.destroy(info)
            except Exception as e:
                logger.warning("Error destroying unhealthy sandbox %s: %s", sandbox_id, e)

        logger.warning("Dropped unhealthy sandbox %s: %s", sandbox_id, reason)

    def _replica_count(self) -> tuple[int, int]:
        """返回配置的 replicas 与当前跟踪的沙箱总数。"""
        replicas = self._config.get("replicas", DEFAULT_REPLICAS)
        with self._lock:
            total = len(self._sandboxes) + len(self._warm_pool)
        return replicas, total

    def _log_replicas_soft_cap(self, replicas: int, sandbox_id: str, evicted: str | None) -> None:
        """记暖池 replica 预算执行结果。"""
        if evicted:
            logger.info("Evicted warm-pool sandbox %s to stay within replicas=%s", evicted, replicas)
            return

        # 所有槽都被活跃沙箱占——照建并记日志。replicas 是软上限，不强停正在服务的容器。
        logger.warning("All %s replica slots are in active use; creating sandbox %s beyond the soft limit", replicas, sandbox_id)

    # ── 核心：acquire / get / release / shutdown ─────────────────────────

    def acquire(self, thread_id: str | None = None) -> str:
        """获取一个沙箱环境，返回其 id。

        同一 thread_id 跨多轮、多进程（共享存储时跨 pod）返回同一 sandbox_id。
        进程内 + 跨进程双重锁保证线程安全。
        """
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id)
            with thread_lock:
                return self._acquire_internal(thread_id)
        else:
            return self._acquire_internal(thread_id)

    async def acquire_async(self, thread_id: str | None = None) -> str:
        """获取沙箱但不卡事件循环。

        镜像 ``acquire()``，阻塞的 backend 操作跑线程外，新建沙箱用 async 就绪轮询。
        """
        if thread_id:
            thread_lock = self._get_thread_lock(thread_id)
            await _acquire_thread_lock_async(thread_lock)
            try:
                return await self._acquire_internal_async(thread_id)
            finally:
                thread_lock.release()

        return await self._acquire_internal_async(thread_id)

    def _acquire_internal(self, thread_id: str | None) -> str:
        """两层一致性的内部获取。

        Layer 1：进程内缓存（最快，覆盖同进程重复访问）。
        Layer 2：backend 发现（覆盖别的进程起的容器；sandbox_id 由 thread_id 确定性派生，
                 无需共享状态文件——任何进程都能推出同一容器名）。
        """
        cached_id = self._reuse_in_process_sandbox(thread_id)
        if cached_id is not None:
            return cached_id

        # thread 专属用确定性 id，匿名用随机。
        sandbox_id = self._sandbox_id_for_thread(thread_id)

        # ── Layer 1.5：暖池（容器还跑着，免冷启动）──
        reclaimed_id = self._reclaim_warm_pool_sandbox(thread_id, sandbox_id)
        if reclaimed_id is not None:
            return reclaimed_id

        # ── Layer 2：backend 发现 + create（跨进程文件锁保护）──
        # 文件锁让两个进程竞相为同一 thread_id 建沙箱时在此串行：后到的进程会发现先到进程起的
        # 容器，而非撞容器名冲突。
        if thread_id:
            return self._discover_or_create_with_lock(thread_id, sandbox_id)

        return self._create_sandbox(thread_id, sandbox_id)

    async def _acquire_internal_async(self, thread_id: str | None) -> str:
        """``_acquire_internal`` 的 async 对应。"""
        cached_id = await asyncio.to_thread(self._reuse_in_process_sandbox, thread_id)
        if cached_id is not None:
            return cached_id

        sandbox_id = self._sandbox_id_for_thread(thread_id)

        reclaimed_id = await asyncio.to_thread(self._reclaim_warm_pool_sandbox, thread_id, sandbox_id)
        if reclaimed_id is not None:
            return reclaimed_id

        if thread_id:
            return await self._discover_or_create_with_lock_async(thread_id, sandbox_id)

        return await self._create_sandbox_async(thread_id, sandbox_id)

    def _thread_lock_dir(self, thread_id: str) -> str:
        """跨进程文件锁所在目录 = 线程目录（user-data 的父级）。"""
        user_id = get_effective_user_id()
        root = ensure_thread_dirs(thread_id, user_id=user_id)
        return str(root.parent)

    def _discover_or_create_with_lock(self, thread_id: str, sandbox_id: str) -> str:
        """跨进程文件锁下发现已有沙箱或建新沙箱。

        文件锁串行化多进程对同一 thread_id 的并发建沙箱，防容器名冲突。
        """
        # ensure_thread_dirs 已在 _thread_lock_dir 内调过（建目录 + 返回路径）。
        lock_dir = self._thread_lock_dir(thread_id)
        lock_path = os.path.join(lock_dir, f"{sandbox_id}.lock")

        with open(lock_path, "a", encoding="utf-8") as lock_file:
            locked = False
            try:
                _lock_file_exclusive(lock_file)
                locked = True
                # 文件锁内再查进程内缓存：等锁期间本进程另一线程可能已赢了竞争。
                cached_id = self._recheck_cached_sandbox(thread_id, sandbox_id)
                if cached_id is not None:
                    return cached_id

                # backend 发现：别的进程可能已建容器。
                discovered = self._backend.discover(sandbox_id)
                if discovered is not None:
                    return self._register_discovered_sandbox(thread_id, discovered)

                return self._create_sandbox(thread_id, sandbox_id)
            finally:
                if locked:
                    _unlock_file(lock_file)

    async def _discover_or_create_with_lock_async(self, thread_id: str, sandbox_id: str) -> str:
        """``_discover_or_create_with_lock`` 的 async 对应。"""
        lock_dir = await asyncio.to_thread(self._thread_lock_dir, thread_id)
        lock_path = os.path.join(lock_dir, f"{sandbox_id}.lock")

        lock_file = await asyncio.to_thread(_open_lock_file, lock_path)
        locked = False
        try:
            await asyncio.to_thread(_lock_file_exclusive, lock_file)
            locked = True
            cached_id = await asyncio.to_thread(self._recheck_cached_sandbox, thread_id, sandbox_id)
            if cached_id is not None:
                return cached_id

            # backend 发现是同步的（本地发现可能 inspect Docker + 健康检查），跑线程外。
            discovered = await asyncio.to_thread(self._backend.discover, sandbox_id)
            if discovered is not None:
                return self._register_discovered_sandbox(thread_id, discovered)

            return await self._create_sandbox_async(thread_id, sandbox_id)
        finally:
            if locked:
                await asyncio.to_thread(_unlock_file, lock_file)
            await asyncio.to_thread(lock_file.close)

    def _evict_oldest_warm(self) -> str | None:
        """销毁暖池里最老的容器腾容量。返回被淘汰的 sandbox_id，空池返回 None。"""
        with self._lock:
            if not self._warm_pool:
                return None
            oldest_id = min(self._warm_pool, key=lambda sid: self._warm_pool[sid][1])
            info, _ = self._warm_pool.pop(oldest_id)

        try:
            self._backend.destroy(info)
            logger.info("Destroyed warm-pool sandbox %s", oldest_id)
        except Exception as e:
            logger.error("Failed to destroy warm-pool sandbox %s: %s", oldest_id, e)
            return None
        return oldest_id

    def _create_sandbox(self, thread_id: str | None, sandbox_id: str) -> str:
        """经 backend 建新沙箱。

        Raises:
            RuntimeError: 建沙箱或就绪检查失败。
        """
        extra_mounts = self._get_extra_mounts(thread_id)

        # 执行 replicas：只有暖池容器算淘汰预算。活跃沙箱正被线程用，不强停。
        replicas, total = self._replica_count()
        if total >= replicas:
            evicted = self._evict_oldest_warm()
            self._log_replicas_soft_cap(replicas, sandbox_id, evicted)

        info = self._backend.create(thread_id, sandbox_id, extra_mounts=extra_mounts or None)

        # 等沙箱就绪
        if not wait_for_sandbox_ready(info.sandbox_url, timeout=60):
            self._backend.destroy(info)
            raise RuntimeError(f"Sandbox {sandbox_id} failed to become ready within timeout at {info.sandbox_url}")

        return self._register_created_sandbox(thread_id, sandbox_id, info)

    async def _create_sandbox_async(self, thread_id: str | None, sandbox_id: str) -> str:
        """``_create_sandbox`` 的 async 对应。"""
        extra_mounts = await asyncio.to_thread(self._get_extra_mounts, thread_id)

        replicas, total = self._replica_count()
        if total >= replicas:
            evicted = await asyncio.to_thread(self._evict_oldest_warm)
            self._log_replicas_soft_cap(replicas, sandbox_id, evicted)

        info = await asyncio.to_thread(self._backend.create, thread_id, sandbox_id, extra_mounts=extra_mounts or None)

        # 不卡事件循环地等就绪。
        if not await wait_for_sandbox_ready_async(info.sandbox_url, timeout=60):
            await asyncio.to_thread(self._backend.destroy, info)
            raise RuntimeError(f"Sandbox {sandbox_id} failed to become ready within timeout at {info.sandbox_url}")

        return self._register_created_sandbox(thread_id, sandbox_id, info)

    def get(self, sandbox_id: str) -> Sandbox | None:
        """按 id 取沙箱。更新最后活动时间戳。"""
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_activity[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        """把沙箱从活跃用释放进暖池。

        容器继续跑，下次同 thread 复用免冷启动。仅在 replicas 上限强制淘汰或 shutdown 时才停。
        缓存的 ``AioSandbox`` 持有的宿主侧 HTTP client 在丢弃实例前关闭（#2872）。暖池条目只存
        ``SandboxInfo``，故容器日后被回收时新建 ``AioSandbox``（与 client）。
        """
        info = None
        sandbox = None
        thread_ids_to_remove: list[str] = []

        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            info = self._sandbox_infos.pop(sandbox_id, None)
            thread_ids_to_remove = [tid for tid, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for tid in thread_ids_to_remove:
                del self._thread_sandboxes[tid]
            self._last_activity.pop(sandbox_id, None)
            # 停进暖池——容器继续跑
            if info and sandbox_id not in self._warm_pool:
                self._warm_pool[sandbox_id] = (info, time.time())

        if sandbox is not None:
            # defense-in-depth：close() 已自吞错误；此 guard 仅防未来的 close() 行为异常，
            # 免得宿主侧 client 清理阻塞停进暖池。
            try:
                sandbox.close()
            except Exception as e:
                logger.warning("Error closing sandbox %s during release: %s", sandbox_id, e)

        logger.info("Released sandbox %s to warm pool (container still running)", sandbox_id)

    def destroy(self, sandbox_id: str) -> None:
        """销毁沙箱：停容器并释放所有资源。

        与 ``release()`` 不同，它真停容器。用于显式清理、容量驱动淘汰或 shutdown。
        缓存的 ``AioSandbox`` 持有的宿主侧 HTTP client 随 backend/容器销毁一起关，免 client/socket 泄漏（#2872）。
        """
        sandbox, info, _ = self._remove_tracked_sandbox(sandbox_id)

        if sandbox is not None:
            try:
                sandbox.close()
            except Exception as e:
                logger.warning("Error closing sandbox %s during destroy: %s", sandbox_id, e)

        if info:
            self._backend.destroy(info)
            logger.info("Destroyed sandbox %s", sandbox_id)

    def shutdown(self) -> None:
        """关所有沙箱。线程安全且幂等。"""
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            sandbox_ids = list(self._sandboxes.keys())
            warm_items = list(self._warm_pool.items())
            self._warm_pool.clear()

        # 停 idle 检查器
        self._idle_checker_stop.set()
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            self._idle_checker_thread.join(timeout=5)
            logger.info("Stopped idle checker thread")

        logger.info("Shutting down %d active + %d warm-pool sandbox(es)", len(sandbox_ids), len(warm_items))

        for sandbox_id in sandbox_ids:
            try:
                self.destroy(sandbox_id)
            except Exception as e:
                logger.error("Failed to destroy sandbox %s during shutdown: %s", sandbox_id, e)

        for sandbox_id, (info, _) in warm_items:
            try:
                self._backend.destroy(info)
                logger.info("Destroyed warm-pool sandbox %s during shutdown", sandbox_id)
            except Exception as e:
                logger.error("Failed to destroy warm-pool sandbox %s during shutdown: %s", sandbox_id, e)

    def reset(self) -> None:
        """重置 provider（``reset_sandbox_provider`` 会调）。等同于 shutdown。"""
        self.shutdown()
