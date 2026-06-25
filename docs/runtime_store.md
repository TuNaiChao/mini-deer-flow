# 27. runtime_store.md — LangGraph Store 工厂（与 checkpointer 平行的跨线程记忆）

> **一句话定位**：本模块是「长效记忆层」工厂——给 agent 一个跨 thread、跨 run 的键值存储
> （`BaseStore`），存 thread 列表、长期数据。和 checkpointer（存图状态快照）是**两种不同的
> 持久化需求**，但 mini 让它们共用同一个 ``checkpointer`` 配置段，用同一种后端技术。

读完 [runs.md](runs.md)（懂了「RunManager + worker 怎么跑一次 run」）再看本篇最省事——本篇回答
「run 之外的那些**跨 run 数据**（thread 列表、记忆）存在哪、怎么建」。worker 的
`RunContext.store` 就是这里建的 Store。

---

## 0. 这个模块解决什么问题

agent 跑一次 run 有两类数据要存：

1. **图状态快照**（按 thread + checkpoint_id）：对话走到哪一步、消息历史、中间变量——这是
   **checkpointer** 的活（M5）。run 之间靠它恢复对话。
2. **跨 thread 的长效数据**（按 namespace + key）：thread 列表本身、用户长期偏好、跨会话事实——
   这是 **Store** 的活（本模块）。它不绑某次 run，是 agent 的「长期记忆」。

两者都是持久化需求，但语义不同。LangGraph 把它们分成两个接口（`BaseCheckpointSaver` vs
`BaseStore`）。mini 让它们**共用同一个 ``checkpointer`` 配置段**——同一份连接配置、一套运维，
最省事。

## 1. Store vs checkpointer（一图看清）

| 维度 | checkpointer（M5） | Store（M19，本模块） |
|------|--------------------|----------------------|
| 存什么 | 图状态快照 | 跨 thread 长效键值 |
| 粒度 | (thread_id, checkpoint_id) | (namespace, key) |
| 生命周期 | 每次 run 写 / 读 | 跨 run / 跨 thread |
| 典型用途 | 恢复对话、rollback | thread 列表、长期记忆、thread_meta |
| 接口 | `aput / aget_tuple / adelete_thread` | `put / get / search / delete` |
| 配置段 | `checkpointer` | `checkpointer`（**共用**） |
| 实现 | InMemorySaver / SqliteSaver / PostgresSaver | InMemoryStore / SqliteStore / PostgresStore |

**为什么共用配置？** 同一种数据库后端最省运维。配 `checkpointer.type: sqlite` 时，checkpointer
和 Store 都用 sqlite（各自的表 / 文件）；配 memory 都内存。mini 的 Store 工厂读同一个
`app_config.checkpointer` 段，保证两者恒用同一种技术。

## 2. 三个入口（异步 / 同步单例 / 同步一次性）

[store/](../backend/packages/harness/deerflow/runtime/store/) 提供三套 API，对应不同调用场景：

### `make_store(app_config)` — 异步上下文管理器（lifespan / 长跑服务用）

```python
from deerflow.runtime.store import make_store

async with make_store(app_config) as store:
    bundle.store = store
```

读 `app_config.checkpointer`：None → `InMemoryStore`（发 WARNING）；否则按 `type` 建对应 Store。
sqlite/postgres 缺包抛 `ImportError` 带安装提示（soft-load，红线 #24）。

### `get_store()` — 同步单例（CLI / 内嵌 client 用）

```python
from deerflow.runtime.store import get_store

store = get_store()   # 首次创建，之后复用；进程退出才关
```

线程安全（`threading.Lock` 双检锁）。`reset_store()` 清单例（测试 / 配置变更后强制重建）。

### `store_context()` — 同步一次性上下文管理器（CLI 脚本 / 测试用）

```python
from deerflow.runtime.store import store_context

with store_context() as store:   # 每次新建，块退出即关（不缓存）
    store.put(("threads",), thread_id, {...})
```

与 `get_store` 不同：**不缓存**，每个 `with` 块自建自毁一个连接。想要确定性清理时用。

## 3. 后端选择（镜像 checkpointer）

[provider.py](../backend/packages/harness/deerflow/runtime/store/provider.py) + [async_provider.py](../backend/packages/harness/deerflow/runtime/store/async_provider.py) 的 `_sync_store_cm` / `_async_store` 按 `config.checkpointer.type` 分支：

| type | 同步 Store | 异步 Store | 缺包 |
|------|-----------|-----------|------|
| `memory` | `InMemoryStore` | `InMemoryStore` | — |
| `sqlite` | `SqliteStore` | `AsyncSqliteStore` | `langgraph-checkpoint-sqlite` |
| `postgres` | `PostgresStore` | `AsyncPostgresStore` | `langgraph-checkpoint-postgres` |

sqlite 用 `_sqlite_utils.resolve_sqlite_conn_str` + `ensure_sqlite_parent_dir`（与 checkpointer
共用，M5 已建）。postgres 要 `connection_string`（缺了抛 `ValueError`）。

**没配 `checkpointer` 段时**：发 WARNING 并 fallback 到 `InMemoryStore`（thread 列表重启丢失）。
这和 checkpointer 的「None → InMemorySaver」对称——开箱即用（红线 #25）。

## 4. worker 怎么用 Store

worker（[runs/worker.py](../backend/packages/harness/deerflow/runtime/runs/worker.py)）的
`RunContext.store` 装着本模块建的 Store，两处用到：

1. **构建 agent 时**：`agent.store = store`——让 agent 图里的节点能经 `ToolRuntime.context.store`
   或 langgraph 的 store 接口读写跨 thread 数据；
2. **thread_meta 回退**：memory 后端无 SQL 时，`MemoryThreadMetaStore(store)` 用 Store 存 thread
   元数据（标题 / 状态）。

`RunContext.store` 在 M18 已预留字段，本模块（M19）落地后 worker 的 `agent.store = store` 自动生效。

## 5. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **checkpointer(M5)** | 共用 `checkpointer` 配置段 + `_sqlite_utils`；同一种后端技术 |
| **runs/worker(M18)** | `RunContext.store` ← 本模块；worker 挂 `agent.store = store` |
| **runs/lifespan(M19 集成)** | `make_store(app_config)` 进 AsyncExitStack，yield 进 bundle |
| **persistence/thread_meta(M4)** | memory 后端的 `MemoryThreadMetaStore(store)` 用 Store 存 thread 元数据 |
| **config/checkpointer_config** | `CheckpointerConfig(type, connection_string)`——Store 读它 |

## 6. 设计要点回顾

1. **Store ≠ checkpointer**：跨 thread 长效记忆 vs 图状态快照，两种持久化需求。
2. **共用配置段**：Store 读 `app_config.checkpointer`，与 checkpointer 同一种后端。
3. **三入口**：`make_store`（异步 CM）/ `get_store`（同步单例）/ `store_context`（同步一次性）。
4. **soft-load**（红线 #24）：sqlite/postgres 缺包抛 `ImportError` 带安装提示。
5. **None → InMemoryStore**：开箱即用（红线 #25），发 WARNING 提示重启丢失。
6. **双检锁单例**：`get_store` 线程安全，`reset_store` 清缓存。

## 7. 排错 FAQ

- **「Thread list will be lost on server restart」WARNING**：没配 `checkpointer` 段，Store 用
  InMemoryStore。要持久化配 `checkpointer.type: sqlite`（或 postgres）。
- **「langgraph-checkpoint-sqlite is required」**：配了 sqlite 后端但没装包。`uv add
  langgraph-checkpoint-sqlite`。
- **「checkpointer.connection_string is required for the postgres backend」**：postgres 后端必填
  DSN。
- **「Store 和 checkpointer 用了不同后端」**：不会发生——两者读同一个 `checkpointer` 段。若你手动
  建了不同后端的 Store，那是绕过了工厂。

---

**下一篇**：[architecture.md](architecture.md)（#28，集成装配总览）——本模块的 `make_store` 经
[runtime/lifespan](../backend/packages/harness/deerflow/runtime/lifespan.py) 的 `runtime_lifespan`
串进完整 bundle，和 checkpointer / stream_bridge / run_manager 一起按正确顺序建好、关停时 drain。
