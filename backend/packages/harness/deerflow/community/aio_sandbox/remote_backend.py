"""远端沙箱 backend —— 把 Pod 生命周期委托给 provisioner 服务。

provisioner 在 k3s 里按 sandbox_id 动态建 Pod + NodePort Service。本 backend 是个薄 HTTP
client，直接经 ``k3s:{NodePort}`` 访问沙箱 pod。本地不持有容器句柄、不管端口。

架构::

    ┌────────────┐  HTTP   ┌─────────────┐  K8s API  ┌──────────┐
    │ 本文件     │ ──────▸ │ provisioner │ ────────▸ │   k3s    │
    │ (backend)  │         │ :8002       │           │ :6443    │
    └────────────┘         └─────────────┘           └─────┬────┘
                                                           │ creates
                           ┌─────────────┐           ┌─────▼──────┐
                           │   backend   │ ────────▸ │  sandbox   │
                           │             │  直连     │  Pod(s)    │
                           └─────────────┘ k3s:NPort └────────────┘

典型 config.yaml::

    sandbox:
      use: deerflow.community.aio_sandbox:AioSandboxProvider
      provisioner_url: http://provisioner:8002
"""

from __future__ import annotations

import logging

import requests

from deerflow.community.aio_sandbox.backend import SandboxBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)


class RemoteSandboxBackend(SandboxBackend):
    """把沙箱生命周期委托给 provisioner 服务的 backend。

    Pod 的创建 / 销毁 / 发现全由 provisioner 处理；本 backend 只是个薄 HTTP client。
    """

    def __init__(self, provisioner_url: str):
        """
        Args:
            provisioner_url: provisioner 服务 URL（如 ``http://provisioner:8002``）。
        """
        self._provisioner_url = provisioner_url.rstrip("/")

    @property
    def provisioner_url(self) -> str:
        return self._provisioner_url

    # ── SandboxBackend interface ──────────────────────────────────────────

    def create(
        self,
        thread_id: str | None,
        sandbox_id: str,
        extra_mounts: list[tuple[str, str, bool]] | None = None,
    ) -> SandboxInfo:
        """经 provisioner ``POST /api/sandboxes`` 建 Pod + Service。"""
        return self._provisioner_create(thread_id, sandbox_id, extra_mounts)

    def destroy(self, info: SandboxInfo) -> None:
        """经 provisioner ``DELETE /api/sandboxes/{id}`` 销毁 Pod + Service。"""
        self._provisioner_destroy(info.sandbox_id)

    def is_alive(self, info: SandboxInfo) -> bool:
        """查 Pod 是否在跑。"""
        return self._provisioner_is_alive(info.sandbox_id)

    def discover(self, sandbox_id: str) -> SandboxInfo | None:
        """经 provisioner ``GET /api/sandboxes/{id}`` 发现已存在沙箱。"""
        return self._provisioner_discover(sandbox_id)

    def list_running(self) -> list[SandboxInfo]:
        """返回 provisioner 当前管的所有沙箱。

        ``GET /api/sandboxes`` 让 ``AioSandboxProvider._reconcile_orphans()`` 能收养前一进程
        建了却从没显式销毁的 pod——否则进程重启会悄悄孤立所有现存 K8s Pod（它们永远跑着，因为
        idle 检查器只跟进程内状态）。
        """
        return self._provisioner_list()

    # ── Provisioner API calls ─────────────────────────────────────────────

    def _provisioner_list(self) -> list[SandboxInfo]:
        """GET /api/sandboxes → 列所有运行中沙箱。"""
        try:
            resp = requests.get(f"{self._provisioner_url}/api/sandboxes", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning("Provisioner list_running returned non-dict payload: %r", type(data))
                return []

            sandboxes = data.get("sandboxes", [])
            if not isinstance(sandboxes, list):
                logger.warning("Provisioner list_running returned non-list sandboxes: %r", type(sandboxes))
                return []

            infos: list[SandboxInfo] = []
            for sandbox in sandboxes:
                if not isinstance(sandbox, dict):
                    logger.warning("Provisioner list_running entry is not a dict: %r", type(sandbox))
                    continue

                sandbox_id = sandbox.get("sandbox_id")
                sandbox_url = sandbox.get("sandbox_url")
                if isinstance(sandbox_id, str) and sandbox_id and isinstance(sandbox_url, str) and sandbox_url:
                    infos.append(SandboxInfo(sandbox_id=sandbox_id, sandbox_url=sandbox_url))

            logger.info("Provisioner list_running: %d sandbox(es) found", len(infos))
            return infos
        except requests.RequestException as exc:
            logger.warning("Provisioner list_running failed: %s", exc)
            return []

    def _provisioner_create(self, thread_id: str | None, sandbox_id: str, extra_mounts: list[tuple[str, str, bool]] | None = None) -> SandboxInfo:
        """POST /api/sandboxes → 建 Pod + Service。"""
        try:
            resp = requests.post(
                f"{self._provisioner_url}/api/sandboxes",
                json={
                    "sandbox_id": sandbox_id,
                    "thread_id": thread_id,
                    "user_id": get_effective_user_id(),
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Provisioner created sandbox %s: sandbox_url=%s", sandbox_id, data["sandbox_url"])
            return SandboxInfo(
                sandbox_id=sandbox_id,
                sandbox_url=data["sandbox_url"],
            )
        except requests.RequestException as exc:
            logger.error("Provisioner create failed for %s: %s", sandbox_id, exc)
            raise RuntimeError(f"Provisioner create failed: {exc}") from exc

    def _provisioner_destroy(self, sandbox_id: str) -> None:
        """DELETE /api/sandboxes/{id} → 销毁 Pod + Service。"""
        try:
            resp = requests.delete(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=15,
            )
            if resp.ok:
                logger.info("Provisioner destroyed sandbox %s", sandbox_id)
            else:
                logger.warning("Provisioner destroy returned %s: %s", resp.status_code, resp.text)
        except requests.RequestException as exc:
            logger.warning("Provisioner destroy failed for %s: %s", sandbox_id, exc)

    def _provisioner_is_alive(self, sandbox_id: str) -> bool:
        """GET /api/sandboxes/{id} → 查 Pod phase。"""
        try:
            resp = requests.get(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=10,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Provisioner health check failed for {sandbox_id}: {exc}") from exc

        if resp.status_code == 404:
            return False
        if not resp.ok:
            raise RuntimeError(f"Provisioner health check failed for {sandbox_id}: HTTP {resp.status_code} {resp.text}")

        data = resp.json()
        return data.get("status") == "Running"

    def _provisioner_discover(self, sandbox_id: str) -> SandboxInfo | None:
        """GET /api/sandboxes/{id} → 发现已存在沙箱。"""
        try:
            resp = requests.get(
                f"{self._provisioner_url}/api/sandboxes/{sandbox_id}",
                timeout=10,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return SandboxInfo(
                sandbox_id=sandbox_id,
                sandbox_url=data["sandbox_url"],
            )
        except requests.RequestException as exc:
            logger.debug("Provisioner discover failed for %s: %s", sandbox_id, exc)
            return None
