"""MCP（Model Context Protocol）集成——经 ``langchain-mcp-adapters`` 加载外部 MCP 服务器工具。

支持 stdio / sse / http 三种传输 + OAuth（HTTP/SSE 鉴权）+ 有状态会话池
（stdio 跨调用保活）+ mtime 缓存失效（改 extensions_config.json 无需重启）。

软加载：``langchain-mcp-adapters`` / ``mcp`` 缺包时 MCP 工具不可用但不影响其它工具
（红线 #24）。
"""

from .cache import (
    get_cached_mcp_tools,
    initialize_mcp_tools,
    reset_mcp_tools_cache,
)
from .client import build_server_params, build_servers_config
from .oauth import (
    OAuthTokenManager,
    build_oauth_tool_interceptor,
    get_initial_oauth_headers,
)
from .session_pool import MCPSessionPool, get_session_pool, reset_session_pool
from .tools import get_mcp_tools

__all__ = [
    "MCPSessionPool",
    "OAuthTokenManager",
    "build_oauth_tool_interceptor",
    "build_server_params",
    "build_servers_config",
    "get_cached_mcp_tools",
    "get_initial_oauth_headers",
    "get_mcp_tools",
    "get_session_pool",
    "initialize_mcp_tools",
    "reset_mcp_tools_cache",
    "reset_session_pool",
]
