"""Store 工厂测试（M19）。

hermetic：用 monkeypatch 注入 AppConfig（无 checkpointer 段 → InMemoryStore；memory 段 →
InMemoryStore），覆盖异步 / 同步单例 / 同步 CM 三入口 + reset + 缺包 soft-load。不连真实
sqlite/postgres。
"""

from __future__ import annotations

import pytest
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from deerflow.config import AppConfig
from deerflow.config.checkpointer_config import CheckpointerConfig
from deerflow.runtime.store import get_store, make_store, reset_store, store_context


@pytest.fixture(autouse=True)
def _reset_store_singleton():
    """每个测试前后清 store 单例（防跨测试污染）。"""
    reset_store()
    yield
    reset_store()


def _config_without_checkpointer() -> AppConfig:
    return AppConfig()


def _config_with_memory_checkpointer() -> AppConfig:
    return AppConfig(checkpointer=CheckpointerConfig(type="memory"))


# ===========================================================================
# 异步 make_store
# ===========================================================================


async def test_make_store_no_checkpointer_uses_inmemory():
    """无 checkpointer 段 → InMemoryStore + warning。"""
    async with make_store(_config_without_checkpointer()) as store:
        assert isinstance(store, InMemoryStore)


async def test_make_store_memory_backend():
    """checkpointer.type=memory → InMemoryStore。"""
    async with make_store(_config_with_memory_checkpointer()) as store:
        assert isinstance(store, InMemoryStore)


async def test_make_store_yields_working_store():
    """yield 的 Store 能 put/get（真实 BaseStore 行为）。"""
    async with make_store(_config_with_memory_checkpointer()) as store:
        store.put(("ns",), "key", {"value": 1})
        item = store.get(("ns",), "key")
        assert item is not None
        assert item.value == {"value": 1}


async def test_make_store_returns_basestore():
    """yield 的对象是 BaseStore 子类。"""
    async with make_store(_config_with_memory_checkpointer()) as store:
        assert isinstance(store, BaseStore)


# ===========================================================================
# 同步单例 get_store
# ===========================================================================


def test_get_store_no_checkpointer_uses_inmemory(monkeypatch):
    """无 checkpointer 段 → get_store 返 InMemoryStore 单例。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_without_checkpointer)
    store = get_store()
    assert isinstance(store, InMemoryStore)


def test_get_store_singleton_reused(monkeypatch):
    """get_store 两次返同一实例（单例）。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_with_memory_checkpointer)
    s1 = get_store()
    s2 = get_store()
    assert s1 is s2


def test_reset_store_forces_recreation(monkeypatch):
    """reset_store 后再 get_store 是新实例。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_with_memory_checkpointer)
    s1 = get_store()
    reset_store()
    s2 = get_store()
    assert s1 is not s2


def test_get_store_memory_backend(monkeypatch):
    """checkpointer.type=memory → get_store 返 InMemoryStore。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_with_memory_checkpointer)
    assert isinstance(get_store(), InMemoryStore)


# ===========================================================================
# 同步一次性 store_context
# ===========================================================================


def test_store_context_no_checkpointer_uses_inmemory(monkeypatch):
    """无 checkpointer 段 → store_context yield InMemoryStore。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_without_checkpointer)
    with store_context() as store:
        assert isinstance(store, InMemoryStore)


def test_store_context_not_cached(monkeypatch):
    """store_context 不缓存——两次 with 是不同实例。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_with_memory_checkpointer)
    with store_context() as s1:
        pass
    with store_context() as s2:
        pass
    assert s1 is not s2


def test_store_context_yields_working_store(monkeypatch):
    """store_context yield 的 Store 能 put/get。"""
    monkeypatch.setattr("deerflow.runtime.store.provider.get_app_config", _config_with_memory_checkpointer)
    with store_context() as store:
        store.put(("ns",), "k", {"v": 42})
        assert store.get(("ns",), "k").value == {"v": 42}


# ===========================================================================
# soft-load（缺包安装提示，红线 #24）
# ===========================================================================


def test_make_store_sqlite_missing_package_raises_importerror(monkeypatch):
    """checkpointer.type=sqlite 但缺包 → ImportError 带安装提示（soft-load，红线 #24）。"""
    # 模拟 langgraph.store.sqlite 不可导入
    import sys

    monkeypatch.setitem(sys.modules, "langgraph.store.sqlite.aio", None)
    monkeypatch.setitem(sys.modules, "langgraph.store.sqlite", None)

    cfg = AppConfig(checkpointer=CheckpointerConfig(type="sqlite", connection_string=":memory:"))

    with pytest.raises(ImportError, match="langgraph-checkpoint-sqlite"):

        async def _go():
            async with make_store(cfg):
                pass

        import asyncio

        asyncio.run(_go())


def test_make_store_unknown_backend_raises_valueerror():
    """未知后端 → ValueError。"""
    cfg = AppConfig()
    cfg.checkpointer = CheckpointerConfig(type="memory")
    cfg.checkpointer.type = "unknown"  # 绕过 Literal 校验

    with pytest.raises(ValueError, match="Unknown store backend"):

        async def _go():
            async with make_store(cfg):
                pass

        import asyncio

        asyncio.run(_go())
