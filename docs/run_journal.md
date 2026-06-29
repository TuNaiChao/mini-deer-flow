# 10. run_journal.md — RunJournal（回调采集 + token 核算）

> 配套代码：[runtime/journal.py](../backend/packages/harness/deerflow/runtime/journal.py)
> 配套测试：[test/test_journal.py](../test/test_journal.py)
> 本文面向「刚接触回调机制 / token 计费 / 异步刷盘的小白」。每个名词第一次出现都会解释。

> **Phase 1 全维重审（2026-06-29）**：逐函数 diff `journal.py` vs 最新上游。M18 已 port 的
> **#3658**（按模型归桶：`on_llm_end` 取 `response_metadata.model_name` → `_record_model_usage`
> 分桶 `_tokens_by_model` → `get_completion_data` 返 `token_usage_by_model`）+ **#3697**
> （`on_chat_model_start` 抓首条 human 也过滤 `hide_from_ui`）经核对**与上游一致、无需补丁**。
> 本轮补 2 处 minor 对齐：① `record_external_llm_usage_records` 形参类型加 `None`
> （`model_name` 可为 None，落 `"unknown"` 桶）+ docstring 补 `model_name` 字段说明；②
> `_counted_external_source_ids.add` 顺序对齐上游（功能等价）。**defer**：上游把内联的
> `_message_text` 合并进了 `utils/messages.py::message_to_text`（mini 还有 memory /
> tool_output_budget 共 3 处内联等价，跨 M7/M13/M16 的重构、join 语义略异），归附加专项
> （同 Phase 0 defer 决定）。journal 侧的 #3658 token 归桶端到端数据流见
> [runs.md](runs.md) §6.1。

---

## 1. 一句话定位

**RunJournal 是一个 LangChain 回调处理器——它「旁听」agent 运行过程中的每个事件（LLM 调用、工具调用、链开始/结束），把事件攒起来批量写入 RunEventStore，同时累加 token 用量。**

它是 RunEventStore 的**写入侧**采集器（[run_event_store.md](run_event_store.md) 是**存储侧**）。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样：

- **事件散落各处，没法统一存**。LangGraph agent 跑起来，LLM 调用、工具调用、中间件状态变更发生在十几个地方。每个地方各自往 store 写 → 代码重复、格式不一、漏写。
- **token 双计**。LangChain 的回调机制有时会对同一个 LLM 响应触发多次 `on_llm_end`。直接累加 token，一次调用可能被算 2-3 次，账单爆炸。
- **同步回调阻塞事件循环**。`BaseCallbackHandler` 的方法是**同步**的（LangGraph 在事件循环里同步调用它们）。回调里直接 `await store.put_batch(...)` 会报错（同步函数不能 await）；开个阻塞写又会卡住整个 agent。
- **不知道 run 出错没有**。worker 跑完要知道「这次是成功还是 LLM 兜底了」，没有统一的 fallback 检测就得各自判断。
- **进度看不见**。长 run 跑了 5 分钟，前端想看「已经用了多少 token」，没有定期快照就只能等结束。

RunJournal 解决这些：**统一回调采集**、**token 按 run_id 去重**、**同步回调里调度异步刷盘**、**error fallback 检测**、**节流的进度快照**。

---

## 3. 核心概念（名词 + 类比）

### 3.1 回调（callback）/ BaseCallbackHandler

**回调** = 「事件发生时，框架自动调你的某个方法」。LangChain 的 `BaseCallbackHandler` 定义了一组钩子方法：`on_chain_start`、`on_llm_end`、`on_tool_end` 等。你继承它、实现这些方法，LangGraph 跑 agent 时就会在对应时机调它们。

类比：RunJournal 是一个「监工」，agent 干活时每个关键步骤都喊它一声（回调），它负责记录。

### 3.2 写入侧 / 存储侧

- **写入侧**（RunJournal）：产生事件、决定「记什么、怎么记、token 怎么算」，把记录塞进 buffer。
- **存储侧**（RunEventStore）：负责「记到哪」（memory/jsonl/db）、seq 单调、查询。

两者解耦：RunJournal 不关心存哪（只调 `store.put_batch`），RunEventStore 不关心事件语义（只存 dict）。换存储后端不影响采集逻辑。

### 3.3 token 用量 / usage_metadata

每次 LLM 调用，响应里带 `usage_metadata`：`{input_tokens, output_tokens, total_tokens}`——这次调用读了多少 token（输入）、写了多少（输出）。这是**计费和限流**的基础。RunJournal 累加这些，run 结束时写到 RunRow，供「这次 run 烧了多少 token」查询。

### 3.4 caller（调用方）分桶

一次 run 里，调用 LLM 的不只是主 agent（lead_agent）——子代理（subagent）、中间件（middleware，如标题生成）也会调 LLM。为了知道「token 都是哪些角色烧的」，RunJournal 按 caller 把 token 分桶：
- `lead_agent`：主 agent。
- `subagent:{name}`：子代理。
- `middleware:{name}`：中间件。

靠 **tags 注入**识别：子代理/中间件在调模型时给自己的回调打 tag，RunJournal 读 tag 分桶。

### 3.5 buffer + flush_threshold

回调是**高频**的（一个 run 可能几十上百条事件）。每条都立即写库（特别是 SQLite）会很慢。所以 RunJournal 攒一个 **buffer**，攒到 `flush_threshold`（默认 20）条才批量写（`put_batch`）——一次事务写一批，高效。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 同步回调 → 异步刷盘（红线 #8 核心）

`BaseCallbackHandler` 的方法是**同步**的。但 `store.put_batch` 是**异步**的（async）。回调里不能直接 `await`。解法：

1. 回调里把事件 append 到 buffer。
2. 达阈值时调 `_flush_sync`：
   - 检测**当前有没有事件循环在跑**（`asyncio.get_running_loop()`）。
   - 有 → `loop.create_task(self._flush_async(batch))`：调度一个异步任务去写，**回调立即返回**（不阻塞）。
   - 没有（无循环）→ 事件留 buffer，等 worker 的 `finally` 里调 async `flush()` 再写。
3. `_pending_flush_tasks` 记录在途任务——**已有 flush 在跑就跳过新的**，防多个 fire-and-forget 任务并发写同一个 SQLite 文件（会撞锁）。

这是「同步世界」与「异步世界」的桥接，关键是**绝不阻塞回调**。

### 4.2 失败 batch 回插（红线 #8）

`_flush_async` 里 `put_batch` 抛错（如 `database is locked`）时，**把整批事件回插 buffer 头部**（`self._buffer = batch + self._buffer`），下次 flush 重试。**事件不丢**——宁可重复尝试，不能丢记录。

测试 `test_failed_batch_returned_to_buffer` 锁住这个：第一次 put_batch 抛错 → batch 回 buffer → 第二次 flush 成功 → 事件全在 store。

### 4.3 token 按 run_id 去重（防双计）

LangChain 可能对**同一个 LLM 响应**触发多次 `on_llm_end`（不同的回调路径）。如果每次都累加 token，一次调用算多次。

解法：用 `_counted_llm_run_ids` 集合记录「已计过 token 的 langchain run_id」。同一 run_id 第二次来，跳过累加。**宁可少计（漏掉罕见的真重复），不可多计（账单错）**。

测试 `test_dedup_same_run_id`：同一 run_id 调两次 on_llm_end → total_tokens 不翻倍。

### 4.4 为什么在 on_chat_model_start 抽首条 human

「首条 human 消息」（run 的用户输入）是 RunRow 的关键字段（`first_human_message`）。在哪抽？
- `on_chain_start`？每个图节点都触发，太多太乱。
- `on_llm_end`？那时消息可能已被 checkpoint 裁剪。
- **`on_chat_model_start`**：只在真实 LLM 调用时触发，此处 prompt 消息是**完整结构化**的、**未被裁剪**。

所以在 `on_chat_model_start` 里，从 messages 反向找第一条 `HumanMessage`（跳过 `name="summary"` 的摘要注入消息）。只抽一次（`_first_human_msg` 未设时才抽）。

### 4.5 last_ai_message 只认 lead_agent

RunRow 的 `last_ai_message` 应代表「主 agent 给用户的最终回答」。子代理/中间件的 AI 消息、以及只有 tool_calls 的空 AI 消息，都不该覆盖它。规则：只当 `caller is None or caller == "lead_agent"` 且文本非空时更新。

测试 `test_subagent_ai_does_not_overwrite`：先 lead 设一条，subagent 的 AI 消息不覆盖。

### 4.6 进度节流（progress throttle）

`progress_reporter`（由 worker 注入的 async callable）定期把 token 快照写到 RunRow，让前端看到「进行中的 run 用了多少 token」。但 LLM 调用可能很密，不能每次都写（写库开销）。

节流：`progress_flush_interval`（默认 5 秒）内只写一次。interval 内的触发标 dirty、调度一个延迟任务到 interval 结束再写。**保证写库频率有上限**。

`progress_reporter` 是 Phase 8 worker 注入的（`run_manager.update_run_progress`），journal 模块只收 `Callable`，**无模块级循环依赖**——journal 不 import run_manager。

### 4.7 error fallback 检测

LLM 调用失败时，`LLMErrorHandlingMiddleware` 会生成一条「兜底 AI 消息」（带 `additional_kwargs.deerflow_error_fallback=True`）让 run 优雅继续。RunJournal 在 `on_llm_end` 检测这个标记，设 `had_llm_error_fallback=True` + 记消息。worker 据此判断「这次 run 其实是 error 兜底」，在 RunRow 标 error 状态。

### 4.8 不实现 on_llm_new_token

流式 token（`on_llm_new_token`）每个 token 触发一次——一次回复几百次回调，buffer 爆炸、写库爆炸。所以 RunJournal **不**实现它，只在 `on_llm_end` 收**完整消息**。流式展示走 stream_bridge（[stream_bridge.md](stream_bridge.md)），不走 event store。

---

## 5. 文件结构

```
runtime/
└── journal.py    # RunJournal(BaseCallbackHandler)：单文件，~600 行
```

依赖：[runtime/events/store](../backend/packages/harness/deerflow/runtime/events/store/)（RunEventStore，存储侧，硬依赖）、`langchain_core`（回调 + 消息）、`langgraph.types.Command`（on_tool_end 的 Command 输出）。**不**依赖 run_manager（progress_reporter 由 worker 注入，无循环）。

---

## 6. 关键接口 / 签名

### 构造

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

### LangChain 回调（框架调）

```python
on_chain_start / on_chain_end / on_chain_error
on_chat_model_start        # 抽首条 human（并发 llm.human.input 事件）
on_llm_end                 # 存 llm_response + token 累加去重 + error fallback 检测
on_llm_error
on_tool_start / on_tool_end
```

### worker 调的公开方法

```python
record_external_llm_usage_records(records)      # 子代理等外部来源 token（去重）
set_first_human_message(content)                # 手动设首条 human
record_middleware(tag, *, name, hook, action, changes)  # 中间件状态变更事件
async flush()                                   # 强制 flush 剩余 buffer（worker finally 调）
get_completion_data() -> dict                   # 累加的 token + 消息数据（写 RunRow 用）
had_llm_error_fallback  # property：是否发生过 error 兜底
llm_error_fallback_message  # property：兜底消息
```

---

## 7. 应用方法（可跑 demo）

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

### 7.2 worker 注入进度上报（Phase 8）

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

---

## 8. 与其它模块的关系（文字依赖图）

```
LangChain/LangGraph 回调机制
        │（on_llm_end / on_chat_model_start / on_tool_end / ...）
        ▼
   runtime/journal.RunJournal  ──put_batch──→  runtime/events/store（RunEventStore，存储侧）
        │                                            │
        │ token 累加（去重 + 分桶）                    │ 落盘（memory/jsonl/db）
        │ error fallback 检测                         │
        │ progress 节流快照                           │
        ▼                                            ▼
   get_completion_data() ──→ （未来）runs/worker 写 RunRow（run 完成时的 token/首末消息）
        │
   progress_reporter（worker 注入的 Callable）──→ （未来）RunManager.update_run_progress
```

- **被谁依赖**：未来的 runs/worker（构造它、注入 progress_reporter、调 flush + get_completion_data）。
- **依赖谁**：RunEventStore（存储侧，硬）、langchain_core、langgraph.types。**不**依赖 run_manager（progress_reporter 是注入的 Callable，无模块循环）。
- **与 stream_bridge 的区别**：journal 是「落盘的事件流」（给历史/审计/token 核算）；stream_bridge 是「实时推送的事件流」（给在线前端）。worker 会同时用两者。

---

## 9. 常见问题 / 排错

**Q: 事件丢了（store 里没有）？**
A: 检查 worker 有没有在 `finally` 里调 `await journal.flush()`。回调里达阈值才刷盘，没达阈值的残余事件靠 flush 兜底。没 flush 就丢。

**Q: token 数翻倍了？**
A: 不应该——有 run_id 去重。若仍翻倍，可能是 LangChain 对不同 run_id 触发了同一逻辑响应（罕见）。检查 `usage_metadata` 是否重复绑定。

**Q: 回调里 `await` 报错？**
A: 回调方法是同步的，不能 `await`。RunJournal 内部用 `loop.create_task` 调度异步刷盘。你若扩展回调，别在里头直接 await store。

**Q: `database is locked` 导致 flush 失败？**
A: 红线 #8 已处理——失败 batch 回插 buffer，下次 flush 重试。但若持续锁，说明写并发太高（多 worker 同写同库），考虑 postgres 或调大 flush_threshold 减少写频率。

**Q: 前端看不到进行中的 token 进度？**
A: 检查 worker 有没有注入 `progress_reporter`。没注入则 `_schedule_progress_flush` 直接 return，无进度快照。

**Q: `had_llm_error_fallback` 是 True 但 run 状态显示 success？**
A: worker 应据 `had_llm_error_fallback` 把 RunRow 标 error（即使图正常结束）。若 worker（Phase 8）还没接，这是预期——journal 只负责检测，状态判定在 worker。

**Q: 首条 human 抽到了摘要？**
A: 不会——`on_chat_model_start` 抽取时跳过 `name="summary"` 的 HumanMessage（那是摘要注入，不是用户原话）。

---

> 红线索引：#8（RunJournal sync→async flush 去重：`_pending_flush_tasks` 防并发写 + 失败 batch 回插）。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
