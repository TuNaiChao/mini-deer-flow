"""沙箱元数据（跨进程发现 / 状态持久化用）。

``SandboxInfo`` 持有「重连一个已存在沙箱」所需的全部信息——sandbox_id、URL、容器名/id、
创建时间。它会被持久化（或经 backend 枚举出来），让另一个进程（gateway vs langgraph、
多 worker、K8s 多 pod 共享存储）能发现并复用前一个进程起的容器。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SandboxInfo:
    """持久化的沙箱元数据，支撑跨进程发现。

    Attributes:
        sandbox_id: 沙箱的确定性 id（由 thread_id 哈希派生）。
        sandbox_url: 沙箱 API 地址（如 ``http://localhost:8080`` 或 ``http://k3s:30001``）。
        container_name: 容器名（仅本地容器 backend 用）。
        container_id: 容器 id（仅本地容器 backend 用）。
        created_at: 创建时间戳（Unix epoch 秒），供 idle 判定 / 孤儿收养排序。
    """

    sandbox_id: str
    sandbox_url: str  # e.g. http://localhost:8080 or http://k3s:30001
    container_name: str | None = None  # Only for local container backend
    container_id: str | None = None  # Only for local container backend
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id,
            "sandbox_url": self.sandbox_url,
            "container_name": self.container_name,
            "container_id": self.container_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SandboxInfo:
        # 兼容旧字段名 base_url → sandbox_url。
        return cls(
            sandbox_id=data["sandbox_id"],
            sandbox_url=data.get("sandbox_url", data.get("base_url", "")),
            container_name=data.get("container_name"),
            container_id=data.get("container_id"),
            created_at=data.get("created_at", time.time()),
        )
