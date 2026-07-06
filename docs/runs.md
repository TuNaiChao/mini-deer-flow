# 26. runs.md — 运行管理（RunManager + worker，把 agent 跑成一次可观测、可取消、可回滚的 run）

> **一句话定位**：本模块是「调度层」——把一次「用户发消息 → agent 跑完 → 流式回播」包装成一个有生命周期的 **run**：创建 / 排队 / 取消 / 回滚 / 异常恢复 / 关停 drain。`RunManager` 管 run 的状态机和并发安全；`run_agent`（worker）在后台 task 里真正驱动 agent 图。

**学完能回答（learning outcomes）**：

1. 一次 run 为什么不能只是 `agent.invoke(input)` 一句——并发安全 / 可取消 / 可观测 / 崩溃恢复 / 关停 drain 各解决什么问题；
2. `RunManager`（状态机 + 并发）与 `run_agent`（执行）为什么必须分成两个类，各自管什么；
3. `create_or_reject` 怎么用一把锁消除 TOCTOU 竞态，三策略（reject/interrupt/rollback）各干什么；
4. 进程重启后那些「持久化了但没本地 worker」的 inflight run 怎么被恢复成 error（orphan reconcile）；
5. 关停时为什么必须先 drain 在途 run 再关 checkpointer（`PoolClosed` 未处理异常的根因）；
6. rollback 为什么要深拷贝含 `pending_writes` 的快照、cancel 为什么是幂等的；
7. token 怎么按**真计费模型**归桶（一个 run 可能调多个模型），写侧→传递→读侧的端到端链路；
8. 能在面试里讲清「mini 的运行管理与上游 deer-flow 源码的差异」（见 §10——这是迄今最忠实的移植：6/7 文件剥 docstring 后逐字节相同）。

读完 [agents.md](agents.md)（懂了「`make_lead_agent` 把模型/工具/中间件/提示词拼成一张图」）再看本篇最省事——本篇回答「这张图**怎么跑起来**、跑到一半用户取消怎么办、进程崩了重启后那些没跑完的 run 怎么办」。图本身是静态产物，run 是它的**一次动态执行**。

---

## 1. 名词（先懂这些再往下看）

### 1.1 计算机基础层（每个名词第一次出现就解释）

- **run**：agent 的一次完整执行（从用户发消息到 agent 回完）。本模块把每次执行包装成一个有生命周期的对象（`RunRecord`），给它一个 UUID、记状态、可取消可回滚。
- **thread（会话）**：一段连续的对话。同一 thread 里的多次 run 共享对话历史（checkpoint）。一个用户可以有多个 thread。
- **后台 task / `asyncio.Task`**：异步程序里「在后台跑的协程」对象。`asyncio.create_task(coro)` 把协程调度成 task，不阻塞当前代码。worker 就跑在这种后台 task 里——请求处理函数返回了，agent 还在后台继续跑。
- **`asyncio.Lock`**：异步版的互斥锁。同一时刻只有一个协程能「持有锁」进入临界区，别的想进就等。本模块所有写 run 状态的操作都经 `self._lock`，保证并发请求不把状态机撕裂。详见 [user_context.md](user_context.md)。
- **`asyncio.Event`**：异步版的「信号旗」。一个协程 `set()` 把旗升起来，其它在 `wait()` 等旗的协程就被唤醒。本模块的 `abort_event` 就是这种旗——用户点取消时 `set()`，worker 在迭代边界看到旗升了就停。
- **`asyncio.Task.cancel()` / `CancelledError`**：`task.cancel()` 给 task 注入一个 `CancelledError`，task 在下一个 `await` 点抛出。这是「协作式取消」——不是立刻杀，而是在一个安全的暂停点抛异常，让 task 有机会清理。
- **TOCTOU（Time-Of-Check-To-Time-Of-Use）**：一类竞态：「检查」和「使用」之间有时间窗，别的线程/协程可能在窗里改了状态。例：先检查「无 inflight run」再「创建 run」，两个并发请求可能都看到「无」然后都创建。解法是把 check + use 放进**同一把锁**里（原子化）。详见 [sandbox.md](sandbox.md)。
- **原子操作（atomic）**：不可分割的操作——要么全做完，要么全不做，中间不会被别的代码打断。本模块用「持锁」把「检查 + 创建」包成原子操作。
- **指数退避（exponential backoff）**：重试时每次间隔翻倍（0.05s → 0.1s → 0.2s …），有上限。避免一群客户端同时重试压垮下游。
- **SQLite busy / `SQLITE_BUSY`**：SQLite 写锁被别人占着时返回的错误（「database is locked」）。通常是瞬时的——稍等再试就好。本模块对这类错误有界重试。
- **幂等（idempotent）**：同一操作做多次和做一次效果一样。「幂等 cancel」= 已取消的 run 再 cancel 不报错、当成功。
- **checkpoint**：agent 图跑到某一步时把**完整状态**存下来（像游戏存档）。下次能从 checkpoint 接着跑。checkpointer 是负责存取 checkpoint 的组件（见 [checkpointer.md](checkpointer.md)）。
- **rollback（回滚）**：把状态**还原**到之前的某个 checkpoint。本模块在用户取消且策略是 rollback 时，把 thread 还原到这次 run **开始前**的 checkpoint。
- **pending_writes**：checkpoint 里「已经算出来但还没提交」的写入。是可变列表，run 期间会被改。rollback 要还原它，所以得在 run 前深拷贝。
- **orphan（孤儿）**：本模块特指「持久化记录还在、但产生它的本地 worker（后台 task）已经没了」的 run——进程重启后，重启前的 pending/running run 都成了 orphan。
- **drain（排空）**：关闭前把「在途」的任务等完/取消干净。像关门前先把店里的客人送走，再锁门。
- **哨兵键（sentinel key）**：用特殊命名（如 `__run_journal`）标记「这是内部 runtime 通道，不是用户数据」。中间件通过它拿到本次 run 的 journal 实例。
- **SSE（Server-Sent Events）**：服务器单向推消息给浏览器的协议。worker 把流式 chunk 经 StreamBridge 发成 SSE 事件给前端。见 [stream_bridge.md](stream_bridge.md)。
- **`StrEnum`**：Python 枚举，每个成员是字符串（`RunStatus.success == "success"`）。方便序列化成 JSON 存进 DB。

### 1.2 模块层名词

- **`RunManager`**（[manager.py:134](../backend/packages/harness/deerflow/runtime/runs/manager.py#L134)）：内存 run 注册表 + 可选持久化。管状态机 + 并发锁 + 取消 + 恢复 + drain。**不驱动图**。
- **`run_agent`（worker）**（[worker.py:125](../backend/packages/harness/deerflow/runtime/runs/worker.py#L125)）：在后台 task 里驱动 agent 图，流式发事件到 StreamBridge，处理 abort / rollback / 异常。**不管并发**。
- **`RunRecord`**（[manager.py:94](../backend/packages/harness/deerflow/runtime/runs/manager.py#L94)）：一个 run 的全部状态（dataclass）。
- **`RunStore`**（[store/base.py:28](../backend/packages/harness/deerflow/runtime/runs/store/base.py#L28)）：run 元数据存储的抽象基类（ABC）。两实现：`MemoryRunStore`（内存）+ `RunRepository`（SQL，在 persistence 层）。
- **`RunContext`**（[worker.py:77](../backend/packages/harness/deerflow/runtime/runs/worker.py#L77)）：把 worker 的基础设施依赖（checkpointer / store / event_store / thread_store / app_config）打包成一个对象，避免一长串 kwargs。
- **store-only record**：从 `RunStore` 还原的 record（设 `store_only=True`），无 task/abort_event——本 worker 停不了别的 worker 的 run。

---

## 2. 这个模块解决什么问题

「跑一次 agent」远不止 `agent.invoke(input)` 这一句。一次 run 要处理：

- **并发安全**：同一 thread 同时来两条消息怎么办？（reject / interrupt / rollback 三种策略）
- **可取消**：用户点了「停止」，正在跑的后台 task 怎么优雅停下？要不要回滚已写的 checkpoint？
- **可观测**：run 的状态（pending/running/success/error/...）、token 用量、错误信息要能查、能持久化。
- **崩溃恢复**：进程重启后，DB 里那些「持久化了但没本地 worker」的 pending/running run 怎么办？（不能让 UI 永远显示一个活跃 run）
- **关停 drain**：进程要关了，checkpointer 连接池要拆，但还有 run 在写 checkpoint——拆早了 `PoolClosed` 异常冒上来。得先 drain 在途 run。

本模块用两个核心类解决这些：`RunManager`（状态机 + 并发）+ `run_agent`（执行）。

---

## 3. 结构（装配关系 + 文件分工）

```
runtime/runs/
├── manager.py      ← RunManager（状态机 + 并发锁 + 取消 + 恢复 + drain）+ RunRecord + ConflictError
├── worker.py       ← run_agent（后台执行）+ RunContext + rollback + LLM 兜底抽取
├── schemas.py      ← RunStatus / DisconnectMode 枚举（领域词汇表，无 IO）
├── naming.py       ← resolve_root_run_name（trace 根名）
├── __init__.py     ← 包导出
└── store/
    ├── base.py     ← RunStore ABC（11 个抽象方法）
    ├── memory.py   ← MemoryRunStore（内存 dict + _runs_by_thread 索引）
    └── __init__.py
```

调用关系（谁在请求路径上把这两类串起来）：

```
请求处理函数（Gateway / 内嵌 client）
   │  RunManager.create_or_reject(thread_id, multitask_strategy=...)
   ▼
RunManager 创建 RunRecord + 持久化到 RunStore
   │  asyncio.create_task(run_agent(bridge, run_manager, record, ctx=..., agent_factory=..., ...))
   ▼
run_agent（后台 task）
   ├─ set_status(running) + 快照 pre-run checkpoint
   ├─ 构建 agent（注入 __pregel_runtime / __run_journal / Langfuse metadata / RunJournal callback）
   ├─ agent.astream(...) 流式驱动，每个 chunk → serialize → bridge.publish → SSE
   ├─ 终态决定（abort / LLM 兜底 / success）
   └─ finally：flush journal + 持久化 completion + 标题回写 thread_meta + publish_end
```

---

## 4. 核心概念

### 4.1 双类分工

`RunManager` 管「run 的元信息和并发」，不驱动图；`run_agent` 管「驱动图执行」，不管并发。这样状态机逻辑（锁、恢复、drain）和执行逻辑（流式、rollback、异常）各管一摊，可独立测、独立演进。

### 4.2 RunStatus 生命周期

[schemas.py:10](../backend/packages/harness/deerflow/runtime/runs/schemas.py#L10)：

```
pending → running → success
                  → error
                  → timeout
                  → interrupted（被 multitask 策略 / cancel 打断）
```

`interrupted` 是「被人为打断」的终态（区别于 `error` 的「自己崩了」）。`cancel` 和 `create_or_reject` 的 interrupt/rollback 策略都会把 run 推到 `interrupted`。

### 4.3 三种 multitask 策略

同一 thread 已有 inflight run 时，新 run 怎么处理旧的（[manager.py:552](../backend/packages/harness/deerflow/runtime/runs/manager.py#L552) 的 `create_or_reject`）：

- **reject**：抛 `ConflictError`（让前端提示「已有 run 进行中」）；
- **interrupt**：取消 inflight（保留 checkpoint），再创建；
- **rollback**：取消 inflight + 回滚到 run 前 checkpoint，再创建。

### 4.4 store-only record（跨 worker 边界）

`task` / `abort_event` 是**进程内**状态——只有创建该 run 的 worker 才有。从 `RunStore` 还原的 record 设 `store_only=True`（[manager.py:304](../backend/packages/harness/deerflow/runtime/runs/manager.py#L304)）且无 task/abort_event。这意味着**跨 worker 取消不了别人的 run**——cancel 一个 store_only record 返回 `False`。

---

## 5. 代码走读

### 5.1 RunManager 并发模型：asyncio 锁 + 线程索引

[manager.py:134](../backend/packages/harness/deerflow/runtime/runs/manager.py#L134)。所有写操作（create / set_status / cancel / create_or_reject）都经 `self._lock`（[manager.py:152](../backend/packages/harness/deerflow/runtime/runs/manager.py#L152)），保证状态机不被并发请求撕裂。

除了主 dict `_runs`，还维护二级索引 `_runs_by_thread`（`thread_id → 插入序 run_id 集合`，[manager.py:151](../backend/packages/harness/deerflow/runtime/runs/manager.py#L151)），让 per-thread 查询（`has_inflight` / `list_by_thread`）走 O(该 thread 的 run 数) 索引而非 O(全部 run) 全扫。两者在锁下同步变更（中间无 `await`），任何持锁者看到的一致。

### 5.2 create_or_reject：原子 check-and-create（消除 TOCTOU）

[manager.py:552](../backend/packages/harness/deerflow/runtime/runs/manager.py#L552) 跨「检查 inflight」与「插入新 run」**持同一把锁**（[manager.py:577](../backend/packages/harness/deerflow/runtime/runs/manager.py#L577)），消除分开 `has_inflight` + `create` 的 TOCTOU 竞态（两个并发请求都看到「无 inflight」然后都创建）。inflight run 的取消也在同一锁内完成（[manager.py:623-631](../backend/packages/harness/deerflow/runtime/runs/manager.py#L623-L631)），锁外只做 `_persist_status`（持久的 IO）。

### 5.3 SQLite busy 有界重试

[manager.py:56](../backend/packages/harness/deerflow/runtime/runs/manager.py#L56) 的 `_is_retryable_persistence_error` 识别瞬时 SQLite 锁（`database is locked` / `SQLITE_BUSY` / `SQLITE_LOCKED`，遍历异常链找）；[manager.py:199](../backend/packages/harness/deerflow/runtime/runs/manager.py#L199) 的 `_call_store_with_retry` 做有界指数退避重试（默认 5 次）。保护 run 状态终态化不被瞬时写压力卡死，同时**不重试永久失败**（如 `no such table`）——否则会把永久错误永远藏起来。

### 5.4 rowcount 驱动 recovery

`RunStore.update_status` / `update_run_completion` 返回 `False` 表示「能证明没行被更新」（行没了）。这时 `RunManager` 用内存 snapshot 调 `put` **重建行**再重试（[manager.py:277-278](../backend/packages/harness/deerflow/runtime/runs/manager.py#L277-L278)、[manager.py:339-351](../backend/packages/harness/deerflow/runtime/runs/manager.py#L339-L351)）。为什么？SQLite 后端可能因 migration / 并发 delete 丢行；与其让 run 状态悬空，不如从内存重建。

### 5.5 创建失败回滚内存

`create` / `create_or_reject` 在 `_persist_new_run_to_store` 失败时，把刚插进 `_runs` 的内存 record **回滚**（pop + unindex，[manager.py:410-414](../backend/packages/harness/deerflow/runtime/runs/manager.py#L410-L414)）。run 的初始创建是可见性边界——调用方不应在内存里看到一个 store 行还没建的 run。`finally` 而非 `except` 覆盖 cancellation（它绕过 `except Exception`）。

### 5.6 orphan 恢复

[manager.py:638](../backend/packages/harness/deerflow/runtime/runs/manager.py#L638) 的 `reconcile_orphaned_inflight_runs`：Gateway 重启后，DB 里那些「重启前创建的 pending/running」行**不可能**还有本地 worker（task 在内存，进程没了就没了）。这一步把它们标 `error`，而不是让 UI 永远显示一个活跃 run。**跳过本地仍活跃的 run**（[manager.py:672-675](../backend/packages/harness/deerflow/runtime/runs/manager.py#L672-L675)，本 worker 正在跑的不算 orphan）。

### 5.7 shutdown drain

[manager.py:705](../backend/packages/harness/deerflow/runtime/runs/manager.py#L705) 的 `shutdown(timeout=5.0)`：

1. cancel 所有在途 run 的 task（**不设状态**，[manager.py:726-731](../backend/packages/harness/deerflow/runtime/runs/manager.py#L726-L731)）；
2. bounded `asyncio.wait`（timeout，[manager.py:738](../backend/packages/harness/deerflow/runtime/runs/manager.py#L738)）；
3. **只有没自行 settle 的才标 interrupted**（[manager.py:743-753](../backend/packages/harness/deerflow/runtime/runs/manager.py#L743-L753)）——drain 期间正常完成（如 `success`）的 run 保留真实终态，不被一刀覆盖；
4. 尾部状态持久化卡在剩余预算内（防慢 store 拖死关停，[manager.py:757-776](../backend/packages/harness/deerflow/runtime/runs/manager.py#L757-L776)）。

**为什么必须在关 checkpointer 前 drain？** chat run 在后台 task 里经共享 checkpointer 写 checkpoint。关停时 checkpointer 的连接池（如 postgres）被拆；若此时还有 run task 在图执行中途，langgraph 的 `AsyncPregelLoop._checkpointer_put_after_previous` 会在已关的池上跑 `aput(...)`。那个 put 跑在 langgraph 内部 task 里（不在 `run_agent` 调用栈上），导致的 `PoolClosed` worker 捕获不到，会在 `asyncio.run()` 关闭时作为未处理异常冒上来。

### 5.8 幂等 cancel

[manager.py:523](../backend/packages/harness/deerflow/runtime/runs/manager.py#L523) 的 `cancel`：已 `interrupted` 的再 cancel 直接返回 `True`（[manager.py:538-539](../backend/packages/harness/deerflow/runtime/runs/manager.py#L538-L539)）——避免「用户连点两次停止」时第二次报错。返回 `False` 只在「本 worker 不认识这个 run」或「run 已到 interrupted 以外的终态」。

### 5.9 worker `run_agent`：后台执行的 8 步

[worker.py:125](../backend/packages/harness/deerflow/runtime/runs/worker.py#L125) 在后台 task 里：

1. **标 running** + 快照 pre-run checkpoint（供 rollback，含 `pending_writes` 深拷贝，[worker.py:191-196](../backend/packages/harness/deerflow/runtime/runs/worker.py#L191-L196)）；
2. 发 `metadata` 事件（`useStream` 要 run_id + thread_id）；
3. 构建 agent：注入 `__pregel_runtime`（langgraph 的 `runtime.context`，[worker.py:225-226](../backend/packages/harness/deerflow/runtime/runs/worker.py#L225-L226)）+ `__run_journal` 哨兵键（[worker.py:222-223](../backend/packages/harness/deerflow/runtime/runs/worker.py#L222-L223)，让中间件写审计事件）+ Langfuse metadata（[worker.py:236](../backend/packages/harness/deerflow/runtime/runs/worker.py#L236)）+ RunJournal callback（[worker.py:230-231](../backend/packages/harness/deerflow/runtime/runs/worker.py#L230-L231)）；
4. 挂 checkpointer / store / interrupt 节点；
5. `agent.astream(stream_mode=[...])` 流式驱动，每个 chunk 经 `serialize` 发到 bridge；**abort 在迭代边界检查**（[worker.py:305](../backend/packages/harness/deerflow/runtime/runs/worker.py#L305)）；
6. 终态决定（[worker.py:331-357](../backend/packages/harness/deerflow/runtime/runs/worker.py#L331-L357)）：abort+rollback→error+rollback；abort+interrupt→interrupted；LLM 兜底→error；否则 success；
7. `except asyncio.CancelledError`（[worker.py:359](../backend/packages/harness/deerflow/runtime/runs/worker.py#L359)）：task 被 cancel 时同上按 action 决定终态；
8. `finally`（[worker.py:392](../backend/packages/harness/deerflow/runtime/runs/worker.py#L392)）：flush journal + 持久化 completion + 标题回写 thread_meta + `publish_end`。

> worker 把 `graph_input` **原样**传给 `agent.astream`，不吞 `Command(resume=...)`——human-in-the-loop 的「恢复」走 LangGraph 原生路径（前端把 resume payload 当 `graph_input` 传进来，langgraph 自己识别并驱动 interrupt 节点继续）。

### 5.10 rollback 深拷贝快照还原

[worker.py:448](../backend/packages/harness/deerflow/runtime/runs/worker.py#L448) 的 `_rollback_to_pre_run_checkpoint`：无快照（首次 run）→ `adelete_thread` 清空；有快照 → `aput` 写回 checkpoint（带新 id/ts 标记）+ `aput_writes` 还原 `pending_writes`。**为什么 run 前要深拷贝？** checkpoint 的 `pending_writes` 是可变列表，run 期间会被改；深拷贝（[worker.py:195](../backend/packages/harness/deerflow/runtime/runs/worker.py#L195)）确保 rollback 用的是 run **前**的完整状态。

### 5.11 LLM 兜底消息抽取

模型调用中间件（`LLMErrorHandlingMiddleware`）的兜底 AIMessage **不保证过 LLM end 回调**（它是中间件合成的，不是真模型返回的），但会出现在图状态 chunk 里。[worker.py:581](../backend/packages/harness/deerflow/runtime/runs/worker.py#L581) 的 `_extract_llm_error_fallback_message` 扫流式 chunk 找 `additional_kwargs.deerflow_error_fallback` 标记：**快路径**——`values` chunk 有顶层 `messages` list → 只扫那个（避免对大状态 dict 深递归）；**深扫**——`updates`/`messages` 等 chunk 小，全递归可接受。找到就把 run 标 `error`。

---

## 6. 数据流

### 6.1 创建 run 端到端

```
请求处理函数
   │ RunManager.create_or_reject(thread_id, multitask_strategy, on_disconnect, ...)
   ▼
持锁：检查 inflight →（reject 抛错 / interrupt·rollback 取消 inflight）→ 插入 _runs + _runs_by_thread
   │ _persist_new_run_to_store（失败则 pop 回滚内存）
   ▼
返回 RunRecord（pending）
   │ asyncio.create_task(run_agent(bridge, run_manager, record, ctx=RunContext(...), agent_factory, graph_input, config))
   ▼
run_agent 后台跑：running → 流式 chunk → bridge → SSE → 终态 → publish_end
```

### 6.2 按模型归桶 token（一次 run 可能调多个模型）

「一个 run 里调了 `gpt-4o` 又调了 `gpt-4o-mini`，各自花了多少 token？」要回答这个，token 不能只记一个总数，得**按模型分桶**。这是跨多模块的纵向链路（写侧 → 传递 → 读侧）：

```
① 写侧（run 进行中，逐次 LLM 调用记下来）
   RunJournal.on_llm_end(每条 AIMessage)
        │  message.response_metadata.model_name  ← provider 返回的真模型（不是配置里写的）
        │  message.usage_metadata.{input,output,total}_tokens
        ▼
   RunJournal._record_model_usage(model_name, input, output, total)
        │  total<=0 跳过；model_name 为 None → 桶名 "unknown"
        │  累加进 self._tokens_by_model[model] = {tokens, runs}
        │
   子代理路径：SubagentTokenCollector.on_llm_end 同样取 model_name
        → record_external_llm_usage_records → 同一个 _record_model_usage（子代理 token 回灌父 run）

② 传递（run 结束，journal → worker → manager → store）
   RunJournal.get_completion_data()
        │  返回 token_usage_by_model: {model: {tokens, runs}}（深拷贝防外部改）
        ▼
   worker → run_manager.update_run_completion(token_usage_by_model=...)  ← worker.py:402-403
        ▼
   RunRecord.token_usage_by_model（内存 dataclass 字段）+ store 行（持久化）

③ 读侧（查询历史花销）
   RunStore.aggregate_tokens_by_thread(thread_id)   ← 走 _runs_by_thread 索引
        │  遍历该 thread 的 completed run
        │  优先读 row.token_usage_by_model → 逐模型累加
        │  老行（无此字段）→ 回退 row.model_name（单模型时代的字段）
        ▼
   {model: {tokens, runs}}  ← UI 展示「各模型花了多少」
```

**为什么用 `response_metadata.model_name` 而非 config 里写的 model？** 一个 agent 可能路由到多个模型（主模型 + 小模型兜底 / 子代理换模型）；真正的计费模型由 provider **返回**的 `response_metadata` 决定。记配置里的会错把兜底模型算成主模型。

---

## 7. 配置

**`RunManager.__init__`**（[manager.py:141](../backend/packages/harness/deerflow/runtime/runs/manager.py#L141)）：

| 参数 | 作用 | 默认 |
|------|------|------|
| `store` | `RunStore` 持久化后端（None=纯内存，重启丢） | `None` |
| `persistence_retry_policy` | SQLite busy 重试策略（max_attempts/退避） | `PersistenceRetryPolicy(max_attempts=5)` |

**`RunRecord` 关键字段**（[manager.py:94](../backend/packages/harness/deerflow/runtime/runs/manager.py#L94)）：`run_id` / `thread_id` / `status` / `on_disconnect`（cancel/continue）/ `multitask_strategy` / `task`（后台 task）/ `abort_event` / `abort_action`（interrupt/rollback）/ `store_only` / `token_usage_by_model`（按模型分桶）/ 各类 token 计数。

**`RunContext`**（[worker.py:77](../backend/packages/harness/deerflow/runtime/runs/worker.py#L77)）：`checkpointer` / `store`（LangGraph BaseStore）/ `event_store` / `thread_store` / `app_config`。

---

## 8. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **agents** | `agent_factory`（`make_lead_agent`）由 worker 调用构建图 |
| **checkpointer** | `RunContext.checkpointer`——run 写 checkpoint；rollback 读 pre-run 快照；shutdown drain 防池关早 |
| **events/store** | `RunContext.event_store`——RunJournal 写事件 |
| **journal** | worker 注入 RunJournal 作 callback + `__run_journal` 哨兵；token 用量（按模型归桶）/ completion 从它取 |
| **stream_bridge** | worker 把流式 chunk 发到 bridge；`publish_end` 收尾 |
| **serialization** | chunk 经 `serialize(mode=...)` 转 JSON 再发 bridge |
| **tracing** | worker 注入 Langfuse metadata（session/user/name/tags） |
| **user_context** | `get_effective_user_id()` 给 Langfuse metadata |
| **persistence** | `RunRepository(RunStore)` SQL 实现（跨重启持久化 run 历史） |

---

## 9. 设计动机分析

### 9.0 核心设计动机表

| 设计 | 为什么 | 不这么设计会怎样 |
|------|--------|------------------|
| **双类分工**（manager vs worker） | 状态机逻辑 vs 执行逻辑解耦 | 一坨：改锁怕动执行、改执行怕动状态 |
| **asyncio 锁 + 线程索引** | 写全加锁保一致；per-thread 查询走索引 | 并发撕裂状态机 / list_by_thread 全扫慢 |
| **create_or_reject 跨 check+insert 持锁** | 消除 TOCTOU | 两个并发请求都创建 run |
| **三 multitask 策略** | 不同业务场景要不同并发语义 | 只有 reject：用户体验差；只有 rollback：误删进行中工作 |
| **busy 重试不藏永久失败** | 只重试瞬时 SQLite 锁 | `no such table` 永远藏起来，bug 难查 |
| **rowcount 驱动 recovery** | update_status 返 False 时从内存重建行 | run 状态悬空，store 行莫名消失 |
| **创建失败回滚内存** | store put 失败时保可见性边界 | 内存里有个「幽灵 run」store 里却没有 |
| **orphan 恢复** | 重启后 inflight 行不可能有本地 worker | UI 永远显示一个活跃 run，用户困惑 |
| **shutdown drain** | 关 checkpointer 前让在途 run flush | `PoolClosed` 未处理异常在 asyncio.run 冒出 |
| **drain 只标未 settle 的 interrupted** | drain 期间正常完成的保留真实终态 | 一刀覆盖，success 变 interrupted |
| **幂等 cancel** | 已 interrupted 再 cancel 当成功 | 用户连点停止第二次报错 |
| **rollback 深拷贝 pending_writes** | 还原 run 前完整状态 | pending_writes 被改后还原不全 |
| **store-only record 取消返 False** | 本 worker 停不了别的 worker 的 run | 假装取消成功，run 其实还在跑 |
| **token 按真计费模型归桶** | 一个 run 可能调多个模型 | 兜底模型算进主模型，账不准 |

### 9.1 为什么 create_or_reject 必须跨 check + insert 持锁

最朴素的实现是两个方法：先调 `has_inflight(thread_id)` 检查，再调 `create(thread_id)` 创建。问题是这俩之间有个**时间窗**：

```
请求 A：has_inflight() → False ──────（窗口）────── create()
请求 B：           has_inflight() → False ────── create()
```

A 和 B 都在窗口里看到「无 inflight」，于是都创建——同一线程冒出两个并发 run，状态机被撕裂。

`create_or_reject` 把「检查 inflight + 取消 inflight + 插入新 run」**全放进 `async with self._lock` 一把锁**里（[manager.py:577-631](../backend/packages/harness/deerflow/runtime/runs/manager.py#L577-L631)）。持锁期间没有 `await`，别的协程进不来，check 和 insert 对外界就是原子的。这就是消除 TOCTOU 的标准手法。

### 9.2 为什么 shutdown 要 drain（且只标未 settle 的）

后台 run task 经共享 checkpointer 写 checkpoint。关停流程会拆 checkpointer 的连接池。如果还有 run task 在图执行中途，langgraph 的 pregel loop 会在它的内部 task 里（不在 worker 调用栈上）调 `checkpointer.aput(...)`——这个 put 命中已关的池，抛 `PoolClosed`。worker 的 try/except 捕获不到（不在它调用栈），异常在 `asyncio.run()` 关闭时作为「未处理异常」冒出。

`shutdown(timeout)` 先 cancel 所有在途 task，再 bounded-await（[worker.py:738](../backend/packages/harness/deerflow/runtime/runs/worker.py#L738) 的 `asyncio.wait`），让每个能在 timeout 内 settle 的 run 趁资源还开着 flush 最终 checkpoint。

**为什么只标未 settle 的 interrupted？** drain 期间可能正好有 run 正常跑完（success）。如果一刀把所有 drain 中的 run 标 interrupted，就把真实终态覆盖了。所以 worker 检查：task 在 timeout 内完成的（不在 pending 里）→ 保留它自己设的终态（还顺手 `task.exception()` 取走异常免得「never retrieved」）；只有 timeout 后仍 active 的才标 interrupted。

### 9.3 为什么 rollback 要深拷贝 pending_writes

`pre_run_snapshot` 在 run 开始前从 checkpointer `aget_tuple` 拿到的快照里有 `pending_writes`——它是**可变列表**，run 期间 langgraph 会改它（追加新的 writes）。如果 rollback 时直接引用这个列表，拿到的是 run 跑过之后的、被改过的状态，不是 run **前**的。

`copy.deepcopy`（[worker.py:195](../backend/packages/harness/deerflow/runtime/runs/worker.py#L195)）在 run 前冻结一份完整状态。rollback 时 `aput` 写回 checkpoint + `aput_writes` 还原深拷贝的 pending_writes（[worker.py:517-538](../backend/packages/harness/deerflow/runtime/runs/worker.py#L517-L538)），确保还原到 run 前的精确状态。

### 9.4 为什么 cancel 要幂等

用户点了「停止」，网络可能抖，前端可能重发取消请求；或者用户连点两次。如果第二次 cancel 报错（「run 已取消」），前端就得专门处理这种「其实成功了但报错」的情况，体验差。

幂等设计（[manager.py:538-539](../backend/packages/harness/deerflow/runtime/runs/manager.py#L538-L539)）：run 已是 `interrupted` → 直接返回 `True`（当成功）。这样「取消一次」和「取消十次」效果一样，前端不用区分。返回 `False` 只在真正无法取消时（store-only / 已到别的终态），含义明确。

---

## 10. 实现差异（vs 上游 deer-flow 源码）

对照两侧 `backend/packages/harness/deerflow/runtime/runs/`（7 个文件一一对应），**剥 docstring/comment 后判逻辑差**。结论：**这是迄今最忠实的移植——7 个文件里 6 个剥 docstring 后逐字节相同，运行管理的全部状态机不变量（busy 重试 / create_or_reject 消除 TOCTOU / rowcount recovery / orphan 恢复 / shutdown drain / 幂等 cancel / rollback 深拷贝快照）逐行一致；唯一差异是 `__init__.py` 的导出方式与多导出两个符号**。

### 10.1 6/7 文件剥 docstring 后逐字节相同

| 文件 | stripped mini | stripped 上游 | 结论 |
|------|---------------|---------------|------|
| `manager.py` | 4121 | 4121 | **逐字节相同** |
| `worker.py` | 3187 | 3187 | **逐字节相同** |
| `store/base.py` | 606 | 606 | **逐字节相同** |
| `store/memory.py` | 1270 | 1270 | **逐字节相同** |
| `naming.py` | 88 | 88 | **逐字节相同** |
| `schemas.py` | 40 | 40 | **逐字节相同** |
| `store/__init__.py` | 31 | 31 | **逐字节相同** |

这意味着 `RunManager` 的全部并发逻辑（锁 + 线程索引、create_or_reject、busy 重试、rowcount recovery、orphan reconcile、shutdown drain、幂等 cancel）、worker 的全部执行逻辑（runtime context 注入、rollback、LLM 兜底抽取、终态决定）、`RunStore` ABC 的 11 个抽象方法、`MemoryRunStore` 的 `_runs_by_thread` 索引——**都与上游一字不差**（差的全是中英 docstring 翻译）。`_runs_by_thread` 二级索引、`token_usage_by_model` 按模型归桶、`inject_langfuse_metadata` 注入这些特性两边都有，无「mini 简化」之说。

### 10.2 `__init__.py`：导出方式 + 多导出两个符号（API 面）

- 上游用**相对导入**（`from .manager import ...`）；mini 用**绝对导入**（`from deerflow.runtime.runs.manager import ...`）。等价。
- mini **多导出** `MemoryRunStore` / `RunStore`（让 store ABC 从包顶层可直接 import）。这是 API 面差异，无逻辑差。

### 10.3 几个特性的来源说明

`_runs_by_thread` 二级索引、`token_usage_by_model` 按模型归桶、`inject_langfuse_metadata` 注入、worker 把 `graph_input` 原样直传 `agent.astream`（走 LangGraph 原生 resume 路径）——这些**两边源码都有**，逐行一致。mini 与上游同步演进，没有「mini 简化掉」或「mini 补齐」的特性。

**测试覆盖**：`test/test_run_manager.py`（57 测试）+ `test/test_worker.py`（39 测试），共 **96 个 dedicated runs 测试**，覆盖状态机、并发、三策略、orphan 恢复、drain、幂等 cancel、rollback、LLM 兜底抽取。

---

## 11. 排错 FAQ

- **「同 thread 两条消息报 ConflictError」**：`multitask_strategy=reject` 且已有 inflight。要排队改 `interrupt`/`rollback`，或等前一个 run 结束。
- **「重启后 UI 显示一个永远 active 的 run」**：`reconcile_orphaned_inflight_runs` 没在启动时调，或用的是 `MemoryRunStore`（重启后 dict 为空，无可恢复行；只有 sqlite/postgres 的 `RunRepository` 才有跨重启的持久行）。
- **「关停时报 PoolClosed / asyncio 未处理异常」**：没在关 checkpointer 前 `shutdown()` drain。drain 让在途 run 趁资源还开着 flush checkpoint。
- **「cancel 返回 False 但 run 还在跑」**：那是 store-only record（别的 worker 的 run），本 worker 停不了。或 run 已到终态。
- **「rollback 后状态没还原」**：`snapshot_capture_failed`（run 前 aget_tuple 抛错）会跳过 rollback；或无 checkpointer。查日志「Could not capture pre-run checkpoint snapshot」。
- **「LLM 挂了 run 却标 success」**：兜底消息没贴 `deerflow_error_fallback` 标记，或不在 `messages` channel。`_extract_llm_error_fallback_message` 只扫 values chunk 的顶层 messages list + 其它 chunk 深扫。
- **「update_status 返 False 一直循环」**：那是 rowcount recovery 在工作——它会 put 重建行再重试。若 store 真的 put 也失败，看日志「Failed to persist」。
- **「token 账对不上（兜底模型算进主模型）」**：检查是不是用了 `response_metadata.model_name`（provider 返回的真模型）而非配置里的 model 名。

---

**下一篇**：[runtime_store.md](runtime_store.md)（LangGraph BaseStore 工厂）——本模块的 `RunContext.store` 已为它预留；它落地后 worker 的 `agent.store = store` 即生效。[architecture.md](architecture.md)（集成装配总览）会把 `RunManager` + `run_agent` 串进完整请求路径。
