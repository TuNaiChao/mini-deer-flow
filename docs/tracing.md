# 16. tracing.md — 链路追踪（LangSmith / Langfuse 图根注入）

> **一句话定位**：tracing 让一次 agent run 产**一条完整 trace**——把所有 node 调用、LLM 调用、
> tool 调用串成一棵子 span 树，在 LangSmith / Langfuse 面板上可观测、可回放、可调试。
> 本模块负责在**图根**注入追踪回调 + 构造 Langfuse 的 trace 属性元数据。

读完 [models.md](models.md)（懂了 `create_chat_model` 与 `attach_tracing`）再看本篇最省事——
tracing 的核心就是「回调挂在哪一层」，而 models 的 `attach_tracing` 开关正是这个分层的旋钮。

> **M12 全维重审（2026-06-28）**：逐文件 diff 最新上游，tracing 模块本身（factory/metadata）
> 剥 docstring 后**零逻辑漂移**。补 **#17 + #3611 的子代理注入点**——上游在 `subagents/executor.py`
> 的 `_aexecute` 图根也挂 tracing callbacks 并注入 Langfuse 元数据（让子代理 trace 归属父 thread），
> mini 此前缺这一处。本次补齐：① 子代理图根挂 `build_tracing_callbacks`（#17）；② `inject_langfuse_metadata`
> 注入父 `thread_id`→session、`task_tool` 捕获的 user_id→user、`subagent:<归一化名>`→trace_name（#3611）。
> + 8 项 hermetic 测试（`test_subagents.py::TestSubagentTracingWiring`）。详见 §「三个注入点」。

---

## 为什么需要链路追踪（痛点）

agent 是个「黑盒」：一个用户问题进来，里面跑了一堆 LLM 调用、tool 调用、中间推理。出问题时
（答非所问、token 暴涨、循环卡死）你只能看最后那句话，**看不到中间发生了什么**。

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

在 LangSmith / Langfuse 面板上，你能展开每一层，看输入输出、耗时、token、报错。**没有它，
生产排障基本靠猜。**

---

## 核心概念（名词 + 类比）

### ① Trace / Span / Callback

- **trace**：一次完整 run（一个用户问题从进到出）。
- **span**：trace 里的一个步骤（一次 LLM 调用、一次 tool 调用、一个 node）。
- **callback**：LangChain 的回调钩子（`BaseCallbackHandler`）。LangSmith / Langfuse 各提供一个
  callback handler，挂在 run 上后，LangChain 在每次 LLM/tool/chain 调用前后自动上报 span。

类比：trace 是「一个快递单号」，span 是「每一段物流轨迹」（揽收→分拣→运输→派送），callback 是
「每到一个节点就扫码上报」的那个扫码动作。

### ② 两个 provider：LangSmith / Langfuse

- **LangSmith**：LangChain 官方追踪平台。`LangChainTracer(project_name=...)`。
- **Langfuse**：开源可自建的追踪平台。`langfuse.langchain.CallbackHandler`，还支持把
  `session_id`/`user_id`/`trace_name`/`tags` 提升到根 trace 上（驱动 Sessions/Users 页）。

mini 两个都支持，靠环境变量开关（见下）。**两个都不配 = 零开销**（返回空回调列表）。

### ③ 图根注入（graph-root injection）

回调必须在**图调用的根**注入——也就是在调 `agent.astream(...)` / `ainvoke(...)` 前，把 callbacks
append 进 `config["callbacks"]`。这样整棵 span 树挂在**一个根 trace** 下。

mini 有**三个图根注入点**（都调 `build_tracing_callbacks()` + `inject_langfuse_metadata()`）：

| 注入点 | 位置 | 作用 |
|--------|------|------|
| **lead agent** | [lead_agent/agent.py](../backend/packages/harness/deerflow/agents/lead_agent/agent.py) `_make_lead_agent` | 主 run 一条 trace（#17） |
| **子代理** | [subagents/executor.py](../backend/packages/harness/deerflow/subagents/executor.py) `_aexecute` | 每个 task 子代理一条子 trace，归属父 thread（#17 + #3611） |
| **独立调用方** | [models/factory.py](../backend/packages/harness/deerflow/models/factory.py) `attach_tracing=True` | 图外的 LLM 调用（如 MemoryUpdater），模型级兜底 |

> 上游还有第四个注入点：嵌入式 `client.py`（DeerFlowClient.stream）+ gateway `runtime/runs/worker.py`。
> mini 不 port `client.py`（§2.1 设计上不做嵌入式客户端）和 gateway worker（M18 范畴）——mini 走
> `langgraph dev` / 基于 `runtime_lifespan` bundle 自搭，故这两条路径不在 harness 层。

为什么不在每个 LLM 调用处挂？——那样每次 `create_chat_model` 各挂各的回调，会产生**一堆碎片
trace**（每个 LLM 调用一条），而不是一棵完整树，面板上根本串不起来。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### 红线 #17：in-graph 一律 `attach_tracing=False`

`create_chat_model` 有个 `attach_tracing` 参数（默认 `True`），控制是否在**模型级**挂回调：

- **独立调用方**（图外的，如 MemoryUpdater、临时脚本）：`attach_tracing=True`——它们没经过图根，
  只能在模型级挂回调才能产出 trace。
- **图内调用方**（make_lead_agent、in-graph 的 TitleMiddleware 等）：**必须传 `False`**——图根
  已注入回调，再在模型级挂一次会发**重复 span**，且模型成为嵌套观测后 langfuse 的 trace 属性
  元数据会被剥离。

`_maybe_build_tracing_callbacks()`（[models/factory.py](../backend/packages/harness/deerflow/models/factory.py)）
**懒导入** `from deerflow.tracing import build_tracing_callbacks`：tracing 模块未落地时返回 `[]`
（零副作用），落地后自动生效——这就是为什么 M12 落地后 models 的 `attach_tracing=True` 路径
「自动」开始工作，无需改 models。

### Langfuse 元数据：session_id / user_id / trace_name / tags

Langfuse v4 的 callback handler 从 `RunnableConfig.metadata` 里取一组**保留键**提升到根 trace：

| Langfuse 字段 | metadata 键 | 来源 |
|--------------|------------|------|
| session_id（分组） | `langfuse_session_id` | LangGraph `thread_id` |
| user_id（Users 页） | `langfuse_user_id` | `get_effective_user_id()`（无鉴权回退 `default`） |
| trace_name | `langfuse_trace_name` | `assistant_id`（默认 `lead-agent`） |
| tags | `langfuse_tags` | `env:<DEER_FLOW_ENV>` + `model:<model_name>` |

`build_langfuse_trace_metadata()` 构造这个 dict，`inject_langfuse_metadata()` 把它 merge 进
`config["metadata"]`。所有图根注入点（lead agent / 子代理 / 独立调用方）共用这两个函数，防漂移。

**Langfuse 不在启用 provider 时返回 `{}`**——调用方可以无条件 merge 结果而不影响 LangSmith
或其它 tracer。这是「可选 provider 不污染必选路径」的不变量。

### #3611：子代理 trace 归属父 thread

子代理经 `task` 工具触发后，在自己的隔离事件循环里跑一棵**独立子图**。若不注入 Langfuse 元数据，
子代理的 trace 会**飘成一个独立 session**——和父对话断开，面板上看不出「这次子代理调用是哪个
对话发起的」。#3611 的修复：子代理 executor 在 `_aexecute` 图根也调 `inject_langfuse_metadata`，
把**父 run 的身份**映射进子代理 trace：

| 子代理 metadata 字段 | 来源 | 作用 |
|---------------------|------|------|
| `langfuse_session_id` | 父 `thread_id`（executor 的 `self.thread_id`） | 子代理 trace 归到父对话的 Session 卡片 |
| `langfuse_user_id` | `task_tool` 经 `resolve_runtime_user_id` 捕获的 `user_id` | 子代理 trace 落在正确的 Langfuse Users 页 |
| `langfuse_trace_name` | `subagent:<归一化名>` | 区分「这是哪个子代理的 trace」 |

**子代理名归一化**：对齐 lead-agent 命名形状——`self.config.name.strip().lower().replace("_", "-")`。
例如 `Deep_Research` → `subagent:deep-research`。无共享 helper（`runtime/runs/naming.py` 只管 lead
run），故内联在 executor。

**user_id 捕获跨线程**：子代理跑在隔离 daemon 线程的持久事件循环上。`task_tool` 在调用方线程
（有 runtime 上下文）经 `resolve_runtime_user_id(runtime)` 解析 user_id，**显式传入** executor
（存 `self.user_id`），不依赖 contextvar 跨线程传播（同 [memory.md](memory.md) #20 的「入队捕获」
思路）。子代理沙箱/记忆另经 `copy_context()` 传播的 contextvar 解析 user_id，与此处互不依赖。

> `setdefault` 同样适用：调用方（如前端）已设的 `langfuse_session_id` 不被子代理覆盖。

### `setdefault`：调用方优先

`inject_langfuse_metadata` 用 `merged_metadata.setdefault(key, value)`——**调用方已有的 key 不被
覆盖**。例如前端设的 `langfuse_session_id`（把多个 run 归到一个会话）会留住，不被后端的
`thread_id` 冲掉。

### 构造失败响亮报错

`build_tracing_callbacks` 把 tracer 构造异常包成 `RuntimeError`（「LangSmith tracing
initialization failed」），**不静默吞**——追踪是可观测性，坏了得知道（不然你以为在追踪其实没追踪）。
但 `validate_enabled_tracing_providers()`（检查 API key 齐全）的失败是 `ValueError`，在构造前抛。

models 侧的 `_maybe_build_tracing_callbacks` 反而**吞掉所有异常**返回 `[]`——因为它在模型创建
路径上，不能因为追踪坏了就让 agent 起不来（追踪是 nice-to-have，agent 能跑是 must-have）。
两层语义不同，别搞混。

---

## 文件结构

```
tracing/
├── __init__.py      # 导出 build_tracing_callbacks / build_langfuse_trace_metadata / inject_langfuse_metadata
├── factory.py       # build_tracing_callbacks()：按启用 provider 构造回调列表（未配置返回 []）
└── metadata.py      # build_langfuse_trace_metadata() + inject_langfuse_metadata()（session/user/name/tags 映射）

config/__init__.py   # get_enabled_tracing_providers / get_tracing_config / validate_enabled_tracing_providers（env 驱动）
models/factory.py    # _maybe_build_tracing_callbacks() 懒导入 tracing；create_chat_model(attach_tracing=...)
```

> **为什么 provider 开关在 config 而非 tracing？** tracing 模块只管「构造回调」，而「是否启用」
> 是部署配置（env var）。mini 用环境变量（`LANGSMITH_TRACING`/`LANGFUSE_TRACING`）驱动，deer 用
> pydantic `TracingConfig`——两者语义一致，mini 更轻（不占 config.yaml schema）。

---

## 关键接口

### `build_tracing_callbacks()`（`factory.py`）

```python
def build_tracing_callbacks() -> list[Any]:
    """为所有显式启用的追踪 provider 构造回调。

    先 validate（缺凭证 ValueError），再逐 provider 构造（异常包 RuntimeError）。
    未启用任何 provider 时返回 []。
    """
```

内部两个私有构造器（延迟导入 SDK，构造不发网络）：

```python
def _create_langsmith_tracer(config) -> LangChainTracer: ...   # langchain_core.tracers.langchain
def _create_langfuse_handler(config) -> CallbackHandler: ...   # langfuse + langfuse.langchain
```

### `build_langfuse_trace_metadata()` / `inject_langfuse_metadata()`（`metadata.py`）

```python
def build_langfuse_trace_metadata(
    *, thread_id, user_id=None, assistant_id=None, model_name=None, environment=None,
) -> dict[str, Any]: ...   # langfuse 未启用返回 {}

def inject_langfuse_metadata(
    config: dict, *, thread_id, user_id=None, assistant_id=None, model_name=None, environment=None,
) -> None: ...   # 就地 merge 进 config["metadata"]，setdefault 调用方优先；未启用 no-op
```

---

## 应用方法

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

两个都设 = 同时上报两个平台。一个都不设 = 零开销（`build_tracing_callbacks()` 返回 `[]`）。

### 安装 SDK

```bash
# LangSmith（langchain 已带 langchain_core.tracers，无需额外装）
# Langfuse
pip install langfuse   # 提供 langfuse + langfuse.langchain.CallbackHandler
```

> `langfuse` 是 **soft-load**：缺包时 `_create_langfuse_handler` 的 `from langfuse import ...`
> 抛 ImportError，被包成 RuntimeError。不影响 LangSmith 路径。

### 注入点（三处图根，都已落地）

**① lead agent**（[lead_agent/agent.py](../backend/packages/harness/deerflow/agents/lead_agent/agent.py) `_make_lead_agent`）：

```python
# 图根注入——一次 run 一条 trace，所有节点/LLM/工具调用作为子 span
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    config["callbacks"] = [*config.get("callbacks", []), *tracing_callbacks]
# Langfuse 元数据经 worker/client 在调图前 merge 进 config["metadata"]（见 §关系图）
```

**② 子代理**（[subagents/executor.py](../backend/packages/harness/deerflow/subagents/executor.py) `_aexecute`，#3611）：

```python
run_config: RunnableConfig = {"recursion_limit": ..., "callbacks": [collector], "tags": [...]}
# #17：图根追加 tracing callbacks（在 collector 之后）
tracing_callbacks = build_tracing_callbacks()
if tracing_callbacks:
    run_config["callbacks"] = [*(run_config.get("callbacks") or []), *tracing_callbacks]
# 子代理名归一化（对齐 lead-agent 命名形状）
assistant_id = f"subagent:{self.config.name.strip().lower().replace('_', '-')}" if self.config.name else "subagent"
# #3611：注入 Langfuse 元数据（父 thread_id / 捕获 user_id / subagent:<name>）
inject_langfuse_metadata(run_config, thread_id=self.thread_id, user_id=self.user_id,
                         assistant_id=assistant_id, model_name=self.model_name,
                         environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"))
```

**③ 独立调用方**（图外的 LLM 调用，如 MemoryUpdater）：`create_chat_model(attach_tracing=True)`
经 `_maybe_build_tracing_callbacks` 懒导入 tracing，在模型级挂回调——这些调用没经过图根，只能模型级兜底。

### 跑测试

```bash
cd backend && make test    # 含 test/test_tracing.py（hermetic 单元测试）+ test_subagents.py::TestSubagentTracingWiring（#17/#3611 注入点）
```

测试约定：env var 经 `monkeypatch.setenv/delenv` 控制；langfuse SDK（非依赖）用
`sys.modules` 注入 fake；langsmith tracer 经 monkeypatch 替身；models `attach_tracing` 联动用
`_FakeModelClass` + `resolve_class`/app_config 替身 + `_maybe_build_tracing_callbacks` spy，跑真
`create_chat_model` 到「挂回调」那步而不碰真模型 provider。子代理注入点测试（`TestSubagentTracingWiring`）
把 `_build_initial_state`/`_create_agent` 打桩短路，在 `agent.astream` 处捕获 `run_config`，断言
callbacks 追加 + langfuse 元数据字段映射——不跑真 agent / 真 Langfuse 后端。

---

## 与其它模块的关系

```
config/__init__ (get_enabled_tracing_providers ← env LANGSMITH_TRACING/LANGFUSE_TRACING
                 get_tracing_config            ← env 各 provider 凭证
                 validate_enabled_tracing_providers) ← 缺凭证 ValueError
  │
tracing
  ├── factory.build_tracing_callbacks() ──→ [LangChainTracer / LangfuseCallbackHandler]
  └── metadata.build_langfuse_trace_metadata() / inject_langfuse_metadata()
                                       ↑
                            runtime/user_context (DEFAULT_USER_ID 兜底 langfuse_user_id)
  │
models/factory._maybe_build_tracing_callbacks() 懒导入 ← tracing（落地后 attach_tracing=True 路径生效）
  │
▼ 图根注入：M17 lead_agent 工厂 / M18 runs worker / 嵌入式 client（把 callbacks + metadata 合进 config）
```

- **上游**：`config`（provider 开关 + 凭证，env 驱动）、`runtime/user_context`
  （`DEFAULT_USER_ID` 给 langfuse_user_id 兜底；`resolve_runtime_user_id` 给子代理捕获 user_id）。
- **下游消费者**（三个图根注入点，都已落地）：
  - **lead agent**（`make_lead_agent` → `_make_lead_agent`）——主 run trace；
  - **子代理**（`executor._aexecute`，#3611）——子代理 trace 归属父 thread；
  - **独立调用方**（`models.create_chat_model(attach_tracing=True)`，懒导入已就位）——图外 LLM 兜底。
- **不 port**：嵌入式 `client.py`（DeerFlowClient）+ gateway `runtime/runs/worker.py`——属 Gateway 层
  （§2.1 / §2.3），mini 走 `langgraph dev`。harness 层的 worker 注入在 M18 范畴。
- **红线 #17**：图内 `create_chat_model` 一律 `attach_tracing=False`，图根统一注入，防重复 span。
- **红线 #3611**：子代理 trace 必须注入父 `thread_id`/`user_id`，否则飘成独立 session、与父对话断开。

---

## 常见问题 / 排错

**Q：为什么回调必须在图根注入，不能每个 LLM 调用各挂各的？**
A：那样会产出一堆碎片 trace（每个 LLM 调用一条），面板上串不成一棵完整树。图根注入让整棵 span
树挂在一个根 trace 下。这就是红线 #17——图内 `create_chat_model` 必须 `attach_tracing=False`。

**Q：我设了 `LANGFUSE_TRACING=true` 但 Langfuse 面板看不到 trace？**
A：常见原因：① 没装 `langfuse` 包（`_create_langfuse_handler` ImportError → RuntimeError）；
② 缺 `LANGFUSE_SECRET_KEY`/`LANGFUSE_PUBLIC_KEY`（validate ValueError）；③ 回调没在图根注入
（检查 `make_lead_agent` 是否在调图前 append 了 `build_tracing_callbacks()`）。本模块负责**构造回调**，
**注入**在 lead_agent（已落地）/ 子代理 executor（#3611 已落地）。

**Q：`build_tracing_callbacks` 抛 RuntimeError 但 `_maybe_build_tracing_callbacks` 返回 `[]`，矛盾吗？**
A：不矛盾，两层语义不同。`build_tracing_callbacks` 是**显式构造**，坏了得知道（响亮报错）；
`_maybe_build_tracing_callbacks` 在**模型创建路径**上，追踪是 nice-to-have，不能因为它坏了就让
agent 起不来，所以吞掉异常返回 `[]`。

**Q：Langfuse 的 session_id 有什么用？**
A：把同一 `thread_id` 的多次 run 归到一个 Langfuse **Session**——面板上能看到一个对话的完整
历史，而不是散落的单次 trace。`inject_langfuse_metadata` 把 `thread_id` 映射成
`langfuse_session_id`。

**Q：前端设的 `langfuse_session_id` 会被后端覆盖吗？**
A：不会。`inject_langfuse_metadata` 用 `setdefault`——调用方已有的 key 优先。这让前端能把多个
run 归到一个自定义会话。

**Q：子代理（task 工具触发的）的 trace 飘成了独立 session，和父对话断开？**
A：这是 #3611 的征兆——子代理 executor 没注入父 `thread_id`/`user_id`。已修复：`_aexecute` 在图根
调 `inject_langfuse_metadata`，把父 `thread_id`→`langfuse_session_id`、捕获的 `user_id`→
`langfuse_user_id`、`subagent:<归一化名>`→`langfuse_trace_name`。现在子代理 trace 归到父对话的
Session 卡片下，带 `subagent:deep-research` 这样的 trace 名。

**Q：不配任何 tracing 会有开销吗？**
A：零。`get_enabled_tracing_providers()` 返回 `[]` → `build_tracing_callbacks()` 直接返回 `[]`，
`build_langfuse_trace_metadata()` 返回 `{}`。没有回调对象，没有注入，没有额外 import。

**Q：M12 落地后 models 的 `attach_tracing=True` 怎么就「自动」生效了？**
A：`_maybe_build_tracing_callbacks()` 早就写好了 `from deerflow.tracing import build_tracing_callbacks`
的懒导入，只是 tracing 包不存在时 ImportError → 返回 `[]`。M12 创建了 tracing 包，这个 import
成功了，`attach_tracing=True` 的独立调用方（如未来的 MemoryUpdater）就开始在模型级挂真回调了。
models 代码一行没改——这就是懒导入设计的好处。
