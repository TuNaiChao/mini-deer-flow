# 11. stream_bridge.md — 流桥（SSE 实时推送 · 生产者-消费者解耦 + 重连补播）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d

> **一句话定位**：`StreamBridge` 是夹在「后台跑 agent 的 worker」与「向前端推 SSE 的 HTTP 端点」之间的中转站——它把事件缓冲起来、支持断线重连续播、有界窗口防爆内存、心跳防代理掐断。

> **配套代码**：[runtime/stream_bridge/](../backend/packages/harness/deerflow/runtime/stream_bridge/)（[base.py](../backend/packages/harness/deerflow/runtime/stream_bridge/base.py) · [memory.py](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py) · [async_provider.py](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py)）+ [config/stream_bridge_config.py](../backend/packages/harness/deerflow/config/stream_bridge_config.py)
> **配套测试**：[test/test_stream_bridge.py](../test/test_stream_bridge.py)
> 本文面向「刚接触 SSE / 流式 / 生产者-消费者的小白」。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么 worker 不能直接把事件推给 SSE 连接，非要在中间夹一个「流桥」？（解耦 / 多消费者 / 断线续播）
2. 为什么每个 run 用 `asyncio.Condition` + `list`（事件日志），而不是 `asyncio.Queue`？（Queue 是单消费的，事件日志才支持多消费者各读各的 offset + 回放）
3. 重连时，前端在请求头带的 `Last-Event-ID`，流桥怎么用它在 **O(1)** 时间内定位续播点，而不是扫整个缓冲？为什么定位完还要核验一次 id？
4. 有界窗口（`queue_maxsize=256`）会淘汰最旧事件——订阅者「落后」到窗口之前时怎么办？为什么丢失的事件不补？
5. 心跳（`HEARTBEAT_SENTINEL`）和结束（`END_SENTINEL`）都是哨兵对象，为什么用 `is` 判等而不是某个特殊 data 值？两者对迭代器的影响有什么不同？
6. 事件 id 为什么是 `{毫秒时间戳}-{seq}` 而不是纯序号？跨进程重启时这个格式能保证什么、不能保证什么？
7. `StreamBridge` 为什么设计成 ABC，目前只有内存实现，Redis 实现却直接 `NotImplementedError`？单进程内存桥在多 worker 部署下会出什么问题？

---

## §1 为什么需要它（痛点 / 故障场景）

先看「worker 与 SSE 直连」会怎样（四种故障）：

| 故障 | 直连的后果 | 流桥怎么解 |
|------|-----------|-----------|
| **前端一断，worker 就废** | 一次深度研究 run 可能跑 5 分钟。worker 直接把事件推给 SSE 连接，前端网络一抖断开，worker 要么跟着取消（5 分钟白跑），要么事件丢失 | **解耦**：worker 把事件投进桥就返回，不在乎消费者在不在线 |
| **多个客户端看不到同一次 run** | 想在两个浏览器标签页看同一次 run 的进度，一个 worker 只能推给一个连接，做不到 | **事件日志 + 多消费者**：每个消费者各读各的 offset，互不抢 |
| **内存爆炸** | 无限给一个 run 缓冲所有事件，长 run 累积几万条，进程 OOM | **有界窗口 + 淘汰最旧**：每 run 最多 `queue_maxsize` 条 |
| **代理掐断空闲连接** | nginx / 云负载均衡器对「长时间无数据」的 HTTP 连接会静默关闭（默认约 60 秒）。run 在思考、暂时没事件，SSE 被掐了，前端以为断了 | **心跳**：无事件超时发哨兵，端点转成 SSE 注释保活 |

一句话：**解耦生产/消费 + 有界窗口重连补播 + 心跳保活**。

---

## §2 零基础名词（第一次出现都解释）

**SSE（Server-Sent Events，服务器推送事件）**：HTTP 上的「服务器单向推流」协议。服务器不关连接，持续写一行行 `event:`/`data:` 文本，浏览器用 `EventSource` 接收。比 WebSocket 简单（单向、纯文本、浏览器自动重连），适合「AI 边想边吐字」这种场景。

**worker**：后台跑 agent 的任务。一次 run 可能跑几分钟，期间不断产生事件（AI 的每个 token、每次工具调用、每次状态更新）。

**生产者 / 消费者 / 解耦**：
- **生产者**（producer）：worker，产生事件。
- **消费者**（consumer）：SSE 端点，把事件推给前端。
- **解耦**：生产者把事件投进流桥就返回（不阻塞、不等消费者）；消费者从流桥读。两者各自独立节奏。

**类比**：流桥是「**快递柜**」。worker（快递员）把包裹投进柜就走；前端（收件人）有空来取。柜子有限格（有界窗口），旧包裹超期被清。多个收件人（多标签页）可以各自来取同一个包裹——只要它还没过期。

**EventSource**：浏览器内置的 SSE 客户端。它会自动重连，并在重连的请求头带上 `Last-Event-ID`（最后收到的事件 id）。

**Last-Event-ID 重连**：连接断开后浏览器自动重连 + 带上 `Last-Event-ID`，流桥据此**从该事件之后续播**——前端不重复看已收到的事件，也不漏掉断连期间的事件（只要它们还在缓冲窗口里）。

**有界窗口 / eviction / start_offset**：每个 run 的事件不是无限保留，而是**最多 `queue_maxsize` 条**（默认 256）。超出就**淘汰最旧的**（eviction）。用 `start_offset` 记「当前缓冲里最早事件的全局序号」——逻辑上事件流是 `{0,1,2,...}` 无限的，但内存里只留一个滑动窗口 `[start_offset, start_offset+len)`。

**心跳（heartbeat）**：消费者如果在 `heartbeat_interval` 秒内没收到任何事件，流桥发一个 `HEARTBEAT_SENTINEL`。SSE 端点把它转成一行注释（`: keepalive`）写给前端——这条「数据」维持连接活跃，让 nginx 等代理看到「连接还活着」，不掐断。

---

## §3 整体结构

```
runtime/stream_bridge/
├── __init__.py          # 导出：make_stream_bridge / StreamBridge / MemoryStreamBridge / StreamEvent / 两个哨兵
├── base.py              # StreamEvent（frozen dataclass）+ HEARTBEAT/END 哨兵 + StreamBridge ABC
├── memory.py            # MemoryStreamBridge（每 run _RunStream + Condition + 有界窗口 + Last-Event-ID + 心跳）
└── async_provider.py    # make_stream_bridge(app_config) async cm（按 config 选 memory / redis）
```

依赖：[config/stream_bridge_config.py](../backend/packages/harness/deerflow/config/stream_bridge_config.py)（`type`/`redis_url`/`queue_maxsize`）、[config/app_config.py](../backend/packages/harness/deerflow/config/app_config.py) 的 `stream_bridge` 段。**无** LangChain / 持久化依赖——这是个纯 asyncio 中间件，不碰 ORM、不碰模型。

> 行业类比：这个「Queue + StreamManager」中转模式，与 LangGraph Platform 的流式架构是同一类思路（中转 + 缓冲 + 重连补播）。mini 只做了单进程内存版。

---

## §4 核心概念

### 4.1 StreamEvent（一条事件）

[base.py:19-32](../backend/packages/harness/deerflow/runtime/stream_bridge/base.py#L19-L32) 一个 frozen dataclass，三字段：

| 字段 | 含义 | 例 |
|------|------|----|
| `id` | 单调递增的事件 id，格式 `{毫秒时间戳}-{seq}` | `1718600000000-3` |
| `event` | SSE 事件名 | `metadata` / `updates` / `events` / `error` / `end` |
| `data` | JSON 可序列化的 payload | `{"i": 0}` |

`id` 用作 SSE 协议的 `id:` 字段——浏览器 `EventSource` 会记住最后收到的 id，断线重连时在请求头回传，就是 `Last-Event-ID`。

### 4.2 两个哨兵（sentinel）

[base.py:35-36](../backend/packages/harness/deerflow/runtime/stream_bridge/base.py#L35-L36)：

```python
HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)  # 等待超时
END_SENTINEL       = StreamEvent(id="", event="__end__",       data=None)  # run 结束
```

「哨兵」= 一个预先做好的、独一无二的对象，专门用来发「特殊信号」。消费者用 `entry is END_SENTINEL`（身份判等）来识别，**绝不会**和任何用户 data 混淆（用户 data 是 dict/str，不可能 `is` 等于哨兵对象）。两者对消费者迭代器的影响不同——见 §6.5。

### 4.3 每个 run 一个 `_RunStream`

[memory.py:27-32](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L27-L32)：一个 run 在内存里的全部状态。

| 字段 | 类型 | 作用 |
|------|------|------|
| `events` | `list[StreamEvent]` | 有界事件日志（滑动窗口） |
| `condition` | `asyncio.Condition` | 通知 + 等待新事件的生产/消费同步原语 |
| `ended` | `bool` | 生产者是否已 `publish_end` |
| `start_offset` | `int` | 当前 `events[0]` 对应的全局序号（eviction 后递增） |

### 4.4 StreamBridge ABC（四方法一默认）

[base.py:39-72](../backend/packages/harness/deerflow/runtime/stream_bridge/base.py#L39-L72)：抽象基类定契约，子类填实现。

```
publish(run_id, event, data)       生产者：入队一条
publish_end(run_id)                生产者：示意不再有新事件
subscribe(run_id, *, last_event_id=None, heartbeat_interval=15.0)
                                   消费者：yield 事件的异步迭代器（超时发心跳，end 后收 END 再停）
cleanup(run_id, *, delay=0)        释放某 run 的资源
close()                            释放全部（默认 no-op，见 base.py:71-72）
```

---

## §5 代码走读（逐函数）

### 5.1 `__init__` —— 两个字典登记 run

[memory.py:42-45](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L42-L45)：`_streams: dict[run_id, _RunStream]` + `_counters: dict[run_id, int]`（每个 run 的递增计数器，给 id 分配 seq）。`queue_maxsize` 默认 256（[memory.py:42](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L42)）。

### 5.2 `_next_id` —— 生成 `{ts_ms}-{seq}`

[memory.py:55-59](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L55-L59)：先 `_counters[run_id] += 1`，`seq = counter - 1`（从 0 起），`ts = int(time.time()*1000)`，拼成 `f"{ts}-{seq}"`。

**关键**：`seq` 是**该 run 内从 0 起的单调递增序号，恰好等于这条事件在 run 内的绝对 offset**。这个性质是 §5.4 的 O(1) 重连定位的基础——后面会反复用到。

### 5.3 `_parse_event_seq` —— 从 id 反解 seq

[memory.py:61-74](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L61-L74)：`event_id.rpartition("-")` 取最后一段，`int()` 之。格式不符（没有 `-`、不是数字）返回 `None`。用 `rpartition` 而非 `split` 是因为时间戳里不含 `-`，但要稳健地只切最后一个 `-` 后的部分。

### 5.4 `_resolve_start_offset` —— O(1) 重连定位（核心）

[memory.py:76-94](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L76-L94)：`subscribe(last_event_id)` 时要算「从缓冲的第几条开始吐」。这是重连补播的关键，逻辑分三步：

```python
def _resolve_start_offset(self, stream, last_event_id):
    if last_event_id is None:                          # 首次订阅：从窗口头起
        return stream.start_offset

    seq = self._parse_event_seq(last_event_id)         # 1. 算术定位
    if seq is not None:
        local_index = seq - stream.start_offset
        if 0 <= local_index < len(stream.events) and stream.events[local_index].id == last_event_id:
            return stream.start_offset + local_index + 1   # 2. 核验通过：从下一条起

    if stream.events:                                  # 3. 核验不符：回退最早保留
        logger.warning("last_event_id=%s not found in retained buffer; replaying from earliest retained event", last_event_id)
    return stream.start_offset
```

- **步骤 1（算术，O(1)）**：因为 seq == 绝对 offset（§5.2），`local_index = seq - start_offset` 直接算出这条事件在 `events` 列表里的下标，不必线性扫。
- **步骤 2（核验 id）**：算出 `local_index` 后还要确认 `events[local_index].id == last_event_id`。这一步**至关重要**——纯算术定位若碰到一个「恰好 plausible」的外来 seq（别的 run 的、或本地重启后 seq 撞了），会从错误位置开始吐。核验保证只有「真本 run 产出的、且还在窗口内」的 id 才被当 resume 锚点。
- **步骤 3（回退）**：核验不符（外来 id / 畸形 id / seq 在窗口之外已被淘汰）→ 从 `start_offset`（最早保留事件）回放，打警告。这是「尽力补播」——窗口已淘汰的精确位置保不住，至少不丢当前窗口的，且行为和旧的线性扫完全一致。

> 旧的实现是线性扫整个缓冲找匹配 id（O(n)），每次重连都扫。改用「seq 嵌进 id + 算术定位」是 O(1) 的优化，但**保留了核验这一步**，所以语义不变、只是更快。

### 5.5 `publish` —— 入队 + eviction + notify_all

[memory.py:98-107](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L98-L107)：

```python
async def publish(self, run_id, event, data):
    stream = self._get_or_create_stream(run_id)
    entry = StreamEvent(id=self._next_id(run_id), event=event, data=data)
    async with stream.condition:                  # 拿锁
        stream.events.append(entry)
        if len(stream.events) > self._maxsize:    # 超窗口 → 淘汰最旧
            overflow = len(stream.events) - self._maxsize
            del stream.events[:overflow]
            stream.start_offset += overflow       # 窗口头右移
        stream.condition.notify_all()             # 唤醒所有等待的消费者
```

eviction 后 `start_offset` 递增——这正是「落后恢复」（§6.3）的根源：订阅者的 `next_offset` 可能落到新的 `start_offset` 之前。

### 5.6 `publish_end` —— 标记结束

[memory.py:109-113](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L109-L113)：`ended = True` + `notify_all()`。注意**不直接发 END 哨兵**——END 由 `subscribe` 在消费者读完缓冲后自己 yield（§5.7），这样保证消费者不会漏掉 `publish_end` 之前已入队但还没读的事件。

### 5.7 `subscribe` —— 消费者主循环（最长最关键）

[memory.py:115-153](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L115-L153)：

```python
async def subscribe(self, run_id, *, last_event_id=None, heartbeat_interval=15.0):
    stream = self._get_or_create_stream(run_id)
    async with stream.condition:
        next_offset = self._resolve_start_offset(stream, last_event_id)  # 先定续播点

    while True:
        async with stream.condition:
            if next_offset < stream.start_offset:                 # A. 落后：被淘汰了
                logger.warning("subscriber for run %s fell behind ...", run_id)
                next_offset = stream.start_offset                 #    从窗口头恢复

            local_index = next_offset - stream.start_offset
            if 0 <= local_index < len(stream.events):             # B. 有事件可读
                entry = stream.events[local_index]
                next_offset += 1
            elif stream.ended:                                    # C. 没事件且已 end
                entry = END_SENTINEL
            else:                                                 # D. 没事件没 end → 等
                try:
                    await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                except TimeoutError:
                    entry = HEARTBEAT_SENTINEL                    #    超时发心跳
                else:
                    continue                                      #    被唤醒：回去重判 A/B/C

        if entry is END_SENTINEL:                                 # END → yield + 正常结束
            yield END_SENTINEL
            return
        yield entry                                               # 普通事件/心跳 → yield 后继续循环
```

四个分支的理解：
- **A（落后恢复）**：订阅者的 `next_offset`（下次要读的全局序号）落到了 `start_offset` 之前——它断连太久，窗口已经滑过去了。从 `start_offset` 恢复（最早还保留的），丢失的不补（§6.3）。
- **B（读事件）**：offset 在窗口内，读 `events[local_index]`，`next_offset += 1`。
- **C（END）**：缓冲读空且 `ended` → yield END，`return` 结束迭代器。
- **D（等待）**：缓冲读空但没 end → `wait(timeout)`。超时发心跳（不结束循环）；被生产者唤醒则 `continue` 重新判 A/B/C（此时可能已有新事件）。

注意心跳和 END 的区别：**心跳 yield 后继续循环**（连接还活着，继续等），**END yield 后 `return`**（run 真的结束了）。这就是为什么两者要分开处理。

### 5.8 `cleanup` / `close`

[memory.py:155-159](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L155-L159)：`cleanup(run_id, delay=0)` 先 `sleep(delay)`（给迟到的订阅者排空机会），再 `pop` 掉该 run 的 `_streams`/`_counters`。[:161-163](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L161-L163) `close()` 清空全部（进程退出时）。

### 5.9 `make_stream_bridge` —— 工厂（async cm）

[async_provider.py:26-48](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py#L26-L48)：`@asynccontextmanager`，按 config 选后端：

```python
async with make_stream_bridge() as bridge:   # app.state.stream_bridge = bridge
    ...                                        # 退出时 finally 自动 bridge.close()
```

逻辑：`config is None or config.type == "memory"` → `MemoryStreamBridge(queue_maxsize=maxsize)`（config 为 None 时 maxsize 取默认 256，[async_provider.py:41](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py#L41)）；`config.type == "redis"` → `NotImplementedError`（[async_provider.py:50-51](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py#L50-L51)）；未知 type → `ValueError`（[async_provider.py:53](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py#L53)）。

设计上对齐 [make_checkpointer](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py)——都是 async context manager，给 FastAPI lifespan 用（见 [#8 checkpointer.md](checkpointer.md)）。

---

## §6 设计权衡（不变量 / 踩坑）

### 6.1 为什么用 Condition + 事件日志，而非 Queue

[memory.py:30](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L30) 每个 run 一个 `asyncio.Condition` + 一个 `list`（事件日志）。**不用 `asyncio.Queue`** 的两个硬理由：

- **Queue 是单消费的**——一个 `get()` 走了事件就没了，第二个消费者（重连的、多标签页的）看不到。事件日志让多消费者各读各的 offset，事件本身不消费掉。
- **Condition 支持回放**：新订阅者先读 `events` 列表（从它的 offset 起），再 `wait()` 等新事件；生产者 `notify_all()` 唤醒所有等待者。Queue 没有「读历史」的概念。

### 6.2 有界窗口的 eviction 语义

`publish` 后若 `len(events) > maxsize`：删掉最前面的 `overflow` 条，`start_offset += overflow`（[memory.py:103-106](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L103-L106)）。效果：**内存恒定**（每 run 最多 maxsize 条）。代价：**被淘汰的事件，重连时补不回来**——客户端断开太久，期间产生的事件超过窗口就会「落后」。

### 6.3 落后恢复（fell-behind）

[subscribe 的 A 分支](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L128-L134)：订阅者 `next_offset` 落到 `start_offset` 之前时，从 `start_offset` 恢复，打警告，**丢失的不补**。这是有界窗口的固有代价——客户端能感知（seq 跳跃）并决定是否走持久化存储全量重取（§8）。

测试 `test_subscriber_fell_behind_resumes_from_start_offset` 锁住这个行为。

### 6.4 Last-Event-ID 续播：为什么 O(1) 还要核验

详见 §5.4。一句话：**算术定位（O(1)）靠 seq==offset 的性质，核验 id 防外来/畸形/过期 id 误判**。两者缺一不可——只算术会从错误位置吐，只核验就退化成 O(n) 扫。

### 6.5 END 哨兵 vs 心跳哨兵

| 哨兵 | 何时发 | 迭代器 | 前端怎么处理 |
|------|--------|--------|------------|
| `HEARTBEAT_SENTINEL` | `wait` 超时（默认 15s 无事件） | **不结束**，继续循环 | 转成 SSE 注释 `: keepalive` 保活，不显示 |
| `END_SENTINEL` | 缓冲读空 + `publish_end` 已调 | **结束**（`yield + return`） | 关 `EventSource` |

两者用「哨兵对象 + `is` 判等」，不会被用户 data 误触（用户 data 是 dict/str，不会 `is` 等于这两个预置对象）。

### 6.6 id 为何是 `{ts_ms}-{seq}` 而非纯序号

纯序号（`0,1,2`）逻辑上就够了，为何加时间戳（[memory.py:55-59](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L55-L59)）？

- **跨重启的弱唯一性**：进程重启后 seq 从 0 重来，但旧的事件 id 若还在前端的 `Last-Event-ID` 里，`0` 可能撞上新一轮的 `0`。加毫秒时间戳让不同时段的 id 几乎不撞。
- **可读 / 可调试**：看 id 就知道大概何时产生。
- `seq` 部分保证同毫秒内的单调（计数器递增）。

**不能保证**跨进程唯一（两进程的 seq 各自从 0 起，ts 大概率不同但不绝对）。要绝对唯一得上 Redis（全局 seq），这正是 Redis 实现的意义之一（§6.8）。

### 6.7 `cleanup` 的 `delay` 有什么用

run 刚结束时，可能有客户端正在重连 / 刚订阅。`cleanup(run_id, delay=...)` 的 `delay > 0` 让事件多留一会儿（`await asyncio.sleep(delay)`），给它们排空的机会，再释放（[memory.py:156-157](../backend/packages/harness/deerflow/runtime/stream_bridge/memory.py#L156-L157)）。worker 退出时常传一个小的 delay。

### 6.8 为什么是 ABC + 仅 memory 实现

`StreamBridge` 是抽象基类（[base.py:39](../backend/packages/harness/deerflow/runtime/stream_bridge/base.py#L39)），目前只有 `MemoryStreamBridge`。留 ABC 是为未来加 **Redis** 实现（跨进程 / 多节点共享事件流，`redis_url`）——届时多个 worker 进程能把事件推到同一个 Redis Stream，任一前端都能订阅，seq 由 Redis 全局生成保证唯一。

Redis 当前 [直接 `NotImplementedError`](../backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py#L50-L51)。**memory 实现只能单进程**——多 worker 进程部署时，前端连的进程 A 看不到进程 B 跑的 run 的事件。这是 mini 作为教学版刻意保留的边界（不 port Gateway / 多节点部署）。

---

## §7 配置与用法

### 7.1 StreamBridgeConfig

[config/stream_bridge_config.py:16-30](../backend/packages/harness/deerflow/config/stream_bridge_config.py#L16-L30)：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `type` | `Literal["memory","redis"]` | `"memory"` | 后端类型。memory = 进程内（仅单进程）；redis = Redis Streams（规划中，未实现） |
| `redis_url` | `str \| None` | `None` | redis 类型的连接 URL（例 `redis://localhost:6379/0`） |
| `queue_maxsize` | `int` | `256` | memory 桥里每个 run 缓冲的最大事件数（有界窗口，超限淘汰最旧） |

`stream_bridge` 段在 AppConfig 里可设为 `None`——表示「用内存默认」（memory + 默认 maxsize）。

### 7.2 生产者-消费者基本流（可跑 demo）

```python
import asyncio
from deerflow.runtime.stream_bridge import MemoryStreamBridge, END_SENTINEL

bridge = MemoryStreamBridge(queue_maxsize=256)

async def producer():
    for i in range(3):
        await bridge.publish("run-1", "updates", {"i": i})
    await bridge.publish_end("run-1")          # 跑完必须调，否则消费者永不结束

async def consumer():
    async for ev in bridge.subscribe("run-1"):
        if ev is END_SENTINEL:                 # 用 is 判等
            break
        print(ev.id, ev.event, ev.data)

await asyncio.gather(producer(), consumer())
```

### 7.3 重连补播

```python
# 前端断开时最后收到的事件 id 是 last_id
async for ev in bridge.subscribe("run-1", last_event_id=last_id):
    ...   # 从 last_id 之后续播，O(1) 定位 + id 核验
```

### 7.4 lifespan 装配

```python
from deerflow.runtime.stream_bridge import make_stream_bridge

async with make_stream_bridge() as bridge:    # 退出时 finally 自动 bridge.close()
    app.state.stream_bridge = bridge
    ...
```

实际接入见 [#26 runs.md](runs.md)（worker 作生产者）+ [#28 architecture.md](architecture.md)（lifespan 把 bridge 装进 RuntimeBundle）。

---

## §8 与其它模块的关系

```
config/stream_bridge_config ──→ async_provider.make_stream_bridge
                                         │
                                         ▼
                              stream_bridge/{base, memory}
                                         │
          runs/worker（生产者）──publish──→ bridge ──subscribe──→ SSE / stream 端点（消费者）
                                         │
                            心跳 → SSE `: keepalive`（防代理掐断）
                            END  → 前端关 EventSource
                            Last-Event-ID → 重连补播（O(1)）
```

- **被谁依赖**：[#26 runs.md](runs.md) 的 worker（生产者）、SSE / stream 端点（消费者）、[#28 architecture.md](architecture.md) 的 lifespan 装配。
- **依赖谁**：[#3 config.md](config.md)（StreamBridgeConfig / AppConfig）。无业务模块依赖。
- **平行设计**：`make_stream_bridge` 的 async cm 形态对齐 [#8 checkpointer.md](checkpointer.md) 的 `make_checkpointer`。

### 与 RunEventStore / RunJournal 的区别（重要）

这是最容易混的三件事，一次讲清：

| | stream_bridge（本篇） | [#9 RunEventStore](run_event_store.md) | [#10 RunJournal](run_journal.md) |
|---|---|---|---|
| **定位** | 实时传输的事件流 | 持久化的事件流 | RunEventStore 的写入侧采集器 |
| **存储** | 内存、瞬时、进程内 | 落盘（db/jsonl/memory）、可查、跨重启 | 不存储，只采集+转发 |
| **给谁** | 在线前端（实时推送） | 离线查询 / 历史回放 / 审计 | 把 LangChain 回调转成事件喂给 store |
| **谁写** | worker | RunJournal（经 store 接口） | LangChain 回调 |

一次 run 里，worker 会**同时**往 bridge 和 event store 写：**bridge 给在线前端实时推，event store 落盘给离线查**。所以即便前端断线错过了 bridge 的有界窗口，event store 里那份完整的还在——要补全量就走持久化存储离线重取。

---

## §9 常见问题 / 排错

**Q: 前端重连后丢了一些事件？**
A: 断连太久，事件超过了 `queue_maxsize`（默认 256）窗口被淘汰。三选一：① 调大 `stream_bridge.queue_maxsize`；② 接受「落后时从最早保留事件续」（部分丢失，§6.3）；③ 要完全不丢，走持久化的 [#9 RunEventStore](run_event_store.md) + 离线补取。

**Q: SSE 连接老是被 nginx 关？**
A: 调小 `heartbeat_interval`（默认 15 秒）。心跳让连接保持活跃，绕过代理的空闲超时。确认 SSE 端点把 `HEARTBEAT_SENTINEL` 转成了一行 SSE 注释（`: keepalive\n\n`）。

**Q: `subscribe` 的迭代器一直不停？**
A: 没收到 `END_SENTINEL`。确认生产者跑完调了 `publish_end(run_id)`——否则消费者会一直等（发心跳但不结束）。

**Q: 多个 worker 进程，前端看不到别进程的事件？**
A: `MemoryStreamBridge` 是单进程的。多进程 / 多节点要用 Redis 实现（当前 `NotImplementedError`）。mini 作教学版不 port 多节点部署。

**Q: 事件 id 重复了？**
A: 单进程内正常不会——`{ts_ms}-{seq}` 里 seq 每 run 单调递增。跨进程则两进程的 seq 各自从 0 起，id 的 ts 部分大概率不同但不绝对；要绝对唯一得上 Redis（全局 seq）。

**Q: `_resolve_start_offset` 为什么定位完还要核验 id？**
A: 防外来 / 畸形 / 过期 id 误判续播点。纯算术（seq==offset）若碰到一个恰好 plausible 的外来 seq，会从错误位置开始吐；核验保证只有真本 run 的、还在窗口内的 id 才当 resume 锚点（§5.4 / §6.4）。

---

## §10 小结

StreamBridge 解决的是「长任务 + 前端实时推送」的四个老大难：**解耦**（worker 不等消费者）、**多消费者**（事件日志 + Condition，而非单消费的 Queue）、**有界防爆**（256 滑动窗口 + eviction）、**重连续播**（`{ts_ms}-{seq}` 内嵌 offset → O(1) 算术定位 + id 核验）、**心跳保活**（防代理掐断）。

记住三组对子就够：
- **Condition + 事件日志** vs Queue：前者支持多消费者 + 回放。
- **HEARTBEAT vs END**：心跳继续循环、END 结束迭代器，都用 `is` 判等。
- **stream_bridge vs RunEventStore**：一个实时内存传输给前端，一个持久落盘给离线查；worker 同时往两者写。

它和 RunEventStore 是「同一种事件流」的两种用途——理解了这篇，再回头看 [#9](run_event_store.md) / [#10](run_journal.md) 的「存储侧 / 写入侧」分工会更清楚。

---

> 上一篇：[#10 run_journal.md](run_journal.md)（RunEventStore 的写入侧采集器） · 下一篇：[#12 serialization.md](serialization.md)（序列化与消息转换——LangChain/LangGraph 对象 → JSON 的单一真相源，放最后讲「为什么剥 pregel / image」）
