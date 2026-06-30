"""宿主机本地沙箱 provider：``LocalSandboxProvider``。

「provider」是 **工厂 + 生命周期管理器**：它负责 acquire（取一个沙箱）、get（按 id 查）、
release（释放）、reset / shutdown。为什么要 provider，而不是每次 ``new LocalSandbox()``？

- **复用**：同一个 thread 多次工具调用应复用同一个沙箱（否则每个 bash 都重新建目录、
  丢失 ``_agent_written_paths`` 等缓存）。
- **生命周期统一**：单例 provider 让 ``SandboxMiddleware`` 与工具层取到同一个实例，
  acquire / release 配对正确。
- **per-thread 隔离**：按 ``thread_id`` 造沙箱，把 ``/mnt/user-data/...`` 绑到该线程的
  宿主目录，与 AIO 的 bind-mount 行为一致。``acquire(None)`` 返回通用单例供无线程上下文场景。
- **内存封顶**：``_thread_sandboxes`` 是 LRU（默认 256），超限淘汰最久未用的。

线程安全：``acquire`` / ``get`` / ``reset`` 可能从多个线程调用（工具分发、子代理 worker 池、
后台 memory updater），所有缓存状态变更都串行化进一把 ``threading.Lock``。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from deerflow.config import get_app_config
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.local.local_sandbox import (
    DEFAULT_MAX_CACHED_THREAD_SANDBOXES,
    LocalSandbox,
    PathMapping,
    ensure_thread_dirs,
)
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

# 模块级别名，向后兼容直接摸 ``local_sandbox_provider._singleton`` 的旧调用方 / 测试。
# 新代码读 provider 实例属性（``_generic_sandbox`` / ``_thread_sandboxes``）。
_singleton: LocalSandbox | None = None

# 虚拟前缀常量（与 config.paths.VIRTUAL_PATH_PREFIX 一致）。
_USER_DATA_VIRTUAL_PREFIX = "/mnt/user-data"


class LocalSandboxProvider(SandboxProvider):
    """宿主机本地沙箱 provider：按 thread_id 做 per-thread 路径隔离 + LRU 缓存。

    早期版本返回一个进程级单例 ``LocalSandbox``（id 固定 ``"local"``），但那样无法兑现
    ``/mnt/user-data/...`` 契约——因为对应的宿主目录是按线程隔离的。现在
    ``acquire(thread_id)`` 为每个 thread 造一个 ``LocalSandbox``（id ``"local:{thread_id}"``），
    其 ``path_mappings`` 把 ``/mnt/user-data/{workspace,uploads,outputs}`` 绑到该线程的
    宿主目录，与 AIO 的 bind-mount 行为一致。``acquire(None)`` 仍返回通用单例（id ``"local"``）
    供无线程上下文的调用方 / 测试用。
    """

    uses_thread_data_mounts = True
    needs_upload_permission_adjustment = False

    def __init__(self, max_cached_threads: int = DEFAULT_MAX_CACHED_THREAD_SANDBOXES):
        """
        Args:
            max_cached_threads: per-thread 沙箱缓存上限，超出 LRU 淘汰。
        """
        self._path_mappings = self._setup_path_mappings()
        self._generic_sandbox: LocalSandbox | None = None
        self._thread_sandboxes: OrderedDict[str, LocalSandbox] = OrderedDict()
        self._max_cached_threads = max_cached_threads
        self._lock = threading.Lock()

    def _setup_path_mappings(self) -> list[PathMapping]:
        """建静态映射（skills 目录 + 自定义卷挂载，进程级、所有线程共享）。

        per-thread 的 ``/mnt/user-data/...`` 映射在 ``acquire`` 里追加（依赖 thread_id +
        user_id）。自定义卷挂载（``sandbox.mounts``）在此处理：operator 配的额外目录
        按各自的 ``container_path`` 暴露给 agent（如 ``/data/shared``）。host_path 必须绝对
        且存在，否则跳过（与 AIO bind-mount 一致——不存在的源目录挂不进去）。ACP workspace
        由 ``invoke_acp_agent_tool`` 自己建 per-thread 目录、不在此处。
        """
        mappings: list[PathMapping] = []
        try:
            config = get_app_config()
            skills_path = config.skills.get_skills_path()
            container_path = config.skills.container_path
            if skills_path.exists():
                mappings.append(PathMapping(container_path=container_path, local_path=str(skills_path), read_only=True))
        except Exception as e:
            # 配置加载失败不致命：skills 不可用，沙箱仍能跑 user-data。
            logger.warning("Could not setup skills path mapping: %s", e, exc_info=True)

        # 自定义卷挂载（sandbox.mounts）。host_path 必须绝对且存在。
        try:
            from pathlib import Path

            config = get_app_config()
            sandbox_config = getattr(config, "sandbox", None)
            mounts = getattr(sandbox_config, "mounts", None) if sandbox_config else None
            if mounts:
                for mount in mounts:
                    host_path = Path(mount.host_path)
                    if not host_path.is_absolute():
                        logger.warning("Mount host_path must be absolute, skipping: %s -> %s", mount.host_path, mount.container_path)
                        continue
                    if not host_path.exists():
                        logger.warning("Mount host_path does not exist, skipping: %s -> %s", mount.host_path, mount.container_path)
                        continue
                    mappings.append(
                        PathMapping(
                            container_path=mount.container_path,
                            local_path=str(host_path),
                            read_only=bool(getattr(mount, "read_only", False)),
                        )
                    )
        except Exception as e:
            # 自定义挂载配置加载失败不致命。
            logger.warning("Could not setup custom volume mounts: %s", e, exc_info=True)
        return mappings

    @staticmethod
    def _build_thread_path_mappings(thread_id: str) -> list[PathMapping]:
        """建 per-thread 映射：``/mnt/user-data[/{workspace,uploads,outputs}]`` → 宿主目录。

        通过 ``get_effective_user_id()`` 解析用户，``ensure_thread_dirs`` 确保宿主目录存在。
        """
        user_id = get_effective_user_id()
        root = ensure_thread_dirs(thread_id, user_id=user_id)
        return [
            # 聚合父级映射，让 ``ls /mnt/user-data`` 等父级操作与 AIO 一致；
            # 下面的子路径映射因 _find_path_mapping 按长度排序仍会对 /mnt/user-data/workspace/... 胜出。
            PathMapping(container_path=_USER_DATA_VIRTUAL_PREFIX, local_path=str(root), read_only=False),
            PathMapping(container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/workspace", local_path=str(root / "workspace"), read_only=False),
            PathMapping(container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/uploads", local_path=str(root / "uploads"), read_only=False),
            PathMapping(container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/outputs", local_path=str(root / "outputs"), read_only=False),
        ]

    def acquire(self, thread_id: str | None = None) -> str:
        """返回按 thread_id 隔离的沙箱 id（或通用单例 ``"local"``）。

        - ``thread_id=None``：返回通用单例（id ``"local"``），供无线程上下文场景。
        - ``thread_id="abc"``：返回 per-thread ``LocalSandbox``（id ``"local:abc"``）。

        线程安全：缓存检查 + 插入在 ``self._lock`` 内，同一 thread_id 的并发调用拿到同一实例。
        """
        global _singleton

        if thread_id is None:
            with self._lock:
                if self._generic_sandbox is None:
                    self._generic_sandbox = LocalSandbox("local", path_mappings=list(self._path_mappings))
                    _singleton = self._generic_sandbox
                return self._generic_sandbox.id

        # 锁内快路径：命中缓存。
        with self._lock:
            cached = self._thread_sandboxes.get(thread_id)
            if cached is not None:
                self._thread_sandboxes.move_to_end(thread_id)  # 标记为最近使用，免被淘汰
                return cached.id

        # ensure_thread_dirs 触及文件系统；I/O 期间释放锁。
        new_mappings = list(self._path_mappings) + self._build_thread_path_mappings(thread_id)

        with self._lock:
            # I/O 后复查：期间可能已被别的调用方填入缓存。
            cached = self._thread_sandboxes.get(thread_id)
            if cached is None:
                cached = LocalSandbox(f"local:{thread_id}", path_mappings=new_mappings)
                self._thread_sandboxes[thread_id] = cached
                self._evict_until_within_cap_locked()
            else:
                self._thread_sandboxes.move_to_end(thread_id)
            return cached.id

    def _evict_until_within_cap_locked(self) -> None:
        """LRU 淘汰直到缓存数回到上限内。调用方必须持有 ``self._lock``。"""
        while len(self._thread_sandboxes) > self._max_cached_threads:
            evicted_thread_id, _ = self._thread_sandboxes.popitem(last=False)
            logger.info("Evicting LocalSandbox cache entry for thread %s (cap=%d)", evicted_thread_id, self._max_cached_threads)

    def get(self, sandbox_id: str) -> Sandbox | None:
        if sandbox_id == "local":
            with self._lock:
                generic = self._generic_sandbox
            if generic is None:
                self.acquire()
                with self._lock:
                    return self._generic_sandbox
            return generic
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local:"):
            thread_id = sandbox_id[len("local:") :]
            with self._lock:
                cached = self._thread_sandboxes.get(thread_id)
                if cached is not None:
                    # get（工具每次调用都会查一次）也提升 LRU 顺序，活跃线程不易被淘汰。
                    self._thread_sandboxes.move_to_end(thread_id)
                return cached
        return None

    def release(self, sandbox_id: str) -> None:
        # LocalSandbox 无需释放的资源；保留缓存实例让 _agent_written_paths 跨轮次存活。
        # 只有 LRU 淘汰和显式 reset/shutdown 会清缓存。
        # 注：SandboxMiddleware 刻意不调本方法，以支持沙箱跨轮次复用。
        pass

    def reset(self) -> None:
        """丢弃所有缓存的 LocalSandbox（``reset_sandbox_provider`` 会调）。

        让 config / 挂载改动在下次 acquire 生效；同时清模块级 ``_singleton`` 别名。
        """
        global _singleton
        with self._lock:
            self._generic_sandbox = None
            self._thread_sandboxes.clear()
            _singleton = None

    def shutdown(self) -> None:
        # LocalSandbox 无额外资源，shutdown 与 reset 同路径。
        self.reset()
