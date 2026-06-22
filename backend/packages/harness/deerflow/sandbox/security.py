"""沙箱能力开关：host bash 是否允许。

为什么要单独管这个？

- ``LocalSandboxProvider`` 直接在**宿主机**进程里跑 bash，**不是**安全沙箱边界——
  agent 跑的 ``rm -rf`` / ``cat /etc/passwd`` 会真实生效在宿主上。
- 因此默认**禁用** host bash，只在用户**显式**设置 ``sandbox.allow_host_bash: true``
  （且 provider 是 Local）时才放行，并要求「完全可信的本地环境」。
- 真正的隔离要靠 AIO 容器沙箱（M10b）。

``is_host_bash_allowed()`` 是 ``bash`` 工具的唯一准入闸：返回 False 时 bash 工具直接
返回禁用提示，不执行任何命令。
"""

from __future__ import annotations

from deerflow.config import get_app_config

# ``config.sandbox.use`` 可能写成「包:类」或「模块:类」两种形式，都认。
# provider 现拆到独立模块 local_sandbox_provider.py（v1.2 对齐 deer 文件结构），
# 同时保留旧 local_sandbox 模块路径作兼容（早期版本 provider 与 LocalSandbox 同文件）。
_LOCAL_SANDBOX_PROVIDER_MARKERS = (
    "deerflow.sandbox.local:LocalSandboxProvider",
    "deerflow.sandbox.local.local_sandbox_provider:LocalSandboxProvider",
    "deerflow.sandbox.local.local_sandbox:LocalSandboxProvider",
)

LOCAL_HOST_BASH_DISABLED_MESSAGE = (
    "Host bash execution is disabled for LocalSandboxProvider because it is not a secure "
    "sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or set "
    "sandbox.allow_host_bash: true only in a fully trusted local environment."
)

LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE = (
    "Bash subagent is disabled for LocalSandboxProvider because host bash execution is not "
    "a secure sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or "
    "set sandbox.allow_host_bash: true only in a fully trusted local environment."
)


def uses_local_sandbox_provider(config=None) -> bool:
    """当前 provider 是否是宿主机本地 provider（``LocalSandboxProvider``）。"""
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    sandbox_use = getattr(sandbox_cfg, "use", "")
    if sandbox_use in _LOCAL_SANDBOX_PROVIDER_MARKERS:
        return True
    return sandbox_use.endswith(":LocalSandboxProvider") and "deerflow.sandbox.local" in sandbox_use


def is_host_bash_allowed(config=None) -> bool:
    """host bash 是否被显式放行。

    语义：
    - 非 Local provider（未来的 Docker 等）：总是允许（它们有真正的隔离）。
    - Local provider：仅当 ``sandbox.allow_host_bash`` 显式为 True 才允许（默认 False）。
    """
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    if sandbox_cfg is None:
        return False
    if not uses_local_sandbox_provider(config):
        return True
    return bool(getattr(sandbox_cfg, "allow_host_bash", False))
