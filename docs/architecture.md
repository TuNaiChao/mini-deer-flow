# 28. architecture.md — 集成装配总览（所有模块怎么拼成一个能跑的系统）

> **一句话定位**：本篇是「全景图」——前面 27 篇讲了每个模块**单独**怎么工作，本篇讲它们**一起**
> 怎么拼成一个能跑的 agent 系统：谁先建、谁后建、关停时按什么顺序 drain、一次请求从进到出经过
> 哪些层。

读完前面所有 Phase 文档（尤其 [runs.md](runs.md) + [runtime_store.md](runtime_store.md)）再看本篇
最省事——本篇是它们的「组装说明书」。对应的代码是
[runtime/lifespan.py](../backend/packages/harness/deerflow/runtime/lifespan.py)。

---

## 0. mini 的整体架构

mini-deer-flow 是 deer-flow 的**教学化对齐版**：用更小的代码量、更细的讲解重写，但行为全面对标。
和 deer-flow 的关键区别：**mini 没有 Gateway（app/）层**——deer-flow 的 FastAPI Gateway 在 mini
里不存在。mini 的「跑起来」靠两种入口：

1. **`langgraph dev`**：读 [langgraph.json](../backend/langgraph.json)，加载 `make_lead_agent` 图工厂，
   langgraph 自己管 checkpointer 生命周期（经 `langgraph.json` 的 `checkpointer` 段）；
2. **`runtime_lifespan`**（本模块）：框架级装配上下文管理器，把所有运行时单件串成一个
   `RuntimeBundle`，给 CLI / 测试 / 未来 Gateway 复用。

```
                ┌─────────────────────────────────────────┐
                │           runtime_lifespan              │  ← 本篇主角
                │  （AsyncExitStack 装配 + drain）         │
                └─────────────────────────────────────────┘
                          │ yield RuntimeBundle
          ┌───────────────┼───────────────┬──────────────┐
          ▼               ▼               ▼              ▼
    checkpointer    stream_bridge       store        run_manager
     (M5 图快照)    (M8 SSE 桥)      (M19 跨thread)  (M18 状态机)
          │               │               │              │
          └───────┬───────┴───────┬───────┘              │
                  ▼               ▼                      │
            event_store     thread_store                 │
            (M6 消息+轨迹)  (M4 thread元数据)            │
                                                          │
                              run_agent (M18 worker) ◄───┘
                                  │
                                  ▼
                          make_lead_agent (M17 图工厂)
                                  │
                                  ▼
                      build_middlewares (M16 23步链)
                                  │
                                  ▼
                  models + tools + sandbox + skills + ...
```

## 1. runtime_lifespan —— 装配 + drain

[runtime/lifespan.py](../backend/packages/harness/deerflow/runtime/lifespan.py) 的 `runtime_lifespan(app_config)`
是一个 async 上下文管理器，`yield` 一个 `RuntimeBundle`。

### 进：按依赖序建单件

```python
async with runtime_lifespan(app_config) as bundle:
    # 1. engine 先建（SQL 后端要 session factory；memory → no-op）
    await init_engine_from_config(app_config.database)

    # 2. checkpointer / stream_bridge / store 并行起（AsyncExitStack）
    checkpointer = make_checkpointer(app_config)
    stream_bridge = make_stream_bridge(app_config)
    store = make_store(app_config)

    # 3. run_store / event_store / thread_store 按 session factory 挑实现
    run_store   = RunRepository(sf) if sf else MemoryRunStore()
    event_store = make_run_event_store(run_events_config)
    thread_store = make_thread_store(sf, store)

    # 4. RunManager(store=run_store) + 启动恢复
    run_manager = RunManager(store=run_store)
    await run_manager.reconcile_orphaned_inflight_runs(error="Worker restarted")
```

### 出：先 drain 再关 checkpointer（红线 #6 / #3373）

```python
    finally:
        # 先 drain 在途 run（必须在关 checkpointer 前！）
        await run_manager.shutdown(timeout=5.0)
    # AsyncExitStack 退出：关 checkpointer / stream_bridge / store
    await close_engine()
```

**为什么这个顺序至关重要？** chat run 在后台 task 里经共享 checkpointer 写 checkpoint。关停时
checkpointer 的连接池被拆；若还有 run task 在写，langgraph 内部 task 对**已关的池**写 →
`PoolClosed` 未处理异常冒上来（#3373）。**先 drain 让在途 run 趁资源还开着 flush 最终 checkpoint**，
只有没 settle 的才标 interrupted。整个 drain 被 `timeout` 卡住，防慢 store 拖死关停。

## 2. RuntimeBundle —— 装配产物

```python
@dataclass
class RuntimeBundle:
    checkpointer: Any            # M5 InMemorySaver/SqliteSaver/PostgresSaver
    stream_bridge: Any           # M8 MemoryStreamBridge/RedisStreamBridge
    store: Any | None            # M19 InMemoryStore/SqliteStore/PostgresStore
    run_manager: RunManager      # M18 状态机
    event_store: Any | None      # M6 MemoryRunEventStore/JsonlRunEventStore/DbRunEventStore
    thread_store: Any | None     # M4 MemoryThreadMetaStore/ThreadMetaRepository
    run_events_config: Any | None
    app_config: AppConfig
    run_context: RunContext      # 打包好直接喂 worker
```

`bundle.run_context` 是把 checkpointer / store / event_store / thread_store 打包好的 `RunContext`，
**可直接喂 `run_agent`**——调用方不用再手动组装 kwargs。

## 3. 一次请求的完整路径（端到端）

以「用户发一条消息」为例，从进到出：

```
用户消息
   │
   ▼
[创建 run]  run_manager.create_or_reject(thread_id, multitask_strategy)
   │        ├── 原子 check-and-create（消除 TOCTOU）
   │        └── reject/interrupt/rollback 三策略
   ▼
[起后台 task]  asyncio.create_task(run_agent(bridge, run_manager, record, ctx=bundle.run_context, ...))
   │
   ▼
[run_agent / worker]
   ├── set_status(running) + 快照 pre-run checkpoint（红线 #5，供 rollback）
   ├── 发 metadata 事件到 bridge
   ├── 注入 __pregel_runtime + __run_journal + Langfuse metadata + RunJournal callback
   ├── agent_factory(config) → make_lead_agent → build_middlewares(23 步) → create_agent
   ├── agent.astream(stream_mode=[...]) 流式驱动
   │      ├── 每个 chunk 经 serialize → bridge.publish → SSE 给前端
   │      ├── abort_event 迭代边界检查（用户点停止）
   │      └── LLM 兜底扫 chunk（deerflow_error_fallback 标记）
   ├── 终态：success / error / interrupted（abort action 决定）
   └── finally：journal flush + completion 持久化 + 标题回写 thread_meta + bridge.publish_end
   ▼
[前端]  SSE 流收到 messages-tuple / values / end
```

## 4. 后端选择矩阵（memory vs sqlite vs postgres）

`database.backend` + `checkpointer.type` 决定所有持久化单件用哪种实现。memory 是默认（红线 #25，
开箱即用）：

| 单件 | memory | sqlite/postgres |
|------|--------|-----------------|
| checkpointer | InMemorySaver | AsyncSqliteSaver / AsyncPostgresSaver |
| store | InMemoryStore | AsyncSqliteStore / AsyncPostgresStore |
| run_store | MemoryRunStore | RunRepository(sf) |
| event_store | MemoryRunEventStore | DbRunEventStore(sf) / JsonlRunEventStore |
| thread_store | MemoryThreadMetaStore(store) | ThreadMetaRepository(sf) |
| session_factory | None | async_sessionmaker |

memory 后端**全进程内**，重启全丢——开发 / 测试用。sqlite/postgres 持久化，重启存活（orphan
recovery 处理「重启前没跑完的 run」）。

## 5. 集成装配清单（对齐 ALIGNMENT_OUTLINE Part D）

| Part | 内容 | mini 落地 |
|------|------|----------|
| D.1 | build_middlewares 23 步顺序 | ✅ M16 |
| D.2 | lifespan 装配 | ✅ `runtime/lifespan.py`（本篇） |
| D.3 | langgraph.json 补 checkpointer 段 | ✅ [langgraph.json](../backend/langgraph.json) |
| D.4 | config.example.yaml 增补 | ✅ [config.example.yaml](../config.example.yaml)（config_version + database/run_events/stream_bridge/safety_finish_reason/skill_evolution） |
| D.5 | build_event_store / build_thread_store 工厂 | ✅ `make_run_event_store`(M6) + `make_thread_store`(M4) |

## 6. 与其它模块的关系（全景）

本篇是所有模块的汇聚点——`runtime_lifespan` 调用了几乎所有运行时模块的工厂：

| 模块 | 在 lifespan 里的角色 |
|------|---------------------|
| **config(M0)** | `app_config` 驱动一切 |
| **persistence(M4)** | `init_engine_from_config` + `get_session_factory` + `make_thread_store` + `RunRepository` |
| **checkpointer(M5)** | `make_checkpointer(app_config)` |
| **events/store(M6)** | `make_run_event_store(run_events_config)` |
| **stream_bridge(M8)** | `make_stream_bridge(app_config)` |
| **runs/manager(M18)** | `RunManager(store=run_store)` + reconcile + shutdown drain |
| **runs/worker(M18)** | `run_agent` 用 `bundle.run_context` |
| **store(M19)** | `make_store(app_config)` |
| **agents(M17)** | `make_lead_agent` 作为 `agent_factory` 被 worker 调 |
| **middlewares(M16)** | `build_middlewares` 由 `make_lead_agent` 调 |

## 7. 设计要点回顾

1. **框架级装配**：`runtime_lifespan` 不绑 FastAPI——CLI / 测试 / 未来 Gateway 都复用。
2. **AsyncExitStack**：checkpointer / stream_bridge / store 并行起、LIFO 关，自动清理。
3. **drain 顺序**（红线 #6/#3373）：先 `run_manager.shutdown` drain 在途 run，再关 checkpointer。
4. **bundle.run_context 打包**：调用方不用手组装 kwargs，直接喂 worker。
5. **后端一致性**：所有持久化单件由 `database.backend` + `checkpointer.type` 统一决定，memory 默认。
6. **启动恢复**（红线 #7）：`reconcile_orphaned_inflight_runs` 处理重启前的悬空 run。
7. **三入口对齐**：`langgraph dev`（langgraph.json checkpointer 段）vs `runtime_lifespan`（bundle）
   vs 内嵌（未来 DeerFlowClient）。

## 8. 排错 FAQ

- **「关停时报 PoolClosed / asyncio 未处理异常」**：没在关 checkpointer 前 drain。`runtime_lifespan`
  的 finally 先 `run_manager.shutdown`——自己写入口时别漏这步。
- **「重启后 UI 有永远 active 的 run」**：`reconcile_orphaned_inflight_runs` 没在 lifespan 进时调。
  memory 后端无 store 无可恢复对象（no-op）；sqlite/postgres 后端会标 error。
- **「bundle.store 是 None」**：memory 后端不会——`make_store` 总 yield InMemoryStore。若为 None 说明
  make_store 抛了（看日志「Failed to ... store」）。
- **「想自己跑一次 agent」**：`async with runtime_lifespan() as bundle: record = await
  bundle.run_manager.create(...); await run_agent(bundle.stream_bridge, bundle.run_manager, record,
  ctx=bundle.run_context, agent_factory=..., graph_input=..., config={})`。

---

**mini-deer-flow 全部 Phase 0–8 文档到此完结**（#1–#28）。从 [build.md](build.md)（怎么跑起来）一路
读到本篇（怎么拼成系统），覆盖地基 → 模型/运行时 → 沙箱/子代理/追踪 → 记忆 → 技能 → MCP/联网 →
工具 → 上传 → 中间件 → agent 装配 → 运行管理 → Store → 集成。仅剩 Guardrail / DeerFlowClient 两个
真正可选模块（按需）。
