"""MCP 工具缓存——避免重复加载。

缓存初始化一次，之后 ``get_cached_mcp_tools()`` 懒加载 + 复用。**mtime 失效**：
检测 extensions_config.json 被改过（Gateway API 在另一进程写），自动重置缓存重新初始化，
这样改配置无需重启进程。

重置时关闭所有持久会话（它们持有旧连接配置）。
"""

from __future__ import annotations

import asyncio
import logging
import os

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_mcp_tools_cache: list[BaseTool] | None = None
_cache_initialized = False
_initialization_lock = asyncio.Lock()
_config_mtime: float | None = None  # 跟踪 extensions_config.json 的 mtime


def _get_config_mtime() -> float | None:
    """获取 extensions_config.json 的修改时间。文件不存在返回 None。"""
    from deerflow.config.extensions_config import ExtensionsConfig

    config_path = ExtensionsConfig.resolve_config_path()
    if config_path and config_path.exists():
        return os.path.getmtime(config_path)
    return None


def _is_cache_stale() -> bool:
    """缓存是否因配置文件改动而过期。"""
    global _config_mtime

    if not _cache_initialized:
        return False  # 尚未初始化，谈不上过期

    current_mtime = _get_config_mtime()

    # 之前或现在都拿不到 mtime，当作未过期。
    if _config_mtime is None or current_mtime is None:
        return False

    # 配置文件在我们缓存后被改过 → 过期。
    if current_mtime > _config_mtime:
        logger.info("MCP 配置文件被改过（mtime: %s -> %s），缓存过期", _config_mtime, current_mtime)
        return True

    return False


async def initialize_mcp_tools() -> list[BaseTool]:
    """初始化并缓存 MCP 工具（应用启动时调一次）。"""
    global _mcp_tools_cache, _cache_initialized, _config_mtime

    async with _initialization_lock:
        if _cache_initialized:
            logger.info("MCP 工具已初始化")
            return _mcp_tools_cache or []

        from deerflow.mcp.tools import get_mcp_tools

        logger.info("初始化 MCP 工具...")
        _mcp_tools_cache = await get_mcp_tools()
        _cache_initialized = True
        _config_mtime = _get_config_mtime()  # 记录配置 mtime
        logger.info(
            "MCP 工具已初始化: 加载 %d 个工具（config mtime: %s）",
            len(_mcp_tools_cache),
            _config_mtime,
        )

        return _mcp_tools_cache


def get_cached_mcp_tools() -> list[BaseTool]:
    """获取缓存的 MCP 工具（懒加载）。

    未初始化时自动初始化，使 MCP 工具在 FastAPI 与 LangGraph Studio 两种上下文都可用。
    同时检测配置文件是否被改过，是则重新初始化——Gateway API 的改动能反映到
    Gateway 内嵌的 LangGraph runtime。

    Returns:
        缓存的 MCP 工具列表。
    """
    global _cache_initialized

    # 检测配置文件改动。
    if _is_cache_stale():
        logger.info("MCP 缓存过期，重置以重新初始化...")
        reset_mcp_tools_cache()

    if not _cache_initialized:
        logger.info("MCP 工具未初始化，执行懒加载...")
        try:
            # Python 3.14：get_event_loop() 已废弃，改用 get_running_loop 检测。
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is not None and running.is_running():
                # 循环在跑（如 LangGraph Studio）—— 在线程里开新循环跑。
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, initialize_mcp_tools())
                    future.result()
            else:
                # 无循环在跑——直接 asyncio.run。
                asyncio.run(initialize_mcp_tools())
        except Exception:
            logger.exception("懒加载 MCP 工具失败")
            return []

    return _mcp_tools_cache or []


def reset_mcp_tools_cache() -> None:
    """重置 MCP 工具缓存。

    测试或想重载 MCP 工具时用。同时关闭所有持久会话，下次加载时按（可能更新的）连接配置重建。

    ``close_all_sync()`` 已按 owner 循环选对策略：
    * 当前运行循环拥有的会话仅**信号**（其 owner task 在循环重获控制后跑 ``__aexit__``——
      正确且无泄漏，循环让 task 活着）；
    * 其它线程循环上的会话确定性拆除；
    * 空闲/已关闭循环被处理或跳过。
    这里刻意**不**同步等当前运行循环完成拆除：那是自死锁（循环只能在本同步调用返回后跑拆除）。
    """
    global _mcp_tools_cache, _cache_initialized, _config_mtime
    _mcp_tools_cache = None
    _cache_initialized = False
    _config_mtime = None

    try:
        from deerflow.mcp.session_pool import get_session_pool

        get_session_pool().close_all_sync()
    except Exception:
        logger.debug("重置缓存时未能关闭 MCP 会话池", exc_info=True)

    from deerflow.mcp.session_pool import reset_session_pool

    reset_session_pool()
    logger.info("MCP 工具缓存已重置")
