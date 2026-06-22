"""
扩展配置模块

从 extensions_config.json 加载 MCP 服务器配置和技能启用状态。
与 config.yaml 分离——运行时通过 Gateway API 修改此文件。

M20（v1.2）扩展：补齐 MCP 集成所需的字段——OAuth（HTTP/SSE 鉴权）、
description（可读说明）、mcpInterceptors（自定义工具拦截器 builder 路径）、
resolve_config_path（缓存 mtime 失效的路径源）、resolve_env_variables（``$VAR``
展开）。同时**兼容两种输入格式**：

- deer 原生格式：``{"mcpServers": {"name": {...}}, "skills": {"name": {"enabled": true}}}``
- mini 早期格式：``{"mcp_servers": [{...}], "enabled_skills": ["name"]}``

内部统一归一为 ``mcp_servers: list[McpServerConfig]`` + ``enabled_skills: list[str]``
（mini 既有表示，``get_enabled_mcp_servers`` 已按名返回 dict）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class McpOAuthConfig:
    """单个 MCP 服务器的 OAuth 鉴权配置（仅 sse/http 传输用）。

    支持 ``client_credentials`` 与 ``refresh_token`` 两种授权类型；
    token 过期前 ``refresh_skew_seconds`` 秒提前刷新（红线 #30）。
    对齐 deer ``McpOAuthConfig`` 字段集（pydantic → mini dataclass）。
    """

    enabled: bool = True
    token_url: str = ""
    grant_type: str = "client_credentials"  # "client_credentials" | "refresh_token"
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None  # refresh_token 授权用
    scope: str | None = None
    audience: str | None = None  # provider 特定（如 Auth0 audience）
    # token 响应字段名（不同 IdP 字段名可能不同，提供覆写）
    token_field: str = "access_token"
    token_type_field: str = "token_type"
    expires_in_field: str = "expires_in"
    default_token_type: str = "Bearer"
    refresh_skew_seconds: int = 60  # 过期前这么多秒提前刷新（红线 #30）
    extra_token_params: dict[str, str] = field(default_factory=dict)
    # 未知字段原样保留（向前兼容 IdP 私有扩展）
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> McpOAuthConfig:
        """从 dict 构造，剥离已知字段，未知字段进 extra。"""
        known = {
            "enabled",
            "token_url",
            "grant_type",
            "client_id",
            "client_secret",
            "refresh_token",
            "scope",
            "audience",
            "token_field",
            "token_type_field",
            "expires_in_field",
            "default_token_type",
            "refresh_skew_seconds",
            "extra_token_params",
        }
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in raw.items():
            if key in known:
                kwargs[key] = value
            else:
                extra[key] = value
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)


@dataclass
class McpServerConfig:
    """单个 MCP 服务器的配置。

    支持 stdio（本地子进程）和 sse/http（远程服务）两种传输方式。
    M20 扩展：``oauth``（HTTP/SSE 鉴权）、``description``（可读说明）、
    ``extra``（未知字段向前兼容，含 mcpInterceptors 等扩展）。
    """

    name: str
    enabled: bool = True
    type: str = "stdio"  # "stdio" | "sse" | "http"（接受 "transport" 别名，见 from_dict）
    # stdio 传输参数
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    # sse/http 传输参数
    url: str | None = None
    headers: dict[str, str] | None = None
    # OAuth（仅 sse/http；红线 #30）
    oauth: McpOAuthConfig | None = None
    # 可读说明（Gateway UI 展示用）
    description: str = ""
    # 未知字段原样保留（向前兼容）
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, name: str | None = None) -> McpServerConfig:
        """从 dict 构造，处理 ``transport``→``type`` 别名 + 未知字段进 extra。"""
        raw = dict(raw)  # 浅拷贝，不污染调用方
        # 接受 MCP 规范的 ``transport`` 作为 ``type`` 的别名（type 优先）。
        transport = raw.pop("transport", None)
        if transport and "type" not in raw:
            raw["type"] = transport
        if name is not None and "name" not in raw:
            raw["name"] = name

        oauth_raw = raw.pop("oauth", None)
        known = {
            "name",
            "enabled",
            "type",
            "command",
            "args",
            "env",
            "url",
            "headers",
            "description",
        }
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in raw.items():
            if key in known:
                kwargs[key] = value
            else:
                extra[key] = value
        if extra:
            kwargs["extra"] = extra
        if oauth_raw:
            oauth = oauth_raw if isinstance(oauth_raw, McpOAuthConfig) else McpOAuthConfig.from_dict(oauth_raw)
            kwargs["oauth"] = oauth
        # name 兜底（deer dict 格式下 name 来自 key 而非字段）
        kwargs.setdefault("name", name or "")
        return cls(**kwargs)


@dataclass
class ExtensionsConfig:
    """扩展配置——从 extensions_config.json 加载。

    包含：
    - mcp_servers: MCP 服务器列表（``McpServerConfig``）
    - enabled_skills: 启用的技能名称集合
    - mcp_interceptors: 自定义 MCP 工具拦截器 builder 路径列表（``"pkg.mod:func"``）
    """

    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    enabled_skills: list[str] = field(default_factory=list)
    mcp_interceptors: list[str] = field(default_factory=list)

    # ---- 路径解析（缓存 mtime 失效的路径源，M20） ----

    @classmethod
    def resolve_config_path(cls, config_path: str | Path | None = None) -> Path | None:
        """解析 extensions_config.json 路径（对齐 deer 优先级）。

        优先级：
        1. 显式 ``config_path`` 参数（不存在抛 FileNotFoundError）
        2. ``DEER_FLOW_EXTENSIONS_CONFIG_PATH`` 环境变量（不存在抛 FileNotFoundError）
        3. 调用方项目根（``project_root()``）下的 ``extensions_config.json`` / ``mcp_config.json``
        4. ``default_path()``（backend/extensions_config.json）
        5. 都没有 → 返回 None（扩展是可选的）

        扩展可选：找不到返回 None 而非报错（红线 #25 空配置可启动）。
        """
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"extensions_config 指定的 config_path 不存在: {path}")
            return path

        env_path = os.getenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH")
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(f"DEER_FLOW_EXTENSIONS_CONFIG_PATH 指定的文件不存在: {path}")
            return path

        from .paths import existing_project_file

        project_config = existing_project_file(("extensions_config.json", "mcp_config.json"))
        if project_config is not None:
            return project_config

        default = cls.default_path()
        if default.exists():
            return default

        return None

    @staticmethod
    def default_path() -> Path:
        """extensions_config.json 的默认路径（backend/ 目录下）。"""
        from .paths import PROJECT_ROOT

        return PROJECT_ROOT / "extensions_config.json"

    # ---- 环境变量展开（``$VAR`` → os.getenv，对齐 deer） ----

    @classmethod
    def resolve_env_variables(cls, config: Any) -> Any:
        """递归展开 ``$VAR`` 形式的环境变量。

        未解析的占位符（环境变量不存在）→ 空字符串（避免下游收到字面 ``$VAR``）。
        """
        if isinstance(config, str):
            if not config.startswith("$"):
                return config
            env_value = os.getenv(config[1:])
            return "" if env_value is None else env_value
        if isinstance(config, dict):
            return {key: cls.resolve_env_variables(value) for key, value in config.items()}
        if isinstance(config, list):
            return [cls.resolve_env_variables(item) for item in config]
        if isinstance(config, tuple):
            return tuple(cls.resolve_env_variables(item) for item in config)
        return config

    # ---- 工厂方法 ----

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> ExtensionsConfig:
        """从 JSON 文件加载扩展配置。文件不存在时返回空配置。

        兼容两种输入格式（见模块 docstring）。``$VAR`` 自动展开。
        """
        resolved = cls.resolve_config_path(path) if path is not None else cls.resolve_config_path()
        if resolved is None:
            return cls()

        with open(resolved, "r", encoding="utf-8") as f:
            raw = json.load(f)
        raw = cls.resolve_env_variables(raw)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ExtensionsConfig:
        """从已解析的 dict 构造，归一 deer/mini 两种格式。"""
        # ---- MCP servers：兼容 deer dict（mcpServers）与 mini list（mcp_servers） ----
        servers: list[McpServerConfig] = []
        mcp_servers_raw = raw.get("mcpServers")
        if isinstance(mcp_servers_raw, dict):
            # deer 格式：{name: {...}}
            for server_name, server_cfg in mcp_servers_raw.items():
                if isinstance(server_cfg, dict):
                    servers.append(McpServerConfig.from_dict(server_cfg, name=server_name))
        elif isinstance(mcp_servers_raw, list):
            # mini 早期格式：[{...}]
            for server_cfg in mcp_servers_raw:
                if isinstance(server_cfg, dict):
                    servers.append(McpServerConfig.from_dict(server_cfg))
        else:
            # 兼容 mini snake_case list
            for server_cfg in raw.get("mcp_servers", []) or []:
                if isinstance(server_cfg, dict):
                    servers.append(McpServerConfig.from_dict(server_cfg))

        # ---- skills：兼容 deer dict（skills）与 mini list（enabled_skills） ----
        enabled_skills: list[str] = list(raw.get("enabled_skills", []) or [])
        skills_raw = raw.get("skills")
        if isinstance(skills_raw, dict):
            for skill_name, skill_state in skills_raw.items():
                if isinstance(skill_state, dict) and skill_state.get("enabled", True):
                    if skill_name not in enabled_skills:
                        enabled_skills.append(skill_name)

        # ---- mcpInterceptors：自定义工具拦截器 builder 路径 ----
        interceptors_raw = raw.get("mcpInterceptors")
        if isinstance(interceptors_raw, str):
            interceptors_raw = [interceptors_raw]
        elif not isinstance(interceptors_raw, list):
            interceptors_raw = []
        mcp_interceptors: list[str] = [str(p) for p in interceptors_raw if isinstance(p, str)]

        return cls(
            mcp_servers=servers,
            enabled_skills=enabled_skills,
            mcp_interceptors=mcp_interceptors,
        )

    # ---- 查询方法 ----

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        """返回 {server_name: McpServerConfig} 仅包含 enabled=True 的服务器。"""
        return {s.name: s for s in self.mcp_servers if s.enabled}

    def get_oauth_servers(self) -> dict[str, McpOAuthConfig]:
        """返回 {server_name: McpOAuthConfig} 仅含启用且配置了 OAuth 的服务器。"""
        result: dict[str, McpOAuthConfig] = {}
        for name, server in self.get_enabled_mcp_servers().items():
            if server.oauth is not None and server.oauth.enabled:
                result[name] = server.oauth
        return result

    def is_skill_enabled(self, skill_name: str, skill_category: str = "public") -> bool:
        """判断某个技能是否启用（对齐 deer 语义）。

        显式列入 enabled_skills → True；未显式配置的 public / custom 技能默认启用
        （对齐 deer：未配置即放行，开箱即用）。M14 skills 落地后改为每次重读
        extensions_config.json 的精确 enabled 状态。

        Args:
            skill_name: 技能名（目录名）。
            skill_category: 技能类别（public 或 custom）。
        """
        if skill_name in self.enabled_skills:
            return True
        return skill_category in ("public", "custom")
