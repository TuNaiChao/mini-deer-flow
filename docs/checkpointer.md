# 8. checkpointer.md — 检查点工厂（委托 LangGraph Saver，不自建）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：checkpointer 模块负责把 LangGraph 图的「**执行状态快照**」存起来，让一次对话能**跨轮次恢复、能回放、能断点续跑**。它是「对话记忆」的仓库——和 [#7 persistence.md](persistence.md) 的「谁跑过什么」账本**不同**（两者物理分离，即使共用一个 `.db` 文件，表也不重叠）。本模块**不自己实现存储**，而是**委托 LangGraph 内置的 Saver**——只负责「按配置挑哪个 Saver、管理它的生命周期」。

> 配套代码：[runtime/checkpointer/provider.py](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py)（同步工厂）· [runtime/checkpointer/async_provider.py](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py)（异步工厂）· [runtime/store/_sqlite_utils.py](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py)（sqlite 工具）· [config/checkpointer_config.py](../backend/packages/harness/deerflow/config/checkpointer_config.py) · [config/database_config.py](../backend/packages/harness/deerflow/config/database_config.py)。测试见 [test/test_checkpointer.py](../test/test_checkpointer.py)。

## 学完这篇你能回答什么（learning outcomes）

- LangGraph 的 **checkpoint / channel / thread_id** 是什么？为什么 Agent 需要逐步打状态快照（断点恢复 / 回放调试 / 跨轮次记忆）？
- checkpointer 为什么**委托 LangGraph 内置 Saver 而不自建** `BaseCheckpointSaver` 子类（序列化 / 版本管理 / 并发控制太复杂，要跟 LangGraph 版本走）？
- 三种 Saver（`InMemorySaver` / `SqliteSaver` / `PostgresSaver`）各自存哪、依赖哪个可选包、什么场景用？
- `make_checkpointer` 的**三级优先级**（legacy `checkpointer:` > 统一 `database:` > 默认 `InMemorySaver`）为什么 legacy 优先（后向兼容 + 显式覆盖不被悄悄改）？
- 为什么 **async 用 context manager、sync 用单例**（Saver 持有连接/池资源需成对 setup/teardown；图编译要稳定引用）？
- sqlite 路径准备（建父目录）为什么在 async 里要 `await asyncio.to_thread`（阻塞文件 IO 卸载，防卡事件循环）？
- checkpointer 表与 app 持久化表为什么**物理分离**（即使共用一个 `.db` 文件，各管各的表，schema 演进互不牵制）？

> 这些都是 LangGraph / agent 工程面试的高频点——「状态持久化怎么设计」「资源生命周期管理」「配置优先级与后向兼容」。

---

## 1. 为什么需要它（痛点）

先看「没有它」会怎样：

- **每条消息都是一次性的**。AI 回完，状态就没了。你问「继续刚才那个」，它完全不记得上一轮说了什么。
- **长任务一断就废**。一个跑 10 分钟的深度研究任务，网络一抖断开，全部重来。
- **无法回放 / 调试**。出问题时想看「第 5 步时图的状态是什么」，没有快照就无从查起。

LangGraph 用 **checkpoint（检查点）** 机制解决：图每走一步，把当前所有「频道（channel）」的值打个快照存下来。下次用同一个 `thread_id` 调用，就从最近一次快照恢复。**checkpointer 就是「存这些快照的东西」**。

本模块**不自己实现存储**，而是**委托 LangGraph 内置的 Saver**（`InMemorySaver` / `SqliteSaver` / `PostgresSaver`）——只负责「按配置挑哪个 Saver、管理它的生命周期」。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉 LangGraph 状态管理的话，先读这一节。（LangGraph 图/节点/边的概念见 deerflow-book 第 4 章，或 [#0 start-here.md](start-here.md)。）

### thread_id / channel / checkpoint

- **thread_id（线程 id）**：一次连续对话的唯一标识。同一个 thread_id 的消息共享状态。
- **channel（频道）**：LangGraph 图里的「状态槽」。比如 `messages`（消息列表）、`sandbox`（沙箱信息）都是 channel。图的状态 = 一组 channel 的当前值。
- **checkpoint（检查点）**：某一时刻**所有 channel 值的快照** + 元数据（第几步、谁写的）。每走一步产生一个新的 checkpoint。

类比：**thread 是「存档槽」，checkpoint 是「存档点」，channel 是存档里的各项数据，checkpointer 是「存档系统」**。

### Saver（保存器）

LangGraph 内置三种 Saver，本模块按配置选用：

| Saver | 存哪 | 包 | 何时用 |
|-------|------|-----|--------|
| `InMemorySaver` | 进程内存 | langgraph 自带 | 开发、测试；重启即失 |
| `SqliteSaver` / `AsyncSqliteSaver` | 一个本地文件 | `langgraph-checkpoint-sqlite`（可选 extra） | 单节点部署、本地持久化 |
| `PostgresSaver` / `AsyncPostgresSaver` | PostgreSQL 服务 | `langgraph-checkpoint-postgres`（可选 extra） | 生产、多节点 |

### setup()

`SqliteSaver` / `PostgresSaver` 在首次使用前要 **`setup()`**——建表（`checkpoints`、`checkpoint_blobs`、`checkpoint_writes` 等）。`InMemorySaver` 不需要 setup（内存里没表概念）。本模块在创建 Saver 后自动调 `setup()`（sqlite/postgres 分支，如 [provider.py:74](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L74)）。

### context manager（上下文管理器）vs 单例

本模块提供两种生命周期模式：

- **异步 context manager**（`make_checkpointer`）：`async with` 块内用，退出时自动关连接。给长期 async 服务（FastAPI lifespan）用。**无全局状态**。
- **同步单例**（`get_checkpointer` / `reset_checkpointer`）：进程级缓存一个实例，`reset` 时关。给 CLI、图编译、嵌入式客户端用。
- **同步 context manager**（`checkpointer_context`）：每次 `with` 新建+销毁，不缓存。给想要确定性清理的脚本用。

---

## 3. 整体结构：它在系统里的位置

```
runtime/
├── store/
│   ├── __init__.py          # 转发 _sqlite_utils（完整 Store 工厂在 #27）
│   └── _sqlite_utils.py     # resolve_sqlite_conn_str + ensure_sqlite_parent_dir
└── checkpointer/
    ├── __init__.py           # 导出 get/reset/context + make_checkpointer
    ├── provider.py           # 同步：_sync_checkpointer_cm + get_checkpointer 单例 + reset + checkpointer_context + 安装提示常量
    └── async_provider.py     # 异步：_async_checkpointer + _async_checkpointer_from_database + make_checkpointer（三级优先级）+ postgres 连接池
```

它在系统里的位置（与 persistence 平行，各管各的表）：

```
config/{checkpointer_config, database_config, app_config}
                              │
config/paths.resolve_path ──→ runtime/store/_sqlite_utils ──┐
                                                              ▼
                              runtime/checkpointer/{provider, async_provider}
                                                              │
                                   委托 └→ langgraph.checkpoint.{memory, sqlite, postgres}
                                                              │
                              agents/factory 把 cp 传给 graph.compile(checkpointer=cp)   #25
                                                              │
                              lifespan 装配（#28）+ langgraph.json checkpointer.path
```

> **旁注**：`runtime/store/` 的完整 LangGraph `BaseStore` 工厂（与 checkpointer 平行、跨线程记忆用）在 [#27 runtime_store.md](runtime_store.md) 落地。本模块只建 `_sqlite_utils`（两者共用）。

---

## 4. 核心概念

### 4.1 委托，不自建（关键决策）

我们**不**写 `BaseCheckpointSaver` 的子类。原因：LangGraph 的 Saver 涉及复杂的序列化（channel 值可能是任意 Python 对象）、版本管理、并发控制——自己实现极易出错且要跟 LangGraph 版本走。内置 Saver 官方维护、经过充分测试。**我们只在「挑哪个 + 生命周期管理」这层加值。**

### 4.2 三级优先级（legacy `checkpointer:` > 统一 `database:` > 默认）

`make_checkpointer`（[async_provider.py:172](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L172)）按这个顺序挑后端：

1. **legacy `checkpointer:` 配置段**（[第 191 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L191)）——独立的、显式的 checkpointer 配置（对应 `langgraph.json` 的 `checkpointer:` 段）。**最高优先级**，设了就用它，覆盖一切。
2. **统一 `database:` 配置段**（[第 197 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L197)）——当 `database.backend != "memory"` 时，用同一个数据库（sqlite/postgres）。这样用户只配一个 `database` 段，checkpointer 和 app 持久化自动共用。
3. **默认 `InMemorySaver`**（[第 204 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L204)）——都没配时，进程内存（不持久化）。

为什么 legacy 优先？**后向兼容 + 显式覆盖**。老配置里独立写了 `checkpointer:` 的用户，升级到统一 `database:` 后，他们的显式选择不能被悄悄改掉。

### 4.3 app 表与 checkpoint 表物理分离

即使 sqlite 模式下 checkpointer 和 app 持久化（[#7 persistence.md](persistence.md)）**共用同一个 `.db` 文件**，表也不重叠：

- checkpointer 的表（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes`）由 SqliteSaver 自己建、自己管。
- app 的表（`runs` / `threads_meta` / `run_events`）由我们的 `Base.metadata` 管。

各管各的，schema 演进互不牵制。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 同步工厂 `_sync_checkpointer_cm`（[provider.py:52](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L52)）

按 `config.type` 分三支 yield 一个配好的 Saver：

| 分支 | 行号 | 做什么 |
|------|------|--------|
| memory | [58-63](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L58) | `yield InMemorySaver()`（进程内，不持久化） |
| sqlite | [65-77](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L65) | 缺包抛 `ImportError(SQLITE_INSTALL)`；`resolve_sqlite_conn_str` 解析路径；`ensure_sqlite_parent_dir` 建父目录；`with SqliteSaver.from_conn_string(conn_str) as saver: saver.setup(); yield saver` |
| postgres | [79-92](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L79) | 缺包抛 `ImportError(POSTGRES_INSTALL)`；无 `connection_string` 抛 `ValueError`；`with PostgresSaver.from_conn_string(...) as saver: saver.setup(); yield saver` |

三个错误信息常量（`SQLITE_INSTALL` / `POSTGRES_INSTALL` / `POSTGRES_CONN_REQUIRED`）定义在 [第 41-43 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L41)，async_provider 也 import 复用——缺包时给出**可操作的安装命令**（`uv sync --all-packages --extra sqlite/postgres`），而非晦涩的 `ModuleNotFoundError`。

### 5.2 同步单例 `get_checkpointer`（[provider.py:106](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L106)）

返回进程级缓存的 checkpointer 实例。两个细节：

- **配置读取放在锁外**（[第 124 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L124)）：`get_app_config()` 可能读盘（缓存未命中或 mtime 变化），放在锁外避免「持锁读盘」拖慢其它线程的单例访问。
- **双重检查锁**（[第 120 行 fast-path + 第 126 行 in-lock recheck](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L120)）：先无锁查一次缓存，命中直接返回；进锁后再查一次防并发重复创建。
- `config is None`（没配 checkpointer）→ `InMemorySaver`（[第 130-135 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L130)）。

注意单例模式有个副作用：它把 `_sync_checkpointer_cm` 这个 context manager **长期 `__enter__` 不 `__exit__`**（[第 137-140 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L137)），靠 `reset_checkpointer`（[第 145 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L145)）在退出时补 `__exit__` 关连接——测试 / 配置变更时调。

### 5.3 异步工厂 `make_checkpointer`（[async_provider.py:172](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L172)）

公开的异步 context manager，三级优先级见 §4.2。两条底层路径：

- **`_async_checkpointer`**（[第 95 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L95)）：从 legacy `CheckpointerConfig` 构造。sqlite 分支的路径准备走 `await asyncio.to_thread(_prepare_sqlite_checkpointer_path, ...)`（[第 110 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L110)）——阻塞 IO 卸载（§6.3）。
- **`_async_checkpointer_from_database`**（[第 136 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L136)）：从统一 `DatabaseConfig` 构造，用 `db_config.checkpointer_sqlite_path`（[第 50 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L50)）。

### 5.4 postgres 连接池（[async_provider.py:55-72](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L55)）

postgres Saver 不用单连接，而用 `psycopg_pool.AsyncConnectionPool`，配三组生产加固参数：

- `keepalives_idle=60`（[第 67 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L67)）：60 秒空闲发 TCP keepalive，防云数据库 / 防火墙静默断连。
- `check=AsyncConnectionPool.check_connection`（[第 71 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L71)）：从池里取连接前先验活。
- `prepare_threshold=0`（[第 64 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L64)）：禁用 prepared statement 缓存（避免 pgbouncer / 连接池场景下的 prepared statement 冲突）。

### 5.5 sqlite 工具（[_sqlite_utils.py](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py)）

两个纯函数，store 与 checkpointer 共用：

- `resolve_sqlite_conn_str(raw)`（[第 19 行](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py#L19)）：`":memory:"` / `file:` URI 原样返回；普通路径经 `resolve_path` 转绝对。
- `ensure_sqlite_parent_dir(conn_str)`（[第 30 行](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py#L30)）：`mkdir(parents=True, exist_ok=True)` 建父目录；对 `":memory:"` / `file:` 是 no-op。

---

## 6. 设计权衡与踩坑

### 6.1 为什么 async 用 context manager、sync 用单例

- **async 服务**（FastAPI lifespan）：`async with make_checkpointer()` 在启动时开连接、关闭时干净释放。用 context manager 而非「工厂返回实例」，是因为 Saver 持有连接/池资源，需要**成对的 setup/teardown**——context manager 的 `__aenter__`/`__aexit__` 天然配对，不会漏掉清理。
- **sync CLI / 图编译**：进程内复用一个实例最方便（图编译时 `compile(checkpointer=...)` 要稳定引用）。单例 + 显式 `reset`（测试 / 配置变更时）。

### 6.2 父目录保护

如果 sqlite 文件的父目录不存在（比如第一次跑、`.deer-flow/` 还没建），`SqliteSaver.from_conn_string` 会抛 `unable to open database file`。所以 `ensure_sqlite_parent_dir` 在连接**之前**创建父目录。对 `":memory:"` 和 `file:` URI 是 no-op（它们没有「父目录」的概念）。测试 `test_sync_sqlite_creates_parent_dir` 用深层不存在的路径锁住这个契约。

### 6.3 阻塞 IO 卸载

sqlite 路径准备（`mkdir` 父目录 + 解析绝对路径）是**阻塞文件 IO**。在 async 服务里直接调会卡住事件循环——这正是 mini 自己 blocking-IO gate 要拦的（详见 [#2 testing-setup.md](testing-setup.md)）。所以异步路径用 `await asyncio.to_thread(_prepare_sqlite_checkpointer_path, ...)`（[第 110 行](../backend/packages/harness/deerflow/runtime/checkpointer/async_provider.py#L110)）把它丢到线程池。测试 `test_legacy_sqlite_path_prep_uses_to_thread` 锁住这个契约。

### 6.4 缺包软加载 + 可操作提示

sqlite/postgres 的 Saver 包是**可选 extra**。没装时不能报一个晦涩的 `ModuleNotFoundError`，而要报**可操作的安装命令**（[第 41-43 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L41)）。实现是 `try: from langgraph.checkpoint.sqlite import SqliteSaver except ImportError: raise ImportError(SQLITE_INSTALL)`。

### 6.5 mini 直读 config vs 上游包装（结构选择）

mini 的 `get_checkpointer().checkpointer` 直接调 `get_app_config()` 读配置（[第 124 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L124)）——因为 mini 走 `langgraph dev` 启动，进 lifespan 时配置已加载好。上游 deer-flow 有 Gateway 层、不保证配置已加载，所以多包了一层 `ensure_config_loaded()` + `get_checkpointer_config()`。**语义等价，只是 mini 无 Gateway 故走直读**。

---

## 7. 配置与用法

### 7.1 memory 模式（默认，什么都不配）

```python
import asyncio
from deerflow.runtime.checkpointer import make_checkpointer
from deerflow.config.app_config import AppConfig

async def main():
    async with make_checkpointer(AppConfig()) as cp:
        # cp 是 InMemorySaver
        config = {"configurable": {"thread_id": "1", "checkpoint_ns": ""}}
        print(await cp.aget_tuple(config))  # None（初始无快照）

asyncio.run(main())
```

### 7.2 sqlite 模式（持久化）

config.yaml：

```yaml
database:
  backend: sqlite
  sqlite_dir: .deer-flow/data
```

```python
from deerflow.runtime.checkpointer import make_checkpointer
from deerflow.config import get_app_config

async with make_checkpointer(get_app_config()) as cp:
    # cp 是 AsyncSqliteSaver，指向 .deer-flow/data/deerflow.db
    ...  # 交给 LangGraph 图用：graph.compile(checkpointer=cp)
```

### 7.3 在 lifespan 里装配（[#28 architecture.md](architecture.md) 集成）

```python
from contextlib import asynccontextmanager
from deerflow.runtime.checkpointer import make_checkpointer

@asynccontextmanager
async def lifespan(app):
    async with make_checkpointer() as cp:
        app.state.checkpointer = cp
        yield
    # 退出时自动关连接
```

### 7.4 对应配置（两个可选来源）

```yaml
# 方式 A：统一 database 段（推荐，checkpointer + app 共用一个 .db）
database:
  backend: sqlite              # memory | sqlite | postgres
  sqlite_dir: .deer-flow/data

# 方式 B：独立 checkpointer 段（legacy，优先级更高，显式覆盖）
checkpointer:
  type: sqlite
  connection_string: .deer-flow/checkpoints.db   # 可省略，默认 store.db
```

（`DatabaseConfig` 派生 `sqlite_path` / `checkpointer_sqlite_path` / `app_sqlalchemy_url` 的逻辑见 [#3 config.md](config.md) §6.2。）

---

## 8. 与其它模块的关系

- **依赖**：[#3 config](config.md)（`CheckpointerConfig` / `DatabaseConfig` / `AppConfig`）、`config/paths`（`resolve_path`）、（可选）`langgraph.checkpoint.{sqlite, postgres}` 包。
- **被依赖**：
  - [#25 agents.md](agents.md)：`make_lead_agent` 把 checkpointer 传给 `graph.compile(checkpointer=cp)`。
  - [#28 architecture.md](architecture.md)：lifespan 里 `make_checkpointer` 装配，drain 时关连接。
  - [#26 runs.md](runs.md)：worker 的 rollback 快照需要读 checkpoint 恢复图状态。
  - [#27 runtime_store.md](runtime_store.md)：与 checkpointer 平行的 LangGraph Store（跨线程记忆），共用 `_sqlite_utils`。
- **与 persistence 的区别**：checkpointer 管「图状态快照」（对话内容、节点输出）；persistence 管「应用元数据」（谁跑的、token 用量、线程归属）。**共用 .db 文件但表分离**（§4.3）。

---

## 9. 常见问题 / 排错

**Q: 报 `unable to open database file`？**
A: sqlite 文件的父目录不存在。本模块的 `ensure_sqlite_parent_dir` 会自动建（§6.2）——如果你绕过了 `make_checkpointer` 直接调 `SqliteSaver.from_conn_string`，就要自己先建目录。检查 `sqlite_dir` 是否可写。

**Q: 报 `langgraph-checkpoint-sqlite 未安装`？**
A: sqlite Saver 是可选包。安装：`cd backend && uv sync --all-packages --extra sqlite`。或改用 `backend: memory`（不需该包，但不持久化）。

**Q: 改了 `database.backend` 但 checkpointer 没变？**
A: 检查是否同时设了 legacy `checkpointer:` 段——它优先级更高（§4.2），会覆盖 `database`。想让 `database` 生效，就删掉/注释掉独立的 `checkpointer:` 段。

**Q: `InMemorySaver` 没有 `setup()` 方法？**
A: 对。只有 sqlite/postgres Saver 需要 `setup()`（建表）。memory 没有。本模块在对应分支自动处理——memory 分支不调 setup。

**Q: 同步单例 `get_checkpointer()` 在测试间互相污染？**
A: 它是进程级全局。测试里用 `reset_checkpointer()`（[第 145 行](../backend/packages/harness/deerflow/runtime/checkpointer/provider.py#L145)）在前后清理。

**Q: postgres 报 `psycopg_pool` / `AsyncPostgresSaver` 找不到？**
A: postgres checkpointer 需要 `langgraph-checkpoint-postgres` + `psycopg` + `psycopg-pool`，都在 `postgres` extra 里：`cd backend && uv sync --all-packages --extra postgres`。

**Q: checkpoint 里存的内容是加密的吗？**
A: 不。checkpoint 存的是图状态（含消息内容），明文落盘。敏感数据（API key 等）不应放进图 state。

---

## 小结

checkpointer 模块的精髓是**委托而非自建，把「挑哪个 Saver + 生命周期管理」这层做厚**。记住四件事：

1. **委托**：不自建 `BaseCheckpointSaver` 子类，用 LangGraph 内置 `InMemorySaver` / `SqliteSaver` / `PostgresSaver`——它们管复杂的序列化 / 版本 / 并发。
2. **三级优先级**：legacy `checkpointer:` > 统一 `database:`（非 memory）> 默认 `InMemorySaver`——后向兼容 + 显式覆盖不被悄悄改。
3. **两种生命周期**：async 用 context manager（成对 setup/teardown）；sync 用单例（图编译要稳定引用）+ 显式 reset。
4. **运维细节收口**：sqlite 父目录保护、阻塞 IO 卸载（`to_thread`）、缺包可操作提示、postgres 连接池 + TCP keepalive——这些坑都自动处理。

上一篇：[#7 persistence.md](persistence.md)（应用持久化层——与 checkpointer 共用 .db 但表分离）· 下一篇：[#9 run_event_store.md](run_event_store.md)（运行事件存储——用 persistence 建的 RunEventRow 表，存消息 + 轨迹，seq 单调 + 路径穿越防御）。
