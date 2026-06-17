# 7. persistence.md — 应用持久化层（SQLAlchemy ORM）

> 配套代码：[backend/packages/harness/deerflow/persistence/](../backend/packages/harness/deerflow/persistence/)
> 配套测试：[test/test_persistence.py](../test/test_persistence.py)
> 本文面向「刚接触数据库 / ORM 的小白」。每个名词第一次出现都会解释。

---

## 1. 一句话定位

**persistence 模块负责把「谁在哪个线程跑过哪次对话、结果如何」这类应用元数据，可靠地存进数据库（SQLite / PostgreSQL），并提供内存降级。**

它是「**谁跑过什么**」的账本，**不是**「对话内容本身」的仓库（对话内容由 LangGraph 的 checkpointer 管，两者物理分离）。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样：

- **进程一重启，历史全没了**。你想做一个「列出这个线程的所有 run」的页面，但内存里的字典一关进程就空了。
- **多用户混在一起**。Alice 和 Bob 各自的线程 / run，如果存的时候不带「属于谁」，查询时无法区分——Bob 能看到 Alice 的对话历史（**越权**）。
- **并发写崩溃**。两个 worker 同时往 SQLite 写，默认的 rollback journal 模式会让写者互相阻塞、甚至 `database is locked` 报错。
- **断电 / 崩溃后状态错乱**。一次 run 跑到一半进程挂了，重启后它还显示「运行中」（僵尸 run），没人去恢复。

persistence 模块就是为了解决这些：**用 ORM 把数据落盘**、**带 user_id 做隔离**、**用 WAL 让并发不阻塞**、**配合（未来的）RunManager 做僵尸恢复**。

---

## 3. 核心概念（名词 + 类比）

### 3.1 ORM / SQLAlchemy / DeclarativeBase

- **数据库（database）**：把数据按「表（table）」存成文件的程序（SQLite 是一个文件，PostgreSQL 是一个服务）。
- **SQL**：操作数据库的语言（`INSERT`/`SELECT`/`UPDATE`/`DELETE`）。
- **ORM（Object-Relational Mapping，对象关系映射）**：让你**用 Python 类代替 SQL** 来操作数据库。你定义一个 `RunRow` 类，ORM 帮你把它翻译成 `runs` 表，并自动生成 SQL。
- **SQLAlchemy**：Python 最成熟的 ORM 库。DeerFlow 用它的 **2.0 async** 版本（异步，配合 asyncio）。
- **DeclarativeBase（声明式基类）**：SQLAlchemy 的约定——所有「映射到数据库表的类」都继承一个 `Base`。本模块的 `Base` 在 [base.py](../backend/packages/harness/deerflow/persistence/base.py)。

类比：ORM 像「自动挡汽车」——你不用懂离合换挡（手写 SQL），踩油门（调用 `session.add(obj)`）它自己帮你换。代价是多一层抽象、性能略低，但换来**类型安全 + 可读性 + 跨数据库移植**。

### 3.2 engine / session / session_factory

- **engine（引擎）**：与数据库的「连接池入口」。一个 engine 持有一组数据库连接。
- **session（会话）**：一次「工作单元」。你在一个 session 里改对象，最后 `commit()`（提交）才真正写库。类比：session 是「购物车」，commit 是「结账付款」。
- **session_factory（会话工厂）**：生产 session 的工厂函数。每个仓储方法各自要一个短命 session，用完即关。

### 3.3 run / thread / run_event / thread_meta

这是 DeerFlow 的领域词汇：

| 名词 | 是什么 | 谁管 |
|------|--------|------|
| **thread（线程/会话）** | 一次连续对话（同一个聊天窗口） | LangGraph checkpointer + 本模块的 `thread_meta` |
| **run（运行）** | thread 里**一次**「发消息→AI 回复」的完整执行 | 本模块的 `RunRow` / `RunRepository` |
| **run_event（运行事件）** | run 内的一个时间点记录（一条消息 / 一段轨迹 / 一个生命周期事件） | 本模块建表（`RunEventRow`），存储实现在 M6 |
| **thread_meta（线程元数据）** | thread 的「属性卡」：标题、状态、属主、自定义 metadata | 本模块的 `ThreadMetaRow` / `ThreadMetaRepository` |

一句话：**thread 是一条河，run 是河里一次水流，run_event 是水流里的水滴，thread_meta 是河边的标牌。**

### 3.4 三种存储后端（backend）

| backend | 存哪 | 用途 | 本模块行为 |
|---------|------|------|-----------|
| **memory** | 内存（进程字典 / LangGraph Store） | 开发、测试；重启即失 | `init_engine` 是 **no-op**（什么都不做），`get_session_factory()` 返回 `None` |
| **sqlite** | 一个本地文件（`.db`） | 单节点部署、本地开发持久化 | 创建 engine，每连接开 WAL |
| **postgres** | 一个 PostgreSQL 服务 | 生产、多节点 | 创建 engine + 连接池；需 `asyncpg` 驱动（可选 extra） |

### 3.5 WAL（Write-Ahead Logging）

SQLite 默认用 **rollback journal**（回滚日志）模式：写之前先把原数据复制到日志，写完删日志。这种模式下，**写的时候别人不能读、读的时候别人不能写**——并发很差。

**WAL 模式**：写时不改原文件，而是把改动**追加**到一个 `-wal` 伴随文件；读时把 wal 里的最新改动叠加到原文件读出。于是：**多个读者 + 一个写者可以同时进行，互不阻塞**。这是任何生产 SQLite 部署的标准建议（红线 **#2**）。

配套的 `synchronous=NORMAL`：不在每次提交都 fsync（强制刷盘），而是在 WAL checkpoint（合并点）边界才 fsync——**安全（断电不丢已提交事务）且快**。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 app 表与 checkpointer 表物理分离（重要）

LangGraph 的 checkpointer 也用 SQLAlchemy 存图执行状态（节点输出、消息历史）。**但 checkpointer 的表不归我们的 `Base` 管**——它用自己的元数据。

即使 sqlite 模式下两者共用**同一个 `.db` 文件**，表也互不重叠：

```
deerflow.db
├── runs              ← 本模块 Base（app 元数据）
├── threads_meta      ← 本模块 Base
├── run_events        ← 本模块 Base
└── checkpoint_*      ← LangGraph checkpointer 自己的表（不归本 Base）
```

为什么分离？**生命周期和关注点不同**：checkpointer 管「图跑到哪」，本模块管「谁跑的、结果摘要、token 用了多少」。混在一起会让 schema 演进互相牵制。

### 4.2 memory 模式是 no-op，不是「内存引擎」

`init_engine("memory")` 直接 return，**根本不创建 engine**。`get_session_factory()` 返回 `None`。

这意味着：**所有用到持久化的代码都必须先检查 `None`，并回退到内存实现**。例如（未来的 lifespan）：

```python
sf = get_session_factory()
run_store = RunRepository(sf) if sf else MemoryRunStore()   # None → 内存
thread_store = make_thread_store(sf, store=langgraph_store)  # 工厂内部处理 None
```

这是红线 **#25**（空配置必须能以 memory 模式启动）的体现。

### 4.3 三态 user_id + UUID→str 边界（红线 #10）

仓储方法的 `user_id` 形参有三种取值（详见 [user_context.md](user_context.md)）：

- `AUTO`（默认）：从当前请求的 contextvar 解析。
- 显式 `str`：用给定值。
- 显式 `None`：**绕过属主过滤**（仅迁移 / CLI）。

**踩坑：UUID→str 边界**。用户的 `id` 在类型上可能是 `uuid.UUID` 对象，但数据库列是 `VARCHAR(64)`（字符串）。aiosqlite 驱动**无法把原生 UUID 绑定到 VARCHAR 列**（会报 "type 'UUID' is not supported"）。所以 `resolve_user_id` 在从 contextvar 读取时强制 `str()`：

```python
return str(user.id)   # 边界处转 str，不把类型变更扩散到每个调用方
```

测试见 `test_uuid_user_id_stored_as_str`。

### 4.4 rowcount 驱动 recovery（红线 #12）

`update_status` / `update_run_completion` 返回 `bool`：

- `True`：确实更新了行。
- `False`：**能证明没有行被更新**（rowcount == 0）。

为什么需要这个返回值？未来的 RunManager 在某些恢复路径里需要知道「这个 run 还在库里吗」。如果 `update_status` 返回 `False`，说明库里没这行了，RunManager 要从**内存快照重建**一行（红线 #12）。轻量 / 旧版实现可以返回 `None`（无法报告 rowcount），调用方据此区分「确定没了」vs「不确定」。

### 4.5 幂等 put（防重试变主键冲突）

`RunRepository.put` 是「insert or update」：先 `session.get`，有就改、没有就插。这是因为 RunManager 在遇到瞬时 SQLite 失败（如 `database is locked`）后会**重试 put**。如果 put 不是幂等的，一次「成功但未确认的提交」会让重试变成主键冲突。

### 4.6 SQLite 时间戳丢时区 → coerce_iso 归一

SQLite 声明了 `DateTime(timezone=True)`，但**读回来时 tzinfo 会丢失**（naive datetime）。所以 `_row_to_dict` 用 `coerce_iso`（见 [utils.md](utils.md)）把 naive datetime 当作 UTC 归一成 ISO 字符串，保证线格式始终带时区。否则前端会拿到 `2026-06-17 10:00:00`（没 `T`、没时区），解析出错。

### 4.7 json_compat：跨方言的 JSON 过滤

线程 metadata 是个 JSON 列。要支持「找所有 `metadata.team == 'x'` 的线程」，但 SQLite（`json_extract`）和 PostgreSQL（`->>`）语法完全不同，且要区分 `bool` vs `int`、`NULL` vs 缺键。

[json_compat.py](../backend/packages/harness/deerflow/persistence/json_compat.py) 用 SQLAlchemy 的 `@compiles` 机制为每种方言编译出类型安全的谓词，并**把 key 限制为 `[A-Za-z0-9_-]+`**——因为 key 会被插值进 SQL，放宽字符集会打开 SQL 注入面。当所有 key 都不安全时，`search` 抛 `InvalidMetadataFilterError`（返回 400 给客户端）。

### 4.8 runs 基类为什么提前到 Phase 1

`RunRepository` 继承 `RunStore`（ABC）。但 `RunStore` 属于 runs 领域，而 runs 的**运行管理层**（RunManager / worker）在 Phase 8，又依赖持久化。如果把 ABC 留到 Phase 8，会形成「持久化 → 运行管理 → 持久化」的**循环依赖**。

解法：把纯数据的 `RunStore` ABC + 状态枚举提前到 Phase 1（本模块），运行管理留 Phase 8。于是 `RunRepository(RunStore)` 可以先于 `RunManager` 存在，循环被打破。

---

## 5. 文件结构

```
persistence/
├── __init__.py            # 导出 init_engine / close_engine / get_* 
├── base.py                # Base(DeclarativeBase) + to_dict() / __repr__()
├── engine.py              # 引擎生命周期：init / close / session factory；WAL；auto-create
├── json_compat.py         # 跨方言 JSON 过滤（json_match / JsonMatch）
├── models/
│   ├── __init__.py        # 模型注册入口（导入即注册到 Base.metadata）
│   └── run_event.py       # RunEventRow（run 事件表）
├── run/
│   ├── __init__.py        # 导出 RunRepository / RunRow
│   ├── model.py           # RunRow（run 元数据表）
│   └── sql.py             # RunRepository(RunStore) —— SQL 实现
└── thread_meta/
    ├── __init__.py        # 导出 + make_thread_store 工厂
    ├── base.py            # ThreadMetaStore ABC + InvalidMetadataFilterError
    ├── model.py           # ThreadMetaRow（线程元数据表）
    ├── memory.py          # MemoryThreadMetaStore（包一层 LangGraph BaseStore）
    └── sql.py             # ThreadMetaRepository —— SQL 实现
```

旁落的 runs 基类层（提前到 Phase 1）：

```
runtime/runs/
├── __init__.py            # 导出 RunStatus / DisconnectMode / RunStore
├── schemas.py             # RunStatus / DisconnectMode 枚举
└── store/
    ├── __init__.py        # 导出 RunStore（Phase 8 再加 MemoryRunStore）
    └── base.py            # RunStore ABC（run 元数据存储契约）
```

**裁剪说明（本期不做）**：deer 还有 `feedback/`（反馈）、`user/`（用户表）、`channel_connections/`（IM 平台连接）、`migrations/`（Alembic 数据库迁移）。mini 本期都不实现，需要时再补。

---

## 6. 关键接口 / 签名

### engine 生命周期

```python
await init_engine(backend: str, *, url="", echo=False, pool_size=5, sqlite_dir="") -> None
await init_engine_from_config(config: DatabaseConfig) -> None   # 便利：从配置初始化
get_session_factory() -> async_sessionmaker | None              # memory → None
get_engine() -> AsyncEngine | None
await close_engine() -> None                                    # dispose + 重置全局
```

### RunStore 契约（ABC，[runtime/runs/store/base.py](../backend/packages/harness/deerflow/runtime/runs/store/base.py)）

> 注：ABC 声明 `user_id: str | None = None`；`RunRepository` 实现默认 `AUTO`，经 `resolve_user_id` 解析（三态语义见 [user_context.md](user_context.md)）。下为示意签名。

```python
class RunStore(abc.ABC):
    async def put(run_id, *, thread_id, user_id=AUTO, status="pending", ...) -> None
    async def get(run_id, *, user_id=AUTO) -> dict | None
    async def list_by_thread(thread_id, *, user_id=AUTO, limit=100) -> list[dict]
    async def update_status(run_id, status, *, error=None) -> bool | None   # #12 rowcount
    async def update_run_completion(run_id, *, status, total_tokens=0, ...) -> bool | None
    async def update_run_progress(run_id, *, total_tokens=None, ...) -> None  # 非抽象，默认 no-op
    async def delete(run_id, *, user_id=AUTO) -> None
    async def update_model_name(run_id, model_name) -> None
    async def list_pending(*, before=None) -> list[dict]
    async def list_inflight(*, before=None) -> list[dict]   # pending + running
    async def aggregate_tokens_by_thread(thread_id, *, include_active=False) -> dict
```

### ThreadMetaStore 契约（ABC）

```python
class ThreadMetaStore(abc.ABC):
    async def create(thread_id, *, user_id=AUTO, display_name=None, metadata=None) -> dict
    async def get(thread_id, *, user_id=AUTO) -> dict | None
    async def search(*, metadata=None, status=None, limit=100, offset=0, user_id=AUTO) -> list[dict]
    async def update_display_name / update_status / update_metadata / update_owner / delete
    async def check_access(thread_id, user_id, *, require_existing=False) -> bool
```

### 工厂

```python
make_thread_store(session_factory, store=None) -> ThreadMetaStore
#   有 sf → ThreadMetaRepository；无 sf 有 store → MemoryThreadMetaStore；都没有 → 抛错
```

---

## 7. 应用方法（可跑 demo）

### 7.1 memory 模式（什么都不配，默认）

```python
import asyncio
from deerflow.persistence import init_engine, get_session_factory

async def main():
    await init_engine("memory")          # no-op
    print(get_session_factory())         # None → 调用方回退内存实现

asyncio.run(main())
```

### 7.2 sqlite 模式（本地持久化）

```python
import asyncio
from deerflow.persistence import init_engine, get_session_factory, close_engine
from deerflow.persistence.run import RunRepository
from deerflow.runtime.user_context import set_current_user, reset_current_user
from types import SimpleNamespace

async def main():
    await init_engine("sqlite", url="sqlite+aiosqlite:///./deerflow.db", sqlite_dir=".")
    sf = get_session_factory()
    repo = RunRepository(sf)

    token = set_current_user(SimpleNamespace(id="alice"))
    try:
        await repo.put("run-001", thread_id="thread-1", user_id="alice", status="pending")
        got = await repo.get("run-001", user_id="alice")
        print(got["status"])             # pending
    finally:
        reset_current_user(token)

    await close_engine()

asyncio.run(main())
```

### 7.3 通过配置初始化（生产用法）

```python
from deerflow.config import get_app_config
from deerflow.persistence import init_engine_from_config

cfg = get_app_config()
await init_engine_from_config(cfg.database)
# database.backend 由 config.yaml 决定；memory → no-op，sqlite/postgres → 建 engine
```

### 7.4 对应 config.yaml

```yaml
database:
  backend: sqlite              # memory | sqlite | postgres
  sqlite_dir: .deer-flow/data  # sqlite 文件目录
  # postgres_url: $DATABASE_URL  # postgres 时用，$VAR 从 .env 读
```

---

## 8. 与其它模块的关系（文字依赖图）

```
config/database_config ──→ engine.init_engine_from_config
                              │
utils/time (coerce_iso/now_iso)│
                              ▼
runtime/user_context (AUTO/   persistence/
  resolve_user_id)      ┌─→  ├─ base (Base)
                        │     ├─ engine (生命周期 + WAL)
                        │     ├─ json_compat (JSON 过滤)
                        │     ├─ models (RunEventRow / RunRow / ThreadMetaRow)
                        │     ├─ run/sql.py: RunRepository ──inherits──→ runtime/runs/store/base.py (RunStore)
                        │     └─ thread_meta/{base,memory,sql}.py
                        │                              │
                        │              memory.py ←── langgraph.store.base.BaseStore
                        │
                        ▼
            （未来）runtime/runs/manager.py + worker.py（Phase 8）
                        │
            （未来）runtime/events/store/db.py（M6，用 RunEventRow）
                        │
            （未来）lifespan 集成装配（Phase 8）
```

- **被谁依赖**：未来的 RunManager（Phase 8）、DbRunEventStore（M6）、lifespan。
- **依赖谁**：config（DatabaseConfig）、utils/time、runtime/user_context、runtime/runs 的 ABC、（memory 模式）langgraph BaseStore。

---

## 9. 常见问题 / 排错

**Q: `get_session_factory()` 返回 `None`？**
A: 当前 backend 是 `memory`（默认）。memory 模式不创建 engine。要么改 `config.yaml` 的 `database.backend`，要么调用方检查 `None` 并回退内存实现。

**Q: 报 `database is locked` / `no such table`？**
A: 
- `database is locked`：理论上 WAL + 5 秒 busy 超时已大幅缓解；若仍出现，说明写并发极高，考虑上 postgres。
- `no such table`：`init_engine` 会 `create_all` 自动建表。如果跳过了 `init_engine` 直接用 session_factory，表就不存在。

**Q: 报 `type 'UUID' is not supported`？**
A: 你把 `uuid.UUID` 对象直接传给了 `user_id`。应该传 `str(uuid)`，或经 `resolve_user_id(AUTO)`（它会在边界自动 `str()`）。

**Q: 读回来的时间戳没有时区 / 没有 `T`？**
A: SQLite 会丢 tzinfo。所有出库的时间戳都经 `coerce_iso` 归一成带时区的 ISO 字符串。如果你绕过了 `_row_to_dict` 直接读 ORM 字段，会拿到 naive datetime——请走 `_row_to_dict`。

**Q: postgres 报 `asyncpg is not installed`？**
A: postgres 驱动是可选依赖。安装：`cd backend && uv sync --all-packages --extra postgres`。

**Q: 为什么 `to_dict()` 拿到的 `status` 是 `None`（我刚 new 的对象）？**
A: SQLAlchemy 的列 `default` 只在 **flush / commit** 时生效，不在对象构造时。瞬态（未入库）对象上 `status` 是 `None`。flush 后才会是 `"pending"`。

**Q: 改了表结构（加列），怎么迁移？**
A: 本期没有 Alembic（数据库迁移工具）。开发期直接删 `.db` 文件重启（`create_all` 会按新 schema 重建）。生产迁移工具在后续 Phase 引入。

---

> 红线索引：#2（WAL + busy 重试）、#10（UUID→str 边界）、#12（rowcount 驱动 recovery）、#24（缺包可操作提示）、#25（空配置 memory 启动）。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
