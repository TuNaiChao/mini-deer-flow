# 25. agents.md — Agent 装配（SDK + config 双入口，把模型/工具/中间件/提示词拼成一张可跑的图）

> **一句话定位**：本模块是「总装车间」——把前面所有模块（models / tools / sandbox / skills / memory / middlewares …）按两种入口拼成一张能跑的 LangGraph 图。`create_deerflow_agent` 是 **SDK 入口**（纯 Python 参数，给程序化用）；`make_lead_agent` 是 **config 入口**（读运行时配置，是 LangGraph Studio / Gateway 注册的图工厂）。两者背后共用同一套底层原语。

**学完能回答（learning outcomes）**：

1. 「跑一个 agent」要凑齐哪四样（模型 / 工具 / 中间件 / 系统提示词），LangGraph 的 `create_agent` 原语为什么不够用；
2. SDK 入口（`create_deerflow_agent`，features 驱动）与 config 入口（`make_lead_agent`，RunnableConfig 驱动）各做什么、什么时候用哪个；
3. `RuntimeFeatures` 的「`True` / `False` / 实例」三态 + `@Next`/`@Prev` 锚点插入怎么让自定义中间件精确落位；
4. `ThreadState` 的几个 reducer（`merge_sandbox` / `merge_promoted` / `merge_viewed_images`）各什么语义，为什么 `merge_sandbox` 要 fail-closed；
5. tracing 回调为什么必须挂图根、配套每个图内 `create_chat_model` 为什么必须 `attach_tracing=False`；
6. 系统提示为什么必须完全静态（记忆 / 日期不进系统提示），与 prefix-cache 的关系；
7. 能在面试里讲清「mini 的 agent 装配与上游 deer-flow 源码的差异在哪、为什么」（见 §10）。

读完 [middlewares.md](middlewares.md)（懂了「25 个中间件组成的链是 agent 的行为骨架」）再看本篇最省事——本篇回答「这条链是谁、按什么顺序、在哪儿组装出来的」。中间件链本身在 `build_middlewares`（见 [middlewares.md](middlewares.md)）；本篇讲它的两个调用方（factory + lead_agent）以及它们还做了什么（模型解析 / 工具过滤 / 提示词拼接 / tracing 注入）。

---

## 1. 名词（先懂这些再往下看）

### 1.1 计算机基础层（每个名词第一次出现就解释）

- **图（graph）/ 节点（node）/ 边（edge）**：LangGraph 把一个 agent 建模成「状态图」——节点是一个处理步骤（如「调模型」「跑工具」），边是步骤间的跳转（可以是固定的，也可以由模型决定）。agent 跑起来就是在这张图上游走，每走一步读/写共享状态。`create_agent` 原语帮你把「调模型 + 跑工具 + 循环」编成一张标准图。
- **状态（state）**：图里所有节点共享的数据结构（在 mini 里就是 `ThreadState`）。每个节点读它、改它、把改后的结果写回去。详见 §5.4。
- **reducer（合并函数）**：当**同一图步**里多个节点都写**同一个**状态字段时，框架调这个字段的 reducer 把多个值合并成一个。没有 reducer 的字段会「后写覆盖前写」；有 reducer 的字段按自定义逻辑合并（去重、并集、抛错……）。这是 LangGraph 处理并发写的协议。
- **`Annotated[T, reducer]`**：Python 类型注解的「附加元信息」语法。LangGraph 用它给状态字段绑定 reducer——注解里写 reducer 函数，框架读到就知道合并这个字段时调谁。
- **`TypedDict`**：Python 的「带字段类型的 dict」——像定义一个 struct，规定这个 dict 有哪些 key、各是什么类型。`ThreadState` / `SandboxState` 都是 TypedDict。
- **`dataclass`**：Python 的「自动生成构造器/比较/打印」装饰器——给一个类加 `@dataclass`，就不用手写 `__init__` 一堆 `self.x = x`。`RuntimeFeatures` 是 dataclass。
- **`Literal[...]`**：类型注解，限定「只能是这几个值之一」。`Literal[False]` 表示「这个字段只能是 `False`」（不接受 `True`）——用来表达「这个 feature 没有内置默认，只能关或给自定义实例」。
- **装饰器（decorator）**：`@something` 语法，本质「接收类/函数、返回新类/函数」。`@Next(X)` / `@Prev(X)` 是本模块自定义的装饰器，给中间件类打上「插在 X 旁边」的标记。
- **前缀缓存（prefix cache）**：LLM 提供商的优化——两次请求**开头一样**，第二次复用第一次算到一半的中间结果，省算力降延迟。所以系统提示越静态越能命中。这是「记忆/日期不进系统提示」的根本动机。
- **幂等（idempotent）**：同一操作做多次和做一次效果一样。`merge_sandbox` 接受「多个工具写同一个 sandbox_id」就是幂等写——写十次同一个 id 等于写一次。
- **fail-closed**：遇到不确定 / 冲突时**宁可失败也不放行**（安全优先）。`merge_sandbox` 见到不同 sandbox_id 直接抛错就是 fail-closed；反义是 fail-open（放行继续，可能不安全）。
- **SDK**：Software Development Kit，给程序员用的编程接口（这里是纯 Python 函数）。SDK 入口 = 你写代码调；config 入口 = 框架读配置文件调。

### 1.2 模块层名词

- **`create_agent`**：langchain 提供的底层原语——接收 `model / tools / middleware / system_prompt / state_schema`，编译出一张可跑的 `CompiledStateGraph`。它是「拼图积木」，本模块的两个入口都是它的上层封装。
- **`RuntimeFeatures`**：本模块的声明式 feature flag（[features.py:17](../backend/packages/harness/deerflow/agents/features.py#L17)）——一个 dataclass，每个字段代表一个可开关的行为（sandbox / memory / subagent / vision / token_budget …）。`True`/`False`/实例三态。
- **`@Next(X)` / `@Prev(X)`**：本模块的中间件定位装饰器（[features.py:46](../backend/packages/harness/deerflow/agents/features.py#L46)、[features.py:58](../backend/packages/harness/deerflow/agents/features.py#L58)）——给自定义中间件类打「排在 X 之后 / 之前」的标记，让 `_insert_extra` 能精确插入。
- **LangGraph 图工厂（graph factory）**：一个 `config → 图` 的函数，LangGraph Server 在收到请求时调用它编译出本次 run 要跑的图。`make_lead_agent` 就是 mini 注册在 `langgraph.json` 的图工厂。

---

## 2. 这个模块解决什么问题

「跑一个 agent」需要凑齐四样：

1. **模型**（`BaseChatModel`）——谁来思考；
2. **工具**（`list[BaseTool]`）——能干什么；
3. **中间件**（`list[AgentMiddleware]`）——横切行为（防爆 / 循环检测 / 安全拦截 …）；
4. **系统提示词**（`str`）——告诉模型它的角色 / 规则。

LangGraph 的 `create_agent(model, tools, middleware, system_prompt, ...)` 把这四样编成一张状态图。但「凑齐这四样」本身有大量决策：用哪个模型？开不开子代理？哪些工具对当前 agent 可见？系统提示要不要塞技能段 / 子代理段？这些决策不能堆进 `create_agent`（它是底层原语），所以本模块提供两层封装：

- **`create_deerflow_agent`**（[factory.py:66](../backend/packages/harness/deerflow/agents/factory.py#L66)）：SDK 级，纯 Python 参数，用 `RuntimeFeatures` 声明「开哪些行为」，工厂自动装链。给程序化场景用（测试、内嵌 client、自定义编排）。
- **`make_lead_agent`**（[agent.py:99](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L99)）：config 级，从 `RunnableConfig` 解析一切（模型名 / agent 名 / plan_mode / subagent …），还要做工具策略过滤、延迟工具装配、tracing 注入。这是 LangGraph 注册的正式入口。

---

## 3. 结构（装配关系图）

两个入口背后共用同一套底层（`create_agent` 原语 + 各模块）。装配关系：

```
                  ┌──────────────────────────────────────────────────┐
   SDK 入口       │  create_deerflow_agent()           [factory.py]  │
   (程序化调用)   │  ├─ features 模式 → _assemble_from_features()    │
                  │  │     + _insert_extra()（@Next/@Prev 锚点）      │
                  │  └─ middleware 全接管 → 原样用                    │
                  └──────────────────────────────────────────────────┘
                                       │
                                       ▼
                              create_agent()  ← langchain 底层原语
                                       │
                  ┌──────────────────────────────────────────────────┐
   config 入口    │  make_lead_agent(config)          [agent.py]     │
   (LangGraph     │  └─ _make_lead_agent()                            │
   注册的图工厂)  │       ├─ _resolve_model_name() / _get_runtime_config() │
                  │       ├─ build_tracing_callbacks() 图根注入       │
                  │       ├─ get_available_tools() + filter_tools_by_skill_allowed_tools() │
                  │       ├─ assemble_deferred_tools() 延迟装配       │
                  │       ├─ build_middlewares() 装中间件链            │
                  │       ├─ apply_prompt_template() 拼系统提示        │
                  │       └─ create_agent()  ← 同一底层原语            │
                  └──────────────────────────────────────────────────┘
                                       │
                                       ▼
                            CompiledStateGraph（可跑的图）
```

文件分工：

```
agents/
├── factory.py        ← SDK 入口 create_deerflow_agent + _assemble_from_features + _insert_extra
├── features.py       ← RuntimeFeatures dataclass + @Next / @Prev 装饰器（纯数据，无 IO）
├── thread_state.py   ← ThreadState 状态 schema + 5 个 reducer
├── __init__.py       ← 包导出
└── lead_agent/
    ├── agent.py      ← config 入口 make_lead_agent（图工厂）
    ├── prompt.py     ← apply_prompt_template 系统提示词条件段拼接
    └── __init__.py
```

---

## 4. 核心概念

### 4.1 双入口的分工

| | `create_deerflow_agent`（SDK） | `make_lead_agent`（config） |
|---|---|---|
| 输入 | 纯 Python 参数（model 实例 / tools list / features） | `RunnableConfig`（框架传的运行时配置） |
| 配置来源 | 调用方自己准备 | 从 config 解析（模型名 / agent 名 / plan_mode …） |
| 中间件链 | `_assemble_from_features` 按 15 步精简顺序装 | `build_middlewares` 按 26 槽位完整顺序装（见 [middlewares.md](middlewares.md)） |
| tracing | 不注入（调用方自己挂） | **图根注入** `build_tracing_callbacks` |
| 工具策略 | 不做技能白名单过滤 | 做 `filter_tools_by_skill_allowed_tools` + 延迟装配 |
| 适用 | 测试 / 内嵌 client / 自定义编排 | LangGraph Studio / Gateway 正式 run |

### 4.2 RuntimeFeatures 三态 + @Next/@Prev

[features.py:17](../backend/packages/harness/deerflow/agents/features.py#L17) 是纯数据，没有 IO：

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True       # 沙箱基础设施
    memory: bool | AgentMiddleware = False       # 记忆
    summarization: Literal[False] | AgentMiddleware = False  # 摘要（无内置默认）
    subagent: bool | AgentMiddleware = False     # 子代理
    vision: bool | AgentMiddleware = False       # 图片
    auto_title: bool | AgentMiddleware = False   # 自动标题
    guardrail: Literal[False] | AgentMiddleware = False     # 护栏（无内置默认）
    loop_detection: bool | AgentMiddleware = True  # 循环检测
    token_budget: bool | AgentMiddleware = False   # 单 run token 预算
```

每个 feature 三态：`True`（用内置默认中间件）/ `False`（关）/ 一个 `AgentMiddleware` 实例（自定义替换）。`summarization` 和 `guardrail` 没有内置默认（摘要要传模型实例、护栏要传 provider），所以这俩只能 `False` 或自定义实例——`Literal[False]` 在类型层面就禁止了 `True`。

**`@Next(X)` / `@Prev(X)`**（[features.py:46](../backend/packages/harness/deerflow/agents/features.py#L46)、[features.py:58](../backend/packages/harness/deerflow/agents/features.py#L58)）：给自定义中间件类打「插在 X 旁边」的标记。装饰器把锚点记到类属性 `_next_anchor` / `_prev_anchor`，`_insert_extra` 据此定位。比「传一个 index」健壮——锚点中间件 reorder 时不用改魔法数字。

### 4.3 系统提示静态化（保 prefix-cache）

核心原则：**系统提示完全静态**。记忆和当前日期**不**写进系统提示——它们由 `DynamicContextMiddleware` 每轮作为独立消息注入首条 HumanMessage（见 [middlewares.md](middlewares.md) §5.3）。为什么？让系统提示跨用户 / 会话**完全一致** → provider 的 **prefix-cache** 能复用（省 token / 降延迟）。如果把日期 / 记忆塞进系统提示，每条消息的系统提示都不同，缓存全失效。

---

## 5. 代码走读

### 5.1 SDK 入口 `create_deerflow_agent`

[factory.py:66](../backend/packages/harness/deerflow/agents/factory.py#L66) 的三种装配模式：

1. **features 模式**（默认）：传 `features=RuntimeFeatures(...)`，调 `_assemble_from_features` 装链；feature 注入的工具（vision→`view_image`、subagent→`task`、恒定→`ask_clarification`）自动追加，按 name 去重（用户工具优先，[factory.py:122-127](../backend/packages/harness/deerflow/agents/factory.py#L122-L127)）。
2. **middleware 全接管**：传 `middleware=[...]` 就原样用——跳过 `_assemble_from_features`。不能与 `features` / `extra_middleware` 同用（互斥校验 [factory.py:100-103](../backend/packages/harness/deerflow/agents/factory.py#L100-L103)）。
3. **extra_middleware + @Next/@Prev**：往自动装配的链里插自定义中间件，用 `@Next`/`@Prev` 定位（见 §5.3）。

三模式互斥校验后，最终都汇到 `create_agent(model, tools=..., middleware=..., system_prompt=..., state_schema=..., checkpointer=..., name=...)`（[factory.py:129-137](../backend/packages/harness/deerflow/agents/factory.py#L129-L137)）。

### 5.2 `_assemble_from_features`（SDK 精简链 15 步）

[factory.py:145](../backend/packages/harness/deerflow/agents/factory.py#L145) 按**固定 15 步顺序**装链（config 入口 `build_middlewares` 的精简版）：

```
0-2.  Sandbox 基础设施（ThreadData → Uploads → Sandbox）
3.    DanglingToolCall（恒定）
4.    Guardrail（feature，无内置默认须自定义）
5.    ToolErrorHandling（恒定）
6.    Summarization（feature，须自定义实例）
7.    TodoMiddleware（plan_mode 参数）
8.    TitleMiddleware（auto_title feature）
9.    MemoryMiddleware（memory feature）
10.   ViewImageMiddleware（vision feature，同时追加 view_image 工具）
11.   SubagentLimitMiddleware（subagent feature，同时追加 task 工具）
12.   LoopDetectionMiddleware（loop_detection feature）
13.   TokenBudgetMiddleware（token_budget feature）
14.   ClarificationMiddleware（恒定末位，同时追加 ask_clarification 工具）
```

每个 feature 值三态处理：`False` 跳过 / `True` 用内置默认 / 实例直接用。例：步骤 13 token_budget（[factory.py:276-283](../backend/packages/harness/deerflow/agents/factory.py#L276-L283)）。

### 5.3 `_insert_extra`（@Next/@Prev 锚点插入算法）

[factory.py:306](../backend/packages/harness/deerflow/agents/factory.py#L306) 的算法：

1. **校验**：没人同时有 `@Next` 和 `@Prev`（[factory.py:326](../backend/packages/harness/deerflow/agents/factory.py#L326)）；
2. **冲突检测**：两个 extra 瞄准同一锚点（同向或反向）→ 报错（[factory.py:330-333](../backend/packages/harness/deerflow/agents/factory.py#L330-L333)）；
3. **无锚点的 extra** → 插在 ClarificationMiddleware 之前（[factory.py:347-350](../backend/packages/harness/deerflow/agents/factory.py#L347-L350)）；
4. **有锚点的 extra** → 迭代插入（支持 extra 之间互相锚定，[factory.py:353-379](../backend/packages/harness/deerflow/agents/factory.py#L353-L379)）；
5. 解析不了的锚点 → 检测循环依赖后报错。

**不变量**（[factory.py:292-296](../backend/packages/harness/deerflow/agents/factory.py#L292-L296)）：插完后 ClarificationMiddleware 必须仍是末位——`@Next(Clarification)` 可能把它顶离尾部，检测到就 `chain.append(chain.pop(clar_idx))` 移回末位。

### 5.4 `ThreadState` 的 5 个 reducer

[thread_state.py:128](../backend/packages/harness/deerflow/agents/thread_state.py#L128) 的 `ThreadState` 继承 langchain `AgentState`（已含 `messages`），扩展 7 个字段。几个关键 reducer：

**`merge_sandbox`（fail-closed，[thread_state.py:50](../backend/packages/harness/deerflow/agents/thread_state.py#L50)）**：

```python
def merge_sandbox(existing, new):
    if new is None: return existing
    if existing is None: return new
    if existing.get("sandbox_id") == new.get("sandbox_id"): return existing  # 幂等写，合法
    raise ValueError(f"Conflicting sandbox state updates: ...")              # 不同 id 直接抛
```

多个沙箱工具可能在同一图步懒初始化、写回**同一个** `sandbox_id`（幂等写，合法）。但**不同** `sandbox_id` 意味着隔离 / 生命周期 bug（同一线程不该出现两个沙箱）→ **fail-closed** 抛错（[thread_state.py:67](../backend/packages/harness/deerflow/agents/thread_state.py#L67)）。

**`merge_promoted`（按 catalog_hash scope，[thread_state.py:107](../backend/packages/harness/deerflow/agents/thread_state.py#L107)）**：`tool_search` 把命中的延迟工具写回 `state["promoted"]`。`catalog_hash` 变了 → 整体替换丢陈旧 names（防一条持久化的裸 name 在工具目录改名后暴露成**另一个**工具）；同 `catalog_hash` → 求 names 并集去重。

**`merge_viewed_images`（空 dict 清空，[thread_state.py:80](../backend/packages/harness/deerflow/agents/thread_state.py#L80)）**：`new={}`（空 dict）= 清空全部已查看图片，让中间件处理完后能重置状态；否则浅合并（new 覆盖 existing 同名键）。

**`merge_artifacts`**：去重保序（`dict.fromkeys`）；**`merge_todos`**：保留最后一个非 None 值（空 list = 显式清空）。

### 5.5 config 入口 `make_lead_agent`

[agent.py:99](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L99) 是 langgraph.json 注册的入口。`make_lead_agent(config)` 委托给 `_make_lead_agent`（[agent.py:106](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L106)），它做的事比 SDK factory 多：

**5.5.1 运行时配置解析**：`_get_runtime_config`（[agent.py:45](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L45)）合并 `config["configurable"]`（legacy）和 `config["context"]`（LangGraph runtime）；`_resolve_model_name`（[agent.py:54](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L54)）按「请求 → agent 配置 → 全局默认」解析模型名，未知名回退默认并告警，无模型配置直接抛错。

**5.5.2 tracing 图根注入**（[agent.py:173-178](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L173-L178)）：

```python
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    existing = config.get("callbacks") or []
    config["callbacks"] = [*existing, *tracing_callbacks]
```

tracing 回调挂**图调用根**，让一次 run 产生**一条** trace（所有节点 / LLM / 工具调用作为子 span）。关键：Langfuse handler 只有看到 `on_chain_start(parent_run_id=None)` 才会把 `langfuse_session_id` / `langfuse_user_id` 从 metadata 提到 trace 上——不在根挂的话模型是嵌套观测，handler 会剥掉 `langfuse_*` 键（详见 [tracing.md](tracing.md)）。

**配套不变量**：图里**每个** `create_chat_model(...)` 调用必须传 `attach_tracing=False`（[agent.py:189](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L189)、[agent.py:215](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L215)），否则模型级又挂一份回调 → 重复 span + propagate 失效。agent.py 模块 docstring 专门维护了调用点清单。

**5.5.3 工具策略过滤 + 延迟装配**（[agent.py:180-213](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L180-L213)）：

```python
skills_for_tool_policy = _load_enabled_skills_for_tool_policy(available_skills, ...)
raw_tools = get_available_tools(...)
filtered = filter_tools_by_skill_allowed_tools(raw_tools + extra_tools, skills_for_tool_policy)
final_tools, setup = assemble_deferred_tools(filtered, enabled=...)
```

- **工具策略过滤**（[skills/tool_policy.py](../backend/packages/harness/deerflow/skills/tool_policy.py)）：按技能的 `allowed-tools` 白名单收紧工具集。
- **延迟装配**（[tools/builtins/tool_search.py](../backend/packages/harness/deerflow/tools/builtins/tool_search.py)）：MCP 工具体量大，默认延迟（只暴露名字，要用时 `tool_search` 提升 schema）。**fail-closed**：tool_search 启用、有 MCP 工具通过过滤但没恢复出延迟集合 → 抛错，绝不静默把完整 schema 绑给模型。

`setup.deferred_names` 透传给 `build_middlewares`（挂 `DeferredToolFilterMiddleware`）和 `apply_prompt_template`（渲染 `<available-deferred-tools>` 段）。

**5.5.4 三 agent 分支**：

| 场景 | 触发 | 绑定工具 | 技能白名单 |
|------|------|----------|-----------|
| **bootstrap** | `is_bootstrap=True`（[agent.py:182](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L182)） | `+ setup_agent` | `{"bootstrap"}`（固定，求确定性） |
| **custom-agent** | `agent_name` 设了（非 bootstrap） | `+ update_agent`（[agent.py:209](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L209)） | agent 配置的 skills |
| **默认** | 无 agent_name | 不加额外 | 全部启用技能 |

bootstrap 走初始自定义 agent 创建流程，技能集故意收窄（创建流程在自定义 agent 自己的 config 存在之前必须确定性，[agent.py:40-42](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L40-L42)）；custom-agent 能经 `update_agent` 自更新 SOUL.md / config，默认 agent 看不到这个工具（不能自我修改）。

### 5.6 系统提示词条件段 `apply_prompt_template`

[prompt.py](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py) 的 `apply_prompt_template` 按 feature **条件填充**各段，未启用返回 `""`：

| 占位 | 填充条件 |
|------|----------|
| `{skills_section}` | enabled skills 非空（见 [skills.md](skills.md)） |
| `{deferred_tools_section}`（[prompt.py:474](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L474)） | tool_search 启用 |
| `{subagent_section}` / `{subagent_reminder}` / `{subagent_thinking}`（[prompt.py:233](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L233)） | subagent_enabled |
| `{soul}` + `{self_update_section}`（[prompt.py:392](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L392)） | 自定义 agent（agent_name） |
| `{acp_section}` | 配置了 ACP agent |

子代理段含动态并发上限（`MAXIMUM {n}`），让 LLM 知道每条响应最多发几个 `task` 调用——超出的会被 `SubagentLimitMiddleware` 静默丢弃。

---

## 6. 数据流（两条装配路径）

### 6.1 SDK 路径（程序化）

```
调用方准备 model + tools
        │
        ▼
create_deerflow_agent(model, tools, features=RuntimeFeatures(subagent=True, vision=True))
        │
        ├─ 互斥校验（middleware vs features vs extra_middleware）
        ├─ _assemble_from_features(feat)
        │     ├─ 按 15 步顺序 append 中间件（feature 三态处理）
        │     ├─ feature 注入工具（view_image/task/ask_clarification）追加去重
        │     ├─ _insert_extra(chain, extra_middleware)（@Next/@Prev 锚点）
        │     └─ Clarification 移回末位（不变量）
        └─ create_agent(model, tools, middleware, system_prompt, state_schema=ThreadState)
                │
                ▼
        CompiledStateGraph（调用方 .invoke() / .stream() 跑）
```

### 6.2 config 路径（LangGraph run）

```
LangGraph Server 收到 run 请求 → 传 RunnableConfig
        │
        ▼
make_lead_agent(config)
        │
        ├─ _get_runtime_config（合并 configurable + context）
        ├─ _resolve_model_name（请求→agent 配置→默认，非法回退告警）
        ├─ load_agent_config / _available_skill_names（custom-agent 分支）
        ├─ 注入 config["metadata"]（agent_name/model_name/各 flag → trace 打标）
        ├─ build_tracing_callbacks() → config["callbacks"]（图根注入）
        ├─ _load_enabled_skills_for_tool_policy
        ├─ get_available_tools + filter_tools_by_skill_allowed_tools（白名单收紧）
        ├─ assemble_deferred_tools（fail-closed 延迟装配）
        ├─ build_middlewares(config, model_name, agent_name, deferred_setup, ...)（26 槽位链）
        ├─ apply_prompt_template（条件段填充，系统提示静态）
        └─ create_agent(model=..., attach_tracing=False, tools, middleware, system_prompt, ThreadState)
                │
                ▼
        CompiledStateGraph（框架跑：调模型→工具→循环→checkpoint）
```

---

## 7. 配置

**config 入口读的 `config["configurable"]` / `config["context"]` 字段**（[agent.py:113-123](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L113-L123)）：

| 字段 | 作用 | 默认 |
|------|------|------|
| `model_name` / `model` | 指定模型名 | 全局第一个模型 |
| `thinking_enabled` | 开思考模式 | `True` |
| `reasoning_effort` | 推理强度 | `None` |
| `is_plan_mode` | 挂 TodoMiddleware | `False` |
| `subagent_enabled` | 开子代理 + SubagentLimit | `False` |
| `max_concurrent_subagents` | 子代理并发上限 | `3` |
| `is_bootstrap` | 走 bootstrap 分支 | `False` |
| `agent_name` | 自定义 agent 名（走 custom-agent 分支） | `None` |
| `app_config` | 显式 AppConfig（测试注入） | `get_app_config()` |

**SDK 入口的 `RuntimeFeatures` flag**：见 §4.2，默认 `sandbox=True` / `loop_detection=True`，其余 `False`。

---

## 8. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **models** | `create_chat_model` 解析模型；config 入口图内调用须 `attach_tracing=False` |
| **tools** | `get_available_tools` 组装工具；`assemble_deferred_tools` 延迟装配；`setup_agent`/`update_agent` 按分支绑；SDK `extra_tools` 按 name 去重追加 |
| **middlewares** | config 入口调 `build_middlewares`（26 槽位链）；SDK 入口用 `_assemble_from_features`（15 步精简链） |
| **skills** | `filter_tools_by_skill_allowed_tools` 收紧工具集；技能段进提示词；`get_enabled_skills_for_config` 加载 |
| **subagents** | 子代理段 + `task_tool`；`SubagentLimitMiddleware` 截断；`get_available_subagent_names` 列类型 |
| **memory** | 提示词不写记忆（DynamicContextMiddleware 注入）；`memory_flush_hook` 在 Summarization 前抢拍 |
| **agents_config** | `validate_agent_name` / `load_agent_config` 驱动 custom-agent 分支 |
| **tracing** | `build_tracing_callbacks` 图根注入 |
| **uploads** | `UploadsMiddleware`（链步骤 3，SDK + config 都装） |

---

## 9. 设计动机分析

### 9.0 核心设计动机表

| 设计 | 为什么 | 不这么设计会怎样 |
|------|--------|------------------|
| **双入口**（SDK + config） | SDK 给程序化、config 给框架；前者纯参数后者读配置 | 一个入口要么没法程序化测、要么没法注册成图工厂 |
| **声明式 `RuntimeFeatures`** | 把「开哪些行为」用一组 flag 表达，三态 | 传一长串中间件参数，调用点又长又易错 |
| **`@Next`/`@Prev` 锚点** | 自定义中间件声明插哪个锚点旁边 | 传 index：锚点 reorder 就错位 |
| **Clarification 强制末位** | 它用 `Command(goto=END)` 中断，排后面就跑不到 | 中断后记忆没入队、标题没生成 |
| **`merge_sandbox` fail-closed** | 不同 sandbox_id = 隔离 bug | 静默选一个 → 两工具落不同沙箱、产出对不上、难排查 |
| **`merge_promoted` catalog_hash scope** | 防陈旧提升在目录改名后误暴露 | 旧裸 name 命中改名后的另一个工具 |
| **tracing 挂图根 + `attach_tracing=False`** | 一次 run 一条 trace + Langfuse propagate 到位 | 模型级嵌套挂 → 重复 span + session/user_id 到不了 trace |
| **系统提示静态化** | 保 prefix-cache 复用 | 日期/记忆进系统提示 → 每条消息缓存全失效 |
| **工具策略 + 延迟装配 fail-closed** | 目录绝不暴露 agent 无权/不需要的工具 | 把完整 MCP schema 绑给模型 → token 浪费 + 误调 |
| **bootstrap 窄技能集** | 创建流程在 agent config 存在前要确定性 | 技能集不确定 → 创建流程不可复现 |

### 9.1 为什么 `merge_sandbox` 要 fail-closed

同一线程只该有一个沙箱（per-thread isolation 是沙箱模型的基础不变量，见 [sandbox.md](sandbox.md)）。多个沙箱工具在同一图步懒初始化时，可能各自发出 `Command(update={"sandbox": {"sandbox_id": X}})`——如果 X 都相同，是幂等写，reducer 返回 existing，合法。但如果 X **不同**，说明出了隔离 / 生命周期 bug（两个沙箱实例混进了同一线程）。

**不这么设计会怎样**：如果 reducer 静默选其中一个（比如「保留 existing」），那么发出 `new` 的那个工具后续操作可能落在它以为的沙箱（new）里，而 state 记的是另一个（existing）——agent 的两个工具操作落在**不同沙箱**，产出对不上，而且这种 bug 极难排查（表面没报错）。fail-closed 抛错让 bug **当场暴露**，是安全相关状态的正确默认。

### 9.2 为什么 tracing 必须挂图根 + 配套 `attach_tracing=False`

Langfuse 的 `langchain.CallbackHandler` 有个机制：只有当它看到 `on_chain_start(parent_run_id=None)`（即「这是顶层链」）时，才把 `config["metadata"]` 里的 `langfuse_session_id` / `langfuse_user_id` **提升**到 trace 层。`parent_run_id=None` 只在「图调用根」出现。

**不这么设计会怎样**：
- 如果回调挂在**模型级**（每个 `create_chat_model` 自带），那么模型是**嵌套**在图里的子链，`parent_run_id` 不是 `None`，handler 认为它不是顶层 → **不提升** `langfuse_*` 键 → session/user_id 永远到不了 trace，在 Langfuse UI 里这些 run 飘成「无 session」的孤儿。
- 如果**既挂图根又让模型自带**（没传 `attach_tracing=False`），会发**重复 span**（图根一个、模型一个），trace 里同一调用出现两次。

所以正确做法：图根挂一份，所有图内 `create_chat_model` 传 `attach_tracing=False` 关掉模型级那份。这就是 agent.py 模块 docstring 维护「调用点清单」的原因——每新增一个图内 `create_chat_model` 都得加进清单并传该 flag。

### 9.3 为什么 `@Next`/`@Prev` 比传 index 好

插入自定义中间件最朴素的方式是「传一个位置 index」。问题：链里中间件顺序会演进（加新中间件、reorder）。今天你算出「插在 index 7」，明天上游在 index 3 加了一个中间件，你的 index 7 就错位了——而且这种错位**静默**（不报错，中间件落到了错误的位置，行为悄悄变坏）。

`@Next(ToolErrorHandlingMiddleware)` 用**类**做锚点：不管 ToolErrorHandling 在链里排第几，「插在它后面」始终正确。锚点中间件 reorder 不影响相对位置。解析不了（锚点不在链里）会**报错**而不是静默落错。

### 9.4 为什么系统提示要静态化

LLM 提供商的 prefix-cache：两次请求**开头逐 token 一致**，第二次跳过这部分的 KV 计算。系统提示是请求里**最开头**的一大段（几千 token），如果它完全静态，每个用户的每次请求都能命中缓存 → 大幅省算力、降首 token 延迟。

记忆和日期是**动态**的（每个用户记忆不同、每天日期变）。如果塞进系统提示，开头就变了 → 缓存失效。所以 DynamicContextMiddleware 把它们作为独立消息注入首条 HumanMessage（[middlewares.md](middlewares.md) §5.3 用 ID-swap 技术），**系统提示一字不动**，缓存照常命中。

---

## 10. 实现差异（vs 上游 deer-flow 源码）

对照两侧 `backend/packages/harness/deerflow/agents/`（7 个文件一一对应），**剥 docstring/comment 后判逻辑差**。结论：**mini 是忠实移植——两个入口的核心装配逻辑零漂移；唯一显著差异是 `lead_agent/agent.py` 因「搬移」比上游小很多（build_middlewares + 两个工厂函数迁出到 middlewares 包），其余文件是注解风格 / 内联别名 / lazy-import 等组织差异，逻辑等价**。

### 10.1 头条差异：`lead_agent/agent.py` 搬移（mini 1043 vs 上游 1927 stripped）

上游 `agent.py` 里直接定义了三个大函数：`build_middlewares`（~130 行，26 槽位链装配）、`_create_summarization_middleware`（摘要工厂）、`_create_todo_list_middleware`（todo 工厂）。

**mini 把它们迁出了 agent.py**：
- `build_middlewares` → `agents/middlewares/__init__.py`（见 [middlewares.md](middlewares.md) §10.4）；
- `_create_summarization_middleware` → `agents/middlewares/summarization_middleware.py`（见 [middlewares.md](middlewares.md) §10.3）；
- `_create_todo_list_middleware` → 内联进 `build_middlewares` 的步骤 14。

所以 mini 的 agent.py stripped 只 1043、上游 1927，差值 −884 **全是搬走的这三个函数**。`make_lead_agent` / `_make_lead_agent` 的核心装配逻辑（config 解析 / tracing 注入 / 工具策略 / 三分支）**逐行一致**，只是改为调用迁出后的 `build_middlewares`。这是教学内聚的整理（装配入口与中间件实现同居一个包），不是逻辑裁剪。

### 10.2 `factory.py`：0 逻辑差（仅注解风格）

stripped diff 只显示类型注解形式差异：上游用直接类型（`model: BaseChatModel`、`-> CompiledStateGraph`、`-> tuple[list[AgentMiddleware], list[BaseTool]]`）；mini 用 `from __future__ import annotations` + TYPE_CHECKING 字符串注解（被 stripper 折成 `S`）。装配逻辑、15 步顺序、三态处理、互斥校验、`_insert_extra` 算法**全部一致**。

### 10.3 `thread_state.py`：0 逻辑差（内联 vs 别名 + 类顺序）

- 上游定义类型别名 `SandboxStateField = Annotated[NotRequired[SandboxState | None], merge_sandbox]` 再用 `sandbox: SandboxStateField`；**mini 内联**为 `sandbox: Annotated[NotRequired[SandboxState | None], merge_sandbox]`（[thread_state.py:136](../backend/packages/harness/deerflow/agents/thread_state.py#L136)）。
- `PromotedTools` 类定义位置不同（mini 靠前、上游靠后）——仅顺序。
- 5 个 reducer（`merge_sandbox` / `merge_artifacts` / `merge_viewed_images` / `merge_todos` / `merge_promoted`）**逐行一致**。`ThreadState` 字段集相同。

### 10.4 `features.py`：逐字节相同

stripped 212 = 212，完全一致。`RuntimeFeatures` 9 个字段、`@Next`/`@Prev` 装饰器逻辑一字不差。

### 10.5 `lead_agent/prompt.py`：0 逻辑差（lazy-import + 注解 + 一处包装）

- 上游把 `load_agent_soul` / `get_or_new_skill_storage` / `Skill` / `get_available_subagent_names` / `get_deferred_tools_prompt_section` 都在文件顶部 import；**mini 把部分 import 延迟到函数体内**（如 `_load_enabled_skills_sync` 内部 import `get_or_new_skill_storage`）——为避免循环导入。等价。
- 上游有个 `_get_enabled_skills()` 薄包装（只调 `get_cached_enabled_skills()`）；mini 直接调 `get_cached_enabled_skills()`。等价。
- 注解形式（`list[Skill]` vs `list`）。等价。
- **关键**：系统提示模板字符串（`{soul}` / `{skills_section}` / `{subagent_section}` / `{deferred_tools_section}` / `{acp_section}` 各段内容）**逐字一致**。

### 10.6 其余两个文件

- `agents/__init__.py`：mini 66 vs 上游 54（+12），mini 多导出公共符号（API 面），无逻辑差（同前几篇规律）。
- `lead_agent/__init__.py`：10 = 10，逐字节相同。

**测试覆盖**：`test/test_agent.py`（57 测试）+ `test/test_agent_with_middlewares.py`（6 测试），共 **63 个 agent 装配相关测试函数**，覆盖双入口、features 三态、`@Next`/`@Prev` 插入、reducer、三 agent 分支。

---

## 11. 排错 FAQ

- **「agent 跑起来但 tracing 看不到 session_id / user_id」**：检查图内某个 `create_chat_model` 是不是漏了 `attach_tracing=False`（agent.py docstring 维护调用点清单）。漏传会发重复 span + Langfuse propagate 失效。
- **「自定义中间件插不进去 / 报 Cannot resolve」**：`@Next(X)` 的 X 必须是链里已存在的中间件类。extra 之间互相锚定的话，注意插入顺序（迭代解析，支持 cross-anchoring）。
- **「两个自定义中间件都 `@Next` 同一个锚点」**：冲突，报 `ValueError`。让它们互相锚定（A `@Next(B)`，B `@Next(锚点)`）。
- **「bootstrap agent 技能不对」**：bootstrap 固定 `{"bootstrap"}` 白名单（求确定性），不走 agent 配置。
- **「sandbox reducer 报 Conflicting sandbox state」**：同一线程出现了不同 sandbox_id——是隔离 / 生命周期 bug，查沙箱获取逻辑，**别把这个 fail-closed 改成静默选一个**。
- **「延迟工具提升后还看不到 schema」**：`catalog_hash` 变了会丢陈旧提升（防改名误用）；`DeferredToolFilterMiddleware` 据图状态 `promoted` 重新暴露，确认 tool_search 真的提升过。
- **「SDK agent 没装 TokenBudget / 没装某中间件」**：SDK 用 `RuntimeFeatures` flag 控制，默认 `token_budget=False`；要开就 `RuntimeFeatures(token_budget=True)`。
- **「系统提示里看不到记忆 / 日期」**：不是 bug——设计如此。记忆/日期由 DynamicContextMiddleware 注入首条 HumanMessage，不进系统提示（保 prefix-cache）。

---

**下一篇**：[runs.md](runs.md)（RunManager 状态机 + run_agent 后台执行 + worker）——本模块的 `make_lead_agent` 是运行时 worker 调用的图工厂，[runs.md](runs.md) 把它包进 RunManager + worker 形成完整的「创建 run → 跑 agent → 流式回播」链路。
