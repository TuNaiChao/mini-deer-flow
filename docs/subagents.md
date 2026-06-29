# 15. subagents.md — 子代理（委派 / 单调度池 + 持久隔离事件循环 / 自定义子代理）

> **M11 全维重审（2026-06-27）**：逐文件 diff 最新上游——确认 #34（持久隔离循环）/ #35（并发上限）/
> 5 状态契约 / `checkpointer=False`（#3559）/ 协作取消 / 单 SystemMessage 合并（#2701）/ 超时终态原子
> （#2583）mini **均已含**；补 1 项性能对齐 **#3687**（流式 AI 消息去重 O(n²)→O(n)）；**#3658**（按模型
> 归桶 token）属 journal/runs 域特性、mini 全缺 `_record_model_usage`，归 M18 重审。详见 [todo.md](todo.md) §3.1-B。

> **一句话定位**：子代理让主 agent（lead agent）把一个子任务**整个委派**给另一个独立 agent 去跑——
> 比如「去 bash 里跑一串命令」「去做一轮深度调研」。主 agent 只发一句「task」工具调用，
> 子代理在**后台**跑完，把结果摘要回灌。主 agent 的上下文不被冗长的中间步骤污染。

读完 [sandbox.md](sandbox.md)（懂了工具与沙箱）再看本篇最省事——子代理就是一个**自带
工具集与提示词的小 agent**，跑在主 agent 给它的沙箱里。

---

## 为什么需要子代理（痛点）

主 agent（lead agent）一把梭有两个常见问题：

1. **上下文爆炸**：跑一串 `npm install && npm test` 会产出几千行日志。全塞进主对话，
   后续每一轮都带着这些日志，token 很快爆。
2. **职责不清**：调研、写代码、跑命令混在一个 agent 里，提示词难写、容易跑偏。

子代理的解法：主 agent 调一次 `task` 工具说「帮我调研 X」或「帮我跑这些命令」，一个
**独立的子 agent** 用自己的上下文把活干完，只回**一句摘要**。主 agent 的对话保持干净。

类比：你是项目经理（lead agent），遇到一个独立子活，你不自己干，而是**派一个专员**
（subagent）去干，专员干完回来给你一份**一页纸汇报**。专员的草稿纸（中间步骤）你不看。

---

## 核心概念（名词 + 类比）

### ① SubagentConfig（一个子代理长什么样）

一个 dataclass，描述子代理的「人设」：

- `name`：唯一标识（如 `general-purpose` / `bash` / 自定义名）。
- `description`：**何时**该委派给它（写进 `task` 工具描述，供主 agent 的 LLM 选择）。
- `system_prompt`：引导它行为的系统提示词。
- `tools`：工具**白名单**。`None` = 继承主 agent 全部工具；给列表 = 只留这些。
- `disallowed_tools`：工具**黑名单**（默认含 `task`，防子代理再嵌套委派→无限递归）。
- `skills`：技能白名单（`None`=全部、`[]`=无、列表=只这些）。
- `model`：`'inherit'`（用主 agent 的模型）或显式模型名。
- `max_turns` / `timeout_seconds`：最大轮次 / 执行时间上限。

### ② 内置子代理（builtins）

mini 开箱两个内置子代理（[builtins/](../backend/packages/harness/deerflow/subagents/builtins/)）：

- **`general-purpose`**：通用多步子代理。继承**除** `task`/`ask_clarification`/`present_files`
  外的全部工具，`max_turns=150`，适合「既要探索又要动手」的深任务。
- **`bash`**：命令执行专员。只挂沙箱 5 工具（bash/ls/read_file/write_file/str_replace），
  `max_turns=60`。当 host bash 未被放行时（`is_host_bash_allowed()=False`，Local 沙箱默认），
  registry 会把它从可见列表**隐藏**——因为它不安全。

### ③ 自定义子代理（config.yaml `subagents.custom_agents`）

用户可在 config.yaml 里声明任意自定义子代理类型：

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

然后主 agent 的 `task` 工具调 `subagent_type: researcher` 就能委派给它。

### ④ per-agent 覆盖（config.yaml `subagents.agents`）

对**任意**子代理（内置或自定义）按名压一层覆盖：

```yaml
subagents:
  agents:
    bash:
      timeout_seconds: 120     # 把 bash 子代理超时改 120s
      max_turns: 30
```

合并优先级（见 [registry.py](../backend/packages/harness/deerflow/subagents/registry.py) `get_subagent_config`）：

```
built-in（内置默认） → custom（自定义自带值） → per-agent override（agents.<name>）
```

注意：**全局** `subagents.timeout_seconds` / `max_turns` 只覆盖**内置**子代理，**不**覆盖
自定义子代理（自定义子代理在 `custom_agents` 段自带默认值）。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### ⚠️ 单 scheduler pool + 持久隔离事件循环（红线 #34，**非双线程池**）

这是 M11 最关键、也最容易被记错的设计。deer 早期文档误记成「双线程池」——实际是：

- **唯一线程池** `_scheduler_pool = ThreadPoolExecutor(max_workers=3)`：负责后台任务的
  调度与编排（`execute_async` 把任务提交到这里）。
- **持久隔离事件循环** `_isolated_subagent_loop`：一个 **daemon 线程上的常驻 `asyncio`
  事件循环**。当主 agent 自己已在一个事件循环里跑时（LangGraph 的常态），子代理的协程
  **提交到这个隔离循环**执行，而不是每次 `asyncio.run` 起一个新循环再关掉。

为什么这样？——**复用共享 async client**。httpx / MCP 等异步客户端是**绑在创建它的那个
事件循环**上的。如果每次执行子代理都 `asyncio.run`（起一个新循环 → 跑 → 关循环），client
就被绑到一个随即关闭的短命循环上，下次用就坏了。复用一个长命循环 = 复用 client。

> 测试锁定（[test_subagents.py](../test/test_subagents.py) `test_single_scheduler_pool_not_dual`）：
> 断言 `_scheduler_pool` 是 3-worker 的 ThreadPoolExecutor，且**不存在** `_execution_pool`。

### `MAX_CONCURRENT_SUBAGENTS = 3`：并发上限怎么保证

并发上限由**两道关**共同保证，不是执行器单方面的事：

1. **`SubagentLimitMiddleware`**（M16 第 19 步）：在模型响应**之后**截断多余的 `task` 调用——
   如果 LLM 一口气发了 5 个 `task` 调用，中间件只留前 3 个，多余转成「并发上限」提示。
2. **`_scheduler_pool` 只有 3 个 worker**：即便中间件漏了，线程池也只有 3 个槽，第 4 个排队。

### 子代理图 `checkpointer=False`（一次性）

子代理是**一次性**的：跑完就结束，**从不 resume**。故子代理图编译时 `checkpointer=False`——
既不继承主 run 的 checkpointer（会污染主状态），也不自建。主 run 的状态由主 agent 自己的
checkpointer 管，子代理只回一个结果字符串。

### 协作取消（在 astream 迭代边界）

子代理线程**不能被 `Future.cancel()` 强杀**——协程一旦在跑，杀不掉。所以取消是**协作式**的：

- `request_cancel_background_task(task_id)` 在结果的 `cancel_event` 上置位。
- `_aexecute` 在 `agent.astream()` 的**每次迭代顶部**检查 `cancel_event`，置位了就返回 CANCELLED。

代价：**单个迭代内的长工具调用不会被中断**——要等下一个 chunk 才能停。这是有意权衡：
强杀会让子代理的文件 / 状态半途而废，协作取消保证停在一个干净的边界。

### 5 状态契约（红线 #35，后端↔前端单一真相源）

子代理执行有 5 个**终态**（外加 pending/running 两个非终态）：

```
pending → running → {completed, failed, cancelled, timed_out, polling_timed_out}
```

前端要据此渲染子任务卡片。旧前端靠**字符串前缀匹配** `task` 工具结果文本推状态——后端
改个措辞前端就坏（#3146）。新契约把状态塞进 `ToolMessage.additional_kwargs`：

- `subagent_status`：5 值之一。
- `subagent_error`（可选）：可读错误文本。

「结果文本 → 状态」的映射是后端 stamper 与前端 fallback 解析器**唯一**要对齐的东西。共享
fixture [contracts/subagent_status_contract.json](../contracts/subagent_status_contract.json)
是单一真相源——13 个 case，两侧测试都加载它并断言（`test_all_fixture_cases`）。

### token 回灌（子代理开销算进父 run）

子代理的 LLM 调用 token 不能丢。`SubagentTokenCollector`（一个 `BaseCallbackHandler`）挂在
子代理执行上，收集每次 `on_llm_end` 的 usage（按 `run_id` 去重防流式双计）。执行完后，
`record_external_llm_usage_records` 把这些记录回灌父 `RunJournal`，caller 标 `subagent:<name>`。
于是父 run 的 token 核算**含**子代理开销。

> **#3658（按模型归桶，待 M18）**：上游 `token_collector` 额外把**真正产生这次响应的模型名**
> （从 `message.response_metadata` 的 `model_name`/`model` 键）写进记录，journal 的
> `_record_model_usage` 再按模型分桶统计（`token_usage_by_model`）。mini 的 journal **完全没有**
> `_record_model_usage` 这套机制（主线 LLM 路径也未归桶），故本特性跨 token_collector + journal +
> worker + 持久化迁移四个模块，整体归 **M18 runs / M6 journal** 重审一并补，不在 M11 内零碎半挂。

### 流式 AI 消息去重（#3687，O(n²)→O(n)）

`_aexecute` 用 `stream_mode="values"` 流式跑子代理图——LangGraph 每个 super-step 都把**完整
累积态**重发一次，所以同一条尾部 AI 消息会在**每个 chunk** 被重新看到。要把这些 AI 消息收进
`result.ai_messages`（供后续序列化 / 调试用），就得去重。

- **旧**（mini 此前）：每个 chunk 都 `any(msg.get("id") == new_id for msg in ai_messages)` 扫一遍
  列表 → 每 chunk O(n)、整轮 O(n²)。深研究子代理能跑到 `max_turns=150`，平方级开销显著。
- **新（#3687，对齐上游）**：进流式循环**前**用集合推导 `{mid for msg in ai_messages if (mid := msg.get("id"))}`
  预建 `seen_message_ids`（也兜住 `result_holder` 自带的旧消息），循环内 `message_id in seen_message_ids`
  O(1) 查、append 后 `seen_message_ids.add(message_id)` 维护。无 `id` 的极少路径仍 fallback 到
  `message_dict in ai_messages` 整 dict 比较。

语义不变（去重结果一致），只是把平方级压成线性。测试：[test_subagents.py](../test/test_subagents.py)
`test_execute_dedups_ai_messages_by_id` / `test_execute_seen_set_seeded_from_holder_messages` /
`test_execute_dedups_many_distinct_ids_and_repeats`。

### Phase 2 骨架：真实 agent 构造依赖 Phase 7

执行器的**机制**（状态/结果/线程池/隔离循环/取消/超时/后台任务存储）在 Phase 2 就完整可用。
但 `_create_agent` 真正构造 agent 依赖：

- **Phase 7** 的 `build_subagent_runtime_middlewares`（中间件组装）；
- **Phase 5** 的 `tool_search`（延迟 MCP 工具装配）；
- **Phase 4** 的 skills（技能加载与 allowed-tools 策略）。

这些用**延迟导入 + 缺包降级**处理（[executor.py](../backend/packages/harness/deerflow/subagents/executor.py)
`_resolve_subagent_runtime_middlewares` / `_load_skills` / `_build_initial_state`）：

- `build_subagent_runtime_middlewares` 缺失 → 回退 `[ToolErrorHandlingMiddleware()]`（mini 已有）。
- `tool_search` 缺失 → 跳过延迟装配，工具即策略过滤后的列表。
- skills 包缺失 → 子代理不带技能跑。

等对应 Phase 落地，本文件**自动**切到真实实现，无需改执行器。

---

## 代码架构与数据流

子代理一次执行的**内部数据流**（谁触发谁、数据怎么流）：

```
主 agent（lead agent）的 LLM 决定委派子任务
  │  调 task 工具：task(description, prompt, subagent_type?)
  ▼
task_tool（M15，唯一调用方）
  │  ① get_subagent_config(subagent_type)        ← registry 三层合并（内置→custom→per-agent 覆盖）
  │  ② 构造 SubagentExecutor(config, tools, sandbox_state, thread_data, thread_id, parent_model)
  │  ③ executor.execute_async(task, task_id)      ← 提交到 _scheduler_pool
  │  ④ 轮询 get_background_task_result(task_id)，每 5s 一次
  ▼
_scheduler_pool（唯一 3-worker ThreadPoolExecutor，红线 #34）
  │  worker 线程跑 run_task：
  │    主 agent 已在事件循环 → asyncio.run_coroutine_threadsafe(_aexecute, _isolated_subagent_loop)
  │    否则                  → asyncio.run(_aexecute)
  ▼
_aexecute（跑在持久隔离循环上的协程主体）
  │  ① _build_initial_state(task) → 初始 state + 工具（+ skills[M14] + tool_search[M15] 延迟装配）
  │  ② _create_agent → create_agent(…, checkpointer=False)   ← #3559：一次性，不继承父 checkpointer
  │  ③ 挂 SubagentTokenCollector 回调（收 usage）
  │  ④ async for chunk in agent.astream(state, stream_mode="values"):
  │        • 迭代顶部查 cancel_event → 协作取消（返回 CANCELLED）
  │        • 抽 AIMessage，按 id 用 seen_message_ids 集合 O(1) 去重（#3687）→ 入 result.ai_messages
  │        • 超时（timeout_seconds 到）→ TIMED_OUT
  │  ⑤ collector.snapshot_records() → token 记录
  │  ⑥ result.try_set_terminal(COMPLETED | FAILED | TIMED_OUT | CANCELLED, token_usage_records=…)
  ▼
task_tool 收 result
  │  • 抽最终 AIMessage 文本作为结果摘要
  │  • make_subagent_additional_kwargs(status) → 盖 subagent_status 戳到 ToolMessage.additional_kwargs
  │  • journal.record_external_llm_usage_records(token_records) → token 回灌父 run（caller=subagent:<name>）
  ▼
主 agent 收到带 subagent_status 的 ToolMessage，继续对话
```

**状态机**（`SubagentStatus` 枚举 6 值；终态由 `try_set_terminal` 在 `_state_lock` 下**原子**设一次，#2583）：

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
   终态 = {COMPLETED, FAILED, CANCELLED, TIMED_OUT}；is_terminal 判定后 try_set_terminal 拒绝再改。
   前端可见的 subagent_status 串（status_contract）= completed/failed/cancelled/timed_out/polling_timed_out。
```

---

## 逐文件分析

```
subagents/
├── __init__.py · config.py · registry.py · executor.py · status_contract.py · token_collector.py
└── builtins/ { __init__.py · general_purpose.py · bash_agent.py }

config/subagents_config.py          # 全局配置（custom_agents / agents 覆盖 / 全局 timeout·max_turns）
contracts/subagent_status_contract.json   # 5 状态契约的后端↔前端共享 fixture
```

**`executor.py`** — 子代理执行的**引擎**，模块最重、改动最频的文件。定义 `SubagentStatus` 枚举（6 值）、
`SubagentResult` 数据类（带 `try_set_terminal` 原子终态转移 + `cancel_event` 协作取消位）、模块级
`_scheduler_pool`（唯一 3-worker 线程池）、`_isolated_subagent_loop`（daemon 线程上的常驻事件循环）
+ `_shutdown_isolated_subagent_loop`（进程退出经 `atexit` 优雅关闭，防循环泄漏）。`SubagentExecutor`
类提供 `execute`（同步）/ `execute_async`（后台，提交到线程池）/ `_aexecute`（隔离循环上的协程主体：
建状态→建图→流式收集 AI 消息→协作取消→终态盖戳）。**为什么单独**：把「机制全集」（状态/线程池/循环/
取消/超时/后台任务存储）与「配置/契约」解耦——执行机制独立演进时不必碰纯数据文件。

**`config.py`** — `SubagentConfig` dataclass（一个子代理的「人设」：name/description/system_prompt/
tools/disallowed_tools/skills/model/max_turns/timeout_seconds）+ `resolve_subagent_model_name`（模型名
三优先级：显式非 `'inherit'` > 父模型 > `app_config.models[0]`）。**为什么单独**：纯数据 + 无副作用
的纯函数，零运行时依赖，便于单测、且被 registry 与 executor 共享。

**`registry.py`** — 内置子代理表 + config.yaml `custom_agents` + `agents.<name>` per-agent 覆盖的**三层
合并**（`get_subagent_config`，优先级 built-in→custom→per-agent），外加 `get_available_subagent_names`
按 `is_host_bash_allowed()` 隐藏不安全的 `bash` 子代理。**为什么单独**：合并优先级是独立于执行的纯
函数逻辑，且它桥接 `subagents_config`（用户配置）与 `sandbox/security`（安全准入）两个上游——单独
成文件把这两类外部依赖收敛在一处。

**`status_contract.py`** — 子代理 5 终态契约：`extract_subagent_status`（结果文本→状态，前缀表按「最
具体在前」排序，因部分前缀互为子串）+ `make_subagent_additional_kwargs`（状态→`ToolMessage.
additional_kwargs` 的 `subagent_status`/`subagent_error`）。**为什么单独**：这是**后端↔前端的对齐
边界**——旧前端靠字符串前缀匹配推状态、后端改措辞前端就坏（#3146）；独立成文件 + 共享 fixture
让两侧测试能钉死同一份契约。

**`token_collector.py`** — `SubagentTokenCollector`（一个 `BaseCallbackHandler`）：挂子代理执行上，
每次 `on_llm_end` 收 LLM usage、按 `run_id` 去重（防流式双计）、`snapshot_records` 返回记录副本供
回灌父 `RunJournal`。**为什么单独**：回调处理器是独立于执行机制的「**计量旁路**」——它不改变子代理
行为，只旁听 token 用量。单独成文件便于在执行器之外复用与单测，也对应上游「计量与执行分离」的分层。

**`builtins/__init__.py`** — `BUILTIN_SUBAGENTS` 表：`general-purpose` + `bash` 两个出厂 `SubagentConfig`。
**为什么单独**：内置子代理是「出厂自带」配置，与用户自定义分开存放，便于 registry 区分 built-in vs
custom、应用不同的合并优先级（全局 timeout/max_turns 只覆盖内置）。

**`builtins/general_purpose.py`** — `GENERAL_PURPOSE_CONFIG`：继承主 agent 全部工具（除
`task`/`ask_clarification`/`present_files`，防嵌套委派与澄清中断），`max_turns=150`（深研究子任务不再
开箱撞 `GraphRecursionError`）。**为什么单独**：通用子代理有独立的工具策略 + 较长 system_prompt 文本，
与 bash 专员的安全考量分开。

**`builtins/bash_agent.py`** — `BASH_AGENT_CONFIG`：仅 5 个沙箱工具（bash/ls/read_file/write_file/
str_replace），`max_turns=60`。**为什么单独**：bash 专员有独立的安全语境（host-bash 未放行时被
registry 隐藏）与独立提示词。

**`__init__.py`** — 包导出面：`SubagentConfig`/`SubagentExecutor`/`SubagentResult`/`SubagentStatus` +
registry 函数 + 后台任务管理函数（`request_cancel_background_task`/`get_background_task_result`/
`list_background_tasks`/`cleanup_background_task`）。**为什么单独**：聚合导出层，定义外部唯一调用方
（`task` 工具）的 import 面，避免外部直接深 import 内部模块。

> **配套（非本目录）**
> - [config/subagents_config.py](../backend/packages/harness/deerflow/config/subagents_config.py) —
>   `SubagentsAppConfig`（`enabled`/`max_concurrent`/全局 `timeout_seconds`·`max_turns` + `custom_agents`
>   + `agents` 覆盖）+ `CustomSubagentConfig` + `SubagentOverrideConfig` + helper。属 config 模块但服务
>   subagents。
> - [contracts/subagent_status_contract.json](../contracts/subagent_status_contract.json) —
>   5 状态 × 多 case 的后端↔前端契约 fixture（`valid_status_values` + `cases`），两侧测试加载同一份。

---

## 关键接口

### `SubagentConfig`（`config.py`，dataclass）

```python
@dataclass
class SubagentConfig:
    name: str
    description: str
    system_prompt: str | None = None
    tools: list[str] | None = None            # None = 继承全部
    disallowed_tools: list[str] | None = ["task"]
    skills: list[str] | None = None           # None = 全部 / [] = 无 / 列表 = 白名单
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900

def resolve_subagent_model_name(config, parent_model, *, app_config=None) -> str: ...
```

### registry（`registry.py`）

```python
def get_subagent_config(name, *, app_config=None) -> SubagentConfig | None: ...
    # built-in → custom → per-agent override 合并
def get_subagent_names(*, app_config=None) -> list[str]: ...       # 内置 + 自定义
def get_available_subagent_names(*, app_config=None) -> list[str]: ...  # 按沙箱安全隐藏 bash
def list_subagents(*, app_config=None) -> list[SubagentConfig]: ...
```

### `SubagentExecutor`（`executor.py`）

```python
class SubagentExecutor:
    def __init__(self, config, tools, *, app_config=None, parent_model=None,
                 sandbox_state=None, thread_data=None, thread_id=None, trace_id=None): ...
    def execute(self, task, result_holder=None) -> SubagentResult: ...        # 同步
    def execute_async(self, task, task_id=None) -> str: ...                   # 后台，返回 task_id

# 后台任务管理（模块级）
MAX_CONCURRENT_SUBAGENTS = 3
def request_cancel_background_task(task_id) -> None: ...   # 协作取消
def get_background_task_result(task_id) -> SubagentResult | None: ...
def list_background_tasks() -> list[SubagentResult]: ...
def cleanup_background_task(task_id) -> None: ...          # 只清终态任务
```

### status_contract（`status_contract.py`）

```python
SUBAGENT_STATUS_KEY = "subagent_status"
SUBAGENT_ERROR_KEY = "subagent_error"
SUBAGENT_STATUS_VALUES = ("completed", "failed", "cancelled", "timed_out", "polling_timed_out")
def extract_subagent_status(content: str) -> SubagentStatusValue | None: ...
def make_subagent_additional_kwargs(status, *, error=None) -> dict[str, str]: ...
```

---

## 应用方法

### 配置（`config.yaml`）

```yaml
subagents:
  enabled: true                # 主开关（task 工具据此挂载）
  max_concurrent: 3            # 并发上限（与 SubagentLimitMiddleware 共同保证）
  timeout_seconds: 1800        # 内置子代理默认超时（30 分钟）
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

### 主 agent 如何委派（M15 task 工具，Phase 5 落地后）

主 agent 的 `task` 工具（`subagent_type` 可选内置名或自定义名）→ `SubagentExecutor.execute_async`
后台跑 → 轮询 5s → SSE 事件（`task_started`/`running`/`completed`/`failed`/`timed_out`）→
结果摘要 + 状态盖戳（`additional_kwargs.subagent_status`）。

### 跑测试

```bash
cd backend && make test    # 含 test/test_subagents.py（72 个 hermetic 测试，含 #3687 去重回归）
```

测试约定：agent 构造经 monkeypatch 注入 fake（`_FakeAgent` 带 `.astream` 产预设 chunk），
不碰真模型；隔离循环复用、后台任务生命周期、协作取消、超时、降级全 mock；后台任务全局存储
每测试 autouse 清理，防跨测试污染；status_contract 加载 `contracts/subagent_status_contract.json`
13 个 case 全断言。

---

## 与其它模块的关系

```
config/subagents_config (custom_agents / agents 覆盖 / 全局 timeout/max_turns)
  │
sandbox/security (is_host_bash_allowed → get_available_subagent_names 隐藏 bash)
  │
subagents
  ├── config.SubagentConfig + resolve_subagent_model_name
  ├── registry (built-in + custom + override 合并)
  ├── status_contract (5 状态契约 ← contracts/subagent_status_contract.json)
  ├── token_collector (收 usage → 回灌父 RunJournal[M7])
  ├── builtins (general-purpose / bash)
  └── executor
        ├── 单 _scheduler_pool(3) + 持久 _isolated_subagent_loop（红线 #34）
        ├── _create_agent → create_chat_model[M-models] + build_subagent_runtime_middlewares[M16] + create_agent
        ├── _build_initial_state → skills[M14] + tool_search[M15] 延迟装配
        └── 子代理图 checkpointer=False（一次性）
  │
▼ 被 task 工具（M15）调用，由 SubagentLimitMiddleware（M16 第 19 步，MAX_CONCURRENT=3）截断
```

- **上游**：`config/subagents_config`（自定义/per-agent/全局覆盖）、`sandbox/security`
  （host-bash 准入决定 bash 子代理可见性）、`models`（`create_chat_model`）。
- **下游**：`task` 工具（M15）是唯一调用方；`SubagentLimitMiddleware`（M16）截断并发；
  `ToolErrorHandlingMiddleware`（M16）用 `status_contract` 给结果盖状态戳。
- **Phase 7 解锁**：`build_subagent_runtime_middlewares` 落地后，执行器从「fallback 最小中间件」
  自动切到「与 lead agent 共享的完整中间件组装」。

---

## 常见问题 / 排错

**Q：为什么是「单 scheduler pool + 持久隔离循环」，不是双线程池？**
A：复用共享 async client。httpx/MCP 等异步 client 绑在创建它的事件循环上。若每次执行都
`asyncio.run`（起循环→跑→关循环），client 就绑到一个随即关闭的短命循环，下次用就坏。持久
隔离循环让 client 长命复用。这是红线 #34，deer 早期文档误记成「双线程池」。

**Q：`MAX_CONCURRENT_SUBAGENTS=3` 怎么保证？**
A：两道关——① `SubagentLimitMiddleware`（M16）在模型响应后截断多余 `task` 调用；② `_scheduler_pool`
本身只有 3 个 worker，第 4 个排队。执行器不自建第二线程池。

**Q：子代理能再调 `task` 委派吗？**
A：不能。所有内置子代理的 `disallowed_tools` 都含 `task`，防无限递归嵌套。自定义子代理的
`disallowed_tools` 默认也含 `task`（见 `CustomSubagentConfig`）。

**Q：子代理用主 agent 的沙箱吗？**
A：用。`SubagentExecutor` 接收父的 `sandbox_state` / `thread_data` / `thread_id`，透传进子代理
初始状态。所以子代理读写的是**同一个** `/mnt/user-data/workspace`——主子代理共享工作目录。

**Q：`bash` 子代理为什么有时看不到？**
A：当 host bash 未被放行（`is_host_bash_allowed()=False`，LocalSandboxProvider 默认）时，
`get_available_subagent_names` 会把 `bash` 从可见列表隐藏——Local 沙箱的 host bash 不是安全
边界（见 [sandbox.md](sandbox.md)）。切到 AIO 沙箱（有真隔离）或设 `sandbox.allow_host_bash: true`
才会显示。

**Q：子代理的 token 算进谁的账？**
A：算进**父 run**。`SubagentTokenCollector` 收子代理 LLM 用量，执行完经
`RunJournal.record_external_llm_usage_records` 回灌，caller 标 `subagent:<name>`。按 `run_id`
去重防流式双计。（注：上游 #3658 还会按**模型**归桶 `token_usage_by_model`，mini 的 journal 尚无
这套机制，归 M18 一并补——见上文 token 回灌小节。）

**Q：流式收集 AI 消息为什么不会重复？性能如何？**
A：`stream_mode="values"` 每个 super-step 重发完整累积态，同一条尾部 AI 消息每个 chunk 都会被重新
看到。`_aexecute` 进循环前预建 `seen_message_ids` 集合（含 holder 自带旧消息的 id），循环内 O(1)
查重、append 后维护集合——把原本每 chunk O(n) 的 `any(...)` 扫描（整轮 O(n²)，max_turns=150 时显著）
压成线性（#3687）。无 id 的极少路径仍 fallback 到整 dict 比较，语义不变。

**Q：自定义子代理的 timeout 会被全局覆盖吗？**
A：不会。全局 `subagents.timeout_seconds` 只覆盖**内置**子代理。自定义子代理用自身在
`custom_agents` 段声明的 `timeout_seconds`（除非在 `agents.<name>` 里给了 per-agent 覆盖）。
合并优先级：built-in → custom → per-agent override。

**Q：Phase 2 的执行器能真跑 agent 吗？**
A：机制能跑（执行/取消/超时/后台任务全可用），但 `_create_agent` 真正构造 agent 依赖
Phase 7 的 `build_subagent_runtime_middlewares`、Phase 5 的 `tool_search`、Phase 4 的 skills。
这些未落地时优雅降级（fallback 中间件 / 无延迟 / 无技能），落地后自动切真实实现，不改执行器。
