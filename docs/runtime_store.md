# 27. runtime_store.md — LangGraph Store 工厂（与 checkpointer 平行的跨线程记忆）

> **一句话定位**：本模块是「长效记忆层」工厂——给 agent 一个跨 thread、跨 run 的键值存储（`BaseStore`），存 thread 列表、长期数据。和 checkpointer（存图状态快照）是**两种不同的持久化需求**，但 mini 让它们共用同一个 `checkpointer` 配置段、用同一种后端技术。

**学完能回答（learning outcomes）**：

1. Store 和 checkpointer 各存什么、为什么是两种不同的持久化需求；
2. 为什么 Store 要和 checkpointer 共用同一个 `checkpointer` 配置段（同一种后端技术）；
3. 三个入口（`make_store` 异步 / `get_store` 同步单例 / `store_context` 同步一次性）各什么场景用、区别在哪；
4. 双检锁单例（double-checked locking）怎么保证线程安全又避免重复建；
5. soft-load（缺包抛 ImportError 带安装提示）和 None→InMemoryStore（开箱即用）这两条原则为什么重要；
6. 能在面试里讲清「mini 的 Store 工厂与上游 deer-flow 源码的差异」（见 §10——与 checkpointer 工厂同一套忠实移植模式）。

读完 [runs.md](runs.md)（懂了「RunManager + worker 怎么跑一次 run」）再看本篇最省事——本篇回答「run 之外的那些**跨 run 数据**（thread 列表、记忆）存在哪、怎么建」。worker 的 `RunContext.store` 就是这里建的 Store。本模块和 [checkpointer.md](checkpointer.md)（#8）是**平行结构**，先读那篇再看本篇最顺畅。

---

## 1. 名词（先懂这些再往下看）

### 1.1 计算机基础层（每个名词第一次出现就解释）

- **Store（键值存储）**：按「命名空间 + 键」存取数据的存储。像一个大 dict，但跨进程、跨重启存活。LangGraph 的 `BaseStore` 是它的接口，专门存「跨 thread 的长效数据」（区别于存图状态快照的 checkpointer）。
- **namespace（命名空间）**：键值存储里给 key 分组的「路径」，通常是个元组如 `("threads",)` 或 `("users", user_id, "prefs")`。同一 namespace 下的 key 不冲突，不同 namespace 互不干扰。
- **上下文管理器（context manager）**：Python 的 `with` / `async with` 协议——进入 `with` 块时获取资源（如打开数据库连接），块退出时自动清理（关连接）。本模块的 `make_store` / `store_context` 都是上下文管理器，保证「用完即拆」。详见 [checkpointer.md](checkpointer.md)。
- **单例（singleton）**：一个进程里只存在一个实例的对象。`get_store()` 返回单例——首次调用时建一个 Store，之后所有调用复用同一个，进程退出才关。避免每次请求都重建连接。
- **双检锁（double-checked locking）**：线程安全的单例建法——先无锁检查「是否已建」（快路径，已建就直接返回），没建才进锁；进锁后**再检查一次**（防止两个线程同时通过第一次检查）。详见 [skills.md](skills.md)。
- **soft-load（软加载）**：import 一个可选依赖时，不把它写死在顶层（否则缺包整个模块 import 就崩），而是延迟到用的时候才 import；缺了就抛 `ImportError` 带**安装提示**（告诉用户装哪个包）。本模块对 sqlite/postgres 后端就这么做。
- **DSN / connection_string**：Data Source Name，连数据库的连接串（如 `postgresql://user:pass@host:5432/db`）。
- **`:memory:`**：SQLite 的特殊「连接串」——表示在内存里建库（不落盘，进程退出就没）。本模块的 `resolve_sqlite_conn_str` 对它原样返回。
- **`file:` URI**：SQLite 的另一种连接串形式（`file:/path/to/db`），也原样返回。

### 1.2 模块层名词

- **`BaseStore`**：LangGraph 的 Store 接口（`put / get / search / delete` 等方法）。本模块的工厂产出它的具体实现。
- **`make_store` / `get_store` / `store_context`**：本模块的三个入口（异步 CM / 同步单例 / 同步一次性 CM），见 §4。
- **`InMemoryStore` / `SqliteStore` / `PostgresStore`**：LangGraph 提供的三种 Store 实现（内存 / SQLite / PostgreSQL），各有同步和异步（`Async*`）变体。

---

## 2. 这个模块解决什么问题

agent 跑一次 run 有两类数据要存：

1. **图状态快照**（按 thread + checkpoint_id）：对话走到哪一步、消息历史、中间变量——这是 **checkpointer** 的活（见 [checkpointer.md](checkpointer.md)）。run 之间靠它恢复对话。
2. **跨 thread 的长效数据**（按 namespace + key）：thread 列表本身、用户长期偏好、跨会话事实——这是 **Store** 的活（本模块）。它不绑某次 run，是 agent 的「长期记忆」。

两者都是持久化需求，但语义不同。LangGraph 把它们分成两个接口（`BaseCheckpointSaver` vs `BaseStore`）。mini 让它们**共用同一个 `checkpointer` 配置段**——同一份连接配置、一套运维，最省事。

---

## 3. Store vs checkpointer（一图看清）

| 维度 | checkpointer（[checkpointer.md](checkpointer.md)） | Store（本模块） |
|------|--------------------|----------------------|
| 存什么 | 图状态快照 | 跨 thread 长效键值 |
| 粒度 | (thread_id, checkpoint_id) | (namespace, key) |
| 生命周期 | 每次 run 写 / 读 | 跨 run / 跨 thread |
| 典型用途 | 恢复对话、rollback | thread 列表、长期记忆、thread_meta |
| 接口 | `aput / aget_tuple / adelete_thread` | `put / get / search / delete` |
| 配置段 | `checkpointer` | `checkpointer`（**共用**） |
| 实现 | InMemorySaver / SqliteSaver / PostgresSaver | InMemoryStore / SqliteStore / PostgresStore |

**为什么共用配置？** 同一种数据库后端最省运维。配 `checkpointer.type: sqlite` 时，checkpointer 和 Store 都用 sqlite（各自的表 / 文件）；配 memory 都内存。mini 的 Store 工厂读同一个 `app_config.checkpointer` 段，保证两者恒用同一种技术。

---

## 4. 三个入口（异步 / 同步单例 / 同步一次性）

[store/](../backend/packages/harness/deerflow/runtime/store/) 提供三套 API，对应不同调用场景：

### `make_store(app_config)` — 异步上下文管理器（lifespan / 长跑服务用）

[async_provider.py:90](../backend/packages/harness/deerflow/runtime/store/async_provider.py#L90)：

```python
from deerflow.runtime.store import make_store

async with make_store(app_config) as store:
    bundle.store = store
```

读 `app_config.checkpointer`：None → `InMemoryStore`（发 WARNING）；否则按 `type` 建对应 Store。sqlite/postgres 缺包抛 `ImportError` 带安装提示（soft-load）。

### `get_store()` — 同步单例（CLI / 内嵌 client 用）

[provider.py:109](../backend/packages/harness/deerflow/runtime/store/provider.py#L109)：

```python
from deerflow.runtime.store import get_store

store = get_store()   # 首次创建，之后复用；进程退出才关
```

线程安全（`threading.Lock` 双检锁，[provider.py:124-126](../backend/packages/harness/deerflow/runtime/store/provider.py#L124-L126)）。`reset_store()`（[provider.py:145](../backend/packages/harness/deerflow/runtime/store/provider.py#L145)）清单例（测试 / 配置变更后强制重建）。

### `store_context()` — 同步一次性上下文管理器（CLI 脚本 / 测试用）

[provider.py:166](../backend/packages/harness/deerflow/runtime/store/provider.py#L166)：

```python
from deerflow.runtime.store import store_context

with store_context() as store:   # 每次新建，块退出即关（不缓存）
    store.put(("threads",), thread_id, {...})
```

与 `get_store` 不同：**不缓存**，每个 `with` 块自建自毁一个连接。想要确定性清理时用。

---

## 5. 代码走读

### 5.1 后端选择（镜像 checkpointer）

[provider.py:53](../backend/packages/harness/deerflow/runtime/store/provider.py#L53) 的 `_sync_store_cm` + [async_provider.py:38](../backend/packages/harness/deerflow/runtime/store/async_provider.py#L38) 的 `_async_store` 按 `config.checkpointer.type` 分支：

| type | 同步 Store | 异步 Store | 缺包提示 |
|------|-----------|-----------|------|
| `memory` | `InMemoryStore` | `InMemoryStore` | — |
| `sqlite` | `SqliteStore` | `AsyncSqliteStore` | `langgraph-checkpoint-sqlite` |
| `postgres` | `PostgresStore` | `AsyncPostgresStore` | `langgraph-checkpoint-postgres` |

sqlite 用 `_sqlite_utils.resolve_sqlite_conn_str` + `ensure_sqlite_parent_dir`（[_sqlite_utils.py:19](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py#L19)、[_sqlite_utils.py:30](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py#L30)，与 checkpointer 共用）；postgres 要 `connection_string`（缺了抛 `ValueError`）。建后都调 `store.setup()`（建表）。soft-load 安装提示常量在 [provider.py:37-41](../backend/packages/harness/deerflow/runtime/store/provider.py#L37-L41)。

### 5.2 None → InMemoryStore（开箱即用）

[provider.py:44](../backend/packages/harness/deerflow/runtime/store/provider.py#L44) 的 `_no_checkpointer_warning`：没配 `checkpointer` 段时发 WARNING 并 fallback 到 `InMemoryStore`（thread 列表重启丢失）。这和 checkpointer 的「None → InMemorySaver」对称——开箱即用，不强制配置。

### 5.3 `_sqlite_utils` 两个纯函数（与 checkpointer 共用）

[_sqlite_utils.py](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py)：

- `resolve_sqlite_conn_str(raw)`：`:memory:` / `file:` URI 原样返回；普通路径经 `resolve_path` 解析成绝对路径。
- `ensure_sqlite_parent_dir(conn_str)`：确保 SQLite 文件父目录存在（防止「unable to open database file」——当 `.deer-flow` 目录还没建时）；对内存库 / URI 是 no-op。

### 5.4 worker 怎么用 Store

worker（[runs/worker.py](../backend/packages/harness/deerflow/runtime/runs/worker.py)）的 `RunContext.store` 装着本模块建的 Store，两处用到：

1. **构建 agent 时**：`agent.store = store`——让 agent 图里的节点能经 `ToolRuntime.context.store` 或 langgraph 的 store 接口读写跨 thread 数据；
2. **thread_meta 回退**：memory 后端无 SQL 时，`MemoryThreadMetaStore(store)` 用 Store 存 thread 元数据（标题 / 状态）。

---

## 6. 数据流（Store 怎么从配置到 agent）

```
config.yaml 的 checkpointer 段（type: sqlite/memory/postgres + connection_string）
        │
        ▼ lifespan 进时（或 get_store 首次调用）
make_store(app_config) / get_store()
        │ 读 app_config.checkpointer
        ├─ None → InMemoryStore（WARNING：重启丢失）
        └─ 有 type → _async_store / _sync_store_cm 分支
                ├─ memory  → InMemoryStore()
                ├─ sqlite  → resolve_sqlite_conn_str + ensure_sqlite_parent_dir
                │            → SqliteStore / AsyncSqliteStore（soft-load 缺包提示）
                └─ postgres → 校验 connection_string → PostgresStore / AsyncPostgresStore
        │ store.setup()（建表）
        ▼
Store 实例
        ├─ lifespan 路径：yield 进 RuntimeBundle.store（async with 管理）
        ├─ get_store 路径：进单例 _store（双检锁保护）
        └─ store_context 路径：yield 给 with 块（块退出即关）
        │
        ▼ worker 构建时
agent.store = store   ← 图节点经 ToolRuntime.context.store 读写跨 thread 数据
```

---

## 7. 配置

Store 读 `app_config.checkpointer` 段（与 checkpointer 共用）：

| 字段 | 作用 | 默认 |
|------|------|------|
| `checkpointer`（整个段） | None → InMemoryStore（WARNING） | None |
| `checkpointer.type` | `memory` / `sqlite` / `postgres` | — |
| `checkpointer.connection_string` | sqlite 路径或 postgres DSN（sqlite 默认 `"store.db"`，postgres 必填） | — |

示例（`config.yaml`）：

```yaml
checkpointer:
  type: sqlite
  connection_string: ".deer-flow/store.db"
```

---

## 8. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **checkpointer** | 共用 `checkpointer` 配置段 + `_sqlite_utils`；同一种后端技术；三入口结构平行 |
| **runs/worker** | `RunContext.store` ← 本模块；worker 挂 `agent.store = store` |
| **lifespan**（[architecture.md](architecture.md) #28） | `make_store(app_config)` 进 AsyncExitStack，yield 进 bundle |
| **persistence/thread_meta** | memory 后端的 `MemoryThreadMetaStore(store)` 用 Store 存 thread 元数据 |
| **config/checkpointer_config** | `CheckpointerConfig(type, connection_string)`——Store 读它 |

---

## 9. 设计动机分析

### 9.0 核心设计动机表

| 设计 | 为什么 | 不这么设计会怎样 |
|------|--------|------------------|
| **Store 与 checkpointer 分两个接口** | 图状态快照 vs 跨 thread 长效记忆，语义不同 | 一个接口塞两种数据，查询 / 生命周期混乱 |
| **共用 `checkpointer` 配置段** | 同一种后端技术，一份连接配置、一套运维 | 两份配置易漂移（checkpointer sqlite + Store postgres） |
| **三入口（异步 CM / 同步单例 / 同步一次性）** | 长跑服务 / CLI / 测试各需不同生命周期 | 一个入口要么没法复用连接、要么没法确定性清理 |
| **`get_store` 双检锁单例** | 线程安全 + 避免重复建 | 无锁不安全；每次进锁又慢 |
| **soft-load（缺包抛 ImportError 带提示）** | sqlite/postgres 是可选依赖 | 顶层 import 缺包就崩，连 memory 后端都用不了 |
| **None → InMemoryStore** | 开箱即用 | 没配 checkpointer 段就报错，新手卡在第一步 |
| **`_sqlite_utils` 共用** | checkpointer 与 store 的 SQLite 路径处理一样 | 两份重复代码漂移 |

### 9.1 为什么 Store 要和 checkpointer 共用配置段

Store 和 checkpointer 是**两种不同的持久化需求**（跨 thread 长效记忆 vs 图状态快照），LangGraph 也给了两个接口。但它们几乎总是用**同一种数据库后端**——如果你的服务用 PostgreSQL 存 checkpoint，那 thread 列表 / 长期记忆也大概率存在同一个 PostgreSQL 里（运维上不会一个用 PG 一个用 SQLite）。

**不这么设计会怎样**：如果 Store 有独立的 `store` 配置段，用户得配两份（`checkpointer.type: postgres` + `store.type: postgres`，连同一个 DSN 写两遍）。一旦某天改了 checkpointer 的 DSN 忘了改 store 的，两者落到不同库，数据对不上。共用一个段让两者**恒用同一种技术**，配置漂移从根上不可能。

### 9.2 为什么 `get_store` 要双检锁

`get_store` 是同步单例，可能被多线程并发调用（CLI 工具、内嵌 client）。建 Store 要开数据库连接，不能建多个（连接泄漏 + 数据不一致）。双检锁（[provider.py:121-142](../backend/packages/harness/deerflow/runtime/store/provider.py#L121-L142)）：

```python
if _store is not None:      # ① 无锁检查（快路径，已建直接返回）
    return _store
with _store_lock:            # ② 进锁
    if _store is not None:   # ③ 再检查（防两个线程同时通过①）
        return _store
    ...建 store...           # ④ 真正建
```

**不这么设计会怎样**：
- 只有 ①（无锁）——两个线程同时通过检查、都建一个 Store，连接泄漏 + 两个实例状态不一致。
- 只有 ②③④（每次进锁）——已建的情况下每次调用都要争锁，慢。
- 双检锁：已建时无锁返回（快），没建时进锁再核验（只建一次）。

### 9.3 为什么用 soft-load 而不是顶层 import

`langgraph-checkpoint-sqlite` / `langgraph-checkpoint-postgres` 是**可选依赖**（用户可能只用 memory 后端）。如果在文件顶层 `from langgraph.store.sqlite import SqliteStore`，那只要没装 sqlite 包，整个 `store/provider.py` 就 import 失败——连带 memory 后端也用不了。

soft-load（[provider.py:68-71](../backend/packages/harness/deerflow/runtime/store/provider.py#L68-L71)）把 import 延迟到**真正要用 sqlite 后端时**，并在缺包时抛 `ImportError` 带**安装提示**（`uv add langgraph-checkpoint-sqlite`）。这样：memory 后端永远可用（不依赖可选包）；用户配了 sqlite 却没装包时得到**可操作的报错**（告诉他装哪个包），而不是一个含糊的 `ModuleNotFoundError`。

---

## 10. 实现差异（vs 上游 deer-flow 源码）

对照两侧 `backend/packages/harness/deerflow/runtime/store/`（4 个文件一一对应），**剥 docstring/comment 后判逻辑差**。结论：**Store 工厂是 checkpointer 工厂的忠实镜像移植——0 逻辑差，唯一实质差异是配置读取路径（与 #8 checkpointer 同一套模式），其余是 import 风格 + 辅助函数抽取 + API 面**。

### 10.1 配置读取路径（唯一实质差异，与 checkpointer 工厂同一模式）

- 上游 `provider.py` 的 `get_store` / `store_context` 先调 `ensure_config_loaded()`（确保 Gateway 配置加载），再调 `get_checkpointer_config()`（专门的 checkpointer 配置读取器）拿配置。
- **mini 直接 `get_app_config().checkpointer`**——因为 mini 没有 Gateway，lifespan 进时配置必然已加载，无需二次 ensure。

这是**语义等价**的差异：两边都是「读 checkpointer 配置 → 按 type 分支 → 建对应 Store → None fallback InMemoryStore」。mini 更简是因为省了 Gateway 的配置加载层。**这正是 #8 checkpointer 工厂的那处差异**（见 [checkpointer.md](checkpointer.md) §9），Store 工厂与之完全一致——两个工厂是平行移植。

### 10.2 辅助函数抽取 + 变量内联

- mini 把「没配 checkpointer 段」的 WARNING 抽成 `_no_checkpointer_warning()` 辅助（[provider.py:44](../backend/packages/harness/deerflow/runtime/store/provider.py#L44)）；上游内联 `logger.warning(...)`。等价。
- mini 在 `make_store` / `get_store` 里用局部变量 `ckpt_config = app_config.checkpointer`；上游内联 `app_config.checkpointer`。等价。
- mini 的 `async_provider` 从 `_sqlite_utils` 直接 import sqlite 工具；上游从 `provider` 转 import。等价。

### 10.3 import 风格 + API 面

- 上游用相对导入（`from .async_provider import ...`）；mini 用绝对导入（`from deerflow.runtime.store.async_provider import ...`）。等价。
- mini 的 `__init__.py`（stripped 52 vs 上游 26）**多导出** `_sqlite_utils` 的两个工具函数（`ensure_sqlite_parent_dir` / `resolve_sqlite_conn_str`）到包顶层，方便外部直接 import。API 面差异，无逻辑差。

### 10.4 `_sqlite_utils.py`：逐字节相同

stripped 88 = 88，`resolve_sqlite_conn_str` + `ensure_sqlite_parent_dir` 两个纯函数与上游一字不差（与 checkpointer 共用的同一份工具）。

**测试覆盖**：`test/test_store.py`（13 测试），覆盖三入口、memory/sqlite/postgres 分支、None fallback、双检锁单例、`reset_store`。

---

## 11. 排错 FAQ

- **「Thread list will be lost on server restart」WARNING**：没配 `checkpointer` 段，Store 用 InMemoryStore。要持久化配 `checkpointer.type: sqlite`（或 postgres）。
- **「langgraph-checkpoint-sqlite is required」**：配了 sqlite 后端但没装包。`uv add langgraph-checkpoint-sqlite`。
- **「checkpointer.connection_string is required for the postgres backend」**：postgres 后端必填 DSN。
- **「Store 和 checkpointer 用了不同后端」**：不会发生——两者读同一个 `checkpointer` 段。若你手动建了不同后端的 Store，那是绕过了工厂。
- **「换了配置 Store 没重建」**：`get_store` 是单例，配置变更后调 `reset_store()` 强制重建。
- **「CLI 脚本里 Store 连接没关」**：用 `store_context()`（一次性 CM，块退出自动关）而非 `get_store()`（单例，进程退出才关）。

---

**下一篇**：[architecture.md](architecture.md)（#28，集成装配总览）——本模块的 `make_store` 经 `runtime_lifespan` 串进完整 bundle，和 checkpointer / stream_bridge / run_manager 一起按正确顺序建好、关停时 drain。这是 Phase 0–8 文档的完结篇。
