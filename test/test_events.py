"""M6 events/store 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M6 测试要求）：
- MemoryRunEventStore：seq 单调、list_messages 分页、list_events/list_messages_by_run、
  count、delete（thread/run）、message 投影一致性。
- JsonlRunEventStore：seq 单调 + 跨实例持久化（lazy seq load）、路径穿越拒绝（红线 #4）、
  并发写锁（红线 #3）、delete_by_thread 清计数器/锁、跨 run 文件统一 seq、IO 卸载。
- DbRunEventStore：FOR UPDATE seq 单调（红线 #3）、trace 截断、JSON content 往返、
  user_id stamp + UUID→str（红线 #10）、用户隔离、双向游标分页、put_batch 跨 thread 拒绝。
- make_run_event_store 工厂。

hermetic 约定：jsonl 用 tmp base_dir；db 用 tmp sqlite engine，autouse fixture 每测后
close engine 防泄漏；无网络、无真实模型。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.config.run_events_config import RunEventsConfig
from deerflow.runtime.events import make_run_event_store
from deerflow.runtime.events.store.base import RunEventStore
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.store.jsonl import JsonlRunEventStore
from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.user_context import reset_current_user, set_current_user

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _close_engine_after_test():
    """每个测试后关闭全局 persistence engine，防跨测试泄漏（db 后端用）。"""
    yield
    from deerflow.persistence.engine import close_engine

    await close_engine()


@pytest.fixture()
def mem_store() -> MemoryRunEventStore:
    return MemoryRunEventStore()


@pytest.fixture()
def jsonl_store(tmp_path: Path) -> JsonlRunEventStore:
    return JsonlRunEventStore(base_dir=tmp_path)


@pytest.fixture()
async def db_store(tmp_path: Path) -> DbRunEventStore:
    from deerflow.persistence.engine import get_session_factory, init_engine

    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'events.db'}", sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    assert sf is not None
    return DbRunEventStore(sf, max_trace_content=10)


# ---------------------------------------------------------------------------
# MemoryRunEventStore
# ---------------------------------------------------------------------------


class TestMemoryStore:
    async def test_seq_strictly_increasing(self, mem_store):
        r1 = await mem_store.put(thread_id="t1", run_id="r1", event_type="msg", category="message", content="hi")
        r2 = await mem_store.put(thread_id="t1", run_id="r1", event_type="msg", category="message", content="yo")
        assert r1["seq"] == 1
        assert r2["seq"] == 2

    async def test_seq_per_thread_independent(self, mem_store):
        a = await mem_store.put(thread_id="tA", run_id="r1", event_type="m", category="message", content="a")
        b = await mem_store.put(thread_id="tB", run_id="r1", event_type="m", category="message", content="b")
        assert a["seq"] == 1 and b["seq"] == 1  # 不同 thread 各自从 1 起

    async def test_put_batch_assigns_seq(self, mem_store):
        results = await mem_store.put_batch(
            [
                {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "1"},
                {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "2"},
                {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "3"},
            ]
        )
        assert [r["seq"] for r in results] == [1, 2, 3]

    async def test_list_messages_filters_category(self, mem_store):
        await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="msg1")
        await mem_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content="trace1")
        await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="msg2")
        msgs = await mem_store.list_messages("t1")
        assert [m["content"] for m in msgs] == ["msg1", "msg2"]  # 不含 trace

    async def test_list_messages_pagination(self, mem_store):
        for i in range(5):
            await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content=f"m{i}")
        # 默认：最近 limit 条，升序
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=3)] == ["m2", "m3", "m4"]
        # after_seq：游标之后前 limit 条
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=10, after_seq=2)] == ["m2", "m3", "m4"]
        # before_seq：游标之前最后 limit 条
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=2, before_seq=4)] == ["m1", "m2"]

    async def test_list_events_with_filter(self, mem_store):
        await mem_store.put(thread_id="t1", run_id="r1", event_type="alpha", category="trace", content="a")
        await mem_store.put(thread_id="t1", run_id="r1", event_type="beta", category="trace", content="b")
        await mem_store.put(thread_id="t1", run_id="r2", event_type="alpha", category="trace", content="c")
        events = await mem_store.list_events("t1", "r1", event_types=["alpha"])
        assert [e["content"] for e in events] == ["a"]  # 只 r1 + alpha

    async def test_count_and_delete(self, mem_store):
        await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await mem_store.put(thread_id="t1", run_id="r2", event_type="m", category="message", content="b")
        await mem_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content="x")
        assert await mem_store.count_messages("t1") == 2

        n = await mem_store.delete_by_run("t1", "r1")
        assert n == 2  # 1 message + 1 trace
        # message 投影同步更新
        assert await mem_store.count_messages("t1") == 1
        msgs = await mem_store.list_messages("t1")
        assert [m["run_id"] for m in msgs] == ["r2"]

        n2 = await mem_store.delete_by_thread("t1")
        assert n2 == 1
        assert await mem_store.count_messages("t1") == 0

    async def test_delete_resets_seq_counter(self, mem_store):
        await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await mem_store.delete_by_thread("t1")
        r = await mem_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="b")
        assert r["seq"] == 1  # 计数器已清，重新从 1 起


# ---------------------------------------------------------------------------
# JsonlRunEventStore
# ---------------------------------------------------------------------------


class TestJsonlStore:
    async def test_seq_monotonic_and_persisted(self, jsonl_store, tmp_path):
        r1 = await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        r2 = await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="b")
        assert r1["seq"] < r2["seq"]
        # 文件落盘
        assert (tmp_path / "threads" / "t1" / "runs" / "r1.jsonl").exists()

    async def test_lazy_seq_load_across_instances(self, jsonl_store, tmp_path):
        """新 store 实例（同 base_dir）从现有文件 lazy 加载最大 seq，继续单调。"""
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="b")
        # 新实例
        store2 = JsonlRunEventStore(base_dir=tmp_path)
        r = await store2.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="c")
        assert r["seq"] == 3  # 从已有 max(2)+1 继续

    async def test_path_traversal_rejected(self, jsonl_store):
        """红线 #4：非法 thread_id / run_id 拒绝（防 ``../`` 逃逸）。"""
        with pytest.raises(ValueError, match="Invalid thread_id"):
            await jsonl_store.put(thread_id="../escape", run_id="r1", event_type="m", category="message", content="x")
        with pytest.raises(ValueError, match="Invalid run_id"):
            await jsonl_store.put(thread_id="t1", run_id="../../etc/passwd", event_type="m", category="message", content="x")
        # 空也拒绝
        with pytest.raises(ValueError):
            await jsonl_store.put(thread_id="", run_id="r1", event_type="m", category="message", content="x")

    async def test_validate_id_allows_safe(self):
        assert JsonlRunEventStore._validate_id("thread_1", "thread_id") == "thread_1"
        assert JsonlRunEventStore._validate_id("run-abc", "run_id") == "run-abc"

    async def test_concurrent_writes_serialized(self, jsonl_store):
        """红线 #3：同 thread 并发写被 per-thread lock 串行化，seq 全局唯一且递增。"""

        async def one(i):
            return await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content=f"c{i}")

        results = await asyncio.gather(*[one(i) for i in range(20)])
        seqs = [r["seq"] for r in results]
        assert sorted(seqs) == list(range(1, 21))  # 无重复、连续

    async def test_list_messages_across_runs_unified_seq(self, jsonl_store):
        """跨多个 run 文件的消息按统一 seq 排序。"""
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await jsonl_store.put(thread_id="t1", run_id="r2", event_type="m", category="message", content="b")
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="c")
        msgs = await jsonl_store.list_messages("t1")
        assert [m["content"] for m in msgs] == ["a", "b", "c"]  # 按 seq 跨 run 排序
        assert [m["seq"] for m in msgs] == [1, 2, 3]

    async def test_delete_by_thread_clears_counter(self, jsonl_store, tmp_path):
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        n = await jsonl_store.delete_by_thread("t1")
        assert n == 1
        # 计数器 + 锁被清，新写入重新 lazy load（文件已删 → 从 1 起）
        r = await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="b")
        assert r["seq"] == 1

    async def test_delete_by_run(self, jsonl_store):
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await jsonl_store.put(thread_id="t1", run_id="r2", event_type="m", category="message", content="b")
        n = await jsonl_store.delete_by_run("t1", "r1")
        assert n == 1
        msgs = await jsonl_store.list_messages("t1")
        assert [m["run_id"] for m in msgs] == ["r2"]

    async def test_io_offloaded_via_to_thread(self, jsonl_store, monkeypatch):
        """红线 #1：文件 IO 经 asyncio.to_thread 卸载。"""
        import deerflow.runtime.events.store.jsonl as mod

        called: list = []
        real = mod.asyncio.to_thread

        async def spy(fn, *args, **kwargs):
            called.append(fn)
            return await real(fn, *args, **kwargs)

        monkeypatch.setattr(mod.asyncio, "to_thread", spy)
        await jsonl_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        assert called, "jsonl put 未走 asyncio.to_thread"


# ---------------------------------------------------------------------------
# DbRunEventStore
# ---------------------------------------------------------------------------


class TestDbStore:
    async def test_seq_monotonic_for_update(self, db_store):
        """红线 #3：db 用 FOR UPDATE 串行化写者，seq 单调。"""
        r1 = await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        r2 = await db_store.put(thread_id="t1", run_id="r2", event_type="m", category="message", content="b")
        r3 = await db_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content="c")
        assert r1["seq"] == 1 and r2["seq"] == 2 and r3["seq"] == 3

    async def test_concurrent_writes_different_threads_ok(self, db_store):
        """并发写不同 thread：各自 seq 从 1 起，互不影响（锁是 thread 级，非全局）。"""

        async def one(tid):
            return await db_store.put(thread_id=tid, run_id="r1", event_type="m", category="message", content="x")

        results = await asyncio.gather(*[one(f"t{i}") for i in range(10)])
        # 每个 thread 第一条都是 seq=1
        assert sorted(r["seq"] for r in results) == [1] * 10
        assert len({r["thread_id"] for r in results}) == 10

    async def test_unique_constraint_backstops_duplicate_seq(self, db_store):
        """sqlite 上 ``FOR UPDATE`` 是 no-op；靠 ``UNIQUE(thread_id, seq)`` 约束兜底防重复 seq。

        生产中同 thread 的并发写不会发生——RunJournal 用 ``put_batch`` 在单事务里批量写
        （一次 ``max(seq)`` 读取分配整批 seq）；单条 ``put`` 是低频路径（每 run 一次
        human_message）。真正的「并发串行化」在 postgres 上由 ``pg_advisory_xact_lock``
        保证（见 :meth:`DbRunEventStore._max_seq_for_thread`）。
        """
        from deerflow.persistence.models.run_event import RunEventRow

        async with db_store._sf() as session:
            session.add(RunEventRow(thread_id="t1", run_id="r1", event_type="m", category="message", content="a", seq=1))
            await session.commit()
        # 手动插入重复 (thread_id, seq) → 约束拒绝
        async with db_store._sf() as session:
            session.add(RunEventRow(thread_id="t1", run_id="r2", event_type="m", category="message", content="b", seq=1))
            with pytest.raises(Exception, match="UNIQUE constraint"):
                await session.commit()

    async def test_trace_truncation(self, db_store):
        """db 截断超长 trace 内容（max_trace_content=10）。"""
        long_content = "abcdefghijklmnopqrstuvwxyz"  # 26 字节 > 10
        r = await db_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content=long_content)
        assert len(r["content"]) <= 10
        assert r["metadata"].get("content_truncated") is True
        assert r["metadata"].get("original_byte_length") == 26

    async def test_trace_not_truncated_under_limit(self, db_store):
        r = await db_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content="short")
        assert r["content"] == "short"
        assert "content_truncated" not in (r["metadata"] or {})

    async def test_json_content_roundtrip(self, db_store):
        """dict content 经 JSON 序列化写入、读回还原成 dict。"""
        payload = {"text": "hello", "count": 3, "nested": {"x": 1}}
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content=payload)
        msgs = await db_store.list_messages("t1")
        assert msgs[0]["content"] == payload  # 还原成 dict
        assert msgs[0]["metadata"].get("content_is_dict") is True

    async def test_string_content_not_marked_json(self, db_store):
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="plain text")
        msgs = await db_store.list_messages("t1")
        assert msgs[0]["content"] == "plain text"
        assert "content_is_json" not in (msgs[0]["metadata"] or {})

    async def test_user_id_stamped_from_context(self, db_store):
        """写时从 contextvar stamp user_id（autouse 注入 test-user-autouse）。"""
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        msgs = await db_store.list_messages("t1", user_id=None)  # None 绕过过滤读原始行
        assert msgs[0]["user_id"] == "test-user-autouse"

    async def test_user_id_uuid_coerced_to_str(self, db_store):
        """红线 #10：contextvar 里的 UUID.id 在 stamp 时 str() 化。"""
        uid = uuid.uuid4()
        token = set_current_user(SimpleNamespace(id=uid))
        try:
            await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        finally:
            reset_current_user(token)
        msgs = await db_store.list_messages("t1", user_id=None)
        assert msgs[0]["user_id"] == str(uid)
        assert not isinstance(msgs[0]["user_id"], uuid.UUID)

    async def test_user_isolation(self, db_store):
        """list_messages 按 user_id 过滤。"""
        # autouse 已 stamp "test-user-autouse"
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="mine")
        assert await db_store.list_messages("t1", user_id="test-user-autouse")  # 自己可见
        assert await db_store.list_messages("t1", user_id="other") == []  # 别人不可见
        assert await db_store.list_messages("t1", user_id=None)  # None 绕过

    async def test_cursor_pagination(self, db_store):
        for i in range(5):
            await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content=f"m{i}")
        # after_seq 正向
        assert [m["content"] for m in await db_store.list_messages("t1", limit=10, after_seq=2)] == ["m2", "m3", "m4"]
        # before_seq 反向
        assert [m["content"] for m in await db_store.list_messages("t1", limit=2, before_seq=4)] == ["m1", "m2"]
        # 默认最近
        assert [m["content"] for m in await db_store.list_messages("t1", limit=3)] == ["m2", "m3", "m4"]

    async def test_list_events_and_count_and_delete(self, db_store):
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content="a")
        await db_store.put(thread_id="t1", run_id="r1", event_type="t", category="trace", content="b")
        await db_store.put(thread_id="t1", run_id="r2", event_type="m", category="message", content="c")

        events = await db_store.list_events("t1", "r1", user_id=None)
        assert len(events) == 2
        assert await db_store.count_messages("t1", user_id=None) == 2

        n = await db_store.delete_by_run("t1", "r1", user_id=None)
        assert n == 2
        assert await db_store.count_messages("t1", user_id=None) == 1

        n2 = await db_store.delete_by_thread("t1", user_id=None)
        assert n2 == 1

    async def test_put_batch_cross_thread_rejected(self, db_store):
        with pytest.raises(ValueError, match="same thread"):
            await db_store.put_batch(
                [
                    {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "a"},
                    {"thread_id": "t2", "run_id": "r1", "event_type": "m", "category": "message", "content": "b"},
                ]
            )

    async def test_put_batch_seq_monotonic(self, db_store):
        results = await db_store.put_batch(
            [
                {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "a"},
                {"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": "b"},
            ]
        )
        assert [r["seq"] for r in results] == [1, 2]


# ---------------------------------------------------------------------------
# make_run_event_store 工厂
# ---------------------------------------------------------------------------


class TestFactory:
    def test_memory_default(self):
        assert isinstance(make_run_event_store(None), MemoryRunEventStore)
        assert isinstance(make_run_event_store(RunEventsConfig(backend="memory")), MemoryRunEventStore)

    def test_jsonl(self):
        assert isinstance(make_run_event_store(RunEventsConfig(backend="jsonl")), JsonlRunEventStore)

    async def test_db_falls_back_when_no_engine(self):
        """database.backend=memory（engine 未初始化）+ run_events.backend=db → 回退内存。"""
        store = make_run_event_store(RunEventsConfig(backend="db"))
        assert isinstance(store, MemoryRunEventStore)

    async def test_db_when_engine_ready(self, tmp_path):
        from deerflow.persistence.engine import close_engine, init_engine

        await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", sqlite_dir=str(tmp_path))
        try:
            store = make_run_event_store(RunEventsConfig(backend="db", max_trace_content=512))
            assert isinstance(store, DbRunEventStore)
            assert store._max_trace_content == 512
        finally:
            await close_engine()

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown run_events backend"):
            make_run_event_store(RunEventsConfig.model_construct(backend="mysql"))


# ---------------------------------------------------------------------------
# RunEventStore ABC 契约
# ---------------------------------------------------------------------------


def test_run_event_store_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        RunEventStore()  # type: ignore[abstract]


def test_all_backends_are_run_event_store():
    assert issubclass(MemoryRunEventStore, RunEventStore)
    assert issubclass(JsonlRunEventStore, RunEventStore)
    assert issubclass(DbRunEventStore, RunEventStore)


# ---------------------------------------------------------------------------
# postgres advisory-lock 分支（sqlite 上 FOR UPDATE 是 no-op；这里用 FakeSession
# 锁住 _max_seq_for_thread 的 SQL 分支语义，防重构时丢失 advisory lock / 误加 FOR UPDATE）
# ---------------------------------------------------------------------------


class TestPostgresMaxSeqBranch:
    @staticmethod
    def _make_session(dialect_name: str):
        bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

        class _FakeSession:
            def __init__(self) -> None:
                self.executed_sql: list[str] = []
                self.scalar_for_update: bool | None = None

            def get_bind(self):
                return bind

            async def execute(self, statement, params=None):
                self.executed_sql.append(str(statement))

            async def scalar(self, statement):
                # with_for_update() 会设 Select 的 _for_update_arg（ForUpdateArg 对象）；裸 select 为 None。
                # 注意 ForUpdateArg 是 SQLAlchemy clause，不能 bool()（会 raise），故用 is not None。
                self.scalar_for_update = getattr(statement, "_for_update_arg", None) is not None
                return 5  # 假 max(seq)

        return _FakeSession()

    async def test_postgres_uses_advisory_lock_without_for_update(self):
        """postgres 分支：执行 pg_advisory_xact_lock，max(seq) 的 SELECT 不带 FOR UPDATE
        （postgres 拒绝对聚合结果加 FOR UPDATE）。"""
        session = self._make_session("postgresql")
        max_seq = await DbRunEventStore._max_seq_for_thread(session, "t1")
        assert max_seq == 5
        assert any("pg_advisory_xact_lock" in sql for sql in session.executed_sql)
        assert session.scalar_for_update is False  # 聚合 SELECT 未加 FOR UPDATE

    async def test_non_postgres_uses_for_update(self):
        """非 postgres（sqlite 等）：max(seq) 走 stmt.with_for_update()，无 advisory lock。"""
        session = self._make_session("sqlite")
        await DbRunEventStore._max_seq_for_thread(session, "t1")
        assert session.scalar_for_update is True
        assert not any("pg_advisory_xact_lock" in sql for sql in session.executed_sql)


# ---------------------------------------------------------------------------
# message/trace 交错的 cursor 边界（bisect 排他语义易错点）
# ---------------------------------------------------------------------------


class TestCursorInterleaved:
    async def test_cursor_boundaries_with_interleaved_trace(self, mem_store):
        """message seq=[1,3,5,7,9]（中间 2/4/6/8 是 trace）。

        重点锁住「cursor 恰在 message seq 上」的排他语义——若误用 bisect_right
        （before）/bisect_left（after），before_seq=5 / after_seq=5 会越界。
        """
        for seq in range(1, 10):
            is_msg = seq % 2 == 1
            await mem_store.put(
                thread_id="t1",
                run_id="r1",
                event_type="m" if is_msg else "t",
                category="message" if is_msg else "trace",
                content=f"m{seq}",
            )
        # before_seq=6（gap 中）→ seq<6 的最后 2 条 = [3,5]
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=2, before_seq=6)] == ["m3", "m5"]
        # before_seq=5（恰在 message seq 上，排他）→ seq<5 = [1,3]
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=2, before_seq=5)] == ["m1", "m3"]
        # after_seq=4（gap 中）→ seq>4 的前 2 条 = [5,7]
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=2, after_seq=4)] == ["m5", "m7"]
        # after_seq=5（恰在 message seq 上）→ seq>5 = [7,9]
        assert [m["content"] for m in await mem_store.list_messages("t1", limit=2, after_seq=5)] == ["m7", "m9"]


# ---------------------------------------------------------------------------
# list 结构化 content 往返（content_is_dict 是 dict 专属 flag）
# ---------------------------------------------------------------------------


class TestDbStructuredContent:
    async def test_list_content_roundtrip_not_marked_dict(self, db_store):
        """list content → content_is_json=True 但**无** content_is_dict（dict 专属 flag）。"""
        payload = [{"x": 1}, {"y": 2}]
        await db_store.put(thread_id="t1", run_id="r1", event_type="m", category="message", content=payload)
        msgs = await db_store.list_messages("t1")
        assert msgs[0]["content"] == payload
        assert msgs[0]["metadata"].get("content_is_json") is True
        assert "content_is_dict" not in msgs[0]["metadata"]

    async def test_list_content_via_put_batch(self, db_store):
        """list content 经 put_batch 也能正确往返。"""
        payload = [1, 2, 3]
        await db_store.put_batch([{"thread_id": "t1", "run_id": "r1", "event_type": "m", "category": "message", "content": payload}])
        msgs = await db_store.list_messages("t1")
        assert msgs[0]["content"] == payload
        assert msgs[0]["metadata"].get("content_is_json") is True
        assert "content_is_dict" not in msgs[0]["metadata"]
