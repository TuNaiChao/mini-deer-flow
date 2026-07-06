# 15. subagents.md — 子代理（委派 / 单调度池 + 持久隔离事件循环 / 自定义子代理）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d · **2026-07-05 复审**（更面向小白 + 加「实现差异 vs 上游 deer-flow 源码」）

> **一句话定位**：子代理让主 agent（lead agent）把一个子任务**整个委派**给另一个独立 agent 去跑——
> 比如「去 bash 里跑一串命令」「去做一轮深度调研」。主 agent 只发一次 `task` 工具调用，
> 子代理在**后台**跑完，把结果摘要回灌。主 agent 的上下文不被冗长的中间步骤污染。

> **配套代码**：[subagents/](../backend/packages/harness/deerflow/subagents/)（8 个文件 ~1562 行）+ [tools/builtins/task_tool.py](../backend/packages/harness/deerflow/tools/builtins/task_tool.py)（唯一调用方，408 行）+ [config/subagents_config.py](../backend/packages/harness/deerflow/config/subagents_config.py) + [contracts/subagent_status_contract.json](../contracts/subagent_status_contract.json)
> **配套测试**：[test/test_subagents.py](../test/test_subagents.py)（86 个 hermetic 测试，agent 构造经 monkeypatch 注入 fake，不碰真模型）
> **参考**：deerflow-book [08-subagent-overview.md](../deerflow-book/chapters/08-subagent-overview.md) · [09-subagent-executor.md](../deerflow-book/chapters/09-subagent-executor.md)（**仅借「lead↔sub 编排 + 状态机 + 线程池调度」的概念叙事框架**，不作差异基线）；**实现差异一律对照上游源码** `deer-flow/backend/packages/harness/deerflow/subagents/`，见本文 §9
> 本文面向「刚接触多 agent 协作 / 事件循环 / 后台任务的小白」。读完 [sandbox.md](sandbox.md)（懂了工具与沙箱）再看本篇最省事——子代理就是一个**自带工具集与提示词的小 agent**，跑在主 agent 给它的沙箱里。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么需要子代理？「上下文爆炸 / 职责不清」两个痛点它怎么解？主 agent 只收到一句摘要、中间步骤去哪了？
2. 为什么是「单 scheduler pool + 持久隔离事件循环」**而非每次 `asyncio.run` 起新循环**？（复用共享 async client：httpx/MCP 绑在创建它的事件循环上，短命循环会让 client 下次用就坏）
3. `MAX_CONCURRENT_SUBAGENTS=3` 由哪**两道关**保证？为什么执行器不再自建第二线程池？
4. 子代理图为什么 `checkpointer=False`（一次性、从不 resume）？这跟主 run 的 checkpointer 什么关系？
5. 取消为什么是**协作式**的（在 `astream` 迭代边界查 `cancel_event`）？`Future.cancel()` 为什么杀不掉已在跑的子代理线程？代价是什么？
6. 6 个状态（pending/running → completed/failed/cancelled/timed_out）+ 第 5 个 `polling_timed_out` 怎么来的？`try_set_terminal` 为什么要在锁内**原子**设一次？`status_contract` 解决了什么前后端对齐问题？
7. 子代理的 token 怎么**按 caller + 模型**归桶回灌进父 run？`SubagentTokenCollector` 按 `run_id` 去重防什么？流式 AI 消息去重为什么从 O(n²) 优化成 O(1)？
8. 内置/自定义/per-agent 覆盖**三层合并**优先级是什么？全局 `timeout_seconds`/`max_turns` 为什么只覆盖内置、不覆盖自定义？`bash` 子代理为什么有时看不到？

---

## §1 为什么需要子代理（痛点）

主 agent（lead agent）一把梭有两个常见问题：

1. **上下文爆炸**：跑一串 `npm install && npm test` 会产出几千行日志。全塞进主对话，后续每一轮都带着这些日志，token 很快爆。
2. **职责不清**：调研、写代码、跑命令混在一个 agent 里，提示词难写、容易跑偏。

子代理的解法：主 agent 调一次 `task` 工具说「帮我调研 X」或「帮我跑这些命令」，一个**独立的子 agent** 用自己的上下文把活干完，只回**一句摘要**。主 agent 的对话保持干净。

**类比**：你是项目经理（lead agent），遇到一个独立子活，你不自己干，而是**派一个专员**（subagent）去干，专员干完回来给你一份**一页纸汇报**。专员的草稿纸（中间步骤）你不看。

---

## §2 零基础名词（先认这些词）

> 本篇假设你已读过 [#13 sandbox.md](sandbox.md) §2.0 的计算机基础（进程/shell/挂载）+ §2 的「工具 / 沙箱 / 虚拟路径」。子代理跑在「异步 + 多线程」环境里，这几个并发概念是理解它的前提。

### 2.0 最基础（异步 / 并发，不熟先看这）

- **同步 vs 异步**：**同步**=「一件事做完才做下一件」（`result = do()` 停在这行等 do 返回）；**异步**=「这件事启动后先不傻等，去做别的，等它好了再回来收结果」（Python 用 `async/await`）。异步适合「很多等待」（等网络、等 IO）——等待时 CPU 能干别的。agent 大量等 LLM 返回，故用异步。
- **事件循环（event loop）**：异步程序的「调度中枢」——维护一个待办队列，不停「挑一个能跑的跑、遇到在等的就跳过去干别的」。一个进程里可以有多个**独立**的事件循环（各自一个队列）。子代理跑在一个**独立的持久事件循环**上，和主 run 的循环分开（§5.2）。
- **协程（coroutine）/ `async def`**：用 `async def` 定义的函数——它不「立刻执行完返回结果」，而是返回一个「协程对象」，要交给事件循环去跑。`await xxx` =「等这个协程出结果再继续」。
- **线程（thread）vs 协程**：**线程**是操作系统调度的执行单元（一个进程里多条线程真并行跑）；**协程**是程序自己（事件循环）在一条线程内来回切换调度（看起来并发但不真并行）。子代理用「线程池 + 持久事件循环」组合：线程池提供并发槽，每条线程内跑协程。
- **上下文窗口 / token**：LLM 一次能「看到」的文本长度上限，用 token 计量（约 ¾ 个英文单词）。对话历史 + 工具输出都占 token，超了就爆（模型截断或报错）。**子代理的核心动机就是不污染主 agent 的上下文窗口**——把冗长中间步骤关在子代理自己的上下文里，主 agent 只收一句摘要（§1）。

### 2.1 本模块名词

- **lead agent（主代理）**：用户直接对话的那个 agent，拥有完整中间件栈、澄清机制、技能系统。它是「指挥官」。
- **subagent（子代理）**：lead agent 通过 `task` 工具临时拉起的独立 agent，跑完就结束。它是「专员」。
- **委派（delegate）**：lead agent 把一个子任务整包交给子代理的行为。子代理只收到一条 `HumanMessage`（任务描述），**看不到** lead agent 的对话历史——这是刻意设计（§6 详谈）。
- **事件循环（event loop）**：`asyncio` 跑异步代码的「调度器」。一个进程里可以有好几个独立的事件循环。本篇的关键是：异步 HTTP client（httpx）/ MCP client **绑在创建它的那个事件循环上**，循环关了 client 就废了。
- **线程池（ThreadPoolExecutor）**：一组常驻工作线程，提交任务给它们跑。本篇 `_scheduler_pool` 是 3 个 worker 的线程池。
- **`task` 工具**：lead agent 创建子代理的**唯一入口**（[task_tool.py:182](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L182)）。lead agent 的 LLM 决定委派时调它。
- **虚拟路径 `/mnt/user-data`**：子代理和 lead agent **共享同一个沙箱**（同一个 `/mnt/user-data/workspace`），文件系统状态互通——子代理能读写 lead agent 的文件。
- **ContextVar（上下文变量）**：Python 的「按线程/协程隔离的全局变量」——主线程设了 `user_id`，子代理跑在另一条线程上也能读到，但不会互相串。子代理跨隔离 daemon 线程跑时，用 `copy_context()` 把这些变量带过去（§5.2），否则 user_id 会丢。
- **`BaseCallbackHandler`（回调处理器）**：LangChain 的「事件钩子」基类——模型/链跑的时候，它会在固定节点（如 `on_llm_end`）被叫起来。`SubagentTokenCollector` 就是一个回调处理器，挂子代理执行上，每次模型调用完收一次 token 用量（§5.5）。
- **`asyncio.run_coroutine_threadsafe`**：从一条线程把协程「丢」到另一条线程的事件循环上跑、并拿到 future 的方法。子代理把协程提交到隔离循环靠的就是它（§5.2）。
- **daemon 线程**：标记为「后台」的线程——主程序退出时不用等它、直接跟着结束。持久隔离事件循环就跑在一条 daemon 线程上（§5.2）。

---

## §3 整体结构（文件 + 数据流）

### 文件结构

```
subagents/
├── __init__.py              (39 行)  包导出面（SubagentConfig/Executor/Result/Status + registry + 后台任务函数）
├── config.py                (71 行)  SubagentConfig dataclass + resolve_subagent_model_name
├── registry.py             (168 行)  内置 + 自定义 + per-agent 三层合并 + 按沙箱安全隐藏 bash
├── executor.py             (988 行)  执行引擎（状态机/单池+隔离循环/协作取消/超时/后台任务）
├── status_contract.py       (87 行)  5 终态契约（结果文本→状态 + 状态→additional_kwargs）
├── token_collector.py       (72 行)  SubagentTokenCollector（收 LLM usage，按 run_id 去重）
└── builtins/
    ├── __init__.py          (16 行)  BUILTIN_SUBAGENTS 表（general-purpose + bash）
    ├── general_purpose.py   (66 行)  通用子代理（继承全部工具，max_turns=150）
    └── bash_agent.py        (55 行)  bash 专员（5 沙箱工具，max_turns=60）

tools/builtins/task_tool.py (408 行)  task 工具——唯一调用方（后台执行 + 5s 轮询 + SSE + token 回灌）
config/subagents_config.py         SubagentsAppConfig（enabled/max_concurrent/全局 timeout·max_turns + custom_agents + agents 覆盖）
contracts/subagent_status_contract.json  5 状态契约的后端↔前端共享 fixture（13 个 case）
```

### 一次子代理执行的内部数据流

```
lead agent 的 LLM 决定委派子任务
  │  调 task 工具：task(description, prompt, subagent_type)
  ▼
task_tool（唯一调用方）
  │  ① get_subagent_config(subagent_type)        ← registry 三层合并（内置→custom→per-agent 覆盖）
  │  ② get_available_tools(subagent_enabled=False) ← 子代理工具（排除 task 防递归）
  │  ③ 构造 SubagentExecutor(config, tools, sandbox_state, thread_data, thread_id, parent_model, user_id)
  │  ④ executor.execute_async(prompt, task_id=tool_call_id)  ← 提交到 _scheduler_pool
  │  ⑤ 轮询 get_background_task_result(task_id)，每 5s 一次，推 SSE 事件
  ▼
_scheduler_pool（唯一 3-worker ThreadPoolExecutor）
  │  worker 线程跑 run_task：
  │    把协程提交到持久隔离循环 asyncio.run_coroutine_threadsafe(_aexecute, _isolated_subagent_loop)
  │    future.result(timeout=timeout_seconds) 等——超时由这个 worker 当「看门人」
  ▼
_aexecute（跑在持久隔离循环上的协程主体）
  │  ① _build_initial_state(task) → 初始 state（system_prompt+技能合成单条 SystemMessage + HumanMessage）
  │     + 工具（skills 收紧 + tool_search 延迟装配，缺包降级）
  │  ② _create_agent → create_agent(…, checkpointer=False)   ← 一次性，不继承父 checkpointer
  │  ③ 挂 SubagentTokenCollector 回调（收 usage）+ build_tracing_callbacks（图根 tracing）
  │  ④ async for chunk in agent.astream(state, stream_mode="values"):
  │        • 迭代顶部查 cancel_event → 协作取消（返回 CANCELLED）
  │        • 抽 AIMessage，按 id 用 seen_message_ids 集合 O(1) 去重 → 入 result.ai_messages
  │  ⑤ result.try_set_terminal(COMPLETED | FAILED | TIMED_OUT | CANCELLED, token_usage_records=…)
  ▼
task_tool 收 result
  │  • 抽最终 AIMessage 文本作为结果摘要
  │  • _report_subagent_usage → journal.record_external_llm_usage_records(token_records)
  │    → 父 RunJournal 按 caller(subagent:<name>) + 模型归桶（token_usage_by_model）
  ▼
lead agent 收到带结果的 ToolMessage，继续对话
```

---

## §4 核心概念

### 4.1 SubagentConfig（一个子代理长什么样）

一个 dataclass（[config.py:15](../backend/packages/harness/deerflow/subagents/config.py#L15)），描述子代理的「人设」：

| 字段 | 含义 |
|------|------|
| `name` | 唯一标识（如 `general-purpose` / `bash` / 自定义名） |
| `description` | **何时**该委派给它（写进 `task` 工具描述，供 lead agent 的 LLM 选择） |
| `system_prompt` | 引导它行为的系统提示词 |
| `tools` | 工具**白名单**。`None` = 继承 lead agent 全部工具；给列表 = 只留这些 |
| `disallowed_tools` | 工具**黑名单**（默认含 `task`，防子代理再嵌套委派→无限递归） |
| `skills` | 技能白名单（`None`=全部、`[]`=无、列表=只这些） |
| `model` | `'inherit'`（用 lead agent 的模型）或显式模型名 |
| `max_turns` / `timeout_seconds` | 最大轮次 / 执行时间上限 |

模型名解析三优先级（[config.py:51](../backend/packages/harness/deerflow/subagents/config.py#L51)）：子代理显式 model（非 `inherit`） > 父模型 > `app_config.models[0]`。

### 4.2 内置子代理（builtins）

mini 开箱两个内置子代理（[builtins/__init__.py:13](../backend/packages/harness/deerflow/subagents/builtins/__init__.py#L13)）：

- **`general-purpose`**（[general_purpose.py:10](../backend/packages/harness/deerflow/subagents/builtins/general_purpose.py#L10)）：通用多步子代理。`tools=None` 继承**除** `task`/`ask_clarification`/`present_files` 外的全部工具，`max_turns=150`，适合「既要探索又要动手」的深任务。
- **`bash`**（[bash_agent.py:10](../backend/packages/harness/deerflow/subagents/builtins/bash_agent.py#L10)）：命令执行专员。只挂沙箱 5 工具（bash/ls/read_file/write_file/str_replace），`max_turns=60`。当 host bash 未被放行时（`is_host_bash_allowed()=False`，Local 沙箱默认），registry 会把它从可见列表**隐藏**——因为它不安全（[registry.py:153](../backend/packages/harness/deerflow/subagents/registry.py#L153)）。

### 4.3 自定义子代理（config.yaml `subagents.custom_agents`）

用户可在 config.yaml 里声明任意自定义子代理类型（[subagents_config.py:51](../backend/packages/harness/deerflow/config/subagents_config.py#L51)）：

```yaml
subagents:
  custom_agents:
    researcher:
      description: 深度调研某个主题并产出报告
      system_prompt: 你是一个调研专员……
      tools: null              # null = 继承全部工具
      max_turns: 40
      timeout_seconds: 600
      model: inherit           # 或显式模型名
```

然后 lead agent 的 `task` 工具调 `subagent_type: researcher` 就能委派给它。

### 4.4 per-agent 覆盖（config.yaml `subagents.agents`）

对**任意**子代理（内置或自定义）按名压一层覆盖（[subagents_config.py:27](../backend/packages/harness/deerflow/config/subagents_config.py#L27)）：

```yaml
subagents:
  agents:
    bash:
      timeout_seconds: 120     # 把 bash 子代理超时改 120s
      max_turns: 30
```

### 4.5 三层合并优先级

`get_subagent_config`（[registry.py:62](../backend/packages/harness/deerflow/subagents/registry.py#L62)）按下面顺序找到基线 config，再压 per-agent 覆盖层：

```
built-in（内置默认） → custom（自定义自带值） → per-agent override（agents.<name>）
```

注意（[registry.py:94-110](../backend/packages/harness/deerflow/subagents/registry.py#L94)）：**全局** `subagents.timeout_seconds` / `max_turns` 只覆盖**内置**子代理，**不**覆盖自定义子代理（自定义子代理在 `custom_agents` 段自带默认值）。`model` / `skills` 只有 per-agent 覆盖、无全局默认。

### 4.6 与上游 deer-flow 的关系（先看这句）

> **结论先放这**：mini 的子代理是上游 `deer-flow/.../subagents/` 的**忠实移植**——下面 §5 讲的单 `_scheduler_pool`(3) + 持久隔离事件循环、6 状态机（含 `CANCELLED`）、`SubagentTokenCollector` 按 caller+模型归桶、`system_prompt` 合成单条 SystemMessage、`build_subagent_runtime_middlewares`，**上游源码里全有**（`registry.py` / `status_contract.py` 与 mini **0 行差异**）。真正的实现差异很小（mini 多一个小 helper + task_tool 细节），详见 §9。

---

## §5 代码走读

### 5.1 SubagentStatus 状态机 + SubagentResult

`SubagentStatus`（[executor.py:68](../backend/packages/harness/deerflow/subagents/executor.py#L68)）6 值：`PENDING` / `RUNNING`（非终态）+ `COMPLETED` / `FAILED` / `CANCELLED` / `TIMED_OUT`（终态，`is_terminal` 判定）。

```
        ┌─────────┐
        │ PENDING │  execute_async 已提交、worker 未开始
        └────┬────┘
             │ worker 进入 _aexecute
             ▼
        ┌─────────┐   cancel_event 置位（迭代边界查到）   ┌───────────┐
        │ RUNNING │ ──────────────────────────────────▶ │ CANCELLED │
        └────┬────┘                                      └───────────┘
             │ ├─ 正常抽到最终 AIMessage                     ▲
             │ ├─ _aexecute 抛异常                          │ timeout_seconds 到
             │ └─ timeout_seconds 到                        │
             ▼                                             │
    ┌──────────────┐                              ┌───────────┐
    │  COMPLETED   │                              │ TIMED_OUT │
    └──────────────┘                              └───────────┘
             │ 异常
             ▼
        ┌─────────┐
        │ FAILED  │
        └─────────┘
```

`SubagentResult`（[executor.py:88](../backend/packages/harness/deerflow/subagents/executor.py#L88)）承载完整记录：`task_id` / `trace_id` / `status` / `result` / `error` / `ai_messages` / `token_usage_records` / `cancel_event`（协作取消位）/ `_state_lock`。核心方法 `try_set_terminal`（[executor.py:121](../backend/packages/harness/deerflow/subagents/executor.py#L121)）在 `_state_lock` 下**恰好一次**地设终态——后台超时/取消与执行 worker 会在同一个 result holder 上竞争，第一个终态转换赢，迟到的不再改状态：

```python
def try_set_terminal(self, status, *, result=None, error=None, ...):
    if not status.is_terminal: raise ValueError(...)
    with self._state_lock:
        if self.status.is_terminal: return False     # 已终态，拒绝再改
        # ... 设 result/error/ai_messages/token_usage_records ...
        self.status = status
        return True
```

### 5.2 单 scheduler pool + 持久隔离事件循环

这是本模块最关键、也最容易被记错的设计。**唯一线程池**（[executor.py:161](../backend/packages/harness/deerflow/subagents/executor.py#L161)）：

```python
_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")
```

**持久隔离事件循环**（[executor.py:165](../backend/packages/harness/deerflow/subagents/executor.py#L165)）：一个 **daemon 线程上的常驻 `asyncio` 事件循环**。首次用时惰性起（`_get_isolated_subagent_loop`，[executor.py:221](../backend/packages/harness/deerflow/subagents/executor.py#L221)），进程退出经 `atexit` 注册的 `_shutdown_isolated_subagent_loop` 优雅关闭（[executor.py:184](../backend/packages/harness/deerflow/subagents/executor.py#L184)），防循环泄漏。

为什么这样（[executor.py:782](../backend/packages/harness/deerflow/subagents/executor.py#L782)）？——**复用共享 async client**。httpx / MCP 等异步客户端**绑在创建它的那个事件循环**上。如果每次执行子代理都 `asyncio.run`（起一个新循环 → 跑 → 关循环），client 就被绑到一个随即关闭的短命循环上，下次用就坏了。复用一个长命循环 = 复用 client。

`execute_async`（[executor.py:857](../backend/packages/harness/deerflow/subagents/executor.py#L857)）的 `run_task` 把协程提交到隔离循环，worker 线程当「看门人」等 `future.result(timeout=...)`：

```python
def run_task():
    # ... 置 RUNNING ...
    execution_future = _submit_to_isolated_loop_in_context(   # 提交到持久隔离循环
        parent_context, lambda: self._aexecute(task, result_holder))
    try:
        execution_future.result(timeout=self.config.timeout_seconds)   # 看门人超时
    except FuturesTimeoutError:
        result_holder.cancel_event.set()
        result_holder.try_set_terminal(TIMED_OUT, error=...)
        execution_future.cancel()
```

`_submit_to_isolated_loop_in_context`（[executor.py:252](../backend/packages/harness/deerflow/subagents/executor.py#L252)）用 `copy_context()` 保留 ContextVar（user_id 等）再 `run_coroutine_threadsafe`，让 user_id 跨隔离 daemon 线程不丢。

> **测试锁定**：[test_subagents.py](../test/test_subagents.py) 断言 `_scheduler_pool` 是 3-worker 的 ThreadPoolExecutor，且**不存在** `_execution_pool`。

### 5.3 协作取消（在 astream 迭代边界）

子代理线程**不能被 `Future.cancel()` 强杀**——协程一旦在跑，杀不掉。所以取消是**协作式**的（[executor.py:923](../backend/packages/harness/deerflow/subagents/executor.py#L923)）：

- `request_cancel_background_task(task_id)` 在结果的 `cancel_event` 上置位。
- `_aexecute` 在 `agent.astream()` 的**每次迭代顶部**检查 `cancel_event`（[executor.py:653](../backend/packages/harness/deerflow/subagents/executor.py#L653)），置位了就 `try_set_terminal(CANCELLED)` 返回。

代价：**单个迭代内的长工具调用不会被中断**——要等下一个 chunk 才能停。这是有意权衡：强杀会让子代理的文件 / 状态半途而废，协作取消保证停在一个干净的边界。

### 5.4 5 终态契约（后端↔前端单一真相源）

子代理执行有 5 个**终态值**供前端渲染子任务卡片（[status_contract.py:33](../backend/packages/harness/deerflow/subagents/status_contract.py#L33)）：`completed` / `failed` / `cancelled` / `timed_out` / `polling_timed_out`。

旧前端靠**字符串前缀匹配** `task` 工具结果文本推状态——后端改个措辞前端就坏（源码注释标 #3146）。新契约把状态塞进 `ToolMessage.additional_kwargs`：

- `subagent_status`：5 值之一。
- `subagent_error`（可选）：可读错误文本。

`extract_subagent_status`（[status_contract.py:55](../backend/packages/harness/deerflow/subagents/status_contract.py#L55)）的**前缀表按「最具体在前」排序**（[status_contract.py:45](../backend/packages/harness/deerflow/subagents/status_contract.py#L45)），因为部分前缀互为子串（`"Task timed out"` vs `"Task polling timed out"`）。共享 fixture [contracts/subagent_status_contract.json](../contracts/subagent_status_contract.json) 是单一真相源——13 个 case，两侧测试都加载它并断言。

### 5.5 token 回灌（子代理开销算进父 run，按 caller + 模型归桶）

子代理的 LLM 调用 token 不能丢。`SubagentTokenCollector`（[token_collector.py:17](../backend/packages/harness/deerflow/subagents/token_collector.py#L17)）是个 `BaseCallbackHandler`，挂子代理执行上，每次 `on_llm_end` 收 usage、按 `run_id` 去重（防流式双计）、还从 `response_metadata` 取**真正产生这次响应的模型名**写进记录（源码注释标 #3658，[token_collector.py:51](../backend/packages/harness/deerflow/subagents/token_collector.py#L51)）。

执行完后，`task_tool` 的 `_report_subagent_usage`（[task_tool.py:145](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L145)）经 `journal.record_external_llm_usage_records`（[journal.py:444](../backend/packages/harness/deerflow/runtime/journal.py#L444)）把记录回灌父 RunJournal：

- **按 caller 归桶**：`caller` 标 `subagent:<name>`，journal 把它累加进 `_subagent_tokens`（[journal.py:482](../backend/packages/harness/deerflow/runtime/journal.py#L482)）。
- **按模型归桶**：record 带 `model_name` 时，`_record_model_usage`（[journal.py:421](../backend/packages/harness/deerflow/runtime/journal.py#L421)）累加进 `_tokens_by_model`，最终输出 `token_usage_by_model`（[journal.py:601](../backend/packages/harness/deerflow/runtime/journal.py#L601)）并持久化进 DB（[persistence/run/sql.py:316](../backend/packages/harness/deerflow/persistence/run/sql.py#L316)）。

于是父 run 的 token 核算**含**子代理开销，且按真实计费模型分桶。详见 [#10 run_journal.md](run_journal.md) / [#7 persistence.md](persistence.md)。

> 每个 task 只报一次（`usage_reported` 守卫，[task_tool.py:147](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L147)）。父 worker 被取消时，task_tool 用 `asyncio.shield` 等子代理到终态，好把最终 token 快照报给父 journal（[task_tool.py:390](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L390)）。

### 5.6 流式 AI 消息去重（O(n²)→O(n)，源码注释标 #3687）

`_aexecute` 用 `stream_mode="values"` 流式跑子代理图——LangGraph 每个 super-step 都把**完整累积态**重发一次，所以同一条尾部 AI 消息会在**每个 chunk** 被重新看到。要把这些 AI 消息收进 `result.ai_messages`（供 SSE 实时推送 / 调试），就得去重。

解法（[executor.py:579](../backend/packages/harness/deerflow/subagents/executor.py#L579)）：进流式循环**前**用集合推导预建 `seen_message_ids`（也兜住 `result_holder` 自带的旧消息），循环内 `message_id in seen_message_ids` O(1) 查、append 后 `seen_message_ids.add(message_id)` 维护。无 `id` 的极少路径仍 fallback 到整 dict 比较（[executor.py:683](../backend/packages/harness/deerflow/subagents/executor.py#L683)）。语义不变（去重结果一致），只是把平方级压成线性——深研究子代理能跑到 `max_turns=150`，平方级开销显著。

### 5.7 task_tool：唯一调用方（后台执行 + 5s 轮询 + SSE）

`task_tool`（[task_tool.py:182](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L182)）是 lead agent 创建子代理的唯一入口：

1. `get_subagent_config` 三层合并 + `get_available_tools(subagent_enabled=False)`（子代理不能再有 task，防递归，[task_tool.py:281](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L281)）。
2. 从 runtime 抽父上下文（`sandbox_state` / `thread_data` / `thread_id` / `parent_model` / `trace_id` / `user_id`）透传给 executor。
3. `executor.execute_async(prompt, task_id=tool_call_id)`——用 `tool_call_id` 当 task_id，便于在 lead agent 工具调用与子代理间建立追踪关联。
4. **5s 轮询**（[task_tool.py:319](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L319)）：每轮查 `get_background_task_result`，检测到新 AI 消息就推 `task_running` SSE 事件，到终态推 `task_completed`/`task_failed`/`task_cancelled`/`task_timed_out` 并 `cleanup_background_task`。轮询超时兜底 `max_poll_count = (timeout_seconds + 60) // 5`（防后台超时机制失效时无限等待）。

### 5.8 后台任务清理（只清终态）

`cleanup_background_task`（[executor.py:962](../backend/packages/harness/deerflow/subagents/executor.py#L962)）只移除**终态**任务，避免与后台执行器仍在更新该条目的竞态。取消时若后台还没到终态，task_tool 用 `_schedule_deferred_subagent_cleanup`（[task_tool.py:103](../backend/packages/harness/deerflow/tools/builtins/task_tool.py#L103)）延迟轮询清理，防已取消任务堆积泄漏内存。

---

## §6 设计动机分析（为什么这么设计 / 作用 / 好处）

### 6.0 核心设计动机（先看这五个「为什么」）

**① 为什么需要子代理（委派），不就让 lead agent 一把梭？**
- **作用**：lead agent 把一个子任务整个委派给独立子 agent 跑，只收回一句摘要。
- **好处（双）**：① **不爆主上下文**——子任务冗长的中间步骤（几千行命令日志）关在子代理自己的上下文里，主对话保持干净（省 token、保推理质量）；② **职责分离**——调研/写代码/跑命令各用专属提示词的子代理，比一个 agent 什么都干更聚焦、更不容易跑偏。
- **不这么设计会怎样**：lead agent 自己跑 `npm install && npm test` → 几千行日志全进主对话 → 后续每轮都带着这些 token，很快爆窗口、推理也被干扰。

**② 为什么单 scheduler pool + 持久隔离事件循环，而不是每次 `asyncio.run` 起新循环？**
- **作用**：唯一 3-worker 线程池提交任务；协程跑在一条常驻 daemon 线程的持久事件循环上。
- **好处**：**复用共享 async client**——httpx/MCP 等异步 client 绑在创建它的事件循环上，复用长命循环 = 复用 client。
- **不这么设计会怎样**：每次执行都 `asyncio.run`（起循环→跑→关循环）→ client 绑到随即关闭的短命循环，下次用就坏。

**③ 为什么协作取消（在 astream 迭代边界查 `cancel_event`），不直接强杀线程？**
- **作用**：取消时置位 `cancel_event`，子代理在每次迭代顶部检查、置位就停下；不强制中断线程。
- **好处**：**停在干净边界**——协程一旦在跑，`Future.cancel()` 杀不掉；强杀会让文件/状态半途而废。协作取消保证停在一个完整步骤后、状态一致。
- **代价**：单个迭代内的长工具调用不会被中断，要等下一个 chunk——这是有意权衡。

**④ 为什么子代理图 `checkpointer=False`（一次性，从不 resume）？**
- **作用**：子代理图不继承主 run 的 checkpointer，也不自建。
- **好处**：**不污染主状态**——主 run 的对话状态由 lead agent 的 checkpointer 管；子代理跑完即弃，只回结果字符串，不把中间状态写进主库。
- **不这么设计会怎样**：子代理继承主 checkpointer → 中间节点状态写进主 run 的 checkpoint，污染主对话历史。

**⑤ 为什么上下文隔离（子代理只收一条 HumanMessage，看不到 lead 历史）？**
- **作用**：子代理只拿到任务描述这一条消息，看不到 lead agent 的对话历史 / 其他子代理结果。
- **好处**：**专注提升推理质量**——只面对单一任务的 agent，比被无关上下文淹没的 agent 推理更准。
- **代价**：lead agent 的 prompt 必须**自包含**（不能写「继续之前的搜索」，要把背景/约束/输出格式打包进这一条）。

---

| 权衡 | 选择 | 理由 |
|------|------|------|
| **单池 + 持久隔离循环** | 唯一 `_scheduler_pool` + 长命 `_isolated_subagent_loop` | 复用共享 async client（绑循环）；每次起新循环会让 client 绑到短命循环、下次用就坏 |
| **并发上限两道关** | `SubagentLimitMiddleware`（[#24 middlewares.md](middlewares.md)）截断多余 task 调用 + `_scheduler_pool` 3 worker | 中间件在模型响应后截断；线程池本身只有 3 槽兜底。执行器不自建第二池 |
| **子代理图 `checkpointer=False`** | 一次性，从不 resume | 不继承主 run 的 checkpointer（会污染主状态）也不自建；主 run 状态由 lead agent 自己的 checkpointer 管，子代理只回结果字符串 |
| **协作取消（非强杀）** | `cancel_event` 在 astream 迭代边界查 | `Future.cancel()` 杀不掉已在跑的协程；强杀会让文件/状态半途而废，协作取消停在干净边界 |
| **6 状态 + 第 5 契约值** | 多 `CANCELLED` + 契约 `polling_timed_out` | 区分用户取消 / 执行超时 / 轮询兜底超时；`status_contract` 替代脆弱的字符串前缀匹配 |
| **`try_set_terminal` 锁内原子** | `_state_lock` 下恰好一次设终态 | 后台超时/取消与执行 worker 竞争同一 holder，第一个赢、迟到的不改 |
| **token 按 caller + 模型归桶** | collector 收 model_name → journal 双维归桶 | 父 run 核算含子代理开销，且按真实计费模型分桶（不只是按 caller） |
| **流式 AI 消息 O(1) 去重** | 预建 `seen_message_ids` 集合 | `stream_mode="values"` 每 chunk 重发完整态，平方级扫描在 max_turns=150 时显著 |
| **system_prompt 合成单条 SystemMessage** | 放进初始 state，不传 `create_agent(system_prompt=)` | 有些 LLM API 拒绝多条 SystemMessage（"System message must be at the beginning"） |
| **上下文隔离（子代理看不到 lead 历史）** | 子代理只收一条 HumanMessage | 专注单一任务的 agent 推理质量 > 被无关上下文淹没；prompt 须自包含 |
| **共享沙箱 + 隔离上下文** | sandbox_state 透传，messages 独立 | 文件系统状态互通（协作），对话历史隔离（不污染） |

---

## §7 配置用法

### 配置（`config.yaml`）

```yaml
subagents:
  enabled: true                # 主开关（task 工具据此挂载）
  max_concurrent: 3            # 并发上限（与 SubagentLimitMiddleware 共同保证）
  timeout_seconds: 1800        # 内置子代理默认超时（30 分钟；不覆盖自定义）
  max_turns: null              # 可选全局 max_turns 覆盖（null = 保持内置默认）

  # 自定义子代理类型
  custom_agents:
    researcher:
      description: 深度调研某个主题并产出报告
      system_prompt: 你是一个调研专员……
      tools: null
      max_turns: 40
      timeout_seconds: 600
      model: inherit

  # 对任意子代理（内置或自定义）按名覆盖
  agents:
    bash:
      timeout_seconds: 120
```

### lead agent 如何委派

lead agent 的 `task` 工具（`subagent_type` 可选内置名或自定义名）→ `SubagentExecutor.execute_async` 后台跑 → 5s 轮询 → SSE 事件（`task_started`/`task_running`/`task_completed`/`task_failed`/`task_cancelled`/`task_timed_out`）→ 结果摘要回灌。真实 agent 构造依赖 [#25 agents.md](agents.md) 的 `build_subagent_runtime_middlewares`、[#22 tools.md](tools.md) 的 `tool_search`、[#19 skills.md](skills.md) 的 skills——这些未落地时优雅降级（fallback 中间件 / 无延迟装配 / 无技能），落地后自动切真实实现，不改执行器。

### 跑测试

```bash
cd backend && make test    # 含 test/test_subagents.py（86 个 hermetic 测试，含 #3687 去重回归）
```

测试约定（[test_subagents.py](../test/test_subagents.py)）：agent 构造经 monkeypatch 注入 fake（`_FakeAgent` 带 `.astream` 产预设 chunk），不碰真模型；隔离循环复用、后台任务生命周期、协作取消、超时、降级全 mock；后台任务全局存储每测试 autouse 清理，防跨测试污染；status_contract 加载 [contracts/subagent_status_contract.json](../contracts/subagent_status_contract.json) 13 个 case 全断言。

---

## §8 与其它模块的关系

```
config/subagents_config (custom_agents / agents 覆盖 / 全局 timeout/max_turns / enabled / max_concurrent)
  │
sandbox/security (#13：is_host_bash_allowed → get_available_subagent_names 隐藏 bash)
  │
subagents
  ├── config.SubagentConfig + resolve_subagent_model_name
  ├── registry (built-in + custom + override 三层合并)
  ├── status_contract (5 终态契约 ← contracts/subagent_status_contract.json)
  ├── token_collector (收 usage + model_name → 回灌父 RunJournal)
  ├── builtins (general-purpose / bash)
  └── executor
        ├── 单 _scheduler_pool(3) + 持久 _isolated_subagent_loop
        ├── _create_agent → create_chat_model[#6 models] + build_subagent_runtime_middlewares[#24] + create_agent(checkpointer=False)
        ├── _build_initial_state → skills[#19] + tool_search[#22] 延迟装配
        └── 图根 tracing callbacks[#16] + Langfuse 元数据注入
  │
▼ 被 task 工具调用，由 SubagentLimitMiddleware（#24，MAX_CONCURRENT=3）截断
▼ token 经 record_external_llm_usage_records 回灌父 RunJournal（#10）→ 持久化 token_usage_by_model（#7）
```

- **上游**：[config/subagents_config.py](../backend/packages/harness/deerflow/config/subagents_config.py)（自定义/per-agent/全局覆盖）、[#13 sandbox.md](sandbox.md) 的 `security.is_host_bash_allowed`（host-bash 准入决定 bash 子代理可见性）、[#6 models.md](models.md) 的 `create_chat_model`。
- **下游**：`task` 工具（[#22 tools.md](tools.md)）是唯一调用方；`SubagentLimitMiddleware`（[#24 middlewares.md](middlewares.md)）截断并发；`ToolErrorHandlingMiddleware` 用 `status_contract` 给结果盖状态戳。
- **token 链路**：collector → [#10 run_journal.md](run_journal.md) 的 `record_external_llm_usage_records` / `_record_model_usage` → [#7 persistence.md](persistence.md) 的 `token_usage_by_model` 持久化。
- **tracing**：图根挂 `build_tracing_callbacks`（[#16 tracing.md](tracing.md)），`inject_langfuse_metadata` 关联父 thread / user_id（源码注释标 #3611）。

---

## §9 实现差异（vs 上游 deer-flow 源码）

> 对照基线 = `deer-flow/backend/packages/harness/deerflow/subagents/`（与 mini 同布局：同 7 文件 + `builtins/`）。已**剥 docstring/comment 后**判逻辑差。结论：**mini 的子代理是上游的忠实移植**——§5 讲的单 `_scheduler_pool`(3) + 持久隔离事件循环、6 状态机（含 `CANCELLED`）、协作取消、`SubagentTokenCollector`、`system_prompt` 合成单条 SystemMessage、`build_subagent_runtime_middlewares`，**上游源码全有**（`registry.py` / `status_contract.py` 与 mini **0 行差异**）。真差异很小：

### 9.1 一致的部分（先放心）

| 维度 | 上游 deer-flow | mini |
|---|---|---|
| 单 `_scheduler_pool`(3) + 持久隔离事件循环 | 有（`_scheduler_pool`/`_run_isolated_subagent_loop`/`asyncio.new_event_loop` + `subagent-persistent-loop` daemon 线程） | **完全相同** |
| 6 状态机（PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/TIMED_OUT） | 有（含 `CANCELLED`） | **相同** |
| 协作取消（astream 迭代边界查 `cancel_event`） | 有 | **相同** |
| `SubagentTokenCollector`（按 caller+模型归桶 + run_id 去重） | 有 | **相同** |
| `system_prompt` 合成单条 SystemMessage（避开多 SystemMessage API 限制） | 有（executor 同一注释「avoid multiple SystemMessages」） | **相同** |
| `build_subagent_runtime_middlewares` | 有 | **相同** |
| 内置 max_turns（general-purpose=150 / bash=60） | 150 / 60 | **相同 150 / 60** |
| `registry.py` / `status_contract.py` | 有 | **0 行差异** |

### 9.2 mini 新增的小 helper / 细节差异

| 差异 | 说明 |
|---|---|
| `_resolve_subagent_runtime_middlewares`（mini executor.py） | mini 把子代理中间件解析抽成 helper（上游内联/换名），功能等价 |
| `task_tool.py`（51 行差） | task 工具签名/参数有细节差异（mini 走 config 覆盖拿 max_turns，不在工具签名里带），实码 + docstring 差异 |
| `config.py` / `token_collector.py` / `__init__.py` | 各几行小差异（导出面/默认值），无功能分叉 |

### 9.3 一句话总结

mini 子代理的设计原则是「**忠实移植**」：核心执行模型（单池 + 持久隔离循环 / 6 状态 / 协作取消 / token 归桶 / system_prompt 单条）与上游 deer-flow **一一对应**，不是 mini 自创、也不是「修正 book」。差异只是几个小 helper 的抽法 + task_tool 细节。读完 mini 这篇，迁到上游子代理几乎零认知差。

---

## §10 常见问题 / 排错

**Q：为什么用「持久隔离事件循环」而不是每次 `asyncio.run`？**
A：复用共享 async client。httpx/MCP 等异步 client 绑在创建它的事件循环上。若每次执行都 `asyncio.run`（起循环→跑→关循环），client 就绑到一个随即关闭的短命循环，下次用就坏。持久隔离循环让 client 长命复用。看门人超时由 scheduler worker 担任（[executor.py:857](../backend/packages/harness/deerflow/subagents/executor.py#L857)）。

**Q：`MAX_CONCURRENT_SUBAGENTS=3` 怎么保证？**
A：两道关——① `SubagentLimitMiddleware`（[#24 middlewares.md](middlewares.md)）在模型响应后截断多余 `task` 调用；② `_scheduler_pool` 本身只有 3 个 worker，第 4 个排队。执行器不自建第二线程池。

**Q：子代理能再调 `task` 委派吗？**
A：不能。所有内置子代理的 `disallowed_tools` 都含 `task`（[general_purpose.py:63](../backend/packages/harness/deerflow/subagents/builtins/general_purpose.py#L63)），防无限递归嵌套。自定义子代理的 `disallowed_tools` 默认也含 `task`（[subagents_config.py:65](../backend/packages/harness/deerflow/config/subagents_config.py#L65)）。

**Q：子代理用 lead agent 的沙箱吗？**
A：用。`SubagentExecutor` 接收父的 `sandbox_state` / `thread_data` / `thread_id`，透传进子代理初始状态（[executor.py:546](../backend/packages/harness/deerflow/subagents/executor.py#L546)）。所以子代理读写的是**同一个** `/mnt/user-data/workspace`——主子代理共享工作目录。

**Q：`bash` 子代理为什么有时看不到？**
A：当 host bash 未被放行（`is_host_bash_allowed()=False`，`LocalSandboxProvider` 默认）时，`get_available_subagent_names` 会把 `bash` 从可见列表隐藏（[registry.py:166](../backend/packages/harness/deerflow/subagents/registry.py#L166)）——Local 沙箱的 host bash 不是安全边界（见 [sandbox.md](sandbox.md)）。切到 AIO 沙箱（有真隔离，见 [aio_sandbox.md](aio_sandbox.md)）或设 `sandbox.allow_host_bash: true` 才会显示。

**Q：子代理的 token 算进谁的账？按模型还是按 caller？**
A：算进**父 run**，且**既按 caller 又按模型**。`SubagentTokenCollector` 收子代理 LLM 用量（含真实模型名），执行完经 `RunJournal.record_external_llm_usage_records` 回灌——caller 标 `subagent:<name>` 归桶进 `_subagent_tokens`，model_name 归桶进 `_tokens_by_model`（输出 `token_usage_by_model`）。按 `run_id` 去重防流式双计。详见 [#10 run_journal.md](run_journal.md)。

**Q：流式收集 AI 消息为什么不会重复？性能如何？**
A：`stream_mode="values"` 每个 super-step 重发完整累积态，同一条尾部 AI 消息每个 chunk 都会被重新看到。`_aexecute` 进循环前预建 `seen_message_ids` 集合（含 holder 自带旧消息的 id），循环内 O(1) 查重、append 后维护集合（源码注释标 #3687）——把原本每 chunk O(n) 的 `any(...)` 扫描（整轮 O(n²)，max_turns=150 时显著）压成线性。无 id 的极少路径仍 fallback 到整 dict 比较，语义不变。

**Q：自定义子代理的 timeout 会被全局覆盖吗？**
A：不会。全局 `subagents.timeout_seconds` 只覆盖**内置**子代理（[registry.py:99](../backend/packages/harness/deerflow/subagents/registry.py#L99)）。自定义子代理用自身在 `custom_agents` 段声明的 `timeout_seconds`（除非在 `agents.<name>` 里给了 per-agent 覆盖）。合并优先级：built-in → custom → per-agent override。

**Q：子代理能看到 lead agent 的对话历史吗？**
A：不能，这是刻意设计。子代理只收到一条 `HumanMessage`（任务描述），看不到 lead agent 的对话历史、其他子代理的中间结果、用户最初上下文（见 book §8.8）。专注单一任务的 agent 推理质量更高。代价：lead agent 的 `prompt` 必须**自包含**——不能写「继续之前的搜索」，要把所有背景/约束/输出格式打包进这一条消息。

---

## §11 小结

子代理是 lead agent 的「专员委派」机制：主 agent 调一次 `task` 工具，一个独立子 agent 用自己的上下文把子任务干完，只回一句摘要，主对话保持干净。核心设计：

- **执行引擎**（[executor.py](../backend/packages/harness/deerflow/subagents/executor.py)）：单 `_scheduler_pool`(3) + 持久隔离事件循环（复用共享 async client），6 状态机 + `try_set_terminal` 原子终态，协作取消（迭代边界查 `cancel_event`），看门人超时。
- **配置/合并**（[registry.py](../backend/packages/harness/deerflow/subagents/registry.py)）：内置 → 自定义 → per-agent 三层合并，全局 timeout/max_turns 只覆盖内置，按沙箱安全隐藏 bash。
- **状态契约**（[status_contract.py](../backend/packages/harness/deerflow/subagents/status_contract.py)）：5 终态值塞进 `additional_kwargs`，替代脆弱的字符串前缀匹配，共享 fixture 钉死前后端。
- **token 计量**（[token_collector.py](../backend/packages/harness/deerflow/subagents/token_collector.py)）：按 caller + 模型归桶回灌父 RunJournal，按 run_id 去重。

关键不变量是**共享沙箱 + 隔离上下文**：子代理读写 lead agent 的文件（协作），但对话历史独立（不污染），prompt 须自包含。这正是多 agent 系统在「协作」与「独立」之间的平衡。

> 上一篇：[#14 aio_sandbox.md](aio_sandbox.md)（AIO 沙箱——容器隔离；本篇子代理跑在 lead agent 给它的沙箱里）
> 下一篇：[#16 tracing.md](tracing.md)（链路追踪——LangSmith/Langfuse 图根注入，本篇 executor 的 `build_tracing_callbacks` + `inject_langfuse_metadata` 的来源）
