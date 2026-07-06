# 28. architecture.md — 集成装配总览（所有模块怎么拼成一个能跑的系统）

> **一句话定位**：本篇是「全景图」与**完结篇**——前面 27 篇讲了每个模块**单独**怎么工作，本篇讲它们**一起**怎么拼成一个能跑的 agent 系统：谁先建、谁后建、关停时按什么顺序 drain、一次请求从进到出经过哪些层。对应的代码是 [runtime/lifespan.py](../backend/packages/harness/deerflow/runtime/lifespan.py)。

**学完能回答（learning outcomes）**：

1. 一次运行时装配要按什么顺序建哪些单件，为什么是这个顺序；
2. 关停时为什么**必须先 drain 在途 run 再关 checkpointer**（`PoolClosed` 未处理异常的根因）；
3. `RuntimeBundle` 打包了什么、为什么把 `run_context` 也塞进去（调用方零组装）；
4. `AsyncExitStack` 怎么让多个 async 上下文管理器「并行起、LIFO 关、自动清理」；
5. memory / sqlite / postgres 后端怎么由 `database.backend` + `checkpointer.type` 统一决定所有持久化单件；
6. 启动时的 orphan 恢复为什么必须在 lifespan 进时跑一次；
7. 能在面试里讲清「mini 的集成装配与上游 deer-flow 的根本差异」（见 §10——这是全系列**唯一一个 mini 原创的模块**，因为上游的装配在 Gateway 层、mini 没有 Gateway）。

读完前面所有 Phase 文档（尤其 [runs.md](runs.md) + [runtime_store.md](runtime_store.md)）再看本篇最省事——本篇是它们的「组装说明书」。

---

## 1. 名词（先懂这些再往下看）

### 1.1 计算机基础层（每个名词第一次出现就解释）

- **lifespan（生命周期）**：一个程序 / 服务「从启动到关闭」的整个过程。Web 框架（如 FastAPI）有个 `lifespan` 钩子——在「开始接受请求前」做初始化（建连接池、加载配置），在「关闭前」做清理（drain 任务、关连接）。本模块的 `runtime_lifespan` 就是这个角色，但不绑 FastAPI。
- **上下文管理器（context manager）**：Python 的 `with` / `async with` 协议——进入时获取资源，退出时清理。本模块的 `runtime_lifespan` 是 async 上下文管理器：`async with runtime_lifespan() as bundle:` 进时装配，块退出时 drain + 清理。详见 [checkpointer.md](checkpointer.md)。
- **`AsyncExitStack`**：Python 标准库的工具——把多个 async 上下文管理器「叠」在一起，**并行进入、按 LIFO（后进先出）顺序退出**。任何一个抛异常也会正确清理已进栈的。本模块用它同时管 checkpointer / stream_bridge / store 三个 async CM。
- **LIFO（后进先出）**：像栈一样，最后进去的最先出来。资源清理通常要 LIFO——后建的依赖先建的，拆的时候要反过来（先拆依赖方）。`AsyncExitStack` 自动保证这个顺序。
- **单件（singleton）**：一个进程里只存在一个实例的对象（如 checkpointer、run_manager）。本模块装配出一组单件，打包进 `RuntimeBundle`。
- **session factory（会话工厂）**：SQLAlchemy 的对象，用来「按需创建数据库会话」。SQL 后端才有；memory 后端没有（`get_session_factory()` 返回 `None`）。本模块用它判断「该用持久化的 SQL 实现还是内存实现」。
- **drain（排空）**：关闭前把「在途」的任务等完 / 取消干净。详见 [runs.md](runs.md)。
- **orphan（孤儿 run）**：持久化记录还在、但产生它的本地 worker（后台 task）已经没了的 run——进程重启后，重启前的 pending/running run 都成了 orphan。详见 [runs.md](runs.md)。
- **checkpoint**：agent 图跑到某一步时的完整状态存档。详见 [checkpointer.md](checkpointer.md)。
- **TOCTOU**：Time-Of-Check-To-Time-Of-Use 竞态。详见 [runs.md](runs.md)。

### 1.2 模块层名词

- **`runtime_lifespan`**（[lifespan.py:60](../backend/packages/harness/deerflow/runtime/lifespan.py#L60)）：框架级 async 上下文管理器——装配所有运行时单件，`yield` 一个 `RuntimeBundle`，退出时按序 drain + 清理。**不绑 FastAPI**——CLI / 测试 / `langgraph dev` 都能复用。
- **`RuntimeBundle`**（[lifespan.py:40](../backend/packages/harness/deerflow/runtime/lifespan.py#L40)）：装配产物 dataclass——checkpointer / stream_bridge / store / run_manager / event_store / thread_store / run_context / app_config 全打包。
- **`RunContext`**（[runs/worker.py](../backend/packages/harness/deerflow/runtime/runs/worker.py)）：worker 的基础设施依赖打包（checkpointer / store / event_store / thread_store / app_config）。`bundle.run_context` 就是装好的，可直接喂 `run_agent`。

---

## 2. mini 的整体架构

mini-deer-flow 是 deer-flow 的教学化重写：用更小的代码量、更细的讲解，覆盖 deer-flow harness 的全部核心行为。和 deer-flow 的关键区别：**mini 没有 Gateway（`app/`）层**——deer-flow 的 FastAPI Gateway 在 mini 里不存在。mini 的「跑起来」靠两种入口：

1. **`langgraph dev`**：读 [langgraph.json](../backend/langgraph.json)，加载 `make_lead_agent` 图工厂，langgraph 自己管 checkpointer 生命周期（经 `langgraph.json` 的 `checkpointer` 段）；
2. **`runtime_lifespan`**（本模块）：框架级装配上下文管理器，把所有运行时单件串成一个 `RuntimeBundle`，给 CLI / 测试 / 未来 Gateway 复用。

```
                ┌─────────────────────────────────────────┐
                │           runtime_lifespan              │  ← 本篇主角
                │  （AsyncExitStack 装配 + drain）         │
                └─────────────────────────────────────────┘
                          │ yield RuntimeBundle
          ┌───────────────┼───────────────┬──────────────┐
          ▼               ▼               ▼              ▼
    checkpointer    stream_bridge       store        run_manager
   （图状态快照）   （SSE 桥）        （跨thread记忆）（run 状态机）
   #8              #11               #27           #26
          │               │               │              │
          └───────┬───────┴───────┬───────┘              │
                  ▼               ▼                      │
            event_store     thread_store                 │
           （消息+轨迹）   （thread 元数据）              │
            #9              #7                           │
                                                          │
                              run_agent (worker) ◄───────┘
                                  │  bundle.run_context
                                  ▼
                          make_lead_agent (图工厂)  #25
                                  │
                                  ▼
                      build_middlewares (25 步链)  #24
                                  │
                                  ▼
                  models + tools + sandbox + skills + ...  （#6 #22 #13 #19 …）
```

---

## 3. 这个模块解决什么问题

前面 27 篇讲的每个模块都是**独立组件**：checkpointer 管存档、stream_bridge 管 SSE、run_manager 管 run 状态、worker 管执行……但一个能跑的 agent 系统需要把这些组件**按正确顺序建好、注入彼此、关停时按正确顺序清理**。本模块就是这道「总装」工序：

- **建**：按依赖序——engine 先（SQL 要 session factory）→ checkpointer / stream_bridge / store 并行 → run_store / event_store / thread_store（按 session factory 挑实现）→ RunManager + 启动恢复；
- **注入**：把 checkpointer / store / event_store / thread_store 打包成 `RunContext` 塞进 bundle，worker 直接用；
- **关停**：先 drain 在途 run（**必须在关 checkpointer 前**）→ 再关各 CM → 最后关 engine。

---

## 4. 核心概念

### 4.1 进：按依赖序建单件

[lifespan.py:72-122](../backend/packages/harness/deerflow/runtime/lifespan.py#L72-L122) 的装配顺序：

```python
async with runtime_lifespan(app_config) as bundle:
    # 1. engine 先建（SQL 后端要 session factory；memory → no-op）
    await init_engine_from_config(app_config.database)

    # 2. checkpointer / stream_bridge / store 并行起（AsyncExitStack）
    checkpointer = make_checkpointer(app_config)
    stream_bridge = make_stream_bridge(app_config)
    store = make_store(app_config)

    # 3. run_store / event_store / thread_store 按 session factory 挑实现
    run_store    = RunRepository(sf) if sf else MemoryRunStore()
    event_store  = make_run_event_store(run_events_config)
    thread_store = make_thread_store(sf, store)

    # 4. RunManager(store=run_store) + 启动恢复
    run_manager = RunManager(store=run_store)
    await run_manager.reconcile_orphaned_inflight_runs(error="Worker restarted")
```

为什么这个顺序：engine 必须先（后面挑 store 实现要问 `get_session_factory()`，它由 engine 建）；checkpointer / stream_bridge / store 互不依赖，并行进 `AsyncExitStack`；run_store / thread_store 的实现选择**依赖** session factory 和 store，所以排在后面；RunManager 持有 run_store，所以最后建；建完立刻 reconcile orphan（趁还没接请求）。

### 4.2 出：先 drain 再关 checkpointer

[lifespan.py:132-146](../backend/packages/harness/deerflow/runtime/lifespan.py#L132-L146) 的关停顺序：

```python
    finally:
        # 先 drain 在途 run（必须在关 checkpointer 前！）
        await run_manager.shutdown(timeout=5.0)
    # AsyncExitStack 退出：LIFO 关 store → stream_bridge → checkpointer
    await close_engine()   # 最后关 engine
```

**为什么这个顺序至关重要？** chat run 在后台 task 里经共享 checkpointer 写 checkpoint。关停时 checkpointer 的连接池被拆；若还有 run task 在写，langgraph 内部 task 对**已关的池**写 → `PoolClosed` 未处理异常冒上来。**先 drain 让在途 run 趁资源还开着 flush 最终 checkpoint**，只有没 settle 的才标 interrupted（见 [runs.md](runs.md) §5.7）。整个 drain 被 `timeout` 卡住，防慢 store 拖死关停。

### 4.3 RuntimeBundle：装配产物

[lifespan.py:40](../backend/packages/harness/deerflow/runtime/lifespan.py#L40)：

```python
@dataclass
class RuntimeBundle:
    checkpointer: Any            # InMemorySaver / SqliteSaver / PostgresSaver
    stream_bridge: Any           # MemoryStreamBridge
    store: Any | None            # InMemoryStore / SqliteStore / PostgresStore
    run_manager: RunManager      # run 状态机
    event_store: Any | None      # 消息 + 轨迹存储
    thread_store: Any | None     # thread 元数据（标题 / 状态）
    run_events_config: Any | None
    app_config: AppConfig
    run_context: RunContext      # 打包好直接喂 worker
```

`bundle.run_context`（[lifespan.py:103-110](../backend/packages/harness/deerflow/runtime/lifespan.py#L103-L110)）把 checkpointer / store / event_store / thread_store 打包好，**可直接喂 `run_agent`**——调用方不用再手动组装一长串 kwargs。

---

## 5. 代码走读

### 5.1 `runtime_lifespan` 的骨架

[lifespan.py:60](../backend/packages/harness/deerflow/runtime/lifespan.py#L60) 是一个 async 上下文管理器。骨架：

1. 读 config（`app_config or get_app_config()`）；
2. `init_engine_from_config(app_config.database)`（[lifespan.py:76](../backend/packages/harness/deerflow/runtime/lifespan.py#L76)）；
3. 进 `AsyncExitStack`（[lifespan.py:79](../backend/packages/harness/deerflow/runtime/lifespan.py#L79)）——后续三个 CM 都挂在栈上，栈退出时 LIFO 自动关；
4. 并行起 checkpointer / stream_bridge / store（[lifespan.py:84-86](../backend/packages/harness/deerflow/runtime/lifespan.py#L84-L86)）；
5. 建 run_store / event_store / thread_store（[lifespan.py:89-93](../backend/packages/harness/deerflow/runtime/lifespan.py#L89-L93)）；
6. `RunManager(store=run_store)` + reconcile orphan（[lifespan.py:95-101](../backend/packages/harness/deerflow/runtime/lifespan.py#L95-L101)）；
7. 组装 `RunContext` + `RuntimeBundle`（[lifespan.py:103-122](../backend/packages/harness/deerflow/runtime/lifespan.py#L103-L122)）；
8. `yield bundle`；finally 里 `run_manager.shutdown(timeout=5.0)` drain（[lifespan.py:136-139](../backend/packages/harness/deerflow/runtime/lifespan.py#L136-L139)）；
9. 栈退出关三个 CM；栈外 `close_engine()`（[lifespan.py:143-146](../backend/packages/harness/deerflow/runtime/lifespan.py#L143-L146)）。

### 5.2 `_build_run_store` / `_build_thread_store`：按后端挑实现

[lifespan.py:149](../backend/packages/harness/deerflow/runtime/lifespan.py#L149) 的 `_build_run_store`：有 session factory（SQL）→ `RunRepository(sf)`；否则 → `MemoryRunStore()`（重启丢失）。[lifespan.py:167](../backend/packages/harness/deerflow/runtime/lifespan.py#L167) 的 `_build_thread_store`：调 `make_thread_store(sf, store)`——sf 在用 SQL 实现，否则用基于 store 的内存实现；两者都没有返 `None`（worker 的标题 / 状态回写 best-effort 跳过）。

### 5.3 后端选择矩阵（memory vs sqlite vs postgres）

`database.backend` + `checkpointer.type` 决定所有持久化单件用哪种实现。memory 是默认（开箱即用）：

| 单件 | memory | sqlite / postgres |
|------|--------|-----------------|
| checkpointer | InMemorySaver | AsyncSqliteSaver / AsyncPostgresSaver |
| store | InMemoryStore | AsyncSqliteStore / AsyncPostgresStore |
| run_store | MemoryRunStore | RunRepository(sf) |
| event_store | MemoryRunEventStore | DbRunEventStore(sf) / JsonlRunEventStore |
| thread_store | MemoryThreadMetaStore(store) | ThreadMetaRepository(sf) |
| session_factory | None | async_sessionmaker |

memory 后端**全进程内**，重启全丢——开发 / 测试用。sqlite / postgres 持久化，重启存活（orphan recovery 处理「重启前没跑完的 run」）。

---

## 6. 一次请求的完整路径（端到端）

以「用户发一条消息」为例，从进到出：

```
用户消息
   │
   ▼
[创建 run]  run_manager.create_or_reject(thread_id, multitask_strategy)
   │        ├── 原子 check-and-create（消除 TOCTOU）
   │        └── reject / interrupt / rollback 三策略
   ▼
[起后台 task]  asyncio.create_task(run_agent(bridge, run_manager, record, ctx=bundle.run_context, ...))
   │
   ▼
[run_agent / worker]
   ├── set_status(running) + 快照 pre-run checkpoint（供 rollback）
   ├── 发 metadata 事件到 bridge
   ├── 注入 __pregel_runtime + __run_journal + Langfuse metadata + RunJournal callback
   ├── agent_factory(config) → make_lead_agent → build_middlewares(25 步) → create_agent
   ├── agent.astream(stream_mode=[...]) 流式驱动
   │      ├── 每个 chunk 经 serialize → bridge.publish → SSE 给前端
   │      ├── abort_event 迭代边界检查（用户点停止）
   │      └── LLM 兜底扫 chunk（deerflow_error_fallback 标记）
   ├── 终态：success / error / interrupted（abort action 决定）
   └── finally：journal flush + completion 持久化 + 标题回写 thread_meta + bridge.publish_end
   ▼
[前端]  SSE 流收到 messages-tuple / values / end
```

---

## 7. 配置

`runtime_lifespan` 读 `app_config`（默认 `get_app_config()`）：

| 配置段 | 作用 |
|--------|------|
| `app_config.database` | `init_engine_from_config` 决定 SQL 后端（memory → no-op） |
| `app_config.checkpointer` | `make_checkpointer` + `make_store` 共用（type: memory/sqlite/postgres） |
| `app_config.stream_bridge` | `make_stream_bridge` |
| `app_config.run_events` | `make_run_event_store` 挑 event_store 实现 |

[langgraph.json](../backend/langgraph.json) 的 `checkpointer` 段（`make_checkpointer` 路径）让 `langgraph dev` 也能用同一个 checkpointer 工厂——两条入口的持久化行为一致。

---

## 8. 与其它模块的关系（全景）

本篇是所有模块的汇聚点——`runtime_lifespan` 调用了几乎所有运行时模块的工厂：

| 模块 | 在 lifespan 里的角色 |
|------|---------------------|
| **config**（[#3](config.md)） | `app_config` 驱动一切 |
| **persistence**（[#7](persistence.md)） | `init_engine_from_config` + `get_session_factory` + `make_thread_store` + `RunRepository` |
| **checkpointer**（[#8](checkpointer.md)） | `make_checkpointer(app_config)` |
| **run_event_store**（[#9](run_event_store.md)） | `make_run_event_store(run_events_config)` |
| **stream_bridge**（[#11](stream_bridge.md)） | `make_stream_bridge(app_config)` |
| **runs/manager + worker**（[#26](runs.md)） | `RunManager(store=run_store)` + reconcile + shutdown drain；`run_agent` 用 `bundle.run_context` |
| **store**（[#27](runtime_store.md)） | `make_store(app_config)` |
| **agents**（[#25](agents.md)） | `make_lead_agent` 作为 `agent_factory` 被 worker 调 |
| **middlewares**（[#24](middlewares.md)） | `build_middlewares` 由 `make_lead_agent` 调 |

---

## 9. 设计动机分析

### 9.0 核心设计动机表

| 设计 | 为什么 | 不这么设计会怎样 |
|------|--------|------------------|
| **框架级 lifespan（不绑 FastAPI）** | mini 无 Gateway；CLI / 测试 / langgraph dev 都要复用装配 | 装配绑死在 FastAPI lifespan，CLI / 测试没法用 |
| **AsyncExitStack 并行起 + LIFO 关** | 三个 CM 互不依赖可并行起；LIFO 保证依赖方先拆 | 手写嵌套 with：深层难读 + 异常清理易漏 |
| **drain 在关 checkpointer 前** | 防 langgraph 内部 task 对已关池写 → PoolClosed | 关停时未处理异常冒上来 |
| **`bundle.run_context` 打包** | 调用方零组装直接喂 worker | 每个 worker 调用方都手组装 5 个 kwargs，易错 |
| **engine 最先建** | 后面挑 store 实现要问 session factory | store 实现选择拿不到 sf，误退回内存 |
| **启动时 reconcile orphan** | 重启前没跑完的 run 成了 orphan | UI 永远显示活跃 run |
| **后端一致性（统一由 database + checkpointer 决定）** | 一份配置驱动所有单件 | 各单件各自读配置易漂移到不同后端 |
| **memory 默认（开箱即用）** | 新手零配置就能跑 | 没配 SQL 就报错，卡在第一步 |

### 9.1 为什么 drain 必须在关 checkpointer 前

这是本模块最关键的顺序约束。chat run 在 fire-and-forget 后台 `asyncio.Task` 里跑，经**共享** checkpointer 写 checkpoint。关停流程会拆 checkpointer 的连接池（如 postgres 的 `psycopg_pool`）。如果还有 run task 在图执行中途：

- langgraph 的 `AsyncPregelLoop._checkpointer_put_after_previous` 会在它的**内部 task**里（不在 worker 调用栈上）调 `checkpointer.aput(...)`；
- 这个 put 命中**已关的池**，抛 `PoolClosed`；
- worker 的 `try/except` 捕获不到（不在它调用栈），异常在 `asyncio.run()` 关闭时作为「未处理异常」冒出来。

`runtime_lifespan` 的 `finally` 先 `run_manager.shutdown(timeout=5.0)` drain（[lifespan.py:136-139](../backend/packages/harness/deerflow/runtime/lifespan.py#L136-L139)），让每个能在 timeout 内 settle 的 run 趁资源还开着 flush 最终 checkpoint；只有没 settle 的才标 interrupted。**然后**才让 `AsyncExitStack` 退出关 checkpointer。顺序一换就炸。

### 9.2 为什么用 AsyncExitStack 而不是嵌套 with

要同时管 checkpointer / stream_bridge / store 三个 async CM。最朴素的写法是嵌套：

```python
async with make_checkpointer(...) as checkpointer:
    async with make_stream_bridge(...) as stream_bridge:
        async with make_store(...) as store:
            ...
```

问题：① 嵌套深了难读；② 三个 CM 互不依赖却写成串行嵌套，语义上误导（像有依赖）；③ 想动态加减 CM 很笨。

`AsyncExitStack`（[lifespan.py:79](../backend/packages/harness/deerflow/runtime/lifespan.py#L79)）把三个 CM 都 `enter_async_context` 进栈——**并行进入**（语义清晰：互不依赖）、**LIFO 退出**（栈退出时自动按反序关，异常也正确清理）。代码扁平、意图明确。

### 9.3 为什么把 run_context 塞进 bundle

`run_agent` 需要 5 个基础设施依赖（checkpointer / store / event_store / thread_store / app_config）。如果不打包，每个调用方（CLI / 测试 / 未来 Gateway）都得手组装这 5 个 kwargs——容易漏、容易传错。

`runtime_lifespan` 在装配完就把它们打包成 `RunContext` 塞进 `bundle.run_context`（[lifespan.py:103-110](../backend/packages/harness/deerflow/runtime/lifespan.py#L103-L110)）。调用方只要 `await run_agent(bridge, bundle.run_manager, record, ctx=bundle.run_context, ...)`——零组装。这也让「装配」与「执行」的边界清晰：lifespan 负责凑齐依赖，worker 只管跑。

### 9.4 为什么 engine 必须最先建

SQL 后端的 `get_session_factory()` 返回的 session factory 由 `init_engine_from_config` 创建。后面 `_build_run_store`（[lifespan.py:156](../backend/packages/harness/deerflow/runtime/lifespan.py#L156)）和 `_build_thread_store`（[lifespan.py:175](../backend/packages/harness/deerflow/runtime/lifespan.py#L175)）都要问 `get_session_factory()` 来决定「用 SQL 实现还是内存实现」。

**不这么设计会怎样**：如果 checkpointer / store 先建（它们内部也可能间接需要 engine），或者 store 实现选择跑在 engine 之前——`get_session_factory()` 返回 `None`（engine 没建），于是误退回 `MemoryRunStore`，即使配置了 SQL 后端。engine 先建保证后续选择拿到正确的 sf。

---

## 10. 实现差异（vs 上游 deer-flow 源码）

本篇的 §10 与前 27 篇**根本不同**：`runtime/lifespan.py` 是全系列**唯一一个 mini 原创的模块**——上游 harness 包**没有对应的 lifespan 文件**。这不是「忠实移植」也不是「教学简化」，而是 mini 因为砍了 Gateway 层而**新增的框架级装配层**。

### 10.1 上游的装配在哪

上游 deer-flow 的运行时装配散在 **Gateway 应用层**（mini 不 port 的 `app/`）：

- `app/gateway/app.py:163` 的 `async def lifespan(app: FastAPI)`——FastAPI 的生命周期钩子，在那里直接建 checkpointer / store / run_manager 等；
- `app/gateway/deps.py`——依赖注入，持有 `init_engine` / `make_checkpointer` 等的装配逻辑。

上游 harness 包（`packages/harness/deerflow/runtime/`）**没有** `lifespan.py`——已确认 `find` 无结果，也没有 `RuntimeBundle` / `runtime_lifespan` 符号。上游的装配**绑在 FastAPI 上**（`lifespan(app: FastAPI)` 签名），离开 Gateway 用不了。

### 10.2 mini 为什么新增这个模块

mini 没有 Gateway（`app/` 整层不存在）。但 mini 仍需要一个「把所有运行时单件按序建好 + 关停 drain」的地方——给 CLI（未来 TUI）、测试、`langgraph dev` 复用。所以 mini 把上游 Gateway lifespan 里的**装配逻辑抽成框架级 `runtime_lifespan`**：

- 不绑 FastAPI（纯 async 上下文管理器，任何 async 入口都能用）；
- `yield` 一个 `RuntimeBundle`（上游没有这个 dataclass，因为上游各单件直接挂在 FastAPI `app.state` 上）。

### 10.3 装配 / drain 语义与上游一致

虽然 `runtime/lifespan.py` 是 mini 原创，但它的**装配顺序与关停 drain 的语义**与上游 Gateway 装配一致——因为这套顺序是由组件间的依赖关系和 `PoolClosed` 那个底层约束**推出的**，换谁装配都一样：

- **进**：engine → checkpointer / store / stream_bridge（并行）→ run_store / event_store / thread_store → RunManager + reconcile orphan；
- **出**：先 `run_manager.shutdown` drain 在途 run（必须在关 checkpointer 前）→ 再关各 CM → 最后关 engine。

drain-before-close-checkpointer 是 [runs.md](runs.md) §9.2 讲的那个 `PoolClosed` 约束——它由 langgraph 的内部 task 写 checkpoint 行为决定，与上游一致（`RunManager.shutdown` 逐行一致，见 [runs.md](runs.md) §10）。

### 10.4 上游 lifespan 多做的事（mini 不需要）

上游 Gateway lifespan 还做 mini 不需要的 Gateway 专属事：关 channel service（IM 平台 worker，[app.py:234](../backend/app/gateway/app.py)）、bounded shutdown hook 防信号重入死锁等。这些都绑在 Gateway / IM 层，mini 无 Gateway 无 IM，自然不做。

**测试覆盖**：`test/test_integration.py`（9 测试）用 `runtime_lifespan` 跑端到端集成，验证 bundle 装配 + run 创建 + worker 执行的完整链路。

---

## 11. 排错 FAQ

- **「关停时报 PoolClosed / asyncio 未处理异常」**：没在关 checkpointer 前 drain。`runtime_lifespan` 的 finally 先 `run_manager.shutdown`——自己写入口时别漏这步。
- **「重启后 UI 有永远 active 的 run」**：`reconcile_orphaned_inflight_runs` 没在 lifespan 进时调。memory 后端无 store 无可恢复对象（no-op）；sqlite / postgres 后端会标 error。
- **「bundle.store 是 None」**：memory 后端不会——`make_store` 总 yield InMemoryStore。若为 None 说明 make_store 抛了（看日志）。
- **「配了 SQL 后端但 run_store 还是 MemoryRunStore」**：检查 `init_engine_from_config` 是不是先跑了——engine 没建时 `get_session_factory()` 返 None，`_build_run_store` 退回内存。
- **「想自己跑一次 agent」**：

  ```python
  async with runtime_lifespan() as bundle:
      record = await bundle.run_manager.create("thread-1")
      await run_agent(
          bundle.stream_bridge, bundle.run_manager, record,
          ctx=bundle.run_context, agent_factory=make_lead_agent,
          graph_input={"messages": [...]}, config={},
      )
  ```
- **「`langgraph dev` 用的是哪个 checkpointer」**：[langgraph.json](../backend/langgraph.json) 的 `checkpointer.path` 指向 `make_checkpointer`——和 `runtime_lifespan` 用的是同一个工厂，行为一致。

---

**🎉 mini-deer-flow 全部 Phase 0–8 文档到此完结**（#0 导论 + #1–#28）。从 [start-here.md](start-here.md)（零基础起点）→ [build.md](build.md)（怎么跑起来）一路读到本篇（怎么拼成系统），覆盖：地基 → 模型 / 运行时 → 沙箱 / 子代理 / 追踪 → 记忆 → 技能 → MCP / 联网 → 工具 → 上传 → 中间件 → agent 装配 → 运行管理 → Store → 集成。仅剩 Guardrail（#24 §10.1）与 DeerFlowClient（上游嵌入式 client，mini 列为可选）两个真正可选模块按需深入。面试前过一遍 [README.md](README.md) 末尾的「面试概念地图」。
