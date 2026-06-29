"""M8 stream_bridge 的 hermetic 测试。

覆盖（对齐 ALIGNMENT_OUTLINE M8 测试要求）：
- 有界 evict（红线 #11：queue_maxsize + 淘汰最旧 + start_offset 前移）。
- Last-Event-ID 重连：subscribe(last_event_id) 从该事件之后续播。
- 过期 last_event_id（不在缓冲）：从最早保留事件回放 + 警告。
- 落后恢复：订阅者 offset 落后于 start_offset 时从 start_offset 恢复（部分事件丢失）。
- 心跳：heartbeat_interval 内无事件 → HEARTBEAT_SENTINEL（防代理掐断）。
- END 终止：publish_end → 订阅者收到 END_SENTINEL 后停止。
- cleanup / close、id 格式 {ts_ms}-{seq} 单调、迟到订阅者回放+续接、make_stream_bridge 工厂。

hermetic 约定：纯 asyncio，无 IO/无网络/无 fixture。
"""

from __future__ import annotations

import asyncio

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.stream_bridge_config import StreamBridgeConfig
from deerflow.runtime.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    MemoryStreamBridge,
    StreamBridge,
    StreamEvent,
    make_stream_bridge,
)

# ---------------------------------------------------------------------------
# 数据结构 + ABC
# ---------------------------------------------------------------------------


def test_stream_event_dataclass():
    ev = StreamEvent(id="1", event="updates", data={"x": 1})
    assert ev.id == "1"
    assert ev.event == "updates"
    assert ev.data == {"x": 1}
    # frozen
    with pytest.raises(Exception):
        ev.id = "2"  # type: ignore[misc]


def test_sentinels():
    assert END_SENTINEL.event == "__end__" and END_SENTINEL.id == "" and END_SENTINEL.data is None
    assert HEARTBEAT_SENTINEL.event == "__heartbeat__" and HEARTBEAT_SENTINEL.id == ""


def test_stream_bridge_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        StreamBridge()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 基础 publish/subscribe
# ---------------------------------------------------------------------------


class TestBasic:
    async def test_publish_subscribe_order_and_end(self):
        bridge = MemoryStreamBridge()
        for i in range(3):
            await bridge.publish("r", "updates", {"i": i})
        await bridge.publish_end("r")
        out = [e async for e in bridge.subscribe("r")]
        assert [e.data for e in out[:-1]] == [{"i": 0}, {"i": 1}, {"i": 2}]
        assert out[-1] is END_SENTINEL

    async def test_id_format_ts_ms_seq_monotonic(self):
        bridge = MemoryStreamBridge()
        for i in range(3):
            await bridge.publish("r", "e", str(i))
        ids = [e.id for e in bridge._streams["r"].events]
        # {ts_ms}-{seq}，seq 从 0 单调
        for seq, eid in enumerate(ids):
            assert eid.endswith(f"-{seq}"), eid
        # ts 部分非空
        assert all(eid.split("-")[0].isdigit() for eid in ids)


# ---------------------------------------------------------------------------
# 有界窗口 + eviction（红线 #11）
# ---------------------------------------------------------------------------


class TestBoundedEviction:
    async def test_evicts_oldest_beyond_maxsize(self):
        bridge = MemoryStreamBridge(queue_maxsize=3)
        for i in range(5):  # 5 > 3
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        rs = bridge._streams["r"]
        assert len(rs.events) == 3  # 只留 3 条
        assert rs.start_offset == 2  # 淘汰了 2 条最旧的
        # 订阅只看到 e2,e3,e4
        out = [e.data for e in [e async for e in bridge.subscribe("r")] if e is not END_SENTINEL]
        assert out == ["2", "3", "4"]

    async def test_no_evict_under_maxsize(self):
        bridge = MemoryStreamBridge(queue_maxsize=10)
        for i in range(3):
            await bridge.publish("r", "e", str(i))
        assert bridge._streams["r"].start_offset == 0
        assert len(bridge._streams["r"].events) == 3


# ---------------------------------------------------------------------------
# Last-Event-ID 重连
# ---------------------------------------------------------------------------


class TestLastEventIdReconnect:
    async def test_resume_after_last_event_id(self):
        bridge = MemoryStreamBridge(queue_maxsize=10)
        for i in range(3):
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        e0_id = bridge._streams["r"].events[0].id
        # 从 e0 之后续播 → e1, e2
        out = [e async for e in bridge.subscribe("r", last_event_id=e0_id)]
        assert [e.data for e in out[:-1]] == ["1", "2"]
        assert out[-1] is END_SENTINEL

    async def test_resume_from_middle_event(self):
        bridge = MemoryStreamBridge(queue_maxsize=10)
        for i in range(4):
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        e1_id = bridge._streams["r"].events[1].id
        out = [e.data for e in [e async for e in bridge.subscribe("r", last_event_id=e1_id)] if e is not END_SENTINEL]
        assert out == ["2", "3"]

    async def test_stale_last_event_id_replays_earliest(self):
        """last_event_id 不在缓冲（已被淘汰或不存在）→ 从最早保留事件回放 + 警告。"""
        bridge = MemoryStreamBridge(queue_maxsize=2)
        for i in range(4):  # buffer=[e2,e3], start_offset=2
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        out = [e.data for e in [e async for e in bridge.subscribe("r", last_event_id="nonexistent-id")] if e is not END_SENTINEL]
        # 找不到 → 从 start_offset 回放 → e2, e3
        assert out == ["2", "3"]


class TestResumeOffsetO1:
    """#3700：subscribe(last_event_id) 用事件 id 内嵌的 seq 算术 O(1) 定位，而非线性扫缓冲。

    事件 id 形如 ``{ts_ms}-{seq}``，``seq`` 是 per-run 单调序号且 == 该事件的绝对 offset。
    算出 index 后仍核验该处 id，不符则回退（行为与旧线性扫一致）。
    """

    def test_parse_event_seq_extracts_offset(self):
        assert MemoryStreamBridge._parse_event_seq("1700000000000-0") == 0
        assert MemoryStreamBridge._parse_event_seq("1700000000000-5") == 5
        assert MemoryStreamBridge._parse_event_seq("1700000000000-12") == 12

    def test_parse_event_seq_returns_none_for_malformed(self):
        # 无 ``-`` 分隔
        assert MemoryStreamBridge._parse_event_seq("nohyphen") is None
        # seq 段非整数
        assert MemoryStreamBridge._parse_event_seq("1700000000000-abc") is None

    async def test_resume_uses_embedded_seq_arithmetic(self):
        """有效 last_event_id 经算术定位 → 从该事件之后续播（O(1)，不扫缓冲）。"""
        bridge = MemoryStreamBridge(queue_maxsize=10)
        for i in range(5):
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        # e2 的 id 内嵌 seq=2；算术 local_index = 2 - start_offset(0) = 2
        e2_id = bridge._streams["r"].events[2].id
        assert MemoryStreamBridge._parse_event_seq(e2_id) == 2
        out = [e.data for e in [e async for e in bridge.subscribe("r", last_event_id=e2_id)] if e is not END_SENTINEL]
        assert out == ["3", "4"]

    async def test_foreign_id_with_plausible_seq_falls_back(self):
        """id 能解析出 seq，但算出 index 处的事件 id 不匹配（外来/猜测 id）→ 回退到最早。

        证明 O(1) 算术不被盲信：算出 index 后仍核验该处 id，不符则回退（与旧线性扫行为一致）。
        """
        bridge = MemoryStreamBridge(queue_maxsize=10)
        for i in range(4):
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        # 真实 e1 的 ts 未知，构造一个 seq=1 但 ts 完全不同的外来 id
        foreign_id = "0000000000000-1"
        assert MemoryStreamBridge._parse_event_seq(foreign_id) == 1  # 能解析
        # 算出 local_index=1，但 events[1].id != foreign_id → 回退 → 全量 e0..e3
        out = [e.data for e in [e async for e in bridge.subscribe("r", last_event_id=foreign_id)] if e is not END_SENTINEL]
        assert out == ["0", "1", "2", "3"]


# ---------------------------------------------------------------------------
# 落后恢复
# ---------------------------------------------------------------------------


class TestFellBehindRecovery:
    async def test_subscriber_fell_behind_resumes_from_start_offset(self):
        """订阅者 offset 落后于 start_offset（因 eviction 推进）→ 从 start_offset 恢复，丢失的事件不补。"""
        bridge = MemoryStreamBridge(queue_maxsize=4)
        # 先放 e0, e1
        await bridge.publish("r", "e", "0")
        await bridge.publish("r", "e", "1")
        e0_id = bridge._streams["r"].events[0].id
        # 订阅者从 e0 之后续播 → next_offset=1
        gen = bridge.subscribe("r", last_event_id=e0_id, heartbeat_interval=10.0)
        first = await gen.__anext__()
        assert first.data == "1"  # e1（next_offset 现为 2）
        # 洪水发布，把订阅者的 next_offset 甩在 start_offset 之后
        for i in range(2, 7):  # e2..e6 → 最终 start_offset=3, buffer=[e3,e4,e5,e6]
            await bridge.publish("r", "e", str(i))
        await bridge.publish_end("r")
        rest = [e async for e in gen]
        # 落后恢复：从 start_offset(3) 起 → e3,e4,e5,e6（e2 丢了）
        data = [e.data for e in rest if e is not END_SENTINEL]
        assert data == ["3", "4", "5", "6"]
        assert rest[-1] is END_SENTINEL


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------


class TestHeartbeat:
    async def test_heartbeat_when_idle(self):
        """无事件且未 end：heartbeat_interval 后收到 HEARTBEAT_SENTINEL。"""
        bridge = MemoryStreamBridge()
        gen = bridge.subscribe("r", heartbeat_interval=0.05)
        ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert ev is HEARTBEAT_SENTINEL
        await gen.aclose()

    async def test_heartbeat_does_not_block_real_events(self):
        """有事件时不发心跳，先发事件。"""
        bridge = MemoryStreamBridge()
        await bridge.publish("r", "e", "a")
        await bridge.publish_end("r")
        out = [e async for e in bridge.subscribe("r", heartbeat_interval=0.05)]
        # 事件立即投递，没有心跳混入
        assert all(e is not HEARTBEAT_SENTINEL for e in out)


# ---------------------------------------------------------------------------
# 迟到订阅者 + 实时续接
# ---------------------------------------------------------------------------


class TestLiveStreaming:
    async def test_late_subscriber_replays_then_live(self):
        """迟到订阅者：先拿缓冲里的事件，再续接实时事件。"""
        bridge = MemoryStreamBridge()
        await bridge.publish("r", "e", "a")  # 订阅前已缓冲

        async def consumer():
            return [e async for e in bridge.subscribe("r")]

        async def producer():
            await asyncio.sleep(0.02)
            await bridge.publish("r", "e", "b")
            await bridge.publish_end("r")

        consumer_task = asyncio.create_task(consumer())
        await producer()
        out = await asyncio.wait_for(consumer_task, timeout=1.0)
        assert [e.data for e in out if e is not END_SENTINEL] == ["a", "b"]


# ---------------------------------------------------------------------------
# cleanup / close
# ---------------------------------------------------------------------------


class TestCleanupClose:
    async def test_cleanup_removes_run(self):
        bridge = MemoryStreamBridge()
        await bridge.publish("r", "e", "a")
        assert "r" in bridge._streams
        await bridge.cleanup("r")
        assert "r" not in bridge._streams
        assert "r" not in bridge._counters

    async def test_cleanup_with_delay(self):
        bridge = MemoryStreamBridge()
        await bridge.publish("r", "e", "a")
        import time

        t0 = time.monotonic()
        await bridge.cleanup("r", delay=0.05)
        assert time.monotonic() - t0 >= 0.04
        assert "r" not in bridge._streams

    async def test_close_clears_all(self):
        bridge = MemoryStreamBridge()
        await bridge.publish("r1", "e", "a")
        await bridge.publish("r2", "e", "b")
        await bridge.close()
        assert bridge._streams == {}
        assert bridge._counters == {}


# ---------------------------------------------------------------------------
# make_stream_bridge 工厂
# ---------------------------------------------------------------------------


class TestMakeStreamBridge:
    async def test_default_memory(self):
        async with make_stream_bridge() as b:
            assert isinstance(b, MemoryStreamBridge)
            assert b._maxsize == 256  # 默认

    async def test_with_explicit_config(self):
        cfg = AppConfig(stream_bridge=StreamBridgeConfig(type="memory", queue_maxsize=64))
        async with make_stream_bridge(cfg) as b:
            assert isinstance(b, MemoryStreamBridge)
            assert b._maxsize == 64

    async def test_close_called_on_exit(self):
        """async with 退出时 bridge.close() 被调。"""
        async with make_stream_bridge() as b:
            await b.publish("r", "e", "a")
            assert b._streams  # 非空
        # 退出后已清空
        assert b._streams == {}

    async def test_redis_not_implemented(self):
        cfg = AppConfig(stream_bridge=StreamBridgeConfig(type="redis", redis_url="redis://localhost"))
        with pytest.raises(NotImplementedError):
            async with make_stream_bridge(cfg):
                pass

    async def test_unknown_type_raises(self):
        cfg = AppConfig(stream_bridge=StreamBridgeConfig.model_construct(type="kafka"))
        with pytest.raises(ValueError, match="Unknown stream bridge type"):
            async with make_stream_bridge(cfg):
                pass
