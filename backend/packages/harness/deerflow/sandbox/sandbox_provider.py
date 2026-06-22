"""沙箱 provider 抽象基类 + 进程级单例。

「provider」是**工厂 + 生命周期管理器**：它负责 acquire（取一个沙箱）、get（按 id 查）、
release（释放）。为什么要 provider，而不是每次 ``new LocalSandbox()``？

- **复用**：同一个 thread 多次工具调用应复用同一个沙箱（否则每个 bash 都重新建目录、
  丢失 ``_agent_written_paths`` 等缓存）。
- **生命周期统一**：单例 provider 让 ``SandboxMiddleware`` 与工具层取到同一个实例，
  acquire / release 配对正确。
- **可替换实现**：通过 ``config.sandbox.use`` 指向不同 provider 类（Local / 未来的
  Docker），运行时反射实例化，无需改业务代码。

单例通过模块级 ``_default_sandbox_provider`` 缓存；``reset_sandbox_provider`` /
``shutdown_sandbox_provider`` / ``set_sandbox_provider`` 给测试与生命周期管理用。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from deerflow.config import get_app_config
from deerflow.reflection import resolve_class
from deerflow.sandbox.sandbox import Sandbox


class SandboxProvider(ABC):
    """沙箱 provider 抽象基类。"""

    #: 该 provider 是否依赖 ``thread_data`` 挂载（LocalSandboxProvider=True）。
    #: 给未来的 UploadsMiddleware 等用来判断是否需要调整上传目录权限。
    uses_thread_data_mounts: bool = False
    #: 是否需要调整上传目录权限（Local 模式不需要，容器模式需要）。
    needs_upload_permission_adjustment: bool = True

    @abstractmethod
    def acquire(self, thread_id: str | None = None) -> str:
        """获取（或复用）一个沙箱，返回其 id。

        Args:
            thread_id: 线程 id。``None`` 表示无线程上下文（返回进程级通用单例）。

        Returns:
            沙箱 id（如 ``"local:abc"``）。
        """

    async def acquire_async(self, thread_id: str | None = None) -> str:
        """异步获取沙箱——把阻塞的 acquire 卸载到工作线程，不卡事件循环。

        大多数 provider 的生命周期 API 是同步的（本地 Docker / provisioner 操作阻塞）。
        async 运行时应调本方法，让这些阻塞操作跑在 worker 线程里。
        """
        return await asyncio.to_thread(self.acquire, thread_id)

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """按 id 取沙箱实例；不存在返回 ``None``。"""

    @abstractmethod
    def release(self, sandbox_id: str) -> None:
        """释放沙箱资源。"""

    def reset(self) -> None:
        """清空跨 provider 实例存活的缓存状态（供子类覆盖）。

        ``LocalSandboxProvider`` 会覆盖本方法清掉缓存的 per-thread ``LocalSandbox``，
        否则 config / 挂载改动在下次 acquire 时不生效。
        """


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_default_sandbox_provider: SandboxProvider | None = None


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    """取沙箱 provider 单例。

    首次调用时按 ``config.sandbox.use`` 反射实例化并缓存。用
    ``reset_sandbox_provider()`` 清缓存，``shutdown_sandbox_provider()`` 先 shutdown
    再清，``set_sandbox_provider()`` 注入自定义（测试）实例。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is None:
        config = get_app_config()
        cls = resolve_class(config.sandbox.use, SandboxProvider)
        _default_sandbox_provider = cls(**kwargs)
    return _default_sandbox_provider


def reset_sandbox_provider() -> None:
    """重置 provider 单例（不清资源）。

    调子类 ``reset()`` 清跨实例缓存，再把单例置空。下次 ``get_sandbox_provider()``
    会按当前 config 重建。注意：若有活跃沙箱会被孤儿化；要干净清理用
    ``shutdown_sandbox_provider()``。
    """
    global _default_sandbox_provider
    if _default_sandbox_provider is not None:
        _default_sandbox_provider.reset()
        _default_sandbox_provider = None


def shutdown_sandbox_provider() -> None:
    """先 shutdown（释放所有沙箱）再清单例。应用退出时调。"""
    global _default_sandbox_provider
    if _default_sandbox_provider is not None:
        if hasattr(_default_sandbox_provider, "shutdown"):
            _default_sandbox_provider.shutdown()
        _default_sandbox_provider = None


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """注入自定义 provider（测试用）。"""
    global _default_sandbox_provider
    _default_sandbox_provider = provider
