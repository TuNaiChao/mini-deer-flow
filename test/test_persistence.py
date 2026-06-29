"""M4 persistence 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M4 测试要求）：
- engine：sqlite WAL 生效、memory no-op、auto-create、init_engine_from_config、close 重置。
- RunRepository：CRUD、user 隔离、update_status/update_run_completion 的 rowcount（红线 #12）、
  list_pending/list_inflight、aggregate_tokens_by_thread、幂等 put、UUID→str 边界（红线 #10）。
- ThreadMetaRepository：增删查改、metadata 合并、search（含 json_match）、check_access 双模式、
  InvalidMetadataFilterError、user 隔离。
- MemoryThreadMetaStore：基于 LangGraph InMemoryStore 的等价行为。
- RunStore ABC 契约 + make_thread_store 工厂。
- json_compat 校验器。

hermetic 约定：全部走临时 sqlite 文件（tmp_path），无网络、无真实模型；engine 全局状态
用 autouse fixture 在每个测试后 close，防跨测试泄漏。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from deerflow.persistence import (
    close_engine,
    get_engine,
    get_session_factory,
    init_engine,
    init_engine_from_config,
)
from deerflow.persistence.json_compat import (
    validate_metadata_filter_key,
    validate_metadata_filter_value,
)
from deerflow.persistence.run import RunRepository
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta import (
    InvalidMetadataFilterError,
    MemoryThreadMetaStore,
    ThreadMetaRepository,
    ThreadMetaStore,
    make_thread_store,
)
from deerflow.runtime.runs import DisconnectMode, RunStatus, RunStore
from deerflow.runtime.user_context import AUTO, reset_current_user, set_current_user

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _close_engine_after_test():
    """每个测试后关闭全局 engine，防止模块级全局态跨测试泄漏。"""
    yield
    await close_engine()


@pytest.fixture()
def sqlite_dir(tmp_path: Path) -> Path:
    """隔离的 sqlite 数据目录。"""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
async def session_factory(sqlite_dir: Path):
    """初始化一个 sqlite engine 并返回其 session factory。"""
    db_path = sqlite_dir / "deerflow.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    await init_engine("sqlite", url=url, sqlite_dir=str(sqlite_dir))
    sf = get_session_factory()
    assert sf is not None
    return sf


@pytest.fixture()
def repo(session_factory) -> RunRepository:
    return RunRepository(session_factory)


@pytest.fixture()
def thread_repo(session_factory) -> ThreadMetaRepository:
    return ThreadMetaRepository(session_factory)


# ---------------------------------------------------------------------------
# engine 生命周期
# ---------------------------------------------------------------------------


async def test_memory_backend_is_noop():
    """backend=memory：engine 不初始化，session factory 为 None。"""
    await init_engine("memory")
    assert get_engine() is None
    assert get_session_factory() is None


async def test_sqlite_wal_pragmas_fire(sqlite_dir: Path):
    """红线 #2：每条新连接开 WAL / synchronous=NORMAL / foreign_keys=ON / busy_timeout=30000。"""
    db_path = sqlite_dir / "deerflow.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    await init_engine("sqlite", url=url, sqlite_dir=str(sqlite_dir))

    engine = get_engine()
    assert engine is not None
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar()
        foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()

    assert mode == "wal"
    assert synchronous == 1  # NORMAL
    assert foreign_keys == 1  # ON
    assert busy_timeout == 30000  # 30s，锁竞争等待窗口（默认只有 5s）


async def test_sqlite_wal_persists_in_db_file(sqlite_dir: Path):
    """WAL 模式持久化在 db 文件头：init 后用一个全新 sqlite3 连接读出仍是 wal。

    init_engine 的 create_all 已打开过连接 → connect listener 把 db 切到 WAL（WAL
    是 db 文件级属性，写在文件头）。之后用一个未经 listener 的全新 sqlite3 连接
    读 journal_mode，仍应是 wal。
    """
    db_path = sqlite_dir / "deerflow.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    await init_engine("sqlite", url=url, sqlite_dir=str(sqlite_dir))

    raw = sqlite3.connect(db_path)
    try:
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()
    assert mode == "wal"


async def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown persistence backend"):
        await init_engine("mysql")


async def test_auto_create_tables(sqlite_dir: Path):
    """init_engine 后 create_all 自动建表（runs/threads_meta/run_events）。"""
    db_path = sqlite_dir / "deerflow.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    await init_engine("sqlite", url=url, sqlite_dir=str(sqlite_dir))
    engine = get_engine()
    async with engine.connect() as conn:
        tables = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars().all()
    assert "runs" in tables
    assert "threads_meta" in tables
    assert "run_events" in tables


async def test_init_engine_from_config_memory(tmp_path: Path):
    """init_engine_from_config 对 memory DatabaseConfig 是 no-op。"""
    from deerflow.config.database_config import DatabaseConfig

    cfg = DatabaseConfig(backend="memory")
    await init_engine_from_config(cfg)
    assert get_session_factory() is None


async def test_init_engine_from_config_sqlite(tmp_path: Path):
    """init_engine_from_config 对 sqlite DatabaseConfig 创建 engine + 建表。"""
    from deerflow.config.database_config import DatabaseConfig

    cfg = DatabaseConfig(backend="sqlite", sqlite_dir=str(tmp_path / "cfgdata"))
    await init_engine_from_config(cfg)
    assert get_session_factory() is not None
    engine = get_engine()
    async with engine.connect() as conn:
        tables = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).scalars().all()
    assert "runs" in tables


async def test_close_engine_resets_globals(sqlite_dir: Path):
    url = f"sqlite+aiosqlite:///{sqlite_dir / 'deerflow.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(sqlite_dir))
    assert get_engine() is not None
    await close_engine()
    assert get_engine() is None
    assert get_session_factory() is None


# ---------------------------------------------------------------------------
# RunRepository CRUD + 契约
# ---------------------------------------------------------------------------


def test_runstore_abc_cannot_instantiate():
    """RunStore 是 ABC，不能直接实例化。"""
    with pytest.raises(TypeError):
        RunStore()  # type: ignore[abstract]


def test_runrepository_is_runstore():
    assert issubclass(RunRepository, RunStore)
    assert RunStore in RunRepository.__mro__
    assert ThreadMetaStore in ThreadMetaRepository.__mro__


async def test_put_then_get(repo: RunRepository):
    await repo.put("run-1", thread_id="t1", user_id="alice", status="pending")
    got = await repo.get("run-1", user_id="alice")
    assert got is not None
    assert got["run_id"] == "run-1"
    assert got["thread_id"] == "t1"
    assert got["status"] == "pending"
    assert got["user_id"] == "alice"
    # JSON 列被重映射为 metadata/kwargs
    assert got["metadata"] == {}
    assert got["kwargs"] == {}
    # 时间戳是 ISO 字符串（coerce_iso）
    assert isinstance(got["created_at"], str) and "T" in got["created_at"]


async def test_put_metadata_kwargs_roundtrip(repo: RunRepository):
    await repo.put(
        "run-2",
        thread_id="t1",
        user_id="alice",
        metadata={"source": "web", "nested": {"a": 1}},
        kwargs={"model": "gpt"},
    )
    got = await repo.get("run-2", user_id="alice")
    assert got["metadata"] == {"source": "web", "nested": {"a": 1}}
    assert got["kwargs"] == {"model": "gpt"}


async def test_put_idempotent_on_retry(repo: RunRepository):
    """put 幂等：重复 put 同一 run_id 不报主键冲突，且更新字段。"""
    await repo.put("run-3", thread_id="t1", user_id="alice", status="pending")
    # 模拟 RunManager 重试
    await repo.put("run-3", thread_id="t1", user_id="alice", status="running")
    got = await repo.get("run-3", user_id="alice")
    assert got["status"] == "running"


async def test_get_returns_none_for_missing(repo: RunRepository):
    assert await repo.get("nope", user_id="alice") is None


async def test_user_isolation_get(repo: RunRepository):
    """红线：user_id 隔离——别人的 run 不可见。"""
    await repo.put("run-a", thread_id="t1", user_id="alice", status="pending")
    assert await repo.get("run-a", user_id="bob") is None
    assert await repo.get("run-a", user_id=None) is not None  # None 绕过过滤


async def test_list_by_thread(repo: RunRepository):
    await repo.put("r1", thread_id="t1", user_id="alice", status="success")
    await repo.put("r2", thread_id="t1", user_id="alice", status="error")
    await repo.put("r3", thread_id="t1", user_id="bob", status="success")  # 别人的
    result = await repo.list_by_thread("t1", user_id="alice")
    assert {r["run_id"] for r in result} == {"r1", "r2"}
    # bob 只看到自己的
    result_bob = await repo.list_by_thread("t1", user_id="bob")
    assert {r["run_id"] for r in result_bob} == {"r3"}


async def test_update_status_rowcount(repo: RunRepository):
    """红线 #12：update_status 返回 bool rowcount。"""
    await repo.put("run-s", thread_id="t1", user_id="alice", status="pending")
    assert await repo.update_status("run-s", "running") is True
    # 不存在的 run → False
    assert await repo.update_status("ghost", "running") is False


async def test_update_status_with_error(repo: RunRepository):
    await repo.put("run-e", thread_id="t1", user_id="alice")
    await repo.update_status("run-e", "error", error="boom")
    got = await repo.get("run-e", user_id=None)
    assert got["status"] == "error"
    assert got["error"] == "boom"


async def test_update_run_completion_rowcount(repo: RunRepository):
    """红线 #12：update_run_completion 返回 bool。"""
    await repo.put("run-c", thread_id="t1", user_id="alice")
    ok = await repo.update_run_completion(
        "run-c",
        status="success",
        total_tokens=500,
        total_input_tokens=300,
        total_output_tokens=200,
        llm_call_count=3,
        message_count=4,
        last_ai_message="hello",
        first_human_message="hi",
    )
    assert ok is True
    got = await repo.get("run-c", user_id=None)
    assert got["status"] == "success"
    assert got["total_tokens"] == 500
    assert got["llm_call_count"] == 3
    assert got["last_ai_message"] == "hello"
    # 不存在的 run → False
    assert await repo.update_run_completion("ghost", status="success") is False


async def test_update_model_name(repo: RunRepository):
    await repo.put("run-m", thread_id="t1", user_id="alice", model_name="  gpt-4  ")
    await repo.update_model_name("run-m", "claude")
    got = await repo.get("run-m", user_id=None)
    assert got["model_name"] == "claude"


async def test_normalize_model_name_truncates():
    """model_name 超 128 字符截断 + strip。"""
    long = "x" * 200
    assert RunRepository._normalize_model_name(f"  {long}  ") == "x" * 128
    assert RunRepository._normalize_model_name(None) is None


async def test_safe_json_handles_pydantic_and_fallback():
    """_safe_json：pydantic 对象走 model_dump，不可序列化走 str。"""
    from pydantic import BaseModel

    class P(BaseModel):
        a: int = 1

    assert RunRepository._safe_json(P()) == {"a": 1}
    assert RunRepository._safe_json({"k": [1, 2]}) == {"k": [1, 2]}
    # 不可序列化对象 → str()
    obj = object()
    assert RunRepository._safe_json(obj) == str(obj)


async def test_delete(repo: RunRepository):
    await repo.put("run-d", thread_id="t1", user_id="alice")
    await repo.delete("run-d", user_id="alice")
    assert await repo.get("run-d", user_id="alice") is None
    # 别人删不掉
    await repo.put("run-d2", thread_id="t1", user_id="alice")
    await repo.delete("run-d2", user_id="bob")  # bob 无权
    assert await repo.get("run-d2", user_id="alice") is not None


async def test_list_pending_and_inflight(repo: RunRepository):
    now = datetime.now(UTC)
    await repo.put("p1", thread_id="t1", user_id="alice", status="pending", created_at=now.isoformat())
    await repo.put("p2", thread_id="t1", user_id="alice", status="running", created_at=(now - timedelta(minutes=1)).isoformat())
    await repo.put("p3", thread_id="t1", user_id="alice", status="success", created_at=now.isoformat())

    pending = await repo.list_pending()
    assert {r["run_id"] for r in pending} == {"p1"}

    inflight = await repo.list_inflight()
    assert {r["run_id"] for r in inflight} == {"p1", "p2"}


async def test_aggregate_tokens_by_thread(repo: RunRepository):
    await repo.put("a1", thread_id="t1", user_id="alice", status="pending", model_name="gpt")
    await repo.update_run_completion("a1", status="success", total_tokens=100, total_input_tokens=60, total_output_tokens=40, lead_agent_tokens=100)
    await repo.put("a2", thread_id="t1", user_id="alice", status="pending", model_name="gpt")
    await repo.update_run_completion("a2", status="error", total_tokens=50, subagent_tokens=50)

    agg = await repo.aggregate_tokens_by_thread("t1")
    assert agg["total_tokens"] == 150
    assert agg["total_input_tokens"] == 60
    assert agg["total_output_tokens"] == 40
    assert agg["total_runs"] == 2
    assert agg["by_model"]["gpt"] == {"tokens": 150, "runs": 2}
    assert agg["by_caller"]["lead_agent"] == 100
    assert agg["by_caller"]["subagent"] == 50


async def test_aggregate_tokens_by_thread_per_model(repo: RunRepository):
    """#3658 SQL 侧：run 带 ``token_usage_by_model`` 时，按真计费模型逐模型归桶
    （一个 run 可能贡献给多个模型桶），而非按行的 model_name GROUP BY。"""
    # run-1：model_name=gpt-4，但实际用了 gpt-4(70) + gpt-4o-mini(30) 两个模型
    await repo.put("m1", thread_id="t1", user_id="alice", status="pending", model_name="gpt-4")
    await repo.update_run_completion(
        "m1",
        status="success",
        total_tokens=100,
        total_input_tokens=60,
        total_output_tokens=40,
        token_usage_by_model={
            "gpt-4": {"input_tokens": 40, "output_tokens": 30, "total_tokens": 70},
            "gpt-4o-mini": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        },
    )
    # run-2：老行，无 token_usage_by_model → 回退到 model_name
    await repo.put("m2", thread_id="t1", user_id="alice", status="pending", model_name="claude")
    await repo.update_run_completion("m2", status="success", total_tokens=50, lead_agent_tokens=50)

    agg = await repo.aggregate_tokens_by_thread("t1")
    assert agg["total_tokens"] == 150
    assert agg["total_runs"] == 2
    # 按真计费模型归桶：gpt-4 只收 run-1 的 70，gpt-4o-mini 收 30，claude 回退收 50
    assert agg["by_model"]["gpt-4"] == {"tokens": 70, "runs": 1}
    assert agg["by_model"]["gpt-4o-mini"] == {"tokens": 30, "runs": 1}
    assert agg["by_model"]["claude"] == {"tokens": 50, "runs": 1}
    assert agg["by_caller"]["lead_agent"] == 50


async def test_update_run_completion_persists_token_usage_by_model(repo: RunRepository):
    """#3658 SQL 侧：update_run_completion 把 token_usage_by_model 写进列。"""
    await repo.put("c1", thread_id="t1", user_id="alice", status="pending", model_name="gpt")
    await repo.update_run_completion(
        "c1",
        status="success",
        total_tokens=10,
        token_usage_by_model={"gpt": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}},
    )
    got = await repo.get("c1", user_id=None)
    assert got["token_usage_by_model"] == {"gpt": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}}


async def test_update_run_progress_persists_token_usage_by_model(repo: RunRepository):
    """#3658 SQL 侧：update_run_progress 也写 token_usage_by_model（仅 status=running 行）。"""
    await repo.put("p1", thread_id="t1", user_id="alice", status="running", model_name="gpt")
    await repo.update_run_progress(
        "p1",
        total_tokens=10,
        token_usage_by_model={"gpt": {"total_tokens": 10}},
    )
    got = await repo.get("p1", user_id=None)
    assert got["token_usage_by_model"] == {"gpt": {"total_tokens": 10}}

    # None 时不覆盖已有值
    await repo.update_run_progress("p1", total_tokens=20)
    got2 = await repo.get("p1", user_id=None)
    assert got2["token_usage_by_model"] == {"gpt": {"total_tokens": 10}}


async def test_update_run_progress_only_running(repo: RunRepository):
    """update_run_progress 仅更新 status=running 的行；非 running 行不变。"""
    await repo.put("prog1", thread_id="t1", user_id="alice", status="pending")
    await repo.update_run_progress("prog1", total_tokens=999)
    got = await repo.get("prog1", user_id=None)
    assert got["total_tokens"] == 0  # pending 行未被更新


async def test_uuid_to_str_boundary():
    """红线 #10：contextvar 里的 UUID.id 在边界 str() 化后入库（aiosqlite 不能绑 UUID 到 VARCHAR）。"""
    from deerflow.runtime.user_context import resolve_user_id

    uid = uuid.uuid4()
    token = set_current_user(SimpleNamespace(id=uid))
    try:
        assert resolve_user_id(AUTO, method_name="test") == str(uid)
    finally:
        reset_current_user(token)


async def test_uuid_user_id_stored_as_str(repo: RunRepository):
    """端到端：UUID 用户经 contextvar 解析后，库里存的是 str。"""
    uid = uuid.uuid4()
    token = set_current_user(SimpleNamespace(id=uid))
    try:
        await repo.put("run-uuid", thread_id="t1", user_id=AUTO)
    finally:
        reset_current_user(token)

    # 用 str(uid) 显式取回（AUTO 此刻已无用户上下文，需显式）
    got = await repo.get("run-uuid", user_id=str(uid))
    assert got is not None
    assert got["user_id"] == str(uid)
    assert not isinstance(got["user_id"], uuid.UUID)


# ---------------------------------------------------------------------------
# ThreadMetaRepository
# ---------------------------------------------------------------------------


async def test_threadmeta_create_get(thread_repo: ThreadMetaRepository):
    rec = await thread_repo.create("th1", user_id="alice", display_name="My Thread", metadata={"k": "v"})
    assert rec["thread_id"] == "th1"
    assert rec["display_name"] == "My Thread"
    assert rec["metadata"] == {"k": "v"}
    assert rec["status"] == "idle"

    got = await thread_repo.get("th1", user_id="alice")
    assert got is not None
    assert got["display_name"] == "My Thread"
    assert "T" in got["created_at"]  # coerce_iso 归一


async def test_threadmeta_user_isolation(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th2", user_id="alice")
    assert await thread_repo.get("th2", user_id="bob") is None
    assert await thread_repo.get("th2", user_id="alice") is not None
    # None 绕过
    assert await thread_repo.get("th2", user_id=None) is not None


async def test_threadmeta_update_display_name(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th3", user_id="alice")
    await thread_repo.update_display_name("th3", "Renamed", user_id="alice")
    got = await thread_repo.get("th3", user_id="alice")
    assert got["display_name"] == "Renamed"
    # bob 无权改名 → no-op
    await thread_repo.update_display_name("th3", "Hacked", user_id="bob")
    got = await thread_repo.get("th3", user_id="alice")
    assert got["display_name"] == "Renamed"


async def test_threadmeta_update_status(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th4", user_id="alice")
    await thread_repo.update_status("th4", "busy", user_id="alice")
    assert (await thread_repo.get("th4", user_id="alice"))["status"] == "busy"


async def test_threadmeta_update_metadata_merges(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th5", user_id="alice", metadata={"a": 1, "b": 2})
    await thread_repo.update_metadata("th5", {"b": 20, "c": 3}, user_id="alice")
    got = await thread_repo.get("th5", user_id="alice")
    assert got["metadata"] == {"a": 1, "b": 20, "c": 3}  # b 被覆盖，a 保留，c 新增


async def test_threadmeta_update_owner(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th6", user_id="alice")
    await thread_repo.update_owner("th6", "carol", user_id="alice")
    # alice 不再拥有
    assert await thread_repo.get("th6", user_id="alice") is None
    assert (await thread_repo.get("th6", user_id="carol")) is not None


async def test_threadmeta_check_access(thread_repo: ThreadMetaRepository):
    await thread_repo.create("th7", user_id="alice")
    # 宽松模式（默认）
    assert await thread_repo.check_access("th7", "alice") is True
    assert await thread_repo.check_access("th7", "bob") is False
    assert await thread_repo.check_access("ghost", "alice") is True  # 行缺失=可访问（legacy）
    # 严格模式
    assert await thread_repo.check_access("th7", "alice", require_existing=True) is True
    assert await thread_repo.check_access("ghost", "alice", require_existing=True) is False


async def test_threadmeta_check_access_null_owner(thread_repo: ThreadMetaRepository, session_factory):
    """user_id=None 的行（迁移遗留）对所有人可访问。"""
    await thread_repo.create("th-null", user_id=None)
    assert await thread_repo.check_access("th-null", "anyone") is True


async def test_threadmeta_search_with_metadata_filter(thread_repo: ThreadMetaRepository):
    await thread_repo.create("s1", user_id="alice", metadata={"team": "x", "prio": 1})
    await thread_repo.create("s2", user_id="alice", metadata={"team": "y", "prio": 1})
    await thread_repo.create("s3", user_id="bob", metadata={"team": "x"})
    # json_match 端到端：alice 的 team=x 线程
    result = await thread_repo.search(metadata={"team": "x"}, user_id="alice")
    assert {r["thread_id"] for r in result} == {"s1"}


async def test_threadmeta_search_rejects_all_unsafe_keys(thread_repo: ThreadMetaRepository):
    """所有 metadata 键都不安全（含非法字符）→ InvalidMetadataFilterError。"""
    await thread_repo.create("s4", user_id="alice")
    with pytest.raises(InvalidMetadataFilterError):
        await thread_repo.search(metadata={"bad key!": "v"}, user_id="alice")


async def test_threadmeta_search_status_filter(thread_repo: ThreadMetaRepository):
    await thread_repo.create("st1", user_id="alice")
    await thread_repo.update_status("st1", "busy", user_id="alice")
    await thread_repo.create("st2", user_id="alice")
    result = await thread_repo.search(status="busy", user_id="alice")
    assert {r["thread_id"] for r in result} == {"st1"}


async def test_threadmeta_delete(thread_repo: ThreadMetaRepository):
    await thread_repo.create("del1", user_id="alice")
    await thread_repo.delete("del1", user_id="alice")
    assert await thread_repo.get("del1", user_id="alice") is None
    # bob 删不掉
    await thread_repo.create("del2", user_id="alice")
    await thread_repo.delete("del2", user_id="bob")
    assert await thread_repo.get("del2", user_id="alice") is not None


# ---------------------------------------------------------------------------
# MemoryThreadMetaStore（LangGraph BaseStore 后端）
# ---------------------------------------------------------------------------


@pytest.fixture()
def base_store():
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


async def test_memory_thread_store_crud(base_store):
    store = MemoryThreadMetaStore(base_store)
    rec = await store.create("m1", user_id="alice", display_name="Mem", metadata={"k": 1})
    assert rec["thread_id"] == "m1"
    got = await store.get("m1", user_id="alice")
    assert got is not None
    assert got["display_name"] == "Mem"
    assert got["metadata"] == {"k": 1}

    await store.update_metadata("m1", {"k": 2, "j": 3}, user_id="alice")
    assert (await store.get("m1", user_id="alice"))["metadata"] == {"k": 2, "j": 3}

    await store.delete("m1", user_id="alice")
    assert await store.get("m1", user_id="alice") is None


async def test_memory_thread_store_check_access(base_store):
    store = MemoryThreadMetaStore(base_store)
    await store.create("m2", user_id="alice")
    assert await store.check_access("m2", "alice") is True
    assert await store.check_access("m2", "bob") is False
    assert await store.check_access("ghost", "alice") is True
    assert await store.check_access("ghost", "alice", require_existing=True) is False


async def test_memory_thread_store_search_user_filter(base_store):
    store = MemoryThreadMetaStore(base_store)
    await store.create("m3", user_id="alice")
    await store.create("m4", user_id="bob")
    alice = await store.search(user_id="alice")
    assert {r["thread_id"] for r in alice} == {"m3"}


async def test_memory_thread_store_coerces_legacy_timestamp(base_store):
    """coerce_iso：search 路径把 legacy unix 时间戳归一成 ISO。

    ``get`` 返回原始记录（不经 _item_to_dict）；``search`` 经 _item_to_dict 调
    coerce_iso，把早期版本用 ``str(time.time())`` 写入的 unix 秒值翻译成 ISO。
    """
    store = MemoryThreadMetaStore(base_store)
    # 直接往底层 store 写一条 legacy 时间戳的记录
    await base_store.aput(("threads",), "legacy", {"user_id": "alice", "status": "idle", "created_at": "1700000000.0", "updated_at": "1700000000.0"})
    result = await store.search(user_id="alice")
    found = [r for r in result if r["thread_id"] == "legacy"]
    assert found
    assert "T" in found[0]["created_at"]  # unix 秒 → ISO
    assert "T" in found[0]["updated_at"]


# ---------------------------------------------------------------------------
# make_thread_store 工厂
# ---------------------------------------------------------------------------


async def test_make_thread_store_sql_when_factory(session_factory):
    store = make_thread_store(session_factory)
    assert isinstance(store, ThreadMetaRepository)


async def test_make_thread_store_memory_when_store(base_store):
    store = make_thread_store(None, store=base_store)
    assert isinstance(store, MemoryThreadMetaStore)


async def test_make_thread_store_requires_one_or_the_other():
    with pytest.raises(ValueError, match="requires either"):
        make_thread_store(None, store=None)


# ---------------------------------------------------------------------------
# json_compat 校验器
# ---------------------------------------------------------------------------


def test_validate_key_safe_and_unsafe():
    assert validate_metadata_filter_key("team") is True
    assert validate_metadata_filter_key("user-id_1") is True
    assert validate_metadata_filter_key("bad key!") is False
    assert validate_metadata_filter_key("a;b") is False
    assert validate_metadata_filter_key(123) is False  # 非字符串


def test_validate_value_safe_and_unsafe():
    assert validate_metadata_filter_value(None) is True
    assert validate_metadata_filter_value(True) is True
    assert validate_metadata_filter_value(42) is True
    assert validate_metadata_filter_value(1.5) is True
    assert validate_metadata_filter_value("str") is True
    # 超出 int64 范围
    assert validate_metadata_filter_value(2**63) is False
    # 不允许的类型
    assert validate_metadata_filter_value([1, 2]) is False
    assert validate_metadata_filter_value({"a": 1}) is False
    # bool 是 int 子类但被接受为 bool
    assert validate_metadata_filter_value(False) is True


def test_base_to_dict_and_repr():
    """Base.to_dict / __repr__ 通过 SQLAlchemy inspect 工作。"""
    # 显式给值：列 default 仅在 flush 时生效，瞬态对象上需显式设。
    row = RunRow(run_id="r", thread_id="t", status="custom")
    d = row.to_dict()
    assert d["run_id"] == "r"
    assert d["thread_id"] == "t"
    assert d["status"] == "custom"
    # exclude
    d2 = row.to_dict(exclude={"run_id"})
    assert "run_id" not in d2
    assert "thread_id" in d2
    # repr
    r = repr(row)
    assert "RunRow(" in r and "run_id='r'" in r


# ---------------------------------------------------------------------------
# runs 领域枚举
# ---------------------------------------------------------------------------


def test_runstatus_str_enum_values():
    assert RunStatus.pending == "pending"
    assert RunStatus.running == "running"
    assert RunStatus.success == "success"
    assert RunStatus.error == "error"
    assert RunStatus.timeout == "timeout"
    assert RunStatus.interrupted == "interrupted"
    # StrEnum：可与字符串比较
    assert RunStatus.success == "success"


def test_disconnect_mode_values():
    assert DisconnectMode.cancel == "cancel"
    assert DisconnectMode.continue_ == "continue"
