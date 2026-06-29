"""#3730：沙箱 provider 单例生命周期的并发回归测试。

守护 ``get_sandbox_provider`` 里未同步的 check-then-create、以及未加锁的
``reset`` / ``shutdown`` / ``set`` 路径：加锁前，并发冷启动的调用方可能各自构造一个
provider 并覆盖全局，而 ``reset`` / ``shutdown`` 与 ``get`` 竞争会把 ``None`` 或
半拆除的实例交给调用方。

每个测试在进入时和 ``finally`` 退出时都重置进程全局单例，测试之间不互相泄漏 provider。
对齐上游 ``test_sandbox_provider_lifecycle.py``。
"""

import threading
import time

import deerflow.sandbox.sandbox_provider as sandbox_provider
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider


class SlowSandboxProvider(SandboxProvider):
    """构造慢的 provider，用来拉大 check-then-create 的竞争窗口。"""

    instances_created = 0
    instances_lock = threading.Lock()

    def __init__(self) -> None:
        time.sleep(0.05)
        with self.instances_lock:
            type(self).instances_created += 1

    def acquire(self, thread_id: str | None = None) -> str:
        return "sandbox-id"

    def get(self, sandbox_id: str) -> Sandbox | None:
        return None

    def release(self, sandbox_id: str) -> None:
        pass


class ShutdownSandboxProvider(SlowSandboxProvider):
    """额外暴露 ``shutdown`` / ``reset`` 的 provider，用来跑那些在 ``_provider_lock``
    锁外执行 provider 回调的路径。

    每个构造出来的实例把自己登记进 ``registry``，测试据此断言哪些实例后来被拆除。
    """

    registry: list["ShutdownSandboxProvider"] = []
    registry_lock = threading.Lock()

    def __init__(self) -> None:
        super().__init__()
        self.shutdown_calls = 0
        self.reset_calls = 0
        with self.registry_lock:
            type(self).registry.append(self)

    def shutdown(self) -> None:
        # 非平凡的拆除：修复后在锁外跑它，并发 get() 不会被它阻塞或撕坏。
        time.sleep(0.02)
        self.shutdown_calls += 1

    def reset(self) -> None:
        self.reset_calls += 1


class _SandboxConfig:
    use = "SlowSandboxProvider"


class _AppConfig:
    sandbox = _SandboxConfig()


def _patch_provider_resolution(monkeypatch, cls=SlowSandboxProvider) -> None:
    monkeypatch.setattr(sandbox_provider, "get_app_config", lambda: _AppConfig())
    monkeypatch.setattr(sandbox_provider, "resolve_class", lambda *args: cls)


def test_get_sandbox_provider_installs_one_singleton_under_concurrent_access(monkeypatch):
    """8 个线程冷启动竞争，必须都观察到**同一个**被装上去的实例。

    构造在 ``_provider_lock`` 锁外跑（插件 ``__init__`` / import 不在非重入锁下执行），
    所以竞争的调用方可能各自建一个候选；契约是恰好一个被装上去、所有调用方都看到它。
    输家被拆除——见 ``test_losing_cold_start_racer_shuts_down_its_orphan``。
    """
    sandbox_provider.reset_sandbox_provider()
    SlowSandboxProvider.instances_created = 0
    _patch_provider_resolution(monkeypatch)

    n_threads = 8
    providers: list[SandboxProvider] = []
    providers_lock = threading.Lock()
    # Barrier 让所有线程在同一瞬间进入 get_sandbox_provider()，让竞争确定性地触发。
    barrier = threading.Barrier(n_threads)

    def get_provider() -> None:
        barrier.wait()
        provider = sandbox_provider.get_sandbox_provider()
        with providers_lock:
            providers.append(provider)

    threads = [threading.Thread(target=get_provider) for _ in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        # 每个调用方都看到那一个被装上去的单例，不管哪个候选赢了。
        assert len({id(provider) for provider in providers}) == 1
        installed = sandbox_provider.get_sandbox_provider()
        assert all(p is installed for p in providers)
    finally:
        sandbox_provider.reset_sandbox_provider()


def test_reset_racing_get_of_live_singleton_never_returns_none_or_torn(monkeypatch):
    """reset 与对**存活**单例的并发 get 竞争，绝不能返回 ``None`` 或半成品——每个返回值
    都得是真实 provider。

    单例在 barrier **之前**装好，让 resetter 拆一个存活实例、getter 同时读它——正是未加锁
    的 get 读路径可能变成 ``None`` / 半成品返回的交错。
    """
    sandbox_provider.reset_sandbox_provider()
    SlowSandboxProvider.instances_created = 0
    _patch_provider_resolution(monkeypatch)

    # 先装好单例，让 reset 与存活实例竞争。
    sandbox_provider.get_sandbox_provider()

    results: list[object] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def getter() -> None:
        barrier.wait()
        provider = sandbox_provider.get_sandbox_provider()
        with results_lock:
            results.append(provider)

    def resetter() -> None:
        barrier.wait()
        sandbox_provider.reset_sandbox_provider()

    threads = [threading.Thread(target=getter) for _ in range(4)]
    threads.append(threading.Thread(target=resetter))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        # 不管每个 getter 看到的是原单例还是 reset 后重建的——都得是真实 provider，绝非 None / 半成品。
        assert results, "每个 getter 都记了结果"
        assert all(isinstance(p, SlowSandboxProvider) for p in results)
    finally:
        sandbox_provider.reset_sandbox_provider()


def test_shutdown_racing_get_of_live_singleton_never_returns_none_or_torn(monkeypatch):
    """与 reset 同样的保证，针对 ``shutdown_sandbox_provider()``。

    用一个有真实（非平凡）``shutdown()`` 的 provider，让拆除在锁外跑、getter 并发读全局。
    """
    sandbox_provider.reset_sandbox_provider()
    SlowSandboxProvider.instances_created = 0
    _patch_provider_resolution(monkeypatch, cls=ShutdownSandboxProvider)

    sandbox_provider.get_sandbox_provider()  # 竞争前先装存活单例

    results: list[object] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def getter() -> None:
        barrier.wait()
        provider = sandbox_provider.get_sandbox_provider()
        with results_lock:
            results.append(provider)

    def shutter() -> None:
        barrier.wait()
        sandbox_provider.shutdown_sandbox_provider()

    threads = [threading.Thread(target=getter) for _ in range(4)]
    threads.append(threading.Thread(target=shutter))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert results
        assert all(isinstance(p, ShutdownSandboxProvider) for p in results)
    finally:
        sandbox_provider.reset_sandbox_provider()


def test_set_racing_get_never_returns_none_or_torn(monkeypatch):
    """``set_sandbox_provider()`` 与并发 get 竞争，绝不能暴露 ``None`` 全局：每个 getter
    看到的都是完整构造的 provider。"""
    sandbox_provider.reset_sandbox_provider()
    SlowSandboxProvider.instances_created = 0
    _patch_provider_resolution(monkeypatch)

    sandbox_provider.get_sandbox_provider()  # 竞争前先装存活单例
    injected = SlowSandboxProvider()

    results: list[object] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def getter() -> None:
        barrier.wait()
        provider = sandbox_provider.get_sandbox_provider()
        with results_lock:
            results.append(provider)

    def setter() -> None:
        barrier.wait()
        sandbox_provider.set_sandbox_provider(injected)

    threads = [threading.Thread(target=getter) for _ in range(4)]
    threads.append(threading.Thread(target=setter))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert results
        assert all(isinstance(p, SlowSandboxProvider) for p in results)
    finally:
        sandbox_provider.reset_sandbox_provider()


def test_losing_cold_start_racer_shuts_down_its_orphan(monkeypatch):
    """两个冷启动调用方竞争时，输家必须把自己建的实例 shutdown 掉，免得有副作用的构造器
    （idle-checker 线程等）泄漏——issue #3721 的核心后果。

    用 ``ShutdownSandboxProvider``：每个构造但被丢弃的实例都被调了 ``shutdown()``，所以
    恰好 ``(constructed - 1)`` 个被拆除（唯一的赢家被保留）。
    """
    sandbox_provider.reset_sandbox_provider()
    ShutdownSandboxProvider.instances_created = 0
    ShutdownSandboxProvider.registry = []
    _patch_provider_resolution(monkeypatch, cls=ShutdownSandboxProvider)

    n_threads = 8
    providers: list[ShutdownSandboxProvider] = []
    providers_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def get_provider() -> None:
        barrier.wait()
        provider = sandbox_provider.get_sandbox_provider()
        with providers_lock:
            providers.append(provider)

    threads = [threading.Thread(target=get_provider) for _ in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        winner = sandbox_provider.get_sandbox_provider()
        # 恰好一个实例被装上去、返回给每个调用方。
        assert len({id(p) for p in providers}) == 1
        assert all(p is winner for p in providers)
        # 赢家从不被拆除……
        assert winner.shutdown_calls == 0
        # ……每个被构造的输家都恰好被调了一次 shutdown()。
        losers = [inst for inst in ShutdownSandboxProvider.registry if inst is not winner]
        assert len(losers) == ShutdownSandboxProvider.instances_created - 1
        assert all(inst.shutdown_calls == 1 for inst in losers)
    finally:
        sandbox_provider.reset_sandbox_provider()
