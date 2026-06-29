# 25. agents.md — Agent 装配（SDK + config 双入口，把模型/工具/中间件/提示词拼成一张可跑的图）

> **一句话定位**：本模块是「总装车间」——把前面所有模块（models / tools / sandbox / skills /
> memory / middlewares …）按两种入口拼成一张能跑的 LangGraph 图。`create_deerflow_agent`
> 是 **SDK 入口**（纯 Python 参数，给程序化用）；`make_lead_agent` 是 **config 入口**
>（读运行时配置，是 LangGraph Studio / Gateway 注册的图工厂）。两者背后共用同一套装配逻辑。

读完 [middlewares.md](middlewares.md)（懂了「23 步中间件链是 agent 的行为骨架」）再看本篇
最省事——本篇回答「这条链是谁、按什么顺序、在哪儿组装出来的」。中间件链本身在
`build_middlewares`（M16 落地，见 [middlewares.md](middlewares.md)）；本篇讲它的两个调用方
（factory + lead_agent）以及它们还做了什么（模型解析 / 工具过滤 / 提示词拼接 / tracing 注入）。

> **M17 全维重审（2026-06-28）**：逐文件 diff 最新上游（`__init__` / `factory` / `features` /
> `thread_state` / `lead_agent/{agent,prompt}` / `lead_agent/__init__`）。差异**几乎全是中英 docstring
> 翻译 + 类型注解引号（`from __future__ import annotations`）+ mini 把 `build_middlewares` 从
> `lead_agent/agent.py` 拆到 `agents/middlewares/__init__.py` 的结构选择**，剥 docstring 后逻辑零漂移。
> 补 **1 项半挂对齐**：SDK 路径 `create_deerflow_agent` 缺 TokenBudget——M16 给 lead 链（步骤 #23）
> 加了 `TokenBudgetMiddleware`，但 SDK 的 `RuntimeFeatures` 漏了 `token_budget` 字段、`factory.py`
> 漏了步骤 [13]。本次补齐：`features.py` 加 `token_budget` flag + `factory.py` 加步骤 [13]（Clarification
> 顺延 [14]）。+ 3 项 hermetic 测试（`test_agent_with_middlewares.py`：token_budget=True 进链且在
> Clarification 前 / 默认 False 不误装 / 自定义实例直用）。deferred：**#3592 guaranteed memory injection**
>（跨 prompt.py+memory_config+lead_agent/prompt 3 模块的新增特性，非 bug，归后续专项）。

---

## 0. 这个模块解决什么问题

「跑一个 agent」需要把四样东西凑齐：

1. **模型**（`BaseChatModel`）——谁来思考；
2. **工具**（`list[BaseTool]`）——能干什么；
3. **中间件**（`list[AgentMiddleware]`）——横切行为（防爆 / 循环检测 / 安全拦截 …）；
4. **系统提示词**（`str`）——告诉模型它的角色 / 规则。

LangGraph 的 `create_agent(model, tools, middleware, system_prompt, ...)` 把这四样编成一张
状态图（`CompiledStateGraph`）。但「凑齐这四样」本身有大量决策：用哪个模型？开不开子代理？
哪些工具对当前 agent 可见？系统提示要不要塞技能段 / 子代理段？这些问题不能堆进 `create_agent`
（它是底层原语），所以本模块提供两层封装：

- **`create_deerflow_agent`**（[factory.py](../backend/packages/harness/deerflow/agents/factory.py)）：
  SDK 级，用 `RuntimeFeatures` 声明「开哪些行为」，工厂自动装链。给程序化场景用（测试、
  内嵌 client、自定义编排）。
- **`make_lead_agent`**（[lead_agent/agent.py](../backend/packages/harness/deerflow/agents/lead_agent/agent.py)）：
  config 级，从 `RunnableConfig` 解析一切（模型名 / agent 名 / plan_mode / subagent …），
  还要做工具策略过滤、延迟工具装配、tracing 注入。这是 LangGraph 注册的正式入口。

## 1. RuntimeFeatures + @Next/@Prev（声明式 feature flag）

[features.py](../backend/packages/harness/deerflow/agents/features.py) 是纯数据，没有 IO：

```python
@dataclass
class RuntimeFeatures:
    sandbox: bool | AgentMiddleware = True       # 沙箱基础设施（ThreadData+Uploads+Sandbox）
    memory: bool | AgentMiddleware = False       # 记忆
    summarization: Literal[False] | AgentMiddleware = False  # 摘要（要传模型，无内置默认）
    subagent: bool | AgentMiddleware = False     # 子代理
    vision: bool | AgentMiddleware = False       # 图片
    auto_title: bool | AgentMiddleware = False   # 自动标题
    guardrail: Literal[False] | AgentMiddleware = False     # 护栏（无内置默认）
    loop_detection: bool | AgentMiddleware = True  # 循环检测
    token_budget: bool | AgentMiddleware = False   # 单 run token 预算（M17 重审补）
```

每个 feature 接受三类值：`True`（用内置默认中间件）/ `False`（关）/ 一个 `AgentMiddleware`
实例（自定义替换）。`summarization` 和 `guardrail` 没有内置默认——因为摘要要传一个模型实例、
护栏要传 provider，所以这俩只能 `False` 或自定义实例。

**`@Next` / `@Prev` 装饰器**：给自定义中间件声明「插在链里哪个锚点旁边」。例如：

```python
@Next(ToolErrorHandlingMiddleware)   # 插在 ToolErrorHandling 之后
class MyMiddleware(AgentMiddleware): ...
```

装饰器把锚点记到类属性 `_next_anchor` / `_prev_anchor`，`_insert_extra` 据此定位插入。
这比「传一个 index」健壮——锚点中间件 reorder 时不用改魔法数字。

## 2. thread_state 类型化 reducer（为什么 fail-closed）

[thread_state.py](../backend/packages/harness/deerflow/agents/thread_state.py) 定义图状态。reducer
是 LangGraph 的合并协议：同一图步里多个节点都写同一个 key 时，框架调 reducer 合并。几个关键
reducer：

### merge_sandbox（fail-closed，红线 #16）

```python
def merge_sandbox(existing, new):
    if new is None: return existing
    if existing is None: return new
    if existing.get("sandbox_id") == new.get("sandbox_id"): return existing
    raise ValueError(f"Conflicting sandbox state updates: ...")  # ← 不同 id 直接抛
```

多个沙箱工具可能在同一图步懒初始化、经 `Command(update=...)` 写回**同一个** `sandbox_id`
（幂等写，合法）。但**不同** `sandbox_id` 意味着隔离 / 生命周期 bug（同一线程不该出现两个沙箱）。
这种情况 **fail-closed**——抛错而不是静默选一个。为什么？静默选一个可能让 agent 的两个工具
操作落在不同沙箱，产出对不上、还难排查。抛错让 bug 当场暴露。

### merge_promoted（按 catalog_hash scope）

`tool_search` 把命中的延迟工具写回 `state["promoted"]`。reducer 按 `catalog_hash` scope：

- `catalog_hash` 变了 → 整体替换，丢陈旧 names（防止一条持久化的裸 name 在工具目录改名
  后暴露成**另一个**工具）；
- 同 `catalog_hash` → 求 names 并集去重。

为什么这么谨慎？延迟工具是按**名字**提升的。如果工具目录改了（某 MCP 工具改名 / 漂移），
一条旧的提升记录里的裸名字可能命中一个完全不同的工具。`catalog_hash` 是当前工具目录的指纹，
变了就认为旧提升失效。

### merge_viewed_images（空 dict 清空）

特例：`new={}`（空 dict）= 清空全部已查看图片。让中间件处理完后能重置状态。否则是浅合并
（new 覆盖 existing 同名键）。

## 3. create_deerflow_agent（SDK 入口，features 驱动）

[factory.py](../backend/packages/harness/deerflow/agents/factory.py) 的 `create_deerflow_agent`：
纯参数化，不读 config。两种模式：

### features 模式（默认）

```python
agent = create_deerflow_agent(
    model=my_model,
    tools=[my_tool],
    features=RuntimeFeatures(subagent=True, vision=True),
)
```

`_assemble_from_features` 按**固定 15 步顺序**装链（与 lead_agent 对齐的精简版）：

```
0-2.  Sandbox 基础设施（ThreadData → Uploads → Sandbox）
3.    DanglingToolCall（恒定）
4.    Guardrail（feature）
5.    ToolErrorHandling（恒定）
6.    Summarization（feature）
7.    TodoMiddleware（plan_mode 参数）
8.    TitleMiddleware（auto_title feature）
9.    MemoryMiddleware（memory feature）
10.   ViewImageMiddleware（vision feature）
11.   SubagentLimitMiddleware（subagent feature）
12.   LoopDetectionMiddleware（loop_detection feature）
13.   TokenBudgetMiddleware（token_budget feature，M17 重审补）
14.   ClarificationMiddleware（恒定末位）
```

每个 feature 值按 `False`（跳过）/ `True`（内置默认）/ 实例（自定义）三态处理。feature 注入
的**工具**（vision→`view_image`、subagent→`task`、恒定→`ask_clarification`）自动追加，按 name
去重（用户工具优先）。

### middleware 全接管模式

传 `middleware=[...]` 就原样用这条链——跳过 `_assemble_from_features`。不能与 `features` /
`extra_middleware` 同用（互斥校验，报 `ValueError`）。

### extra_middleware + @Next/@Prev

`extra_middleware` 让你往自动装配的链里插自定义中间件，用 `@Next`/`@Prev` 定位。`_insert_extra`
算法（见 [factory.py](../backend/packages/harness/deerflow/agents/factory.py) `_insert_extra`）：

1. 校验：没人同时有 `@Next` 和 `@Prev`；
2. 冲突检测：两个 extra 瞄准同一锚点（同向或反向）→ 报错；
3. 无锚点的 extra → 插在 Clarification 之前；
4. 有锚点的 extra → 迭代插入（支持 extra 之间互相锚定）；
5. 解析不了的锚点 → 报错。

**不变量**：插完后 ClarificationMiddleware 必须仍是末位（`@Next(Clarification)` 可能把它顶离尾部，
`_assemble_from_features` 检测到就把它移回末位）。

## 4. make_lead_agent（config 入口，LangGraph 图工厂）

[lead_agent/agent.py](../backend/packages/harness/deerflow/agents/lead_agent/agent.py) 的
`make_lead_agent(config)` 是 langgraph.json 注册的入口。它做的事比 factory 多——除了调
`build_middlewares` 组装中间件链，还要：

### 4.1 运行时配置解析（`_get_runtime_config` + `_resolve_model_name`）

合并 `config["configurable"]`（legacy）和 `config["context"]`（LangGraph runtime）。模型名解析：
请求 → agent 配置 → 全局默认，未知名回退默认并告警。无模型配置直接抛错。

### 4.2 tracing 图根注入（红线 #17）

```python
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    config["callbacks"] = [*existing, *tracing_callbacks]
```

tracing 回调挂在**图调用根**，让一次 run 产生**一条** trace（所有节点 / LLM / 工具调用作为子 span）。
关键：Langfuse handler 只有看到 `on_chain_start(parent_run_id=None)` 才会把
`langfuse_session_id` / `langfuse_user_id` 从 metadata 提到 trace 上——不在根挂的话模型是嵌套
观测，handler 会剥掉 `langfuse_*` 键。

**配套不变量**：图里**每个** `create_chat_model(...)` 调用必须传 `attach_tracing=False`
（否则模型级又挂一份回调 → 重复 span + propagate 失效）。agent.py 模块 docstring 专门记录了
这四个调用点（bootstrap agent / 默认 agent / summarization 中间件 / TitleMiddleware）。

> **子代理也镜像这套**（#3611，见 [tracing.md](tracing.md)）：`subagents/executor.py::_aexecute`
> 在子代理图根同样挂 `build_tracing_callbacks()` + 注入 `inject_langfuse_metadata`（父 `thread_id`
> → session、捕获 `user_id` → user、`subagent:<归一化名>` → trace_name），让子代理 trace 归属
> 父对话而非飘成独立 session。

### 4.3 工具策略过滤 + 延迟装配

```python
skills_for_tool_policy = _load_enabled_skills_for_tool_policy(available_skills, ...)
raw_tools = get_available_tools(...)
filtered = filter_tools_by_skill_allowed_tools(raw_tools + extra_tools, skills_for_tool_policy)
final_tools, setup = assemble_deferred_tools(filtered, enabled=...)
```

- **工具策略过滤**（[skills/tool_policy.py](../backend/packages/harness/deerflow/skills/tool_policy.py)）：
  按技能的 `allowed-tools` 白名单收紧工具集——技能能限制 agent 只用某些工具。
- **延迟装配**（[tools/builtins/tool_search.py](../backend/packages/harness/deerflow/tools/builtins/tool_search.py)）：
  MCP 工具体量大，默认延迟（只暴露名字，要用时 `tool_search` 提升完整 schema）。**fail-closed**：
  tool_search 启用、有 MCP 工具通过过滤但没恢复出延迟集合 → 抛错，绝不静默把完整 schema 绑给模型。

`setup.deferred_names` 透传给 `build_middlewares`（挂 `DeferredToolFilterMiddleware`）和
`apply_prompt_template`（渲染 `<available-deferred-tools>` 段）。

### 4.4 bootstrap 分支 vs custom-agent 分支 vs 默认

| 场景 | 触发 | 绑定工具 | 技能白名单 |
|------|------|----------|-----------|
| **bootstrap** | `is_bootstrap=True` | `+ setup_agent` | `{"bootstrap"}`（固定，求确定性） |
| **custom-agent** | `agent_name` 设了（非 bootstrap） | `+ update_agent` | agent 配置的 skills |
| **默认** | 无 agent_name | 不加额外 | 全部启用技能 |

- **bootstrap**：走初始自定义 agent 创建流程。技能集故意收窄（创建流程在自定义 agent 自己的
  config 存在之前必须确定性）。绑 `setup_agent`（写 SOUL.md + config.yaml）。
- **custom-agent**：自定义 agent 能经 `update_agent` 自更新 SOUL.md / config。默认 agent 看不到
  这个工具（不能自我修改）。

### 4.5 metadata 注入

往 `config["metadata"]` 注 `agent_name` / `model_name` / 各 flag，供 LangSmith trace 打标。

## 5. 系统提示词条件段（为什么保持静态）

[lead_agent/prompt.py](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py) 的
`apply_prompt_template` 按 feature **条件填充**各段，未启用返回 `""`：

| 占位 | 填充条件 |
|------|----------|
| `{skills_section}` | enabled skills 非空（M14） |
| `{deferred_tools_section}` | tool_search 启用（M20） |
| `{subagent_section}` / `{subagent_reminder}` / `{subagent_thinking}` | subagent_enabled |
| `{soul}` + `{self_update_section}` | 自定义 agent（agent_name，M22） |
| `{acp_section}` | 配置了 ACP agent |

**核心原则：系统提示完全静态。** 记忆和当前日期**不**写进系统提示——它们由 `DynamicContextMiddleware`
每轮作为 `<system-reminder>` 注入**首条 HumanMessage**。为什么？让系统提示跨用户 / 会话**完全一致**
→ provider 的 **prefix-cache** 能复用（系统提示不变，缓存命中省 token / 延迟）。如果把日期 /
记忆塞进系统提示，每条消息的系统提示都不同，缓存全失效。

子代理段含动态并发上限（`MAXIMUM {n}`），让 LLM 知道每条响应最多发几个 `task` 调用——超出的
会被 `SubagentLimitMiddleware` 静默丢弃。

## 6. 与其它模块的关系

| 模块 | 关系 |
|------|------|
| **models** | `create_chat_model` 解析模型；图内调用须 `attach_tracing=False` |
| **tools(M15)** | `get_available_tools` 组装工具；`assemble_deferred_tools` 延迟装配；`setup_agent`/`update_agent` 按上下文绑 |
| **middlewares(M16)** | `build_middlewares` 23 步链由 `make_lead_agent` 调用组装 |
| **skills(M14)** | `filter_tools_by_skill_allowed_tools` 收紧工具集；技能段进提示词 |
| **subagents(M11)** | 子代理段 + `task_tool`；`get_available_subagent_names` 列类型 |
| **memory(M13)** | 提示词不写记忆（DynamicContextMiddleware 注入）；`memory_flush_hook` 抢拍 |
| **agents_config(M22)** | `validate_agent_name` / `load_agent_config` / `load_agent_soul` 驱动 custom-agent 分支 |
| **tracing(M12)** | `build_tracing_callbacks` 图根注入 |
| **uploads(M23)** | `UploadsMiddleware`（链步骤 3） |

## 7. 设计要点回顾

1. **双入口**：SDK（`create_deerflow_agent`，features 驱动）+ config（`make_lead_agent`，
   RunnableConfig 驱动）。前者程序化、后者是 LangGraph 注册入口。
2. **声明式 feature flag**：`RuntimeFeatures` 把「开哪些行为」用一组 flag 表达，`True`/`False`/
   实例三态。比传一长串中间件参数清晰。
3. **`@Next`/`@Prev` 锚点插入**：自定义中间件声明插在哪个锚点旁边，比 index 健壮（reorder 不破）。
4. **Clarification 永远末位**：`_assemble_from_features` 插完 extra 后检测并把它移回末位（红线 #14）。
5. **fail-closed reducer**：`merge_sandbox` 不同 id 抛错（红线 #16）；`merge_promoted` catalog_hash
   变了丢陈旧 names。
6. **tracing 图根注入**：回调挂在图根，配套所有图内 `create_chat_model` 传 `attach_tracing=False`
   （红线 #17）。
7. **工具策略 + 延迟装配**：技能白名单收紧 + MCP 工具延迟（fail-closed），目录绝不暴露 agent
   无权用的工具。
8. **三 agent 分支**：bootstrap（创建流程，窄技能）/ custom-agent（自更新）/ 默认。
9. **系统提示静态化**：记忆 / 日期不进系统提示（DynamicContextMiddleware 注入），保 prefix-cache 复用。
10. **条件段 gating**：6 个占位按 feature 填充，未启用返回 `""`。

## 8. 排错 FAQ

- **「agent 跑起来但 tracing 看不到 session_id / user_id」**：检查图内某个 `create_chat_model`
  是不是漏了 `attach_tracing=False`（agent.py docstring 维护调用点清单）。漏传会发重复 span +
  Langfuse propagate 失效。
- **「自定义中间件插不进去 / 报 Cannot resolve」**：`@Next(X)` 的 X 必须是链里已存在的中间件类。
  extra 之间互相锚定的话，注意插入顺序（迭代解析，支持 cross-anchoring）。
- **「两个自定义中间件都 `@Next` 同一个锚点」**：冲突，报 `ValueError`。让它们互相锚定（A `@Next(B)`，
  B `@Next(锚点)`）。
- **「bootstrap agent 技能不对」**：bootstrap 固定 `{"bootstrap"}` 白名单（求确定性），不走 agent 配置。
- **「sandbox reducer 报 Conflicting sandbox state」**：同一线程出现了不同 sandbox_id——是隔离 /
  生命周期 bug，查沙箱获取逻辑，别把这个 fail-closed 改成静默选一个。
- **「延迟工具提升后还看不到 schema」**：`catalog_hash` 变了会丢陈旧提升（防改名误用）；
  `DeferredToolFilterMiddleware` 据图状态 `promoted` 重新暴露，确认 tool_search 真的提升过。

---

**下一篇**：[README.md](README.md) 待写表里下一个是 `runs.md` / `runtime_store.md`（M18 / M19，
运行管理 + 集成）——本模块的 `make_lead_agent` 是运行时 worker 调用的图工厂，M18 把它包进
RunManager + worker 形成完整的「创建 run → 跑 agent → 流式回播」链路。
