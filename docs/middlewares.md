# 24. middlewares.md — 中间件链（对齐上游 26 步，Agent 的行为骨架）

> **一句话定位**：本模块给 agent 装「行为骨架」——25 个中间件按**严格顺序**串成一条链（对齐
> 上游 deer-flow 的 26 步，mini 跳过 #8 Guardrail），在模型调用前后、工具调用前后、agent 轮次
> 的开始结束做各种横切处理（防上下文爆炸、防循环、防提示词注入、注入上下文、安全拦截、错误兜底……）。
> 顺序是**契约**：换顺序会破坏功能甚至安全。

读完 [uploads.md](uploads.md)（懂了「上传文件怎么变 agent 上下文」）再看本篇最省事——本篇是
**所有横切行为的总装配**。`UploadsMiddleware` 是链的第 3 步，本篇解释它在整条链里的位置、以及
其它每一步各自干什么、为什么这个顺序。它把 Phase 2–5.5 的各模块（sandbox / subagents / skills /
memory / mcp / tools / uploads）串成一个能跑的 agent。

> **2026-06-27 六维重审**：补齐此前缺失的 `InputSanitization`(#1) / `SystemMessageCoalescing`(#20, #3711)
> / `TokenBudget`(#23) 三个中间件；确认 `DanglingToolCall`(#3746) 与 `LoopDetection`(#3709) 的上游
> 修复 mini 此前已含（逐文件 diff 仅文档措辞差异）。详见 §2 / §6。

---

## 0. 这个模块解决什么问题

agent 一次「思考-行动」会经过很多阶段：用户消息进来 → 净化输入 → 建隔离目录 → 读上传文件 →
分配沙箱 → 调模型 → 处理工具调用 → 检测循环 → 算 token 预算 → 生成标题 → 入记忆队列 …… 如果把
这些逻辑全堆进主 agent 函数，会得到一个几千行的巨型函数，谁都不敢改。

**中间件模式**：把每个横切关注点写成一个独立类（中间件），每个类实现几个**钩子**（hook）——
「模型调用前」「工具调用后」等。框架按顺序把所有中间件串起来，agent 跑到某个阶段时自动调对应
钩子。`build_middlewares()`（在 [__init__.py](../backend/packages/harness/deerflow/agents/middlewares/__init__.py)）
就是这条链的**装配清单**。

本模块对齐 deer-flow 的 26 步生产链（mini 跳过 #8 Guardrail——它依赖独立的 `guardrails` 模块，
列为「真正可选」，见 [todo.md](todo.md) §2.1），不裁剪其它核心功能。

## 1. AgentMiddleware 钩子机制（洋葱 + 前后）

每个中间件继承 `AgentMiddleware`，按需实现这些钩子（mini 用 langchain 的实现）：

| 钩子 | 触发时机 | 典型用途 |
|------|----------|----------|
| `before_agent` / `abefore_agent` | 一轮 agent 开始 | 建目录、注入上下文（日期/记忆/上传清单）、token 预算标记「已见」 |
| `after_agent` / `aafter_agent` | 一轮 agent 结束 | 入记忆队列、清瞬态状态（循环警告 / token 账本） |
| `before_model` / `abefore_model` | 调 LLM 前 | 摘要检测 |
| `after_model` / `aafter_model` | LLM 返回后 | 循环检测、token 预算累计、标题生成、安全拦截、子代理限流 |
| `wrap_model_call` / `awrap_model_call` | **包住**模型调用（可改入参/出参/重试） | 输入净化、SystemMessage 合并、LLM 错误重试、补 dangling 工具响应、延迟工具过滤、循环/预算警告注入 |
| `wrap_tool_call` / `awrap_tool_call` | **包住**工具调用 | 工具异常兜底、澄清拦截、沙箱审计、工具输出预算 |

两类模型：**前后钩子**（`before_*/after_*`）只能返回状态更新（dict），按链**正序**逐个跑；
**包裹钩子**（`wrap_*`）是「洋葱模型」——能改请求 / 响应、吞异常、重试，按链**倒序**包裹
（最后注册的 = 最外层）。异步钩子（`a` 前缀）跑在事件循环上；**阻塞 IO 必须卸线程**（红线 #1）。

## 2. 26 步顺序契约（对齐上游）

`build_middlewares(config, model_name, agent_name, ...)` 按下表顺序装配。顺序里的**硬约束**（红线）
用 ⚠️ 标注；步骤号对齐上游 deer-flow AGENTS.md：

```
=== 共享前置段 build_lead_runtime_middlewares（lead + subagent 都要）===
 1. InputSanitizationMiddleware    ⚠️ 最外层 wrap_model_call：提示词注入标签转义（#3630）
 2. ToolOutputBudgetMiddleware     ← 防爆：单个工具返回太大 → 外置磁盘 + 预览
 3. UploadsMiddleware              ⚠️ 接 M23：注入上传清单（仅 lead）
 4. ThreadDataMiddleware           ⚠️ 算/建每线程隔离目录（workspace/uploads/outputs）
 5. SandboxMiddleware              ⚠️ 须在 ThreadData 之后（依赖 thread_data 路径）
 6. DanglingToolCallMiddleware     ← 补悬空工具调用的占位响应（防 provider 400，#3746 已含）
 7. LLMErrorHandlingMiddleware     ← 重试/退避/熔断 + 用户可读兜底
 8. [GuardrailMiddleware]          ← 真正可选，mini 不做（依赖 guardrails 独立模块）
 9. SandboxAuditMiddleware         ← 沙箱命令审计（block/warn/pass 分级）
10. ToolErrorHandlingMiddleware    ← 工具异常 → 错误 ToolMessage（run 不中断）

=== lead-only 段 build_middlewares（仅 lead agent）===
11. DynamicContextMiddleware       ← ID-swap 注入日期/记忆到首条 HumanMessage
12. SkillActivationMiddleware      ← /skill-name 激活，注入 SKILL.md
13. SummarizationMiddleware        ← 可选：上下文近 token 上限时压缩（接 M13 抢拍钩子）
14. TodoMiddleware                 ← plan_mode 时挂载（write_todos 工具 + 上下文丢失检测）
15. TokenUsageMiddleware           ← 可选：记 token 用量 + 给每步贴动作归因
16. TitleMiddleware                ← 首轮后生成线程标题
17. MemoryMiddleware               ← 入记忆更新队列（filter→correction/reinforcement→add）
18. ViewImageMiddleware            ← 仅 supports_vision：view_image 完成后注入 base64 图片
19. DeferredToolFilterMiddleware   ← 接 M15/M20：延迟 MCP 工具未提升前不暴露 schema
20. SystemMessageCoalescingMiddleware ⚠️ #3711：合并所有 SystemMessage 成一条领头（严格后端兼容）
21. SubagentLimitMiddleware        ← 接 M11：截断超额 task 调用（clamp [2,4]）
22. LoopDetectionMiddleware        ← 哈希 + 频率双层循环检测（#3709 已含）
23. TokenBudgetMiddleware          ← 可选：单 run token 预算（软提醒 + 硬停剥 tool_calls）
24. custom_middlewares             ← 调用方自定义（插在 Clarification 前）
25. SafetyFinishReasonMiddleware   ← provider 安全终止时剥 tool_calls（content_filter/refusal/SAFETY）
26. ClarificationMiddleware        ⚠️ 永远最后（红线 #14）：拦截 ask_clarification 中断执行
```

subagent 用 `build_subagent_runtime_middlewares`（步骤 1-10 + vision/deferred/safety），不含 lead-only 段。

## 3. 一轮 agent 的数据流（架构图）

下面是一次「用户发消息 → agent 响应」里，链上各钩子何时触发、谁触发谁。**横向是时间**，纵向是
链上位置；`wrap_*` 用洋葱（ nesting ）表示，`before_*/after_*` 用顺序队列表示：

```
用户消息进入
   │
   ▼  ┌─ before_agent（正序：链头→链尾）─────────────────────────┐
      │  #3 Uploads 扫上传目录、注入文件清单                       │
      │  #4 ThreadData 建 (user_id,thread_id) 隔离目录            │
      │  #11 DynamicContext ID-swap 注日期/记忆                   │
      │  #23 TokenBudget 标记历史消息「已见」（不计入本轮）        │
      │  #22 LoopDetection 清上一轮残留警告                        │
      └──────────────────────────────────────────────────────────┘
   │
   ▼  ┌─ wrap_model_call（洋葱，倒序包裹：#26 最内 → #1 最外）──┐
      │  #1  InputSanitization   净化最后一条真实用户消息          ┐
      │  #20 SystemMessageCoalescing 合并 SystemMessage            │ 每层可改
      │  #22 LoopDetection       追加队列里的循环警告              │ request 并
      │  #23 TokenBudget         追加队列里的预算提醒              │ 交给下一层
      │  #7  LLMErrorHandling    重试 / 退避                       │
      │  #6  DanglingToolCall    补悬空调用的占位 ToolMessage      ┘
      │                          ↓ 最终的 request → 调 LLM ← 返回 AIMessage
      └──────────────────────────────────────────────────────────┘
   │
   ▼  ┌─ after_model（正序）────────────────────────────────────┐
      │  #22 LoopDetection    检测重复调用（命中→队列警告/硬停）  │
      │  #23 TokenBudget      累计 token（命中→队列提醒/硬停）    │
      │  #25 SafetyFinishReason 命中安全终止→剥 tool_calls        │
      │  #16 Title            首轮后异步生成标题                   │
      │  #15 TokenUsage       贴 usage 归因                       │
      └──────────────────────────────────────────────────────────┘
   │
   │  （若 AIMessage 带 tool_calls）→ 工具节点
   ▼  ┌─ wrap_tool_call（洋葱，倒序）───────────────────────────┐
      │  #9  SandboxAudit      审计命令（block/warn/pass）         │
      │  #10 ToolErrorHandling 异常→错误 ToolMessage（透传 BubUp） │
      │                          ↓ 执行工具 ← 返回 ToolMessage
      └──────────────────────────────────────────────────────────┘
   │
   │  （循环回到 wrap_model_call，直到模型不再调工具）
   ▼  ┌─ after_agent（正序）────────────────────────────────────┐
      │  #17 Memory           入记忆更新队列                       │
      │  #22 LoopDetection    清本轮未排空的瞬态警告               │
      │  #23 TokenBudget      清本轮账本                           │
      └──────────────────────────────────────────────────────────┘
   │
   ▼  agent 一轮结束，状态落盘（checkpoint）
```

**关键**：`after_model` 里检测到问题（循环 / 超预算）时，**不能当场插消息**（会破 `AIMessage(tool_calls)
→ ToolMessage` 配对），而是**排队**，由下一轮 `wrap_model_call` 把警告追加到请求**末尾**——这是
LoopDetection(#22) 与 TokenBudget(#23) 共用的「延迟注入」套路。

## 4. 为什么是这个顺序（关键不变量 / 红线）

### InputSanitization(#1) 必须最外层

它排第一 → 是 `wrap_model_call` 的**最外层**包装，所有内层中间件（含 #7 LLMErrorHandling 的重试）
看到的都是**已净化**的消息（提示词注入标签被转义）。否则重试时模型可能又吃到注入。

### Uploads(#3) → ThreadData(#4) → Sandbox(#5)

`SandboxMiddleware` 把沙箱目录挂进 agent 可见的虚拟路径，需要 `thread_data` 里**已经算好**的
`outputs_path`（`ToolOutputBudgetMiddleware` 外置大输出时写这里）。`UploadsMiddleware` 要在沙箱
挂载前算好上传目录。

> 注：`UploadsMiddleware` 实际从 `runtime.context` 取 `thread_id`、自己解析 `uploads_dir`
> （不读 `thread_data` state），故它与 `ThreadData` 无硬先后依赖——本链按**上游顺序**把 Uploads 排在
> ThreadData 之前（AGENTS.md #3/#4）。Sandbox 仍必须在两者之后（它读 `thread_data.outputs_path`）。

### SystemMessageCoalescing(#20) 在请求拍平前

严格后端（vLLM/SGLang/Qwen/Anthropic）要求 SystemMessage 只在对话开头、且通常只有一条。
DynamicContext(#11) 会注入额外的 SystemMessage 提醒，所以 #20 在 `wrap_model_call`（请求拍平前）
把它们合并成一条领头 SystemMessage。**只改请求、不动 checkpoint**。

### TokenBudget(#23) 在 Loop(#22) 后 / custom(#24) 前

预算硬停剥掉 tool_calls 后，custom / Safety(#25) 才看到清理过的消息，不会被截断参数误导。

### Clarification 永远最后（#26）

`ClarificationMiddleware` 用 `Command(goto=END)` **中断整次执行**。如果它不在最后，排在它后面的
中间件就跑不到（记忆没入队、标题没生成）。所以它必须是链尾（红线 #14）。

### 所有 `wrap_tool_call` / `wrap_model_call` 必须 `raise GraphBubbleUp`（红线 #15）

`GraphBubbleUp` 是 LangGraph 的控制流信号（interrupt / pause / resume / `Command(goto=...)`）。
`wrap_*` 里调 `handler(request)` 时，若 handler 抛 `GraphBubbleUp`，**必须原样 `raise`**，不能被
`except Exception` 吞掉——否则 Clarification 的中断、subagent 的 interrupt 全失效。

```python
def wrap_tool_call(self, request, handler):
    try:
        return handler(request)
    except GraphBubbleUp:   # ← 控制流信号，透传
        raise
    except Exception as exc:  # ← 普通异常，兜底
        return self._build_error_message(request, exc)
```

### Safety(#25) 在 Loop(#22) 之后注册

LangChain 的 `after_model` 按**倒序**列表分发——最后注册的最先观察模型输出。Safety 在 Loop 之后
注册 → Safety 先看原始响应 → 命中（content_filter）则清 tool_calls → Loop 再对清理后的消息计数，
不会因被滤掉的调用误触循环警。

## 5. config 驱动 gating（哪些步骤条件挂载）

不是 26 步恒定——按 config 开关 + 运行时上下文条件挂载：

| 步骤 | 挂载条件 |
|------|----------|
| 8 Guardrail | 真正可选，**mini 不做**（无 guardrails 模块） |
| 13 Summarization | `summarization.enabled` |
| 14 Todo | `config.configurable.is_plan_mode` |
| 15 TokenUsage | `token_usage.enabled` |
| 18 ViewImage | 当前模型 `supports_vision` |
| 19 DeferredToolFilter | `deferred_setup` 非空（tool_search 启用 + 有 MCP 工具） |
| 21 SubagentLimit | `config.configurable.subagent_enabled` |
| 22 LoopDetection | `loop_detection.enabled` |
| 23 TokenBudget | `token_budget.enabled`（**默认关**） |
| 25 SafetyFinishReason | `safety_finish_reason.enabled`（默认开） |

**总是挂载**的（#1 InputSanitization / #2 ToolOutputBudget / #3-6 / #9-12 / #16-17 / #20 / #26）：
核心骨架 + SkillActivation + Title + Memory + SystemMessageCoalescing + Clarification。
注意 Title/Memory 虽总在链里，但**内部**按 enabled 决定是否干活（对齐 deer）。

## 6. 逐文件分析——25 个中间件 + 工具文件每个做什么

> 每个文件一段：**做什么** / **为什么单独成文件** / **在链的哪一环**。文件名是
> [agents/middlewares/](../backend/packages/harness/deerflow/agents/middlewares/) 下的 `.py`。

### 共享前置段（lead + subagent）

**[input_sanitization_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/input_sanitization_middleware.py) — `InputSanitizationMiddleware`(#1)**
防提示词注入（#3630）：把最后一条**真实**用户消息里的系统保留标签（`<system>`/`<memory>`/`<think>` …）
HTML 转义（`<system>` → `&lt;system&gt;`），再包进纯文本边界标记。策略是「转义不拒绝」（像 AWS
Bedrock 的 PII ANONYMIZE）——保留用户原意、标签却失去语义。单独成文件因为它是独立的**安全防线**，
正则与边界串安全相关须逐字节可控。`wrap_model_call` 最外层，fail-open（净化失败放行原请求而非搞挂 run）。

**[tool_output_budget_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py) — `ToolOutputBudgetMiddleware`(#2)**
工具返回超大（如 `bash cat huge.log` 50 万字）会把上下文撑爆。超过 `externalize_min_chars`
（默认 12000）→ 完整输出**外置到磁盘**（`outputs/.tool-results/`），替换成精简预览（头 N 字 +
「全文存到 /mnt/user-data/outputs/xxx，用 read_file 按行读」+ 尾 N 字）。磁盘不可用回退首尾截断。
`read_file` 在 `exempt_tools` 里（防「外置→读→再外置」循环）。两条 hook：`wrap_tool_call`（实时
预算每个返回）+ `wrap_model_call`（扫历史 ToolMessage 补截断）。外置有 host-disk / 沙箱内直写两路径，
靠 `provider.uses_thread_data_mounts` 判分支。

**[uploads_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py) — `UploadsMiddleware`(#3，仅 lead)**
接 M23：`before_agent` 把这次上传的文件 + 历史未消化文件 + 文档大纲（PDF/PPT/Excel 经 markitdown
转换）注入对话。从 `runtime.context` 取 `thread_id`、自己解析 `uploads_dir`（不读 thread_data state）。
`abefore_agent` 把同步目录扫描卸到 executor 线程（红线 #1）。

**[thread_data_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/thread_data_middleware.py) — `ThreadDataMiddleware`(#4)**
算并（按 `lazy_init`）建每线程隔离目录 `(user_id, thread_id)` → `workspace/uploads/outputs`，写进
`state["thread_data"]`。Sandbox / ToolOutputBudget 都依赖这里写好的物理路径。`lazy_init=True`（默认）
只算路径不建目录（性能最优），`False` 在 `before_agent` 立即建。

**[sandbox/middleware.py](../backend/packages/harness/deerflow/sandbox/middleware.py) — `SandboxMiddleware`(#5)**
（文件在 sandbox 模块，不在 middlewares/ 目录）`before_agent` 调 `SandboxProvider.acquire(thread_id)`
拿到沙箱、把 `sandbox_id` 存进 state。须在 ThreadData 之后（用算好的路径挂虚拟挂载点）。

**[dangling_tool_call_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py) — `DanglingToolCallMiddleware`(#6)**
**dangling tool call**：某 AIMessage 带 tool_calls 但历史没对应 ToolMessage（用户中断 / 取消导致）。
OpenAI 系校验器要求每个 tool_call 紧跟响应，缺了就 400。本中间件用 `wrap_model_call` 扫历史，给悬空
调用插一条合成错误 ToolMessage（紧跟那条 AIMessage 后），保消息顺序合法。也处理 `invalid_tool_calls`
（malformed provider function call）。为何用 `wrap_model_call` 而非 `before_model`：`before_model` +
reducer 会把消息追加到末尾而非紧跟 AIMessage；`wrap_model_call` 能 `request.override(messages=...)`
重建正确顺序。**#3746（ID-swap 递归注入 + orphan peer 压缩）mini 已含**。

**[llm_error_handling_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py) — `LLMErrorHandlingMiddleware`(#7)**
包装模型调用。错误分类：quota / auth / transient（超时/限流/可重试状态码）/ busy / generic。瞬时错误
指数退避重试；重试耗尽或非瞬时错误 → 返回兜底 AIMessage（agent 优雅降级而非崩）。**熔断器**
（`CircuitBreakerConfig`）：连续失败达 `failure_threshold` → 短路返回兜底（半开探测恢复）。流断错误
（`StreamChunkTimeoutError`）给专门文案「拆小请求」而非「干等重试」。尊重 provider 的 `Retry-After`。
透传 `GraphBubbleUp`（红线 #15）。

**[sandbox_audit_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/sandbox_audit_middleware.py) — `SandboxAuditMiddleware`(#9)**
`wrap_tool_call` 审计沙箱内的 shell / file 操作：按命令模式分级（block / warn / pass），写审计日志。
安全可观测层——不改变工具结果，只记录。

**[tool_error_handling_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py) — `ToolErrorHandlingMiddleware`(#10) + 三个工厂**
`wrap_tool_call` 捕工具异常 → 错误 ToolMessage（run 不因单个工具失败中止）。同时给 `task` 工具返回贴
结构化子代理状态（`additional_kwargs.subagent_status`，#3146）。**这个文件还含三个工厂函数**：
`_build_runtime_middlewares` / `build_lead_runtime_middlewares` / `build_subagent_runtime_middlewares`——
把共享前置段（#1-10）集中装配，lead 与 subagent 复用。集中在这里因为装配顺序与 ToolErrorHandling 同属
「工具执行容错」关注点。

### lead-only 段

**[dynamic_context_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py) — `DynamicContextMiddleware`(#11) + `is_dynamic_context_reminder`**
把当前日期 + 记忆经 **ID-swap** 注入首条 HumanMessage：保留原消息 id、原地替换内容、紧随派生一条
`{id}__user` 消息。**保持系统提示完全静态** → 让 provider 的 prefix-cache 复用（省 token / 延迟）。
跨午夜时更新日期提醒。模块还导出 `is_dynamic_context_reminder`（认 HumanMessage **和** SystemMessage
的 `dynamic_context_reminder` 标记）——SystemMessageCoalescing(#20) 用它去重跨午夜的日期提醒。

**[skill_activation_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py) — `SkillActivationMiddleware`(#12)**
用户以 `/skill-name task` 开头时，加载对应已启用技能的 `SKILL.md` 注入当次模型调用（隐藏的当轮上下文）。
接 M14。

**[summarization_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py) — `_create_summarization_middleware`(#13，可选)**
继承 langchain `SummarizationMiddleware`，加两个 deer 能力：① **`before_summarization` 钩子**——摘要
删消息前派发 `SummarizationEvent`，让 [memory.md](memory.md) 的 `memory_flush_hook` 把对话**抢拍**进
记忆队列（否则被压缩的消息细节永久丢失）；② **技能文件抢救**——分区后保留最近 N 个「加载过技能文件」
的 read_file 调用。摘要 LLM 调用贴 `TAG_NOSTREAM`（防其 token 流被当幽灵 AI 消息广播给前端）。

**[todo_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py) — `TodoMiddleware`(#14，plan_mode)**
`is_plan_mode` 时挂载：提供 `write_todos` 工具 + 检测上下文丢失（删 / 改 todo 时同步）。系统提示与工具
描述在 [__init__.py](../backend/packages/harness/deerflow/agents/middlewares/__init__.py) 的 `_TODO_SYSTEM_PROMPT`
（教模型「复杂任务才用、一次一个 in_progress、做完立刻标 completed」）。

**[token_usage_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/token_usage_middleware.py) — `TokenUsageMiddleware`(#15，可选)**
记每步 token 用量、给每步贴动作归因。子代理的 token 会**回溯**合并进 dispatching AIMessage（按消息位置）——
TokenBudget(#23) 靠这个机制捕捉「事后增加」的 token。

**[title_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/title_middleware.py) — `TitleMiddleware`(#16)**
首轮完整交互后异步生成线程标题（内部按 `title.enabled`）。总在链里，enabled=False 时 `_should_generate`
返回 False。

**[memory_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py) — `MemoryMiddleware`(#17)**
`after_agent` 把对话（filter 到用户输入 + 最终 AI 回复）入记忆更新队列。接 M13。总在链里，内部按
`memory.enabled` 跳过。

**[view_image_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/view_image_middleware.py) — `ViewImageMiddleware`(#18，仅 vision)**
模型 `supports_vision` 时挂：`view_image` 工具完成后，把图片 base64 注入对话供模型看。

**[deferred_tool_filter_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/deferred_tool_filter_middleware.py) — `DeferredToolFilterMiddleware`(#19，可选)**
接 M15/M20 的 tool_search：延迟 MCP 工具未「提升」前不暴露 schema（按 `catalog_hash` scope，防陈旧
提升暴露改名工具）。提升状态从图状态读。

**[system_message_coalescing_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/system_message_coalescing_middleware.py) — `SystemMessageCoalescingMiddleware`(#20)**
#3711：把 `request.system_message` + `request.messages` 里**所有** SystemMessage 合并成**一条**领头的
SystemMessage，经 `system_message` 字段交回。严格后端（vLLM/SGLang/Qwen/Anthropic）拒收非领头
SystemMessage；DynamicContext(#11) 会注入额外 SystemMessage 提醒（跨午夜还会多条），故须合并。
跨午夜的 `dynamic_context_reminder` 去重——只保留最后一条（最新日期）。**只改请求、不动 checkpoint**。
无开关（provider 兼容修复，始终生效）。

**[subagent_limit_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py) — `SubagentLimitMiddleware`(#21，可选)**
接 M11：`after_model` 截断超额 `task` 调用，强制 `max_concurrent_subagents`（clamp [2,4]）。

**[loop_detection_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py) — `LoopDetectionMiddleware`(#22，可选)**
防 agent 用相同参数无限调工具直到 recursion limit。两层：① **哈希层**——哈希 tool_calls（name +
关键字段，**顺序无关**），滑动窗口里同一哈希 ≥ warn（默认 3）→ 队列警告；≥ hard（默认 5）→ 剥
tool_calls 强制文本答复。② **频率层**——同一工具**类型**（不限参数）调多次（read_file 读 40 个不同
文件）哈希层抓不到，用 `tool_freq_warn/hard_limit` + 每工具覆盖兜底。警告在 `wrap_model_call` 注入
而非 `after_model`（延迟注入套路，保 AIMessage→ToolMessage 配对）。警告瞬态，run 结束未排空就丢。
**#3709（位置回退 bug）mini 已含**。

**[token_budget_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/token_budget_middleware.py) — `TokenBudgetMiddleware`(#23，可选)**
单 run token 预算：`after_model` 按「已见消息」账本累计增量（`max(0, 现在 - 上次)`，顺带捕捉子代理
token 回灌），到 `warn_threshold` 软提醒、到 `hard_stop_threshold` 剥 tool_calls 强制收尾。提醒走
延迟注入（`after_model` 排队、`wrap_model_call` 追加到末尾）。账本按 `run_id` 分桶 + `BoundedDict`
（容量 1000 LRU）防遗弃 run 泄漏。`after_agent` 清账本。配置见
[token_budget_config.py](../backend/packages/harness/deerflow/config/token_budget_config.py)。

**custom_middlewares(#24)** —— 调用方传入的任意 `AgentMiddleware`，插在 Clarification 前。

**[safety_finish_reason_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/safety_finish_reason_middleware.py) — `SafetyFinishReasonMiddleware`(#25，可选)**
provider（OpenAI `content_filter` / Anthropic `refusal` / Gemini `SAFETY`）会在流中途停生成但**仍返回
半成形 tool_calls**。LangChain 把带 tool_calls 的 AIMessage 当「去执行」，截断参数被当完整派发 →
agent 看截断结果 → 修 → 又被滤 → 死循环。本中间件 `after_model` 门控：检测器命中**且**带 tool_calls →
剥掉 tool_calls、追加用户说明、塞 `additional_kwargs.safety_termination`。仅在有 tool_calls 时介入。

**[clarification_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py) — `ClarificationMiddleware`(#26)**
拦截 `ask_clarification` 工具调用 → 格式化问题 + 选项 + 图标 → `Command(goto=END)` 中断执行等用户
回复。确定性 message id（`clarification:{tool_call_id}`）让**重试的澄清替换而非追加**。永远末位（红线 #14）。

### 工具 / 辅助文件（不在链上，但属本模块）

**[safety_termination_detectors.py](../backend/packages/harness/deerflow/agents/middlewares/safety_termination_detectors.py)**
SafetyFinishReason(#25) 的**检测器策略接口** + 三个内置（OpenAI `content_filter` / Anthropic `refusal`
/ Gemini `SAFETY`）。新 provider 实现接口经 config 接入。单独成文件因为「什么算安全终止」是可插拔策略，
与中间件本体（剥 tool_calls 的机制）解耦。

**[tool_call_metadata.py](../backend/packages/harness/deerflow/agents/middlewares/tool_call_metadata.py)**
`clone_ai_message_with_tool_calls` 等工具：克隆 AIMessage 时同步 raw provider payload
（`additional_kwargs["tool_calls"]`）与结构化 `tool_calls`，防两者漂移。供 DanglingToolCall /
SafetyFinishReason 等用。

**[__init__.py](../backend/packages/harness/deerflow/agents/middlewares/__init__.py)**
`build_middlewares(config, ...)`——lead agent 的 26 步链装配入口（步骤 11-26）；还含 `_TODO_SYSTEM_PROMPT`
/ `_TODO_TOOL_DESCRIPTION`（TodoMiddleware 的提示词）。由 `make_lead_agent`（M17）调用。

## 7. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **config/paths** | ThreadData 的 workspace/uploads/outputs 路径**唯一真相源** |
| **config/token_budget_config** | TokenBudget(#23) 的配置（阈值校验） |
| **sandbox** | SandboxMiddleware（#5）+ SandboxAuditMiddleware（#9）；ToolOutputBudget 外置写 outputs |
| **uploads(M23)** | UploadsMiddleware（#3）调 `list_files_in_dir`/`extract_outline` 注入清单 |
| **memory(M13)** | MemoryMiddleware（#17）入队；Summarization（#13）的 `memory_flush_hook` 抢拍 |
| **skills(M14)** | SkillActivationMiddleware（#12）激活；Summarization 抢救技能文件 |
| **subagents(M11)** | SubagentLimitMiddleware（#21）截断 task 调用；ToolErrorHandling 贴 subagent_status；TokenBudget 捕捉子代理 token 回灌 |
| **tools(M15)** | DeferredToolFilter（#19）接 tool_search；ViewImage（#18）接 view_image；TokenUsage 合并子代理 token |
| **models** | SystemMessageCoalescing(#20) provider 无关地修严格后端的 SystemMessage 顺序问题 |
| **M17 lead_agent** | `make_lead_agent` 调 `build_middlewares` 组装链 + tracing 图根注入 |

## 8. 设计要点回顾

1. **顺序是契约**：InputSanitization 最外层；Uploads→ThreadData→Sandbox（对齐上游）；SystemMessageCoalescing 在请求拍平前；TokenBudget 在 Loop 后；Clarification 永远末位；Safety 在 Loop 后。
2. **红线 #15**：所有 `wrap_*` 透传 `GraphBubbleUp`，控制流信号不被吞。
3. **config 驱动 gating**：10 步条件挂载，16 步恒定骨架。
4. **洋葱 vs 前后**：`wrap_*` 能改入参/出参/重试（倒序包裹）；`before_*/after_*` 只返回状态更新（正序）。
5. **延迟注入套路**：LoopDetection(#22) / TokenBudget(#23) 在 `after_model` 检测、`wrap_model_call` 注入——保 AIMessage→ToolMessage 配对不被破。
6. **阻塞 IO 卸线程**：`wrap_tool_call` 的沙箱 IO、`Uploads.abefore_agent` 的目录扫描、`DynamicContext.abefore_agent` 的 tiktoken 走 `to_thread`/`run_in_executor`（红线 #1）。
7. **熔断 + 分类重试**：LLMErrorHandling 按错误类型给不同策略，熔断防放大噪声。
8. **防注入 + 防爆 + 防循环 + 防超支**：四道独立安全防线——InputSanitization(#1) / ToolOutputBudget(#2) / LoopDetection(#22) / TokenBudget(#23)。
9. **只改请求、不动 checkpoint**：InputSanitization(#1) / SystemMessageCoalescing(#20) / DanglingToolCall(#6) 都只在 `wrap_model_call` 改出站请求，持久状态（checkpoint）原样——靠标记扫描历史的中中间件继续正常。
10. **lead/subagent 共享前置段**：`build_lead_runtime_middlewares` 复用 #1-10，subagent 不含 uploads/lead-only 段。

## 9. 排错 FAQ

- **「工具调用后 agent 不继续 / 报 tool_call_ids did not have response messages」**：检查消息历史有无 dangling tool call（DanglingToolCall 应已补占位）；或某中间件在 `after_model` 插了消息破配对（正确做法是排队到 wrap_model_call）。
- **「用户的 `<system>` 标签被模型当指令执行」**：确认 InputSanitization(#1) 在链上（默认在）；它转义系统保留标签，普通 HTML 不动。想看净化效果可查日志 `InputSanitizationMiddleware: original=... -> processed=...`。
- **「vLLM/SGLang/Qwen 报 System message must be at the beginning」**：SystemMessageCoalescing(#20) 应已合并多条 SystemMessage；若仍报，检查是否有 provider 在中间件之外又注入了 SystemMessage。
- **「agent 一个 run 烧了太多 token」**：开 `token_budget.enabled`，设 `max_tokens` + `warn_threshold`/`hard_stop_threshold`；硬停会剥 tool_calls 逼模型收尾。
- **「LLM 一直超时重试」**：StreamChunkTimeoutError 只重试 1 次；若持续，看熔断器是否打开（`circuit_breaker`）。
- **「agent 死循环调同一工具」**：调低 `loop_detection.warn/hard_limit`；频率层 `tool_freq_*` 抓同类型不同参数。
- **「task 子代理调用太多」**：`SubagentLimitMiddleware` clamp 到 [2,4]，超出在 `configurable.max_concurrent_subagents` 调。
- **「MCP 工具 schema 没暴露」**：tool_search 未启用 / 未提升；DeferredToolFilter 据此隐藏，调 tool_search 提升。
- **「provider 返回 content_filter 但 agent 还在执行工具」**：SafetyFinishReasonMiddleware 是否启用（默认开）；检测器（safety_termination_detectors）是否覆盖该 provider。

---

**下一篇**：[agents.md](agents.md)（M17 lead_agent factory + RuntimeFeatures + thread_state 类型化
reducer + custom-agent 分支）——本模块的 `build_middlewares` 由 `make_lead_agent` 调用组装成最终 agent。
