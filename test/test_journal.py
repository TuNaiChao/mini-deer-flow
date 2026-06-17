"""M7 journal 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M7 测试要求）：
- token 分桶（lead_agent / subagent / middleware）+ 按 run_id 去重防双计。
- sync→async flush：同步回调内检测事件循环 → create_task 调度 put_batch。
- 失败 batch 回插（红线 #8）：put_batch 抛错 → batch 回 buffer，下次 flush 重试。
- progress 节流：progress_reporter 节流（用 AsyncMock callable），不一次调用一次。
- error fallback：deerflow_error_fallback 标记 → had_llm_error_fallback + 消息。
- record_middleware：中间件状态变更事件。
- 首条 human 抽取（on_chat_model_start）。
- record_external_llm_usage_records 去重。
- caller 识别（tags）。
- last_ai_message 只被 lead_agent 的非空 AI 文本更新。

hermetic 约定：event_store 用 M6 的 MemoryRunEventStore（真实、内存）；LLM 响应用真实
langchain AIMessage + SimpleNamespace 包装 generations；无网络、无真实模型。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from deerflow.runtime.events.store.memory import MemoryRunEventStore
from deerflow.runtime.journal import RunJournal

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _llm_response(message):
    """构造 on_llm_end 的 response：.generations = [[gen]]，gen.message = message。"""
    gen = SimpleNamespace(message=message)
    return SimpleNamespace(generations=[[gen]])


class _FlakyStore:
    """put_batch 第一次抛错、之后成功的假 store（测失败回插）。"""

    def __init__(self):
        self.calls = 0
        self._inner = MemoryRunEventStore()

    async def put_batch(self, events):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated db locked")
        return await self._inner.put_batch(events)


@pytest.fixture()
def store() -> MemoryRunEventStore:
    return MemoryRunEventStore()


@pytest.fixture()
def journal(store) -> RunJournal:
    return RunJournal("run-1", "thread-1", store, flush_threshold=50)


# ---------------------------------------------------------------------------
# caller 识别
# ---------------------------------------------------------------------------


class TestCallerIdentification:
    def test_default_lead_agent(self, journal):
        assert journal._identify_caller(None) == "lead_agent"
        assert journal._identify_caller([]) == "lead_agent"
        assert journal._identify_caller(["other"]) == "lead_agent"

    def test_subagent_tag(self, journal):
        assert journal._identify_caller(["subagent:general-purpose"]) == "subagent:general-purpose"

    def test_middleware_tag(self, journal):
        assert journal._identify_caller(["middleware:title"]) == "middleware:title"

    def test_lead_agent_explicit(self, journal):
        assert journal._identify_caller(["lead_agent"]) == "lead_agent"


# ---------------------------------------------------------------------------
# token 分桶 + 去重
# ---------------------------------------------------------------------------


class TestTokenBucketing:
    async def test_lead_agent_tokens(self, journal):
        msg = AIMessage(content="hi", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        data = journal.get_completion_data()
        assert data["total_tokens"] == 15
        assert data["total_input_tokens"] == 10
        assert data["total_output_tokens"] == 5
        assert data["llm_call_count"] == 1
        assert data["lead_agent_tokens"] == 15
        assert data["subagent_tokens"] == 0

    async def test_subagent_bucket(self, journal):
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["subagent:gp"])
        data = journal.get_completion_data()
        assert data["subagent_tokens"] == 5
        assert data["lead_agent_tokens"] == 0

    async def test_middleware_bucket(self, journal):
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["middleware:title"])
        data = journal.get_completion_data()
        assert data["middleware_tokens"] == 5

    async def test_dedup_same_run_id(self, journal):
        """同一 run_id 多次 on_llm_end → token 只计一次。"""
        rid = uuid4()
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        journal.on_llm_end(_llm_response(msg), run_id=rid, tags=["lead_agent"])
        journal.on_llm_end(_llm_response(msg), run_id=rid, tags=["lead_agent"])  # 重复
        data = journal.get_completion_data()
        assert data["total_tokens"] == 15  # 没翻倍
        assert data["llm_call_count"] == 1

    async def test_total_computed_from_input_output_when_missing(self, journal):
        # total_tokens=0 → journal 用 input+output 补算（langchain 要求 total_tokens 字段在，故传 0）
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 0})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["total_tokens"] == 10

    async def test_track_tokens_disabled(self, store):
        j = RunJournal("r", "t", store, track_token_usage=False)
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        j.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        data = j.get_completion_data()
        assert data["total_tokens"] == 0  # 不追踪

    async def test_multiple_runs_accumulate(self, journal):
        for _ in range(3):
            msg = AIMessage(content="x", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        data = journal.get_completion_data()
        assert data["total_tokens"] == 45
        assert data["llm_call_count"] == 3


# ---------------------------------------------------------------------------
# sync → async flush
# ---------------------------------------------------------------------------


class TestSyncAsyncFlush:
    async def test_flush_threshold_triggers_put_batch(self):
        store = MemoryRunEventStore()
        j = RunJournal("r", "t", store, flush_threshold=2)
        j._put(event_type="e", category="trace", content="a")
        j._put(event_type="e", category="trace", content="b")  # 达阈值 → _flush_sync 调度 task
        # buffer 已清空（copy 给 task）
        assert j._buffer == []
        # 等 task 跑完
        await asyncio.sleep(0.02)
        events = await store.list_events("t", "r")
        assert len(events) == 2

    async def test_put_below_threshold_keeps_buffer(self):
        """未达 flush_threshold 的事件留在 buffer，不触发 flush。"""
        store = MemoryRunEventStore()
        j = RunJournal("r", "t", store, flush_threshold=5)
        j._put(event_type="e", category="trace", content="a")
        assert len(j._buffer) == 1  # 未达阈值，留 buffer
        await asyncio.sleep(0.01)
        # 仍未写入 store（没 flush）
        assert await store.list_events("t", "r") == []

    async def test_flush_drains_remaining_buffer(self, journal, store):
        """flush() 把残留 buffer 写到 store。"""
        journal._put(event_type="e", category="trace", content="a")
        journal._put(event_type="e", category="trace", content="b")
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert len(events) == 2


# ---------------------------------------------------------------------------
# 失败 batch 回插（红线 #8）
# ---------------------------------------------------------------------------


class TestFailedBatchReinsert:
    async def test_failed_batch_returned_to_buffer(self):
        store = _FlakyStore()
        j = RunJournal("r", "t", store, flush_threshold=2)
        j._put(event_type="e", category="trace", content="a")
        j._put(event_type="e", category="trace", content="b")  # 触发 flush → 第一次失败
        await asyncio.sleep(0.02)  # 等失败的 task 跑完
        assert store.calls == 1
        # 失败 batch 回插 buffer
        assert len(j._buffer) == 2
        # 再 flush → 第二次成功
        await j.flush()
        assert store.calls == 2
        events = await store._inner.list_events("t", "r")
        assert len(events) == 2  # 事件没丢


# ---------------------------------------------------------------------------
# error fallback
# ---------------------------------------------------------------------------


class TestErrorFallback:
    async def test_error_fallback_detected(self, journal):
        msg = AIMessage(
            content="抱歉，出错了",
            additional_kwargs={"deerflow_error_fallback": True, "error_detail": "rate limited"},
        )
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.had_llm_error_fallback is True
        assert journal.llm_error_fallback_message == "rate limited"

    async def test_error_fallback_falls_back_to_reason_then_text(self, journal):
        msg = AIMessage(content="兜底文本", additional_kwargs={"deerflow_error_fallback": True})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.had_llm_error_fallback is True
        assert journal.llm_error_fallback_message == "兜底文本"

    async def test_no_fallback_when_not_flagged(self, journal):
        msg = AIMessage(content="正常回复")
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.had_llm_error_fallback is False
        assert journal.llm_error_fallback_message is None


# ---------------------------------------------------------------------------
# record_middleware
# ---------------------------------------------------------------------------


class TestRecordMiddleware:
    async def test_middleware_event_buffered_and_flushed(self, journal, store):
        journal.record_middleware(
            "title",
            name="TitleMiddleware",
            hook="after_model",
            action="generate_title",
            changes={"title": "新标题"},
        )
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "middleware:title"
        assert ev["category"] == "middleware"
        assert ev["content"]["action"] == "generate_title"
        assert ev["content"]["changes"] == {"title": "新标题"}


# ---------------------------------------------------------------------------
# 首条 human 抽取
# ---------------------------------------------------------------------------


class TestFirstHumanExtraction:
    def test_on_chat_model_start_extracts_first_human(self, journal):
        msgs = [[HumanMessage(content="你好，世界")]]
        journal.on_chat_model_start({}, msgs, run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["first_human_message"] == "你好，世界"

    def test_first_human_only_set_once(self, journal):
        journal.on_chat_model_start({}, [[HumanMessage(content="第一条")]], run_id=uuid4(), tags=["lead_agent"])
        journal.on_chat_model_start({}, [[HumanMessage(content="第二条")]], run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["first_human_message"] == "第一条"

    def test_summary_human_ignored(self, journal):
        """name='summary' 的 HumanMessage（摘要注入）不当首条 human。"""
        journal.on_chat_model_start({}, [[HumanMessage(content="摘要内容", name="summary")]], run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["first_human_message"] is None

    def test_set_first_human_message_truncates(self, journal):
        journal.set_first_human_message("x" * 3000)
        assert len(journal.get_completion_data()["first_human_message"]) == 2000


# ---------------------------------------------------------------------------
# record_external_llm_usage_records
# ---------------------------------------------------------------------------


class TestExternalLLMUsage:
    def test_records_accumulate(self, journal):
        journal.record_external_llm_usage_records(
            [
                {"source_run_id": "ext-1", "caller": "subagent:gp", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                {"source_run_id": "ext-2", "caller": "middleware:x", "total_tokens": 8},
            ]
        )
        data = journal.get_completion_data()
        assert data["total_tokens"] == 23
        assert data["subagent_tokens"] == 15
        assert data["middleware_tokens"] == 8
        # input/output 累加正确（ext-2 无 input/output → 记 0，不污染）
        assert data["total_input_tokens"] == 10
        assert data["total_output_tokens"] == 5

    def test_external_dedup(self, journal):
        journal.record_external_llm_usage_records(
            [
                {"source_run_id": "ext-1", "caller": "lead_agent", "total_tokens": 15},
            ]
        )
        journal.record_external_llm_usage_records(
            [
                {"source_run_id": "ext-1", "caller": "lead_agent", "total_tokens": 15},  # 重复
            ]
        )
        assert journal.get_completion_data()["total_tokens"] == 15  # 没翻倍

    def test_external_computes_total_when_missing(self, journal):
        journal.record_external_llm_usage_records(
            [
                {"source_run_id": "ext-1", "caller": "lead_agent", "input_tokens": 4, "output_tokens": 6},
            ]
        )
        assert journal.get_completion_data()["total_tokens"] == 10

    def test_external_skips_zero(self, journal):
        journal.record_external_llm_usage_records(
            [
                {"source_run_id": "ext-1", "caller": "lead_agent", "total_tokens": 0},
            ]
        )
        assert journal.get_completion_data()["total_tokens"] == 0


# ---------------------------------------------------------------------------
# last_ai_message 只被 lead_agent 非空 AI 更新
# ---------------------------------------------------------------------------


class TestLastAIMessage:
    async def test_lead_ai_updates_last(self, journal):
        msg = AIMessage(content="最终答案", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["last_ai_message"] == "最终答案"

    async def test_subagent_ai_does_not_overwrite(self, journal):
        # 先 lead agent 设一条
        lead = AIMessage(content="lead 回答", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(lead), run_id=uuid4(), tags=["lead_agent"])
        # 子代理的 AI 消息不应覆盖
        sub = AIMessage(content="subagent 回答", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(sub), run_id=uuid4(), tags=["subagent:gp"])
        assert journal.get_completion_data()["last_ai_message"] == "lead 回答"

    async def test_empty_ai_text_not_recorded(self, journal):
        msg = AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["last_ai_message"] is None


# ---------------------------------------------------------------------------
# 生命周期回调（chain / tool）
# ---------------------------------------------------------------------------


class TestLifecycleCallbacks:
    async def test_chain_start_root_emits_run_start(self, journal, store):
        journal.on_chain_start({"name": "LeadAgent"}, {}, run_id=uuid4(), parent_run_id=None, tags=["lead_agent"])
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert any(e["event_type"] == "run.start" and e["content"]["chain"] == "LeadAgent" for e in events)

    async def test_chain_start_nested_ignored(self, journal, store):
        journal.on_chain_start({"name": "inner"}, {}, run_id=uuid4(), parent_run_id=uuid4(), tags=["lead_agent"])
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert events == []  # 嵌套 chain 不发事件

    async def test_chain_end_root_emits_run_end(self, journal, store):
        journal.on_chain_end({"result": "done"}, run_id=uuid4(), parent_run_id=None)
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert any(e["event_type"] == "run.end" for e in events)

    async def test_chain_error_emits_run_error(self, journal, store):
        journal.on_chain_error(ValueError("boom"), run_id=uuid4())
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        err = [e for e in events if e["event_type"] == "run.error"]
        assert len(err) == 1
        assert err[0]["content"] == "boom"
        assert err[0]["metadata"]["error_type"] == "ValueError"

    async def test_tool_end_tool_message(self, journal, store):
        journal.on_tool_end(ToolMessage(content="结果", tool_call_id="tc1"), run_id=uuid4())
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        assert any(e["event_type"] == "llm.tool.result" for e in events)

    async def test_message_count_increments(self, journal):
        msg = AIMessage(content="hi", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["message_count"] == 1


# ---------------------------------------------------------------------------
# on_llm_error / latency / Command tool end / 多 batch 首条 human（补漏分支）
# ---------------------------------------------------------------------------


class TestAdditionalCallbacks:
    async def test_llm_error_emits_event_and_clears_start(self, journal, store):
        """on_llm_error 发 llm.error trace 并清理 _llm_start_times。"""
        rid = uuid4()
        journal._llm_start_times[str(rid)] = 123.0
        journal.on_llm_error(ValueError("boom"), run_id=rid)
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        err = [e for e in events if e["event_type"] == "llm.error"]
        assert len(err) == 1
        assert err[0]["content"] == "boom"
        assert str(rid) not in journal._llm_start_times

    async def test_latency_ms_tracked_on_llm_end(self, journal):
        """on_chat_model_start → on_llm_end 之间记 latency_ms（非负 int）。"""
        rid = uuid4()
        journal.on_chat_model_start({}, [[HumanMessage(content="hi")]], run_id=rid, tags=["lead_agent"])
        msg = AIMessage(content="yo", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(msg), run_id=rid, tags=["lead_agent"])
        # journal fixture flush_threshold=50，llm.ai.response 尚未刷盘，仍在 buffer 里
        resp = [e for e in journal._buffer if e["event_type"] == "llm.ai.response"]
        assert resp
        assert isinstance(resp[0]["metadata"]["latency_ms"], int)
        assert resp[0]["metadata"]["latency_ms"] >= 0

    async def test_latency_none_without_start(self, journal):
        """没先 on_chat_model_start / on_llm_start → latency_ms=None。"""
        msg = AIMessage(content="yo", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        journal.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        resp = [e for e in journal._buffer if e["event_type"] == "llm.ai.response"]
        assert resp
        assert resp[0]["metadata"]["latency_ms"] is None

    async def test_tool_end_command_messages(self, journal, store):
        """on_tool_end 收 Command.update.messages → 逐条发 llm.tool.result。"""
        tm = ToolMessage(content="file list", tool_call_id="tc1")
        cmd = Command(update={"messages": [tm]})
        journal.on_tool_end(cmd, run_id=uuid4())
        await journal.flush()
        events = await store.list_events("thread-1", "run-1")
        tool_evs = [e for e in events if e["event_type"] == "llm.tool.result"]
        assert len(tool_evs) == 1
        assert tool_evs[0]["content"]["content"] == "file list"

    def test_first_human_from_multi_batch(self, journal):
        """多 batch prompt：反向扫到含 HumanMessage 的 batch，抽其 human。"""
        msgs = [
            [SystemMessage(content="sys"), AIMessage(content="prev")],
            [HumanMessage(content="real question")],
        ]
        journal.on_chat_model_start({}, msgs, run_id=uuid4(), tags=["lead_agent"])
        assert journal.get_completion_data()["first_human_message"] == "real question"


# ---------------------------------------------------------------------------
# progress 节流（progress_reporter 注入；红线：节流 + delayed + flush 取消）
# ---------------------------------------------------------------------------


class TestProgressThrottle:
    async def test_snapshot_reported_on_llm_end(self, store):
        """on_llm_end 触发一次进度快照上报，snapshot 含 token。"""
        reporter = AsyncMock()
        j = RunJournal("r", "t", store, progress_reporter=reporter, progress_flush_interval=0.0)
        msg = AIMessage(content="hi", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        j.on_llm_end(_llm_response(msg), run_id=uuid4(), tags=["lead_agent"])
        if j._pending_progress_task is not None:
            await asyncio.gather(j._pending_progress_task, return_exceptions=True)
        reporter.assert_awaited_once()
        snapshot = reporter.await_args.args[0]
        assert snapshot["total_tokens"] == 15

    async def test_throttled_within_interval_reports_once(self, store):
        """interval 内第二次 on_llm_end 标 dirty + delayed，不立即重复上报。"""
        reporter = AsyncMock()
        j = RunJournal("r", "t", store, progress_reporter=reporter, progress_flush_interval=100.0)
        # 首次：_last_progress_flush=0 → elapsed 巨大 → 立即上报
        msg1 = AIMessage(content="a", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        j.on_llm_end(_llm_response(msg1), run_id=uuid4(), tags=["lead_agent"])
        if j._pending_progress_task is not None:
            await asyncio.gather(j._pending_progress_task, return_exceptions=True)
        assert reporter.await_count == 1
        # 第二次在 interval 内 → dirty + delayed（sleep 100，不立即上报）
        msg2 = AIMessage(content="b", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        j.on_llm_end(_llm_response(msg2), run_id=uuid4(), tags=["lead_agent"])
        assert reporter.await_count == 1
        await j.flush()  # 清理 delayed task 防泄漏

    async def test_flush_cancels_delayed_without_extra_report(self, store):
        """flush 取消 delayed 进度 task，不再额外上报。"""
        reporter = AsyncMock()
        j = RunJournal("r", "t", store, progress_reporter=reporter, progress_flush_interval=100.0)
        msg1 = AIMessage(content="a", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        j.on_llm_end(_llm_response(msg1), run_id=uuid4(), tags=["lead_agent"])
        if j._pending_progress_task is not None:
            await asyncio.gather(j._pending_progress_task, return_exceptions=True)
        # 第二次触发 delayed
        msg2 = AIMessage(content="b", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        j.on_llm_end(_llm_response(msg2), run_id=uuid4(), tags=["lead_agent"])
        await j.flush()  # 取消 delayed，不额外上报
        assert reporter.await_count == 1
