# 10. run_journal.md — RunJournal（LangChain 回调 → 事件采集 + token 核算）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：RunJournal 是一个 **LangChain 回调处理器**——它「旁听」agent 运行过程中的每个事件（LLM 调用、工具调用、链开始/结束），把事件攒进 buffer 批量写入 RunEventStore，**同时累加 token 用量**。它是 RunEventStore 的**写入侧**采集器（[#9 run_event_store.md](run_event_store.md) 是**存储侧**）——一个管「记什么、token 怎么算」，一个管「记到哪、seq 单调」。

> 配套代码：[runtime/journal.py](../backend/packages/harness/deerflow/runtime/journal.py)（单文件，~600 行）。测试见 [test/test_journal.py](../test/test_journal.py)。

## 学完这篇你能回答什么（learning outcomes）

- LangChain 的 `BaseCallbackHandler` 钩子是**同步**的，但 `store.put_batch` 是**异步**的——回调里既不能 `await`、又不能阻塞，RunJournal 怎么桥接（`loop.create_task` 调度 + 无循环时留 buffer 等 `flush`）？
- 同一个 LLM 响应 LangChain 可能触发**多次** `on_llm_end`——为什么直接累加 token 会双计？怎么去重（`_counted_llm_run_ids` 按 langchain run_id）？
- 「首条 human 消息」为什么在 `on_chat_model_start` 抽、而不是 `on_chain_start` 或 `on_llm_end`（前者每节点都触发太乱、后者消息可能已被 checkpoint 裁剪，`on_chat_model_start` 消息完整结构化且只真实 LLM 调用时触发）？
- 同一次 run 可能调多个模型（主 agent + 多个子代理）——token 怎么**按真计费模型分桶**（`response_metadata.model_name`）？为什么记返回的 model_name 而非 config 写的？
- 失败的 batch 为什么**回插 buffer 重试**而不是丢（`database is locked` 时事件不丢，宁可重复尝试）？
- 为什么 RunJournal **不实现** `on_llm_new_token`（流式每 token 一次回调，buffer/写库爆炸——流式展示走 stream_bridge）？
- `progress_reporter` 为什么由 worker **注入**而不是 journal 直接 import run_manager（避免模块循环依赖）？

> 这些都是 LLM 应用 / agent 工程面试的高频点——「回调机制」「token 计费去重」「同步↔异步桥接」「防双计 / 防丢失」。

---

## 1. 为什么需要它（痛点）

先看「没有它」会怎样：

- **事件散落各处，没法统一存**。LangGraph agent 跑起来，LLM 调用、工具调用、中间件状态变更发生在十几个地方。每个地方各自往 store 写 → 代码重复、格式不一、漏写。
- **token 双计**。LangChain 的回调机制有时会对同一个 LLM 响应触发多次 `on_llm_end`。直接累加 token，一次调用可能被算 2-3 次，账单爆炸。
- **同步回调阻塞事件循环**。`BaseCallbackHandler` 的方法是**同步**的（LangGraph 在事件循环里同步调用它们）。回调里直接 `await store.put_batch(...)` 会报错（同步函数不能 await）；开个阻塞写又会卡住整个 agent。
- **不知道 run 出错没有**。worker 跑完要知道「这次是成功还是 LLM 兜底了」，没有统一的 fallback 检测就得各自判断。
- **进度看不见**。长 run 跑了 5 分钟，前端想看「已经用了多少 token」，没有定期快照就只能等结束。

RunJournal 解决这些：**统一回调采集**、**token 按 run_id 去重**、**同步回调里调度异步刷盘**、**error fallback 检测**、**节流的进度快照**。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉回调机制 / token 计费 / 异步刷盘的话，先读这一节。

### 回调（callback）/ BaseCallbackHandler

**回调** = 「事件发生时，框架自动调你的某个方法」。LangChain 的 `BaseCallbackHandler` 定义了一组钩子方法：`on_chain_start`、`on_llm_end`、`on_tool_end` 等。你继承它、实现这些方法，LangGraph 跑 agent 时就会在对应时机调它们。类比：RunJournal 是一个「**监工**」，agent 干活时每个关键步骤都喊它一声（回调），它负责记录。

### 写入侧 / 存储侧

- **写入侧**（RunJournal）：产生事件、决定「记什么、怎么记、token 怎么算」，把记录塞进 buffer。
- **存储侧**（RunEventStore，[#9](run_event_store.md)）：负责「记到哪」（memory/jsonl/db）、seq 单调、查询。

两者解耦：RunJournal 不关心存哪（只调 `store.put_batch`），RunEventStore 不关心事件语义（只存 dict）。换存储后端不影响采集逻辑。

### token 用量 / usage_metadata

每次 LLM 调用，响应里带 `usage_metadata`：`{input_tokens, output_tokens, total_tokens}`——这次调用读了多少 token（输入）、写了多少（输出）。这是**计费和限流**的基础。RunJournal 累加这些，run 结束时写到 RunRow，供「这次 run 烧了多少 token」查询。

### caller（调用方）分桶

一次 run 里，调用 LLM 的不只是主 agent（`lead_agent`）——子代理（`subagent`）、中间件（`middleware`，如标题生成）也会调 LLM。为了知道「token 都是哪些角色烧的」，RunJournal 按 caller 把 token 分桶（[第 413 行](../backend/packages/harness/deerflow/runtime/journal.py#L413) `_identify_caller`）：

- `lead_agent`：主 agent。
- `subagent:{name}`：子代理。
- `middleware:{name}`：中间件。

靠 **tags 注入**识别：子代理/中间件在调模型时给自己的回调打 tag，RunJournal 读 tag 分桶。

### buffer + flush_threshold

回调是**高频**的（一个 run 可能几十上百条事件）。每条都立即写库（特别是 SQLite）会很慢。所以 RunJournal 攒一个 **buffer**，攒到 `flush_threshold`（默认 20）条才批量写（`put_batch`）——一次事务写一批，高效。

---

## 3. 整体结构：它在系统里的位置

```
runtime/
└── journal.py    # RunJournal(BaseCallbackHandler)：单文件，~600 行
```

它在系统里的位置（夹在 LangChain 回调机制 与 RunEventStore 之间）：

```
LangChain/LangGraph 回调机制
        │（on_llm_end / on_chat_model_start / on_tool_end / ...）
        ▼
   runtime/journal.RunJournal  ──put_batch──→  runtime/events/store（RunEventStore，存储侧 #9）
        │                                            │
        │ token 累加（去重 + 按 caller/模型分桶）       │ 落盘（memory/jsonl/db）
        │ error fallback 检测                          │
        │ progress 节流快照                            │
        ▼                                            ▼
   get_completion_data() ──→ runs/worker 写 RunRow（#26，run 完成时的 token/首末消息）
        │
   progress_reporter（worker 注入的 Callable）──→ RunManager.update_run_progress（#26）
```

依赖：[#9 RunEventStore](run_event_store.md)（存储侧，硬依赖）、`langchain_core`（回调 + 消息）、`langgraph.types.Command`（`on_tool_end` 的 Command 输出）。**不**依赖 run_manager（`progress_reporter` 由 worker 注入，无循环）。

---

## 4. 核心概念

### 4.1 RunJournal 类（[journal.py:46](../backend/packages/harness/deerflow/runtime/journal.py#L46)）

继承 `BaseCallbackHandler`。构造（[第 49 行](../backend/packages/harness/deerflow/runtime/journal.py#L49)）：

```python
RunJournal(
    run_id: str,
    thread_id: str,
    event_store: RunEventStore,
    *,
    track_token_usage: bool = True,
    flush_threshold: int = 20,
    progress_reporter: Callable[[dict], Awaitable[None]] | None = None,
    progress_flush_interval: float = 5.0,
)
```

内部状态分四组：① 写 buffer（`_buffer` + `_pending_flush_tasks`）；② token 累加器（总量 + 按 caller 分桶 + 按模型分桶）；③ 去重集合（三个 `_counted_*` set）；④ 便利字段（首末消息 / msg_count / error fallback）。

### 4.2 三类事件来源

RunJournal 把三类回调标准化成 RunEvent 记录：

| 回调 | 产生的事件 | 关键逻辑 |
|------|-----------|----------|
| `on_chain_start`（[第 130 行](../backend/packages/harness/deerflow/runtime/journal.py#L130)） | `run.start`（trace） | 仅根调用（`parent_run_id=None`） |
| `on_chat_model_start`（[第 177 行](../backend/packages/harness/deerflow/runtime/journal.py#L177)） | `llm.human.input`（message） | 抽首条 human 消息（§6.4） |
| `on_llm_end`（[第 226 行](../backend/packages/harness/deerflow/runtime/journal.py#L226)） | `llm.ai.response`（message） | token 累加去重 + 按模型分桶 + error fallback 检测 |
| `on_tool_end`（[第 332 行](../backend/packages/harness/deerflow/runtime/journal.py#L332)） | `llm.tool.result`（message） | ToolMessage 或 Command 输出 |

---

## 5. 代码走读：重要函数逐个讲

### 5.1 同步回调 → 异步刷盘（`_flush_sync`，[第 370 行](../backend/packages/harness/deerflow/runtime/journal.py#L370)）

`BaseCallbackHandler` 方法是**同步**的，但 `store.put_batch` 是**异步**的。回调里不能直接 `await`。解法（详见 §6.1）：

```python
def _flush_sync(self):
    if not self._buffer: return
    if self._pending_flush_tasks: return      # :379 已有 flush 在途则跳过（防并发写撞锁）
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                 # :384 无事件循环——留 buffer 待 async flush
    batch = self._buffer.copy(); self._buffer.clear()
    task = loop.create_task(self._flush_async(batch))   # :388 调度异步写，回调立即返回
    self._pending_flush_tasks.add(task)
```

`_flush_async`（[第 392 行](../backend/packages/harness/deerflow/runtime/journal.py#L392)）里 `put_batch` 抛错时**回插 buffer 头部**（`self._buffer = batch + self._buffer`，[第 403 行](../backend/packages/harness/deerflow/runtime/journal.py#L403)），下次 flush 重试——**事件不丢**。

### 5.2 token 累加 + 去重 + 分桶（`on_llm_end`，[第 226 行](../backend/packages/harness/deerflow/runtime/journal.py#L226)）

从 `message.usage_metadata` 取 `input_tokens` / `output_tokens` / `total_tokens`（[第 293-295 行](../backend/packages/harness/deerflow/runtime/journal.py#L293)）。然后：

- **去重**（[第 298 行](../backend/packages/harness/deerflow/runtime/journal.py#L298)）：`if total_tk > 0 and rid not in self._counted_llm_run_ids`——同一 langchain run_id 只计一次。
- **按 caller 分桶**（[第 305-310 行](../backend/packages/harness/deerflow/runtime/journal.py#L305)）：`subagent:` → `_subagent_tokens`，`middleware:` → `_middleware_tokens`，否则 `_lead_agent_tokens`。
- **按模型分桶**（`_record_model_usage`，[第 421 行](../backend/packages/harness/deerflow/runtime/journal.py#L421)）：从 `response_metadata.model_name` 取真计费模型，累加进 `_tokens_by_model`（[第 317 行](../backend/packages/harness/deerflow/runtime/journal.py#L317) 调用）。`total_tokens<=0` 跳过（[第 432 行](../backend/packages/harness/deerflow/runtime/journal.py#L432)，防 0 值污染桶），model_name 取不到归 `"unknown"`（[第 435 行](../backend/packages/harness/deerflow/runtime/journal.py#L435)）。

> 为什么记 `response_metadata.model_name` 而非 config 写的 model？因为**真计费模型由 provider 返回决定**——一个 agent 可能配了 `gpt-4o`，但请求被路由到 fallback 或别名，返回的 `model_name` 才是真正计费的那个。这与 [#7 persistence.md](persistence.md) §6.4 的 `token_usage_by_model` 列配套。

### 5.3 抽首条 human（`on_chat_model_start`，[第 205-220 行](../backend/packages/harness/deerflow/runtime/journal.py#L205)）

从 messages **反向**找第一条 `HumanMessage`，**跳过** `name="summary"`（摘要注入，不是用户原话）和 `hide_from_ui=True`（[第 208 行](../backend/packages/harness/deerflow/runtime/journal.py#L208)）。只抽一次（`_first_human_msg` 未设时才抽，[第 205 行](../backend/packages/harness/deerflow/runtime/journal.py#L205)）。为什么在这里抽见 §6.4。

### 5.4 error fallback 检测（`on_llm_end` 内，[第 256-266 行](../backend/packages/harness/deerflow/runtime/journal.py#L256)）

LLM 调用失败时，`LLMErrorHandlingMiddleware`（[#24 middlewares.md](middlewares.md)）生成「兜底 AI 消息」（带 `additional_kwargs.deerflow_error_fallback=True`）让 run 优雅继续。RunJournal 检测这个标记，设 `had_llm_error_fallback=True` + 记消息（`error_detail` / `error_reason` / 文本兜底）。worker 据此判断「这次 run 其实是 error 兜底」。

### 5.5 进度节流（`_schedule_progress_flush`，[第 539 行](../backend/packages/harness/deerflow/runtime/journal.py#L539)）

`progress_reporter`（worker 注入的 async callable）定期把 token 快照写到 RunRow，让前端看「进行中的 run 用了多少 token」。但 LLM 调用可能很密，不能每次都写（写库开销）。节流：`progress_flush_interval`（默认 5 秒，[第 545 行](../backend/packages/harness/deerflow/runtime/journal.py#L545)）内只写一次，interval 内的触发标 dirty、调度延迟任务到 interval 结束再写——**保证写库频率有上限**。

### 5.6 汇总（`get_completion_data`，[第 590 行](../backend/packages/harness/deerflow/runtime/journal.py#L590)）

返回 run 完成时累加的全部 token + 消息数据，供 worker 写 RunRow：

```python
{
    "total_input_tokens", "total_output_tokens", "total_tokens", "llm_call_count",
    "lead_agent_tokens", "subagent_tokens", "middleware_tokens",
    "token_usage_by_model": {model: dict(usage) ...},   # :601 深拷贝防外部改 accumulator
    "message_count", "last_ai_message", "first_human_message",
}
```

---

## 6. 设计权衡与踩坑

### 6.1 同步回调 → 异步刷盘（核心）

`BaseCallbackHandler` 方法是同步的，`store.put_batch` 是异步的，回调里不能 `await`。解法（§5.1）：

1. 回调里把事件 append 到 buffer。
2. 达阈值时调 `_flush_sync`：检测当前有没有事件循环在跑（`asyncio.get_running_loop()`）。有 → `loop.create_task` 调度异步写，**回调立即返回**（不阻塞）；没有 → 留 buffer，等 worker `finally` 里的 async `flush()`。
3. `_pending_flush_tasks` 记录在途任务——**已有 flush 在跑就跳过新的**，防多个 fire-and-forget 任务并发写同一个 SQLite 文件（撞锁）。

这是「同步世界」与「异步世界」的桥接，关键是**绝不阻塞回调**。

### 6.2 失败 batch 回插

`_flush_async` 里 `put_batch` 抛错（如 `database is locked`）时，**把整批事件回插 buffer 头部**，下次 flush 重试（§5.1）。**事件不丢**——宁可重复尝试，不能丢记录。测试 `test_failed_batch_returned_to_buffer` 锁住这个。

### 6.3 token 按 run_id 去重（防双计）

LangChain 可能对**同一个 LLM 响应**触发多次 `on_llm_end`（不同的回调路径）。如果每次都累加 token，一次调用算多次。解法：用 `_counted_llm_run_ids` 集合记录「已计过 token 的 langchain run_id」（[第 298 行](../backend/packages/harness/deerflow/runtime/journal.py#L298)）。同一 run_id 第二次来，跳过累加。**宁可少计（漏掉罕见的真重复），不可多计（账单错）**。测试 `test_dedup_same_run_id` 锁住。

### 6.4 为什么在 on_chat_model_start 抽首条 human

「首条 human 消息」（run 的用户输入）是 RunRow 的关键字段（`first_human_message`）。在哪抽？

- `on_chain_start`？每个图节点都触发，太多太乱。
- `on_llm_end`？那时消息可能已被 checkpoint 裁剪。
- **`on_chat_model_start`**：只在真实 LLM 调用时触发，此处 prompt 消息是**完整结构化**的、**未被裁剪**。

所以在这里从 messages 反向找第一条 `HumanMessage`（跳过 summary / hide_from_ui），只抽一次（§5.3）。

### 6.5 last_ai_message 只认 lead_agent

RunRow 的 `last_ai_message` 应代表「主 agent 给用户的最终回答」。子代理/中间件的 AI 消息、以及只有 tool_calls 的空 AI 消息，都不该覆盖它。规则（`_record_message_summary`，[第 125 行](../backend/packages/harness/deerflow/runtime/journal.py#L125)）：只当 `caller is None or caller == "lead_agent"` 且文本非空时更新，并截断 `[:2000]`。测试 `test_subagent_ai_does_not_overwrite` 锁住。

### 6.6 不实现 on_llm_new_token

流式 token（`on_llm_new_token`）每个 token 触发一次——一次回复几百次回调，buffer 爆炸、写库爆炸。所以 RunJournal **不**实现它，只在 `on_llm_end` 收**完整消息**。流式展示走 stream_bridge（[#11 stream_bridge.md](stream_bridge.md)），不走 event store。

### 6.7 progress_reporter 注入而非 import（无循环依赖）

`progress_reporter` 由 worker（[#26 runs.md](runs.md)）注入——journal 构造时收一个 `Callable[[dict], Awaitable[None]]`，**不 import run_manager**。这样 runs 模块依赖 journal（构造它），journal 不反向依赖 runs，避免循环依赖。journal 模块本身只依赖 RunEventStore + langchain_core。

---

## 7. 配置与用法

### 7.1 基本采集

```python
from deerflow.runtime.journal import RunJournal
from deerflow.runtime.events.store.memory import MemoryRunEventStore

store = MemoryRunEventStore()
journal = RunJournal("run-1", "thread-1", store, flush_threshold=10)

# 把 journal 作为回调传给图
await graph.ainvoke(input, config={"callbacks": [journal], ...})

# run 结束后强制 flush 残余 + 拿汇总
await journal.flush()
print(journal.get_completion_data())  # token 用量 + 首末消息
```

### 7.2 worker 注入进度上报（[#26 runs.md](runs.md)）

```python
journal = RunJournal(
    "run-1", "thread-1", store,
    progress_reporter=run_manager.update_run_progress,  # async callable，注入
    progress_flush_interval=5.0,
)
```

### 7.3 中间件记录状态变更

```python
journal.record_middleware(
    "title", name="TitleMiddleware", hook="after_model",
    action="generate_title", changes={"title": "新标题"},
)
```

### 7.4 子代理等外部 token 记录

```python
journal.record_external_llm_usage_records([
    {"source_run_id": "sub-1", "caller": "subagent:general-purpose",
     "model_name": "deepseek-chat", "input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
])
# 按 source_run_id 去重，按 caller/模型分桶（§5.2）
```

---

## 8. 与其它模块的关系

- **依赖**：[#9 RunEventStore](run_event_store.md)（存储侧，硬）、`langchain_core`、`langgraph.types.Command`。**不**依赖 run_manager（progress_reporter 注入，无循环）。
- **被依赖**：
  - [#26 runs.md](runs.md)：worker 构造 RunJournal、注入 `progress_reporter`、调 `flush()` + `get_completion_data()` 写 RunRow。
  - [#24 middlewares.md](middlewares.md)：中间件调 `record_middleware()` 记状态变更；`LLMErrorHandlingMiddleware` 的兜底消息被 journal 检测。
  - [#15 subagents.md](subagents.md)：子代理 token 经 `record_external_llm_usage_records` 回灌主 journal。
- **与 stream_bridge 的区别**：journal 是「**落盘**的事件流」（给历史/审计/token 核算）；stream_bridge（[#11](stream_bridge.md)）是「**实时推送**的事件流」（给在线前端）。worker 同时用两者。

---

## 9. 常见问题 / 排错

**Q: 事件丢了（store 里没有）？**
A: 检查 worker 有没有在 `finally` 里调 `await journal.flush()`。回调里达阈值才刷盘，没达阈值的残余事件靠 flush 兜底。没 flush 就丢。

**Q: token 数翻倍了？**
A: 不应该——有 run_id 去重（§6.3）。若仍翻倍，可能是 LangChain 对不同 run_id 触发了同一逻辑响应（罕见）。检查 `usage_metadata` 是否重复绑定。

**Q: 回调里 `await` 报错？**
A: 回调方法是同步的，不能 `await`（§6.1）。RunJournal 内部用 `loop.create_task` 调度异步刷盘。你若扩展回调，别在里头直接 await store。

**Q: `database is locked` 导致 flush 失败？**
A: 已处理——失败 batch 回插 buffer，下次 flush 重试（§6.2）。但若持续锁，说明写并发太高（多 worker 同写同库），考虑 postgres 或调大 `flush_threshold` 减少写频率。

**Q: 前端看不到进行中的 token 进度？**
A: 检查 worker 有没有注入 `progress_reporter`。没注入则 `_schedule_progress_flush` 直接 return（[第 541 行](../backend/packages/harness/deerflow/runtime/journal.py#L541)），无进度快照。

**Q: `had_llm_error_fallback` 是 True 但 run 状态显示 success？**
A: worker 应据 `had_llm_error_fallback` 把 RunRow 标 error（即使图正常结束）。journal 只负责检测（§5.4），状态判定在 worker（[#26](runs.md)）。

**Q: 首条 human 抽到了摘要？**
A: 不会——`on_chat_model_start` 抽取时跳过 `name="summary"` 的 HumanMessage（§5.3）。

---

## 小结

RunJournal 的精髓是**统一回调采集 + 同步↔异步桥接 + token 防双计**。记住五件事：

1. **统一采集**：继承 `BaseCallbackHandler`，把 chain/llm/tool 回调标准化成 RunEvent，攒 buffer 批量写 RunEventStore。
2. **同步→异步桥接**：同步回调里 `loop.create_task` 调度异步 `put_batch`，绝不阻塞回调；无循环时留 buffer 等 `flush`。
3. **防双计 + 防丢失**：token 按 langchain run_id 去重；失败 batch 回插 buffer 重试。
4. **按 caller + 模型分桶**：`_identify_caller` 按 tags 分 lead/subagent/middleware；`_record_model_usage` 按 `response_metadata.model_name` 分真计费模型桶。
5. **不实现 `on_llm_new_token`**：流式走 stream_bridge（#11），event store 只收完整消息。

上一篇：[#9 run_event_store.md](run_event_store.md)（运行事件存储——本模块的**存储侧**）· 下一篇：[#11 stream_bridge.md](stream_bridge.md)（流桥——SSE 实时推送，与本模块的「落盘事件流」互补：一个给历史/审计，一个给在线前端）。
