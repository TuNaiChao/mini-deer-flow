"""AIO 沙箱子包（M10b）：生产容器隔离。

导出公共 API。``agent_sandbox`` SDK 缺包时 ``AioSandbox`` / ``AioSandboxProvider`` 仍可 import
（类定义不依赖 SDK），仅真正实例化 ``AioSandbox`` 时才抛带安装提示的 ImportError。
"""

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
from deerflow.community.aio_sandbox.backend import SandboxBackend
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend
from deerflow.community.aio_sandbox.remote_backend import RemoteSandboxBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

__all__ = [
    "AioSandbox",
    "AioSandboxProvider",
    "LocalContainerBackend",
    "RemoteSandboxBackend",
    "SandboxBackend",
    "SandboxInfo",
]
