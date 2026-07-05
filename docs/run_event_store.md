# 9. run_event_store.md — 运行事件存储（消息 + 轨迹，seq 单调 + 路径穿越防御）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：RunEventStore 是「一次 run 里**发生了什么**」的统一记录本——前端要展示的对话消息、供调试的执行轨迹、run 的生命周期事件，都走**同一个接口**，靠 `category` 字段区分。它和 [#7 persistence.md](persistence.md) 的 `RunRow`（run 摘要）是配合关系：`RunRow` 存「这次 run **结果**如何、token 用了多少」，RunEventStore 存「这次 run **一步步**发生了什么」。

> 配套代码：[runtime/events/](../backend/packages/harness/deerflow/runtime/events/)——[store/base.py](../backend/packages/harness/deerflow/runtime/events/store/base.py)（ABC）· [store/memory.py](../backend/packages/harness/deerflow/runtime/events/store/memory.py) · [store/jsonl.py](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py) · [store/db.py](../backend/packages/harness/deerflow/runtime/events/store/db.py) · [__init__.py](../backend/packages/harness/deerflow/runtime/events/__init__.py)（工厂）。表结构见 [#7 persistence.md](persistence.md) §5.7（`RunEventRow`）。测试见 [test/test_events.py](../test/test_events.py)。

## 学完这篇你能回答什么（learning outcomes）

- 为什么对话消息 / 执行轨迹 / 生命周期事件要走**同一个存储接口**、靠 `category` 区分（`message` 给用户看 / `trace` 给开发者看 / `lifecycle` 记 run 始末）？
- 为什么同一 thread 内 `seq` 必须**严格单调递增**？三个后端各自靠什么保证（memory 计数器 / jsonl 每线程 asyncio.Lock / db 的 `FOR UPDATE` 或 advisory lock）？
- **SQLite 解析但忽略 `FOR UPDATE`**——这意味着什么？靠什么兜底（`UNIQUE(thread_id, seq)` 约束）？为什么生产中不是问题（RunJournal 用 `put_batch` 单事务批量分配）？
- **双向游标分页**为什么用 `seq` 作游标而不是「第几页」（消息不断追加，页码会漂移，seq 是稳定锚点）？
- jsonl 后端把 `thread_id` / `run_id` 拼进文件路径——为什么要校验 `^[A-Za-z0-9_-]+$`（防 `../` 路径穿越越权写文件）？
- db 后端为什么对 `trace` 内容按字节截断（trace 可能几 MB，会撑爆数据库）？为什么 `content` 是 dict 时要 JSON 往返（列是 `Text`，但调用方要拿到原始类型）？
- memory 后端为什么要维护 `_messages` / `_events_by_run` / `_messages_by_run` **多组投影**（热路径读不必重扫全量，按 thread / 按 run 各自 O(log + page)）？

> 这些都是后端 / agent 工程面试的高频点——「事件溯源 / 时序存储」「并发序号分配」「分页设计（游标 vs 偏移）」「安全（路径穿越）」。

---

## 1. 为什么需要它（痛点）

先看「没有它」会怎样：

- **前端拿不到历史消息**。用户刷新页面想看「这个线程之前的对话」，没有事件流存储就只能从 checkpointer 的图状态里硬抠，既慢又乱。
- **出问题没法回放**。run 跑挂了，想知道「第 3 步调了哪个工具、返回了什么」，没有轨迹记录就两眼一抹黑。
- **消息顺序错乱**。多个事件并发写入，没有单调序号，前端按到达顺序渲染就会跳跃。
- **路径被恶意构造**。用户传一个 `thread_id="../../etc"`，事件写到文件系统任意位置——越权写文件。

RunEventStore 解决这些：**统一接口存消息+轨迹**、**seq 单调保证顺序**、**路径穿越防御**、**三后端按需选**。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉事件流 / 分页 / 并发的话，先读这一节。

### event / category / seq

一条 **event（事件）** 是 run 内的一个时间点记录，核心字段：

| 字段 | 含义 |
|------|------|
| `thread_id` / `run_id` | 归属（哪个线程的哪次 run） |
| `event_type` | 事件类型（如 `human_message`、`ai_message`、`tool_call`） |
| **`category`** | **大类**：`message`（前端展示）/ `trace`（调试轨迹）/ `lifecycle`（run 开始/结束） |
| `content` | 内容（字符串或结构化 dict） |
| `metadata` | 附加元数据（如 `content_truncated`） |
| **`seq`** | **单调递增序号**（同一 thread 内） |
| `created_at` | 时间戳 |

**关键区分：`category` 决定「这条事件给谁看」**。`message` 是给用户看的对话气泡；`trace` 是给开发者看的执行细节（可能很长很碎）。前端列表只查 `message`，调试只查 `trace`。

`seq` 是「这一条在整个 thread 里排第几」。**同一 thread 内严格递增、不重复**——这是前端正确排序的基石。

类比：**thread 是一本书，run 是书里的一个章节，event 是章节里的一句话，`seq` 是这句话在全书的页码（全局连续），`category` 决定这句话是「正文」（message）还是「脚注」（trace）**。

### 双向游标分页（cursor pagination）

消息可能很多，前端不能一次全拉。`list_messages` 支持：

- **`after_seq`**：返回 seq 大于它的前 `limit` 条（**向后翻页**，看更新的）。
- **`before_seq`**：返回 seq 小于它的最后 `limit` 条（**向前翻页**，看更旧的）。
- 都不给：返回最近 `limit` 条。

为什么用 seq 作游标而不是「第几页」？因为消息在不断追加，「页码」会随新消息漂移（翻到第 2 页时新消息进来了，内容就错了）。seq 是**稳定锚点**——「seq > 100 的前 50 条」永远精确。

---

## 3. 整体结构：它在系统里的位置

```
runtime/events/
├── __init__.py              # make_run_event_store 工厂（按 run_events.backend 选实现）
└── store/
    ├── __init__.py          # 导出 RunEventStore + MemoryRunEventStore
    ├── base.py              # RunEventStore ABC（8 方法）
    ├── memory.py            # MemoryRunEventStore（+ 4 组投影优化）
    ├── jsonl.py             # JsonlRunEventStore（路径穿越防御 + 每线程锁 + IO 卸载）
    └── db.py                # DbRunEventStore（FOR UPDATE/advisory + trace 截断 + JSON 往返 + UUID→str）
```

它在系统里的位置（与 checkpointer / RunRow 平行，各管各的）：

```
config/run_events_config ─┐
persistence/models/run_event (RunEventRow 表) ─┤
runtime/user_context (三态 user_id) ───────────┤
utils/time (coerce_iso) ───────────────────────┤
                                                ▼
                          runtime/events/store/{base,memory,jsonl,db}
                                                │
                                  make_run_event_store(config)
                                                │
                runtime/journal.RunJournal（#10）──put_batch──→ store
                                                │
                消息/事件查询端点 ──list_messages──→ store
```

- **与 checkpointer 的区别**：checkpointer 存「图状态快照」（可恢复执行）；event store 存「事件流」（可回放/展示）。前者是「机器读」的，后者是「人读」的。
- **与 RunRow 的区别**：RunRow 是 run 的「摘要卡」（状态、token、首末消息）；event store 是 run 的「流水账」（每一步）。

---

## 4. 核心概念

### 4.1 三个后端

| 后端 | 存哪 | 何时用 | seq 单调靠什么 |
|------|------|--------|---------------|
| **memory** | 进程内存 | 开发、测试；重启即失 | 内存计数器（单进程天然安全） |
| **jsonl** | 每 run 一个 `.jsonl` 文件 | 单节点轻量持久化 | 内存计数器 + 每线程 `asyncio.Lock` |
| **db** | `run_events` 表（SQLAlchemy） | 生产、多节点、要查询 | `SELECT max(seq) FOR UPDATE`（postgres 用 advisory lock） |

### 4.2 RunEventStore ABC（8 方法，[base.py:24](../backend/packages/harness/deerflow/runtime/events/store/base.py#L24)）

```python
class RunEventStore(abc.ABC):
    async def put(*, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None) -> dict   # :26
    async def put_batch(events: list[dict]) -> list[dict]            # :40  高频路径（RunJournal flush）
    async def list_messages(thread_id, *, limit=50, before_seq=None, after_seq=None) -> list[dict]   # :47
    async def list_events(thread_id, run_id, *, event_types=None, limit=500) -> list[dict]            # :64
    async def list_messages_by_run(thread_id, run_id, *, limit=50, before_seq=None, after_seq=None) -> list[dict]   # :78
    async def count_messages(thread_id) -> int                       # :93
    async def delete_by_thread(thread_id) -> int                     # :97  返回删除条数
    async def delete_by_run(thread_id, run_id) -> int                # :101
```

所有实现必须保证（[base.py:11-16](../backend/packages/harness/deerflow/runtime/events/store/base.py#L11)）：① put 的事件后续能取到；② **同 thread 内 seq 严格递增**；③ `list_messages` 只返回 `category="message"`；④ `list_events` 返回指定 run 的全部事件；⑤ 返回的 dict 符合 RunEvent 字段结构。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 memory 后端：4 组投影 + bisect 分页（[memory.py](../backend/packages/harness/deerflow/runtime/events/store/memory.py)）

`MemoryRunEventStore` 维护**4 个字典**（[第 27-38 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L27)），存的都是**同一个 dict 对象、无拷贝**：

| 字典 | 粒度 | 内容 |
|------|------|------|
| `_events` | thread 级 | 全量事件，按 seq 排序 |
| `_messages` | thread 级 | 仅 `category=message` 投影 |
| `_events_by_run` | run 级 | `thread_id → run_id → 事件` |
| `_messages_by_run` | run 级 | `thread_id → run_id → message` |

`_put_one`（[第 46 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L46)）往 `_events` 和 `_events_by_run` append；若是 message 还往 `_messages` 和 `_messages_by_run` append——**同一个 dict 对象**，不拷贝。

为什么要 4 组？让热路径读**不必重扫全量**：

- `list_messages`（[第 103 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L103)）：在 thread 级 `_messages`（按 seq 排序）上用 `bisect` 做 O(log m + page)，而非每次扫所有事件（含大量 trace）。
- `list_events`（[第 120 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L120)）：直接取 `_events_by_run[thread_id].get(run_id, [])`，只触碰该 run 的事件。
- `list_messages_by_run`（[第 128 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L128)）：在 `_messages_by_run` 上 `bisect` 定位游标窗口。

`delete_by_run`（[第 152 行](../backend/packages/harness/deerflow/runtime/events/store/memory.py#L152)）同步从 4 个投影清掉对应条目，保持 lockstep。

### 5.2 jsonl 后端：路径穿越防御 + 每线程锁 + IO 卸载（[jsonl.py](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py)）

每个 run 的事件存在单个文件 `.deer-flow/threads/{thread_id}/runs/{run_id}.jsonl`。三个关键设计：

- **路径穿越防御**：`_SAFE_ID_PATTERN = ^[A-Za-z0-9_-]+$`（[第 33 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L33)），`_validate_id`（[第 46 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L46)）校验 `thread_id` / `run_id`，`/`、`.`、空格全拒绝，校验失败抛 `ValueError`——防 `../` 逃出 base_dir。
- **每线程锁**：`_write_locks`（[第 41 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L41)）每 thread 一个 `asyncio.Lock`，串行化单进程内的写，防 JSONL 行交错（保证 seq 单调）。
- **IO 卸载**：所有文件读写（`open`/`read_text`/`glob`/`unlink`）都包成同步方法，经 `await asyncio.to_thread(...)` 丢到线程池（如 [第 83/150/163 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L83)），不阻塞事件循环。
- **lazy seq 加载**：`_ensure_seq_loaded`（[第 79 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L79)）首次写时 `_compute_max_seq`（[第 65 行](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L65)）扫现有文件载入 max(seq)，保证重启后 seq 接着涨。

### 5.3 db 后端：seq 锁 + trace 截断 + JSON 往返 + user_id stamp（[db.py](../backend/packages/harness/deerflow/runtime/events/store/db.py)）

**seq 单调的锁**（`_max_seq_for_thread`，[第 93 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L93)）：

```python
if dialect_name == "postgresql":
    # postgres 拒绝 SELECT max(...) FOR UPDATE（聚合结果不是可锁行）
    # → 用事务级 advisory lock 串行化同 thread 写者
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(...))"))
    return await session.scalar(stmt)
return await session.scalar(stmt.with_for_update())   # sqlite 等保留行锁
```

**trace 截断**（`_truncate_trace`，[第 55 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L55)）：只对 `category="trace"`，按**字节**截断到 `max_trace_content`（默认 10240），decode 用 `errors="ignore"`（可能切断多字节字符），并在 metadata 标 `content_truncated=True` + `original_byte_length`（[第 62 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L62)）。message 不截断（用户要看完整内容）。

**JSON content 往返**：`_content_to_db`（[第 65 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L65)）写时把 dict `json.dumps` 成字符串（列是 `Text`），标 `content_is_json` / `content_is_dict`；`_row_to_dict`（[第 47-49 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L47)）读回检测标记 `json.loads` 还原。调用方拿到的始终是原始类型。

**user_id stamp**（`_user_id_from_context`，[第 77 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L77)）：写时从 contextvar 软读 user_id，在边界 `str(user.id)`（[第 89-90 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L89)，UUID→VARCHAR，详见 [#5 user_context.md](user_context.md)），stamp 到行的 `user_id` 列。

**`put`（低频，[第 113 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L113)）** 开一个带 FOR UPDATE 锁的专用事务分配 seq——当前唯一调用方是 worker 写初始 `human_message`（每 run 一次）。**`put_batch`（高频，[第 141 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L141)）** 先校验「整批属同一 thread」（[第 145-146 行](../backend/packages/harness/deerflow/runtime/events/store/db.py#L145)），然后单事务只取一次锁、批量分配连续 seq——RunJournal flush buffer 用它，避开并发问题。

---

## 6. 设计权衡与踩坑

### 6.1 seq 单调为何要锁

`seq = 当前最大值 + 1`。如果两个写者**同时**读到「当前最大值 = 5」，都算出 seq = 6，就重复了。

- **memory**：单进程单事件循环，`await` 之间不会被打断到另一个 put，内存计数器天然安全（无需锁）。
- **jsonl**：用每线程一个 `asyncio.Lock` 串行化写——同一个 thread 的并发 put 排队执行，绝不会两个同时读 max。
- **db**：写时 `SELECT max(seq) ... FOR UPDATE`（postgres 用 `pg_advisory_xact_lock`）。`FOR UPDATE` 给行加锁，让并发的「读 max」互斥。

### 6.2 SQLite 上 FOR UPDATE 是 no-op（重要踩坑）

SQLite **解析** `FOR UPDATE` 但**忽略**它（SQLite 没有行级锁）。所以在 sqlite db 后端上，**真正的并发同-thread 写仍可能撞 seq**——靠 `UNIQUE(thread_id, seq)` 约束兜底拒绝。

这意味着：**sqlite db 后端不适合同 thread 的并发写**。生产中这不是问题，因为：

- **RunJournal 用 `put_batch`**——在**单个事务**里读一次 max(seq)、批量分配 seq，整批原子完成，不存在并发读 max。
- **单条 `put` 是低频路径**——每 run 只调一次（写初始 human_message）。

真正的「并发同 thread 写」只在 postgres 上由 advisory lock 安全支持（多节点、多 worker 并发 flush）。sqlite 是单节点场景，journal 批量写，不触发并发。测试 `test_unique_constraint_backstops_duplicate_seq` 锁住「约束兜底」这个事实。

### 6.3 路径穿越防御

jsonl 后端把 `thread_id` / `run_id` 直接拼进文件路径。如果 `tid = "../../etc"`，路径就变成 `.deer-flow/threads/../etc/...` 逃出 base_dir，写到任意位置。

防御：`_SAFE_ID_PATTERN = ^[A-Za-z0-9_-]+$`（[jsonl.py:33](../backend/packages/harness/deerflow/runtime/events/store/jsonl.py#L33)）——只允许字母、数字、下划线、短横线。`/`、`.`、空格等全部拒绝。测试 `test_path_traversal_rejected` 用 `../escape`、`../../etc/passwd`、空串验证。

### 6.4 阻塞 IO 卸载

jsonl 的所有文件读写都是**阻塞 IO**。在 async 服务里直接调会卡住事件循环——这正是 mini 自己 blocking-IO gate 要拦的（详见 [#2 testing-setup.md](testing-setup.md)）。所以全部包成同步方法，经 `await asyncio.to_thread(...)` 丢到线程池。测试 `test_io_offloaded_via_to_thread` 锁住这个契约。

### 6.5 user_id stamp + UUID→str

db 后端写时从 contextvar 软读 user_id，stamp 到行的 `user_id` 列，用于用户隔离查询（§5.3）。后台 worker 写时 contextvar 未设 → stamp `None`（不加过滤）；HTTP 请求写时鉴权中间件已设 contextvar → 自动 stamp。

### 6.6 memory 投影：thread 级 + run 级

memory 后端维护多组投影（§5.1）让热路径读不必重扫全量：

- **thread 级 `_messages`**：`list_messages` 用 `bisect` 做 O(log m + page)——否则每次分页都要全扫所有事件（含大量 trace）。
- **run 级 `_events_by_run` / `_messages_by_run`**：前端还有两个**按单次 run** 读的端点（`list_events` / `list_messages_by_run`）。一个 thread 可能累积**成百上千个 run**的事件，但这两个端点每次只关心**其中一个 run**。没有 run 分桶投影时，它们每次都要扫一遍整个 thread 的事件日志——O(该 thread 的总事件数)，而该 run 可能只握着寥寥几条。分桶后降为 O(该 run 的事件数)。

> **为什么语义没变**：投影里存的是**原始 dict 对象本身**（不是拷贝），所以「按 run 过滤后再切片」和「先分桶再切片」产出完全一样的列表。这份等价性由一个 brute-force 等价性测试钉死（`test_events.py::TestRunEventStoreByRunIndex`）：它跑一组两个 run 交错的 trace（让每个 run 的 message seq 不连续，逼 `bisect` 处理间隙），然后对所有 `(run, limit, before_seq, after_seq)` 组合，断言索引实现 == 「全 thread 扫」参考实现的输出。优化再也不能悄悄偏离旧语义。

### 6.7 jsonl 单进程限制

jsonl 的 seq 计数器是**进程内**的。多个进程共用同一个目录会产生重复/非单调 seq。所以 jsonl 只适合**单节点**部署。多节点/多进程用 db 后端（seq 由数据库保证）。

---

## 7. 配置与用法

### 7.1 memory（默认）

```python
from deerflow.runtime.events import make_run_event_store

store = make_run_event_store(None)  # MemoryRunEventStore
rec = await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="你好")
msgs = await store.list_messages("t1")
```

### 7.2 jsonl（持久化到文件）

config.yaml：

```yaml
run_events:
  backend: jsonl
```

```python
from deerflow.runtime.events import make_run_event_store
from deerflow.config import get_app_config

store = make_run_event_store(get_app_config().run_events)
# 事件写到 .deer-flow/threads/{tid}/runs/{rid}.jsonl
```

### 7.3 db（生产）

config.yaml：

```yaml
database:
  backend: sqlite        # 或 postgres
run_events:
  backend: db
  max_trace_content: 10240
```

```python
# 需先 init_engine（lifespan 里做）
from deerflow.persistence import init_engine_from_config
from deerflow.config import get_app_config

await init_engine_from_config(get_app_config().database)
store = make_run_event_store(get_app_config().run_events)  # DbRunEventStore
```

### 7.4 RunJournal 会怎么用它（[#10 run_journal.md](run_journal.md) 预告）

`RunJournal`（LangChain 回调）把 LLM/工具调用攒成 buffer，达阈值后 `put_batch` 一次性写——单事务分配整批 seq，避开并发问题。

---

## 8. 与其它模块的关系

- **依赖**：[#7 persistence.md](persistence.md)（`RunEventRow` 表，本模块建表、event store 用）、`config/run_events_config`（`backend` / `max_trace_content`）、[#5 user_context.md](user_context.md)（三态 user_id）、[#4 utils.md](utils.md)（`coerce_iso`）。
- **被依赖**：
  - [#10 run_journal.md](run_journal.md)：RunJournal 是**写入侧**——LangChain 回调采集 LLM/工具调用 + 核算 token，攒成 buffer 后 `put_batch` 写入。
  - [#26 runs.md](runs.md)：worker 写初始 human_message 事件（每 run 一次，用 `put`）。
  - 消息/事件查询端点：`list_messages` / `list_events` 给前端展示。

---

## 9. 常见问题 / 排错

**Q: `UNIQUE constraint failed: run_events.thread_id, run_events.seq`？**
A: 同 thread 并发写撞 seq（sqlite 上 FOR UPDATE 是 no-op，§6.2）。生产中不应发生（RunJournal 用 `put_batch`）。如果你自己调 `put` 又并发了，改用 `put_batch`，或换 postgres 后端（advisory lock 真正串行化）。

**Q: `Invalid thread_id: must be alphanumeric/dash/underscore`？**
A: 路径穿越防御（§6.3）。`thread_id` / `run_id` 只允许 `[A-Za-z0-9_-]+`。检查是不是传了 `/`、`.`、空格，或 UUID 带了短横线以外的字符（UUID 的 `-` 是允许的）。

**Q: jsonl 模式下重启后 seq 重复了？**
A: jsonl 的 seq 计数器是进程内的，但**首次写会 lazy 扫描现有文件加载 max(seq)**（`_ensure_seq_loaded`）。如果你绕过 `put` 直接写文件，或多个进程共用目录，就会破坏单调性。jsonl 只支持单进程（§6.7）。

**Q: db 后端 trace 内容被截断了？**
A: 设计如此（§5.3）。`max_trace_content`（默认 10240 字节）以上的 trace 会被截断，metadata 标 `content_truncated=True`。调大 `run_events.max_trace_content` 或改存对象存储。

**Q: list_messages 拿到的 content 是 dict 不是字符串？**
A: 正常。写时若是 dict，db 后端会 JSON 序列化 + 标 `content_is_dict`，读回还原成 dict（§5.3）。这是「JSON 往返」设计，调用方拿到的始终是原始类型。

**Q: db 后端 user_id 为什么有时候是 None？**
A: 后台 worker 写时 contextvar 未设（没有 HTTP 请求上下文）→ stamp None（§6.5）。HTTP 请求写时有鉴权 → stamp 真实 user_id。查询时传 `user_id=None` 绕过过滤能看到全部（管理/迁移用）。

---

## 小结

RunEventStore 的精髓是**统一接口 + seq 单调 + 安全收口**。记住五件事：

1. **统一接口**：消息 / 轨迹 / 生命周期走同一接口，靠 `category` 区分谁看。
2. **seq 单调**：三后端各靠内存计数器 / 每线程锁 / `FOR UPDATE`(或 advisory lock) 保证；sqlite 上 `FOR UPDATE` 是 no-op，靠 `UNIQUE(thread_id, seq)` 兜底。
3. **双向游标分页**：用 seq 作稳定锚点（不用漂移的页码）。
4. **安全**：jsonl 路径穿越防御（`[A-Za-z0-9_-]+`）；db trace 按字节截断；UUID→str stamp。
5. **性能**：memory 4 组投影（thread 级 + run 级，同一对象无拷贝），热路径读 O(log + page)。

上一篇：[#8 checkpointer.md](checkpointer.md)（检查点工厂——图状态快照，与本模块的「事件流」互补）· 下一篇：[#10 run_journal.md](run_journal.md)（RunJournal——本模块的**写入侧**：LangChain 回调 → 事件采集 + token 核算 + put_batch 批量写）。
