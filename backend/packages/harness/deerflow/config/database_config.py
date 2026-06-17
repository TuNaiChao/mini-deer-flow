"""统一数据库后端配置。

同时控制 LangGraph checkpointer 和 DeerFlow 应用持久化层（runs / threads 元数据 /
用户数据等）。用户只配置一个后端；系统处理物理分离细节。

- **SQLite 模式**：checkpointer 和 app 共用一个 ``.db`` 文件（``{sqlite_dir}/deerflow.db``），
  每个连接开 WAL 日志模式。WAL 允许并发读 + 单写不阻塞，让统一文件对两种负载都安全。
  争锁的写者通过默认 5 秒 sqlite3 busy 超时等待，而非立即失败。
- **Postgres 模式**：两者用同一个数据库 URL，但维护独立连接池（不同生命周期）。
- **Memory 模式**：checkpointer 用 MemorySaver，app 用内存存储；不初始化数据库。

敏感值（``postgres_url``）应在 config.yaml 用 ``$VAR`` 语法引用 ``.env`` 里的环境变量：

    database:
      backend: postgres
      postgres_url: $DATABASE_URL

``$VAR`` 解析由 ``AppConfig`` 加载时完成，``DatabaseConfig`` 本身不做环境变量处理。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """统一数据库后端配置（checkpointer + app 共用）。"""

    backend: Literal["memory", "sqlite", "postgres"] = Field(
        default="memory",
        description="存储后端（checkpointer 与 app 数据共用）。'memory' 开发用（重启不持久化），'sqlite' 单节点部署，'postgres' 生产多节点部署。",
    )
    sqlite_dir: str = Field(
        default=".deer-flow/data",
        description="SQLite 数据库文件目录。checkpointer 与 app 数据共用 {sqlite_dir}/deerflow.db。",
    )
    postgres_url: str = Field(
        default="",
        description="PostgreSQL 连接 URL，checkpointer 与 app 共用。在 config.yaml 用 $DATABASE_URL 引用 .env。例：postgresql://user:pass@host:5432/deerflow（+asyncpg 驱动后缀在需要处自动添加）。",
    )
    echo_sql: bool = Field(
        default=False,
        description="把所有 SQL 语句回显到日志（仅调试）。",
    )
    pool_size: int = Field(
        default=5,
        description="app ORM 引擎的连接池大小（仅 postgres）。",
    )

    # -- 派生 helper（非用户配置）--

    @property
    def _resolved_sqlite_dir(self) -> str:
        """把 sqlite_dir 解析成绝对路径（相对 CWD）。"""
        from pathlib import Path

        return str(Path(self.sqlite_dir).resolve())

    @property
    def sqlite_path(self) -> str:
        """checkpointer 与 app 共用的统一 SQLite 文件路径。"""
        return os.path.join(self._resolved_sqlite_dir, "deerflow.db")

    # 向后兼容别名
    @property
    def checkpointer_sqlite_path(self) -> str:
        """LangGraph checkpointer 的 SQLite 文件路径（sqlite_path 的别名）。"""
        return self.sqlite_path

    @property
    def app_sqlite_path(self) -> str:
        """应用 ORM 数据的 SQLite 文件路径（sqlite_path 的别名）。"""
        return self.sqlite_path

    @property
    def app_sqlalchemy_url(self) -> str:
        """app ORM 引擎的 SQLAlchemy async URL。"""
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        if self.backend == "postgres":
            url = self.postgres_url
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            return url
        raise ValueError(f"No SQLAlchemy URL for backend={self.backend!r}")
