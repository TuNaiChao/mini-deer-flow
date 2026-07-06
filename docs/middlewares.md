# 24. middlewares.md — 中间件链（agent 的行为骨架，26 槽位实装 25）

> **一句话定位**：本模块给 agent 装「行为骨架」——把 25 个中间件按**严格顺序**串成一条链，在模型调用前后、工具调用前后、每轮 agent 开始结束做各种横切处理（防上下文爆炸、防死循环、防提示词注入、注入上下文、安全拦截、错误兜底……）。**顺序是契约**：换顺序会破坏功能甚至安全。

**学完能回答（learning outcomes）**：

1. 什么是「中间件 / 洋葱模型 / 钩子」，为什么 agent 要用这套模式而不是一个大函数；
2. mini 的 26 个编号位置各自干什么、哪些恒在链里、哪些按 config 条件挂载；
3. 为什么 InputSanitization 必须最外层、Clarification 必须最末位、TokenBudget 必须在 LoopDetection 之后——讲得出「换掉会怎样」；
4. `wrap_*`（包裹钩子，能改请求/重试/吞异常）与 `before_*/after_*`（前后钩子，只返回状态更新）的区别与各自适用场景；
5. 「延迟注入」套路——为什么循环/超预算检测在 `after_model` 发现问题却要到下一轮 `wrap_model_call` 才注入警告；
6. `GraphBubbleUp` 是什么、为什么所有 `wrap_*` 必须原样上抛它；
7. 能在面试里讲清「mini 的中间件链与上游 deer-flow 源码的差异在哪、为什么」（见 §10）。

读完 [uploads.md](uploads.md)（懂了「上传文件怎么变 agent 上下文」）再看本篇最省事——本篇是**所有横切行为的总装配**。`UploadsMiddleware` 是链的第 3 步，本篇解释它在整条链里的位置、以及其它每一步各自干什么、为什么是这个顺序。它把 Phase 2–5.5 的各模块（sandbox / subagents / skills / memory / mcp / tools / uploads）串成一个能跑的 agent。

---

## 1. 名词（先懂这些再往下看）

### 1.1 计算机基础层（每个名词第一次出现就解释）

- **中间件（middleware）**：一种「夹在请求处理流程中间」的组件。想象一条流水线，原料（用户消息）从一头进，经过一道道工序（每道工序就是一个中间件），最后产品（agent 回复）从另一头出。每道工序只管自己那件事（净化输入 / 算预算 / 记日志……），互相不关心。本模块每个 `.py` 文件基本就是一个中间件。
- **钩子（hook）**：中间件能「挂钩」的时机点。比如「模型调用前」「工具调用后」就是两个钩子。中间件按需实现感兴趣的钩子，框架会在跑到那个时机时自动调它。**前后钩子**（`before_*` / `after_*`）像队列，按顺序逐个跑；**包裹钩子**（`wrap_*`）像洋葱，一层包一层。
- **洋葱模型（onion）**：包裹钩子的执行方式。最外层先「进」，一直进到最内层真正执行（比如调模型），再一层层「出」。每一层都能在「进」时改请求、在「出」时改响应、甚至吞掉异常重试。注册顺序最靠后的中间件 = 最内层；最靠前的 = 最外层。
- **装饰器（decorator）**：Python 里 `@something` 语法，本质是「接收一个函数、返回一个新函数」的语法糖。包裹钩子的原理和装饰器一样——把「真正的调用」包一层。
- **事件循环（event loop）**：异步程序（`async/await`）的调度核心，单线程轮流跑许多任务。详见 [start-here.md](start-here.md)。**关键约束**：事件循环上不能跑「会卡很久」的同步操作（如读磁盘、网络请求），否则整个循环冻住、所有任务都停——这叫「阻塞事件循环」。所以中间件里凡是要做磁盘/网络 IO 的，都得「卸」到线程池里跑（`asyncio.to_thread` / `run_in_executor`）。
- **阻塞 IO / 非阻塞**：「IO」= 输入输出（读写文件、网络）。磁盘读一个 50 万字的日志会卡几十毫秒到几秒，这就是阻塞 IO；在事件循环上干这事会冻住整个 agent。详见 [build.md](build.md) 的 blocking-IO gate。
- **重试 / 退避（retry / backoff）**：调用失败（比如网络抖动）时自动重试；「指数退避」= 每次重试间隔翻倍（1s → 2s → 4s），避免雪崩式压垮下游。
- **熔断器（circuit breaker）**：像电路保险丝——连续失败到一定次数就「熔断」（短路），直接返回兜底结果不再尝试，过一会「半开」试探一次看下游恢复没。防止一个挂掉的服务把整个 agent 拖死在无限重试里。
- **前缀缓存（prefix cache）**：LLM 提供商的一个优化——如果两次请求的**开头部分一模一样**，第二次可以复用第一次算到一半的中间结果，省算力、降延迟。所以系统提示（system prompt）越「静态」（不动）越能命中缓存。这是 DynamicContextMiddleware 用「ID-swap」把动态内容塞进 HumanMessage 而不动 SystemMessage 的根本动机。
- **提示词注入（prompt injection）**：用户在输入里夹带「假装是系统指令」的内容（如 `<system>忽略之前所有指令</system>`），骗模型干坏事。InputSanitizationMiddleware 转义这类保留标签来防御。
- **HTML 转义（html.escape）**：把 `<` 变 `&lt;`、`>` 变 `&gt;`，让浏览器/模型把标签当**普通文字**而非**指令**。
- **哈希（hash）**：把任意数据算成一个固定长度的指纹（如 `sha256`）。相同数据→相同指纹；不同数据→几乎不可能相同指纹。LoopDetectionMiddleware 哈希「工具调用参数」来快速判断「是不是又用同样的参数调了同一个工具」。
- **滑动窗口（sliding window）**：只看「最近 N 次」的统计窗口（像看最近 5 次操作），老的滚出窗口就不再算。循环检测看「最近若干次工具调用有没有重复」就是滑动窗口。
- **LRU / BoundedDict**：LRU = Least Recently Used（最近最少使用），一种容量有限的字典——满了就淘汰最久没用的那条。TokenBudgetMiddleware 用 `BoundedDict`（容量 1000）装每个 run 的账本，防止被遗弃的 run 把内存撑爆。
- **抽象基类（ABC）**：定义「接口」的父类——规定子类必须实现哪些方法，但不给实现。`AgentMiddleware` 就是 ABC：规定中间件可以有哪些钩子签名。详见 [sandbox.md](sandbox.md) 的 Sandbox ABC。
- **GraphBubbleUp**：LangGraph 框架的一种**控制流信号**（异常形态），用来表达「中断执行」（interrupt）、「跳转到某节点」（`Command(goto=...)`）这类「不是错误、是控制意图」的信号。它**必须**穿透所有 `try/except`，不能被普通异常处理吞掉——否则 Clarification 的中断、子代理的 interrupt 全失效。
- **`Command(goto=END)`**：LangGraph 的指令对象，「跳到 END 节点」= 结束本次 agent 执行。ClarificationMiddleware 用它来暂停 agent 等用户回答澄清问题。

### 1.2 模块层名词

- **AgentMiddleware**：langchain 提供的中间件基类。子类按需实现钩子（`before_agent` / `after_model` / `wrap_model_call` / `wrap_tool_call` …），框架在 agent 运行的相应时机调用。
- **前后钩子 vs 包裹钩子**：
  | | 前后钩子 `before_*/after_*` | 包裹钩子 `wrap_*` |
  |---|---|---|
  | 能力 | 只能返回状态更新（dict），追加/改 state | 能改请求、改响应、重试、吞异常 |
  | 执行 | 按**链正序**逐个跑（队列） | **洋葱**，按链**倒序**包裹（最后注册=最内层） |
  | 典型 | before_agent 建目录、after_model 记 token | wrap_model_call 净化输入、wrap_tool_call 兜底异常 |
- **build_middlewares / build_lead_runtime_middlewares**：本模块的装配入口函数。前者装 lead agent 的完整链（26 槽位），后者装 lead 与 subagent 共享的前 10 步。详见 §5.1。
- **延迟注入（deferred injection）**：本模块一个反复出现的套路——在 `after_model` 检测到问题（循环 / 超预算）时**不当场插消息**（会破坏 `AIMessage(tool_calls) → ToolMessage` 的配对），而是把警告**排队**，到**下一轮**的 `wrap_model_call` 把它追加到请求末尾。LoopDetection、TokenBudget 共用这套。

---

## 2. 这个模块解决什么问题

agent 一次「思考-行动」要经过很多阶段：用户消息进来 → 净化输入 → 建隔离目录 → 读上传文件 → 分配沙箱 → 调模型 → 处理工具调用 → 检测循环 → 算 token 预算 → 生成标题 → 入记忆队列……如果把它们全堆进主 agent 函数，会得到一个几千行、谁都不敢改的巨型函数。

**中间件模式**把每个横切关注点写成独立类（中间件），每个类实现几个钩子。框架按顺序把所有中间件串起来，agent 跑到某阶段时自动调对应钩子。`build_middlewares()` 就是这条链的**装配清单**。

---

## 3. 结构（装配关系 + 依赖图）

链分**两段**装配，三个工厂函数：

```
                    ┌─────────────────────────────────────────────┐
   lead agent       │  build_middlewares()            [__init__.py]│
   (make_lead_agent │  ├─ build_lead_runtime_middlewares()  ───────┼─→ 共享段 #1-#10
   调用)            │  │      └─ _build_runtime_middlewares()       │   (tool_error_handling_middleware.py)
                    │  └─ 追加 lead-only #11-#26                    │
                    └─────────────────────────────────────────────┘

   subagent         ┌─────────────────────────────────────────────┐
   (build_subagent  │  build_subagent_runtime_middlewares()        │
   _middlewares)    │  └─ _build_runtime_middlewares()  ─ #1-#10 ─┘
                    │      + vision / deferred / safety 专属        │
                    └─────────────────────────────────────────────┘
```

装配文件依赖关系（谁调谁）：

```
agents/lead_agent/agent.py (make_lead_agent)
        │
        ▼
agents/middlewares/__init__.py :: build_middlewares()       ← lead 完整链入口
        │
        ├──► tool_error_handling_middleware.py
        │       :: _build_runtime_middlewares()              ← 共享 #1-#10
        │       :: build_lead_runtime_middlewares()
        │       :: build_subagent_runtime_middlewares()
        │       :: ToolErrorHandlingMiddleware  (#10)
        │
        ├──► input_sanitization_middleware.py    (#1)
        ├──► tool_output_budget_middleware.py    (#2)
        ├──► uploads_middleware.py               (#3, 仅 lead)
        ├──► thread_data_middleware.py           (#4)
        ├──► sandbox/middleware.py               (#5, 在 sandbox 模块)
        ├──► dangling_tool_call_middleware.py    (#6)
        ├──► llm_error_handling_middleware.py    (#7)
        ├──  [跳过 #8 Guardrail]
        ├──► sandbox_audit_middleware.py         (#9)
        ├──► dynamic_context_middleware.py       (#11)
        ├──► skill_activation_middleware.py      (#12)
        ├──► summarization_middleware.py         (#13, 可选)
        ├──► todo_middleware.py                  (#14, plan_mode)
        ├──► token_usage_middleware.py           (#15, 可选)
        ├──► title_middleware.py                 (#16)
        ├──► memory_middleware.py                (#17)
        ├──► view_image_middleware.py            (#18, 仅 vision)
        ├──► deferred_tool_filter_middleware.py  (#19, 可选)
        ├──► system_message_coalescing_middleware.py (#20)
        ├──► subagent_limit_middleware.py        (#21, 可选)
        ├──► loop_detection_middleware.py        (#22, 可选)
        ├──► token_budget_middleware.py          (#23, 可选)
        ├──► [custom_middlewares]                (#24)
        ├──► safety_finish_reason_middleware.py  (#25, 可选)
        └──► clarification_middleware.py         (#26, 永远末位)

辅助文件（不在链上，供多个中间件复用）：
  safety_termination_detectors.py   ← #25 的「什么算安全终止」策略接口 + 内置实现
  tool_call_metadata.py             ← 克隆 AIMessage 时同步 raw/结构化 tool_calls
```

---

## 4. 26 槽位顺序契约

`build_middlewares`（[__init__.py:151](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L151)）按下表顺序装配。**顺序里的硬约束用 ⚠️ 标注**——换掉会破坏功能或安全：

```
=== 共享前置段 _build_runtime_middlewares（lead + subagent 都要）===
 1. InputSanitizationMiddleware      ⚠️ 最外层 wrap_model_call：提示词注入标签转义
 2. ToolOutputBudgetMiddleware       ← 防爆：单个工具返回太大 → 外置磁盘 + 预览
 3. UploadsMiddleware                ⚠️ 仅 lead：注入上传清单
 4. ThreadDataMiddleware             ⚠️ 算/建每线程隔离目录（workspace/uploads/outputs）
 5. SandboxMiddleware                ⚠️ 须在 ThreadData 之后（依赖 thread_data 路径）
 6. DanglingToolCallMiddleware       ← 补悬空工具调用的占位响应（防 provider 400）
 7. LLMErrorHandlingMiddleware       ← 重试/退避/熔断 + 用户可读兜底
 8. [GuardrailMiddleware]            ← mini 不实装（依赖未引入的 guardrails 独立模块）
 9. SandboxAuditMiddleware           ← 沙箱命令审计（block/warn/pass 分级）
10. ToolErrorHandlingMiddleware      ← 工具异常 → 错误 ToolMessage（run 不中断）

=== lead-only 段 build_middlewares（仅 lead agent）===
11. DynamicContextMiddleware         ← ID-swap 注入日期/记忆到首条 HumanMessage
12. SkillActivationMiddleware        ← /skill-name 激活，注入 SKILL.md
13. SummarizationMiddleware          ← 可选：上下文近 token 上限时压缩（含记忆抢拍钩子）
14. TodoMiddleware                   ← plan_mode 时挂载（write_todos 工具）
15. TokenUsageMiddleware             ← 可选：记 token 用量 + 给每步贴动作归因
16. TitleMiddleware                  ← 首轮后生成线程标题
17. MemoryMiddleware                 ← 入记忆更新队列（filter→抽取→更新）
18. ViewImageMiddleware              ← 仅 supports_vision：view_image 完成后注入 base64 图片
19. DeferredToolFilterMiddleware     ← 可选：延迟 MCP 工具未提升前不暴露 schema
20. SystemMessageCoalescingMiddleware ⚠️ 合并所有 SystemMessage 成一条领头（严格后端兼容）
21. SubagentLimitMiddleware          ← 可选：截断超额 task 调用（clamp [2,4]）
22. LoopDetectionMiddleware          ← 可选：哈希 + 频率双层循环检测
23. TokenBudgetMiddleware            ← 可选：单 run token 预算（软提醒 + 硬停剥 tool_calls）
24. custom_middlewares               ← 调用方自定义（插在 Clarification 前）
25. SafetyFinishReasonMiddleware     ← 可选：provider 安全终止时剥 tool_calls
26. ClarificationMiddleware          ⚠️ 永远最后：拦截 ask_clarification 中断执行
```

subagent 用 `build_subagent_runtime_middlewares`（共享 #1-10 + vision/deferred/safety），不含 lead-only 段。

> **关于「几个中间件」**：链共 **26 个编号位置**（与上游 AGENTS.md 编号一致）；其中 `agents/middlewares/` 目录下有 **23 个中间件类文件**（另含 2 个辅助文件 `safety_termination_detectors.py` / `tool_call_metadata.py` + `__init__.py` 装配入口）+ `sandbox/middleware.py` 里的 `SandboxMiddleware`（#5）+ 调用方传入的 `custom_middlewares` 槽位（#24）。mini **跳过 #8 Guardrail**，其余 25 个位置全部实装。

---

## 5. 代码走读

### 5.1 装配入口 `build_middlewares`

[__init__.py:151](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L151) 是 lead agent 的完整链入口。它的结构很清晰：

1. 先调 `build_lead_runtime_middlewares`（[__init__.py:193](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L193)）拿到共享段 #1-#10；
2. 逐个 `append` lead-only 的 #11-#26，**条件中间件**用 `if` 判断是否 append；
3. `ClarificationMiddleware` 无条件最后一个 append（[__init__.py:286](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L286)）。

每个条件中间件都是「读 config / 运行时上下文 → 满足才 append」的标准模式，例如步骤 18（仅 vision 模型）：

```python
# __init__.py:228-233
model_config = resolved_app_config.get_model_config(model_name) if model_name else None
if model_config is not None and model_config.supports_vision:
    from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware
    middlewares.append(ViewImageMiddleware())
```

> **为什么延迟 import**：所有中间件类都在函数体内 `from ... import`（而非文件顶部），是为了**避免循环导入** + 保持模块轻量——只有真正要用某个中间件时才加载它的依赖。

文件里还有两段长常量 `_TODO_SYSTEM_PROMPT` / `_TODO_TOOL_DESCRIPTION`（[__init__.py:51](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L51)、[__init__.py:87](../backend/packages/harness/deerflow/agents/middlewares/__init__.py#L87)），是 TodoMiddleware（#14）教模型「复杂任务才用 write_todos、一次只一个 in_progress、做完立刻标 completed」的提示词。

### 5.2 共享段 `_build_runtime_middlewares`

[tool_error_handling_middleware.py:128](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py#L128) 装 lead 与 subagent 都要的前 10 步。它用两个布尔参数控制差异：

- `include_uploads`：lead=True（加 #3），subagent=False（不加）；
- `include_dangling_tool_call_patch`：两者都 True。

装配顺序（[tool_error_handling_middleware.py:154-179](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py#L154-L179)）：`[InputSanitization, ToolOutputBudget]` →（lead 加 `Uploads`）→ `ThreadData` → `Sandbox` →（加 `DanglingToolCall`）→ `LLMErrorHandling` → **跳过 Guardrail**（注释见 [tool_error_handling_middleware.py:173](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py#L173)）→ `SandboxAudit` → `ToolErrorHandling`。

同文件里 `ToolErrorHandlingMiddleware`（#10）本身就是这个文件的主类——把「#10 的实现」与「#1-#10 的装配」放一起，是因为它们都属于「工具执行容错」这一关注点。

### 5.3 关键中间件详解（挑 8 个最能体现设计动机的）

#### #1 InputSanitizationMiddleware — 防提示词注入，必须最外层

[input_sanitization_middleware.py:299](../backend/packages/harness/deerflow/agents/middlewares/input_sanitization_middleware.py#L299) 的 `wrap_model_call`：把最后一条**真实**用户消息里的系统保留标签（`<system>`/`<memory>`/`<think>` …）HTML 转义（`<system>` → `&lt;system&gt;`），再包进纯文本边界标记。

策略是「转义不拒绝」（像 AWS Bedrock 的 PII ANONYMIZE）——保留用户原意、标签却失去语义。**fail-open**：净化失败时放行原请求而非搞挂整个 run（安全防线宁可弱化也不能让 agent 停摆）。

**为什么必须最外层**：它排第一 → 是 `wrap_model_call` 洋葱的**最外层**，所有内层（含 #7 LLMErrorHandling 的重试）看到的都是**已净化**的消息。否则 #7 重试时模型可能又吃到注入。

#### #2 ToolOutputBudgetMiddleware — 防上下文爆炸

工具返回超大（如 `bash cat huge.log` 50 万字）会把上下文撑爆。超过 `externalize_min_chars`（默认 12000）→ 完整输出**外置到磁盘**（`outputs/.tool-results/`，见 [tool_output_budget_middleware.py:109](../backend/packages/harness/deerflow/agents/middlewares/tool_output_budget_middleware.py#L109) 的 `_externalize`），替换成精简预览（头 N 字 +「全文存到 /mnt/user-data/outputs/xxx，用 read_file 按行读」+ 尾 N 字）。磁盘不可用时回退首尾截断。

`read_file` 在 `exempt_tools` 里——防「外置 → 读回 → 再外置」的循环。两条 hook：`wrap_tool_call`（实时预算每个返回）+ `wrap_model_call`（扫历史 ToolMessage 补截断）。外置有 host-disk / 沙箱内直写两路径，靠 provider 的 `uses_thread_data_mounts` 判分支（沙箱模式见 [aio_sandbox.md](aio_sandbox.md)）。

#### #6 DanglingToolCallMiddleware — 补悬空调用，保消息合法

**dangling tool call**：某 AIMessage 带 tool_calls 但历史没对应 ToolMessage（用户中断 / 取消导致）。OpenAI 系校验器要求每个 tool_call 紧跟响应，缺了就返回 400。

[dangling_tool_call_middleware.py:176](../backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py#L176) 的 `wrap_model_call` 扫历史，给悬空调用插一条合成错误 ToolMessage（紧跟那条 AIMessage 后）。也处理 `invalid_tool_calls`（malformed provider function call，[dangling_tool_call_middleware.py:83](../backend/packages/harness/deerflow/agents/middlewares/dangling_tool_call_middleware.py#L83)）。

**为什么用 `wrap_model_call` 而非 `before_model`**：`before_model` + reducer 会把消息追加到**末尾**而非紧跟 AIMessage；`wrap_model_call` 能 `request.override(messages=...)` 重建正确顺序。

#### #7 LLMErrorHandlingMiddleware — 分类重试 + 熔断

[llm_error_handling_middleware.py:161](../backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py#L161) 的 `_classify_error` 把错误分成 quota / auth / transient（超时/限流/可重试状态码）/ busy / generic 几类。瞬时错误指数退避重试；重试耗尽或非瞬时错误 → 返回兜底 AIMessage（agent 优雅降级而非崩）。

**熔断器**：连续失败达 `failure_threshold` → 短路返回兜底（[llm_error_handling_middleware.py:270](../backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py#L270) 的 `CircuitBreakerOpen`），半开探测恢复。流断错误（`StreamChunkTimeoutError`）给专门文案「拆小请求」而非「干等重试」。尊重 provider 的 `Retry-After` 头。

**透传 `GraphBubbleUp`**（[llm_error_handling_middleware.py:281](../backend/packages/harness/deerflow/agents/middlewares/llm_error_handling_middleware.py#L281)）：`except GraphBubbleUp: raise` 必须在 `except Exception` 之前，否则控制流信号被吞。

#### #22 LoopDetectionMiddleware — 双层循环检测 + 延迟注入

防 agent 用相同参数无限调工具直到 recursion limit。两层：

1. **哈希层**：`_hash_tool_calls`（[loop_detection_middleware.py:120](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py#L120)）哈希 tool_calls（name + 关键字段，**顺序无关**），滑动窗口里同一哈希 ≥ warn（默认 3）→ 队列警告；≥ hard（默认 5）→ 剥 tool_calls 强制文本答复。
2. **频率层**：同一工具**类型**（不限参数）调多次（如 read_file 读 40 个不同文件）哈希层抓不到，用 `tool_freq_warn/hard_limit`（[loop_detection_middleware.py:157](../backend/packages/harness/deerflow/agents/middlewares/loop_detection_middleware.py#L157)）+ 每工具覆盖兜底。

警告在 `wrap_model_call` 注入而非 `after_model`——延迟注入套路，保 `AIMessage→ToolMessage` 配对不被破坏。

#### #23 TokenBudgetMiddleware — 单 run 预算 + 已见账本

[token_budget_middleware.py:81](../backend/packages/harness/deerflow/agents/middlewares/token_budget_middleware.py#L81) 的 `BoundedDict`（容量 1000 LRU）装三个账本（`_seen_messages` / `_pending_warnings` / `_warned`，[token_budget_middleware.py:104-106](../backend/packages/harness/deerflow/agents/middlewares/token_budget_middleware.py#L104-L106)），按 `run_id` 分桶，防遗弃 run 泄漏。

`after_model` 按「已见消息」账本累计增量（`max(0, 现在 - 上次)`，顺带捕捉子代理 token 回灌），到 `warn_threshold` 软提醒、到 `hard_stop_threshold` 剥 tool_calls 强制收尾。提醒走延迟注入（`after_model` 排队、`wrap_model_call` 追加到末尾）。`after_agent` 清账本。

**为什么用「已见」账本而不是累加每轮 token**：同一批历史消息每轮都会被模型重新读一遍，直接累加会重复计；「已见」只算新增的增量，准确。配置见 [token_budget_config.py](../backend/packages/harness/deerflow/config/token_budget_config.py)。

#### #25 SafetyFinishReasonMiddleware — 安全终止拦截

provider（OpenAI `content_filter` / Anthropic `refusal` / Gemini `SAFETY`）会在流中途停生成但**仍返回半成形 tool_calls**。LangChain 把带 tool_calls 的 AIMessage 当「去执行」，截断参数被当完整派发 → agent 看截断结果 → 修 → 又被滤 → 死循环。

[safety_finish_reason_middleware.py:55](../backend/packages/harness/deerflow/agents/middlewares/safety_finish_reason_middleware.py#L55) 的 `__init__` 接收检测器列表（默认 `default_detectors()`，[safety_finish_reason_middleware.py:58](../backend/packages/harness/deerflow/agents/middlewares/safety_finish_reason_middleware.py#L58)）。`after_model` 门控：检测器命中**且**带 tool_calls → 剥掉 tool_calls、追加用户说明、塞 `additional_kwargs.safety_termination`。仅在有 tool_calls 时介入。

**为什么注册在 LoopDetection 之后**：LangChain 的 `after_model` 按**倒序列表**序分发——最后注册的最先观察模型输出。Safety 在 Loop 之后注册 → Safety 先看原始响应 → 命中则清 tool_calls → Loop 再对清理后的消息计数，不误触循环警。

#### #26 ClarificationMiddleware — 永远末位的中断器

[clarification_middleware.py:28](../backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py#L28) import `Command`；拦截 `ask_clarification` 工具调用 → 格式化问题 + 选项 + 图标 → `Command(goto=END)` 中断执行等用户回复。确定性 message id（`clarification:{tool_call_id}`，[clarification_middleware.py:51](../backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py#L51)）让**重试的澄清替换而非追加**。

**为什么必须最末位**：它用 `Command(goto=END)` **中断整次执行**。如果它不在最后，排在后面的中间件就跑不到（记忆没入队、标题没生成）。所以它是链尾的硬约束。

---

## 6. 一轮 agent 的数据流（钩子何时触发）

下面是一次「用户发消息 → agent 响应」里，链上各钩子何时触发。**横向是时间**；`wrap_*` 用洋葱表示，`before_*/after_*` 用顺序队列表示：

```
用户消息进入
   │
   ▼  ┌─ before_agent（正序：链头→链尾）─────────────────────────┐
      │  #3  Uploads 扫上传目录、注入文件清单                       │
      │  #4  ThreadData 建 (user_id,thread_id) 隔离目录            │
      │  #11 DynamicContext ID-swap 注日期/记忆                    │
      │  #23 TokenBudget 标记历史消息「已见」（不计入本轮）         │
      │  #22 LoopDetection 清上一轮残留警告                         │
      └──────────────────────────────────────────────────────────┘
   │
   ▼  ┌─ wrap_model_call（洋葱，倒序包裹：#26 最内 → #1 最外）──┐
      │  #1  InputSanitization   净化最后一条真实用户消息          ┐
      │  #20 SystemMessageCoalescing 合并 SystemMessage            │ 每层可改
      │  #22 LoopDetection       追加队列里的循环警告              │ request 并
      │  #23 TokenBudget         追加队列里的预算提醒              │ 交给下一层
      │  #7  LLMErrorHandling    重试 / 退避                       │
      │  #6  DanglingToolCall    补悬空调用的占位 ToolMessage      ┘
      │                          ↓ 最终 request → 调 LLM ← AIMessage
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
      │                          ↓ 执行工具 ← ToolMessage
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

**关键**：`after_model` 里检测到问题（循环 / 超预算）时，**不能当场插消息**（会破 `AIMessage(tool_calls) → ToolMessage` 配对），而是**排队**，由下一轮 `wrap_model_call` 把警告追加到请求**末尾**——这是 LoopDetection(#22) 与 TokenBudget(#23) 共用的延迟注入套路。

---

## 7. 配置（哪些步骤条件挂载）

不是 26 步恒定——按 config 开关 + 运行时上下文条件挂载：

| 步骤 | 挂载条件 |
|------|----------|
| 8 Guardrail | mini **不实装**（无 guardrails 模块） |
| 13 Summarization | `summarization.enabled` |
| 14 Todo | `config.configurable.is_plan_mode` |
| 15 TokenUsage | `token_usage.enabled` |
| 18 ViewImage | 当前模型 `supports_vision` |
| 19 DeferredToolFilter | `deferred_setup` 非空（tool_search 启用 + 有 MCP 工具） |
| 21 SubagentLimit | `config.configurable.subagent_enabled` |
| 22 LoopDetection | `loop_detection.enabled` |
| 23 TokenBudget | `token_budget.enabled`（**默认关**） |
| 25 SafetyFinishReason | `safety_finish_reason.enabled`（默认开） |

**恒在链里**的（#1 InputSanitization / #2 ToolOutputBudget / #3-6 / #9-12 / #16-17 / #20 / #26）：核心骨架 + SkillActivation + Title + Memory + SystemMessageCoalescing + Clarification。注意 Title/Memory 虽总在链里，但**内部**按 enabled 决定是否干活（`_should_generate` 返回 False 就跳过）。

---

## 8. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **config/paths** | ThreadData 的 workspace/uploads/outputs 路径**唯一真相源** |
| **config/token_budget_config** | TokenBudget(#23) 的配置（阈值校验） |
| **config/safety_finish_reason / loop_detection / summarization / token_usage / title / memory** | 各对应中间件的开关与参数 |
| **sandbox** | SandboxMiddleware（#5）+ SandboxAuditMiddleware（#9）；ToolOutputBudget 外置写 outputs |
| **uploads** | UploadsMiddleware（#3）调 `list_files_in_dir`/`extract_outline` 注入清单 |
| **memory** | MemoryMiddleware（#17）入队；Summarization（#13）的 `memory_flush_hook` 在压缩前抢拍对话进记忆 |
| **skills** | SkillActivationMiddleware（#12）激活；Summarization 抢救技能文件 read_file 调用 |
| **subagents** | SubagentLimitMiddleware（#21）截断 task 调用；ToolErrorHandling 贴 `subagent_status`；TokenBudget 捕捉子代理 token 回灌；subagent 用 `build_subagent_runtime_middlewares` 复用 #1-10 |
| **tools** | DeferredToolFilter（#19）接 tool_search；ViewImage（#18）接 view_image；TokenUsage 合并子代理 token |
| **models** | SystemMessageCoalescing(#20) provider 无关地修严格后端的 SystemMessage 顺序问题 |
| **lead_agent** | `make_lead_agent` 调 `build_middlewares` 组装链 + tracing 图根注入（见 [tracing.md](tracing.md)） |

---

## 9. 设计动机分析

本模块的每个关键决策都值得讲清「为什么这么设计 + 不这么设计会怎样」。先看核心动机表，再展开几个最容易在面试被追问的。

### 9.0 核心设计动机表

| 设计 | 为什么 | 不这么设计会怎样 |
|------|--------|------------------|
| **中间件模式**而非大函数 | 把横切关注点解耦成独立类，各管各的 | 主 agent 函数几千行，改一处怕动全身 |
| **`wrap_*` 洋葱** vs **`before_*/after_*` 队列**两套钩子 | 包裹钩子要能改请求/重试/吞异常；前后钩子只需追加 state | 一套钩子要么能力不够、要么 everyone-can-mutate 太危险 |
| **顺序是契约**（InputSanitization 最外、Clarification 最末、Safety 在 Loop 后、TokenBudget 在 Loop 后） | 每个位置都由依赖关系推出 | 重试吃到未净化输入 / 中断后记忆没入队 / Loop 对滤掉的调用误触警 |
| **延迟注入**（after_model 检测、wrap_model_call 注入） | 保 `AIMessage→ToolMessage` 配对不被破坏 | 当场插消息 → provider 报 tool_call_ids 不匹配 |
| **`GraphBubbleUp` 强制透传** | 控制流信号（中断/跳转）必须穿透 try/except | Clarification 中断、subagent interrupt 被普通异常吞掉 |
| **config 驱动 gating** | 链长随开关伸缩，关掉的中间件零开销 | 恒定 26 步，关掉的功能仍跑空逻辑浪费 |
| **「已见消息」账本 + BoundedDict** | 准确累计增量、防遗弃 run 内存泄漏 | 直接累加重复计 token / run 遗弃内存撑爆 |
| **lead/subagent 共享前置段** | #1-10 容错逻辑两者都要，复用一份 | 两份装配代码漂移，subagent 少一道防线 |
| **熔断器** | 连续失败时短路，防无限重试拖死 agent | 一个挂掉的服务把整个 agent 卡在重试里 |
| **只改请求、不动 checkpoint**（#1/#6/#20） | 出站请求净化/补全，持久 state 原样 | checkpoint 被中间件污染，靠标记扫描的中件失效 |

### 9.1 为什么 InputSanitization 必须最外层

`wrap_model_call` 是洋葱：注册顺序最前 = 最外层。InputSanitization 排第一，所以它包在所有内层之外——**任何内层中间件（含 #7 LLMErrorHandling 的重试循环）拿到的 request 都已经是净化过的**。

**不这么设计会怎样**：假如 InputSanitization 排在 LLMErrorHandling 之内。第一次调用，用户输入里的 `<system>` 标签被净化后送给模型；模型返回瞬时错误，LLMErrorHandling 重试——重试时它**重新组装请求**，可能又从原始 state 拉到**未净化**的消息送给模型。注入标签在重试路径上漏网。所以防注入必须是最外层，把所有重试都罩在里面。

### 9.2 为什么延迟注入（不能在 after_model 当场插消息）

OpenAI 系协议要求消息历史里 `AIMessage(tool_calls=[X])` 紧跟 `ToolMessage(tool_call_id=X)`——一一配对。`after_model` 是在模型返回 AIMessage **之后**跑的；如果这时往 state 里插一条 HumanMessage（循环警告），消息顺序变成 `AIMessage(tool_calls) → HumanMessage(警告) → ...`，**断了配对**，下一轮 provider 直接 400。

所以 LoopDetection / TokenBudget 在 `after_model` 只把警告**塞进队列**，下一轮 `wrap_model_call`（在调模型**之前**）把队列里的警告**追加到请求末尾**——这时请求还没发给 provider，怎么追加都安全。

### 9.3 为什么 Clarification 必须最末位

`ClarificationMiddleware` 拦到 `ask_clarification` 工具调用时，返回 `Command(goto=END)`——这是 LangGraph 的「跳到 END 节点」指令，**立刻结束本次 agent 执行**。

**不这么设计会怎样**：假如 Clarification 排在 Memory（#17）之前。用户问了个模糊问题，agent 调 `ask_clarification`，Clarification 立刻 `goto=END` 中断——**排在它后面的 Memory 永远跑不到**，这次对话没入记忆队列，跨会话就忘了。所以中断器必须在最末位，让所有「收尾」中间件（记忆入队、标题生成、账本清理）先跑完。

### 9.4 为什么 TokenBudget 在 LoopDetection 之后

预算硬停会**剥掉 tool_calls**（强制模型收尾）。如果 TokenBudget 排在 LoopDetection 之前：TokenBudget 先剥 tool_calls → LoopDetection 看到的是**清理后**的消息，可能误判「没有工具调用」而漏掉本该检测的循环。把 TokenBudget 放在 Loop 之后，Loop 先对原始消息计数，TokenBudget 再清理，互不干扰。

### 9.5 为什么用 `wrap_tool_call` 透传 `GraphBubbleUp`

`GraphBubbleUp` 是 LangGraph 的控制流信号（中断、跳转），形态是异常但**语义不是错误**。`wrap_tool_call` 里典型写法：

```python
def wrap_tool_call(self, request, handler):
    try:
        return handler(request)
    except GraphBubbleUp:   # ← 控制流信号，透传
        raise
    except Exception as exc:  # ← 普通异常，兜底
        return self._build_error_message(request, exc)
```

**不这么设计会怎样**：如果只有 `except Exception`（GraphBubbleUp 是其子类），Clarification 的 `Command(goto=END)`、subagent 的 interrupt 会被当成「工具失败」转成错误 ToolMessage——中断语义彻底失效，agent 不再暂停、一路狂奔到结束。所以 `except GraphBubbleUp: raise` 必须在前（[tool_error_handling_middleware.py:104-106](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py#L104-L106)）。

### 9.6 为什么 lead/subagent 要共享前置段

#1-#10 是「容错与隔离」基础（净化、防爆、隔离目录、沙箱、补悬空、LLM 重试、审计、工具异常兜底）——lead agent 需要，subagent **一样**需要（subagent 也会调模型、调工具、可能死循环、可能遇到 provider 400）。把这些抽成 `_build_runtime_middlewares`，lead 和 subagent 各调一次，复用同一份容错防线。

**不这么设计会怎样**：两份独立装配代码，某天给 lead 的 LoopDetection 升级了忘了给 subagent 升级——subagent 就少了这道防线，可能死循环到 recursion limit。共享段让升级一处即生效两者。

---

## 10. 实现差异（vs 上游 deer-flow 源码）

对照两侧 `backend/packages/harness/deerflow/agents/middlewares/`（与 mini 同布局，26 个文件一一对应），**剥 docstring/comment 后判逻辑差**。结论：**mini 是忠实移植——26 槽位里 25 个中间件逻辑一致，唯一结构性缺失是 #8 Guardrail；另有 dynamic_context / summarization 一处真分歧（提醒载体），其余差异是组织迁移与排版**。

### 10.1 头条差异：#8 Guardrail 整段不实装

上游 `tool_error_handling_middleware.py` 在 `_build_runtime_middlewares` 里有约 16 行 Guardrail 工厂块（`if guardrails_config.enabled and guardrails_config.provider: ... GuardrailMiddleware(provider, fail_closed=..., passport=...)`），从独立的 `guardrails/` 包（4 个文件 `__init__.py`/`builtin.py`/`middleware.py`/`provider.py`）加载可插拔的 `GuardrailProvider`，在工具调用前做授权（deny 返回错误 ToolMessage）。

mini **既没有这 16 行工厂块**（[tool_error_handling_middleware.py:173](../backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py#L173) 只留注释「此处跳过」），**也没有 `guardrails/` 包**。这是 mini 唯一「整步缺失」的位置——Guardrail 列为「真正可选」（依赖独立外部包，按需），不影响核心 agent 行为。

### 10.2 dynamic_context 提醒载体：HumanMessage vs SystemMessage（真分歧）

mini 的 `DynamicContextMiddleware._make_reminder_and_user_messages` 把日期+记忆提醒作为**独立 HumanMessage** 注入（[dynamic_context_middleware.py:70](../backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py#L70)）；上游已演进为用 **SystemMessage** 注入（带结构化 `additional_kwargs` 的 `_DYNAMIC_CONTEXT_REMINDER_KEY` + `_REMINDER_DATE_KEY`）。

**mini 源码本身记录了这处分歧**（[dynamic_context_middleware.py:68-74](../backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py#L68-L74)）：「上游已把动态上下文提醒从 HumanMessage 迁到 SystemMessage（HumanMessage 形态仅旧 checkpoint 残留），mini 当前仍以 HumanMessage 注入」。两边都靠 ID-swap 保前缀缓存，只是载体不同。

mini 的 `is_dynamic_context_reminder`（[dynamic_context_middleware.py:74](../backend/packages/harness/deerflow/agents/middlewares/dynamic_context_middleware.py#L74)）**同时认 HumanMessage 和 SystemMessage**——为兼容旧 checkpoint 里可能残留的 SystemMessage 形态，也保 SystemMessageCoalescing(#20) 的去重逻辑（[system_message_coalescing_middleware.py:103](../backend/packages/harness/deerflow/agents/middlewares/system_message_coalescing_middleware.py#L103)）继续正常。

### 10.3 summarization：提醒抢救简化 + 工厂迁入

- 上游 `_partition_messages` 用 base-id 匹配（`removesuffix` + `startswith`）抢救「技能文件 read_file」与「提醒派生」消息；mini 简化为只过滤 `is_dynamic_context_reminder` 消息（与 mini 的 HumanMessage 提醒载体配套）。
- mini **把 `_create_summarization_middleware` 工厂搬进了本文件**（上游该工厂在 `lead_agent/agent.py`）。核心 `DeerFlowSummarizationMiddleware` 类逻辑一致（`before_summarization` 抢拍钩子、技能文件抢救、`TAG_NOSTREAM` 标记都在）。

### 10.4 `build_middlewares` 装配入口位置（组织迁移）

上游 `agents/middlewares/__init__.py` **是空文件**（仅 2 行）；`build_middlewares` 函数与 `_TODO_SYSTEM_PROMPT` 等都放在 `lead_agent/agent.py`。**mini 把它们整体搬进了 `middlewares/__init__.py`**——让「中间件链的装配入口」与「中间件实现」同居一个包，教学上更内聚。装配逻辑（顺序、条件、延迟 import）一致。

### 10.5 其余零星差异（多为组织迁移 / 排版）

| 文件 | 差异 | 性质 |
|------|------|------|
| title_middleware.py | 上游 TitleMiddleware 给标题模型调用加 `TAG_NOSTREAM`（防标题生成流广播给前端）；mini 省略此 tag | 小分歧 |
| uploads_middleware.py | 上游 `from deerflow.utils.file_conversion import extract_outline`；mini `from deerflow.uploads.conversion import extract_outline` | 组织迁移（同 #23 uploads.md） |
| token_usage_middleware.py | 上游 todo 去重多一个 `content not in previous_by_content` 检查；mini 省略 | 小分歧 |
| dynamic_context_middleware.py | 上游 `runtime: Runtime` 类型注解（import `langgraph.runtime.Runtime`）；mini 不注解 | 仅注解 |

### 10.6 其余约 20 个中间件：0 逻辑差

剥 docstring 后 **stripped token 数完全相同或 ±10 以内**（差的全是 docstring 残留 + `from __future__ import annotations` + 多行/单行排版差）的文件：

- **逐字节相同**：`input_sanitization_middleware.py`（1008=1008）、`system_message_coalescing_middleware.py`（484=484）、`token_budget_middleware.py`（1721=1721）、`tool_call_metadata.py`（283=283）。
- **±10 以内**：`clarification` / `dangling_tool_call` / `deferred_tool_filter` / `llm_error_handling` / `loop_detection` / `memory` / `safety_finish_reason` / `safety_termination_detectors` / `sandbox_audit` / `skill_activation` / `thread_data` / `todo` / `tool_output_budget` / `view_image` —— 全是 docstring 中英 + 排版，无逻辑差。

**测试覆盖**：5 个 dedicated 中间件测试文件（`test_middlewares.py` 81 + `test_input_sanitization_middleware.py` 29 + `test_token_budget_middleware.py` 22 + `test_system_message_coalescing_middleware.py` 16 + `test_agent_with_middlewares.py` 6），共 **154 个测试函数**，覆盖装配顺序契约、各中间件行为、config gating。

---

## 11. 排错 FAQ

- **「工具调用后 agent 不继续 / 报 tool_call_ids did not have response messages」**：检查消息历史有无 dangling tool call（DanglingToolCall 应已补占位）；或某中间件在 `after_model` 插了消息破配对（正确做法是排队到 wrap_model_call）。
- **「用户的 `<system>` 标签被模型当指令执行」**：确认 InputSanitization(#1) 在链上（默认在）；它转义系统保留标签，普通 HTML 不动。
- **「vLLM/SGLang/Qwen 报 System message must be at the beginning」**：SystemMessageCoalescing(#20) 应已合并多条 SystemMessage；若仍报，检查是否有 provider 在中间件之外又注入了 SystemMessage。
- **「agent 一个 run 烧了太多 token」**：开 `token_budget.enabled`，设 `warn_threshold`/`hard_stop_threshold`；硬停会剥 tool_calls 逼模型收尾。
- **「LLM 一直超时重试」**：StreamChunkTimeoutError 只重试 1 次；若持续，看熔断器是否打开（`circuit_breaker`）。
- **「agent 死循环调同一工具」**：调低 `loop_detection.warn/hard_limit`；频率层 `tool_freq_*` 抓同类型不同参数。
- **「task 子代理调用太多」**：`SubagentLimitMiddleware` clamp 到 [2,4]，超出在 `configurable.max_concurrent_subagents` 调。
- **「MCP 工具 schema 没暴露」**：tool_search 未启用 / 未提升；DeferredToolFilter 据此隐藏，调 tool_search 提升。
- **「provider 返回 content_filter 但 agent 还在执行工具」**：SafetyFinishReasonMiddleware 是否启用（默认开）；检测器（safety_termination_detectors）是否覆盖该 provider。

---

**下一篇**：[agents.md](agents.md)（lead_agent factory + RuntimeFeatures + thread_state 类型化 reducer + custom-agent 分支）——本模块的 `build_middlewares` 由 `make_lead_agent` 调用组装成最终 agent。
