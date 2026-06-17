"""
扩展配置模块

从 extensions_config.json 加载 MCP 服务器配置和技能启用状态。
与 config.yaml 分离——运行时通过 Gateway API 修改此文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class McpServerConfig:
    """单个 MCP 服务器的配置。

    支持 stdio（本地子进程）和 sse/http（远程服务）两种传输方式。
    """

    name: str
    enabled: bool = True
    type: str = "stdio"  # "stdio" | "sse" | "http"
    # stdio 传输参数
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    # sse/http 传输参数
    url: str | None = None
    headers: dict[str, str] | None = None


@dataclass
class ExtensionsConfig:
    """扩展配置——从 extensions_config.json 加载。

    包含：
    - mcp_servers: MCP 服务器列表
    - enabled_skills: 启用的技能名称集合
    """

    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    enabled_skills: list[str] = field(default_factory=list)

    # ---- 工厂方法 ----

    @staticmethod
    def default_path() -> Path:
        """extensions_config.json 的默认路径（backend/ 目录下）。"""
        from .paths import PROJECT_ROOT

        return PROJECT_ROOT / "extensions_config.json"

    @classmethod
    def from_file(cls, path: Path | None = None) -> ExtensionsConfig:
        """从 JSON 文件加载扩展配置。文件不存在时返回空配置。"""
        if path is None:
            path = cls.default_path()

        if not path.exists():
            return cls()

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        servers = [McpServerConfig(**s) for s in raw.get("mcp_servers", [])]
        skills = raw.get("enabled_skills", [])
        return cls(mcp_servers=servers, enabled_skills=skills)

    # ---- 查询方法 ----

    def get_enabled_mcp_servers(self) -> dict[str, McpServerConfig]:
        """返回 {server_name: McpServerConfig} 仅包含 enabled=True 的服务器。"""
        return {s.name: s for s in self.mcp_servers if s.enabled}

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
