"""MCP HTTP/SSE 服务器的 OAuth token 支持。

``OAuthTokenManager`` 负责 token 的获取/缓存/刷新（过期前 ``refresh_skew_seconds``
秒提前刷新——红线 #30）。``build_oauth_tool_interceptor`` 构造工具拦截器，
在每次工具调用前把 ``Authorization`` 头注入请求（httpx 层）。``get_initial_oauth_headers``
为 server 连接初始化（工具发现/会话建立）提供初始鉴权头。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpOAuthConfig

logger = logging.getLogger(__name__)


@dataclass
class _OAuthToken:
    """缓存的 OAuth token。"""

    access_token: str
    token_type: str
    expires_at: datetime


class OAuthTokenManager:
    """为 MCP 服务器获取/缓存/刷新 OAuth token。

    每个 server 一把 ``asyncio.Lock`` 防并发刷新（双检锁：锁外先看缓存，未过期直接返；
    进锁后再看一次，避免多协程同时刷新）。
    """

    def __init__(self, oauth_by_server: dict[str, McpOAuthConfig]):
        self._oauth_by_server = oauth_by_server
        self._tokens: dict[str, _OAuthToken] = {}
        self._locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in oauth_by_server}

    @classmethod
    def from_extensions_config(cls, extensions_config: ExtensionsConfig) -> OAuthTokenManager:
        """从扩展配置提取所有启用 OAuth 的服务器。"""
        return cls(extensions_config.get_oauth_servers())

    def has_oauth_servers(self) -> bool:
        return bool(self._oauth_by_server)

    def oauth_server_names(self) -> list[str]:
        return list(self._oauth_by_server.keys())

    async def get_authorization_header(self, server_name: str) -> str | None:
        """返回 ``"{token_type} {access_token}"`` 鉴权头；无 OAuth 配置返回 None。

        过期前 ``refresh_skew_seconds`` 秒提前刷新（红线 #30）。
        """
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        token = self._tokens.get(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        # 双检锁：进锁后再看一次，避免多协程同时刷新。
        lock = self._locks[server_name]
        async with lock:
            token = self._tokens.get(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            fresh = await self._fetch_token(oauth)
            self._tokens[server_name] = fresh
            logger.info("已刷新 MCP 服务器 %s 的 OAuth token", server_name)
            return f"{fresh.token_type} {fresh.access_token}"

    @staticmethod
    def _is_expiring(token: _OAuthToken, oauth: McpOAuthConfig) -> bool:
        """token 是否已过期或在 ``refresh_skew_seconds`` 刷新窗内。"""
        now = datetime.now(UTC)
        return token.expires_at <= now + timedelta(seconds=max(oauth.refresh_skew_seconds, 0))

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        """向 token endpoint 发请求拿新 token。

        按 ``grant_type`` 组装表单：``client_credentials`` 需 client_id/secret；
        ``refresh_token`` 需 refresh_token（client_id/secret 可选）。
        """
        import httpx  # pyright: ignore[reportMissingImports]  # 软加载：httpx 缺包时抛 ImportError

        data: dict[str, str] = {
            "grant_type": oauth.grant_type,
            **oauth.extra_token_params,
        }

        if oauth.scope:
            data["scope"] = oauth.scope
        if oauth.audience:
            data["audience"] = oauth.audience

        if oauth.grant_type == "client_credentials":
            if not oauth.client_id or not oauth.client_secret:
                raise ValueError("OAuth client_credentials 需要 client_id 和 client_secret")
            data["client_id"] = oauth.client_id
            data["client_secret"] = oauth.client_secret
        elif oauth.grant_type == "refresh_token":
            if not oauth.refresh_token:
                raise ValueError("OAuth refresh_token 授权需要 refresh_token")
            data["refresh_token"] = oauth.refresh_token
            if oauth.client_id:
                data["client_id"] = oauth.client_id
            if oauth.client_secret:
                data["client_secret"] = oauth.client_secret
        else:
            raise ValueError(f"不支持的 OAuth grant_type: {oauth.grant_type}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(oauth.token_url, data=data)
            response.raise_for_status()
            payload = response.json()

        access_token = payload.get(oauth.token_field)
        if not access_token:
            raise ValueError(f"OAuth token 响应缺少 '{oauth.token_field}' 字段")

        token_type = str(payload.get(oauth.token_type_field, oauth.default_token_type) or oauth.default_token_type)

        expires_in_raw = payload.get(oauth.expires_in_field, 3600)
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3600

        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 1))
        return _OAuthToken(access_token=access_token, token_type=token_type, expires_at=expires_at)


def build_oauth_tool_interceptor(extensions_config: ExtensionsConfig) -> Any | None:
    """构造一个工具拦截器：在每次工具调用前注入 OAuth ``Authorization`` 头。

    无 OAuth 服务器时返回 None（调用方据此决定是否加进拦截器链）。

    拦截器签名对齐 ``langchain-mcp-adapters`` 的 ``MCPToolCallRequest`` 协议：
    ``async def interceptor(request, handler) -> Any``，通过
    ``request.override(headers=...)`` 透传更新后的头。
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return None

    async def oauth_interceptor(request: Any, handler: Any) -> Any:
        header = await token_manager.get_authorization_header(request.server_name)
        if not header:
            return await handler(request)

        updated_headers = dict(request.headers or {})
        updated_headers["Authorization"] = header
        return await handler(request.override(headers=updated_headers))

    return oauth_interceptor


async def get_initial_oauth_headers(extensions_config: ExtensionsConfig) -> dict[str, str]:
    """为所有 OAuth 服务器获取初始鉴权头（server 连接初始化用）。

    返回 ``{server_name: "Bearer ..."}``（空的过滤掉）。
    """
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return {}

    headers: dict[str, str] = {}
    for server_name in token_manager.oauth_server_names():
        headers[server_name] = await token_manager.get_authorization_header(server_name) or ""

    return {name: value for name, value in headers.items() if value}
