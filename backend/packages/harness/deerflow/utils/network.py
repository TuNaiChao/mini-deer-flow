"""线程安全的网络端口分配工具。

为什么要它？

- AIO 沙箱（M10b）的 ``LocalContainerBackend`` 要给每个容器分配一个宿主机端口做
  ``-p host_port:8080`` 映射。并发起多个容器时，若两个线程同时挑端口可能撞同一个。
- 所以用一个进程级 ``PortAllocator``：锁保护的「已保留端口集合」+ socket bind 探测，
  分配后标记保留，显式 ``release`` 才回收。

为什么要 bind 到 ``0.0.0.0`` 而不是 ``localhost``？Docker 也是绑 ``0.0.0.0:PORT``；
只查 ``127.0.0.1`` 可能在 Docker 已占 wildcard 时误报端口空闲，导致 ``docker run -p``
报「port is already allocated」。镜像 Docker 的绑定行为才能准确探测。
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager


class PortAllocator:
    """线程安全的端口分配器：分配后保留至显式释放。

    用法::

        allocator = PortAllocator()
        port = allocator.allocate(start_port=8080)
        try:
            ...  # 用端口
        finally:
            allocator.release(port)

        # 或上下文管理器（推荐）：
        with allocator.allocate_context(start_port=8080) as port:
            ...
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reserved_ports: set[int] = set()

    def _is_port_available(self, port: int) -> bool:
        """端口是否可 bind（先查保留集合，再 socket 实测）。"""
        if port in self._reserved_ports:
            return False
        # bind 0.0.0.0 镜像 Docker 的 wildcard 绑定，避免只查 loopback 的误报。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def allocate(self, start_port: int = 8080, max_range: int = 100) -> int:
        """线程安全地分配一个可用端口，标记保留并返回。

        Args:
            start_port: 起始搜索端口。
            max_range: 最多往后搜多少个。

        Returns:
            可用端口号。

        Raises:
            RuntimeError: 区间内无可用端口。
        """
        with self._lock:
            for port in range(start_port, start_port + max_range):
                if self._is_port_available(port):
                    self._reserved_ports.add(port)
                    return port
            raise RuntimeError(f"No available port found in range {start_port}-{start_port + max_range}")

    def release(self, port: int) -> None:
        """释放之前分配的端口。"""
        with self._lock:
            self._reserved_ports.discard(port)

    @contextmanager
    def allocate_context(self, start_port: int = 8080, max_range: int = 100):
        """端口分配的上下文管理器（退出自动释放）。"""
        port = self.allocate(start_port, max_range)
        try:
            yield port
        finally:
            self.release(port)


# 进程级全局分配器（AIO 沙箱 backend 共用，防并发撞端口）。
_global_port_allocator = PortAllocator()


def get_free_port(start_port: int = 8080, max_range: int = 100) -> int:
    """线程安全地取一个空闲端口（全局分配器，分配后保留至 ``release_port``）。"""
    return _global_port_allocator.allocate(start_port, max_range)


def release_port(port: int) -> None:
    """释放 ``get_free_port`` 分配的端口。"""
    _global_port_allocator.release(port)
