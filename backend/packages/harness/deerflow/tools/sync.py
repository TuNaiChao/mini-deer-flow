"""异步工具的同步调用包装（同步 agent 路径驱动异步工具）。

M20（MCP 工具）落地时引入 ``make_sync_tool_wrapper``：MCP 工具以协程形式被发现，但同步
流式路径（``DeerFlowClient.chat()``）需要 ``BaseTool.func`` 同步入口。该包装把协程包成同步
函数——有运行中的事件循环时，扔进专用线程池在新循环里跑（不能在运行中的循环里嵌套
``asyncio.run``）；无循环时直接 ``asyncio.run``。

M15（tools）在此补全对齐 deer 的两点：
1. ``_get_runnable_config_param``——检测协程是否声明了 ``RunnableConfig`` 类型参数；
2. 若有，生成的同步包装暴露 ``config: RunnableConfig`` 参数（LangChain 据此注入运行时配置），
   再转发到协程检测到的 config 参数名。覆盖 ``invoke_acp_agent`` 这类配置敏感工具。
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import functools
import logging
from collections.abc import Callable
from typing import Any, get_type_hints

from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

# 同步调用异步工具的共享线程池（运行中的循环不能嵌套 asyncio.run，须卸到线程）。
_SYNC_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="tool-sync")
atexit.register(lambda: _SYNC_TOOL_EXECUTOR.shutdown(wait=False))


def _get_runnable_config_param(func: Callable[..., Any]) -> str | None:
    """返回协程里期望 LangChain ``RunnableConfig`` 的参数名。

    扫描 ``func`` 的类型注解，找到类型恰好是 ``RunnableConfig`` 的参数返回其名；
    没有则返回 None。``functools.partial`` 会先解包到原函数。获取类型注解失败（如某些
    内建/无注解可调用）静默返回 None。
    """
    if isinstance(func, functools.partial):
        func = func.func

    try:
        type_hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 — 无注解 / 前向引用解析失败 → 视作无 config 参数
        return None

    for name, type_ in type_hints.items():
        if type_ is RunnableConfig:
            return name
    return None


def make_sync_tool_wrapper(coro: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """把异步工具协程包成同步函数（供 ``BaseTool.func``）。

    Args:
        coro: 工具背后的异步可调用。
        tool_name: 工具名（错误日志用）。

    Returns:
        同步可调用。若 ``coro`` 声明了 ``RunnableConfig`` 参数，返回的包装暴露
        ``config: RunnableConfig`` 参数（LangChain 据此注入运行时配置）并转发到协程的
        config 参数名——覆盖 ``invoke_acp_agent`` 这类配置敏感工具。

        本包装**不**合成动态函数签名。未来若出现「用户可见参数恰好叫 ``config`` 且
        ``RunnableConfig`` 参数另起他名」的异步工具，可能与 LangChain 注入的 ``config`` 撞名——
        那时重命名用户参数或扩展本 helper。

        当前调用线程有运行中的事件循环时，卸到 ``_SYNC_TOOL_EXECUTOR`` 在新循环里执行
        （复制 contextvar，避免跨线程丢失上下文）；否则直接 ``asyncio.run``。
    """
    config_param = _get_runnable_config_param(coro)

    def run_coroutine(*args: Any, **kwargs: Any) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        try:
            if loop is not None and loop.is_running():
                # 运行中的循环不能嵌套 asyncio.run —— 卸到线程池在新循环里跑。
                # 直接调 coro(*args, **kwargs)：若 coro 是 functools.partial，partial 的
                # __call__ 会正确合并已绑定参数 + 本次参数（旧版先取 .func 会丢绑定参数）。
                context = contextvars.copy_context()
                future = _SYNC_TOOL_EXECUTOR.submit(context.run, lambda: asyncio.run(coro(*args, **kwargs)))
                return future.result()
            return asyncio.run(coro(*args, **kwargs))
        except Exception as e:
            logger.error("同步包装调用工具 %r 失败: %s", tool_name, e, exc_info=True)
            raise

    if config_param:

        def sync_wrapper(*args: Any, config: RunnableConfig = None, **kwargs: Any) -> Any:
            # LangChain 注入 config → 转发到协程实际声明的 config 参数名。
            if config is not None or config_param not in kwargs:
                kwargs[config_param] = config
            return run_coroutine(*args, **kwargs)

        return sync_wrapper

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return run_coroutine(*args, **kwargs)

    return sync_wrapper
