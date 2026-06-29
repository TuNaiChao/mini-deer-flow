"""经 ``langchain-mcp-adapters`` 加载 MCP 工具，stdio 会话用池复用。

``get_mcp_tools`` 是入口：读 extensions_config → 组装 servers_config → 注入初始
OAuth 头 → 构造 ``MultiServerMCPClient`` → 发现工具 → **仅 stdio 工具**包一层
持久会话复用（``_make_session_pool_tool``）→ 给纯协程工具补同步入口。

软加载：``langchain-mcp-adapters`` 缺包返回 ``[]`` 并记可操作安装提示（红线 #24）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_config

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers
from deerflow.mcp.session_pool import get_session_pool
from deerflow.reflection import resolve_variable
from deerflow.tools.sync import make_sync_tool_wrapper
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)


def _extract_thread_id(runtime: Runtime | None) -> str:
    """从注入的工具 runtime 或 LangGraph config 提取 thread_id（会话池的 scope_key）。"""
    if runtime is not None:
        tid = runtime.context.get("thread_id") if runtime.context else None
        if tid is not None:
            return str(tid)
        config = runtime.config or {}
        tid = config.get("configurable", {}).get("thread_id")
        if tid is not None:
            return str(tid)

    try:
        tid = get_config().get("configurable", {}).get("thread_id")
        return str(tid) if tid is not None else "default"
    except RuntimeError:
        return "default"


def _convert_call_tool_result(call_tool_result: Any) -> Any:
    """把 MCP ``CallToolResult`` 转成 LangChain ``content_and_artifact`` 格式。

    复刻 adapter 的转换逻辑，不依赖私有 ``langchain_mcp_adapters.tools._convert_call_tool_result``。
    """
    from langchain_core.messages import ToolMessage
    from langchain_core.messages.content import (
        create_file_block,
        create_image_block,
        create_text_block,
    )
    from langchain_core.tools import ToolException
    from mcp.types import (
        BlobResourceContents,
        EmbeddedResource,
        ImageContent,
        ResourceLink,
        TextContent,
        TextResourceContents,
    )

    # 拦截器短路：ToolMessage 直接透传。
    if isinstance(call_tool_result, ToolMessage):
        return call_tool_result, None

    # langgraph 装好时 Command 直接透传。
    try:
        from langgraph.types import Command

        if isinstance(call_tool_result, Command):
            return call_tool_result, None
    except ImportError:
        # langgraph 可选；不可用时走标准 MCP content 转换。
        pass

    # 把 MCP content block 转成 LangChain content block。
    lc_content = []
    for item in call_tool_result.content:
        if isinstance(item, TextContent):
            lc_content.append(create_text_block(text=item.text))
        elif isinstance(item, ImageContent):
            lc_content.append(create_image_block(base64=item.data, mime_type=item.mimeType))
        elif isinstance(item, ResourceLink):
            mime = item.mimeType or None
            if mime and mime.startswith("image/"):
                lc_content.append(create_image_block(url=str(item.uri), mime_type=mime))
            else:
                lc_content.append(create_file_block(url=str(item.uri), mime_type=mime))
        elif isinstance(item, EmbeddedResource):
            res = item.resource
            if isinstance(res, TextResourceContents):
                lc_content.append(create_text_block(text=res.text))
            elif isinstance(res, BlobResourceContents):
                mime = res.mimeType or None
                if mime and mime.startswith("image/"):
                    lc_content.append(create_image_block(base64=res.blob, mime_type=mime))
                else:
                    lc_content.append(create_file_block(base64=res.blob, mime_type=mime))
            else:
                lc_content.append(create_text_block(text=str(res)))
        else:
            lc_content.append(create_text_block(text=str(item)))

    if call_tool_result.isError:
        error_parts = [item["text"] for item in lc_content if isinstance(item, dict) and item.get("type") == "text"]
        raise ToolException("\n".join(error_parts) if error_parts else str(lc_content))

    artifact = None
    if call_tool_result.structuredContent is not None:
        artifact = {"structured_content": call_tool_result.structuredContent}

    return lc_content, artifact


def _make_session_pool_tool(
    tool: BaseTool,
    server_name: str,
    connection: dict[str, Any],
    tool_interceptors: list[Any] | None = None,
) -> BaseTool:
    """把一个 MCP 工具包成「复用池中持久会话」的版本。

    以 ``(server_name, thread_id)`` 为 scope 复用会话，保证有状态 MCP 服务器
    （如 Playwright）在同一线程的工具调用间保活状态。配置的 ``tool_interceptors``
    （OAuth、自定义）在每次调用前保留并应用。
    """
    # 剥掉 server-name 前缀，恢复原始 MCP 工具名。
    original_name = tool.name
    prefix = f"{server_name}_"
    if original_name.startswith(prefix):
        original_name = original_name[len(prefix) :]

    pool = get_session_pool()

    async def call_with_persistent_session(
        runtime: Runtime | None = None,
        **arguments: Any,
    ) -> Any:
        thread_id = _extract_thread_id(runtime)
        session = await pool.get_session(server_name, thread_id, connection)

        if tool_interceptors:
            from langchain_mcp_adapters.interceptors import MCPToolCallRequest

            async def base_handler(request: MCPToolCallRequest) -> Any:
                # 经 MCP call meta 保留拦截器注入的头，供 stdio MCP 调用透传。
                call_kwargs: dict[str, Any] = {}
                if request.headers:
                    if isinstance(request.headers, Mapping):
                        call_kwargs["meta"] = {"headers": dict(request.headers)}
                    else:
                        logger.warning("忽略类型不支持的 MCP 拦截器头: %s", type(request.headers).__name__)
                return await session.call_tool(request.name, request.args, **call_kwargs)

            handler: Any = base_handler
            for interceptor in reversed(tool_interceptors):
                outer = handler

                async def wrapped(req: Any, _i: Any = interceptor, _h: Any = outer) -> Any:
                    return await _i(req, _h)

                handler = wrapped

            request = MCPToolCallRequest(
                name=original_name,
                args=arguments,
                server_name=server_name,
                runtime=runtime,
            )
            call_tool_result = await handler(request)
        else:
            call_tool_result = await session.call_tool(original_name, arguments)

        return _convert_call_tool_result(call_tool_result)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=call_with_persistent_session,
        response_format="content_and_artifact",
        metadata=tool.metadata,
    )


async def get_mcp_tools() -> list[BaseTool]:
    """从所有启用的 MCP 服务器获取工具。

    stdio 传输的工具被包上持久会话逻辑，使同线程连续调用复用同一 MCP 会话。
    HTTP/SSE 工具不包（避免跨 task TaskGroup 清理错误，issue #3203）。

    Returns:
        所有启用 MCP 服务器的 LangChain 工具列表。缺包/无配置返回 ``[]``。
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("未安装 langchain-mcp-adapters。装上以启用 MCP 工具: pip install langchain-mcp-adapters")
        return []

    # 用 ExtensionsConfig.from_file() 而非 get_extensions_config()，始终读盘最新配置。
    # 这样 Gateway API（在另一进程跑）的改动初始化 MCP 工具时立即生效。
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("没有启用的 MCP 服务器配置")
        return []

    try:
        logger.info("用 %d 个服务器初始化 MCP client", len(servers_config))

        # 注入 server 连接的初始 OAuth 头（工具发现/会话初始化）。
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        tool_interceptors: list[Any] = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # 加载 extensions_config.json 声明的自定义拦截器。
        # 格式: "mcpInterceptors": ["pkg.module:builder_func", ...]
        for interceptor_path in extensions_config.mcp_interceptors:
            try:
                builder = resolve_variable(interceptor_path)
                interceptor = builder()
                if callable(interceptor):
                    tool_interceptors.append(interceptor)
                    logger.info("已加载 MCP 拦截器: %s", interceptor_path)
                elif interceptor is not None:
                    logger.warning(
                        "builder %s 返回了非可调用对象 %s；跳过",
                        interceptor_path,
                        type(interceptor).__name__,
                    )
            except Exception as e:
                logger.warning("加载 MCP 拦截器 %s 失败: %s", interceptor_path, e, exc_info=True)

        client = MultiServerMCPClient(
            servers_config,
            tool_interceptors=tool_interceptors,
            tool_name_prefix=True,
        )

        # 按服务器独立发现工具——单个坏 MCP 服务器不让健康服务器的工具一起丢（#3772）。
        async def load_server_tools(server_name: str) -> list[BaseTool]:
            try:
                return await client.get_tools(server_name=server_name)
            except Exception as e:
                logger.warning("MCP 服务器 '%s' 工具发现失败，跳过: %s", server_name, e, exc_info=True)
                return []

        tools_by_server = await asyncio.gather(*(load_server_tools(name) for name in servers_config))
        tools = [tool for server_tools in tools_by_server for tool in server_tools]
        logger.info("成功从 MCP 服务器加载 %d 个工具", len(tools))

        # 给每个工具包上持久会话逻辑。仅池化 stdio 会话。HTTP/SSE 传输内部用
        # anyio TaskGroup，无法从不同 async task 关闭，池化会在清理时 RuntimeError（issue #3203）。
        wrapped_tools: list[BaseTool] = []
        for tool in tools:
            tool_server: str | None = None
            for name in servers_config:
                if tool.name.startswith(f"{name}_"):
                    tool_server = name
                    break

            if tool_server is not None:
                transport = servers_config[tool_server].get("transport", "stdio")
                if transport == "stdio":
                    wrapped_tools.append(_make_session_pool_tool(tool, tool_server, servers_config[tool_server], tool_interceptors))
                else:
                    wrapped_tools.append(tool)
            else:
                wrapped_tools.append(tool)

        # 给工具补同步入口——deerflow client 同步流式需要 BaseTool.func。
        for tool in wrapped_tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)

        return wrapped_tools

    except Exception as e:
        logger.error("加载 MCP 工具失败: %s", e, exc_info=True)
        return []
