# 9. run_event_store.md — 运行事件存储（消息 + 轨迹）

> 配套代码：[backend/packages/harness/deerflow/runtime/events/](../backend/packages/harness/deerflow/runtime/events/)
> 配套测试：[test/test_events.py](../test/test_events.py)
> 本文面向「刚接触事件流 / 分页 / 并发的小白」。每个名词第一次出现都会解释。

---

## 1. 一句话定位

**RunEventStore 是「一次 run 里发生了什么」的统一记录本——前端要展示的对话消息、供调试的执行轨迹、run 的生命周期事件，都走同一个接口，靠 `category` 字段区分。**

它和 [persistence.md](persistence.md) 的 `RunRow`（run 摘要）是配合关系：`RunRow` 存「这次 run 结果如何、token 用了多少」，`RunEventStore` 存「这次 run 一步步发生了什么」。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样：

- **前端拿不到历史消息**。用户刷新页面想看「这个线程之前的对话」，没有事件流存储就只能从 checkpointer 的图状态里硬抠，既慢又乱。
- **出问题没法回放**。run 跑挂了，想知道「第 3 步调了哪个工具、返回了什么」，没有轨迹记录就两眼一抹黑。
- **消息顺序错乱**。多个事件并发写入，没有单调序号，前端按到达顺序渲染就会跳跃。
- **路径被恶意构造**。用户传一个 `thread_id="../../etc"`，事件写到文件系统任意位置——越权写文件。

RunEventStore 解决这些：**统一接口存消息+轨迹**、**seq 单调保证顺序**、**路径穿越防御**、**三后端按需选**。

---

## 3. 核心概念（名词 + 类比）

### 3.1 event / category / seq

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

类比：thread 是一本书，run 是书里的一个章节，event 是章节里的一句话，`seq` 是这句话在全书的页码（全局连续），`category` 决定这句话是「正文」（message）还是「脚注」（trace）。

### 3.2 三个后端

| 后端 | 存哪 | 何时用 | seq 单调靠什么 |
|------|------|--------|---------------|
| **memory** | 进程内存 | 开发、测试；重启即失 | 内存计数器（单进程天然安全） |
| **jsonl** | 每 run 一个 `.jsonl` 文件 | 单节点轻量持久化 | 内存计数器 + 每线程 `asyncio.Lock` |
| **db** | `run_events` 表（SQLAlchemy） | 生产、多节点、要查询 | `SELECT max(seq) FOR UPDATE`（postgres 用 advisory lock） |

### 3.3 双向游标分页（cursor pagination）

消息可能很多，前端不能一次全拉。`list_messages` 支持：

- **`after_seq`**：返回 seq 大于它的前 `limit` 条（**向后翻页**，看更新的）。
- **`before_seq`**：返回 seq 小于它的最后 `limit` 条（**向前翻页**，看更旧的）。
- 都不给：返回最近 `limit` 条。

为什么用 seq 作游标而不是「第几页」？因为消息在不断追加，「页码」会随新消息漂移（翻到第 2 页时新消息进来了，内容就错了）。seq 是稳定锚点——「seq > 100 的前 50 条」永远精确。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 seq 单调为何要锁（红线 #3）

`seq = 当前最大值 + 1`。如果两个写者**同时**读到「当前最大值 = 5」，都算出 seq = 6，就重复了。

- **memory**：单进程单事件循环，`await` 之间不会被打断到另一个 put，内存计数器天然安全（无需锁）。
- **jsonl**：用每线程一个 `asyncio.Lock` 串行化写——同一个 thread 的并发 put 排队执行，绝不会两个同时读 max。
- **db**：写时 `SELECT max(seq) ... FOR UPDATE`（postgres 用 `pg_advisory_xact_lock`）。`FOR UPDATE` 给行加锁，让并发的「读 max」互斥。

### 4.2 sqlite 上 FOR UPDATE 是 no-op（重要踩坑）

SQLite **解析** `FOR UPDATE` 但**忽略**它（SQLite 没有行级锁）。所以在 sqlite db 后端上，**真正的并发同-thread 写仍可能撞 seq**——靠 `UNIQUE(thread_id, seq)` 约束兜底拒绝。

这意味着：**sqlite db 后端不适合同 thread 的并发写**。生产中这不是问题，因为：
- **RunJournal 用 `put_batch`**——在**单个事务**里读一次 max(seq)、批量分配 seq，整批原子完成，不存在并发读 max。
- **单条 `put` 是低频路径**——每 run 只调一次（写初始 human_message）。

真正的「并发同 thread 写」只在 postgres 上由 advisory lock 安全支持（多节点、多 worker 并发 flush）。sqlite 是单节点场景，journal 批量写，不触发并发。

测试 `test_unique_constraint_backstops_duplicate_seq` 锁住「约束兜底」这个事实。

### 4.3 路径穿越防御（红线 #4）

jsonl 后端把 `thread_id` / `run_id` 直接拼进文件路径 `.deer-flow/threads/{tid}/runs/{rid}.jsonl`。如果 `tid = "../../etc"`，路径就变成 `.deer-flow/threads/../etc/...` 逃出 base_dir，写到任意位置。

防御：`_SAFE_ID_PATTERN = ^[A-Za-z0-9_-]+$`——只允许字母、数字、下划线、短横线。`/`、`.`、空格等全部拒绝。校验失败抛 `ValueError`。

测试 `test_path_traversal_rejected` 用 `../escape`、`../../etc/passwd`、空串验证。

### 4.4 阻塞 IO 卸载（红线 #1）

jsonl 的所有文件读写（`open`/`read_text`/`glob`/`unlink`）都是**阻塞 IO**。在 async 服务里直接调会卡事件循环。所以全部包成 `_write_record` / `_read_thread_events` 等同步方法，经 `await asyncio.to_thread(...)` 丢到线程池。

测试 `test_io_offloaded_via_to_thread` 锁住这个契约。

### 4.5 trace 截断（db 后端）

`trace` 事件可能很大（比如一次工具调用返回几 MB 的输出）。直接塞进数据库会撑爆。db 后端在写入前对 `category="trace"` 的内容按 `max_trace_content` 字节截断，并在 metadata 标记 `content_truncated=True` + `original_byte_length`。

截断按**字节**做（不是字符），decode 回来可能切断多字节字符，故用 `errors="ignore"`。message 不截断（用户要看完整内容）。

### 4.6 JSON content 往返（db 后端）

`content` 可能是结构化 dict（如 `{"text": "...", "role": "ai"}`）。但 db 的 `content` 列是 `Text`（字符串）。所以写时把 dict `json.dumps` 成字符串存，并在 metadata 标记 `content_is_json` / `content_is_dict`；读回时检测标记，`json.loads` 还原成 dict。这样调用方拿到的始终是原始类型。

### 4.7 user_id stamp + UUID→str（红线 #10）

db 后端写时从 contextvar 软读 user_id（`_user_id_from_context`），stamp 到行的 `user_id` 列，用于用户隔离查询。在边界处 `str(user.id)`——因为 `User.id` 可能是 `UUID`，而列是 `VARCHAR(64)`，aiosqlite 无法绑 UUID（详见 [persistence.md](persistence.md) §4.3 / [user_context.md](user_context.md)）。

后台 worker 写时 contextvar 未设 → stamp `None`（不加过滤）。HTTP 请求写时鉴权中间件已设 contextvar → 自动 stamp。

### 4.8 message 投影（memory 后端优化）

memory 后端除了 `_events`（全量），还维护 `_messages`（仅 message 的投影，同 dict 对象、按 seq 排序）。这样 `list_messages` 分页用 `bisect` 做 O(log m + page)——否则每次分页都要全扫所有事件（含大量 trace）。jsonl/db 后端靠查询条件过滤，不需要这个投影。

### 4.9 jsonl 单进程限制

jsonl 的 seq 计数器是**进程内**的。多个进程共用同一个目录会产生重复/非单调 seq。所以 jsonl 只适合**单节点**部署。多节点/多进程用 db 后端（seq 由数据库保证）。

---

## 5. 文件结构

```
runtime/events/
├── __init__.py              # make_run_event_store 工厂（按 run_events.backend 选实现）
└── store/
    ├── __init__.py          # 导出 RunEventStore + MemoryRunEventStore
    ├── base.py              # RunEventStore ABC（8 方法）
    ├── memory.py            # MemoryRunEventStore（+ message 投影优化）
    ├── jsonl.py             # JsonlRunEventStore（路径穿越防御 + 每线程锁 + IO 卸载）
    └── db.py                # DbRunEventStore（FOR UPDATE/advisory + trace 截断 + JSON 往返 + UUID→str）
```

依赖：[persistence/models/run_event.py](../backend/packages/harness/deerflow/persistence/models/run_event.py)（M4 的 `RunEventRow`）、[config/run_events_config.py](../backend/packages/harness/deerflow/config/run_events_config.py)（`backend` / `max_trace_content`）、[runtime/user_context](../backend/packages/harness/deerflow/runtime/user_context.py)（三态 user_id）、[utils/time](../backend/packages/harness/deerflow/utils/time.py)（`coerce_iso`）。

---

## 6. 关键接口 / 签名

### RunEventStore ABC（8 方法）

```python
class RunEventStore(abc.ABC):
    async def put(*, thread_id, run_id, event_type, category, content="", metadata=None, created_at=None) -> dict
    async def put_batch(events: list[dict]) -> list[dict]            # 高频路径（RunJournal flush）
    async def list_messages(thread_id, *, limit=50, before_seq=None, after_seq=None) -> list[dict]
    async def list_events(thread_id, run_id, *, event_types=None, limit=500) -> list[dict]
    async def list_messages_by_run(thread_id, run_id, *, limit=50, before_seq=None, after_seq=None) -> list[dict]
    async def count_messages(thread_id) -> int
    async def delete_by_thread(thread_id) -> int                     # 返回删除条数
    async def delete_by_run(thread_id, run_id) -> int
```

### 工厂

```python
make_run_event_store(config: RunEventsConfig | None) -> RunEventStore
#   memory → MemoryRunEventStore；db → DbRunEventStore（engine 未就绪回退 memory）；
#   jsonl → JsonlRunEventStore
```

---

## 7. 应用方法（可跑 demo）

### 7.1 memory（默认）

```python
from deerflow.runtime.events import make_run_event_store

store = make_run_event_store(None)  # MemoryRunEventStore
rec = await store.put(thread_id="t1", run_id="r1", event_type="ai_message", category="message", content="你好")
msgs = await store.list_messages("t1")
```

### 7.2 jsonl（持久化到文件）

config.yaml:

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

config.yaml:

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

### 7.4 RunJournal 会怎么用它（M7 预告）

未来的 `RunJournal`（LangChain 回调）把 LLM/工具调用攒成 buffer，达阈值后 `put_batch` 一次性写——单事务分配整批 seq，避开并发问题。

---

## 8. 与其它模块的关系（文字依赖图）

```
config/run_events_config ─┐
persistence/models/run_event (M4 RunEventRow) ─┤
runtime/user_context (三态 user_id) ───────────┤
utils/time (coerce_iso) ───────────────────────┤
                                                ▼
                          runtime/events/store/{base,memory,jsonl,db}
                                                │
                                  make_run_event_store(config)
                                                │
                （未来）runtime/journal.RunJournal ──put_batch──→ store
                                                │
                （未来）Gateway messages/events 端点 ──list_messages──→ store
```

- **被谁依赖**：未来的 RunJournal（写入侧）、runs worker、消息/事件查询端点。
- **与 checkpointer 的区别**：checkpointer 存「图状态快照」（可恢复执行）；event store 存「事件流」（可回放/展示）。前者是「机器读」的，后者是「人读」的。
- **与 RunRow 的区别**：RunRow 是 run 的「摘要卡」（状态、token、首末消息）；event store 是 run 的「流水账」（每一步）。

---

## 9. 常见问题 / 排错

**Q: `UNIQUE constraint failed: run_events.thread_id, run_events.seq`？**
A: 同 thread 并发写撞 seq（sqlite 上 FOR UPDATE 是 no-op）。生产中不应发生（RunJournal 用 put_batch）。如果你自己调 `put` 又并发了，改用 `put_batch`，或换 postgres 后端（advisory lock 真正串行化）。

**Q: `Invalid thread_id: must be alphanumeric/dash/underscore`？**
A: 红线 #4 路径穿越防御。`thread_id` / `run_id` 只允许 `[A-Za-z0-9_-]+`。检查是不是传了 `/`、`.`、空格，或 UUID 带了短横线以外的字符（UUID 的 `-` 是允许的）。

**Q: jsonl 模式下重启后 seq 重复了？**
A: jsonl 的 seq 计数器是进程内的，但**首次写会 lazy 扫描现有文件加载 max(seq)**（`_ensure_seq_loaded`）。如果你绕过 `put` 直接写文件，或多个进程共用目录，就会破坏单调性。jsonl 只支持单进程。

**Q: db 后端 trace 内容被截断了？**
A: 红线设计。`max_trace_content`（默认 10240 字节）以上的 trace 会被截断，metadata 标 `content_truncated=True`。调大 `run_events.max_trace_content` 或改存对象存储。

**Q: list_messages 拿到的 content 是 dict 不是字符串？**
A: 正常。写时若是 dict，db 后端会 JSON 序列化 + 标 `content_is_dict`，读回还原成 dict。这是「JSON 往返」设计，调用方拿到的始终是原始类型。

**Q: db 后端 user_id 为什么有时候是 None？**
A: 后台 worker 写时 contextvar 未设（没有 HTTP 请求上下文）→ stamp None。HTTP 请求写时有鉴权 → stamp 真实 user_id。查询时传 `user_id=None` 绕过过滤能看到全部（管理/迁移用）。

---

> 红线索引：#1（阻塞 IO 卸载）、#3（seq 单调）、#4（路径穿越防御）、#10（UUID→str stamp）。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
