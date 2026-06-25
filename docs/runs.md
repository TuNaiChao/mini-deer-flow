# 26. runs.md — 运行管理（RunManager + worker，把 agent 跑成一次可观测、可取消、可回滚的 run）

> **一句话定位**：本模块是「调度层」——把一次「用户发消息 → agent 跑完 → 流式回播」包装成一个
> 有生命周期的 **run**：创建 / 排队 / 取消 / 回滚 / 异常恢复 / 关停 drain。`RunManager` 管 run 的
> 状态机和并发安全；`run_agent`（worker）在后台 task 里真正驱动 agent 图。

读完 [agents.md](agents.md)（懂了「`make_lead_agent` 把模型/工具/中间件/提示词拼成一张图」）再看本篇
最省事——本篇回答「这张图**怎么跑起来**、跑到一半用户取消怎么办、进程崩了重启后那些没跑完的 run
怎么办」。图本身是静态产物（M17），run 是它的**一次动态执行**。

---

## 0. 这个模块解决什么问题

「跑一次 agent」远不止 `agent.invoke(input)` 这一句。一次 run 要处理：

- **并发安全**：同一 thread 同时来两条消息怎么办？（reject / interrupt / rollback 三种策略）
- **可取消**：用户点了「停止」，正在跑的后台 task 怎么优雅停下？要不要回滚已写的 checkpoint？
- **可观测**：run 的状态（pending/running/success/error/...）、token 用量、错误信息要能查、能持久化。
- **崩溃恢复**：进程重启后，DB 里那些「持久化了但没本地 worker」的 pending/running run 怎么办？
  （不能让 UI 永远显示一个活跃 run）
- **关停 drain**：进程要关了，checkpointer 连接池要拆，但还有 run 在写 checkpoint——拆早了
  `PoolClosed` 异常冒上来（#3373）。得先 drain 在途 run。

本模块用两个核心类解决这些：

- **`RunManager`**（[manager.py](../backend/packages/harness/deerflow/runtime/runs/manager.py)）：
  内存 run 注册表 + 可选 `RunStore` 持久化。管状态机 + 并发锁 + 取消 + 恢复 + drain。
- **`run_agent`**（[worker.py](../backend/packages/harness/deerflow/runtime/runs/worker.py)）：
  在后台 `asyncio.Task` 里驱动 agent 图，流式发事件到 `StreamBridge`，处理 abort / rollback / 异常。

## 1. RunRecord —— 一个 run 的全部状态

[manager.py](../backend/packages/harness/deerflow/runtime/runs/manager.py) 的 `RunRecord` 是一个 dataclass，
装一个 run 的所有字段：

```python
@dataclass
class RunRecord:
    run_id: str               # UUID
    thread_id: str            # 哪个会话
    status: RunStatus         # pending/running/success/error/timeout/interrupted
    on_disconnect: DisconnectMode  # SSE 断连时 cancel 还是 continue
    multitask_strategy: str   # reject/interrupt/rollback
    task: asyncio.Task | None # 后台 task（进程内）
    abort_event: asyncio.Event # 取消信号
    abort_action: str         # "interrupt" / "rollback"
    # ... token 用量、metadata、user_id 等
    store_only: bool          # 从 store 还原的 record（无 task）
```

**关键区分**：`task` / `abort_event` 是**进程内**状态——只有创建该 run 的 worker 才有。从 `RunStore`
还原的 record 设 `store_only=True` 且无 task/abort_event。这意味着**跨 worker 取消不了别人的 run**
（红线 #9）——cancel 一个 store_only record 返回 `False`。

## 2. RunStatus —— 生命周期状态

[schemas.py](../backend/packages/harness/deerflow/runtime/runs/schemas.py)：

```
pending → running → success
                  → error
                  → timeout
                  → interrupted（被 multitask 策略 / cancel 打断）
```

`interrupted` 是「被人为打断」的终态（区别于 `error` 的「自己崩了」）。`cancel` 和 `create_or_reject`
的 interrupt/rollback 策略都会把 run 推到 `interrupted`。

## 3. RunManager —— 并发模型与红线

### 3.1 asyncio 锁 + 线程索引

所有写操作（create / set_status / cancel / create_or_reject）都经 `self._lock`（一个 `asyncio.Lock`），
保证状态机不被并发请求撕裂。除了主 dict `_runs`，还维护二级索引 `_runs_by_thread`
（`thread_id → 插入序 run_id 集合`），让 per-thread 查询（`has_inflight` / `list_by_thread`）不必
O(全部 run) 全扫。两者在锁下同步变更（中间无 `await`），所以任何持锁者看到的一致。

### 3.2 create_or_reject —— 原子 check-and-create（消除 TOCTOU）

```python
async def create_or_reject(self, thread_id, ..., multitask_strategy="reject"):
    async with self._lock:
        inflight = [r for r in self._thread_records_locked(thread_id)
                    if r.status in (pending, running)]
        if multitask_strategy == "reject" and inflight:
            raise ConflictError(...)
        if multitask_strategy in ("interrupt", "rollback") and inflight:
            # 取消 inflight
        # 创建新 run
```

**跨「检查 inflight」与「插入新 run」持锁**，消除分开 `has_inflight` + `create` 的 TOCTOU 竞态
（两个并发请求都看到「无 inflight」然后都创建）。三策略：

- **reject**：有 inflight → 抛 `ConflictError`（让前端提示「已有 run 进行中」）；
- **interrupt**：取消 inflight（保留 checkpoint），再创建；
- **rollback**：取消 inflight + 回滚到 run 前 checkpoint，再创建。

### 3.3 SQLite busy 重试（红线 #2）

`_call_store_with_retry` 对瞬时 SQLite 锁（`database is locked` / `SQLITE_BUSY` / `SQLITE_LOCKED`）
做有界指数退避重试（默认 5 次）。保护 run 状态终态化不被瞬时写压力卡死，同时**不重试永久失败**
（如 `no such table`）——否则会把永久错误永远藏起来。

### 3.4 rowcount 驱动 recovery（红线 #12）

`RunStore.update_status` / `update_run_completion` 返回 `False` 表示「能证明没行被更新」（行没了）。
这时 `RunManager` 用内存 snapshot 调 `put` **重建行**，再重试一次更新。为什么？SQLite 后端可能因
migration / 并发 delete 丢行；与其让 run 状态悬空，不如从内存重建。

### 3.5 创建失败回滚内存（红线 #13）

`create` / `create_or_reject` 在 `_persist_new_run_to_store` 失败时，把刚插进 `_runs` 的内存 record
**回滚**（pop + unindex）。run 的初始创建是可见性边界——调用方不应在内存里看到一个 store 行还没
建的 run。

### 3.6 orphan 恢复（红线 #7）

```python
async def reconcile_orphaned_inflight_runs(self, *, error, before=None):
    rows = await self._store.list_inflight(before=before)
    for row in rows:
        # 本地无活跃 worker 的 → 标 error
```

Gateway 重启后，DB 里那些「重启前创建的 pending/running」行**不可能**还有本地 worker（task 在内存，
进程没了就没了）。这一步把它们标 `error`，而不是让 UI 永远显示一个活跃 run。**跳过本地仍活跃的 run**
（本 worker 正在跑的不算 orphan）。

### 3.7 shutdown drain（红线 #6 / #3373）

```python
async def shutdown(self, *, timeout=5.0):
    # 1. cancel 所有在途 run 的 task（不设状态）
    # 2. bounded await（timeout）
    # 3. 只有没自行 settle 的才标 interrupted
```

**为什么必须在关 checkpointer 前 drain？** chat run 在 fire-and-forget 后台 task 里经共享 checkpointer
写 checkpoint。关停时 checkpointer 的连接池（如 postgres）被拆；若此时还有 run task 在图执行中途，
langgraph 的 `AsyncPregelLoop._checkpointer_put_after_previous` 会在已关的池上跑 `aput(...)`。那个 put
跑在 langgraph 内部 task 里（不在 `run_agent` 调用栈上），导致的 `PoolClosed` worker 捕获不到，会在
`asyncio.run()` 关闭时作为未处理异常冒上来（#3373）。

drain 让每个能在 `timeout` 内 settle 的 run 趁资源还开着 flush 最终 checkpoint。**关键**：只有没自行
settle 的 run 才标 `interrupted`——drain 期间正常完成（如 `success`）的 run 保留真实终态，不被一刀
覆盖。整个 drain（含尾部状态持久化）都被 `timeout` 卡住，防慢 store 拖死关停。

### 3.8 幂等 cancel

```python
if record.status == RunStatus.interrupted:
    return True  # 已取消，再 cancel 是 no-op 成功
```

避免「用户连点两次停止」时第二次报错。返回 `False` 只在「本 worker 不认识这个 run」或「run 已到
interrupted 以外的终态」。

## 4. run_agent（worker）—— 后台执行

[worker.py](../backend/packages/harness/deerflow/runtime/runs/worker.py) 的 `run_agent` 在后台 task 里：

1. **标 running** + 快照 pre-run checkpoint（供 rollback，红线 #5，含 `pending_writes` 深拷贝）；
2. 发 `metadata` 事件（`useStream` 要 run_id + thread_id）；
3. 构建 agent：注入 `__pregel_runtime`（langgraph 1.1+ 的 `runtime.context`）+ `__run_journal`
   （哨兵键，让中间件写审计事件）+ Langfuse metadata + RunJournal callback；
4. 挂 checkpointer / store / interrupt 节点；
5. `agent.astream(stream_mode=[...])` 流式驱动，每个 chunk 经 `serialize` 发到 bridge；
6. 终态决定：
   - `abort_event` set + `rollback` → status error + `_rollback_to_pre_run_checkpoint`；
   - `abort_event` set + `interrupt` → status interrupted；
   - LLM 兜底消息（chunk 里的 `deerflow_error_fallback`）→ status error；
   - 否则 → status success；
7. finally：flush journal + 持久化 completion + 标题回写 thread_meta + `publish_end`。

### 4.1 abort 在迭代边界检查

`record.abort_event.is_set()` 在 `astream` 的每个 chunk 之间检查。为什么不在 task cancel 时立即停？
因为 `asyncio.Task.cancel()` 抛 `CancelledError`，得在**协作点**（迭代边界）被捕获才能干净退出，
否则可能在中途留下半写状态。worker 同时处理两条路径：正常迭代退出（abort_event set → break）和
`CancelledError`（task 被 cancel）。

### 4.2 rollback 快照还原（红线 #5）

`_rollback_to_pre_run_checkpoint` 把 thread 状态还原到 run 开始前的 checkpoint：

- 无快照（首次 run）→ `adelete_thread` 清空；
- 有快照 → `aput` 写回 checkpoint（带新 id/ts 标记）+ `aput_writes` 还原 pending_writes。

**为什么 run 前要深拷贝快照？** checkpoint 的 `pending_writes` 是可变列表，run 期间会被改。深拷贝
确保 rollback 用的是 run **前**的完整状态（含未提交的 writes）。

### 4.3 LLM 兜底消息抽取

模型调用中间件（`LLMErrorHandlingMiddleware`）的兜底 AIMessage **不保证过 LLM end 回调**（它是
中间件合成的，不是真模型返回的），但会出现在图状态 chunk 里。`_extract_llm_error_fallback_message`
扫流式 chunk 找 `additional_kwargs.deerflow_error_fallback` 标记：

- **快路径**：`values` chunk 有顶层 `messages` list → 只扫那个（避免对大状态 dict 深递归）；
- **深扫**：`updates` / `messages` 等 chunk 小，全递归可接受。

找到就把 run 标 `error`（带 `error_detail`），否则正常 `success`。

## 5. RunContext + RunStore —— 依赖打包

### RunContext

`run_agent` 的基础设施依赖打包成一个对象（避免一长串 kwargs）：

```python
@dataclass(frozen=True)
class RunContext:
    checkpointer: Any           # 状态持久化（LangGraph Saver）
    store: Any | None           # 跨线程记忆（LangGraph BaseStore，M19）
    event_store: Any | None     # run 事件存储（写 journal）
    run_events_config: Any | None
    thread_store: Any | None    # thread 元数据（标题/状态回写）
    app_config: AppConfig | None
```

### RunStore

[store/base.py](../backend/packages/harness/deerflow/runtime/runs/store/base.py) 的 ABC——run 元数据存储。
两个实现：

- **`MemoryRunStore`**（[store/memory.py](../backend/packages/harness/deerflow/runtime/runs/store/memory.py)）：
  内存 dict，默认 / 测试用；
- **`RunRepository`**（`persistence.run.sql`）：SQLAlchemy ORM，持久化。

`RunManager` 给了 store 时，元数据双写（内存 + store），让 run 历史跨进程重启存活。ABC 提前到 Phase 1
是为了打破「持久化 → 运行管理 → 持久化」的循环依赖（`RunRepository` 要继承 `RunStore`，而 `RunStore`
属于 runs 领域，但 runs 的运行管理又依赖持久化）。

## 6. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **agents(M17)** | `agent_factory`（`make_lead_agent`）由 worker 调用构建图 |
| **checkpointer(M5)** | `RunContext.checkpointer`——run 写 checkpoint；rollback 读 pre-run 快照 |
| **events/store(M6)** | `RunContext.event_store`——RunJournal 写事件 |
| **journal(M7)** | worker 注入 RunJournal 作 callback + `__run_journal` 哨兵；token 用量 / completion 从它取 |
| **stream_bridge(M8)** | worker 把流式 chunk 发到 bridge；`publish_end` 收尾 |
| **serialization(M9)** | chunk 经 `serialize(mode=...)` 转 JSON 再发 bridge |
| **tracing(M12)** | worker 注入 Langfuse metadata（session/user/name/tags） |
| **user_context(M3)** | `get_effective_user_id()` 给 Langfuse metadata |
| **persistence(M4)** | `RunRepository(RunStore)` SQL 实现 |

## 7. 设计要点回顾

1. **双类分工**：`RunManager`（状态机 + 并发）vs `run_agent`（执行）。manager 不驱动图，worker 不管并发。
2. **asyncio 锁 + 线程索引**：写全加锁；per-thread 查询走索引免全扫。
3. **create_or_reject 原子性**：跨 check-and-create 持锁，消除 TOCTOU。
4. **三 multitask 策略**：reject / interrupt / rollback，原子地在锁内处理 inflight。
5. **busy 重试不藏永久失败**：只重试瞬时 SQLite 锁，`no such table` 这类直接上抛。
6. **rowcount 驱动 recovery**：`update_status` 返 False → 内存 snapshot 重建行。
7. **创建失败回滚内存**：store put 失败 → pop 内存 record，保可见性边界。
8. **orphan 恢复**：重启后把无本地 worker 的 inflight 行标 error，跳过本地仍活跃的。
9. **shutdown drain**：关 checkpointer 前 bounded-await 在途 run，只对未 settle 的标 interrupted。
10. **幂等 cancel**：已 interrupted 的再 cancel 是 no-op 成功。
11. **rollback 深拷贝快照**：含 pending_writes，确保还原 run 前完整状态。
12. **LLM 兜底扫 chunk**：中间件合成的兜底消息不过 LLM 回调，得从图状态 chunk 抽。

## 8. 排错 FAQ

- **「同 thread 两条消息报 ConflictError」**：`multitask_strategy=reject` 且已有 inflight。要排队改
  `interrupt`/`rollback`，或等前一个 run 结束。
- **「重启后 UI 显示一个永远 active 的 run」**：`reconcile_orphaned_inflight_runs` 没在启动时调，
  或用的是内存 store（``MemoryRunStore``——重启后 dict 为空，无可恢复行；只有 sqlite/postgres
  的 ``RunRepository`` 才有跨重启的持久行可恢复）。本 worker 仍活跃的 run 会被跳过。
- **「关停时报 PoolClosed / asyncio 未处理异常」**：没在关 checkpointer 前 `shutdown()` drain。
  drain 让在途 run 趁资源还开着 flush checkpoint。
- **「cancel 返回 False 但 run 还在跑」**：那是 store-only record（别的 worker 的 run），本 worker
  停不了。或 run 已到终态。
- **「rollback 后状态没还原」**：`snapshot_capture_failed`（run 前 aget_tuple 抛错）会跳过 rollback；
  或无 checkpointer。查日志「Could not capture pre-run checkpoint snapshot」。
- **「LLM 挂了 run 却标 success」**：兜底消息没贴 `deerflow_error_fallback` 标记，或不在 `messages`
  channel。`_extract_llm_error_fallback_message` 只扫 values chunk 的顶层 messages list + 其它 chunk 深扫。
- **「update_status 返 False 一直循环」**：那是 rowcount recovery 在工作——它会 put 重建行再重试。
  若 store 真的put 也失败，看日志「Failed to persist」。

---

**下一篇**：[README.md](README.md) 待写表里下一个是 `runtime_store.md`（M19，LangGraph BaseStore 工厂）
/ `architecture.md`（集成装配）。本模块的 `RunContext.store` 已为 M19 预留；M19 落地后 worker 的
`agent.store = store` 即生效。集成装配（lifespan）会把 `RunManager` + `run_agent` 串进 Gateway 的
请求路径，形成完整「创建 run → 跑 agent → 流式回播」端到端链路。
