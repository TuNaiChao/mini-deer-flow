# 6. models.md — 模型工厂（thinking / tracing）

> 对应模块：**M-models**（Phase 1）
> 源码：`backend/packages/harness/deerflow/models/factory.py`、`models/__init__.py`、`models/vllm_provider.py`
> 关联配置：`config/model_config.py`（`ModelConfig`）、`config/app_config.py`（`AppConfig.get_model_config`）

> **Phase 1 全维重审（2026-06-29）**：逐函数 diff `models/{factory,__init__,vllm_provider}.py`
> vs 最新上游（剥 docstring 后判逻辑差）。**无真实漂移**——`vllm_provider.py`（`VllmChatModel`
> 保 vLLM `reasoning` 字段）2026-06-26 已补、与上游一致；`factory.py` 的差异全是：① cosmetic
> （import 风格 / 错误提示中英）；② mini 的**防御性增强**（`_maybe_build_tracing_callbacks`
> 包了 try/except 防 tracing 不可导入、`get_default_model` 便利函数、空 models 配置的友好报错）；
> ③ 上游对 `CodexChatModel` / `MindIEChatModel` 的特殊处理——这两个 provider mini **有意不 ship**
> （§2.2 🟢低，langchain 已有 `ChatAnthropic` 等替代，mini 经 env/`$VAR` 直读），port 了反而
> ImportError。故无需补丁。`get_model_config` 的 O(1) 索引（#3688）已在 Phase 0 port，factory
> 透传受益。

---

## 1. 一句话定位

**模型工厂**是一个「把 `config.yaml` 里的一行文字，变成一个可以对话的 AI 模型实例」的转换器。

你只要在配置里写：

```yaml
models:
  - name: deepseek
    use: langchain_deepseek:ChatDeepSeek
    model: deepseek-chat
    api_key: $DEEPSEEK_API_KEY
```

代码里一行 `create_chat_model("deepseek")`，就能拿到一个能 `.invoke()` 的模型对象。工厂负责「找到类 → 读配置 → 处理特殊参数 → 实例化」，你不用 `import` 任何具体的模型库。

---

## 2. 为什么需要它

如果没有模型工厂，你会遇到这些痛点：

| 场景 | 没有工厂 | 有工厂 |
|------|----------|--------|
| 切换模型 | 改代码 `from langchain_openai import ChatOpenAI` → `from langchain_deepseek import ChatDeepSeek`，到处改 | 只改 `config.yaml` 一行 |
| 推理模型（thinking） | 手动拼 `extra_body={"chat_template_kwargs": {"enable_thinking": True}}`，每个 provider 写法不同 | 配置里写 `when_thinking_enabled`，工厂按 provider 自动拼 |
| 流式输出超时 | 推理模型（DeepSeek-R1）首字要 90s，默认 60s 超时直接报错 | 工厂自动放宽到 240s |
| token 用量统计 | 自定义 `base_url` 时 LangChain 不自动开 `stream_usage`，统计永远为 0 | 工厂自动开 |
| 链路追踪 | 不知道该在哪挂回调，要么漏挂要么重复 | `attach_tracing` 参数明确两个调用方 |

一句话：**工厂把「跟模型打交道」的所有脏活、坑、provider 差异，集中收口到一个函数。**

---

## 3. 核心概念

### 3.1 反射加载（reflection）

「反射」= **用字符串名字找到真正的类**。

```python
resolve_class("langchain_deepseek:ChatDeepSeek", BaseChatModel)
# 等价于：from langchain_deepseek import ChatDeepSeek
```

好处：代码里**不写死**任何 `import langchain_deepseek`。你没装这个包时，代码也能加载（只是用到时才报「缺包」）。这就是「配置驱动」的基础。

### 3.2 ModelConfig —— 一个模型的「档案」

`ModelConfig` 是一个模型的所有信息（`config/model_config.py`）：

- **必需**：`name`（业务名，代码引用用）、`use`（类路径）、`model`（模型标识，如 `deepseek-chat`）
- **能力声明**：`supports_thinking`、`supports_vision`、`supports_reasoning_effort`
- **思考开关**：`when_thinking_enabled`、`when_thinking_disabled`、`thinking`（快捷别名）
- **透传字段**：`api_key`、`temperature`、`max_tokens`、`base_url`……（`extra="allow"`，写啥都原样传给模型类）

### 3.3 三种「参数来源」的优先级

工厂实例化时，最终参数 = `模型类(**kwargs, **config派生设置)`。记住一个规则：

> **config.yaml 派生的设置 > 调用方传入的 kwargs**（`reasoning_effort` 有专门门控除外）。

即配置文件说了算。这是对齐 deer 的设计——避免调用方意外覆盖掉 thinking/stream 等关键设置。

---

## 4. 设计原理（讲清楚每个「为什么」）

### 4.1 thinking 模式：为什么要四种「关闭」路径？

很多推理模型**默认就开着 thinking**（费 token、慢）。当用户没要求 thinking 时，得显式关掉。但不同 provider 关闭的写法完全不同：

| Provider | thinking 在哪 | 关闭写法 |
|----------|--------------|----------|
| OpenAI 兼容网关 | `extra_body.thinking.type` | `{"thinking": {"type": "disabled"}}` + `reasoning_effort="minimal"` |
| vLLM（Qwen） | `extra_body.chat_template_kwargs` | `{"chat_template_kwargs": {"enable_thinking": False}}` |
| Anthropic 原生 | 直接构造参数 | `thinking={"type": "disabled"}` |
| 任意 | 用户自定义 | `when_thinking_disabled: {...}`（最高优先级） |

工厂按**优先级顺序**逐个尝试这四条路径，所以你换 provider 不用改代码。

> 红线关联：`thinking_enabled=True` 但模型 `supports_thinking=False` 时，工厂**直接抛 ValueError**（fail-fast），而不是静默忽略——静默会让「我以为开了思考其实没开」的 bug 极难排查。

### 4.2 stream_chunk_timeout 为什么默认 240 秒？

LangChain 的 OpenAI 客户端有个参数 `stream_chunk_timeout`：流式输出时，相邻两个数据块之间最长能等多久，超时就报错。它的默认值是 **60 秒**。

但推理模型（DeepSeek-R1、GPT-5、Doubao-thinking）在「思考」阶段可能**几十秒甚至 150 秒**才吐第一个字——这是正常的，不是卡死。60 秒会误判超时，直接中断。

所以工厂对 `ChatOpenAI` 默认注入 **240 秒**。可以在 config.yaml 单模型覆盖（`stream_chunk_timeout: 120`）。

> 注意：这个参数是 `langchain_openai:ChatOpenAI` **专有**的。传给 DeepSeek/Ollama 等其它 provider 会触发 `TypeError: unexpected keyword argument`。所以工厂对非 OpenAI provider 会**剔除**该键。

### 4.3 stream_usage 为什么默认开？

`stream_usage=True` 让模型在流式响应里附带 token 用量。没有它，`TokenUsageMiddleware`（M16）就**统计不到任何 token**，前端的用量面板永远是 0。

LangChain 只在「标准 OpenAI」时自动开 `stream_usage`。但 DeerFlow 常用 OpenAI **兼容**网关（豆包、DeepSeek 自建 base_url），这种情况下 LangChain 不会自动开——所以工厂检测到 `base_url` 时强制开。

### 4.4 attach_tracing 的两个调用方（红线 #17）

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
- **图内调用方**（make_lead_agent、TitleMiddleware）：图根已经挂了一份回调 → **必须 `attach_tracing=False`**。

为什么图内必须关？如果图内也开，同一次 LLM 调用会**发两份 span**（图根一份、模型级一份）；更糟的是，模型级回调会让这次调用变成「嵌套观测」，导致 `session_id`/`user_id` 这些元数据被追踪系统**剥离**——你的 trace 里就看不到是哪个用户、哪个会话了。

> 工厂用懒加载 `from deerflow.tracing import build_tracing_callbacks`，tracing 模块（M12）没落地时自动降级为空（零副作用），落地后自动生效。

### 4.5 reasoning_effort 门控

`reasoning_effort`（low/medium/high）不是所有模型都支持。工厂根据 `supports_reasoning_effort` 决定：不支持时，无论你从 kwargs 还是 config 传了 `reasoning_effort`，都**剔除**——否则模型类报 `TypeError`。

### 4.6 为什么 vLLM 推理模型需要专门的 provider（VllmChatModel）？

**vLLM** 是一个「自己在 GPU 机器上跑大模型」的开源推理引擎，对外暴露一个和 OpenAI 一模一样的 HTTP 接口。所以一般情况下，你直接用 LangChain 自带的 `langchain_openai:ChatOpenAI` 连它就行——**不需要**本节这个专门 provider。

只有一种情况需要：**你在 vLLM 上跑「会先想一想的推理模型」（reasoning model，比如 Qwen3 开了思考模式）**。这时会遇到一个字段丢失的坑。

**问题是什么？** vLLM 0.19.0 跑推理模型时，会在返回结果里多塞一个 OpenAI 官方接口里**没有**的字段：`reasoning`（模型的思考过程）。但 LangChain 默认的 `ChatOpenAI` 不认识这个非标准字段，会把它**直接丢掉**。

**为什么丢掉会出问题？** 在「想完→调工具→继续想」这种交替流程里，vLLM 期望**上一轮 AI 的思考内容要在下一轮原样回传给它**（这样模型才知道自己刚才想到了哪）。一旦被 LangChain 丢掉，下一轮请求里就没了这个字段，vLLM 的行为就会出问题。

**怎么解决？** `models/vllm_provider.py` 提供了 `VllmChatModel`——它继承 `ChatOpenAI`（所以 OpenAI 兼容的所有行为都还在），只**重写三个方法**，把 `reasoning` 字段在三处都保住：

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

| 钩子（被重写的方法） | 保住 reasoning 的地方 | 为什么需要 |
|----------------------|----------------------|-----------|
| `_create_chat_result` | 非流式响应的 `AIMessage.additional_kwargs` | 一次返回完整结果时别丢 |
| `_convert_chunk_to_generation_chunk` | 流式 delta 的 message chunk | 一块一块吐时每一块都别丢，拼起来才完整 |
| `_get_request_payload` | outgoing payload 的 assistant 消息 | 多轮时把上一轮的 reasoning 原样回传给 vLLM |

**额外赠送**：vLLM 0.19.0 的 Qwen 解析器用 `extra_body.chat_template_kwargs.enable_thinking` 开关思考，但 DeerFlow 早期文档用的是旧键名 `thinking`。`_get_request_payload` 在发请求前会顺手把 `thinking` 归一化成 `enable_thinking`，让旧配置继续能用（工厂 §4.1 的 vLLM 关闭路径与之配套）。

> **什么时候用 `VllmChatModel`，什么时候用普通 `ChatOpenAI`？**
> - 跑 vLLM **推理模型**（开了思考、会返回 `reasoning` 字段）→ 用 `deerflow.models.vllm_provider:VllmChatModel`。
> - 跑 vLLM **普通模型**（不思考，比如 Qwen2.5-VL 这类视觉/对话模型）→ 用默认 `langchain_openai:ChatOpenAI` 就行，没有 reasoning 字段需要保。
> - 不跑 vLLM（用云 API、Ollama 等）→ 完全用不到本文件。

---

## 5. 文件结构

```
models/
├── __init__.py          # 导出 create_chat_model / get_default_model
├── factory.py           # 工厂主体（本模块核心）
└── vllm_provider.py     # vLLM 推理模型专用 provider（VllmChatModel）

config/
├── model_config.py  # ModelConfig（模型档案 schema）
└── app_config.py    # AppConfig.get_model_config(name)（按名查找）
```

**逐文件作用**：

- **`__init__.py`**——模块门面。只导出两个公开函数 `create_chat_model` / `get_default_model`，把内部辅助函数挡在包外。**为什么单独成文件**：Python 包的入口约定，外部 `from deerflow.models import create_chat_model` 就靠它。
- **`factory.py`**——工厂主体，本模块的核心。把「找到类 → 读配置 → 处理 thinking/stream/tracing 等特殊参数 → 实例化」收口到 `create_chat_model` 一个函数。**为什么单独成文件**：这是所有 provider 共用的通用逻辑，与具体 provider 无关，独立成文件方便维护、单测（`test/test_model.py`）。
- **`vllm_provider.py`**——vLLM 推理模型专用 provider（`VllmChatModel` + 几个辅助函数）。它**不是**工厂的一部分，而是一个「可以被 config 的 `use` 字段反射加载的具体模型类」，和 `langchain_openai:ChatOpenAI` 是同层东西。**为什么单独成文件**：它只服务 vLLM 推理模型这一个窄场景（见 §4.6），独立成文件让通用工厂（factory.py）不被 vLLM 专有兼容代码污染；不用 vLLM 的人完全不需要读它。

`factory.py` 内部分四块：
- **辅助函数**：`_deep_merge_dicts`、`_vllm_disable_chat_template_kwargs`、`_enable_stream_usage_by_default`、`_apply_stream_chunk_timeout_default`、`_maybe_build_tracing_callbacks`
- **公开 API**：`create_chat_model(...)`、`get_default_model()`

`vllm_provider.py` 内部分两块：
- **模块级辅助函数**：`_normalize_vllm_chat_template_kwargs`（旧键名 `thinking` → `enable_thinking`）、`_reasoning_to_text`（从 reasoning 字段抠文本）、`_convert_delta_to_message_chunk_with_reasoning`（流式 delta 转 message chunk）、`_restore_reasoning_field`（把 reasoning 回灌到 payload assistant 消息）
- **`VllmChatModel` 类**：继承 `ChatOpenAI`，重写 `_get_request_payload` / `_create_chat_result` / `_convert_chunk_to_generation_chunk` 三个钩子

---

## 6. 关键接口

### `create_chat_model`

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

**返回**：一个 LangChain `BaseChatModel` 实例，可直接 `.invoke()` / `.stream()`。

**抛错**：
- `ValueError`：配置无任何模型 / 找不到 `name` / `thinking_enabled=True` 但不支持 thinking。
- `ImportError`：缺 provider 包（由 `resolve_class` 给出可操作安装提示，如 `uv add langchain-deepseek`）。

### `get_default_model`

```python
def get_default_model() -> BaseChatModel  # 等价于 create_chat_model()
```

便捷入口，用配置里第一个模型。

### `AppConfig.get_model_config`

```python
def get_model_config(self, name: str | None) -> ModelConfig | None
# name=None → 第一个模型；找不到 → None
```

---

## 7. 应用方法

### 7.1 最简配置（config.yaml）

```yaml
models:
  - name: deepseek
    use: langchain_deepseek:ChatDeepSeek
    model: deepseek-chat
    api_key: $DEEPSEEK_API_KEY     # $ 开头会从环境变量读
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
# 不开（工厂自动构造 enable_thinking: false 的关闭负载）
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

> 注意：因为配了 `base_url`，工厂会自动开 `stream_usage`（否则用量统计为 0）和 `stream_chunk_timeout=240`。

### 7.4 在 Agent / 中间件里调用（图内 → 必须 attach_tracing=False）

```python
# make_lead_agent / TitleMiddleware 等图内调用方：
model = create_chat_model(
    name=model_name,
    thinking_enabled=thinking_enabled,
    app_config=cfg,
    attach_tracing=False,   # ← 图根已挂回调，这里必须关
)
```

### 7.5 在后台任务里调用（独立 → 默认 attach_tracing=True）

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
model = create_chat_model("t", app_config=cfg)  # 显式注入，不读磁盘
```

### 7.7 vLLM 自托管推理模型（用 VllmChatModel 保住 reasoning）

当你在 vLLM 上跑 Qwen3 这类**会思考的推理模型**时，把 `use` 写成
`deerflow.models.vllm_provider:VllmChatModel`（详见 §4.6）。其余字段和普通 OpenAI 兼容
模型一样。完整可注释示例见 `config.example.yaml`「路径 D」。

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

> 注意：`VllmChatModel` 只在「vLLM + 推理模型」这个窄场景下需要。跑 vLLM 普通模型（如
> Qwen2.5-VL 视觉模型）仍用默认 `langchain_openai:ChatOpenAI`——没有 reasoning 字段要保。

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
  tracing (M12)      memory (M13)       lead_agent (M17)
  attach_tracing     MemoryUpdater      make_lead_agent
  懒加载              独立调用(True)      图内调用(False)
```

- **依赖**：`config`（读 ModelConfig）、`reflection`（resolve_class 反射加载）、`tracing`（懒加载，可选）。
- **被依赖**：
  - `tracing`（M12）：`build_tracing_callbacks` 被 factory 懒加载。
  - `memory`（M13）：`MemoryUpdater` 用 `create_chat_model(thinking_enabled=False)` 做记忆抽取。
  - `lead_agent`（M17）：`make_lead_agent` 用 `create_chat_model(..., attach_tracing=False)` 做主模型。
  - `TitleMiddleware`（M16）：生成标题时调模型。

---

## 9. 常见问题 / 排错

### Q1：`ModuleNotFoundError: No module named 'langchain_deepseek'`

你配了 `use: langchain_deepseek:ChatDeepSeek` 但没装包。按提示装：

```bash
uv add langchain-deepseek
```

（`resolve_class` 会给出这个提示。）

### Q2：开了 `thinking_enabled=True` 但模型没思考

检查两点：
1. `config.yaml` 里该模型是否设了 `supports_thinking: true`（没设会直接抛 ValueError）。
2. 是否配了 `when_thinking_enabled` 告诉工厂「怎么开」。不同 provider 开法不同（见 §4.1 表）。

### Q3：推理模型流式输出报 `StreamChunkTimeoutError`

默认 240s 还不够（极少见）。在 config.yaml 单模型调大：

```yaml
stream_chunk_timeout: 600
```

### Q4：前端 token 用量永远是 0

多半是用了 OpenAI 兼容网关但没配 `base_url`，或模型类不支持 `stream_usage`。确认 config 里有 `base_url`，工厂会自动开 `stream_usage`。

### Q5：trace 里出现重复 span，且看不到 user_id

你在**图内**调用了 `create_chat_model` 但忘了传 `attach_tracing=False`。改成：

```python
create_chat_model(..., attach_tracing=False)
```

（红线 #17）

### Q6：`TypeError: unexpected keyword argument 'stream_chunk_timeout'`

你给非 OpenAI provider（如 DeepSeek/Ollama）传了 `stream_chunk_timeout`。工厂本应自动剔除——如果你看到这个错，说明工厂逻辑被改坏了，检查 `_apply_stream_chunk_timeout_default`。

### Q7：vLLM 推理模型多轮对话丢失了「思考内容」/ 行为异常

你很可能在 vLLM 上跑推理模型（如 Qwen3 开了思考），但 `use` 用了默认的
`langchain_openai:ChatOpenAI`。LangChain 的 OpenAI 适配器会丢掉 vLLM 多出来的非标准
`reasoning` 字段，导致多轮「想完→调工具→继续想」时上一轮思考没回传。

改成 `use: deerflow.models.vllm_provider:VllmChatModel`（见 §4.6 / §7.7）。
非推理的 vLLM 模型（如 Qwen2.5-VL）不需要改，保持 `ChatOpenAI` 即可。

---

## 小结

模型工厂的精髓是**把 provider 差异和运维坑收口到一个函数**。记住四件事：

1. **配置驱动**：换模型只改 `config.yaml`，靠反射加载。
2. **thinking/stream/tracing 三大坑都自动处理**：四路径关闭、240s 超时、stream_usage 默认开、attach_tracing 双调用方。
3. **vLLM 推理模型有专门 provider**：跑 vLLM 推理模型时用 `VllmChatModel`（`vllm_provider.py`）保住非标准的 `reasoning` 字段；其它情况用默认 `ChatOpenAI`。
4. **fail-fast**：能力不匹配（如不支持 thinking 却要求开）直接报错，不静默。

下一个要读的文档：`docs/config.md`（了解 `ModelConfig` 与 `AppConfig` 全貌）。
