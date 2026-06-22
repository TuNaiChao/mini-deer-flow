# 16. tracing.md — 链路追踪（LangSmith / Langfuse 图根注入）

> **一句话定位**：tracing 让一次 agent run 产**一条完整 trace**——把所有 node 调用、LLM 调用、
> tool 调用串成一棵子 span 树，在 LangSmith / Langfuse 面板上可观测、可回放、可调试。
> 本模块负责在**图根**注入追踪回调 + 构造 Langfuse 的 trace 属性元数据。

读完 [models.md](models.md)（懂了 `create_chat_model` 与 `attach_tracing`）再看本篇最省事——
tracing 的核心就是「回调挂在哪一层」，而 models 的 `attach_tracing` 开关正是这个分层的旋钮。

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

回调必须在**图调用的根**注入——也就是 `lead_agent` / client 在调 `agent.astream(...)` 前，
把 callbacks append 进 `config["callbacks"]`。这样整棵 span 树挂在**一个根 trace** 下。

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
`config["metadata"]`。两条注入路径（gateway worker / 嵌入式 client）共用，防漂移。

**Langfuse 不在启用 provider 时返回 `{}`**——调用方可以无条件 merge 结果而不影响 LangSmith
或其它 tracer。这是「可选 provider 不污染必选路径」的不变量。

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

### 注入点（M17 lead_agent / M18 worker 落地后）

```python
# 图根注入（lead_agent 工厂或运行 worker）
config = {...}
callbacks = build_tracing_callbacks()
if callbacks:
    config["callbacks"] = [*config.get("callbacks", []), *callbacks]
inject_langfuse_metadata(config, thread_id=thread_id, user_id=user_id,
                         assistant_id=agent_name, model_name=model_name)
result = await agent.ainvoke(state, config=config)
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_tracing.py（26 个 hermetic 测试）
```

测试约定：env var 经 `monkeypatch.setenv/delenv` 控制；langfuse SDK（非依赖）用
`sys.modules` 注入 fake；langsmith tracer 经 monkeypatch 替身；models `attach_tracing` 联动用
`_FakeModelClass` + `resolve_class`/app_config 替身 + `_maybe_build_tracing_callbacks` spy，跑真
`create_chat_model` 到「挂回调」那步而不碰真模型 provider。

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
  （`DEFAULT_USER_ID` 给 langfuse_user_id 兜底）。
- **下游消费者**：`models.create_chat_model(attach_tracing=True)`（独立调用方，懒导入已就位）；
  M17 lead_agent / M18 worker / 嵌入式 client（图根注入回调 + metadata——Phase 7/8 落地）。
- **红线 #17**：图内 `create_chat_model` 一律 `attach_tracing=False`，图根统一注入，防重复 span。

---

## 常见问题 / 排错

**Q：为什么回调必须在图根注入，不能每个 LLM 调用各挂各的？**
A：那样会产出一堆碎片 trace（每个 LLM 调用一条），面板上串不成一棵完整树。图根注入让整棵 span
树挂在一个根 trace 下。这就是红线 #17——图内 `create_chat_model` 必须 `attach_tracing=False`。

**Q：我设了 `LANGFUSE_TRACING=true` 但 Langfuse 面板看不到 trace？**
A：常见原因：① 没装 `langfuse` 包（`_create_langfuse_handler` ImportError → RuntimeError）；
② 缺 `LANGFUSE_SECRET_KEY`/`LANGFUSE_PUBLIC_KEY`（validate ValueError）；③ 回调没在图根注入
（Phase 7/8 的 lead_agent/worker 还没接）。本模块只管**构造回调**，**注入**在 M17/M18。

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

**Q：不配任何 tracing 会有开销吗？**
A：零。`get_enabled_tracing_providers()` 返回 `[]` → `build_tracing_callbacks()` 直接返回 `[]`，
`build_langfuse_trace_metadata()` 返回 `{}`。没有回调对象，没有注入，没有额外 import。

**Q：M12 落地后 models 的 `attach_tracing=True` 怎么就「自动」生效了？**
A：`_maybe_build_tracing_callbacks()` 早就写好了 `from deerflow.tracing import build_tracing_callbacks`
的懒导入，只是 tracing 包不存在时 ImportError → 返回 `[]`。M12 创建了 tracing 包，这个 import
成功了，`attach_tracing=True` 的独立调用方（如未来的 MemoryUpdater）就开始在模型级挂真回调了。
models 代码一行没改——这就是懒导入设计的好处。
