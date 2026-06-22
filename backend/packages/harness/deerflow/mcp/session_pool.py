"""有状态 MCP 会话池（仅 stdio 传输）。

当 MCP 工具经 ``langchain-mcp-adapters`` 以 ``session=None`` 加载时，每次工具调用都会
新建一个 MCP 会话。对 Playwright 这类**有状态**服务器，这意味着浏览器状态（打开的页面、
填好的表单）在调用间丢失。

本池按 ``(server_name, scope_key)``（scope_key 通常是 thread_id）维护持久会话，
使同一线程内连续工具调用复用同一会话与服务端状态。容量达上限时按 LRU 淘汰（红线 #29）。

生命周期模型（owner task）
--------------------------
MCP ``ClientSession`` 基于 ``anyio`` task group，anyio 强制 cancel scope 必须由
**进入它的同一个 task** 退出。在非 owner task 上调 ``cm.__aexit__`` 会抛::

    RuntimeError: Attempted to exit cancel scope in a different task than it was entered in

同步工具路径（``make_sync_tool_wrapper``）每次调用走一个全新 ``asyncio.run`` 循环，
于是在某次调用里进入的会话会在另一次调用里退出——跨 task——崩溃（issue #3379）。

为杜绝这一点，每个池中会话由专属 ``_run_session`` task 持有：该 task 进入上下文管理器、
把活会话交回调用方、然后**等**一个 close 事件。所有关闭路径只**信号**该事件；
owner task 自己跑 ``__aexit__``，保证进入与退出永远在同一 task。

http/sse 传输**不入池**：它们内部用 anyio TaskGroup，无法从不同 async task 关闭
（issue #3203），入池会在清理时报 RuntimeError。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class MCPSessionPool:
    """按 ``(server_name, scope_key)`` 管理持久 MCP 会话（红线 #29）。"""

    MAX_SESSIONS = 256
    SESSION_CLOSE_TIMEOUT = 5.0  # 跨循环关闭会话的等待上限（秒）

    def __init__(self) -> None:
        # 每项: (session, owning_loop, owner_task, close_event)
        self._entries: OrderedDict[
            tuple[str, str],
            tuple[
                Any,  # ClientSession
                asyncio.AbstractEventLoop,
                asyncio.Task[Any],
                asyncio.Event,
            ],
        ] = OrderedDict()
        # 在建会话（按 (server, scope)）。让同循环上的并发调用共享一次创建，而非各建一个。
        # 值: (loop, ready_future, owner_task, close_event)
        self._inflight: dict[
            tuple[str, str],
            tuple[
                asyncio.AbstractEventLoop,
                asyncio.Future[Any],
                asyncio.Task[Any],
                asyncio.Event,
            ],
        ] = {}
        # threading.Lock 不绑定事件循环，async 路径与同步/工作线程路径都能安全获取。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 会话 owner task
    # ------------------------------------------------------------------

    async def _run_session(
        self,
        connection: dict[str, Any],
        ready: asyncio.Future[Any],
        close_evt: asyncio.Event,
    ) -> None:
        """持有一个 MCP 会话的整个生命周期。

        进入会话上下文管理器 → 初始化 → 经 ``ready`` 发布活会话 → 阻塞到 ``close_evt``。
        上下文管理器**始终**在本 task 退出（满足 anyio same-task cancel-scope 要求）。
        """
        from langchain_mcp_adapters.sessions import create_session  # 软加载

        cm = create_session(connection)
        try:
            session = await cm.__aenter__()
        except BaseException as e:
            # 从未进入 cancel scope，无需退出。
            if not ready.done():
                ready.set_exception(e)
            return

        # 上下文管理器已进入。此后 __aexit__ 必须在本 task 跑——初始化失败/取消/close 信号——
        # 以满足 anyio same-task 要求，避免泄漏会话/子进程。
        try:
            await session.initialize()
            if not ready.done():
                ready.set_result(session)
            await close_evt.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e)
        finally:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                logger.warning("关闭 MCP 会话时出错", exc_info=True)

    async def get_session(
        self,
        server_name: str,
        scope_key: str,
        connection: dict[str, Any],
    ) -> Any:
        """获取或创建一个持久 MCP 会话。

        若已存在的会话属于另一个（或已关闭的）事件循环，则淘汰并替换为当前循环上的新会话。

        Args:
            server_name: MCP 服务器名。
            scope_key: 隔离键（通常是 thread_id）。
            connection: ``create_session`` 的连接配置。

        Returns:
            已初始化的 ``ClientSession``。
        """
        key = (server_name, scope_key)
        current_loop = asyncio.get_running_loop()

        # 阶段 1：线程锁下检查/修改注册表（无 await）。原子决定三种结果之一：
        # 返回现有会话 / 加入在建创建 / 成为该 key 的创建者。
        # 每项: (loop, owner_task, close_event, cancel)。cancel=True 用于在建创建——
        # 其 owner 可能卡在 initialize() 里（close_evt 唤不醒），必须 cancel。
        evicted: list[tuple[asyncio.AbstractEventLoop, asyncio.Task[Any], asyncio.Event, bool]] = []
        join: asyncio.Future[Any] | None = None
        ready: asyncio.Future[Any] | None = None
        close_evt: asyncio.Event | None = None
        task: asyncio.Task[Any] | None = None
        with self._lock:
            if key in self._entries:
                session, loop, ent_task, ent_close = self._entries[key]
                if loop is current_loop and not loop.is_closed():
                    self._entries.move_to_end(key)
                    return session
                # 会话属于另一个/已关闭的循环 —— 淘汰。
                self._entries.pop(key)
                evicted.append((loop, ent_task, ent_close, False))

            inflight = self._inflight.get(key)
            if inflight is not None and inflight[0] is current_loop and not inflight[0].is_closed():
                # 本循环上另一个调用方正在创建该会话；等同一结果而非建副本。
                join = inflight[1]
            else:
                if inflight is not None:
                    # 另一个/已关闭循环上的陈旧在建创建。丢弃记录并拆除其 owner；
                    # 因 owner 可能卡在 initialize()（close_evt 唤不醒），必须 cancel。然后在这重建。
                    self._inflight.pop(key)
                    evicted.append((inflight[0], inflight[2], inflight[3], True))
                # 成为创建者：在任何 await 之前发布在建记录，让并发调用 join 而非竞争。
                ready = current_loop.create_future()
                close_evt = asyncio.Event()
                task = current_loop.create_task(self._run_session(connection, ready, close_evt))
                self._inflight[key] = (current_loop, ready, task, close_evt)

            # 容量达上限时 LRU 淘汰。
            while len(self._entries) >= self.MAX_SESSIONS:
                oldest_key, (_, loop, ent_task, ent_close) = next(iter(self._entries.items()))
                self._entries.pop(oldest_key)
                evicted.append((loop, ent_task, ent_close, False))

        # 阶段 2：关闭被淘汰的会话/创建。同循环 owner 被 await 以确定性地结束；
        # 跨循环 owner 路由到其自己的循环。每种情况都是 owner task——绝非本 task——跑 __aexit__。
        # 在建 owner 被 cancel（cancel=True），卡住的 initialize() 不会让它挂死。
        for loop, ent_task, ent_close, cancel in evicted:
            if loop is current_loop and not loop.is_closed():
                await self._shutdown(ent_close, ent_task, cancel)
            elif cancel:
                await self._shutdown_entry(loop, ent_task, ent_close, cancel=True)
            else:
                self._signal_close(loop, ent_close)

        # 阶段 2b：该 key 的并发创建已在本循环进行 —— 共享其结果而非建副本。
        if join is not None:
            return await asyncio.shield(join)

        assert ready is not None and close_evt is not None and task is not None

        # 阶段 3：等 owner task 发布已初始化的会话。
        try:
            session = await asyncio.shield(ready)
        except BaseException:
            # 两种情况到这里：
            # 1. owner task 失败（连接/初始化错误），经 ready.set_exception() 报告。
            #    它已在 finally 跑 cm.__aexit__（自己的 task），所以不能 cancel 它——
            #    那会中断清理。只等它解完。
            # 2. 本调用自身被取消（CancelledError）。因有 shield，ready 仍 pending、owner 活着且阻塞。
            #    信号 close 并 cancel 它，让它在自己的 task 退出 cancel scope，然后等它结束。
            # 会话尚未注册，无人能关闭它；这里等保证绝不泄漏会话或 owner task。
            owner_already_failed = ready.done() and not ready.cancelled() and ready.exception() is not None
            if not owner_already_failed:
                close_evt.set()
                task.cancel()
            try:
                await asyncio.shield(task)
            except BaseException:
                logger.debug("get_session 解卷时 owner task 结束", exc_info=True)
            with self._lock:
                if self._inflight.get(key) == (current_loop, ready, task, close_evt):
                    self._inflight.pop(key)
            raise

        # 阶段 4：把在建创建提升为注册项——但仅当我们的在建记录仍是活的那个。
        # 并发的 close_* / close_all 可能在我们初始化期间移除了它；此时不能把会话复活进 _entries。
        # 改由我们 own 拆除：信号 owner task 并等它在自己的 task 跑 __aexit__，然后抛取消。
        with self._lock:
            still_ours = self._inflight.get(key) == (current_loop, ready, task, close_evt)
            if still_ours:
                self._inflight.pop(key)
                self._entries[key] = (session, current_loop, task, close_evt)
        if not still_ours:
            await self._shutdown(close_evt, task)
            raise asyncio.CancelledError("MCP 会话池在会话创建期间被关闭")
        logger.info("已为 %s/%s 创建持久 MCP 会话", server_name, scope_key)
        return session

    # ------------------------------------------------------------------
    # 清理 helper
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_close(loop: asyncio.AbstractEventLoop, close_evt: asyncio.Event) -> None:
        """请求 owner task 关闭但不等待。

        ``asyncio.Event.set`` 非线程安全，故 schedule 到 owner 循环。已关闭循环意味着 owner task 已没了。
        """
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(close_evt.set)
        except RuntimeError:
            # is_closed() 检查与此刻之间循环被关闭。
            pass

    async def _shutdown(
        self,
        close_evt: asyncio.Event,
        task: asyncio.Task[Any],
        cancel: bool = False,
    ) -> None:
        """信号 owner task 并等它结束（在其循环上跑）。

        ``cancel=True`` 用于在建创建：owner 可能卡在 initialize()（close_evt 唤不醒），故须 cancel。
        其 finally 仍在自己的 task 跑 ``__aexit__``，满足 anyio same-task 要求。
        """
        close_evt.set()
        if cancel:
            task.cancel()
        try:
            await task
        except (Exception, asyncio.CancelledError):
            logger.debug("关闭时 owner task 结束", exc_info=True)

    async def _shutdown_entry(
        self,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[Any],
        close_evt: asyncio.Event,
        cancel: bool = False,
    ) -> None:
        """关闭一个条目，把 close 路由到其 owner 循环。"""
        if loop.is_closed():
            return
        current_loop = asyncio.get_running_loop()
        if loop is current_loop:
            await self._shutdown(close_evt, task, cancel)
        elif loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(close_evt, task, cancel), loop)
            try:
                await asyncio.wrap_future(future)
            except Exception:
                logger.warning("在 owner 循环关闭 MCP 会话时出错", exc_info=True)
        else:
            # owner 循环存在但既非当前循环也非运行中。这里在 async 上下文内，run_until_complete()
            # 会抛「另一个循环在运行」；且循环可能属于另一线程，从这驱动不安全。此分支实际不期望出现——
            # 会话的 owner 循环要么是长寿命 gateway 循环（运行中）要么是短寿命 asyncio.run 循环（已关闭，上面已捕获）。
            # 退化为 best-effort 线程安全信号，owner task 在其循环再跑时拆除（否则可能泄漏）。
            logger.warning("MCP 会话的 owner 循环空闲；best-effort 信号关闭。会话可能泄漏直到循环再跑。")
            self._signal_close(loop, close_evt)
            if cancel:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

    async def close_scope(self, scope_key: str) -> None:
        """关闭某个 scope（如 thread_id）的所有会话。"""
        with self._lock:
            keys = [k for k in self._entries if k[1] == scope_key]
            entries = [self._entries.pop(k) for k in keys]
            inflight_keys = [k for k in self._inflight if k[1] == scope_key]
            inflight = [self._inflight.pop(k) for k in inflight_keys]
        for _session, loop, task, close_evt in entries:
            await self._shutdown_entry(loop, task, close_evt)
        for loop, _ready, task, close_evt in inflight:
            await self._shutdown_entry(loop, task, close_evt, cancel=True)

    async def close_server(self, server_name: str) -> None:
        """关闭某个服务器的所有会话。"""
        with self._lock:
            keys = [k for k in self._entries if k[0] == server_name]
            entries = [self._entries.pop(k) for k in keys]
            inflight_keys = [k for k in self._inflight if k[0] == server_name]
            inflight = [self._inflight.pop(k) for k in inflight_keys]
        for _session, loop, task, close_evt in entries:
            await self._shutdown_entry(loop, task, close_evt)
        for loop, _ready, task, close_evt in inflight:
            await self._shutdown_entry(loop, task, close_evt, cancel=True)

    async def close_all(self) -> None:
        """关闭所有受管会话。"""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            inflight = list(self._inflight.values())
            self._inflight.clear()
        for _session, loop, task, close_evt in entries:
            await self._shutdown_entry(loop, task, close_evt)
        for loop, _ready, task, close_evt in inflight:
            await self._shutdown_entry(loop, task, close_evt, cancel=True)

    def close_all_sync(self) -> None:
        """在各自的 owner 事件循环上同步关闭所有会话。

        每个会话由其 owner task 在创建它的循环上关闭，避免跨循环/跨 task 错误。
        任何线程、无论有无活动事件循环都安全调用。

        关闭语义随 owner 循环在哪跑而不同：
        * owner 循环空闲或在另一线程跑 → 本调用阻塞直到拆除完成（或 ``SESSION_CLOSE_TIMEOUT`` 到）。
        * owner 循环正是本线程当前在跑的 → 不能阻塞（会死锁），故此处仅**信号**拆除，
          待控制权回到该循环后异步完成。调用方此后须保持该循环运行；若立即停循环，
          owner task 的 ``__aexit__`` 可能不跑。需要确定性关闭时改用 ``await close_all()``。
        """
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            inflight = list(self._inflight.values())
            self._inflight.clear()

        # entries 已初始化（温和 close_evt 路径）。在建创建可能卡在 init 中途，故 cancel 解除。
        owners = [(loop, task, close_evt, False) for _s, loop, task, close_evt in entries]
        owners += [(loop, task, close_evt, True) for loop, _r, task, close_evt in inflight]
        try:
            current_running_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_running_loop = None
        for loop, task, close_evt, cancel in owners:
            if loop.is_closed():
                continue
            try:
                if loop is current_running_loop:
                    # 在本循环的线程内执行，同步等 run_coroutine_threadsafe(...).result() 会死锁到超时。
                    # 直接信号 owner task，让它在本同步调用把控制权交还运行中的循环后完成。
                    close_evt.set()
                    if cancel:
                        task.cancel()
                elif loop.is_running():
                    # 从本线程把关闭 schedule 到 owner 循环。
                    future = asyncio.run_coroutine_threadsafe(self._shutdown(close_evt, task, cancel), loop)
                    future.result(timeout=self.SESSION_CLOSE_TIMEOUT)
                else:
                    loop.run_until_complete(self._shutdown(close_evt, task, cancel))
            except Exception:
                logger.debug("同步关闭期间关闭 MCP 会话出错", exc_info=True)


# ------------------------------------------------------------------
# 模块级单例
# ------------------------------------------------------------------

_pool: MCPSessionPool | None = None
_pool_lock = threading.Lock()


def get_session_pool() -> MCPSessionPool:
    """返回全局会话池单例。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = MCPSessionPool()
    return _pool


def reset_session_pool() -> None:
    """重置单例（测试用）。"""
    global _pool
    _pool = None
