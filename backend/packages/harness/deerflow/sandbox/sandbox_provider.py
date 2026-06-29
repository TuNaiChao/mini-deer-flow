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
import threading
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
# 守卫 ``_default_sandbox_provider`` 的每一次读写。该单例可被多个 OS 线程触达（例如
# 主事件循环与跑自己一个循环的 Feishu channel 线程），裸的 check-then-create 会双重
# 初始化 provider，而未同步的 reset / shutdown 与 get 竞争会把 ``None`` 或半成品实例
# 交给调用方。下面所有对全局的访问都持本锁，包括 ``get_sandbox_provider()`` 的读+返回。
#
# 锁只守卫引用交换。provider 回调（``__init__`` / ``reset()`` / ``shutdown()``）与
# ``resolve_class()`` 里的动态 import 都在锁**外**跑：它们是插件代码（``config.sandbox.use``
# 解析到任意类），可能很慢，更糟的是可能重入这些生命周期函数。用非重入
# ``threading.Lock`` 跨着它们会自死锁，还会在一次慢拆除期间挡住所有并发 ``get()``。
# 把回调挪到锁外，两个问题都避开了（#3730）。
_provider_lock = threading.Lock()


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    """取沙箱 provider 单例。

    首次调用时按 ``config.sandbox.use`` 反射实例化并缓存。用
    ``reset_sandbox_provider()`` 清缓存，``shutdown_sandbox_provider()`` 先 shutdown
    再清，``set_sandbox_provider()`` 注入自定义（测试）实例。
    """
    global _default_sandbox_provider
    # 快路径：一次带锁的读，让并发的 reset / shutdown 没法在 check 与 return 之间把全局置空。
    with _provider_lock:
        if _default_sandbox_provider is not None:
            return _default_sandbox_provider

    # 冷启动。resolve + 构造在锁外做：import 与 provider 构造器是插件代码，不能在非重入锁下跑。
    # 构造可能与另一个调用方竞争；在锁下裁决谁装上去。
    config = get_app_config()
    cls = resolve_class(config.sandbox.use, SandboxProvider)
    provider = cls(**kwargs)

    with _provider_lock:
        if _default_sandbox_provider is None:
            _default_sandbox_provider = provider
            return provider
        # 我们输了安装竞争：另一个线程先到。``winner`` 在同一把锁下读，所以一定是存活实例、绝非 None。
        winner = _default_sandbox_provider

    # 丢弃刚建出来的实例（锁外）。对构造有副作用的 provider（如 ``AioSandboxProvider`` 起了
    # idle-checker 线程），拆除这个孤儿免得泄漏（issue #3721）。
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    return winner


def reset_sandbox_provider() -> None:
    """重置 provider 单例（不清资源）。

    调子类 ``reset()`` 清跨实例缓存，再把单例置空。下次 ``get_sandbox_provider()``
    会按当前 config 重建。注意：若有活跃沙箱会被孤儿化；要干净清理用
    ``shutdown_sandbox_provider()``。
    """
    global _default_sandbox_provider
    # 锁下摘引用，锁外跑 provider 的 ``reset()`` 回调（见 ``_provider_lock`` 注）。
    with _provider_lock:
        provider = _default_sandbox_provider
        _default_sandbox_provider = None
    if provider is not None:
        provider.reset()


def shutdown_sandbox_provider() -> None:
    """先 shutdown（释放所有沙箱）再清单例。应用退出时调。"""
    global _default_sandbox_provider
    # 锁下摘引用，锁外跑（可能很慢的）``shutdown()`` 回调（见 ``_provider_lock`` 注）。
    with _provider_lock:
        provider = _default_sandbox_provider
        _default_sandbox_provider = None
    if provider is not None and hasattr(provider, "shutdown"):
        provider.shutdown()


def set_sandbox_provider(provider: SandboxProvider) -> None:
    """注入自定义 provider（测试用）。

    注意：之前装的 provider 会被**替换但不 shutdown**；被覆盖实例的生命周期由调用方自负。
    """
    global _default_sandbox_provider
    with _provider_lock:
        _default_sandbox_provider = provider
