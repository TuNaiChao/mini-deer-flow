"""沙箱供给 backend 抽象基类 + 就绪轮询。

「backend」回答「**怎么把一个沙箱容器弄起来 / 查它活没活 / 销毁它**」。两类实现：

- ``LocalContainerBackend``（``local_backend.py``）：本机起 Docker / Apple Container，自管端口与生命周期。
- ``RemoteSandboxBackend``（``remote_backend.py``）：连远端 provisioner（K8s），Pod 生命周期委托给 provisioner，
  本地不持有容器句柄。

抽象方法：``create`` / ``destroy`` / ``is_alive`` / ``discover``；``list_running`` 有默认空实现
（远端 backend 委托 provisioner 自己清，本地 backend 覆盖它枚举本机容器）。

``wait_for_sandbox_ready[_async]`` 轮询 ``/v1/sandbox`` 健康端点直到就绪或超时——容器刚起时
HTTP API 还没 listen，必须等它 ready 才能连。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx
import requests

from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

logger = logging.getLogger(__name__)


def wait_for_sandbox_ready(sandbox_url: str, timeout: int = 30) -> bool:
    """轮询沙箱健康端点直到就绪或超时（同步版，供 backend/provider 同步路径用）。

    Args:
        sandbox_url: 沙箱 URL（如 ``http://k3s:30001``）。
        timeout: 最大等待秒数。

    Returns:
        就绪返回 True，超时返回 False。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{sandbox_url}/v1/sandbox", timeout=5)
            if response.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    return False


async def wait_for_sandbox_ready_async(sandbox_url: str, timeout: int = 30, poll_interval: float = 1.0) -> bool:
    """``wait_for_sandbox_ready`` 的 async 版：让沙箱启动等待不卡事件循环。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                response = await client.get(f"{sandbox_url}/v1/sandbox", timeout=min(5.0, remaining))
                if response.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
    return False


class SandboxBackend(ABC):
    """沙箱供给 backend 抽象基类。

    两个实现：
    - ``LocalContainerBackend``：本机起 Docker/Apple Container，管端口。
    - ``RemoteSandboxBackend``：连预置 URL（K8s service / 外部 provisioner）。
    """

    @abstractmethod
    def create(self, thread_id: str | None, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """创建 / 供给一个新沙箱。

        Args:
            thread_id: 为哪个 thread 创建（backend 可据此组织沙箱）。
            sandbox_id: 确定性沙箱 id。
            extra_mounts: 额外卷挂载 ``(host_path, container_path, read_only)``；
                不管容器的 backend（如 remote）忽略它。

        Returns:
            连接信息 ``SandboxInfo``。
        """

    @abstractmethod
    def destroy(self, info: SandboxInfo) -> None:
        """销毁 / 清理一个沙箱，释放资源。"""

    @abstractmethod
    def is_alive(self, info: SandboxInfo) -> bool:
        """快速检查沙箱是否还活着（轻量，如 container inspect，不做完整健康检查）。"""

    @abstractmethod
    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """按确定性 id 尝试发现一个已存在的沙箱（跨进程恢复用）。

        Returns:
            找到且健康返回 ``SandboxInfo``，否则 ``None``。
        """

    def list_running(self) -> list[SandboxInfo]:
        """枚举本 backend 管的所有运行中沙箱（启动 reconcile 用）。

        默认空列表——远端 backend 委托 provisioner 自清，本地 backend 覆盖它枚举本机容器。
        """
        return []
