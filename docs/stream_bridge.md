# 11. stream_bridge.md — 流桥（生产者-消费者解耦 + 重连补播）

> 配套代码：[runtime/stream_bridge/](../backend/packages/harness/deerflow/runtime/stream_bridge/)
> 配套测试：[test/test_stream_bridge.py](../test/test_stream_bridge.py)
> 本文面向「刚接触 SSE / 流式 / 生产者-消费者的小白」。每个名词第一次出现都会解释。

---

## 1. 一句话定位

**StreamBridge 是夹在「后台跑 agent 的 worker」与「向前端推 SSE 的 HTTP 端点」之间的中转站——它把事件缓冲起来、支持重连补播、有界防爆内存。**

worker 是生产者，SSE 端点是消费者。两者不直连，而是通过流桥。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「worker 与 SSE 直连」会怎样：

- **前端一断，worker 就废**。一次深度研究 run 可能跑 5 分钟。如果 worker 直接把事件推给 SSE 连接，前端网络一抖断开，worker 要么跟着取消（5 分钟白跑），要么事件丢失（前端重连后看不到中间过程）。
- **多个客户端看不到同一次 run**。想在两个浏览器标签页看同一次 run 的进度，直连做不到（一个 worker 只能推给一个连接）。
- **内存爆炸**。如果无限制地给一个 run 缓冲所有事件，长 run 累积几万条事件，进程 OOM。
- **代理掐断空闲连接**。nginx / 云负载均衡器对「长时间无数据」的 HTTP 连接会静默关闭（默认 60 秒左右）。如果 run 在思考、暂时没事件，SSE 连接就被掐了，前端以为断了。

StreamBridge 解决这些：**解耦生产/消费**（worker 不关心有几个消费者）、**有界窗口 + 重连补播**（前端断了重连能续播，长 run 不爆内存）、**心跳**（防代理掐断）。

---

## 3. 核心概念（名词 + 类比）

### 3.1 SSE（Server-Sent Events）

**SSE** 是 HTTP 上的「服务器单向推流」协议：服务器不关连接，持续写一行行 `event:`/`data:` 文本，浏览器用 `EventSource` 接收。比 WebSocket 简单（单向、纯文本、自动重连），适合「AI 边想边吐字」这种场景。

### 3.2 生产者 / 消费者 / 解耦

- **生产者**（producer）：后台 worker，跑 agent，产生事件（AI 的每个 token、工具调用、状态更新）。
- **消费者**（consumer）：SSE 端点，把事件推给前端。
- **解耦**：生产者把事件投进流桥就返回（不阻塞、不等消费者）；消费者从流桥读。两者各自独立节奏。

类比：流桥是「快递柜」。worker（快递员）把包裹投进柜就走；前端（收件人）有空来取。柜子有限格（有界窗口），旧包裹超期被清。

### 3.3 StreamEvent / id / event / data

一条事件三字段：
- `id`：单调递增的事件 id（`{毫秒时间戳}-{序号}`，如 `1718600000000-3`）。用作 SSE 的 `id:` 字段。
- `event`：SSE 事件名（`metadata` / `updates` / `events` / `error` / `end`）。
- `data`：JSON 可序列化的 payload。

### 3.4 Last-Event-ID 重连

SSE 协议自带重连：连接断开后，浏览器自动重连，并在请求头带上 `Last-Event-ID`（最后收到的事件 id）。流桥据此**从该事件之后续播**——前端不会重复看已收到的事件，也不会漏掉断连期间的事件（只要它们还在缓冲窗口里）。

### 3.5 有界窗口 + eviction + start_offset

每个 run 的事件不是无限保留，而是**最多 `queue_maxsize` 条**（默认 256）。超出就**淘汰最旧的**（eviction）。用一个 `start_offset` 记录「当前缓冲里最早事件的全局序号」——逻辑上事件流是 `{0,1,2,...}` 无限的，但内存里只留一个滑动窗口 `[start_offset, start_offset+len)`。

这保证长 run 不爆内存（红线 #11）。

### 3.6 心跳（heartbeat）

消费者如果在 `heartbeat_interval` 秒内没收到任何事件，流桥发一个 `HEARTBEAT_SENTINEL`。SSE 端点把它转成一行注释（`: keepalive`）写给前端——这条「数据」维持连接活跃，让 nginx 等代理看到「连接还活着」，不掐断。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 为什么用 Condition 而非 Queue

每个 run 一个 `asyncio.Condition` + 一个 `list`（事件日志）。不用 `asyncio.Queue` 的原因：
- **Queue 是单消费者的**——一个 get 走了事件就没了，第二个消费者（重连的、多标签页的）看不到。需要事件日志（多消费者各读各的 offset）。
- **Condition 支持回放**：新订阅者先读 `events` 列表（从它的 offset 起），再 `wait()` 等新事件。生产者 `notify_all()` 唤醒所有等待者。

每个 run 的 `_RunStream`：`events`（列表）+ `condition`（条件变量）+ `ended`（是否已 end）+ `start_offset`（最早事件序号）。

### 4.2 有界窗口的 eviction 语义（红线 #11）

`publish` 后若 `len(events) > maxsize`：删掉最前面的 `overflow` 条，`start_offset += overflow`。

效果：内存恒定（每 run 最多 maxsize 条）。代价：**被淘汰的事件，重连时补不回来**——如果客户端断开太久，期间产生的事件超过窗口，它会「落后」。

### 4.3 落后恢复（fell-behind）

订阅者的 `next_offset`（下次要读的全局序号）可能落到 `start_offset` 之前——因为它断连太久，窗口已经滑过去了。这时：
- 不能从 `next_offset` 读（那些事件已被淘汰、不在 `events` 里了）。
- **从 `start_offset` 恢复**（最早还保留的事件），打一条警告日志。
- **丢失的事件不补**——这是有界窗口的固有代价。客户端能感知（seq 跳跃）并决定是否要全量重取。

测试 `test_subscriber_fell_behind_resumes_from_start_offset` 锁住这个：订阅者读完一条后，洪水发布把窗口推过它的 offset，验证它从新 start_offset 恢复、丢失的不补。

### 4.4 Last-Event-ID 解析（`_resolve_start_offset`）

`subscribe(last_event_id)` 时：
- 在 `events` 里找 `last_event_id`，找到 → 从它的**下一条**起（`start_offset + index + 1`）。不重复。
- 找不到（被淘汰 / 不存在）→ 从 `start_offset`（最早保留）起，打警告。这是「尽力补播」——保不了精确，至少不丢当前窗口的。

### 4.5 END 哨兵 + 心跳哨兵

- `END_SENTINEL`：`publish_end` 后，订阅者读完缓冲里的事件，收到一次 END，然后迭代器**正常结束**（`return`）。这是「run 跑完了，不会再有事件」的信号，前端据此关 SSE。
- `HEARTBEAT_SENTINEL`：等待超时时发出，迭代器**不结束**（继续循环）。前端忽略它（或转成 SSE 注释保活）。

两者用「哨兵对象」而非特殊 data 值——消费者用 `is END_SENTINEL` 判等，不会被用户 data 误触。

### 4.6 id 为何用 `{ts_ms}-{seq}`

纯序号（`0,1,2`）就够了，为何加时间戳？
- **跨重启的弱唯一性**：进程重启后 seq 从 0 重来，但旧的事件 id 若还在前端的 `Last-Event-ID` 里，`0` 可能撞上新一轮的 `0`。加毫秒时间戳让不同时段的 id 几乎不撞。
- **可读 / 可调试**：看 id 就知道大概何时产生。
- `seq` 部分保证同毫秒内的单调（计数器递增）。

### 4.7 cleanup 与 close

- `cleanup(run_id, delay=...)`：run 结束后释放该 run 的流。`delay > 0` 时先等——给迟到的订阅者（刚重连的）一个排空剩余事件的机会。
- `close()`：关整个 bridge（进程退出时），清空所有 run。

### 4.8 为什么是 ABC + memory 实现

`StreamBridge` 是抽象基类，`MemoryStreamBridge` 是单进程内存实现。留 ABC 是为了未来加 **redis** 实现（跨进程 / 多节点共享事件流，`redis_url`）。redis 当前 `NotImplementedError`（规划中）。memory 实现只能单进程——多进程部署要 redis。

---

## 5. 文件结构

```
runtime/stream_bridge/
├── __init__.py          # 导出 make_stream_bridge + StreamBridge/MemoryStreamBridge/StreamEvent/哨兵
├── base.py              # StreamEvent（frozen dataclass）+ HEARTBEAT/END 哨兵 + StreamBridge ABC
├── memory.py            # MemoryStreamBridge（每 run _RunStream + Condition + 有界窗口 + Last-Event-ID + 心跳）
└── async_provider.py    # make_stream_bridge(app_config) async cm（按 config 选 memory/redis）
```

依赖：[config/stream_bridge_config.py](../backend/packages/harness/deerflow/config/stream_bridge_config.py)（`type`/`redis_url`/`queue_maxsize`）、[config/app_config.py](../backend/packages/harness/deerflow/config/app_config.py)（`stream_bridge` 段）。**无** LangChain / 持久化依赖。

---

## 6. 关键接口 / 签名

### StreamBridge ABC

```python
async def publish(run_id, event, data) -> None            # 生产者：入队一条
async def publish_end(run_id) -> None                     # 生产者：示意不再有事件
def subscribe(run_id, *, last_event_id=None, heartbeat_interval=15.0) -> AsyncIterator[StreamEvent]
                                                          # 消费者：yield 事件，超时发心跳，end 后收 END 再停
async def cleanup(run_id, *, delay=0) -> None             # 释放某 run
async def close() -> None                                 # 释放全部（默认 no-op）
```

### 工厂

```python
@asynccontextmanager
async def make_stream_bridge(app_config=None) -> AsyncIterator[StreamBridge]
#   None/memory → MemoryStreamBridge(queue_maxsize)；redis → NotImplementedError
```

### 哨兵

```python
HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)  # 等待超时
END_SENTINEL       = StreamEvent(id="", event="__end__", data=None)        # run 结束
```

---

## 7. 应用方法（可跑 demo）

### 7.1 生产者-消费者基本流

```python
import asyncio
from deerflow.runtime.stream_bridge import MemoryStreamBridge, END_SENTINEL

bridge = MemoryStreamBridge(queue_maxsize=256)

async def producer():
    for i in range(3):
        await bridge.publish("run-1", "updates", {"i": i})
    await bridge.publish_end("run-1")

async def consumer():
    async for ev in bridge.subscribe("run-1"):
        if ev is END_SENTINEL:
            break
        print(ev.id, ev.event, ev.data)

await asyncio.gather(producer(), consumer())
```

### 7.2 重连补播

```python
# 前端断开时最后收到的事件 id 是 last_id
async for ev in bridge.subscribe("run-1", last_event_id=last_id):
    ...  # 从 last_id 之后续播，不重复
```

### 7.3 lifespan 装配（Phase 8 集成时）

```python
from deerflow.runtime.stream_bridge import make_stream_bridge

async with make_stream_bridge() as bridge:
    app.state.stream_bridge = bridge
    yield
    # 退出时自动 close
```

---

## 8. 与其它模块的关系（文字依赖图）

```
config/stream_bridge_config ──→ stream_bridge/async_provider.make_stream_bridge
                                         │
                                         ▼
                              stream_bridge/{base, memory}
                                         │
            （未来）runs/worker（生产者）──publish──→ bridge ──subscribe──→ （未来）Gateway SSE 端点（消费者）
                                         │
                               心跳 → SSE : keepalive（防代理掐断）
                               END  → 前端关 EventSource
                               Last-Event-ID → 重连补播
```

- **被谁依赖**：未来的 runs worker（生产者）、SSE / stream 端点（消费者）、lifespan 装配。
- **依赖谁**：config（StreamBridgeConfig / AppConfig）。无业务模块依赖。
- **与 RunEventStore 的区别**：RunEventStore 是「持久化的事件流」（落盘、可查、跨重启）；stream_bridge 是「实时传输的事件流」（内存、瞬时、进程内）。前者给「历史回放 / 审计」，后者给「实时推送」。worker 会**同时**往两者写：bridge 给在线前端推，event store 落盘给离线查。

---

## 9. 常见问题 / 排错

**Q: 前端重连后丢了一些事件？**
A: 断连太久，事件超过了 `queue_maxsize`（默认 256）窗口被淘汰。调大 `stream_bridge.queue_maxsize`，或接受「落后时从最早保留事件续」（部分丢失）。要完全不丢，得用持久化的 RunEventStore + 离线补取。

**Q: SSE 连接老是被 nginx 关？**
A: 调小 `heartbeat_interval`（默认 15 秒）。心跳让连接保持活跃，绕过代理的空闲超时。确认 SSE 端点把 `HEARTBEAT_SENTINEL` 转成了一行 SSE 注释（`: keepalive\n\n`）。

**Q: `subscribe` 的迭代器一直不停？**
A: 没收到 `END_SENTINEL`。确认生产者跑完调了 `publish_end(run_id)`。否则消费者会一直等（发心跳但不结束）。

**Q: 多个 worker 进程，前端看不到别进程的事件？**
A: `MemoryStreamBridge` 是单进程的。多进程 / 多节点要用 redis 实现（当前 `NotImplementedError`）。

**Q: 事件 id 重复了？**
A: 正常不会——`{ts_ms}-{seq}` 里 seq 每 run 单调递增。若跨进程，两进程的 seq 各自从 0 起，id 的 ts 部分大概率不同；要绝对唯一得上 redis（全局 seq）。

**Q: `cleanup` 的 delay 有什么用？**
A: run 刚结束时，可能有客户端正在重连 / 刚订阅。`delay` 让事件多留一会儿，给它们排空的机会，再释放。worker 退出时常传一个小的 delay。

---

> 红线索引：#11（bridge 有界回放：queue_maxsize=256 + start_offset + eviction）。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
