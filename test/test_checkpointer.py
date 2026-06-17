"""M5 checkpointer 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M5 测试要求）：
- ``_sqlite_utils``：resolve_sqlite_conn_str（:memory:/file:/路径）、ensure_sqlite_parent_dir。
- ``make_checkpointer``（async）：memory 默认 / memory 显式 / sqlite（legacy + database）真实
  aput→aget_tuple 往返、优先级 legacy>database、database-memory 回退、postgres 缺包提示、
  未知类型 ValueError。
- 同步 ``provider``：get_checkpointer 单例 + reset、checkpointer_context、sqlite 真实往返。
- 红线 #1（阻塞 IO 卸载）：sqlite 路径准备走 asyncio.to_thread。
- 红线 #1912：sqlite 父目录保护（ensure before connect）。
- 红线 #24：缺包可操作提示。

hermetic 约定：sqlite 往返用 tmp_path 隔离文件；同步单例用 reset fixture 防泄漏；postgres
不连真实库，只验缺包提示。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.config.database_config import DatabaseConfig
from deerflow.runtime.checkpointer import (
    checkpointer_context,
    get_checkpointer,
    make_checkpointer,
    reset_checkpointer,
)
from deerflow.runtime.checkpointer.async_provider import _prepare_database_sqlite_checkpointer_path, _prepare_sqlite_checkpointer_path
from deerflow.runtime.checkpointer.provider import POSTGRES_INSTALL, SQLITE_INSTALL
from deerflow.runtime.store._sqlite_utils import ensure_sqlite_parent_dir, resolve_sqlite_conn_str

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_checkpointer_around_test():
    """同步单例在每个测试前后重置，防跨测试泄漏。"""
    reset_checkpointer()
    yield
    reset_checkpointer()


def _ckpt_config(thread_id: str = "t1") -> dict:
    """构造一个最小的 checkpointer config（含 checkpoint_ns）。"""
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _minimal_checkpoint() -> dict:
    """构造当前 langgraph 版本可接受的最小 checkpoint dict。"""
    return {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": "2024-08-01T00:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }


# ---------------------------------------------------------------------------
# _sqlite_utils
# ---------------------------------------------------------------------------


class TestSqliteUtils:
    def test_resolve_memory_passthrough(self):
        assert resolve_sqlite_conn_str(":memory:") == ":memory:"

    def test_resolve_file_uri_passthrough(self):
        assert resolve_sqlite_conn_str("file:/tmp/x.db?cache=shared") == "file:/tmp/x.db?cache=shared"

    def test_resolve_relative_path_becomes_absolute(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        resolved = resolve_sqlite_conn_str("sub/cp.db")
        assert resolved == str((tmp_path / "sub" / "cp.db").resolve())

    def test_ensure_parent_dir_creates(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "cp.db"
        ensure_sqlite_parent_dir(str(target))
        assert target.parent.is_dir()

    def test_ensure_parent_dir_noop_for_memory(self):
        # 不应抛错（:memory: 无父目录）
        ensure_sqlite_parent_dir(":memory:")

    def test_ensure_parent_dir_noop_for_file_uri(self):
        ensure_sqlite_parent_dir("file:/tmp/x.db")


# ---------------------------------------------------------------------------
# make_checkpointer（async）
# ---------------------------------------------------------------------------


class TestMakeCheckpointer:
    async def test_memory_default_when_nothing_configured(self):
        """空配置 → InMemorySaver。"""
        from langgraph.checkpoint.memory import InMemorySaver

        async with make_checkpointer(AppConfig()) as cp:
            assert isinstance(cp, InMemorySaver)

    async def test_memory_explicit_checkpointer_config(self):
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = AppConfig(checkpointer=CheckpointerConfig(type="memory"))
        async with make_checkpointer(cfg) as cp:
            assert isinstance(cp, InMemorySaver)

    async def test_memory_roundtrip_aput_aget_tuple(self):
        """memory checkpointer：aput 后 aget_tuple 能读到（红线 #？setup 后 aput/aget_tuple）。"""
        cfg = AppConfig(checkpointer=CheckpointerConfig(type="memory"))
        async with make_checkpointer(cfg) as cp:
            config = _ckpt_config()
            assert await cp.aget_tuple(config) is None  # 初始无
            await cp.aput(config, _minimal_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
            tup = await cp.aget_tuple(config)
            assert tup is not None
            assert tup.metadata["source"] == "input"

    async def test_sqlite_from_legacy_checkpointer_config(self, tmp_path: Path):
        """legacy checkpointer sqlite → AsyncSqliteSaver + 真实 aput/aget_tuple 往返。"""
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db = tmp_path / "cp.db"
        cfg = AppConfig(checkpointer=CheckpointerConfig(type="sqlite", connection_string=str(db)))
        async with make_checkpointer(cfg) as cp:
            assert isinstance(cp, AsyncSqliteSaver)
            config = _ckpt_config("sqlite-legacy")
            await cp.aput(config, _minimal_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
            tup = await cp.aget_tuple(config)
            assert tup is not None
        # 文件确实落盘
        assert db.exists()

    async def test_sqlite_from_database_config(self, tmp_path: Path):
        """统一 database sqlite → AsyncSqliteSaver（路径由 DatabaseConfig.checkpointer_sqlite_path 派生）。"""
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        cfg = AppConfig(database=DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)))
        async with make_checkpointer(cfg) as cp:
            assert isinstance(cp, AsyncSqliteSaver)
            config = _ckpt_config("sqlite-db")
            await cp.aput(config, _minimal_checkpoint(), {"source": "loop", "step": 1, "writes": {}}, {})
            tup = await cp.aget_tuple(config)
            assert tup is not None
            assert tup.metadata["source"] == "loop"

    async def test_priority_legacy_checkpointer_over_database(self, tmp_path: Path):
        """同时配了 legacy checkpointer(memory) 与 database(sqlite) → legacy 优先（memory 胜出）。"""
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = AppConfig(
            checkpointer=CheckpointerConfig(type="memory"),
            database=DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path)),
        )
        async with make_checkpointer(cfg) as cp:
            assert isinstance(cp, InMemorySaver)  # legacy memory 胜出，非 sqlite

    async def test_database_memory_falls_through_to_inmemory(self):
        """database=memory 且无 legacy checkpointer → 默认 InMemorySaver。"""
        from langgraph.checkpoint.memory import InMemorySaver

        cfg = AppConfig(database=DatabaseConfig(backend="memory"))
        async with make_checkpointer(cfg) as cp:
            assert isinstance(cp, InMemorySaver)

    async def test_postgres_missing_pkg_hint(self):
        """postgres 后端但包未装 → ImportError 含可操作安装提示（红线 #24）。"""
        cfg = AppConfig(checkpointer=CheckpointerConfig(type="postgres", connection_string="postgresql://localhost/db"))
        with pytest.raises(ImportError, match="langgraph-checkpoint-postgres"):
            async with make_checkpointer(cfg):
                pass

    async def test_postgres_missing_connection_string(self):
        """postgres 后端但缺 connection_string → ValueError（即使包没装也先校验连接串）。"""
        # 注：_ensure_postgres_imports 在连接串校验之后？看实现：先校验连接串再 import。
        # 实现里 postgres 分支先 `if not config.connection_string: raise ValueError`。
        cfg = AppConfig(checkpointer=CheckpointerConfig(type="postgres"))
        with pytest.raises(ValueError, match="connection_string is required"):
            async with make_checkpointer(cfg):
                pass

    async def test_unknown_type_raises(self):
        from deerflow.runtime.checkpointer.async_provider import _async_checkpointer

        with pytest.raises(ValueError, match="Unknown checkpointer type"):
            async with _async_checkpointer(SimpleNamespace(type="mysql")):
                pass


# ---------------------------------------------------------------------------
# 阻塞 IO 卸载（红线 #1）：sqlite 路径准备走 asyncio.to_thread
# ---------------------------------------------------------------------------


class TestIoOffload:
    async def test_legacy_sqlite_path_prep_uses_to_thread(self, tmp_path: Path, monkeypatch):
        """legacy sqlite：_prepare_sqlite_checkpointer_path 经 asyncio.to_thread 卸载。"""
        import deerflow.runtime.checkpointer.async_provider as mod

        called: list = []
        real_to_thread = mod.asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            called.append((fn, args))
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(mod.asyncio, "to_thread", spy_to_thread)

        cfg = AppConfig(checkpointer=CheckpointerConfig(type="sqlite", connection_string=str(tmp_path / "cp.db")))
        async with make_checkpointer(cfg):
            pass

        assert called, "sqlite 路径准备未走 asyncio.to_thread"
        assert called[0][0] is _prepare_sqlite_checkpointer_path

    async def test_database_sqlite_path_prep_uses_to_thread(self, tmp_path: Path, monkeypatch):
        """database sqlite：_prepare_database_sqlite_checkpointer_path 经 asyncio.to_thread 卸载。"""
        import deerflow.runtime.checkpointer.async_provider as mod

        called: list = []
        real_to_thread = mod.asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            called.append((fn, args))
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(mod.asyncio, "to_thread", spy_to_thread)

        db_config = DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path))
        cfg = AppConfig(database=db_config)
        async with make_checkpointer(cfg):
            pass

        assert called
        assert called[0][0] is _prepare_database_sqlite_checkpointer_path
        assert called[0][1][0] is db_config


# ---------------------------------------------------------------------------
# 同步 provider
# ---------------------------------------------------------------------------


class TestSyncProvider:
    def test_get_checkpointer_memory_when_unconfigured(self):
        """config.checkpointer=None → InMemorySaver。用 monkeypatch 控制全局配置。"""
        from langgraph.checkpoint.memory import InMemorySaver

        with patch("deerflow.runtime.checkpointer.provider.get_app_config", return_value=AppConfig()):
            cp = get_checkpointer()
        assert isinstance(cp, InMemorySaver)

    def test_singleton_identity(self):
        with patch("deerflow.runtime.checkpointer.provider.get_app_config", return_value=AppConfig()):
            cp1 = get_checkpointer()
            cp2 = get_checkpointer()
            assert cp1 is cp2

    def test_reset_clears_singleton(self):
        with patch("deerflow.runtime.checkpointer.provider.get_app_config", return_value=AppConfig()):
            cp1 = get_checkpointer()
            reset_checkpointer()
            cp2 = get_checkpointer()
            assert cp1 is not cp2

    def test_checkpointer_context_memory(self):
        from langgraph.checkpoint.memory import InMemorySaver

        with patch("deerflow.runtime.checkpointer.provider.get_app_config", return_value=AppConfig()):
            with checkpointer_context() as cp:
                assert isinstance(cp, InMemorySaver)

    def test_sync_sqlite_roundtrip(self, tmp_path: Path):
        """同步 sqlite checkpointer：setup + put + get_tuple 往返。"""
        from langgraph.checkpoint.sqlite import SqliteSaver

        db = tmp_path / "sync.db"
        config = CheckpointerConfig(type="sqlite", connection_string=str(db))
        from deerflow.runtime.checkpointer.provider import _sync_checkpointer_cm

        with _sync_checkpointer_cm(config) as cp:
            assert isinstance(cp, SqliteSaver)
            ck = _ckpt_config("sync")
            assert cp.get_tuple(ck) is None  # 初始无
            cp.put(ck, _minimal_checkpoint(), {"source": "input", "step": 0, "writes": {}}, {})
            tup = cp.get_tuple(ck)
            assert tup is not None
            assert tup.metadata["source"] == "input"
        assert db.exists()

    def test_sync_sqlite_creates_parent_dir(self, tmp_path: Path):
        """红线 #1912：父目录不存在时由 ensure_sqlite_parent_dir 创建（在连接前）。"""
        nested = tmp_path / "deep" / "nested" / "dir" / "sync.db"
        config = CheckpointerConfig(type="sqlite", connection_string=str(nested))
        from deerflow.runtime.checkpointer.provider import _sync_checkpointer_cm

        with _sync_checkpointer_cm(config) as cp:
            from langgraph.checkpoint.sqlite import SqliteSaver

            assert isinstance(cp, SqliteSaver)
        assert nested.parent.is_dir()
        assert nested.exists()

    def test_sync_sqlite_missing_pkg_hint(self, monkeypatch):
        """sqlite 包被屏蔽 → ImportError 含可操作提示（红线 #24）。"""
        import sys

        config = CheckpointerConfig(type="sqlite", connection_string="/tmp/x.db")
        from deerflow.runtime.checkpointer.provider import _sync_checkpointer_cm

        # 屏蔽 sqlite saver 模块
        monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", None)
        with pytest.raises(ImportError, match="langgraph-checkpoint-sqlite"):
            with _sync_checkpointer_cm(config):
                pass

    def test_install_hints_are_actionable(self):
        """安装提示包含可操作的命令（红线 #24）。"""
        assert "uv sync --all-packages --extra sqlite" in SQLITE_INSTALL
        assert "uv sync --all-packages --extra postgres" in POSTGRES_INSTALL
