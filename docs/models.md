# 6. models.md — 模型工厂（thinking / tracing / vLLM 推理模型）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：models 模块是一个「把 `config.yaml` 里的一行文字，变成一个能 `.invoke()` 对话的 AI 模型实例」的**转换器**。你只要在配置里写 `use: langchain_deepseek:ChatDeepSeek`，代码里一行 `create_chat_model("deepseek")` 就拿到模型对象——工厂负责「找到类 → 读配置 → 处理 thinking / 流式 / 追踪等 provider 差异 → 实例化」，你不用 `import` 任何具体的模型库。它是 Agent 能说话的前提，被主代理、记忆抽取、标题生成等多个调用方共享。

> 配套代码：[models/factory.py](../backend/packages/harness/deerflow/models/factory.py) · [models/vllm_provider.py](../backend/packages/harness/deerflow/models/vllm_provider.py) · [models/__init__.py](../backend/packages/harness/deerflow/models/__init__.py) · [config/model_config.py](../backend/packages/harness/deerflow/config/model_config.py)（`ModelConfig`）· [config/app_config.py](../backend/packages/harness/deerflow/config/app_config.py#L198)（`get_model_config`）· [reflection/resolver.py](../backend/packages/harness/deerflow/reflection/resolver.py#L98)（`resolve_class`）。配置实例见 [../backend/config.yaml](../backend/config.yaml) + [../config.example.yaml](../config.example.yaml)。

## 学完这篇你能回答什么（learning outcomes）

- 为什么 agent 项目要**配置驱动 + 反射加载**模型，而不是在代码里 `from langchain_openai import ChatOpenAI`？（换模型零改代码 / 缺包按需报错 / 业务代码与 provider 解耦）
- **thinking 模式为什么有四种「关闭」路径**？每种对应哪种 provider 的 API 风格？（OpenAI 兼容 `extra_body` / vLLM `chat_template_kwargs` / Anthropic 原生参数 / 用户自定义）
- 推理模型流式首字可能要 90~150s，**`stream_chunk_timeout` 为什么默认 240s** 而非 LangChain 自带的 60s？为什么它只对 `ChatOpenAI` 注入、给其它 provider 必须剔除？
- **链路追踪回调挂在哪里**是个高频坑——图内调用方与独立调用方的 `attach_tracing` 为什么取值**相反**？挂错分别会怎样（重复 span / langfuse 元数据被剥离）？
- vLLM 推理模型的 `reasoning` 字段为什么会被 LangChain 默认丢掉？多轮「想完→调工具→继续想」丢失它后果是什么？`VllmChatModel` 重写哪**三个钩子**保住它？
- `supports_thinking=False` 却请求 `thinking_enabled=True`，工厂为什么**直接抛 ValueError** 而非静默忽略？（fail-fast 哲学）

> 这些都是 LLM 应用 / agent 工程面试的高频点——「怎么接多家模型」「推理模型的工程坑」「链路追踪怎么挂」。

---

## 1. 为什么需要它

如果没有模型工厂，直接在业务代码里 `import` 具体 provider，你会踩到这些坑：

| 场景 | 没有工厂 | 有工厂 |
|------|----------|--------|
| 切换模型 | 改代码 `from langchain_openai import ChatOpenAI` → `from langchain_deepseek import ChatDeepSeek`，到处改 | 只改 `config.yaml` 一行 |
| 推理模型（thinking） | 手动拼 `extra_body={"chat_template_kwargs": {"enable_thinking": True}}`，每个 provider 写法不同 | 配置里写 `when_thinking_enabled`，工厂按 provider 自动拼 |
| 流式输出超时 | 推理模型（DeepSeek-R1）首字要 90s，默认 60s 超时直接报错 | 工厂自动放宽到 240s |
| token 用量统计 | 自定义 `base_url` 时 LangChain 不自动开 `stream_usage`，统计永远为 0 | 工厂自动开 |
| 链路追踪 | 不知道该在哪挂回调，要么漏挂要么重复 | `attach_tracing` 参数明确两个调用方 |
| 能力不匹配 | 把 `reasoning_effort` 传给不支持的模型，运行时 `TypeError` | 工厂按 `supports_*` 能力声明提前剔除 |

一句话：**工厂把「跟模型打交道」的所有脏活、坑、provider 差异，集中收口到一个函数。** 这是「配置即代码」——接入任意 LangChain 兼容的模型，无需改源码。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉 LLM API / LangChain 的话，先读这一节。

### LLM / chat model / `.invoke()`

**LLM**（大语言模型）就是你脑子里那个「能对话的 AI」（DeepSeek、GPT、Claude……）。在代码里，一个**模型实例**（model instance）是一个对象，你把问题丢给它、它把答案吐回来，最常见的调用方式是：

```python
model.invoke("你好")   # → AIMessage("你好！有什么可以帮你的？")
```

LangChain 把「能这样对话的模型」抽象成一个基类 `BaseChatModel`——mini 工厂返回的就是它的某个子类实例（`ChatDeepSeek` / `ChatOpenAI` / `VllmChatModel`……）。

### provider / OpenAI 兼容

**provider**（提供者）= 提供模型 API 的那家公司或那个服务（DeepSeek、OpenAI、Anthropic、豆包……）。有意思的是，现在大多数 provider 都**模仿 OpenAI 的接口格式**对外提供服务，这叫 **OpenAI 兼容**（OpenAI-compatible）——好处是你能用同一套客户端代码连不同的服务，只要改 `base_url`（服务的网址）。

### 反射（reflection）/ `use` 字段

**反射** = 「**用一串字符串名字，找到真正的类**」。

```python
resolve_class("langchain_deepseek:ChatDeepSeek", BaseChatModel)
# 等价于：from langchain_deepseek import ChatDeepSeek
```

`use` 字段（如 `langchain_deepseek:ChatDeepSeek`）就是这串字符串，格式是 `包.模块:类名`。好处：代码里**不写死**任何 `import langchain_deepseek`，你没装这个包时代码也能加载别的模型（只是用到这一个时才报「缺包」）。这是「配置驱动」的基础。

### thinking / reasoning_effort

**thinking**（扩展思考 / reasoning）= 有些模型（DeepSeek-R1、GPT-5、Qwen3）在回答前会**先想一段时间**，把「思考过程」也吐出来。优点是答案更准，缺点是**费 token、慢**。

**`reasoning_effort`**（推理力度，`low`/`medium`/`high`）= 控制模型「想多久」的旋钮。注意：**不是所有模型都接受这个参数**，工厂会按能力声明决定传不传。

### vLLM

**vLLM** = 一个「自己在 GPU 机器上跑大模型」的开源推理引擎。它对外暴露一个和 OpenAI 一模一样的 HTTP 接口，所以一般直接用 `ChatOpenAI` 连它即可。**只有**在 vLLM 上跑「会思考的推理模型」时才需要本模块的 `VllmChatModel`（§6.5 详述）。

---

## 3. 整体结构：它在系统里的位置

```
models/
├── __init__.py          # 模块门面：只导出 create_chat_model / get_default_model
├── factory.py           # 工厂主体（本模块核心）
└── vllm_provider.py     # vLLM 推理模型专用 provider（VllmChatModel）

config/
├── model_config.py      # ModelConfig（模型档案 schema）
└── app_config.py        # AppConfig.get_model_config(name)（按名查找）

reflection/
└── resolver.py          # resolve_class（反射加载，缺包给可操作提示）
```

它在系统里的位置：

```
config.yaml 的 models[].use（一串字符串）
        │
        │  get_model_config(name)   ← app_config.py:198，O(1) 查表
        ▼
   ModelConfig（档案：能力声明 + 透传参数）
        │
        │  resolve_class(use, BaseChatModel)  ← resolver.py:98，反射加载
        ▼
models/factory.py 的 create_chat_model(...)   ← 处理 thinking/流式/追踪差异
        │
        ▼
   一个能 .invoke() 的 BaseChatModel 实例
        │
        ├─→ lead_agent（主代理对话）         #25 agents.md
        ├─→ MemoryUpdater（记忆抽取）        #18 memory.md
        └─→ TitleMiddleware（生成标题）      #24 middlewares.md
```

### 逐文件作用

- **[models/__init__.py](../backend/packages/harness/deerflow/models/__init__.py)**——模块门面。只导出两个公开函数 `create_chat_model` / `get_default_model`，把内部辅助函数挡在包外。外部 `from deerflow.models import create_chat_model` 就靠它。
- **[models/factory.py](../backend/packages/harness/deerflow/models/factory.py)**——工厂主体，本模块的核心。把「找到类 → 读配置 → 处理 thinking/stream/tracing 等特殊参数 → 实例化」收口到 `create_chat_model` 一个函数。这是所有 provider 共用的通用逻辑，与具体 provider 无关。
- **[models/vllm_provider.py](../backend/packages/harness/deerflow/models/vllm_provider.py)**——vLLM 推理模型专用 provider（`VllmChatModel` + 几个辅助函数）。它**不是**工厂的一部分，而是一个「可以被 config 的 `use` 字段反射加载的具体模型类」，和 `langchain_openai:ChatOpenAI` 是同层东西。独立成文件让通用工厂不被 vLLM 专有兼容代码污染——不用 vLLM 的人完全不需要读它。

---

## 4. 核心概念

### 4.1 `ModelConfig` —— 一个模型的「档案」

[config/model_config.py](../backend/packages/harness/deerflow/config/model_config.py) 用 pydantic 定义了单个模型的所有信息：

- **必需字段**（[第 20–27 行](../backend/packages/harness/deerflow/config/model_config.py#L20)）：`name`（业务名，代码引用用）、`use`（类路径）、`model`（模型标识，如 `deepseek-chat`）。
- **能力声明**（[第 37–44 行](../backend/packages/harness/deerflow/config/model_config.py#L37)）：`supports_thinking` / `supports_vision` / `supports_reasoning_effort`——告诉工厂这个模型「能干什么」，工厂据此决定传哪些参数。
- **思考开关**（[第 47–54 行](../backend/packages/harness/deerflow/config/model_config.py#L47)）：`when_thinking_enabled` / `when_thinking_disabled` / `thinking`（快捷别名）。
- **透传字段**：`api_key`、`temperature`、`max_tokens`、`base_url`……靠 `extra="allow"`（[第 17 行](../backend/packages/harness/deerflow/config/model_config.py#L17)）——写啥都原样传给模型类构造函数，无需预先定义。

### 4.2 反射加载（`resolve_class`）

[reflection/resolver.py:98](../backend/packages/harness/deerflow/reflection/resolver.py#L98) 的 `resolve_class(class_path, base_class)` 做三件事：

1. `importlib.import_module` 按 `package.module:ClassName` 动态导入；
2. 校验拿到的是**类**（`isinstance(x, type)`）；
3. 校验它是 `base_class`（这里是 `BaseChatModel`）的**子类**（`issubclass`），否则 `ValueError`。

缺包时它会查一张已知包名→pip 名的映射表（`langchain_deepseek → langchain-deepseek`），抛出**可操作的安装提示** `uv add langchain-deepseek`——而不是让你对着一个 `ModuleNotFoundError` 猜半天。

### 4.3 三种「参数来源」的优先级

工厂实例化时，最终参数 = `模型类(**kwargs, **config派生设置)`（[第 234 行](../backend/packages/harness/deerflow/models/factory.py#L234)）。记住一个规则：

> **config.yaml 派生的设置 > 调用方传入的 kwargs**（`reasoning_effort` 有专门门控除外）。

即**配置文件说了算**。设计意图：避免调用方意外覆盖掉 thinking / stream 等关键设置——这些是运维级开关，应该集中在配置里，不该被某处代码随手改掉。

### 4.4 thinking 快捷别名 = `when_thinking_enabled` 的合并写法

`thinking` 字段是 `when_thinking_enabled` 的**快捷写法**。工厂在 [第 185–189 行](../backend/packages/harness/deerflow/models/factory.py#L185) 先把它们合并成一个 `effective_wte`：

```python
effective_wte = dict(model_config.when_thinking_enabled) if ... else {}
if model_config.thinking is not None:
    merged_thinking = {**(effective_wte.get("thinking") or {}), **model_config.thinking}
    effective_wte = {**effective_wte, "thinking": merged_thinking}
```

所以你写 `thinking: {...}` 还是 `when_thinking_enabled: {thinking: {...}}`，最终合并结果一样——快捷别名只是少打几个字。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 `create_chat_model` —— 工厂主体（[第 119 行](../backend/packages/harness/deerflow/models/factory.py#L119)）

签名：

```python
def create_chat_model(
    name: str | None = None,          # 模型名；None=配置里第一个
    thinking_enabled: bool = False,   # 开思考（仅 supports_thinking=True 生效）
    *,
    app_config: AppConfig | None = None,  # 显式配置；None=读全局 config.yaml
    attach_tracing: bool = True,      # 模型级挂追踪回调（图内须传 False）
    **kwargs,                          # 额外构造参数（如 reasoning_effort）
) -> BaseChatModel
```

内部按 **8 步**走（行号对照源码）：

| 步 | 做什么 | 行号 |
|----|--------|------|
| 0 | 取配置：`config = app_config or get_app_config()`；按名查 `ModelConfig`，找不到/空配置抛 `ValueError` | [152–159](../backend/packages/harness/deerflow/models/factory.py#L152) |
| 1 | 反射加载模型类：`resolve_class(model_config.use, BaseChatModel)`（缺包给安装提示） | [162](../backend/packages/harness/deerflow/models/factory.py#L162) |
| 2 | 把 `ModelConfig` 序列化成构造参数字典，**排除纯元数据字段**（`name`/`display_name`/能力声明/thinking 设置都排除——它们不是构造参数） | [167–181](../backend/packages/harness/deerflow/models/factory.py#L167) |
| 3 | thinking 模式处理：合并 `effective_wte`；`thinking_enabled=True` 时校验 `supports_thinking` 并注入开启设置；`=False` 时按四路径关闭 | [183–217](../backend/packages/harness/deerflow/models/factory.py#L183) |
| 4 | reasoning_effort 门控：不支持时从 kwargs 与 config 中**剔除** | [219–222](../backend/packages/harness/deerflow/models/factory.py#L219) |
| 5 | OpenAI 兼容默认值：`stream_usage` + `stream_chunk_timeout` | [225–226](../backend/packages/harness/deerflow/models/factory.py#L225) |
| 6 | `stream_usage` 兜底：模型类自身接受该字段且未配时开 | [229–231](../backend/packages/harness/deerflow/models/factory.py#L229) |
| 7 | 实例化：`model_class(**kwargs, **model_settings_from_config)`（config 优先） | [234](../backend/packages/harness/deerflow/models/factory.py#L234) |
| 8 | 模型级追踪回调（仅独立调用方；图内须 `attach_tracing=False`） | [237–242](../backend/packages/harness/deerflow/models/factory.py#L237) |

### 5.2 thinking 的四种「关闭」路径（[第 197–217 行](../backend/packages/harness/deerflow/models/factory.py#L197)）

很多推理模型**默认就开着 thinking**（费 token、慢）。当用户没要求 thinking 时，得显式关掉。但**不同 provider 关闭的写法完全不同**，工厂按优先级顺序逐个尝试四条路径：

| 优先级 | 触发条件 | 关闭写法 | 行号 |
|--------|----------|----------|------|
| 1（最高） | 配了 `when_thinking_disabled` | 原样用用户定义（最高优先级，用户说了算） | [199–201](../backend/packages/harness/deerflow/models/factory.py#L199) |
| 2 | OpenAI 兼容网关（`extra_body.thinking.type` 存在） | `extra_body.thinking.type=disabled` + `reasoning_effort=minimal` | [202–208](../backend/packages/harness/deerflow/models/factory.py#L202) |
| 3 | vLLM（`chat_template_kwargs` 里有 thinking/enable_thinking） | `chat_template_kwargs.{thinking,enable_thinking}=False` | [209–214](../backend/packages/harness/deerflow/models/factory.py#L209) |
| 4 | Anthropic 原生（`thinking.type` 存在） | `thinking={type: disabled}` 直接构造参数 | [215–217](../backend/packages/harness/deerflow/models/factory.py#L215) |

所以你**换 provider 不用改代码**——工厂自动挑对的关闭方式。配套的还有 `_vllm_disable_chat_template_kwargs`（[第 55 行](../backend/packages/harness/deerflow/models/factory.py#L55)），只有当原配置里真出现了 `thinking`/`enable_thinking` 时才构造关闭负载，否则不向模型注入它不认识的键。

### 5.3 attach_tracing 的两个调用方（[第 237–242 行](../backend/packages/harness/deerflow/models/factory.py#L237)）

这是最容易踩的坑。链路追踪（LangSmith/Langfuse）的回调，**挂在哪里**很有讲究：

```
┌─────────────────────────────────────────────┐
│  LangGraph 图（一次对话）                     │
│   └─ Lead Agent 节点                         │
│        └─ create_chat_model(...)  ← 图内调用 │
│        └─ TitleMiddleware 也调模型 ← 图内调用 │
└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐
│  后台记忆更新线程（MemoryUpdater）            │
│   └─ create_chat_model(...)  ← 独立调用      │
└─────────────────────────────────────────────┘
```

- **独立调用方**（MemoryUpdater、临时脚本）：不在图里，没人帮它挂回调 → **`attach_tracing=True`（默认）**，工厂在模型级挂回调，保证也有 trace。
- **图内调用方**（`make_lead_agent`、`TitleMiddleware`）：图根已经挂了一份回调 → **必须 `attach_tracing=False`**。

为什么图内必须关？如果图内也开，同一次 LLM 调用会**发两份 span**（图根一份、模型级一份）；更糟的是，模型级回调会让这次调用变成「嵌套观测」，导致 `session_id` / `user_id` 这些元数据被追踪系统**剥离**——你的 trace 里就看不到是哪个用户、哪个会话了。（追踪的图根注入详见 [#16 tracing.md](tracing.md)。）

工厂用懒加载 `from deerflow.tracing import build_tracing_callbacks`（[第 104 行](../backend/packages/harness/deerflow/models/factory.py#L104)），tracing 模块没配置时 `_maybe_build_tracing_callbacks` 返回空列表（零副作用），配置了 LangSmith/Langfuse 后自动生效，无需改本文件。

### 5.4 `VllmChatModel` —— vLLM 推理模型保住 `reasoning` 字段（[vllm_provider.py:196](../backend/packages/harness/deerflow/models/vllm_provider.py#L196)）

`VllmChatModel` 继承 `ChatOpenAI`（所以 OpenAI 兼容的所有行为都还在），只**重写三个钩子**，把 vLLM 多出来的非标准 `reasoning` 字段在三处都保住：

```
                vLLM 返回的 reasoning 字段
                          │
   ┌──────────────────────┼──────────────────────┐
   ▼                      ▼                      ▼
非流式响应            流式 delta              多轮请求
(_create_chat_result) (_convert_chunk_... )  (_get_request_payload)
保住到 AIMessage      保住到 message          回灌到 outgoing
.additional_kwargs    chunk 的                payload 的 assistant
                      additional_kwargs       消息上
```

| 钩子（被重写的方法） | 保住 reasoning 的地方 | 行号 | 为什么需要 |
|----------------------|----------------------|------|-----------|
| `_create_chat_result` | 非流式响应的 `AIMessage.additional_kwargs` | [244](../backend/packages/harness/deerflow/models/vllm_provider.py#L244) | 一次返回完整结果时别丢 |
| `_convert_chunk_to_generation_chunk` | 流式 delta 的 message chunk | [270](../backend/packages/harness/deerflow/models/vllm_provider.py#L270) | 一块一块吐时每一块都别丢，拼起来才完整 |
| `_get_request_payload` | outgoing payload 的 assistant 消息 | [211](../backend/packages/harness/deerflow/models/vllm_provider.py#L211) | 多轮时把上一轮的 reasoning 原样回传给 vLLM |

此外 `_get_request_payload` 在发请求前还顺手调 `_normalize_vllm_chat_template_kwargs`（[第 58 行](../backend/packages/harness/deerflow/models/vllm_provider.py#L58)），把 DeerFlow 早期文档用的旧键名 `thinking` 归一化成 vLLM 0.19.0 的 `enable_thinking`，让旧配置继续能用。

> 上游 deer-flow 有个类似的 `PatchedChatDeepSeek`，解决的是 DeepSeek / 豆包 / Kimi 兼容协议里 `reasoning_content` 字段被丢的同类问题。mini ship 的是面向 vLLM 的 `VllmChatModel`（保 `reasoning` 字段），思路一致：都是「LangChain 默认适配器会丢掉某个 provider 私有的思考字段，多轮回传时模型行为会出问题，于是继承 + 重写 payload 钩子把它补回去」。

---

## 6. 设计权衡与踩坑

### 6.1 `stream_chunk_timeout` 为什么默认 240 秒？（[第 36 行](../backend/packages/harness/deerflow/models/factory.py#L36)）

LangChain 的 OpenAI 客户端有个参数 `stream_chunk_timeout`：流式输出时，相邻两个数据块之间最长能等多久，超时就报 `StreamChunkTimeoutError`。它的默认值是 **60 秒**。

但推理模型（DeepSeek-R1、GPT-5、豆包-thinking）在「思考」阶段可能**几十秒甚至 150 秒**才吐第一个字——这是正常的，不是卡死。60 秒会误判超时，直接中断。所以工厂对 `ChatOpenAI` 默认注入 **240 秒**（`_apply_stream_chunk_timeout_default`，[第 83 行](../backend/packages/harness/deerflow/models/factory.py#L83)）。可在 config.yaml 单模型覆盖（`stream_chunk_timeout: 120`）。

> 注意：这个参数是 `langchain_openai:ChatOpenAI` **专有**的。传给 DeepSeek/Ollama 等其它 provider 会触发 `TypeError: unexpected keyword argument`。所以工厂对**非 OpenAI provider 会剔除该键**（[第 89–90 行](../backend/packages/harness/deerflow/models/factory.py#L89)）。

### 6.2 `stream_usage` 为什么默认开？（[第 69 行](../backend/packages/harness/deerflow/models/factory.py#L69)）

`stream_usage=True` 让模型在流式响应里附带 token 用量。没有它，`TokenUsageMiddleware`（见 [#24 middlewares.md](middlewares.md)）就**统计不到任何 token**，前端用量面板永远为 0。

LangChain 只在「标准 OpenAI」时自动开 `stream_usage`。但 DeerFlow 常用 OpenAI **兼容**网关（豆包、DeepSeek 自建 `base_url`），这种情况下 LangChain 不会自动开——所以工厂 `_enable_stream_usage_by_default` 检测到 `base_url`（或 `openai_api_base`）时强制开（[第 79–80 行](../backend/packages/harness/deerflow/models/factory.py#L79)）。

第 6 步（[第 229–231 行](../backend/packages/harness/deerflow/models/factory.py#L229)）还有个兜底：如果上面没开，但模型类**自身**接受 `stream_usage` 字段（`model_class.model_fields` 里有它）且未显式配置，也开。

### 6.3 reasoning_effort 门控（[第 219 行](../backend/packages/harness/deerflow/models/factory.py#L219)）

`reasoning_effort`（low/medium/high）不是所有模型都支持。工厂根据 `supports_reasoning_effort` 决定：不支持时，无论你从 kwargs 还是 config 传了 `reasoning_effort`，都**剔除**——否则模型类构造时报 `TypeError`。

### 6.4 fail-fast：能力不匹配直接报错（[第 192–193 行](../backend/packages/harness/deerflow/models/factory.py#L192)）

`thinking_enabled=True` 但模型 `supports_thinking=False` 时，工厂**直接抛 `ValueError`**（fail-fast），而不是静默忽略。为什么？静默会让「我以为开了思考其实没开」的 bug **极难排查**——你以为模型在认真想，其实啥都没干。fail-fast 把错误顶到调用现场，一眼能看见。

### 6.5 vLLM 推理模型为什么需要专门 provider？

**问题**：vLLM 0.19.0 跑推理模型（如 Qwen3 开思考）时，会在返回结果里多塞一个 OpenAI 官方接口里**没有**的字段 `reasoning`。LangChain 默认的 `ChatOpenAI` 不认识这个非标准字段，会把它**直接丢掉**。

**后果**：在「想完→调工具→继续想」这种交替流程里，vLLM 期望**上一轮 AI 的思考内容要在下一轮原样回传给它**（这样模型才知道自己刚才想到了哪）。一旦被 LangChain 丢掉，下一轮请求里就没了这个字段，vLLM 的行为就会出问题。

**解法**：`VllmChatModel` 继承 `ChatOpenAI`，重写三个钩子把 `reasoning` 保住（§5.4）。**只有 vLLM + 推理模型这个窄场景才需要它**。

---

## 7. 配置与用法

### 7.1 最简配置（config.yaml）

```yaml
models:
  - name: deepseek
    use: langchain_deepseek:ChatDeepSeek
    model: deepseek-chat
    api_key: $DEEPSEEK_API_KEY     # $ 开头会从环境变量读（详见 #3 config.md）
    temperature: 0.7
```

```python
from deerflow.models import create_chat_model

model = create_chat_model("deepseek")
print(model.invoke("你好").content)
```

### 7.2 推理模型（thinking）

```yaml
models:
  - name: deepseek-r1
    use: langchain_deepseek:ChatDeepSeek
    model: deepseek-reasoner
    api_key: $DEEPSEEK_API_KEY
    supports_thinking: true
    when_thinking_enabled:
      extra_body:
        chat_template_kwargs:
          enable_thinking: true
```

```python
# 开思考
model = create_chat_model("deepseek-r1", thinking_enabled=True)
# 不开（工厂按 §5.2 自动构造 enable_thinking: false 的关闭负载）
model = create_chat_model("deepseek-r1", thinking_enabled=False)
```

### 7.3 多模态（vision）

```yaml
models:
  - name: qwen-vl
    use: langchain_openai:ChatOpenAI
    model: qwen-vl-max
    api_key: $DASHSCOPE_API_KEY
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    supports_vision: true
```

> 因为配了 `base_url`，工厂会自动开 `stream_usage`（否则用量统计为 0）和 `stream_chunk_timeout=240`。

### 7.4 在 Agent / 中间件里调用（图内 → 必须 `attach_tracing=False`）

```python
# make_lead_agent / TitleMiddleware 等图内调用方：
model = create_chat_model(
    name=model_name,
    thinking_enabled=thinking_enabled,
    app_config=cfg,
    attach_tracing=False,   # ← 图根已挂回调，这里必须关（§5.3）
)
```

### 7.5 在后台任务里调用（独立 → 默认 `attach_tracing=True`）

```python
# MemoryUpdater 等独立调用方：
model = create_chat_model(name="deepseek", thinking_enabled=False)
# 不传 attach_tracing，默认 True，工厂自动挂追踪回调
```

### 7.6 单元测试里（不依赖 config.yaml）

```python
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

cfg = AppConfig(models=[ModelConfig(name="t", use="fake:X", model="m", temperature=0.5)])
model = create_chat_model("t", app_config=cfg)  # 显式注入，不读磁盘（hermetic）
```

### 7.7 vLLM 自托管推理模型（用 `VllmChatModel` 保住 `reasoning`）

当你在 vLLM 上跑 Qwen3 这类**会思考的推理模型**时，把 `use` 写成 `deerflow.models.vllm_provider:VllmChatModel`（详见 §5.4 / §6.5）。其余字段和普通 OpenAI 兼容模型一样。

```yaml
models:
  - name: qwen3-32b-vllm
    use: deerflow.models.vllm_provider:VllmChatModel   # ← 关键：用 VllmChatModel 而非 ChatOpenAI
    model: Qwen/Qwen3-32B
    api_key: $VLLM_API_KEY
    base_url: http://localhost:8000/v1
    supports_thinking: true
    when_thinking_enabled:
      extra_body:
        chat_template_kwargs:
          enable_thinking: true      # Qwen 系开关思考的键名（旧键名 thinking 也会被自动归一化）
```

```python
model = create_chat_model("qwen3-32b-vllm", thinking_enabled=True)
# 模型返回的 reasoning 字段会被保住到 AIMessage.additional_kwargs["reasoning"]，
# 多轮「想完→调工具→继续想」时也会原样回传给 vLLM，不会丢。
```

> `VllmChatModel` 只在「vLLM + 推理模型」这个窄场景下需要。跑 vLLM 普通模型（如 Qwen2.5-VL 视觉模型）仍用默认 `langchain_openai:ChatOpenAI`——没有 reasoning 字段要保。不跑 vLLM（用云 API、Ollama 等）则完全用不到本文件。

---

## 8. 与其它模块的关系

```
config/model_config.py ──┐
config/app_config.py ─────┤  (get_model_config)
reflection (resolve_class)┤
                          ▼
                   models/factory.py (create_chat_model)
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
  tracing (#16)      memory (#18)       lead_agent (#25)
  attach_tracing     MemoryUpdater      make_lead_agent
  懒加载              独立调用(True)      图内调用(False)
                                          │
                                     TitleMiddleware (#24)
                                     生成标题时调模型
```

- **依赖**：[config](config.md)（读 `ModelConfig`）、[reflection](../backend/packages/harness/deerflow/reflection/resolver.py)（`resolve_class` 反射加载）、[tracing](tracing.md)（懒加载，可选，未配置零开销）。
- **被依赖**：
  - [tracing](tracing.md)：`build_tracing_callbacks` 被 factory 懒加载。
  - [memory](memory.md)：`MemoryUpdater` 用 `create_chat_model(thinking_enabled=False)` 做记忆抽取（独立调用方，默认挂 tracing）。
  - [agents](agents.md)：`make_lead_agent` 用 `create_chat_model(..., attach_tracing=False)` 做主模型（图内调用方，必须关 tracing）。
  - [middlewares](middlewares.md)：`TitleMiddleware` 生成标题时调模型；`TokenUsageMiddleware` 依赖 `stream_usage` 开启才能统计 token。

---

## 9. 常见问题 / 排错

### Q1：`ModuleNotFoundError: No module named 'langchain_deepseek'`

你配了 `use: langchain_deepseek:ChatDeepSeek` 但没装包。按提示装：

```bash
uv add langchain-deepseek
```

（`resolve_class` 会查映射表给出这个提示。）

### Q2：开了 `thinking_enabled=True` 但模型没思考

检查两点：
1. `config.yaml` 里该模型是否设了 `supports_thinking: true`（没设会直接抛 `ValueError`，§6.4）。
2. 是否配了 `when_thinking_enabled` 告诉工厂「怎么开」。不同 provider 开法不同（见 §5.2 四路径表）。

### Q3：推理模型流式输出报 `StreamChunkTimeoutError`

默认 240s 还不够（极少见）。在 config.yaml 单模型调大：

```yaml
stream_chunk_timeout: 600
```

### Q4：前端 token 用量永远是 0

多半是用了 OpenAI 兼容网关但没配 `base_url`，或模型类不支持 `stream_usage`。确认 config 里有 `base_url`，工厂会自动开 `stream_usage`（§6.2）。

### Q5：trace 里出现重复 span，且看不到 user_id

你在**图内**调用了 `create_chat_model` 但忘了传 `attach_tracing=False`（§5.3）。改成：

```python
create_chat_model(..., attach_tracing=False)
```

### Q6：`TypeError: unexpected keyword argument 'stream_chunk_timeout'`

你给非 OpenAI provider（如 DeepSeek/Ollama）传了 `stream_chunk_timeout`。工厂本应自动剔除（§6.1）——如果你看到这个错，说明工厂逻辑被改坏了，检查 `_apply_stream_chunk_timeout_default`（[第 83 行](../backend/packages/harness/deerflow/models/factory.py#L83)）。

### Q7：vLLM 推理模型多轮对话丢失了「思考内容」/ 行为异常

你很可能在 vLLM 上跑推理模型（如 Qwen3 开了思考），但 `use` 用了默认的 `langchain_openai:ChatOpenAI`。LangChain 的 OpenAI 适配器会丢掉 vLLM 多出来的非标准 `reasoning` 字段，导致多轮「想完→调工具→继续想」时上一轮思考没回传（§6.5）。

改成 `use: deerflow.models.vllm_provider:VllmChatModel`（见 §7.7）。非推理的 vLLM 模型（如 Qwen2.5-VL）不需要改，保持 `ChatOpenAI` 即可。

---

## 小结

模型工厂的精髓是**把 provider 差异和运维坑收口到一个函数**。记住四件事：

1. **配置驱动 + 反射**：换模型只改 `config.yaml`，靠 `resolve_class` 反射加载，缺包给可操作提示。
2. **thinking / stream / tracing 三大坑都自动处理**：四路径关闭、240s 超时、`stream_usage` 默认开、`attach_tracing` 双调用方。
3. **vLLM 推理模型有专门 provider**：跑 vLLM 推理模型时用 `VllmChatModel`（`vllm_provider.py`）保住非标准的 `reasoning` 字段；其它情况用默认 `ChatOpenAI`。
4. **fail-fast**：能力不匹配（如不支持 thinking 却要求开）直接报错，不静默。

上一篇：[#5 user_context.md](user_context.md)（用户上下文）· 下一篇：[#7 persistence.md](persistence.md)（应用持久化层——SQLAlchemy ORM + WAL 并发，是 checkpointer / run_event_store 的存储地基）。
