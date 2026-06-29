# 8. checkpointer.md — 检查点工厂（LangGraph 状态持久化）

> 配套代码：[backend/packages/harness/deerflow/runtime/checkpointer/](../backend/packages/harness/deerflow/runtime/checkpointer/) + [runtime/store/_sqlite_utils.py](../backend/packages/harness/deerflow/runtime/store/_sqlite_utils.py)
> 配套测试：[test/test_checkpointer.py](../test/test_checkpointer.py)
> 本文面向「刚接触 LangGraph 状态管理的小白」。每个名词第一次出现都会解释。

> **Phase 1 全维重审（2026-06-29）**：逐文件 diff `checkpointer/{__init__,provider,async_provider}.py`
> vs 最新上游。**勘误 §3.1-A**：上游漂移清单曾记「mini 缺 `async_provider.py`」——**过时**，mini 早已有
> 该文件且与上游 AST 级零漂移（异步 checkpointer provider 完整）。`provider.py` 的差异是
> M19 已认定并记录的**有意结构选择**：mini `get_app_config().checkpointer` 直读（lifespan 进时
> 配置已加载），上游用 `ensure_config_loaded()` + `get_checkpointer_config()` 包装（Gateway 上下文
> 里不保证配置已加载）——语义等价，mini 无 Gateway 故走直读。其余差异为 install hint 文案
> （中英）+ `__init__.py` 相对/绝对 import 顺序，均 cosmetic。无需补丁。

---

## 1. 一句话定位

**checkpointer 模块负责把 LangGraph 图的「执行状态快照」存起来，让一次对话能跨轮次恢复、能回放、能断点续跑。**

它是「对话记忆」的仓库——和 [persistence.md](persistence.md) 的「谁跑过什么」账本**不同**（两者物理分离，即使共用一个 `.db` 文件，表也不重叠）。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样：

- **每条消息都是一次性的**。AI 回完，状态就没了。你问「继续刚才那个」，它完全不记得上一轮说了什么。
- **长任务一断就废**。一个跑 10 分钟的深度研究任务，网络一抖断开，全部重来。
- **无法回放 / 调试**。出问题时想看「第 5 步时图的状态是什么」，没有快照就无从查起。

LangGraph 用 **checkpoint（检查点）** 机制解决：图每走一步，把当前所有「频道（channel）」的值打个快照存下来。下次用同一个 `thread_id` 调用，就从最近一次快照恢复。**checkpointer 就是「存这些快照的东西」**。

本模块**不自己实现存储**，而是**委托 LangGraph 内置的 Saver**（InMemorySaver / SqliteSaver / PostgresSaver）——只负责「按配置挑哪个 Saver、管理它的生命周期」。

---

## 3. 核心概念（名词 + 类比）

### 3.1 checkpoint / channel / thread_id

- **thread_id（线程 id）**：一次连续对话的唯一标识。同一个 thread_id 的消息共享状态。
- **channel（频道）**：LangGraph 图里的「状态槽」。比如 `messages`（消息列表）、`sandbox`（沙箱信息）都是 channel。图的状态 = 一组 channel 的当前值。
- **checkpoint（检查点）**：某一时刻**所有 channel 值的快照** + 元数据（第几步、谁写的）。每走一步产生一个新的 checkpoint。

类比：thread 是「存档槽」，checkpoint 是「存档点」，channel 是存档里的各项数据。checkpointer 是「存档系统」。

### 3.2 Saver（保存器）

LangGraph 内置三种 Saver，本模块按配置选用：

| Saver | 存哪 | 包 | 何时用 |
|-------|------|-----|--------|
| `InMemorySaver` | 进程内存 | langgraph 自带 | 开发、测试；重启即失 |
| `SqliteSaver` / `AsyncSqliteSaver` | 一个本地文件 | `langgraph-checkpoint-sqlite`（可选 extra） | 单节点部署、本地持久化 |
| `PostgresSaver` / `AsyncPostgresSaver` | PostgreSQL 服务 | `langgraph-checkpoint-postgres`（可选 extra） | 生产、多节点 |

**关键决策：委托，不自建。** 我们**不**写 `BaseCheckpointSaver` 的子类。原因：LangGraph 的 Saver 涉及复杂的序列化（channel 值可能是任意 Python 对象）、版本管理、并发控制——自己实现极易出错且要跟 LangGraph 版本走。内置 Saver 官方维护、经过充分测试。我们只在「挑哪个 + 生命周期管理」这层加值。

### 3.3 setup()

`SqliteSaver` / `PostgresSaver` 在首次使用前要 **`setup()`**——建表（`checkpoints`、`checkpoint_blobs` 等）。`InMemorySaver` 不需要 setup（内存里没表概念）。本模块在创建 Saver 后自动调 `setup()`（sqlite/postgres 分支）。

### 3.4 context manager（上下文管理器）vs 单例

本模块提供两种生命周期模式：

- **异步 context manager**（`make_checkpointer`）：`async with` 块内用，退出时自动关连接。给长期 async 服务（FastAPI lifespan）用。**无全局状态**。
- **同步单例**（`get_checkpointer` / `reset_checkpointer`）：进程级缓存一个实例，`reset` 时关。给 CLI、图编译、嵌入式客户端用。
- **同步 context manager**（`checkpointer_context`）：每次 `with` 新建+销毁，不缓存。给想要确定性清理的脚本用。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 三级优先级（legacy > database > 默认）

`make_checkpointer` 按这个顺序挑后端：

1. **legacy `checkpointer:` 配置段**——独立的、显式的 checkpointer 配置（对齐 `langgraph.json` 的 `checkpointer:` 段）。**最高优先级**，设了就用它，覆盖一切。
2. **统一 `database:` 配置段**——当 `database.backend != "memory"` 时，用同一个数据库（sqlite/postgres）。这样用户只配一个 `database` 段，checkpointer 和 app 持久化自动共用。
3. **默认 `InMemorySaver`**——都没配时，进程内存（不持久化）。

为什么 legacy 优先？**后向兼容 + 显式覆盖**。老配置里独立写了 `checkpointer:` 的用户，升级到统一 `database:` 后，他们的显式选择不能被悄悄改掉。

### 4.2 阻塞 IO 卸载（红线 #1）

sqlite 路径准备（`mkdir` 父目录 + 解析绝对路径）是**阻塞文件 IO**。在 async 服务里直接调会卡住事件循环。所以异步路径用 `await asyncio.to_thread(_prepare_sqlite_checkpointer_path, ...)` 把它丢到线程池。

测试 `test_legacy_sqlite_path_prep_uses_to_thread` 锁住这个契约。

### 4.3 父目录保护（红线 #1912）

如果 sqlite 文件的父目录不存在（比如第一次跑、`.deer-flow/` 还没建），`SqliteSaver.from_conn_string` 会抛 `unable to open database file`。所以 `ensure_sqlite_parent_dir` 在连接**之前**创建父目录。

对 `":memory:"` 和 `file:` URI 是 no-op（它们没有「父目录」的概念）。

测试 `test_sync_sqlite_creates_parent_dir` 锁住这个（用深层不存在的路径）。

### 4.4 缺包软加载 + 可操作提示（红线 #24）

sqlite/postgres 的 Saver 包是**可选 extra**。没装时不能报一个晦涩的 `ModuleNotFoundError`，而要报**可操作的安装命令**：

```
langgraph-checkpoint-sqlite 未安装，SQLite checkpointer 不可用。
安装：cd backend && uv sync --all-packages --extra sqlite
```

实现用 `try: from langgraph.checkpoint.sqlite import SqliteSaver except ImportError: raise ImportError(SQLITE_INSTALL)`。

### 4.5 postgres 用连接池 + TCP keepalive

postgres Saver 不用单连接，而用 `psycopg_pool.AsyncConnectionPool`，并配：
- `keepalives_idle=60`：60 秒空闲发 TCP keepalive，防云数据库/防火墙静默断连。
- `check=AsyncConnectionPool.check_connection`：从池里取连接前先验活。
- `prepare_threshold=0`：禁用 prepared statement 缓存（避免 pgbouncer/连接池场景下的 prepared statement 冲突）。

这些是生产 postgres 部署的标准加固。mini 本期不连真实 postgres 测，但代码忠实移植，缺包时报提示。

### 4.6 app 表与 checkpoint 表物理分离

即使 sqlite 模式下 checkpointer 和 app 持久化（[persistence.md](persistence.md)）**共用同一个 `.db` 文件**，表也不重叠：
- checkpointer 的表（`checkpoints` / `checkpoint_blobs` / `checkpoint_writes`）由 SqliteSaver 自己建、自己管。
- app 的表（`runs` / `threads_meta` / `run_events`）由我们的 `Base.metadata` 管。

各管各的，schema 演进互不牵制。

### 4.7 为什么 async 用 context manager、sync 用单例

- **async 服务**（FastAPI）：生命周期由 lifespan 管。`async with make_checkpointer()` 在启动时开连接、关闭时干净释放。用 context manager 而非「工厂返回实例」，是因为 Saver 持有连接/池资源，需要成对的 setup/teardown——context manager 的 `__aenter__`/`__aexit__` 天然配对，不会漏掉清理。
- **sync CLI / 图编译**：进程内复用一个实例最方便（图编译时 `compile(checkpointer=...)` 要稳定引用）。单例 + 显式 `reset`（测试 / 配置变更时）。

---

## 5. 文件结构

```
runtime/
├── store/
│   ├── __init__.py          # 转发 _sqlite_utils（完整 Store 工厂在 M19）
│   └── _sqlite_utils.py     # resolve_sqlite_conn_str + ensure_sqlite_parent_dir（#1912）
└── checkpointer/
    ├── __init__.py           # 导出 get/reset/context + make_checkpointer
    ├── provider.py           # 同步：_sync_checkpointer_cm + get_checkpointer 单例 + reset + checkpointer_context + 安装提示常量
    └── async_provider.py     # 异步：_async_checkpointer + _async_checkpointer_from_database + make_checkpointer（三级优先级）+ postgres 连接池
```

**旁注**：`runtime/store/` 的完整 LangGraph `BaseStore` 工厂（与 checkpointer 平行、跨线程记忆用）在 **M19（Phase 8）** 落地。本 M5 只建 `_sqlite_utils`（两者共用）。

---

## 6. 关键接口 / 签名

### 异步 context manager（主入口）

```python
@asynccontextmanager
async def make_checkpointer(app_config: AppConfig | None = None) -> AsyncIterator[Checkpointer]:
    # 优先级：app_config.checkpointer > app_config.database(非memory) > InMemorySaver
```

### 同步

```python
get_checkpointer() -> Checkpointer          # 全局单例（首次创建）；未配 → InMemorySaver
reset_checkpointer() -> None                # 关连接 + 清单例
checkpointer_context() -> Iterator[...]     # 同步 cm（一次性，不缓存）
```

### sqlite 工具

```python
resolve_sqlite_conn_str(raw: str) -> str        # ":memory:"/file: 原样；路径转绝对
ensure_sqlite_parent_dir(conn_str: str) -> None  # 建父目录；:memory:/file: no-op
```

---

## 7. 应用方法（可跑 demo）

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

config.yaml:

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

### 7.3 在 lifespan 里装配（Phase 8 集成时）

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

### 7.4 langgraph.json 对齐（D.3 集成时补）

```json
{
  "graphs": { "lead_agent": "deerflow.agents:make_lead_agent" },
  "checkpointer": {
    "path": "./packages/harness/deerflow/runtime/checkpointer/async_provider.py:make_checkpointer"
  }
}
```

---

## 8. 与其它模块的关系（文字依赖图）

```
config/{checkpointer_config, database_config, app_config} ──→ checkpointer
                                                                 │
config/paths.resolve_path ──→ runtime/store/_sqlite_utils ──┤
                                                                 ▼
                              runtime/checkpointer/{provider, async_provider}
                                                                 │
                                   委托 └→ langgraph.checkpoint.{memory, sqlite, postgres}
                                                                 │
                              （未来）agents/factory 把 cp 传给 graph.compile(checkpointer=cp)
                                                                 │
                              （未来）lifespan 装配（Phase 8）+ langgraph.json checkpointer.path
```

- **被谁依赖**：未来的 agent factory（图编译时传 checkpointer）、lifespan、runs worker（rollback 快照需要读 checkpoint）。
- **依赖谁**：config（CheckpointerConfig / DatabaseConfig / AppConfig）、config/paths（resolve_path）、（可选）langgraph checkpoint 包。
- **与 persistence 的区别**：checkpointer 管「图状态快照」（对话内容、节点输出）；persistence 管「应用元数据」（谁跑的、token 用量、线程归属）。共用 .db 文件但表分离。

---

## 9. 常见问题 / 排错

**Q: 报 `unable to open database file`？**
A: sqlite 文件的父目录不存在。本模块的 `ensure_sqlite_parent_dir` 会自动建——如果你绕过了 `make_checkpointer` 直接调 `SqliteSaver.from_conn_string`，就要自己先建目录。检查 `sqlite_dir` 是否可写。

**Q: 报 `langgraph-checkpoint-sqlite 未安装`？**
A: sqlite Saver 是可选包。安装：`cd backend && uv sync --all-packages --extra sqlite`。或改用 `backend: memory`（不需该包，但不持久化）。

**Q: 改了 `database.backend` 但 checkpointer 没变？**
A: 检查是否同时设了 legacy `checkpointer:` 段——它优先级更高，会覆盖 `database`。想让 `database` 生效，就删掉/注释掉独立的 `checkpointer:` 段。

**Q: `InMemorySaver` 没有 `setup()` 方法？**
A: 对。只有 sqlite/postgres Saver 需要 `setup()`（建表）。memory 没有。本模块在对应分支自动处理——memory 分支不调 setup。

**Q: 同步单例 `get_checkpointer()` 在测试间互相污染？**
A: 它是进程级全局。测试里用 `reset_checkpointer()` 在前后清理（见 test fixture `_reset_checkpointer_around_test`）。

**Q: postgres 报 `psycopg_pool` / `AsyncPostgresSaver` 找不到？**
A: postgres checkpointer 需要 `langgraph-checkpoint-postgres` + `psycopg` + `psycopg-pool`，都在 `postgres` extra 里：`cd backend && uv sync --all-packages --extra postgres`。

**Q: checkpoint 里存的内容是加密的吗？**
A: 不。checkpoint 存的是图状态（含消息内容），明文落盘。敏感数据（API key 等）不应放进图 state。

---

> 红线索引：#1（阻塞 IO 卸载 to_thread）、#24（缺包可操作提示）、#1912（sqlite 父目录保护）。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
