# 24. middlewares.md — 中间件链（23 步，Agent 的行为骨架）

> **一句话定位**：本模块给 agent 装「行为骨架」——23 个中间件按**严格顺序**串成一条链，
> 在模型调用的前后、工具调用的前后、agent 轮次的开始结束做各种横切处理（防上下文爆炸、
> 防循环、注入上下文、安全拦截、错误兜底……）。顺序是**契约**：换顺序会破坏功能甚至安全。

读完 [uploads.md](uploads.md)（懂了「上传文件怎么变 agent 上下文」）再看本篇最省事——本篇是
**所有横切行为的总装配**。`UploadsMiddleware` 是链的第 3 步，本篇解释它在整条链里的位置、
以及其它 22 步各自干什么、为什么这个顺序。它把 Phase 2–5.5 的各模块（sandbox / subagents /
skills / memory / mcp / tools / uploads）串成一个能跑的 agent。

---

## 0. 这个模块解决什么问题

agent 一次「思考-行动」会经过很多阶段：用户消息进来 → 建隔离目录 → 读上传文件 → 分配沙箱 →
调模型 → 处理工具调用 → 检测循环 → 生成标题 → 入记忆队列 …… 如果把这些逻辑全堆进主 agent
函数，会得到一个几千行的巨型函数，谁都不敢改。

**中间件模式**：把每个横切关注点写成一个独立类（中间件），每个类实现几个**钩子**（hook）——
「模型调用前」「工具调用后」等。框架按顺序把所有中间件串起来，agent 跑到某个阶段时自动调
对应钩子。`build_middlewares()` 就是这条链的**装配清单**。

本模块对齐 deer-flow 的 23 步生产链（v1.2 全面对标），不裁剪。

## 1. AgentMiddleware 钩子机制

每个中间件继承 `AgentMiddleware`，按需实现这些钩子（mini 用 langchain 的实现）：

| 钩子 | 触发时机 | 典型用途 |
|------|----------|----------|
| `before_agent` / `abefore_agent` | 一轮 agent 开始 | 建目录、注入上下文（日期/记忆/上传清单） |
| `after_agent` / `aafter_agent` | 一轮 agent 结束 | 入记忆队列、清状态 |
| `before_model` / `abefore_model` | 调 LLM 前 | 摘要检测、补 dangling 工具响应 |
| `after_model` / `aafter_model` | LLM 返回后 | 循环检测、标题生成、安全拦截、子代理限流 |
| `wrap_model_call` / `awrap_model_call` | **包住**模型调用（可改入参/出参/重试） | LLM 错误重试、延迟工具过滤、工具输出预算 |
| `wrap_tool_call` / `awrap_tool_call` | **包住**工具调用 | 工具异常兜底、澄清拦截、沙箱审计 |

`wrap_*` 是「洋葱模型」——能改请求 / 响应、吞异常、重试；`before_*/after_*` 只能返回状态更新。
异步钩子（`a` 前缀）跑在事件循环上；同步钩子在 worker 线程。**阻塞 IO 必须卸线程**（红线 #1）。

## 2. 23 步顺序（Part D 契约）

`build_middlewares(config, model_name, agent_name, ...)` 按下表顺序 append。顺序里的几个**硬约束**
（红线）用 ⚠️ 标注：

```
 1. ToolOutputBudgetMiddleware   ← 防爆最先：单个工具返回太大 → 外置磁盘 + 预览
 2. ThreadDataMiddleware         ← 算/建每线程隔离目录（workspace/uploads/outputs）
 3. UploadsMiddleware            ⚠️ 接 M23：注入上传清单 + 文档大纲（须在 Sandbox 前）
 4. SandboxMiddleware            ⚠️ 须在 ThreadData 之后（依赖 thread_data 路径）
 5. DanglingToolCallMiddleware   ← 补悬空工具调用的占位响应（防 provider 400）
 6. LLMErrorHandlingMiddleware   ← 重试/退避/熔断 + 用户可读兜底
 7. [GuardrailMiddleware]        ← 真正可选，mini 未做（依赖 guardrails 独立模块）
 8. SandboxAuditMiddleware       ← 沙箱命令审计（block/warn/pass 分级）
 9. ToolErrorHandlingMiddleware  ← 工具异常 → 错误 ToolMessage（run 不中断）
10. DynamicContextMiddleware     ← ID-swap 注入日期/记忆到首条 HumanMessage
11. SkillActivationMiddleware    ← /skill-name 激活，注入 SKILL.md
12. SummarizationMiddleware      ← 可选：上下文近 token 上限时压缩（接 M13 抢拍钩子）
13. TodoMiddleware               ← plan_mode 时挂载（write_todos 工具 + 上下文丢失检测）
14. TokenUsageMiddleware         ← 可选：记 token 用量 + 给每步贴动作归因
15. TitleMiddleware              ← 首轮后生成线程标题
16. MemoryMiddleware             ← 入记忆更新队列（filter→correction/reinforcement→add）
17. ViewImageMiddleware          ← 仅 supports_vision：view_image 完成后注入 base64 图片
18. DeferredToolFilterMiddleware ← 接 M15/M20：延迟 MCP 工具未提升前不暴露 schema
19. SubagentLimitMiddleware      ← 接 M11：截断超额 task 调用（clamp [2,4]）
20. LoopDetectionMiddleware      ← 哈希 + 频率双层循环检测
21. custom_middlewares           ← 调用方自定义（插在 Clarification 前）
22. SafetyFinishReasonMiddleware ← provider 安全终止时剥 tool_calls（content_filter/refusal/SAFETY）
23. ClarificationMiddleware      ⚠️ 永远最后（红线 #14）：拦截 ask_clarification 中断执行
```

链分两段：**步骤 1-9**（`build_lead_runtime_middlewares`）lead 与 subagent **共享**；**步骤 10-23**
lead 专属。subagent 用 `build_subagent_runtime_middlewares`（步骤 1-9 + vision/deferred/safety）。

## 3. 为什么是这个顺序（关键不变量）

### ThreadData(#2) → Uploads(#3) → Sandbox(#4)

`SandboxMiddleware` 把沙箱目录挂进 agent 可见的虚拟路径，需要 `thread_data` 里**已经算好**的
`outputs_path`（`ToolOutputBudgetMiddleware` 外置大输出时写这里）。`UploadsMiddleware` 要在沙箱
挂载前算好上传目录。所以三者严格 ThreadData → Uploads → Sandbox（红线 #14）。

### Clarification 永远最后（#23）

`ClarificationMiddleware` 用 `Command(goto=END)` **中断整次执行**。如果它不在最后，排在它后面的
中间件就跑不到（比如记忆没入队、标题没生成）。所以它必须是链尾（红线 #14）。

### 所有 `wrap_tool_call` 必须 `raise GraphBubbleUp`（红线 #15）

`GraphBubbleUp` 是 LangGraph 的控制流信号（interrupt / pause / resume / `Command(goto=...)`）。
`wrap_tool_call` 里调 `handler(request)` 时，若 handler 抛 `GraphBubbleUp`，**必须原样 `raise`**，
不能被下面的 `except Exception` 吞掉——否则 Clarification 的中断、subagent 的 interrupt 全失效。

```python
def wrap_tool_call(self, request, handler):
    try:
        return handler(request)
    except GraphBubbleUp:   # ← 控制流信号，透传
        raise
    except Exception as exc:  # ← 普通异常，兜底
        return self._build_error_message(request, exc)
```

### Safety(#22) 在 Loop(#20) 之后注册

LangChain 的 `after_model` 按**倒序**列表分发——最后注册的最先观察模型输出。Safety 在 Loop 之后
注册 → Safety 先看原始响应 → 命中（content_filter）则清 tool_calls → Loop 再对清理后的消息计数，
不会因被滤掉的调用误触循环警。

## 4. config 驱动 gating（哪些步骤条件挂载）

不是 23 步恒定——按 config 开关 + 运行时上下文条件挂载：

| 步骤 | 挂载条件 |
|------|----------|
| 7 Guardrail | 真正可选，**mini 不做**（无 guardrails 模块） |
| 12 Summarization | `summarization.enabled` |
| 13 Todo | `config.configurable.is_plan_mode` |
| 14 TokenUsage | `token_usage.enabled` |
| 17 ViewImage | 当前模型 `supports_vision` |
| 18 DeferredToolFilter | `deferred_setup` 非空（tool_search 启用 + 有 MCP 工具） |
| 19 SubagentLimit | `config.configurable.subagent_enabled` |
| 20 LoopDetection | `loop_detection.enabled` |
| 22 SafetyFinishReason | `safety_finish_reason.enabled`（默认开） |

**总是挂载**的（步骤 1-6/8-11/15/16/23）：核心骨架 + Title + Memory + Clarification。
注意 Title/Memory 虽总在链里，但**内部**按 enabled 决定是否干活（对齐 deer）。

## 5. 关键中间件详解

### ToolOutputBudgetMiddleware（#1，防爆）

工具返回超大（如 `bash cat huge.log` 50 万字）会把模型上下文撑爆。本中间件：超过
`externalize_min_chars`（默认 12000）→ 完整输出**外置到磁盘**（`outputs/.tool-results/`），替换成
精简预览（头 N 字 + 「全文存到 /mnt/user-data/outputs/xxx，用 read_file 按行读」+ 尾 N 字）。磁盘
不可用时回退首尾截断。`read_file` 在 `exempt_tools` 里（防「外置→读→再外置」循环）。

两条 hook：`wrap_tool_call`（实时预算每个返回）+ `wrap_model_call`（扫历史 ToolMessage 补截断）。
外置有 host-disk / 沙箱内直写两路径，靠 `provider.uses_thread_data_mounts` 判分支（AIO 远端沙箱
无 host mount，须写进沙箱）。

### DanglingToolCallMiddleware（#5，防 400）

**dangling tool call**：某 AIMessage 带 tool_calls 但历史没对应 ToolMessage（用户中断 / 取消导致）。
OpenAI 系校验器要求每个 tool_call 紧跟响应，缺了就 400，agent 卡死。本中间件用 `wrap_model_call`
扫历史，给悬空调用插一条合成错误 ToolMessage（紧跟那条 AIMessage 后），保消息顺序合法。

为何用 `wrap_model_call` 而非 `before_model`：`before_model` + reducer 会把消息追加到末尾而非紧跟
AIMessage；`wrap_model_call` 能 `request.override(messages=...)` 重建正确顺序。

### LLMErrorHandlingMiddleware（#6，重试 + 熔断）

包装模型调用。错误分类：quota / auth / transient（超时/限流/可重试状态码）/ busy / generic。
瞬时错误指数退避重试；重试耗尽或非瞬时错误 → 返回兜底 AIMessage（agent 优雅降级而非崩）。

**熔断器**（`CircuitBreakerConfig`）：连续失败达 `failure_threshold` → 短路返回兜底（半开探测恢复），
防持续挂的 provider 被逐请求退避放大噪声。流断错误（`StreamChunkTimeoutError`）给专门文案
「拆小请求」而非「干等重试」。尊重 provider 的 `Retry-After` 头。透传 `GraphBubbleUp`（红线 #15）。

### DynamicContextMiddleware（#10，ID-swap 注入）

把当前日期 + 记忆经 **ID-swap** 注入首条 HumanMessage：保留原消息 id、原地替换内容、紧随派生一条
`{id}__user` 消息。为何？**保持系统提示完全静态** → 让模型 provider 的 prefix-cache 能复用
（系统提示不变，缓存命中省 token / 延迟）。M13 落地，记忆是 nice-to-have（异常吞）。跨午夜时
更新日期提醒。

### SummarizationMiddleware（#12，压缩 + 抢拍）

继承 langchain `SummarizationMiddleware`，加两个 deer 能力：① **`before_summarization` 钩子**——
摘要删消息前派发 `SummarizationEvent`，让 [memory.md](memory.md) 的 `memory_flush_hook` 把对话
**抢拍**进记忆队列（否则被压缩的消息细节永久丢失）；② **技能文件抢救**——分区后保留最近 N 个
「加载过技能文件」的 read_file 调用 + 结果，避免技能正文被压掉后重读（费 token）。摘要 LLM 调用
贴 `TAG_NOSTREAM`（防其 token 流被当成幽灵 AI 消息广播给前端）。

### LoopDetectionMiddleware（#20，双层检测）

防 agent 用相同参数无限调工具直到 recursion limit。两层：① **哈希层**——哈希 tool_calls（name +
关键字段，**顺序无关**），滑动窗口里同一哈希 ≥ warn（默认 3）→ 队列警告；≥ hard（默认 5）→ 剥
tool_calls 强制文本答复。② **频率层**——同一工具**类型**（不限参数）调多次（read_file 读 40 个
不同文件）哈希层抓不到，用 `tool_freq_warn/hard_limit` + 每工具覆盖兜底。

**警告在 `wrap_model_call` 注入而非 `after_model`**：`after_model` 时工具节点还没跑、无对应
ToolMessage，插消息会破 OpenAI/Moonshot 配对（报 `tool_call_ids did not have response messages`）。
推迟到 `wrap_model_call`，所有 ToolMessage 已在请求里，警告追加到末尾——配对完整。警告是瞬态的，
run 结束未排空就丢（`after_agent` 清）。`from_config` 构造过 Pydantic 校验。

### SafetyFinishReasonMiddleware（#22，安全终止拦截）

provider（OpenAI `content_filter` / Anthropic `refusal` / Gemini `SAFETY`）会在流中途停生成但**仍
返回半成形 tool_calls**。LangChain 把带 tool_calls 的 AIMessage 当「去执行」，于是截断参数被当完整
派发 → agent 看截断结果 → 修 → 又被滤 → 死循环。本中间件 `after_model` 门控：检测器命中**且**带
tool_calls → 剥掉 tool_calls、追加用户说明、塞 `additional_kwargs.safety_termination` 可观测字段。
检测器（[safety_termination_detectors.py](../backend/packages/harness/deerflow/agents/middlewares/safety_termination_detectors.py)）
是策略接口 + 三个内置（OpenAI/Anthropic/Gemini），新 provider 实现接口经 config 接入。仅在有 tool_calls
时介入——无 tool_calls 的 content_filter 原样放行让部分文本到用户。

### ClarificationMiddleware（#23，中断）

拦截 `ask_clarification` 工具调用 → 格式化问题 + 选项 + 图标 → `Command(goto=END)` 中断执行等用户
回复。确定性 message id（`clarification:{tool_call_id}`）让**重试的澄清替换而非追加**。options 若被
模型序列化成 JSON 字符串会归一成 list。永远末位（红线 #14）。

## 6. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **config/paths** | ThreadData 的 workspace/uploads/outputs 路径**唯一真相源** |
| **sandbox** | SandboxMiddleware（#4）+ SandboxAuditMiddleware（#8）；ToolOutputBudget 外置写 outputs |
| **uploads(M23)** | UploadsMiddleware（#3）调 `list_files_in_dir`/`extract_outline` 注入清单 |
| **memory(M13)** | MemoryMiddleware（#16）入队；Summarization（#12）的 `memory_flush_hook` 抢拍 |
| **skills(M14)** | SkillActivationMiddleware（#11）激活；Summarization 抢救技能文件 |
| **subagents(M11)** | SubagentLimitMiddleware（#19）截断 task 调用；ToolErrorHandling 贴 subagent_status |
| **tools(M15)** | DeferredToolFilter（#18）接 tool_search；ViewImage（#17）接 view_image；TokenUsage 合并子代理 token |
| **M17 lead_agent** | `make_lead_agent` 调 `build_middlewares` 组装链 + tracing 图根注入 |

## 7. 设计要点回顾

1. **顺序是契约**：ThreadData→Uploads→Sandbox；Clarification 永远末位；Safety 在 Loop 后。
2. **红线 #15**：所有 `wrap_*` 透传 `GraphBubbleUp`，控制流信号不被吞。
3. **config 驱动 gating**：9 步条件挂载，14 步恒定骨架。
4. **洋葱 vs 前后**：`wrap_*` 能改入参/出参/重试；`before_*/after_*` 只返回状态更新。
5. **阻塞 IO 卸线程**：`wrap_tool_call` 的沙箱 IO、`Uploads.abefore_agent` 的目录扫描走 `to_thread`/`run_in_executor`（红线 #1）。
6. **熔断 + 分类重试**：LLMErrorHandling 按错误类型给不同策略，熔断防放大噪声。
7. **双层循环检测**：哈希（相同调用）+ 频率（同工具类型），警告延迟到 `wrap_model_call` 注入防破配对。
8. **延迟工具**：DeferredToolFilter 按 catalog_hash scope 提升，防陈旧提升暴露改名工具。
9. **安全终止拦截**：provider 安全信号 + tool_calls 才剥，保截断参数不被当完整派发。
10. **lead/subagent 共享前 9 步**：`build_lead_runtime_middlewares` 复用，subagent 不含 uploads/动态上下文。

## 8. 排错 FAQ

- **「工具调用后 agent 不继续 / 报 tool_call_ids did not have response messages」**：检查消息历史有无 dangling tool call（DanglingToolCall 应已补占位）；或某中间件在 `after_model` 插了消息破配对。
- **「LLM 一直超时重试」**：StreamChunkTimeoutError 只重试 1 次；若持续，看熔断器是否打开（`circuit_breaker`）。
- **「agent 死循环调同一工具」**：调低 `loop_detection.warn/hard_limit`；频率层 `tool_freq_*` 抓同类型不同参数。
- **「task 子代理调用太多」**：`SubagentLimitMiddleware` clamp 到 [2,4]，超出在 `configurable.max_concurrent_subagents` 调。
- **「MCP 工具 schema 没暴露」**：tool_search 未启用 / 未提升；DeferredToolFilter 据此隐藏，调 tool_search 提升。
- **「provider 返回 content_filter 但 agent 还在执行工具」**：SafetyFinishReasonMiddleware 是否启用（默认开）；检测器是否覆盖该 provider。

---

**下一篇**：[README.md](README.md) 待写表里下一个是 [agents.md](legacy/)（M17 lead_agent factory +
RuntimeFeatures + thread_state 类型化 reducer + custom-agent 分支）——本模块的 `build_middlewares`
由 `make_lead_agent` 调用组装成最终 agent。
