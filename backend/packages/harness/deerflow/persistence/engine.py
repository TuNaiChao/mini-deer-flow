"""async SQLAlchemy 引擎的生命周期管理。

在 Gateway / 进程启动时初始化，为各仓储（repository）提供 session factory，在
关闭时 dispose。

当 ``database.backend="memory"`` 时，``init_engine`` 是 no-op，且
``get_session_factory()`` 返回 ``None``。仓储必须检查 ``None`` 并回退到内存实现。

可靠性要点（红线）：
- **#2 SQLite WAL + busy 重试**：每条新连接开 ``journal_mode=WAL`` /
  ``synchronous=NORMAL`` / ``foreign_keys=ON`` / ``busy_timeout=30000``。WAL 让
  并发读 + 单写不阻塞，``synchronous=NORMAL`` 只在 WAL checkpoint 边界 fsync
  （安全且快的搭配）。``busy_timeout=30000`` 把锁竞争等待窗口提到 30 秒
  （Python sqlite3 驱动默认只有 5 秒，并发启动 / 多 worker 同时写时太短会误报
  ``database is locked``）。
- **#24 缺包可操作提示**：postgres 缺 asyncpg 时给出 install 命令。
- **#28 blocking-IO 卸载**：``os.makedirs`` 是同步磁盘 IO，在 async ``init_engine``
  里用 ``asyncio.to_thread`` 卸载。
- **create_all 自动建表**（开发便利；生产用 Alembic，本 Phase 未引入）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def _json_serializer(obj: object) -> str:
    """``ensure_ascii=False`` 的 JSON 序列化器（保留中文，不转义成 \\uXXXX）。"""
    return json.dumps(obj, ensure_ascii=False)


logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _auto_create_postgres_db(url: str) -> None:
    """连到 ``postgres`` 维护库并 ``CREATE DATABASE``。

    目标库名从 *url* 解析。连接打到同一服务器的默认 ``postgres`` 库，用
    ``AUTOCOMMIT`` 隔离级别（``CREATE DATABASE`` 不能在事务里跑）。
    """
    from sqlalchemy import text
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    db_name = parsed.database
    if not db_name:
        raise ValueError("Cannot auto-create database: no database name in URL")

    # 连默认 'postgres' 库来执行 CREATE DATABASE
    maint_url = parsed.set(database="postgres")
    maint_engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    try:
        async with maint_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        logger.info("Auto-created PostgreSQL database: %s", db_name)
    finally:
        await maint_engine.dispose()


async def init_engine(
    backend: str,
    *,
    url: str = "",
    echo: bool = False,
    pool_size: int = 5,
    sqlite_dir: str = "",
) -> None:
    """创建 async engine 与 session factory，然后自动建表。

    Args:
        backend: "memory"、"sqlite" 或 "postgres"。
        url: SQLAlchemy async URL（sqlite/postgres 用）。
        echo: 把 SQL 回显到日志。
        pool_size: Postgres 连接池大小。
        sqlite_dir: 为 SQLite 创建的目录（确保存在）。
    """
    global _engine, _session_factory

    if backend == "memory":
        logger.info("Persistence backend=memory -- ORM engine not initialized")
        return

    if backend == "postgres":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            raise ImportError(
                "database.backend is set to 'postgres' but asyncpg is not installed.\nInstall it with:\n    cd backend && uv sync --all-packages --extra postgres\nOr switch to backend: sqlite in config.yaml for single-node deployment."
            ) from None

    if backend == "sqlite":
        import os

        from sqlalchemy import event

        # ``os.makedirs`` 是同步磁盘 IO——init_engine 跑在 lifespan 的 async 上下文里，
        # 必须用 ``asyncio.to_thread`` 卸载，否则违反 blocking-IO 红线 #28。
        await asyncio.to_thread(os.makedirs, sqlite_dir or ".", exist_ok=True)
        _engine = create_async_engine(url, echo=echo, json_serializer=_json_serializer)

        # 每条新连接开 WAL。SQLite PRAGMA 是连接级的，所以用监听器而非启动时跑一次。
        # WAL 给出并发读 + 写者不阻塞，是任何生产 SQLite 部署的标准建议。配套的
        # ``synchronous=NORMAL`` 是安全且快的搭配——只在 WAL checkpoint 边界 fsync，
        # 而非每次提交。``busy_timeout=30000`` 把锁竞争下的「等还是立刻报 busy」窗口
        # 提到 30 秒——Python sqlite3 驱动默认只有 5 秒，并发启动 / 多 worker 同时写时
        # 太短会误报 ``database is locked``。
        @event.listens_for(_engine.sync_engine, "connect")
        def _enable_sqlite_wal(dbapi_conn, _record):  # noqa: ARG001 — SQLAlchemy 契约
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=30000;")
            finally:
                cursor.close()
    elif backend == "postgres":
        _engine = create_async_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            pool_pre_ping=True,
            json_serializer=_json_serializer,
        )
    else:
        raise ValueError(f"Unknown persistence backend: {backend!r}")

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # 自动建表（开发便利；生产用 Alembic）。
    from deerflow.persistence.base import Base

    # 导入所有模型，让 Base.metadata 发现它们。模型包未全建（scaffolding 阶段）时是 no-op。
    try:
        import deerflow.persistence.models  # noqa: F401
    except ImportError:
        # 模型包尚未就绪——不会自动建表。初始 scaffolding 或最小安装时这是预期行为。
        logger.debug("deerflow.persistence.models not found; skipping auto-create tables")

    try:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        if backend == "postgres" and "does not exist" in str(exc):
            # 库不存在——尝试自动建库后重试。
            await _auto_create_postgres_db(url)
            # 重建 engine 指向现已存在的库
            await _engine.dispose()
            _engine = create_async_engine(url, echo=echo, pool_size=pool_size, pool_pre_ping=True, json_serializer=_json_serializer)
            _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        else:
            raise

    logger.info("Persistence engine initialized: backend=%s", backend)


async def init_engine_from_config(config) -> None:
    """便利函数：从一个 DatabaseConfig 对象初始化 engine。"""
    if config.backend == "memory":
        await init_engine("memory")
        return
    await init_engine(
        backend=config.backend,
        url=config.app_sqlalchemy_url,
        echo=config.echo_sql,
        pool_size=config.pool_size,
        sqlite_dir=config.sqlite_dir if config.backend == "sqlite" else "",
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """返回 async session factory；backend=memory 时返回 None。"""
    return _session_factory


def get_engine() -> AsyncEngine | None:
    """返回 async engine；未初始化时返回 None。"""
    return _engine


async def close_engine() -> None:
    """dispose engine，释放所有连接。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Persistence engine closed")
    _engine = None
    _session_factory = None
