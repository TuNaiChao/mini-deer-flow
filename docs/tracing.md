# 16. tracing.md — 链路追踪（LangSmith / Langfuse 图根注入）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d · **2026-07-05 复审**（更面向小白 + 加「实现差异 vs 上游 deer-flow 源码」）

> **一句话定位**：tracing 让一次 agent run 产**一条完整 trace**——把所有 node 调用、LLM 调用、
> tool 调用串成一棵子 span 树，在 LangSmith / Langfuse 面板上可观测、可回放、可调试。
> 本模块只管两件事：**构造追踪回调**（`build_tracing_callbacks`）+ **注入 Langfuse 元数据**（`inject_langfuse_metadata`）。

> **配套代码**：[tracing/](../backend/packages/harness/deerflow/tracing/)（3 个文件 196 行：`factory.py` 77 + `metadata.py` 100 + `__init__.py` 19）+ [config/__init__.py](../backend/packages/harness/deerflow/config/__init__.py)（provider 开关 + 凭证，env 驱动）+ 4 个调用点（见 §4.3 矩阵）
> **配套测试**：[test/test_tracing.py](../test/test_tracing.py)（36 个 hermetic 测试）+ [test/test_subagents.py](../test/test_subagents.py) 的 `TestSubagentTracingWiring`（子代理注入点）
> **参考**：无 deerflow-book 章节（这是 mini 自己的可观测性模块；trace/span/callback 是 LangChain 通用概念，非借自 book）
> 本文面向「刚接触可观测性 / 分布式追踪的小白」。读完 [models.md](models.md)（懂了 `create_chat_model` 与 `attach_tracing`）再看本篇最省事——tracing 的核心就是「回调挂在哪一层」，而 models 的 `attach_tracing` 开关正是这个分层的旋钮。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 没有 tracing，agent 出问题（答非所问 / token 暴涨 / 循环卡死）为什么只能靠猜？trace / span / callback 各是什么？
2. 为什么回调必须在**图根**注入，不能每个 LLM 调用各挂各的？（碎片 trace vs 一棵完整树）
3. `build_tracing_callbacks` 和 `inject_langfuse_metadata` 各干什么？为什么是**两个** helper 而不是一个？
4. 4 个调用点（lead agent / run worker / 子代理 / 独立调用方）各调哪个 helper？为什么主 run 的回调构造在 lead agent、元数据注入却在 worker？
5. **图内 `attach_tracing=False` 不变量**是什么？违反会怎样？（重复 span + langfuse 元数据被剥离）独立调用方为什么反过来要 `attach_tracing=True`？
6. Langfuse 的 `session_id` / `user_id` / `trace_name` / `tags` 各映射自什么？为什么用 `setdefault`（调用方优先）？
7. 子代理的 trace 为什么会「飘成独立 session」？怎么修（注入父 thread_id / user_id / `subagent:<归一化名>`，源码注释标 #3611）？
8. 不配任何 tracing 会有开销吗？`build_tracing_callbacks` 抛 RuntimeError 和 models 的 `_maybe_build_tracing_callbacks` 返回 `[]` 为什么不矛盾？

---

## §1 为什么需要链路追踪（痛点）

agent 是个「黑盒」：一个用户问题进来，里面跑了一堆 LLM 调用、tool 调用、中间推理。出问题时（答非所问、token 暴涨、循环卡死）你只能看最后那句话，**看不到中间发生了什么**。

链路追踪（tracing）解决这个：把一次 run 拆成一棵 **span 树**——

```
run（根 trace）
├── agent node
│   ├── LLM call（input/output/token）
│   ├── tool call: bash
│   │   └── （命令输出）
│   ├── LLM call
│   └── tool call: task（子代理）
│       └── 子代理 run（又一棵子树）
└── ...
```

在 LangSmith / Langfuse 面板上，你能展开每一层，看输入输出、耗时、token、报错。**没有它，生产排障基本靠猜。**

---

## §2 零基础名词（先认这些词）

- **trace**：一次完整 run（一个用户问题从进到出）。**类比**：一个快递单号。
- **span**：trace 里的一个步骤（一次 LLM 调用、一次 tool 调用、一个 node）。**类比**：每一段物流轨迹（揽收→分拣→运输→派送）。
- **callback**：LangChain 的回调钩子（`BaseCallbackHandler`）。LangSmith / Langfuse 各提供一个 callback handler，挂在 run 上后，LangChain 在每次 LLM/tool/chain 调用前后自动上报 span。**类比**：每到一个节点就扫码上报的那个扫码动作。
- **provider**：追踪平台。mini 支持两个：**LangSmith**（LangChain 官方，`LangChainTracer`）/ **Langfuse**（开源可自建，`langfuse.langchain.CallbackHandler`）。靠环境变量开关，都不配 = 零开销。
- **图根注入（graph-root injection）**：在调 `agent.astream(...)` / `ainvoke(...)` 前，把 callbacks append 进 `config["callbacks"]`。这样整棵 span 树挂在**一个根 trace** 下。
- **`RunnableConfig`**：LangGraph/LangChain 传给图调用的配置字典，里面的 `callbacks`（回调列表）和 `metadata`（元数据）是 tracing 操作的两个关键字段。
- **环境变量（env var）**：操作系统级的配置项，程序启动时从环境读取。tracing 靠它开关——设 `LANGSMITH_TRACING=true` 就启用 LangSmith，不设就不用。**类比**：像电箱里的开关，拨哪边决定哪条电路通。
- **SDK / pip 包**：第三方库，要 `pip install` 才能用。`langfuse` 是一个 SDK 包；`langchain_core.tracers` 随 langchain 自带、不用单独装。
- **面板（dashboard）**：LangSmith / Langfuse 提供的**网页界面**——trace 上报后，你在浏览器里展开 span 树看每一步的输入输出、耗时、token、报错。本模块负责「把 trace 造出来并上报」，面板是「看上报结果」的地方。
- **soft-load（软加载）**：某个可选包缺了也不让程序崩——import 失败就降级（功能少一块但不报错）。`langfuse` 就是 soft-load：没装它，LangSmith 路径照常工作。

---

## §3 整体结构（两个 helper，职责正交）

```
tracing/
├── __init__.py   (19 行)  导出 3 个函数
├── factory.py    (77 行)  build_tracing_callbacks()：按启用 provider 构造回调列表（未配置返回 []）
└── metadata.py  (100 行)  build_langfuse_trace_metadata() + inject_langfuse_metadata()（session/user/name/tags 映射）

config/__init__.py  get_enabled_tracing_providers / get_tracing_config / validate_enabled_tracing_providers（env 驱动）
models/factory.py   _maybe_build_tracing_callbacks() 懒导入 tracing；create_chat_model(attach_tracing=...)
```

两个 helper 职责正交，**不能合并**：

| helper | 干什么 | 操作 config 的哪个字段 | 何时调 |
|--------|--------|----------------------|--------|
| `build_tracing_callbacks()` | 构造 tracer **回调对象**（LangChainTracer / Langfuse CallbackHandler） | `config["callbacks"]`（append） | 图调用前 |
| `inject_langfuse_metadata()` | 构造并 merge Langfuse **元数据**（session/user/name/tags） | `config["metadata"]`（setdefault） | 图调用前 |

回调对象是「**谁来记录** span」，元数据是「**这条 trace 归属哪个会话/用户**」。两个 provider 里只有 Langfuse 读元数据（LangSmith 不读），但回调两个 provider 都要。

---

## §4 核心概念

### 4.1 两个 provider：LangSmith / Langfuse

- **LangSmith**：LangChain 官方追踪平台。`LangChainTracer(project_name=...)`（[factory.py:28](../backend/packages/harness/deerflow/tracing/factory.py#L28)）。
- **Langfuse**：开源可自建的追踪平台。`langfuse.langchain.CallbackHandler`，还支持把 `session_id`/`user_id`/`trace_name`/`tags` 提升到根 trace（驱动 Sessions/Users 页）。

mini 两个都支持，靠环境变量开关（见 §7）。**两个都不配 = 零开销**（`build_tracing_callbacks()` 返回 `[]`，`build_langfuse_trace_metadata()` 返回 `{}`）。

### 4.2 图内 `attach_tracing=False` 不变量

`create_chat_model` 有个 `attach_tracing` 参数（默认 `True`，[models/factory.py:124](../backend/packages/harness/deerflow/models/factory.py#L124)），控制是否在**模型级**挂回调：

- **独立调用方**（图外的，如 MemoryUpdater、临时脚本）：`attach_tracing=True`——它们没经过图根，只能在模型级挂回调才能产出 trace。
- **图内调用方**（`make_lead_agent`、in-graph 的 TitleMiddleware / SummarizationMiddleware 等）：**必须传 `False`**——图根已注入回调，再在模型级挂一次会发**重复 span**，且模型成为嵌套观测后 langfuse 的 trace 属性元数据会被剥离。

这条不变量在源码多处注释里强调（[factory.py:10](../backend/packages/harness/deerflow/tracing/factory.py#L10)、[models/factory.py:14](../backend/packages/harness/deerflow/models/factory.py#L14)、[lead_agent/agent.py:7-13](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L7)）。两个图内中间件都遵守（[title_middleware.py:167](../backend/packages/harness/deerflow/agents/middlewares/title_middleware.py#L167)、[summarization_middleware.py:450](../backend/packages/harness/deerflow/agents/middlewares/summarization_middleware.py#L450)）。

### 4.3 注入点矩阵（2 helper × 4 调用点）

mini 有 4 个调用点，各调哪个 helper：

| 调用点 | `build_tracing_callbacks` | `inject_langfuse_metadata` | 作用 |
|--------|:---:|:---:|------|
| **lead agent**（[lead_agent/agent.py:173](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L173) `_make_lead_agent`） | ✓ | — | 主 run 构造回调 → `config["callbacks"]` |
| **run worker**（[runtime/runs/worker.py:236](../backend/packages/harness/deerflow/runtime/runs/worker.py#L236)） | — | ✓ | 主 run 注入元数据（thread_id/user_id/assistant_id/model_name）→ `config["metadata"]` |
| **子代理**（[subagents/executor.py:605,622](../backend/packages/harness/deerflow/subagents/executor.py#L605) `_aexecute`） | ✓ | ✓ | 子代理两者都做（自己是一棵独立子图） |
| **独立调用方**（[models/factory.py:237](../backend/packages/harness/deerflow/models/factory.py#L237) `attach_tracing=True`） | ✓（模型级） | — | 图外 LLM 调用模型级兜底 |

> **为什么主 run 拆成两处？** lead agent 工厂（[#25 agents.md](agents.md)）负责组装图、在图根挂回调；run worker（[#26 runs.md](runs.md)）负责执行一次 run、知道本次 run 的 `thread_id`/`assistant_id`/`model_name`（来自 `RunRecord`），故由它注入元数据。两者操作的 config 字段不同（`callbacks` vs `metadata`），各司其职、不冲突。子代理因为自己同时是「工厂 + 执行者」，两个都做。

**不 port 的注入点**：上游还有嵌入式 `client.py`（`DeerFlowClient.stream`）——属 Gateway 层的嵌入式客户端，mini 不做（mini 走 `langgraph dev` / 基于 `runtime_lifespan` bundle 自搭）。mini 的 run worker 已覆盖主 run 的元数据注入需求。

### 4.4 Langfuse 元数据：session_id / user_id / trace_name / tags

Langfuse v4 的 callback handler 从 `RunnableConfig.metadata` 里取一组**保留键**提升到根 trace（[metadata.py:26](../backend/packages/harness/deerflow/tracing/metadata.py#L26)）：

| Langfuse 字段 | metadata 键 | 来源 |
|--------------|------------|------|
| session_id（Sessions 页分组） | `langfuse_session_id` | LangGraph `thread_id` |
| user_id（Users 页） | `langfuse_user_id` | `user_id`（无鉴权回退 `DEFAULT_USER_ID="default"`，[user_context.py:85](../backend/packages/harness/deerflow/runtime/user_context.py#L85)） |
| trace_name | `langfuse_trace_name` | `assistant_id`（默认 `lead-agent`） |
| tags | `langfuse_tags` | `env:<DEER_FLOW_ENV>` + `model:<model_name>` |

`build_langfuse_trace_metadata()` 构造这个 dict，`inject_langfuse_metadata()` 把它 merge 进 `config["metadata"]`。所有调用点共用这两个函数，防漂移。

---

## §5 代码走读

### 5.1 `build_tracing_callbacks()`：构造回调列表

```python
def build_tracing_callbacks() -> list[Any]:
    validate_enabled_tracing_providers()          # 缺凭证 ValueError（factory.py:57）
    enabled_providers = get_enabled_tracing_providers()
    if not enabled_providers:
        return []                                  # 未配置 → 零开销
    tracing_config = get_tracing_config()
    callbacks = []
    for provider in enabled_providers:
        if provider == "langsmith":
            callbacks.append(_create_langsmith_tracer(tracing_config.langsmith))
        elif provider == "langfuse":
            callbacks.append(_create_langfuse_handler(tracing_config.langfuse))
    return callbacks
```

两个私有构造器（[factory.py:28](../backend/packages/harness/deerflow/tracing/factory.py#L28) / [35](../backend/packages/harness/deerflow/tracing/factory.py#L35)）**延迟导入 SDK**（`langchain_core.tracers` / `langfuse`），构造不发网络。构造异常包成 `RuntimeError`（「LangSmith/Langfuse tracing initialization failed」，[factory.py:70](../backend/packages/harness/deerflow/tracing/factory.py#L70)），**不静默吞**——追踪是可观测性，坏了得知道。

Langfuse 构造略特殊（[factory.py:35](../backend/packages/harness/deerflow/tracing/factory.py#L35)）：langfuse≥4 经 `Langfuse(...)` client 单例初始化项目级凭证，LangChain 回调再挂到那个已配置的 client 上（`CallbackHandler(public_key=...)`）。

### 5.2 `inject_langfuse_metadata()`：注入元数据

```python
def inject_langfuse_metadata(config, *, thread_id, user_id=None, assistant_id=None,
                             model_name=None, environment=None) -> None:
    langfuse_metadata = build_langfuse_trace_metadata(...)   # Langfuse 未启用返回 {}
    if not langfuse_metadata:
        return                                              # no-op
    merged_metadata = dict(config.get("metadata") or {})
    for key, value in langfuse_metadata.items():
        merged_metadata.setdefault(key, value)              # 调用方优先
    config["metadata"] = merged_metadata
```

关键：**`setdefault`**——调用方已有的 key 不被覆盖（[metadata.py:99](../backend/packages/harness/deerflow/tracing/metadata.py#L99)）。例如前端设的 `langfuse_session_id`（把多个 run 归到一个会话）会留住，不被后端的 `thread_id` 冲掉。`config` 就地修改；Langfuse 不在启用 provider 时是 no-op。

`build_langfuse_trace_metadata` 内部延迟导入 `DEFAULT_USER_ID`（[metadata.py:51](../backend/packages/harness/deerflow/tracing/metadata.py#L51)），避免循环：`deerflow.runtime` 急切导入运行 worker，后者需要 `deerflow.tracing`。

### 5.3 models 的懒导入：`_maybe_build_tracing_callbacks`

`create_chat_model(attach_tracing=True)` 时调 `_maybe_build_tracing_callbacks()`（[models/factory.py:97](../backend/packages/harness/deerflow/models/factory.py#L97)）：

```python
def _maybe_build_tracing_callbacks() -> list:
    try:
        from deerflow.tracing import build_tracing_callbacks   # 懒导入
    except ImportError:
        return []                                              # tracing 包不存在 → []
    try:
        return list(build_tracing_callbacks() or [])
    except Exception:
        logger.warning("构建追踪回调失败，跳过模型级 tracing", exc_info=True)
        return []                                              # 追踪坏了不挡 agent 起来
```

注意它**吞掉所有异常**返回 `[]`——与 `build_tracing_callbacks` 的响亮报错语义不同（§6 详谈）。挂在模型实例的 `callbacks` 属性上（[models/factory.py:240](../backend/packages/harness/deerflow/models/factory.py#L240)）。

### 5.4 子代理注入（trace 归属父 thread，源码注释标 #3611）

子代理经 `task` 工具触发后，在自己的隔离事件循环里跑一棵**独立子图**。若不注入 Langfuse 元数据，子代理的 trace 会**飘成一个独立 session**——和父对话断开。修复（[subagents/executor.py:605](../backend/packages/harness/deerflow/subagents/executor.py#L605)）：`_aexecute` 在图根同时做两件事——

```python
# ① 构造回调（在 token collector 之后追加）
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    run_config["callbacks"] = [*(run_config.get("callbacks") or []), *tracing_callbacks]
# ② 子代理名归一化（对齐 lead-agent 命名形状）
assistant_id = f"subagent:{self.config.name.strip().lower().replace('_', '-')}" if self.config.name else "subagent"
# ③ 注入父 run 身份
inject_langfuse_metadata(run_config, thread_id=self.thread_id, user_id=self.user_id,
                         assistant_id=assistant_id, model_name=self.model_name, environment=...)
```

| 子代理 metadata 字段 | 来源 | 作用 |
|---------------------|------|------|
| `langfuse_session_id` | 父 `thread_id`（executor 的 `self.thread_id`） | 子代理 trace 归到父对话的 Session 卡片 |
| `langfuse_user_id` | `task_tool` 经 `resolve_runtime_user_id` 捕获的 `user_id` | 子代理 trace 落在正确的 Users 页 |
| `langfuse_trace_name` | `subagent:<归一化名>` | 区分「这是哪个子代理的 trace」 |

**user_id 捕获跨线程**：子代理跑在隔离 daemon 线程的持久事件循环上。`task_tool` 在调用方线程（有 runtime 上下文）经 `resolve_runtime_user_id(runtime)` 解析 user_id，**显式传入** executor（存 `self.user_id`），不依赖 contextvar 跨线程传播（同 [memory.md](memory.md) 的「入队捕获」思路）。详见 [#15 subagents.md](subagents.md) §5.2。

### 5.5 run worker 注入（主 run 元数据）

run worker（[runtime/runs/worker.py:236](../backend/packages/harness/deerflow/runtime/runs/worker.py#L236)）在装好 runtime context、解析 agent 之后、调图之前注入：

```python
inject_langfuse_metadata(
    config,
    thread_id=thread_id,
    user_id=get_effective_user_id(),
    assistant_id=record.assistant_id,        # 来自 RunRecord
    model_name=record.model_name,
    environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"),
)
```

它还顺手把 `RunJournal` 作为 callback 注入（[worker.py:231](../backend/packages/harness/deerflow/runtime/runs/worker.py#L231)，抓 token 用量）。详见 [#26 runs.md](runs.md)。

### 5.6 数据流：一次主 run 的 trace 端到端怎么生成

把 §5.1-5.5 串起来——用户发一条消息，配了 LangSmith + Langfuse：

```
用户发消息 → run worker 接管（#26）
   │
   ① run worker：inject_langfuse_metadata(config, thread_id, user_id, assistant_id, model_name)
        → config["metadata"] 写入 langfuse_session_id / user_id / trace_name / tags
   │
   ② 装好 lead agent（#25）：_make_lead_agent 在图根 append build_tracing_callbacks()
        → config["callbacks"] = [LangChainTracer, Langfuse CallbackHandler]
   │
   ③ agent.astream(state, config)   ← 图开始跑
        LangChain 在每个 LLM/tool/node 调用前后自动调 callbacks → 上报 span
        整棵 span 树挂在「一个根 trace」下（回调在图根只注入一次）
   │
   ④ 面板侧：Langfuse 读 ① 写的 session/user/name/tags → trace 落进对应 Session/Users 卡片
             LangSmith 只挂在 project 下（不读这套 metadata）
   │
   ⑤ 中途若触发子代理（task 工具）：子代理在自己隔离循环里
        build_tracing_callbacks() + inject_langfuse_metadata(thread_id=父thread, ...)
        → 子代理 trace 也归到父对话 Session（#3611，§5.4）
```

**三个关键点**：① **元数据**（步①）和**回调**（步②）分开注入、操作 config 不同字段（`metadata` vs `callbacks`），放两处不冲突；② 回调只在图根注入一次（不是每个 LLM 各挂），所以 span 串成**一棵完整树**而非碎片；③ 子代理复用同一套 helper、注入父 thread_id，让它的子树**挂在父 trace 下**而非飘成独立 session。

---

## §6 设计权衡（为什么这么设计）

| 权衡 | 选择 | 理由 |
|------|------|------|
| **图根注入（非每 LLM 各挂）** | 回调在 `astream`/`ainvoke` 前 append 进 `config["callbacks"]` | 每 LLM 各挂会产出一堆碎片 trace（每调用一条），面板串不成完整树 |
| **图内 `attach_tracing=False`** | lead agent / 图内中间件一律 `False` | 图根已注入，模型级再挂发重复 span + langfuse 元数据被剥离 |
| **两个 helper 正交** | `build_tracing_callbacks`（回调）+ `inject_langfuse_metadata`（元数据） | 回调对象 = 谁记录 span；元数据 = trace 归属哪个会话/用户；操作 config 不同字段 |
| **`setdefault` 调用方优先** | 调用方已有的 metadata key 不被覆盖 | 前端能把多个 run 归到自定义会话；后端 `thread_id` 不冲掉它 |
| **构造失败响亮报错** | `build_tracing_callbacks` 包 RuntimeError | 追踪是可观测性，坏了得知道（不然以为在追踪其实没追踪） |
| **models 吞异常返回 `[]`** | `_maybe_build_tracing_callbacks` 吞所有异常 | 在模型创建路径上，追踪是 nice-to-have，不能因为它坏了让 agent 起不来。两层语义不同 |
| **未配置零开销** | provider 未启用 → `[]` / `{}` | 没有回调对象、没有注入、没有额外 import |
| **Langfuse 不启用返回 `{}`** | `build_langfuse_trace_metadata` 返回空 dict | 调用方可无条件 merge，不影响 LangSmith 或其它 tracer（「可选 provider 不污染必选路径」） |
| **provider 开关在 config 而非 tracing** | env var 驱动（`SimpleNamespace`，非 pydantic `TracingConfig`） | 「是否启用」是部署配置；mini 更轻，不占 config.yaml schema |
| **子代理显式传 user_id** | `task_tool` 捕获后传入 executor | 子代理跑隔离 daemon 线程，不依赖 contextvar 跨线程传播 |
| **`langfuse` soft-load** | 缺包时 `_create_langfuse_handler` ImportError → RuntimeError | 不影响 LangSmith 路径 |

---

## §7 配置用法

### 配置（环境变量）

```bash
# LangSmith
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=lsv2_...          # 必填（validate 检查）
export LANGSMITH_PROJECT=deerflow           # 可选，默认 deerflow

# Langfuse
export LANGFUSE_TRACING=true
export LANGFUSE_SECRET_KEY=sk-lf-...        # 必填
export LANGFUSE_PUBLIC_KEY=pk-lf-...        # 必填
export LANGFUSE_HOST=https://cloud.langfuse.com  # 可选，自建时改

# tags 用（可选）
export DEER_FLOW_ENV=production
```

两个都设 = 同时上报两个平台。一个都不设 = 零开销。开关读取见 [config/__init__.py:59](../backend/packages/harness/deerflow/config/__init__.py#L59)，凭证校验见 [:72](../backend/packages/harness/deerflow/config/__init__.py#L72)。

### 安装 SDK

```bash
# LangSmith（langchain 已带 langchain_core.tracers，无需额外装）
# Langfuse
pip install langfuse   # 提供 langfuse + langfuse.langchain.CallbackHandler
```

> `langfuse` 是 **soft-load**：缺包时 `_create_langfuse_handler` 的 `from langfuse import ...` 抛 ImportError，被包成 RuntimeError（[factory.py:74](../backend/packages/harness/deerflow/tracing/factory.py#L74)）。不影响 LangSmith 路径。

### 跑测试

```bash
cd backend && make test    # 含 test/test_tracing.py（36 个 hermetic 测试）+ test_subagents.py::TestSubagentTracingWiring
```

测试约定（[test/test_tracing.py](../test/test_tracing.py)）：env var 经 `monkeypatch.setenv/delenv` 控制；langfuse SDK（非依赖）用 `sys.modules` 注入 fake；langsmith tracer 经 monkeypatch 替身；models `attach_tracing` 联动用 `_FakeModelClass` + `resolve_class`/app_config 替身 + `_maybe_build_tracing_callbacks` spy，跑真 `create_chat_model` 到「挂回调」那步而不碰真模型 provider。子代理注入点测试把 `_build_initial_state`/`_create_agent` 打桩短路，在 `agent.astream` 处捕获 `run_config`，断言 callbacks 追加 + langfuse 元数据字段映射——不跑真 agent / 真 Langfuse 后端。

---

## §8 与其它模块的关系

```
config/__init__ (get_enabled_tracing_providers ← env LANGSMITH_TRACING/LANGFUSE_TRACING
                 get_tracing_config            ← env 各 provider 凭证（SimpleNamespace）
                 validate_enabled_tracing_providers) ← 缺凭证 ValueError
  │
tracing
  ├── factory.build_tracing_callbacks() ──→ [LangChainTracer / LangfuseCallbackHandler]
  └── metadata.build_langfuse_trace_metadata() / inject_langfuse_metadata()
                                       ↑
                            runtime/user_context (#18：DEFAULT_USER_ID="default" 兜底 langfuse_user_id)
  │
models/factory._maybe_build_tracing_callbacks() 懒导入 ← tracing（attach_tracing=True 路径）
  │
▼ 4 个调用点（见 §4.3 矩阵）：
  • lead agent（#25 agents.md）：build_tracing_callbacks → config["callbacks"]
  • run worker（#26 runs.md）：inject_langfuse_metadata → config["metadata"]
  • 子代理（#15 subagents.md）：两者都调（#3611 归属父 thread）
  • 独立调用方（#6 models.md attach_tracing=True）：模型级构造回调
```

- **上游**：[config/__init__.py](../backend/packages/harness/deerflow/config/__init__.py)（provider 开关 + 凭证，env 驱动）、[#18 user_context.md](user_context.md)（`DEFAULT_USER_ID` 给 langfuse_user_id 兜底；`resolve_runtime_user_id` 给子代理捕获 user_id）。
- **下游消费者**（4 个调用点，见 §4.3 矩阵）：lead agent（[#25 agents.md](agents.md)）、run worker（[#26 runs.md](runs.md)）、子代理（[#15 subagents.md](subagents.md)）、独立调用方（[#6 models.md](models.md)）。
- **不 port**：嵌入式 `client.py`（DeerFlowClient）——属 Gateway 层，mini 走 `langgraph dev`。run worker 已覆盖主 run 注入。

---

## §9 实现差异（vs 上游 deer-flow 源码）

> 对照基线 = `deer-flow/backend/packages/harness/deerflow/tracing/`（与 mini 同 3 文件）。已**剥 docstring/comment 后**判逻辑差。结论：**mini 的 tracing 是上游的忠实移植**——`factory.py` / `metadata.py` / `__init__.py` 的逻辑与上游**完全一致**（`metadata.py` 0 行差异；`factory.py` 的「差」全是英文→中文注释翻译，0 逻辑差）。真差异只有一处：

### 9.1 一致的部分

| 维度 | 上游 deer-flow | mini |
|---|---|---|
| 两个 helper（`build_tracing_callbacks` + `inject_langfuse_metadata`） | 有 | **完全相同** |
| 两个 provider（LangSmith `LangChainTracer` / Langfuse `CallbackHandler`） | 有 | **相同** |
| Langfuse 元数据映射（session/user/name/tags） | 有（`build_langfuse_trace_metadata` + `inject_langfuse_metadata`） | **`metadata.py` 0 行差异** |
| 图内 `attach_tracing=False` 不变量 | 有 | **相同** |
| 构造失败包 RuntimeError + langfuse soft-load | 有 | **相同** |

### 9.2 mini 砍掉的

- **嵌入式 `client.py`（DeerFlowClient）的第 5 个注入点**：上游 [`client.py:607-613`](../../deer-flow/backend/packages/harness/deerflow/client.py) 的 `DeerFlowClient.stream` 自己调 `build_tracing_callbacks()` + `inject_langfuse_metadata()`（且 `create_chat_model(attach_tracing=False)`，因为 stream() 自己注入）——这是上游的**第 5 个 tracing 注入点**，属 Gateway 层的嵌入式客户端。mini 不 port `client.py`（mini 走 `langgraph dev` / `runtime_lifespan` bundle 自搭，无嵌入式客户端），故 mini 只有 **4 个注入点**（§4.3 矩阵），主 run 的元数据注入由 run worker 覆盖。

### 9.3 一句话总结

mini tracing 的设计原则是「**忠实移植 + 砍 Gateway 层**」：两个 helper、两个 provider、元数据映射、图内不变量与上游 deer-flow **完全一致**（`metadata.py` 0 差、`factory.py` 仅注释翻译）；唯一差异是不 port 上游嵌入式 `client.py` 的第 5 个注入点（mini 无嵌入式客户端）。读完 mini 这篇，迁到上游 tracing 几乎零认知差，只是多一个 client.py 注入点。

---

## §10 常见问题 / 排错

**Q：为什么回调必须在图根注入，不能每个 LLM 调用各挂各的？**
A：那样会产出一堆碎片 trace（每个 LLM 调用一条），面板上串不成一棵完整树。图根注入让整棵 span 树挂在一个根 trace 下。这就是「图内 `attach_tracing=False`」不变量——图内 `create_chat_model` 必须传 `False`。

**Q：我设了 `LANGFUSE_TRACING=true` 但 Langfuse 面板看不到 trace？**
A：常见原因：① 没装 `langfuse` 包（`_create_langfuse_handler` ImportError → RuntimeError）；② 缺 `LANGFUSE_SECRET_KEY`/`LANGFUSE_PUBLIC_KEY`（validate ValueError）；③ 回调没在图根注入（检查 `make_lead_agent` 是否在调图前 append 了 `build_tracing_callbacks()`，[lead_agent/agent.py:173](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L173)）。本模块负责**构造回调 + 注入元数据**，**挂载**在 4 个调用点。

**Q：`build_tracing_callbacks` 抛 RuntimeError 但 `_maybe_build_tracing_callbacks` 返回 `[]`，矛盾吗？**
A：不矛盾，两层语义不同。`build_tracing_callbacks` 是**显式构造**（图根调用），坏了得知道（响亮报错）；`_maybe_build_tracing_callbacks` 在**模型创建路径**上（独立调用方），追踪是 nice-to-have，不能因为它坏了就让 agent 起不来，所以吞掉异常返回 `[]`。

**Q：Langfuse 的 session_id 有什么用？**
A：把同一 `thread_id` 的多次 run 归到一个 Langfuse **Session**——面板上能看到一个对话的完整历史，而不是散落的单次 trace。`inject_langfuse_metadata` 把 `thread_id` 映射成 `langfuse_session_id`。

**Q：前端设的 `langfuse_session_id` 会被后端覆盖吗？**
A：不会。`inject_langfuse_metadata` 用 `setdefault`（[metadata.py:99](../backend/packages/harness/deerflow/tracing/metadata.py#L99)）——调用方已有的 key 优先。这让前端能把多个 run 归到一个自定义会话。

**Q：子代理（task 工具触发的）的 trace 飘成了独立 session，和父对话断开？**
A：这是 #3611 的征兆——子代理 executor 没注入父 `thread_id`/`user_id`。已修复：`_aexecute` 在图根调 `inject_langfuse_metadata`，把父 `thread_id`→`langfuse_session_id`、捕获的 `user_id`→`langfuse_user_id`、`subagent:<归一化名>`→`langfuse_trace_name`（[subagents/executor.py:622](../backend/packages/harness/deerflow/subagents/executor.py#L622)）。现在子代理 trace 归到父对话的 Session 卡片下，带 `subagent:deep-research` 这样的 trace 名。

**Q：不配任何 tracing 会有开销吗？**
A：零。`get_enabled_tracing_providers()` 返回 `[]` → `build_tracing_callbacks()` 直接返回 `[]`，`build_langfuse_trace_metadata()` 返回 `{}`。没有回调对象，没有注入，没有额外 import。

**Q：主 run 的回调在 lead agent 构造、元数据在 worker 注入，为什么不放一处？**
A：职责分离。lead agent 工厂（[#25 agents.md](agents.md)）组装图、在图根挂回调（它知道图的形状）；run worker（[#26 runs.md](runs.md)）执行一次 run、知道本次 run 的 `thread_id`/`assistant_id`/`model_name`（来自 `RunRecord`），故由它注入元数据。两者操作 config 不同字段（`callbacks` vs `metadata`），不冲突。

---

## §11 小结

tracing 是 agent 的「黑盒透视镜」：把一次 run 拆成一棵 span 树，在 LangSmith/Langfuse 面板可观测。本模块只做两件事，职责正交：

- **构造回调**（[factory.py](../backend/packages/harness/deerflow/tracing/factory.py) `build_tracing_callbacks`）：按启用 provider 造 LangChainTracer / Langfuse CallbackHandler，未配置返回 `[]`（零开销），构造失败响亮报错。
- **注入元数据**（[metadata.py](../backend/packages/harness/deerflow/tracing/metadata.py) `inject_langfuse_metadata`）：把 thread_id/user_id/assistant_id/model_name 映射成 Langfuse session/user/name/tags，`setdefault` 调用方优先，Langfuse 未启用 no-op。

4 个调用点各取所需：lead agent 造回调、run worker 注元数据、子代理两者都做（归属父 thread，#3611）、独立调用方模型级兜底。核心不变量是**图内 `attach_tracing=False`**——图根统一注入，防重复 span + 元数据剥离。models 的懒导入让 tracing 模块落地后 `attach_tracing=True` 路径自动生效，无需改 models。

> 上一篇：[#15 subagents.md](subagents.md)（子代理——本篇 §5.4 子代理注入点的来源；executor 的 `build_tracing_callbacks` + `inject_langfuse_metadata`）
> 下一篇：[#17 agents_config.md](agents_config.md)（自定义 agent——SOUL.md 人格 + config.yaml 能力白名单 + per-user 隔离）
