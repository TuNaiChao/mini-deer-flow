"""test_mcp.py — M20 MCP 集成的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M20）：
- extensions_config 新字段：McpOAuthConfig / McpServerConfig.from_dict / 两种格式归一 /
  get_oauth_servers / resolve_env_variables / resolve_config_path 优先级
- client.build_server_params：stdio / sse / http / 缺字段报错 / 未知传输报错
- client.build_servers_config：空 / 多服务器 / 坏配置跳过
- oauth：OAuthTokenManager 缓存 + 提前刷新（skew）+ 双检锁 + grant_type 校验；
  build_oauth_tool_interceptor（无 OAuth→None / 有→注入头）；get_initial_oauth_headers
- session_pool：单例 + 空池 close_all/close_all_sync no-op + get_session 创建/复用/scope 隔离 +
  owner-task 同 task 进出（mock create_session）+ LRU 淘汰
- tools.get_mcp_tools：soft-load 缺包→[] / 无服务器→[] / 发现工具（mock adapter）+
  stdio 包会话池 / http 不包 / 自定义拦截器加载
- tools.sync.make_sync_tool_wrapper：同步跑协程 / 运行中循环卸线程
- cache：mtime 失效 / 初始化幂等 / reset 清空 / get_cached 懒加载

hermetic：``langchain-mcp-adapters`` / ``mcp`` / ``httpx`` 均**未安装**——用
``sys.modules`` 注入 fake 模块 + monkeypatch，零网络零子进程。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from deerflow.config.extensions_config import (
    ExtensionsConfig,
    McpOAuthConfig,
    McpServerConfig,
)
from deerflow.mcp import tools as tools_module
from deerflow.mcp.client import build_server_params, build_servers_config
from deerflow.mcp.oauth import (
    OAuthTokenManager,
    build_oauth_tool_interceptor,
    get_initial_oauth_headers,
)
from deerflow.mcp.session_pool import (
    MCPSessionPool,
    get_session_pool,
    reset_session_pool,
)

# conftest autouse 已在每个测试前后 reset_mcp_tools_cache（清缓存 + 会话池单例）。


# ===========================================================================
# fake adapter / mcp 包注入 helper（langchain-mcp-adapters / mcp 未安装）
# ===========================================================================


def _make_fake_request(*, name="t", args=None, server_name="srv", headers=None, runtime=None):
    """构造一个 ``MCPToolCallRequest`` 鸭子对象（带 override）。"""

    class _Req:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            if "headers" not in kw:
                self.headers = None

        def override(self, *, headers=None):
            new = _Req()
            new.__dict__.update(self.__dict__)
            if headers is not None:
                new.headers = headers
            return new

    return _Req(name=name, args=args or {}, server_name=server_name, headers=headers, runtime=runtime)


def _install_fake_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools_by_name: dict[str, list] | None = None,
    create_session_cm_factory=None,
    failing_servers: set[str] | None = None,
):
    """注入 fake ``langchain_mcp_adapters`` + ``mcp`` 进 sys.modules。

    Args:
        tools_by_name: ``{server_name: [fake_tools]}``，决定 ``get_tools()`` 返回。
            默认空 dict → 所有服务器无工具。
        create_session_cm_factory: 产出 fake async context manager 的可调用（``create_session``）。
        failing_servers: ``get_tools(server_name=X)`` 时，对这里列出的服务器抛
            ``RuntimeError``——用于测 #3772 单服务器发现失败隔离（默认空集，不抛）。
    """
    tools_by_name = tools_by_name or {}
    failing_servers = failing_servers or set()

    # ---- MultiServerMCPClient：记录 servers_config，get_tools 返回按前缀分发的 fake tools ----
    class _FakeClient:
        def __init__(self, servers_config, **kwargs):
            self.servers_config = servers_config
            self.kwargs = kwargs

        async def get_tools(self, server_name=None):
            # 生产 #3772 路径：按服务器独立发现。坏服务器（在 failing_servers 里）抛错，
            # 由 get_mcp_tools 内的 load_server_tools 捕获后返回 []，不拖累其它服务器。
            if server_name is not None:
                if server_name in failing_servers:
                    raise RuntimeError(f"fake discovery failure for {server_name}")
                return list(tools_by_name.get(server_name, []))
            # 聚合回退路径（与真实库的 get_tools() 语义一致）。
            result = []
            for name in self.servers_config:
                result.extend(tools_by_name.get(name, []))
            return result

    fake_pkg = types.ModuleType("langchain_mcp_adapters")
    fake_client_mod = types.ModuleType("langchain_mcp_adapters.client")
    fake_client_mod.MultiServerMCPClient = _FakeClient
    fake_sessions_mod = types.ModuleType("langchain_mcp_adapters.sessions")
    fake_sessions_mod.create_session = create_session_cm_factory or (lambda conn: _FakeSessionCM())
    fake_interceptors_mod = types.ModuleType("langchain_mcp_adapters.interceptors")

    class _MCPToolCallRequest:  # 简化版，生产代码用关键字构造
        def __init__(self, **kw):
            self.__dict__.update(kw)

    fake_interceptors_mod.MCPToolCallRequest = _MCPToolCallRequest

    fake_pkg.client = fake_client_mod
    fake_pkg.sessions = fake_sessions_mod
    fake_pkg.interceptors = fake_interceptors_mod

    # ---- mcp 包：ClientSession + types ----
    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = MagicMock(name="ClientSession")

    fake_types = types.ModuleType("mcp.types")

    class _TextContent:
        def __init__(self, text):
            self.text = text

    fake_types.TextContent = _TextContent
    fake_types.ImageContent = MagicMock(name="ImageContent")
    fake_types.ResourceLink = MagicMock(name="ResourceLink")
    fake_types.EmbeddedResource = MagicMock(name="EmbeddedResource")

    class _TextResourceContents:
        def __init__(self, text):
            self.text = text

    fake_types.TextResourceContents = _TextResourceContents
    fake_types.BlobResourceContents = MagicMock(name="BlobResourceContents")
    fake_mcp.types = fake_types

    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_pkg)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client_mod)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.sessions", fake_sessions_mod)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.interceptors", fake_interceptors_mod)
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    monkeypatch.setitem(sys.modules, "mcp.types", fake_types)
    return fake_pkg


class _FakeSessionCM:
    """fake ``create_session`` 返回的 async context manager（进/出同 task 可观测）。"""

    def __init__(self):
        self.entered_task = None
        self.exited_task = None
        self.session = SimpleNamespace(
            initialize=AsyncMock(name="initialize"),
            call_tool=AsyncMock(name="call_tool"),
        )

    async def __aenter__(self):
        self.entered_task = asyncio.current_task()
        return self.session

    async def __aexit__(self, *exc):
        self.exited_task = asyncio.current_task()
        return False


def _fake_tool(name: str, *, coroutine=None):
    """构造最小 ``BaseTool`` 鸭子（name/description/args_schema/metadata/coroutine/func）。"""
    return SimpleNamespace(
        name=name,
        description=f"fake tool {name}",
        args_schema=None,
        metadata={},
        coroutine=coroutine,
        func=None,
    )


# ===========================================================================
# 1. extensions_config 新字段
# ===========================================================================


class TestExtensionsConfig:
    def test_mcp_oauth_config_from_dict_known_fields(self):
        oauth = McpOAuthConfig.from_dict(
            {
                "token_url": "https://idp/token",
                "grant_type": "client_credentials",
                "client_id": "cid",
                "client_secret": "csec",
                "refresh_skew_seconds": 30,
            }
        )
        assert oauth.token_url == "https://idp/token"
        assert oauth.grant_type == "client_credentials"
        assert oauth.refresh_skew_seconds == 30
        assert oauth.extra == {}

    def test_mcp_oauth_config_unknown_fields_into_extra(self):
        oauth = McpOAuthConfig.from_dict({"token_url": "u", "custom_field": "x"})
        assert oauth.token_url == "u"
        assert oauth.extra == {"custom_field": "x"}

    def test_mcp_oauth_config_defaults(self):
        oauth = McpOAuthConfig()
        assert oauth.enabled is True
        assert oauth.grant_type == "client_credentials"
        assert oauth.token_field == "access_token"
        assert oauth.default_token_type == "Bearer"
        assert oauth.refresh_skew_seconds == 60

    def test_server_config_transport_alias_for_type(self):
        # MCP 规范用 transport，项目用 type —— transport 作 type 别名
        srv = McpServerConfig.from_dict({"name": "s", "transport": "sse", "url": "http://x"})
        assert srv.type == "sse"

    def test_server_config_type_takes_precedence_over_transport(self):
        srv = McpServerConfig.from_dict({"name": "s", "transport": "sse", "type": "http", "url": "u"})
        assert srv.type == "http"

    def test_server_config_with_oauth(self):
        srv = McpServerConfig.from_dict(
            {
                "name": "s",
                "type": "http",
                "url": "u",
                "oauth": {"token_url": "https://idp/token", "client_id": "c"},
            }
        )
        assert srv.oauth is not None
        assert srv.oauth.token_url == "https://idp/token"
        assert srv.oauth.client_id == "c"

    def test_server_config_unknown_fields_into_extra(self):
        srv = McpServerConfig.from_dict({"name": "s", "command": "c", "weird": 1})
        assert srv.command == "c"
        assert srv.extra == {"weird": 1}

    def test_server_config_name_from_key_when_deer_dict(self):
        srv = McpServerConfig.from_dict({"command": "c"}, name="myserver")
        assert srv.name == "myserver"

    def test_from_dict_deer_format_mcpServers_dict(self):
        cfg = ExtensionsConfig.from_dict(
            {
                "mcpServers": {
                    "alpha": {"type": "stdio", "command": "alpha-bin"},
                    "beta": {"type": "http", "url": "http://beta", "enabled": False},
                },
            }
        )
        names = {s.name for s in cfg.mcp_servers}
        assert names == {"alpha", "beta"}
        enabled = cfg.get_enabled_mcp_servers()
        assert "alpha" in enabled
        assert "beta" not in enabled  # enabled=False

    def test_from_dict_mini_format_mcp_servers_list(self):
        cfg = ExtensionsConfig.from_dict(
            {
                "mcp_servers": [
                    {"name": "a", "command": "a-bin"},
                ],
            }
        )
        assert cfg.mcp_servers[0].name == "a"

    def test_from_dict_skills_deer_dict(self):
        cfg = ExtensionsConfig.from_dict(
            {
                "skills": {"on_skill": {"enabled": True}, "off_skill": {"enabled": False}},
            }
        )
        assert "on_skill" in cfg.enabled_skills
        assert "off_skill" not in cfg.enabled_skills

    def test_from_dict_skills_mini_list_enabled_skills(self):
        cfg = ExtensionsConfig.from_dict({"enabled_skills": ["x", "y"]})
        assert cfg.enabled_skills == ["x", "y"]

    def test_from_dict_mcp_interceptors(self):
        cfg = ExtensionsConfig.from_dict({"mcpInterceptors": ["pkg.mod:builder"]})
        assert cfg.mcp_interceptors == ["pkg.mod:builder"]

    def test_from_dict_mcp_interceptors_string_to_list(self):
        cfg = ExtensionsConfig.from_dict({"mcpInterceptors": "pkg.mod:builder"})
        assert cfg.mcp_interceptors == ["pkg.mod:builder"]

    def test_from_dict_mcp_interceptors_invalid_skipped(self):
        cfg = ExtensionsConfig.from_dict({"mcpInterceptors": [1, 2, "ok"]})
        assert cfg.mcp_interceptors == ["ok"]

    def test_get_oauth_servers_filters(self):
        cfg = ExtensionsConfig.from_dict(
            {
                "mcpServers": {
                    "no_oauth": {"type": "stdio", "command": "c"},
                    "with_oauth": {
                        "type": "http",
                        "url": "u",
                        "oauth": {"token_url": "t", "enabled": True},
                    },
                    "disabled_oauth": {
                        "type": "http",
                        "url": "u",
                        "oauth": {"token_url": "t", "enabled": False},
                    },
                },
            }
        )
        oauth_servers = cfg.get_oauth_servers()
        assert "with_oauth" in oauth_servers
        assert "no_oauth" not in oauth_servers
        assert "disabled_oauth" not in oauth_servers

    def test_resolve_env_variables_string(self, monkeypatch):
        monkeypatch.setenv("MY_MCP_VAR", "resolved")
        assert ExtensionsConfig.resolve_env_variables("$MY_MCP_VAR") == "resolved"

    def test_resolve_env_variables_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET_VAR_X9Z", raising=False)
        assert ExtensionsConfig.resolve_env_variables("$DEFINITELY_UNSET_VAR_X9Z") == ""

    def test_resolve_env_variables_plain_string_passthrough(self):
        assert ExtensionsConfig.resolve_env_variables("plain") == "plain"
        assert ExtensionsConfig.resolve_env_variables("http://host/path") == "http://host/path"

    def test_resolve_env_variables_recursive(self, monkeypatch):
        monkeypatch.setenv("V1", "ok")
        out = ExtensionsConfig.resolve_env_variables({"a": "$V1", "b": ["$V1", "plain"]})
        assert out == {"a": "ok", "b": ["ok", "plain"]}

    def test_resolve_config_path_param_exists(self, tmp_path):
        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        assert ExtensionsConfig.resolve_config_path(f) == f

    def test_resolve_config_path_param_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ExtensionsConfig.resolve_config_path(tmp_path / "nope.json")

    def test_resolve_config_path_env(self, tmp_path, monkeypatch):
        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(f))
        assert ExtensionsConfig.resolve_config_path() == f

    def test_resolve_config_path_env_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(tmp_path / "nope.json"))
        with pytest.raises(FileNotFoundError):
            ExtensionsConfig.resolve_config_path()

    def test_from_file_missing_returns_empty(self, monkeypatch):
        # resolve_config_path 返回 None（无 param 无 env 无项目根文件）→ 空配置（红线 #25）
        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: None))
        cfg = ExtensionsConfig.from_file()
        assert cfg.mcp_servers == []
        assert cfg.enabled_skills == []
        assert cfg.mcp_interceptors == []

    def test_from_file_reads_deer_format(self, tmp_path, monkeypatch):
        f = tmp_path / "ext.json"
        f.write_text(
            json.dumps(
                {
                    "mcpServers": {"srv": {"type": "stdio", "command": "c"}},
                    "skills": {"s1": {"enabled": True}},
                }
            ),
            encoding="utf-8",
        )
        cfg = ExtensionsConfig.from_file(f)
        assert cfg.mcp_servers[0].name == "srv"
        assert "s1" in cfg.enabled_skills

    def test_is_skill_enabled_backward_compat(self):
        # M14 既有用法：is_skill_enabled(name) 单参（默认 public）+ enabled_skills 列表
        cfg = ExtensionsConfig(enabled_skills=["alpha"])
        assert cfg.is_skill_enabled("alpha") is True
        assert cfg.is_skill_enabled("anything", "public") is True
        assert cfg.is_skill_enabled("anything", "system") is False


# ===========================================================================
# 2. client.build_server_params / build_servers_config
# ===========================================================================


class TestClient:
    def test_build_server_params_stdio(self):
        params = build_server_params("s", McpServerConfig(name="s", type="stdio", command="bin", args=["-x"], env={"K": "V"}))
        assert params["transport"] == "stdio"
        assert params["command"] == "bin"
        assert params["args"] == ["-x"]
        assert params["env"] == {"K": "V"}

    def test_build_server_params_stdio_no_env_omitted(self):
        params = build_server_params("s", McpServerConfig(name="s", type="stdio", command="bin"))
        assert "env" not in params

    def test_build_server_params_stdio_missing_command_raises(self):
        with pytest.raises(ValueError, match="command"):
            build_server_params("s", McpServerConfig(name="s", type="stdio"))

    @pytest.mark.parametrize("transport", ["sse", "http"])
    def test_build_server_params_remote(self, transport):
        params = build_server_params("s", McpServerConfig(name="s", type=transport, url="http://x", headers={"H": "1"}))
        assert params["transport"] == transport
        assert params["url"] == "http://x"
        assert params["headers"] == {"H": "1"}

    def test_build_server_params_remote_missing_url_raises(self):
        with pytest.raises(ValueError, match="url"):
            build_server_params("s", McpServerConfig(name="s", type="http"))

    def test_build_server_params_unknown_transport_raises(self):
        with pytest.raises(ValueError, match="不支持"):
            build_server_params("s", McpServerConfig(name="s", type="weird", command="c"))

    def test_build_server_params_default_type_stdio(self):
        # type 未填默认 stdio
        params = build_server_params("s", McpServerConfig(name="s", command="bin"))
        assert params["transport"] == "stdio"

    def test_build_servers_config_empty(self):
        assert build_servers_config(ExtensionsConfig()) == {}

    def test_build_servers_config_multiple(self):
        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="a", type="stdio", command="a-bin"),
                McpServerConfig(name="b", type="http", url="http://b"),
            ]
        )
        out = build_servers_config(cfg)
        assert set(out.keys()) == {"a", "b"}
        assert out["a"]["transport"] == "stdio"
        assert out["b"]["transport"] == "http"

    def test_build_servers_config_bad_server_skipped(self, caplog):
        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="good", type="stdio", command="ok"),
                McpServerConfig(name="bad", type="stdio"),  # 缺 command
            ]
        )
        out = build_servers_config(cfg)
        assert "good" in out
        assert "bad" not in out  # 坏配置跳过不拖垮其它

    def test_build_servers_config_disabled_excluded(self):
        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="on", type="stdio", command="c", enabled=True),
                McpServerConfig(name="off", type="stdio", command="c", enabled=False),
            ]
        )
        out = build_servers_config(cfg)
        assert "on" in out
        assert "off" not in out


# ===========================================================================
# 3. oauth
# ===========================================================================


class _FakeOAuthResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncHttpxClient:
    """fake httpx.AsyncClient——记录 post 调用，返回预设响应。"""

    instances = []

    def __init__(self, *, responses=None):
        self.posts = []
        self._responses = responses or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **kw):
        self.posts.append({"url": url, "data": data})
        if self._responses:
            return self._responses.pop(0)
        return _FakeOAuthResponse({"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600})


class TestOAuth:
    def _config_with_oauth(self, **oauth_overrides):
        oauth = {"token_url": "https://idp/token", "client_id": "cid", "client_secret": "csec", **oauth_overrides}
        return ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="srv", type="http", url="http://srv", oauth=McpOAuthConfig.from_dict(oauth)),
            ]
        )

    def test_token_manager_no_oauth_servers(self):
        mgr = OAuthTokenManager.from_extensions_config(ExtensionsConfig())
        assert mgr.has_oauth_servers() is False
        assert mgr.oauth_server_names() == []

    @pytest.mark.asyncio
    async def test_get_authorization_header_no_oauth_returns_none(self):
        mgr = OAuthTokenManager({})
        assert await mgr.get_authorization_header("any") is None

    @pytest.mark.asyncio
    async def test_fetch_and_cache_token(self, monkeypatch):
        cfg = self._config_with_oauth()
        mgr = OAuthTokenManager.from_extensions_config(cfg)

        client = _FakeAsyncHttpxClient()
        # patch httpx import：oauth._fetch_token 内 import httpx
        fake_httpx = SimpleNamespace(AsyncClient=lambda *a, **kw: client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        header = await mgr.get_authorization_header("srv")
        assert header == "Bearer tok-1"
        assert client.posts[0]["url"] == "https://idp/token"
        # client_credentials 表单
        assert client.posts[0]["data"]["grant_type"] == "client_credentials"
        assert client.posts[0]["data"]["client_id"] == "cid"
        assert client.posts[0]["data"]["client_secret"] == "csec"

    @pytest.mark.asyncio
    async def test_cached_token_reused_no_second_fetch(self, monkeypatch):
        cfg = self._config_with_oauth()
        mgr = OAuthTokenManager.from_extensions_config(cfg)
        client = _FakeAsyncHttpxClient()
        fake_httpx = SimpleNamespace(AsyncClient=lambda *a, **kw: client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        await mgr.get_authorization_header("srv")
        await mgr.get_authorization_header("srv")  # 第二次走缓存
        assert len(client.posts) == 1

    @pytest.mark.asyncio
    async def test_refresh_skew_triggers_refetch(self, monkeypatch):
        # refresh_skew 设很大 → token 一拿到就「即将过期」→ 第二次必然刷新
        cfg = self._config_with_oauth(refresh_skew_seconds=100000)
        mgr = OAuthTokenManager.from_extensions_config(cfg)
        client = _FakeAsyncHttpxClient(
            responses=[
                _FakeOAuthResponse({"access_token": "tok-1", "expires_in": 3600}),
                _FakeOAuthResponse({"access_token": "tok-2", "expires_in": 3600}),
            ]
        )
        fake_httpx = SimpleNamespace(AsyncClient=lambda *a, **kw: client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        h1 = await mgr.get_authorization_header("srv")
        h2 = await mgr.get_authorization_header("srv")
        assert h1 == "Bearer tok-1"
        assert h2 == "Bearer tok-2"  # skew 内 → 重新刷新
        assert len(client.posts) == 2

    @pytest.mark.asyncio
    async def test_refresh_token_grant_requires_refresh_token(self):
        cfg = self._config_with_oauth(grant_type="refresh_token")  # 无 refresh_token
        mgr = OAuthTokenManager.from_extensions_config(cfg)
        with pytest.raises(ValueError, match="refresh_token"):
            await mgr.get_authorization_header("srv")

    @pytest.mark.asyncio
    async def test_unsupported_grant_type(self):
        cfg = self._config_with_oauth(grant_type="password")
        mgr = OAuthTokenManager.from_extensions_config(cfg)
        with pytest.raises(ValueError, match="grant_type"):
            await mgr.get_authorization_header("srv")

    @pytest.mark.asyncio
    async def test_client_credentials_requires_id_secret(self):
        cfg = self._config_with_oauth(client_id=None, client_secret=None)
        mgr = OAuthTokenManager.from_extensions_config(cfg)
        with pytest.raises(ValueError, match="client_id"):
            await mgr.get_authorization_header("srv")

    def test_build_oauth_tool_interceptor_none_when_no_oauth(self):
        assert build_oauth_tool_interceptor(ExtensionsConfig()) is None

    @pytest.mark.asyncio
    async def test_build_oauth_tool_interceptor_injects_header(self, monkeypatch):
        cfg = self._config_with_oauth()
        interceptor = build_oauth_tool_interceptor(cfg)
        assert interceptor is not None

        client = _FakeAsyncHttpxClient()
        fake_httpx = SimpleNamespace(AsyncClient=lambda *a, **kw: client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

        captured = {}

        async def handler(request):
            captured["headers"] = request.headers
            return "ok"

        req = _make_fake_request(server_name="srv", headers={"X": "1"})
        result = await interceptor(req, handler)
        assert result == "ok"
        assert captured["headers"]["Authorization"] == "Bearer tok-1"
        assert captured["headers"]["X"] == "1"  # 原头保留

    @pytest.mark.asyncio
    async def test_build_oauth_tool_interceptor_no_header_passes_through(self, monkeypatch):
        # server 不在 oauth 列表 → get_authorization_header 返 None → 不注入
        cfg = self._config_with_oauth()
        interceptor = build_oauth_tool_interceptor(cfg)
        called = {"v": False}

        async def handler(request):
            called["v"] = True
            return "passthrough"

        req = _make_fake_request(server_name="other_server")
        assert await interceptor(req, handler) == "passthrough"
        assert called["v"] is True

    @pytest.mark.asyncio
    async def test_get_initial_oauth_headers_empty_when_no_oauth(self):
        assert await get_initial_oauth_headers(ExtensionsConfig()) == {}

    @pytest.mark.asyncio
    async def test_get_initial_oauth_headers_for_servers(self, monkeypatch):
        cfg = self._config_with_oauth()
        client = _FakeAsyncHttpxClient()
        fake_httpx = SimpleNamespace(AsyncClient=lambda *a, **kw: client)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
        headers = await get_initial_oauth_headers(cfg)
        assert headers == {"srv": "Bearer tok-1"}


# ===========================================================================
# 4. session_pool
# ===========================================================================


class TestSessionPool:
    def test_constants(self):
        assert MCPSessionPool.MAX_SESSIONS == 256
        assert MCPSessionPool.SESSION_CLOSE_TIMEOUT == 5.0

    def test_singleton(self):
        reset_session_pool()
        pool1 = get_session_pool()
        pool2 = get_session_pool()
        assert pool1 is pool2

    def test_reset_singleton(self):
        pool1 = get_session_pool()
        reset_session_pool()
        pool2 = get_session_pool()
        assert pool1 is not pool2

    @pytest.mark.asyncio
    async def test_close_all_empty_noop(self):
        pool = MCPSessionPool()
        await pool.close_all()  # 不抛

    def test_close_all_sync_empty_noop(self):
        pool = MCPSessionPool()
        pool.close_all_sync()  # 不抛

    @pytest.mark.asyncio
    async def test_get_session_creates_and_initializes(self, monkeypatch):
        entered_cms = []
        cm = _FakeSessionCM()

        def factory(conn):
            entered_cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        session = await pool.get_session("srv", "thread-1", {"transport": "stdio", "command": "c"})
        assert session is cm.session
        cm.session.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_session_reuses_same_scope(self, monkeypatch):
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        s1 = await pool.get_session("srv", "thread-1", {"transport": "stdio"})
        s2 = await pool.get_session("srv", "thread-1", {"transport": "stdio"})
        assert s1 is s2  # 同 (server, thread) 复用
        assert len(cms) == 1  # 只建了一个会话

    @pytest.mark.asyncio
    async def test_get_session_isolates_different_scope(self, monkeypatch):
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        s1 = await pool.get_session("srv", "thread-1", {"transport": "stdio"})
        s2 = await pool.get_session("srv", "thread-2", {"transport": "stdio"})
        assert s1 is not s2  # 不同 thread 隔离
        assert len(cms) == 2

    @pytest.mark.asyncio
    async def test_get_session_same_task_enters_and_exits(self, monkeypatch):
        """owner task 进出 cancel scope 必须同 task（anyio 要求）—— close 后验证。"""
        cm = _FakeSessionCM()
        _install_fake_adapter(monkeypatch, create_session_cm_factory=lambda conn: cm)

        pool = MCPSessionPool()
        await pool.get_session("srv", "thread-1", {"transport": "stdio"})
        await pool.close_all()
        assert cm.entered_task is not None
        assert cm.exited_task is not None
        # enter 与 exit 在同一个 owner task
        assert cm.entered_task is cm.exited_task

    @pytest.mark.asyncio
    async def test_close_scope_closes_only_that_scope(self, monkeypatch):
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        await pool.get_session("srv", "thread-1", {"transport": "stdio"})
        await pool.get_session("srv", "thread-2", {"transport": "stdio"})

        await pool.close_scope("thread-1")
        # thread-2 的会话仍在
        s2 = await pool.get_session("srv", "thread-2", {"transport": "stdio"})
        assert s2 is cms[1].session

    @pytest.mark.asyncio
    async def test_close_server_closes_only_that_server(self, monkeypatch):
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        await pool.get_session("srv-a", "t1", {"transport": "stdio"})
        await pool.get_session("srv-b", "t1", {"transport": "stdio"})

        await pool.close_server("srv-a")
        # srv-b 仍在
        s_b = await pool.get_session("srv-b", "t1", {"transport": "stdio"})
        assert s_b is cms[1].session

    @pytest.mark.asyncio
    async def test_lru_eviction_at_capacity(self, monkeypatch):
        """容量上限 LRU 淘汰（用小 MAX_SESSIONS monkeypatch 验证语义）。"""
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        monkeypatch.setattr(pool, "MAX_SESSIONS", 2)

        await pool.get_session("srv", "t1", {"transport": "stdio"})
        await pool.get_session("srv", "t2", {"transport": "stdio"})
        await pool.get_session("srv", "t3", {"transport": "stdio"})  # 触发淘汰 t1

        with pool._lock:
            keys = set(k[1] for k in pool._entries)
        assert "t1" not in keys  # 最老的被淘汰
        assert {"t2", "t3"} <= keys

    @pytest.mark.asyncio
    async def test_close_all_drains_entries(self, monkeypatch):
        cms = []

        def factory(conn):
            cm = _FakeSessionCM()
            cms.append(cm)
            return cm

        _install_fake_adapter(monkeypatch, create_session_cm_factory=factory)

        pool = MCPSessionPool()
        await pool.get_session("srv", "t1", {"transport": "stdio"})
        await pool.get_session("srv", "t2", {"transport": "stdio"})
        await pool.close_all()
        with pool._lock:
            assert len(pool._entries) == 0
            assert len(pool._inflight) == 0


# ===========================================================================
# 5. tools.get_mcp_tools
# ===========================================================================


class TestGetMcpTools:
    @pytest.mark.asyncio
    async def test_soft_load_missing_adapter_returns_empty(self, monkeypatch):
        # 确保 langchain_mcp_adapters 不在 sys.modules（模拟缺包）
        for mod in list(sys.modules):
            if mod.startswith("langchain_mcp_adapters") or mod == "mcp" or mod == "mcp.types":
                monkeypatch.setitem(sys.modules, mod, None)  # import 时 ModuleNotFound
        result = await tools_module.get_mcp_tools()
        assert result == []

    @pytest.mark.asyncio
    async def test_no_enabled_servers_returns_empty(self, monkeypatch, tmp_path):
        _install_fake_adapter(monkeypatch)
        # ExtensionsConfig.from_file 读盘——指到空配置
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: ExtensionsConfig()))
        result = await tools_module.get_mcp_tools()
        assert result == []

    @pytest.mark.asyncio
    async def test_discovers_tools_from_servers(self, monkeypatch):
        tool_a = _fake_tool("alpha_tool1")
        tool_b = _fake_tool("beta_tool1")
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool_a], "beta": [tool_b]})

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="alpha", type="stdio", command="a"),
                McpServerConfig(name="beta", type="stdio", command="b"),
            ]
        )
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        names = [t.name for t in tools]
        assert "alpha_tool1" in names
        assert "beta_tool1" in names

    @pytest.mark.asyncio
    async def test_one_failing_server_does_not_block_others(self, monkeypatch):
        """#3772：单个 MCP 服务器发现失败不拖累其它健康服务器。

        ``broken`` 的 ``get_tools(server_name="broken")`` 抛错，被 ``load_server_tools``
        内 try/except 捕获后返回 ``[]``；``good`` 的工具照常贡献。
        """
        tool_good = _fake_tool("good_tool1")
        _install_fake_adapter(
            monkeypatch,
            tools_by_name={"good": [tool_good], "broken": []},
            failing_servers={"broken"},
        )

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="good", type="stdio", command="g"),
                McpServerConfig(name="broken", type="stdio", command="b"),
            ]
        )
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        names = [t.name for t in tools]
        assert "good_tool1" in names
        # broken 的发现失败被隔离——不贡献任何工具
        assert all(not n.startswith("broken") for n in names)
        assert len(tools) == 1

    @pytest.mark.asyncio
    async def test_all_servers_failing_returns_empty_not_raise(self, monkeypatch):
        """#3772：所有服务器发现都失败 → 返回 ``[]``（不抛错到调用方）。"""
        _install_fake_adapter(
            monkeypatch,
            tools_by_name={"a": [], "b": []},
            failing_servers={"a", "b"},
        )

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(
            mcp_servers=[
                McpServerConfig(name="a", type="stdio", command="x"),
                McpServerConfig(name="b", type="stdio", command="y"),
            ]
        )
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        assert tools == []

    @pytest.mark.asyncio
    async def test_stdio_tool_wrapped_with_session_pool(self, monkeypatch):
        """stdio 工具被包成持久会话版（StructuredTool，有 coroutine）。"""
        from langchain_core.tools import StructuredTool

        tool = _fake_tool("alpha_search")
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool]})

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(mcp_servers=[McpServerConfig(name="alpha", type="stdio", command="a")])
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        wrapped = next(t for t in tools if t.name == "alpha_search")
        # _make_session_pool_tool 返回 StructuredTool（而非原始 SimpleNamespace）
        assert isinstance(wrapped, StructuredTool)
        assert wrapped.coroutine is not None

    @pytest.mark.asyncio
    async def test_http_tool_not_wrapped(self, monkeypatch):
        """http/sse 工具不包会话池（避免跨 task TaskGroup 清理错误）。"""
        tool = _fake_tool("alpha_query")
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool]})

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(mcp_servers=[McpServerConfig(name="alpha", type="http", url="http://x")])
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        # http 工具原样返回（SimpleNamespace，非 StructuredTool）
        wrapped = next(t for t in tools if t.name == "alpha_query")
        from langchain_core.tools import StructuredTool

        assert not isinstance(wrapped, StructuredTool)

    @pytest.mark.asyncio
    async def test_sync_func_attached_to_async_tool(self, monkeypatch):
        """纯协程工具补同步入口（func）。"""

        async def fake_coro(**kw):
            return "result"

        tool = _fake_tool("alpha_async", coroutine=fake_coro)
        tool.func = None
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool]})

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(mcp_servers=[McpServerConfig(name="alpha", type="http", url="http://x")])
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        t = next(x for x in tools if x.name == "alpha_async")
        assert t.func is not None  # 补了同步入口

    @pytest.mark.asyncio
    async def test_custom_interceptor_loaded(self, monkeypatch):
        """mcpInterceptors 声明的 builder 被加载进拦截器链。"""
        tool = _fake_tool("alpha_x")
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool]})

        loaded = {"v": False}

        def builder():
            loaded["v"] = True

            async def interceptor(request, handler):
                return await handler(request)

            return interceptor

        # 把 builder 注册到一个可 import 的路径（用 monkeypatch 注入 sys.modules）
        fake_mod = types.ModuleType("fake_interceptor_pkg")
        fake_mod.builder = builder
        monkeypatch.setitem(sys.modules, "fake_interceptor_pkg", fake_mod)

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(
            mcp_servers=[McpServerConfig(name="alpha", type="stdio", command="a")],
            mcp_interceptors=["fake_interceptor_pkg:builder"],
        )
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        await tools_module.get_mcp_tools()
        assert loaded["v"] is True

    @pytest.mark.asyncio
    async def test_bad_interceptor_skipped(self, monkeypatch, caplog):
        tool = _fake_tool("alpha_x")
        _install_fake_adapter(monkeypatch, tools_by_name={"alpha": [tool]})

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(
            mcp_servers=[McpServerConfig(name="alpha", type="stdio", command="a")],
            mcp_interceptors=["nonexistent.pkg:builder"],
        )
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        tools = await tools_module.get_mcp_tools()
        # 坏拦截器不拖垮工具发现
        assert any(t.name == "alpha_x" for t in tools)

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, monkeypatch):
        """get_tools 抛异常 → 返回 []（不向上抛）。"""
        _install_fake_adapter(monkeypatch)

        from deerflow.config.extensions_config import ExtensionsConfig

        cfg = ExtensionsConfig(mcp_servers=[McpServerConfig(name="alpha", type="stdio", command="a")])
        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: cfg))

        # 让 MultiServerMCPClient.get_tools 抛
        class _BoomClient:
            def __init__(self, *a, **kw):
                pass

            async def get_tools(self):
                raise RuntimeError("boom")

        sys.modules["langchain_mcp_adapters.client"].MultiServerMCPClient = _BoomClient
        result = await tools_module.get_mcp_tools()
        assert result == []

    def test_extract_thread_id_from_runtime_context(self):
        from deerflow.mcp.tools import _extract_thread_id

        rt = SimpleNamespace(context={"thread_id": "t-123"}, config=None)
        assert _extract_thread_id(rt) == "t-123"

    def test_extract_thread_id_from_runtime_config(self):
        from deerflow.mcp.tools import _extract_thread_id

        rt = SimpleNamespace(context=None, config={"configurable": {"thread_id": "t-456"}})
        assert _extract_thread_id(rt) == "t-456"

    def test_extract_thread_id_default_when_none(self):
        from deerflow.mcp.tools import _extract_thread_id

        # 无 runtime 且无 LangGraph config → default
        assert _extract_thread_id(None) == "default"


# ===========================================================================
# 6. tools.sync.make_sync_tool_wrapper
# ===========================================================================


class TestSyncWrapper:
    def test_runs_coroutine_sync_no_loop(self):
        from deerflow.tools.sync import make_sync_tool_wrapper

        async def coro(x):
            return x * 2

        wrapped = make_sync_tool_wrapper(coro, "t")
        assert wrapped(21) == 42

    @pytest.mark.asyncio
    async def test_offloads_when_loop_running(self):
        """运行中的循环里调同步包装 → 卸到线程池跑新循环（不死锁）。"""
        from deerflow.tools.sync import make_sync_tool_wrapper

        async def coro(x):
            return x + 1

        wrapped = make_sync_tool_wrapper(coro, "t")
        # 在事件循环内调用——必须卸线程而非嵌套 asyncio.run
        result = await asyncio.to_thread(wrapped, 10)
        assert result == 11

    def test_propagates_exception(self):
        from deerflow.tools.sync import make_sync_tool_wrapper

        async def coro():
            raise ValueError("boom")

        wrapped = make_sync_tool_wrapper(coro, "t")
        with pytest.raises(ValueError, match="boom"):
            wrapped()


# ===========================================================================
# 7. cache（mtime 失效 + 初始化幂等 + reset + 懒加载）
# ===========================================================================


class TestCache:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """每个测试前后清缓存（conftest autouse 也清，这里显式保险）。"""
        from deerflow.mcp import cache as cache_module

        cache_module._mcp_tools_cache = None
        cache_module._cache_initialized = False
        cache_module._config_mtime = None
        yield
        cache_module._mcp_tools_cache = None
        cache_module._cache_initialized = False
        cache_module._config_mtime = None

    def test_get_config_mtime_none_when_no_file(self, monkeypatch, tmp_path):
        from deerflow.mcp import cache as cache_module

        monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(tmp_path / "nope.json"))
        monkeypatch.delenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", raising=False)
        # 让 resolve_config_path 返回 None
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: None))
        assert cache_module._get_config_mtime() is None

    def test_get_config_mtime_returns_float(self, monkeypatch, tmp_path):
        from deerflow.mcp import cache as cache_module

        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: f))
        mtime = cache_module._get_config_mtime()
        assert isinstance(mtime, float)
        assert mtime > 0

    def test_is_cache_stale_false_when_uninitialized(self):
        from deerflow.mcp import cache as cache_module

        cache_module._cache_initialized = False
        assert cache_module._is_cache_stale() is False

    def test_is_cache_stale_true_when_mtime_advanced(self, monkeypatch, tmp_path):
        from deerflow.mcp import cache as cache_module

        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: f))
        cache_module._cache_initialized = True
        cache_module._config_mtime = 1.0  # 很早
        assert cache_module._is_cache_stale() is True

    def test_is_cache_stale_false_when_mtime_unchanged(self, monkeypatch, tmp_path):
        from deerflow.mcp import cache as cache_module

        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: f))
        current = cache_module._get_config_mtime()
        cache_module._cache_initialized = True
        cache_module._config_mtime = current
        assert cache_module._is_cache_stale() is False

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, monkeypatch):
        from deerflow.mcp import cache as cache_module

        calls = {"n": 0}

        async def fake_get_mcp_tools():
            calls["n"] += 1
            return [_fake_tool("only_tool")]

        monkeypatch.setattr(cache_module, "_get_config_mtime", lambda: 12345.0)
        # initialize 内部 from deerflow.mcp.tools import get_mcp_tools —— patch tools 模块属性
        monkeypatch.setattr(tools_module, "get_mcp_tools", fake_get_mcp_tools)

        r1 = await cache_module.initialize_mcp_tools()
        r2 = await cache_module.initialize_mcp_tools()
        assert r1 is r2  # 同一列表对象
        assert calls["n"] == 1  # 只初始化一次
        assert cache_module._cache_initialized is True
        assert cache_module._config_mtime == 12345.0

    def test_reset_clears_state(self, monkeypatch):
        from deerflow.mcp import cache as cache_module

        cache_module._mcp_tools_cache = [_fake_tool("x")]
        cache_module._cache_initialized = True
        cache_module._config_mtime = 999.0
        cache_module.reset_mcp_tools_cache()
        assert cache_module._mcp_tools_cache is None
        assert cache_module._cache_initialized is False
        assert cache_module._config_mtime is None

    @pytest.mark.asyncio
    async def test_get_cached_lazy_initializes(self, monkeypatch):
        from deerflow.mcp import cache as cache_module

        async def fake_get_mcp_tools():
            return [_fake_tool("lazy_tool")]

        monkeypatch.setattr(cache_module, "_get_config_mtime", lambda: 1.0)
        monkeypatch.setattr(tools_module, "get_mcp_tools", fake_get_mcp_tools)

        tools = cache_module.get_cached_mcp_tools()
        assert len(tools) == 1
        assert tools[0].name == "lazy_tool"
        assert cache_module._cache_initialized is True

    def test_get_cached_returns_empty_on_init_failure(self, monkeypatch):
        from deerflow.mcp import cache as cache_module

        async def boom():
            raise RuntimeError("init failed")

        monkeypatch.setattr(cache_module, "_get_config_mtime", lambda: 1.0)
        monkeypatch.setattr(tools_module, "get_mcp_tools", boom)

        # get_cached 捕获异常返回 []
        result = cache_module.get_cached_mcp_tools()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_cached_reinitializes_after_mtime_change(self, monkeypatch, tmp_path):
        """mtime 变化后 get_cached 触发重新初始化。"""
        from deerflow.mcp import cache as cache_module

        f = tmp_path / "ext.json"
        f.write_text("{}", encoding="utf-8")
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "resolve_config_path", classmethod(lambda cls, *a, **kw: f))

        call_count = {"n": 0}

        async def fake_get_mcp_tools():
            call_count["n"] += 1
            return [_fake_tool(f"tool_{call_count['n']}")]

        monkeypatch.setattr(tools_module, "get_mcp_tools", fake_get_mcp_tools)

        # 第一次初始化
        cache_module.get_cached_mcp_tools()
        assert call_count["n"] == 1

        # 改 mtime（模拟 Gateway API 写了 extensions_config.json）
        import os

        new_mtime = os.path.getmtime(f) + 100
        monkeypatch.setattr(cache_module, "_get_config_mtime", lambda: new_mtime)

        t2 = cache_module.get_cached_mcp_tools()
        assert call_count["n"] == 2  # 重新初始化了
        assert t2[0].name == "tool_2"


# ===========================================================================
# 8. 集成：get_available_tools 接 MCP（软加载不崩）
# ===========================================================================


class TestGetAvailableToolsIntegration:
    def test_no_mcp_servers_no_extra_tools(self, monkeypatch, tmp_path):
        """无 MCP 服务器时 get_available_tools 不触发 MCP 加载。"""
        from deerflow.config.extensions_config import ExtensionsConfig

        monkeypatch.setattr(ExtensionsConfig, "from_file", classmethod(lambda cls, *a, **kw: ExtensionsConfig()))

        from deerflow.tools.tools import get_available_tools

        tools = get_available_tools(include_mcp=True)
        # 只有内置工具（present_files + ask_clarification）
        names = [t.name for t in tools]
        assert "present_files" in names
        assert "ask_clarification" in names
