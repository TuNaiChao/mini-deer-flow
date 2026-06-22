"""把 extensions_config 的 MCP 服务器配置转成 ``MultiServerMCPClient`` 的入参。

``build_server_params`` 负责单个服务器的传输参数组装（stdio→command/args/env，
sse/http→url/headers），``build_servers_config`` 遍历所有启用服务器。
"""

from __future__ import annotations

import logging
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

logger = logging.getLogger(__name__)


def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]:
    """组装单个 MCP 服务器的 ``MultiServerMCPClient`` 参数。

    Args:
        server_name: 服务器名。
        config: 单个 MCP 服务器配置。

    Returns:
        ``langchain-mcp-adapters`` 的 server 参数 dict。

    Raises:
        ValueError: stdio 缺 ``command`` / sse/http 缺 ``url`` / 未知传输类型。
    """
    transport_type = config.type or "stdio"
    params: dict[str, Any] = {"transport": transport_type}

    if transport_type == "stdio":
        if not config.command:
            raise ValueError(f"MCP server '{server_name}' 用 stdio 传输，必须提供 'command' 字段")
        params["command"] = config.command
        params["args"] = config.args
        if config.env:
            params["env"] = config.env
    elif transport_type in ("sse", "http"):
        if not config.url:
            raise ValueError(f"MCP server '{server_name}' 用 {transport_type} 传输，必须提供 'url' 字段")
        params["url"] = config.url
        if config.headers:
            params["headers"] = config.headers
    else:
        raise ValueError(f"MCP server '{server_name}' 用了不支持的传输类型: {transport_type}")

    return params


def build_servers_config(extensions_config: ExtensionsConfig) -> dict[str, dict[str, Any]]:
    """组装所有启用 MCP 服务器的 ``MultiServerMCPClient`` 配置。

    Args:
        extensions_config: 扩展配置（含所有 MCP 服务器）。

    Returns:
        ``{server_name: server_params}`` dict。坏配置记 warning 跳过（不拖垮其它服务器）。
    """
    enabled_servers = extensions_config.get_enabled_mcp_servers()

    if not enabled_servers:
        logger.info("没有启用的 MCP 服务器")
        return {}

    servers_config: dict[str, dict[str, Any]] = {}
    for server_name, server_config in enabled_servers.items():
        try:
            servers_config[server_name] = build_server_params(server_name, server_config)
            logger.info("已配置 MCP 服务器: %s", server_name)
        except Exception as e:
            logger.error("配置 MCP 服务器 '%s' 失败: %s", server_name, e)

    return servers_config
