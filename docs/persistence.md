# 7. persistence.md — 应用持久化层（SQLAlchemy ORM + WAL 并发）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：persistence 模块负责把「**谁**在哪个**线程**跑过哪次**对话（run）**、结果如何、token 花了多少」这类**应用元数据**，可靠地存进数据库（SQLite / PostgreSQL），并提供内存降级。它是「谁跑过什么」的**账本**，**不是**对话内容本身的仓库（对话内容由 LangGraph checkpointer 管，两者物理分离）。

> 配套代码：[persistence/](../backend/packages/harness/deerflow/persistence/)（14 个文件）——[base.py](../backend/packages/harness/deerflow/persistence/base.py) · [engine.py](../backend/packages/harness/deerflow/persistence/engine.py) · [json_compat.py](../backend/packages/harness/deerflow/persistence/json_compat.py) · [run/model.py](../backend/packages/harness/deerflow/persistence/run/model.py) · [run/sql.py](../backend/packages/harness/deerflow/persistence/run/sql.py) · [thread_meta/](../backend/packages/harness/deerflow/persistence/thread_meta/) · [models/run_event.py](../backend/packages/harness/deerflow/persistence/models/run_event.py)。测试见 [test/test_persistence.py](../test/test_persistence.py)。

## 学完这篇你能回答什么（learning outcomes）

- 为什么 agent 应用要把「应用元数据（谁跑的、结果摘要、token 用量）」和「对话内容本身（checkpointer 存的消息历史）」**物理分库 / 分表**？
- **SQLite 为什么要开 WAL**（而不是默认 rollback journal）？配套的 `synchronous=NORMAL` 和 `busy_timeout=30000` 各解决什么（并发不阻塞 / 断电不丢 / 锁竞争等待窗口）？
- `database.backend="memory"` 时 `init_engine` 为什么是 **no-op**、`get_session_factory()` 为什么返回 `None`？调用方据此怎么写（空配置必须能启动）？
- 仓储方法为什么对 `user_id` 做三态（AUTO/str/None）？**UUID 对象直接塞进 `VARCHAR` 列**会发生什么（aiosqlite 不支持）？
- `update_status` / `update_run_completion` 为什么返回 `bool`（rowcount 驱动 recovery）？`put` 为什么必须是幂等的（重试安全）？
- 一个 run 可能**路由到多个模型**——为什么平铺的 token 列不够，要加一列 `token_usage_by_model`（JSON 按真计费模型分桶）？聚合时老行怎么回退？
- 跨方言（SQLite/PostgreSQL）的 **JSON 列过滤**为什么不能直接 `column["key"] == value`？key 为什么限制为 `[A-Za-z0-9_-]+`（防 SQL/JSONPath 注入）？

> 这些都是后端 / 数据库工程面试的高频点——「并发控制（WAL）」「ORM 设计」「多租户隔离」「幂等与重试」「跨方言兼容」。

---

## 1. 为什么需要它（痛点）

先看「没有它」会怎样：

- **进程一重启，历史全没了**。你想做一个「列出这个线程的所有 run」的页面，但内存里的字典一关进程就空了。
- **多用户混在一起**。Alice 和 Bob 各自的线程 / run，如果存的时候不带「属于谁」，查询时无法区分——Bob 能看到 Alice 的对话历史（**越权**）。
- **并发写崩溃**。两个 worker 同时往 SQLite 写，默认的 rollback journal 模式会让写者互相阻塞、甚至 `database is locked` 报错。
- **断电 / 崩溃后状态错乱**。一次 run 跑到一半进程挂了，重启后它还显示「运行中」（僵尸 run），没人去恢复。

persistence 模块就是为了解决这些：**用 ORM 把数据落盘**、**带 user_id 做隔离**、**用 WAL 让并发不阻塞**、**配合 RunManager 做僵尸恢复**。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉数据库 / ORM 的话，先读这一节。

### 数据库 / SQL / 表

**数据库（database）**：把数据按「表（table）」存成文件的程序。SQLite 是一个**文件**（一个 `.db`），PostgreSQL 是一个**服务**（要单独跑起来连上去）。**SQL** 是操作数据库的语言（`INSERT`/`SELECT`/`UPDATE`/`DELETE`）。

### ORM / SQLAlchemy / DeclarativeBase

- **ORM（Object-Relational Mapping，对象关系映射）**：让你**用 Python 类代替 SQL** 来操作数据库。你定义一个 `RunRow` 类，ORM 帮你把它翻译成 `runs` 表，并自动生成 SQL。类比 ORM 像「自动挡汽车」——你不用懂离合换挡（手写 SQL），踩油门（`session.add(obj)`）它自己帮你换。代价是多一层抽象、性能略低，但换来**类型安全 + 可读性 + 跨数据库移植**。
- **SQLAlchemy**：Python 最成熟的 ORM 库。DeerFlow 用它的 **2.0 async** 版本（异步，配合 asyncio）。
- **DeclarativeBase（声明式基类）**：SQLAlchemy 的约定——所有「映射到数据库表的类」都继承一个 `Base`。本模块的 `Base` 在 [base.py](../backend/packages/harness/deerflow/persistence/base.py)。

### engine / session / session_factory

- **engine（引擎）**：与数据库的「连接池入口」。一个 engine 持有一组数据库连接。
- **session（会话）**：一次「工作单元」。你在一个 session 里改对象，最后 `commit()`（提交）才真正写库。类比：session 是「购物车」，commit 是「结账付款」。
- **session_factory（会话工厂）**：生产 session 的工厂函数。每个仓储方法各自要一个**短命 session**，用完即关——不在长执行期间持有连接（见 [run/sql.py](../backend/packages/harness/deerflow/persistence/run/sql.py) 顶部 docstring）。

### WAL（Write-Ahead Logging）

SQLite 默认用 **rollback journal**（回滚日志）模式：写之前先把原数据复制到日志，写完删日志。这种模式下，**写的时候别人不能读、读的时候别人不能写**——并发很差。

**WAL 模式**：写时不改原文件，而是把改动**追加**到一个 `-wal` 伴随文件；读时把 wal 里的最新改动叠加到原文件读出。于是：**多个读者 + 一个写者可以同时进行，互不阻塞**。

### run / thread / run_event / thread_meta（DeerFlow 领域词）

| 名词 | 是什么 | 谁管 |
|------|--------|------|
| **thread（线程/会话）** | 一次连续对话（同一个聊天窗口） | LangGraph checkpointer + 本模块的 `thread_meta` |
| **run（运行）** | thread 里**一次**「发消息→AI 回复」的完整执行 | 本模块的 `RunRow` / `RunRepository` |
| **run_event（运行事件）** | run 内的一个时间点记录（一条消息 / 一段轨迹 / 一个生命周期事件） | 本模块建表（`RunEventRow`），存储实现在 [#9 run_event_store.md](run_event_store.md) |
| **thread_meta（线程元数据）** | thread 的「属性卡」：标题、状态、属主、自定义 metadata | 本模块的 `ThreadMetaRow` / `ThreadMetaRepository` |

一句话：**thread 是一条河，run 是河里一次水流，run_event 是水流里的水滴，thread_meta 是河边的标牌。**

---

## 3. 整体结构：它在系统里的位置

```
persistence/
├── __init__.py            # 导出 init_engine / close_engine / get_* 
├── base.py                # Base(DeclarativeBase) + to_dict() / __repr__()
├── engine.py              # 引擎生命周期：init / close / session factory；WAL；auto-create
├── json_compat.py         # 跨方言 JSON 过滤（json_match / JsonMatch）
├── models/
│   ├── __init__.py        # 模型注册入口（导入即注册到 Base.metadata）
│   └── run_event.py       # RunEventRow（run 事件表，存储实现在 #9）
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

它在系统里的位置（这是**存储地基**，checkpointer / run_event_store 都建在它上面）：

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
            runtime/runs/manager.py + worker.py（#26 runs.md）
                        │
            runtime/events/store/db.py（#9 run_event_store.md，用 RunEventRow）
                        │
            lifespan 集成装配（#28 architecture.md）
```

旁落的 runs 基类层（`runtime/runs/store/base.py` 的 `RunStore` ABC + 状态枚举）也属于持久化契约——它为什么和 manager 一起却「提前」到存储层，见 §6.6。

---

## 4. 核心概念

### 4.1 三种存储后端

| backend | 存哪 | 用途 | 本模块行为 |
|---------|------|------|-----------|
| **memory**（默认） | 内存（进程字典 / LangGraph Store） | 开发、测试；重启即失 | `init_engine` 是 **no-op**（[engine.py:86-88](../backend/packages/harness/deerflow/persistence/engine.py#L86)），`get_session_factory()` 返回 `None` |
| **sqlite** | 一个本地文件（`.db`） | 单节点部署、本地开发持久化 | 创建 engine，每连接开 WAL |
| **postgres** | 一个 PostgreSQL 服务 | 生产、多节点 | 创建 engine + 连接池；需 `asyncpg` 驱动（可选 extra） |

### 4.2 WAL 三件套：WAL + synchronous=NORMAL + busy_timeout=30000

[engine.py:114-123](../backend/packages/harness/deerflow/persistence/engine.py#L114) 对每条新 SQLite 连接开四个 PRAGMA：

```python
cursor.execute("PRAGMA journal_mode=WAL;")        # 改用 WAL：并发读 + 单写不阻塞
cursor.execute("PRAGMA synchronous=NORMAL;")      # 只在 WAL checkpoint 边界 fsync
cursor.execute("PRAGMA foreign_keys=ON;")          # 开外键约束
cursor.execute("PRAGMA busy_timeout=30000;")       # 锁竞争等待 30 秒
```

为什么这三条搭配：

- **WAL**：多个读者 + 一个写者互不阻塞（§2 已述）。这是任何生产 SQLite 部署的标准建议。
- **`synchronous=NORMAL`**：不在每次提交都 `fsync`（强制刷盘），而是在 WAL checkpoint（合并点）边界才 fsync——**安全（断电不丢已提交事务）且快**。
- **`busy_timeout=30000`（30 秒）**：当两个连接同时想写、SQLite 文件级写锁被占着时，后到的连接**等多久**再放弃并报 `database is locked`。Python 的 sqlite3 驱动默认只等 5 秒——并发启动 / 多 worker 同时写时太短，容易误报 locked。提到 30 秒给锁竞争留足窗口。

> PRAGMA 是**连接级**的，所以用 SQLAlchemy 的 `@event.listens_for(engine, "connect")` 监听器对每条新连接开，而不是启动时跑一次。

### 4.3 app 表与 checkpointer 表物理分离

LangGraph 的 checkpointer 也用 SQLAlchemy 存图执行状态（节点输出、消息历史）。**但 checkpointer 的表不归我们的 `Base` 管**（[base.py](../backend/packages/harness/deerflow/persistence/base.py) 顶部 docstring 明说）——它用自己的元数据。

即使 sqlite 模式下两者共用**同一个 `.db` 文件**，表也互不重叠：

```
deerflow.db
├── runs              ← 本模块 Base（app 元数据）
├── threads_meta      ← 本模块 Base
├── run_events        ← 本模块 Base
└── checkpoint_*      ← LangGraph checkpointer 自己的表（不归本 Base）
```

为什么分离？**生命周期和关注点不同**：checkpointer 管「图跑到哪」，本模块管「谁跑的、结果摘要、token 用了多少」。混在一起会让 schema 演进互相牵制。

### 4.4 三态 user_id + UUID→str 边界

仓储方法的 `user_id` 形参有三种取值（详见 [#5 user_context.md](user_context.md)）：

- `AUTO`（默认）：从当前请求的 contextvar 解析。
- 显式 `str`：用给定值。
- 显式 `None`：**绕过属主过滤**（仅迁移 / CLI）。

**踩坑：UUID→str 边界**。用户的 `id` 在类型上可能是 `uuid.UUID` 对象，但数据库列是 `VARCHAR(64)`（字符串）。aiosqlite 驱动**无法把原生 UUID 绑定到 VARCHAR 列**（会报 `type 'UUID' is not supported`）。所以 `resolve_user_id` 在从 contextvar 读取时强制 `str()`——在边界处转一次，不把类型变更扩散到每个调用方。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 engine 生命周期（[engine.py](../backend/packages/harness/deerflow/persistence/engine.py)）

```python
await init_engine(backend, *, url="", echo=False, pool_size=5, sqlite_dir="") -> None   # :67
await init_engine_from_config(config: DatabaseConfig) -> None   # :166  便利：从配置初始化
get_session_factory() -> async_sessionmaker | None              # :180  memory → None
get_engine() -> AsyncEngine | None                              # :185
await close_engine() -> None                                    # :190  dispose + 重置全局
```

`init_engine` 按后端分支（[第 86 行起](../backend/packages/harness/deerflow/persistence/engine.py#L86)）：

1. **memory**（[:86](../backend/packages/harness/deerflow/persistence/engine.py#L86)）：直接 `return`，不创建 engine。
2. **postgres**（[:90](../backend/packages/harness/deerflow/persistence/engine.py#L90)）：先 `import asyncpg`，缺包抛带安装命令的 `ImportError`（`uv sync --all-packages --extra postgres`）。
3. **sqlite**（[:98](../backend/packages/harness/deerflow/persistence/engine.py#L98)）：`os.makedirs` 建 SQLite 目录——**用 `asyncio.to_thread` 卸载**（[:105](../backend/packages/harness/deerflow/persistence/engine.py#L105)，见 §6.5），`create_async_engine`，挂 WAL 监听器（§4.2）。

建表用 `create_all`（[:147-149](../backend/packages/harness/deerflow/persistence/engine.py#L147)）：`async with engine.begin(): conn.run_sync(Base.metadata.create_all)`。对 postgres 有个容错：若报「database does not exist」，自动连 `postgres` 维护库 `CREATE DATABASE` 后重建 engine 重试（[:151-159](../backend/packages/harness/deerflow/persistence/engine.py#L151)）。

> engine 还注入了一个 `ensure_ascii=False` 的 `_json_serializer`（[:31-33](../backend/packages/harness/deerflow/persistence/engine.py#L31)）——序列化 JSON 列时保留中文，不转义成 `\uXXXX`。

### 5.2 `Base.to_dict()` + `@cache _column_keys`（[base.py:18-55](../backend/packages/harness/deerflow/persistence/base.py#L18)）

所有 app ORM 模型继承 `Base(DeclarativeBase)`（[:29](../backend/packages/harness/deerflow/persistence/base.py#L29)），自带通用的 `to_dict(exclude=)`（[:37](../backend/packages/harness/deerflow/persistence/base.py#L37)）和 `__repr__`（[:53](../backend/packages/harness/deerflow/persistence/base.py#L53)）——让每个模型不必各自写序列化。

关键优化：列键用 `@cache` 缓存（[:18-26](../backend/packages/harness/deerflow/persistence/base.py#L18)）。`inspect(cls).mapper.column_attrs` 每次都要走 SQLAlchemy mapper 内省，而列集合在类定义后就不变——按类缓存一份元组，把每次内省省掉。list 端点一次序列化成百上千行时是有意义的热路径优化。

### 5.3 `RunRepository` —— run 元数据的 SQL 实现（[run/sql.py:29](../backend/packages/harness/deerflow/persistence/run/sql.py#L29)）

继承 `RunStore`（ABC 在 `runtime/runs/store/base.py`）。每个方法各自获取并释放一个短命 session。三个值得讲的方法：

**`put`（幂等，[:87-131](../backend/packages/harness/deerflow/persistence/run/sql.py#L87)）**：先 `session.get(RunRow, run_id)`，有就改、没有就插——「insert or update」。为什么幂等见 §6.3。

**`update_status` / `update_run_completion`（返回 `bool`，[:164](../backend/packages/harness/deerflow/persistence/run/sql.py#L164) / [:226](../backend/packages/harness/deerflow/persistence/run/sql.py#L226)）**：返回 `result.rowcount != 0`。`True`=确实更新了行；`False`=**能证明没有行被更新**（rowcount==0）。为什么需要这个返回值见 §6.2。

**`aggregate_tokens_by_thread`（[:313-375](../backend/packages/harness/deerflow/persistence/run/sql.py#L313)）**：拉出每行的各 token 列 + `token_usage_by_model`，在 **Python 侧**按真计费模型逐模型累加（不能用单条 `GROUP BY model_name`，见 §6.4）。

还有几个细节：`_normalize_model_name` strip + 截断 128 字符（[:33](../backend/packages/harness/deerflow/persistence/run/sql.py#L33)）；`_row_to_dict` 把 `created_at`/`updated_at` 经 `coerce_iso` 归一（[:72-85](../backend/packages/harness/deerflow/persistence/run/sql.py#L72)，见 §6.7）；`update_run_progress` 带 `.where(status == "running")`，只更新仍活跃的 run（[:310](../backend/packages/harness/deerflow/persistence/run/sql.py#L310)）；`last_ai_message`/`first_human_message` 截断 `[:2000]` 防长文本撑爆列。

### 5.4 `RunRow` —— run 元数据表（[run/model.py:13](../backend/packages/harness/deerflow/persistence/run/model.py#L13)）

关键字段：`run_id`（PK）、`thread_id`（索引）、`user_id`（索引，做隔离）、`status`（默认 `"pending"`，[:22](../backend/packages/harness/deerflow/persistence/run/model.py#L22)）、`model_name`、一组平铺 token 列、`token_usage_by_model`（JSON，[:48](../backend/packages/harness/deerflow/persistence/run/model.py#L48)）、便利字段（`message_count` / `first_human_message` / `last_ai_message`，让列表页不必查 RunEventStore）、`created_at`/`updated_at`（`DateTime(timezone=True)`）。`__table_args__` 加 `(thread_id, status)` 复合索引（[:56](../backend/packages/harness/deerflow/persistence/run/model.py#L56)）。

### 5.5 跨方言 JSON 过滤（[json_compat.py](../backend/packages/harness/deerflow/persistence/json_compat.py)）

线程 metadata 是个 JSON 列。要支持「找所有 `metadata.team == 'x'` 的线程」，但 SQLite（`json_extract`）和 PostgreSQL（`->>`）语法完全不同，且要区分 `bool` vs `int`、`NULL` vs 缺键。

`json_match(column, key, value)`（[:204](../backend/packages/harness/deerflow/persistence/json_compat.py#L204)）用 SQLAlchemy 的 `@compiles` 机制为每种方言编译出类型安全的谓词（sqlite 编译器 [:178](../backend/packages/harness/deerflow/persistence/json_compat.py#L178) / postgres 编译器 [:189](../backend/packages/harness/deerflow/persistence/json_compat.py#L189)）。bool 检查在 int 检查之前（[:159-164](../backend/packages/harness/deerflow/persistence/json_compat.py#L159)，因为 Python 里 `bool` 是 `int` 的子类）；int 还额外限制在有符号 64 位范围（[:39-40](../backend/packages/harness/deerflow/persistence/json_compat.py#L39)，防 SQLite/PG 溢出）。

**安全关键**：key 限制为 `[A-Za-z0-9_-]+`（[第 32 行](../backend/packages/harness/deerflow/persistence/json_compat.py#L32)），因为 key 会被插值进编译出的 SQL 路径表达式（`$."<key>"` / `->` 字面量），放宽字符集会打开 SQL/JSONPath 注入面。当所有 key 都不安全时，`search` 抛 `InvalidMetadataFilterError`（返回 400 给客户端）。

### 5.6 `thread_meta` —— 线程元数据 + 内存实现

- **`ThreadMetaStore` ABC**（[thread_meta/base.py:27](../backend/packages/harness/deerflow/persistence/thread_meta/base.py#L27)）：`create/get/search/update_*/check_access/delete`，全带三态 `user_id`。`InvalidMetadataFilterError` 在 [:23](../backend/packages/harness/deerflow/persistence/thread_meta/base.py#L23)。
- **`ThreadMetaRow`**（[thread_meta/model.py:13](../backend/packages/harness/deerflow/persistence/thread_meta/model.py#L13)）：`thread_id` PK、`assistant_id`、`user_id`（索引）、`display_name`（String 256）、`status`（默认 `"idle"`）、`metadata_json` JSON、时间戳。
- **`MemoryThreadMetaStore`**（[thread_meta/memory.py:20](../backend/packages/harness/deerflow/persistence/thread_meta/memory.py#L20)）：memory 模式用，包一层 LangGraph `BaseStore`，委托给 `("threads",)` 命名空间（[:17](../backend/packages/harness/deerflow/persistence/thread_meta/memory.py#L17)）。`_item_to_dict`（[:143-158](../backend/packages/harness/deerflow/persistence/thread_meta/memory.py#L143)）用 `coerce_iso` 修复早期版本用 `str(time.time())` 写入的 legacy unix 秒值。
- **`make_thread_store` 工厂**（[thread_meta/__init__.py:33-45](../backend/packages/harness/deerflow/persistence/thread_meta/__init__.py#L33)）：有 session_factory → `ThreadMetaRepository`；无 sf 有 LangGraph Store → `MemoryThreadMetaStore`；都没有 → 抛错。

### 5.7 `RunEventRow` —— 只建表，存储实现在 #9（[models/run_event.py:19](../backend/packages/harness/deerflow/persistence/models/run_event.py#L19)）

一条 run event = run 内的一个时间点记录：一条消息（message）、一段轨迹（trace，如 LLM/tool 调用）、或一个生命周期事件（lifecycle，如 run 开始/结束）。本 Phase 1 **只把表结构建好**（供 `create_all` 与未来的 `DbRunEventStore` 用），存储实现（memory/jsonl/db）在 [#9 run_event_store.md](run_event_store.md)。

关键约束：`UniqueConstraint("thread_id", "seq")`（[:39](../backend/packages/harness/deerflow/persistence/models/run_event.py#L39)，防同一线程 seq 重复）+ 两个复合索引。

---

## 6. 设计动机分析（为什么这么设计 / 作用 / 好处）

> persistence 的每个选择都直击「**怎么把应用元数据可靠落盘，且并发 / 隔离 / 重试都不出岔**」。读得懂这些「为什么」，你才算理解了后端持久化工程（面试高频：「并发控制 WAL」「ORM 设计」「多租户隔离」「幂等与重试」「跨方言兼容」）。每条都问：**解决什么问题？带来什么好处？不这么设计会怎样？**

### 6.0 核心设计动机（先看这张表）

一句话总动机：**用 ORM 把应用元数据可靠落盘，并把并发 / 隔离 / 重试 / 跨方言的脏活用机制兜住**——让数据层在 SQLite/PostgreSQL/内存三种后端下都正确、且空配置也能启动。

| 设计选择 | 存在动机（为什么） | 作用 / 好处 | 不这么设计会怎样 |
|---------|-------------------|------------|-----------------|
| **物理分库分表**（app 元数据 vs checkpointer 对话） | 两者生命周期 / 关注点不同 | schema 演进互不牵制，各管各的 | 混表 → 改一边牵动另一边，迁移噩梦 |
| **WAL 三件套**（WAL+synch=NORMAL+busy_timeout） | SQLite 默认 rollback journal 并发极差 | 多读 + 单写不阻塞；安全且快；锁竞争等 30s | 默认模式 → 读写互阻、频发 `database is locked` |
| **memory 是 no-op**（不建 engine） | 空配置必须能启动 | 什么都不配也能跑、CI 不依赖外部服务 | memory 也建 engine → 缺包/缺目录就起不来 |
| **三态 user_id + UUID→str 边界** | 多租户隔离 + aiosqlite 不支持 UUID 绑 VARCHAR | 隔离越权；边界转一次不扩散到每个调用方 | 无 user_id → Bob 看到 Alice 数据；UUID 直进 → `type not supported` |
| **rowcount `bool` 返回 + 幂等 put** | 恢复需知「行还在吗」+ 重试需安全 | rowcount 驱动 recovery；幂等让重试不主键冲突 | 返回 None → 恢复盲猜；非幂等 → 重试撞主键 |
| **`token_usage_by_model` JSON 列** | 一个 run 可能路由多模型 | 按真计费模型分桶，能答「gpt-4o 花多少」 | 平铺列 → 只知总数，不知各模型明细 |

下面 §6.1–§6.7 是逐条展开。

### 6.1 memory 模式是 no-op，不是「内存引擎」

`init_engine("memory")` 直接 return，**根本不创建 engine**。`get_session_factory()` 返回 `None`。

这意味着：**所有用到持久化的代码都必须先检查 `None`，并回退到内存实现**。例如 lifespan 里：

```python
sf = get_session_factory()
run_store = RunRepository(sf) if sf else MemoryRunStore()   # None → 内存
thread_store = make_thread_store(sf, store=langgraph_store)  # 工厂内部处理 None
```

这是「**空配置必须能以 memory 模式启动**」的体现——什么都不配也能跑起来、CI 不依赖外部服务（详见 [#3 config.md](config.md)）。

### 6.2 rowcount 驱动 recovery（`update_status` / `update_run_completion` 返回 `bool`）

这两个方法返回 `bool`：

- `True`：确实更新了行。
- `False`：**能证明没有行被更新**（rowcount == 0）。

为什么需要这个返回值？RunManager（[#26 runs.md](runs.md)）在某些恢复路径里需要知道「这个 run 还在库里吗」。如果 `update_status` 返回 `False`，说明库里没这行了，RunManager 要从**内存快照重建**一行。轻量 / 旧版实现可以返回 `None`（无法报告 rowcount），调用方据此区分「确定没了」vs「不确定」。

### 6.3 幂等 put（防重试变主键冲突）

`RunRepository.put` 是「insert or update」（§5.3）。这是因为 RunManager 在遇到瞬时 SQLite 失败（如 `database is locked`）后会**重试 put**。如果 put 不是幂等的，一次「成功但未确认的提交」会让重试变成主键冲突。

### 6.4 `token_usage_by_model`：一个 run 可能用多个模型（[run/model.py:48](../backend/packages/harness/deerflow/persistence/run/model.py#L48)）

`RunRow` 有一组平铺的 token 列（`total_tokens` / `lead_agent_tokens` / `subagent_tokens` / `middleware_tokens` …）。但**一个 run 可能路由到多个模型**——主模型 + 兜底模型、子代理用了别的模型。平铺列只能记「这个 run 一共花了多少 token」，没法回答「`gpt-4o` 花了多少、`gpt-4o-mini` 花了多少」这种按真计费模型的账。

解法：加一列 `token_usage_by_model`（JSON，结构 `{model_name: {input_tokens, output_tokens, total_tokens}}`），由 RunJournal 在内存里按 `response_metadata.model_name` 分桶累加，run 完成时随其它 token 列一起写入（`update_run_completion` / `update_run_progress` 签名都加了该参数）。

> 为什么记 `response_metadata.model_name` 而不是 config 里写的 model？因为**真计费模型由 provider 返回决定**，不是配置里写的。一个 agent 可能配了 `gpt-4o`，但请求被路由到 fallback 或别名，provider 返回的 `model_name` 才是真正计费的那个。

`aggregate_tokens_by_thread` 因此也变了：旧版用一条 SQL `GROUP BY model_name`（假设一行 run 只对应一个模型，错！）；新版拉出每行各 token 列 + `token_usage_by_model`，在 Python 侧按真计费模型逐模型累加——有分桶数据的行按桶累加（一个 run 可贡献给多个模型桶），**老行**（无该列值）回退到行的 `model_name` 整 run 归一桶。mini 无 alembic，所以列上加 `server_default=text("'{}'")`——旧库 `create_all` 重建或手动 ALTER 加列时直接给空 JSON，无需回填迁移。

### 6.5 hot path 微优化：to_thread 卸载 + @cache 列键

两条不改语义、只让热路径更稳更快的优化：

- **`os.makedirs` 用 `asyncio.to_thread` 卸载**：`init_engine` 是 async 函数，跑在 lifespan 的事件循环里；`os.makedirs`（建 SQLite 目录）是同步磁盘 IO。直接调会卡住事件循环——这正是 mini 自己 blocking-IO gate 要拦的（详见 [#2 testing-setup.md](testing-setup.md)）。卸到线程池后，事件循环期间不被这一个系统调用阻塞。
- **`@cache _column_keys`**（§5.2）：把 `to_dict()` / `__repr__()` 每次的 SQLAlchemy 内省省掉。

### 6.6 runs 基类为什么提前到持久化层

`RunRepository` 继承 `RunStore`（ABC）。但 `RunStore` 属于 runs 领域，而 runs 的**运行管理层**（RunManager / worker）在 [#26 runs.md](runs.md)，又依赖持久化。如果把 ABC 留到 #26，会形成「持久化 → 运行管理 → 持久化」的**循环依赖**。

解法：把纯数据的 `RunStore` ABC + 状态枚举（`runtime/runs/store/base.py` + `runtime/runs/schemas.py`）提前到持久化层，运行管理留 #26。于是 `RunRepository(RunStore)` 可以先于 `RunManager` 存在，循环被打破。

### 6.7 SQLite 时间戳丢时区 → `coerce_iso` 归一

SQLite 声明了 `DateTime(timezone=True)`，但**读回来时 tzinfo 会丢失**（naive datetime）。所以 `_row_to_dict` 用 `coerce_iso`（见 [#4 utils.md](utils.md)）把 naive datetime 当作 UTC 归一成 ISO 字符串，保证线格式始终带时区。否则前端会拿到 `2026-06-17 10:00:00`（没 `T`、没时区），解析出错。

---

## 7. 配置与用法

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

（DatabaseConfig 派生 `sqlite_path` / `app_sqlalchemy_url` 的逻辑见 [#3 config.md](config.md) §6.2。）

---

## 8. 与其它模块的关系

- **依赖**：[#3 config](config.md)（`DatabaseConfig`）、[#4 utils](utils.md)（`coerce_iso` / `now_iso`）、[#5 user_context](user_context.md)（三态 `resolve_user_id`）、`runtime/runs/store/base.py`（`RunStore` ABC）、（memory 模式）LangGraph `BaseStore`。
- **被依赖**：
  - [#26 runs.md](runs.md)：`RunManager` / worker 用 `RunRepository` 存 run 生命周期 + token；`MemoryRunStore` 是内存侧对应实现。
  - [#9 run_event_store.md](run_event_store.md)：`DbRunEventStore` 用本模块建的 `RunEventRow` 表。
  - [#8 checkpointer.md](checkpointer.md)：sqlite 模式下与 checkpointer 共用同一个 `.db` 文件（但表分离）。
  - [#28 architecture.md](architecture.md)：lifespan 里 `init_engine_from_config` + `make_thread_store` 装配，drain 时先 drain 再 `close_engine`。

---

## 9. 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/persistence/`（上游 27 个 `.py` / mini 14 个），剥 docstring 后判逻辑差。结论：**核心持久化层（10 个共享文件）是上游的忠实移植**——`base.py` 逐行相同（55=55），`engine` / `run` / `thread_meta` / `json_compat` / `run_event` 都在几行误差内（差异是 docstring 语言）。差异是 mini **砍掉 5 类上游独有的文件**，全部对应「砍 Gateway / IM / 未 port 功能 / 不要迁移工具」。

### 差异 1：核心持久化层——忠实移植（10 文件逐个对齐）

| 文件 | mini / 上游 行数 | 说明 |
|---|---|---|
| `base.py` | 55 / 55 | **逐行相同**（`Base` + `to_dict` + `@cache _column_keys`） |
| `engine.py` | 197 / 205 | WAL 三件套 / `init`·`close`·`session_factory` 一致（差几行 docstring） |
| `run/sql.py` | 375 / 378 | `RunRepository` 全方法一致，含 `token_usage_by_model` 分桶（上游 `migrations/0002` 加的列，两边都有） |
| `run/model.py` | 56 / 50 | `RunRow` 字段一致 |
| `json_compat.py` | 206 / 195 | `json_match` 跨方言过滤一致 |
| `thread_meta/{base,memory,model,sql}.py` | 88+158+25+232 / 90+159+23+243 | `ThreadMetaStore` ABC + 内存/SQL 双实现一致 |
| `models/run_event.py` | 42 / 35 | `RunEventRow` 建表一致 |

### 差异 2：mini 砍掉的 5 类上游文件

| 上游独有 | 是什么 | mini 为什么不要 |
|---|---|---|
| `migrations/`（env.py + helpers + versions/0001 baseline + 0002 runs_token_usage） | **Alembic 数据库迁移**（schema 演进工具） | mini **故意不引入 Alembic**（§10 FAQ）——开发期直接删 `.db` 让 `create_all` 重建；教学版不背迁移工具的复杂度 |
| `bootstrap.py` | 启动时自动 `alembic upgrade head` 的 schema 引导 | 依赖 Alembic，一起砍 |
| `channel_connections/`（model + sql） | IM 渠道连接（飞书/Slack/钉钉）存储 | mini 无 IM（→ [start-here.md](start-here.md) §2.2） |
| `feedback/`（model + sql） | 用户反馈存储 | 功能未 port |
| `user/`（model） | 真实 `User` 表 | mini 用 `CurrentUser` Protocol + `SimpleNamespace`（→ #5），不建独立 User 表（鉴权是 Gateway 层） |

### 为什么核心层如此一致

persistence 是**纯数据层**——它的输入（`DatabaseConfig` / `user_id` / `time`）都来自抽象的 config / user_context / utils，**不依赖 Gateway / app**。所以砍掉 Gateway 后，核心持久化层**几乎零改动**——和 #5 user_context 的 Protocol 抽象红利同理（底层定义接口、不依赖上层具体实现，换掉上层底层零改动）。mini 只是「砍掉 IM/反馈/迁移/User 这些上游产品功能，保留通用存储骨架」。

> **一句话总结**：mini 的 persistence = 上游核心存储层的**忠实移植**（`base.py` 逐行相同，`engine`/`run`/`thread_meta`/`json_compat` 全在几行误差内），砍掉 5 类上游独有文件：Alembic 迁移（`migrations/` + `bootstrap.py`，mini 故意不要迁移工具）+ IM 渠道连接（`channel_connections/`）+ 用户反馈（`feedback/`）+ 真实 User 表（`user/`，mini 用 Protocol 代替）。核心层零改动，因为数据层靠抽象解耦了上层。

---

## 10. 常见问题 / 排错

**Q: `get_session_factory()` 返回 `None`？**
A: 当前 backend 是 `memory`（默认）。memory 模式不创建 engine。要么改 `config.yaml` 的 `database.backend`，要么调用方检查 `None` 并回退内存实现（§6.1）。

**Q: 报 `database is locked` / `no such table`？**
A：
- `database is locked`：理论上 WAL + 30 秒 busy 超时已大幅缓解；若仍出现，说明写并发极高，考虑上 postgres。
- `no such table`：`init_engine` 会 `create_all` 自动建表。如果跳过了 `init_engine` 直接用 session_factory，表就不存在。

**Q: 报 `type 'UUID' is not supported`？**
A: 你把 `uuid.UUID` 对象直接传给了 `user_id`。应该传 `str(uuid)`，或经 `resolve_user_id(AUTO)`（它会在边界自动 `str()`，§4.4）。

**Q: 读回来的时间戳没有时区 / 没有 `T`？**
A: SQLite 会丢 tzinfo。所有出库的时间戳都经 `coerce_iso` 归一成带时区的 ISO 字符串（§6.7）。如果你绕过了 `_row_to_dict` 直接读 ORM 字段，会拿到 naive datetime——请走 `_row_to_dict`。

**Q: postgres 报 `asyncpg is not installed`？**
A: postgres 驱动是可选依赖。安装：`cd backend && uv sync --all-packages --extra postgres`。

**Q: 为什么 `to_dict()` 拿到的 `status` 是 `None`（我刚 new 的对象）？**
A: SQLAlchemy 的列 `default` 只在 **flush / commit** 时生效，不在对象构造时。瞬态（未入库）对象上 `status` 是 `None`。flush 后才会是 `"pending"`。

**Q: 改了表结构（加列），怎么迁移？**
A: 本期没有 Alembic（数据库迁移工具）。开发期直接删 `.db` 文件重启（`create_all` 会按新 schema 重建）。生产迁移工具在后续引入。

---

## 11. 小结

persistence 模块的精髓是**用 ORM 把应用元数据可靠落盘，并把并发 / 隔离 / 重试的脏活收口**。记住五件事：

1. **物理分离**：应用元数据（runs / threads_meta / run_events）与对话内容（checkpointer 表）分表，即使共用一个 `.db` 文件。
2. **WAL 三件套**：WAL（并发不阻塞）+ `synchronous=NORMAL`（安全且快）+ `busy_timeout=30000`（锁竞争等 30 秒）。
3. **memory 是 no-op**：`get_session_factory()` 返回 `None`，调用方必须回退内存实现——保证空配置能启动。
4. **三态 user_id + rowcount + 幂等 put**：用户隔离靠 `user_id` 三态；恢复靠 rowcount `bool` 返回值；重试安全靠幂等 put。
5. **按模型归桶 token**：一个 run 可能用多模型，加 `token_usage_by_model` JSON 列记真计费模型，老行回退 `model_name`。

上一篇：[#6 models.md](models.md)（模型工厂）· 下一篇：[#8 checkpointer.md](checkpointer.md)（检查点工厂——委托 LangGraph Saver，不自建，依赖本模块的 sqlite 工具）。
