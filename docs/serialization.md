# 12. serialization.md — 序列化与消息转换（LangChain/LangGraph → JSON）

> 配套代码：[runtime/serialization.py](../backend/packages/harness/deerflow/runtime/serialization.py) + [runtime/converters.py](../backend/packages/harness/deerflow/runtime/converters.py)
> 配套测试：[test/test_serialization.py](../test/test_serialization.py)
> 本文面向「刚接触 JSON 序列化 / 线协议的小白」。每个名词第一次出现都会解释。

---

## 1. 一句话定位

**serialization 模块是「把内存里的 LangChain/LangGraph 对象，变成能安全发给前端 / 存进 JSON 的纯 Python 结构」的单一真相源——顺手剥掉内部脏数据。**

它是「出风口」过滤器：所有要离开进程（发给前端、写进 JSON、转给外部 API）的对象都走它，保证格式统一、不含内部键、不含巨型 base64 图片。

---

## 2. 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样：

- **`json.dumps` 直接炸**。LangChain 消息是 pydantic 对象，`json.dumps(message)` 报 `TypeError: not JSON serializable`。每个调用方各自 `message.model_dump()` 会产生格式漂移（字段名、嵌套深度不一致）。
- **内部键泄漏给前端**。LangGraph 的图状态（channel values）里有 `__pregel_node_finished`、`__interrupt__` 这类**内部实现键**，是给 LangGraph 引擎自己用的，发到前端既没用又暴露实现细节、还可能让前端解析崩。
- **响应体爆炸**。`ViewImageMiddleware` 把**完整 base64 图片**塞进 `hide_from_ui` 的 human 消息当模型上下文。历史回放端点若原样返回，一条消息可能几 MB——前端卡死、流量浪费。
- **格式不兼容外部 API**。内部用 LangChain 消息，但要对接 OpenAI 兼容协议（前端 / SDK 按 OpenAI 格式期望 `{"role": "assistant", "tool_calls": [...]}`），不转就对接不上。

serialization + converters 解决这些：**统一递归序列化** + **剥 `__pregel_*`/`__interrupt__`** + **剥 base64 图片块** + **LangChain↔OpenAI 转换**。

---

## 3. 核心概念（名词 + 类比）

### 3.1 JSON 序列化（serialization）

**序列化** = 把内存对象转成可存储 / 可传输的格式（这里是 JSON 可解析的纯 Python 结构：dict / list / str / int / float / bool / None）。

JSON 只认这 7 种类型。pydantic 对象、tuple、自定义类都不在其中，必须先转。`serialize_lc_object` 递归地把任意嵌套结构转成这 7 种。

### 3.2 LangChain 消息 / pydantic model_dump

LangChain 的消息（`HumanMessage` / `AIMessage` / `SystemMessage` / `ToolMessage`）是 **pydantic v2 模型**。转 JSON 的标准方式是 `.model_dump()`（v1 是 `.dict()`），返回一个普通 dict。`serialize_lc_object` 自动探测并用 `model_dump()`。

### 3.3 channel values / `__pregel_*`

LangGraph 图运行时，状态存在一组 **channel（频道）** 里。`channel_values` 是「所有频道的当前值」这个 dict，是图状态的全貌。

其中混着 LangGraph 引擎自己的内部键，都以 `__pregel_` 开头（Pregel 是 LangGraph 底层执行模型的名字），外加 `__interrupt__`（中断状态）。这些是**引擎内部账本**，对前端毫无意义，序列化时必须剥掉，以对齐 LangGraph Platform API 的返回（它也不暴露这些）。

### 3.4 hide_from_ui / data: image_url

`hide_from_ui` 是消息 `additional_kwargs` 里的一个标记，表示「这条消息只给模型看、不给前端用户看」（比如注入的系统上下文、base64 图片）。

`data:` scheme URL 是把数据**内联**在 URL 里的写法，如 `data:image/png;base64,iVBORw0K...`。base64 图片会很长（一张图几十 KB 到几 MB）。`strip_data_url_image_blocks` 专门剥这种块。

### 3.5 messages-mode tuple

LangGraph 流式输出有几种 `stream_mode`。`messages` 模式下，每个 chunk 是一个二元组 `(message_chunk, metadata)`。序列化时要把 chunk 递归序列化、metadata 原样保留（它已经是 dict）。

### 3.6 OpenAI Chat Completions 格式

OpenAI 的消息格式是事实上的行业标准：`{"role": "user"|"assistant"|"system"|"tool", "content": "..."}`，带工具调用时还有 `tool_calls`。很多前端 / SDK 按这个格式对接。`converters.py` 把 LangChain 消息翻成这个格式。

---

## 4. 设计原理（权衡 / 不变量 / 踩坑）

### 4.1 单一真相源

所有「对象出进程」的地方（未来的 worker SSE 发布、REST 端点）都调 `serialize` / `serialize_channel_values_for_api`，而不是各自 `model_dump`。好处：
- **格式统一**——前端不用应对 N 种字段命名。
- **剥内部键的逻辑只写一处**——改 `__pregel_*` 规则只改 `serialize_channel_values`。
- **剥图片的逻辑只写一处**——防某个端点漏剥导致响应爆炸。

### 4.2 剥 `__pregel_*` 为何要剥

这些键是 LangGraph 引擎的**内部执行账本**：
- `__pregel_node_finished`：记录哪些节点跑完了。
- `__interrupt__`：记录中断点（人在环路 / 工具确认）。

它们：
1. 对前端无意义（前端不关心引擎内部节点状态）。
2. 暴露实现细节（引擎版本变了键名可能变）。
3. 可能让前端解析崩（结构不预期）。

对齐 LangGraph Platform API——官方 API 也不返回这些。规则：**键以 `__pregel_` 开头，或等于 `__interrupt__`，就剥**。注意：只剥这两个精确模式，普通的双下划线自定义键（`__custom__`）保留——用户自己的 state 不该被误删。

### 4.3 base64 图片剥离的体积问题

一张 1MB 的 PNG，base64 编码后约 1.33MB。一次对话若有 5 张图被 `ViewImageMiddleware` 注入，`hide_from_ui` 消息就 ~7MB。历史端点返回整个线程 → 几十 MB 响应体，前端卡死、移动端流量爆炸。

`strip_data_url_image_blocks` 的精确策略：
- **只剥 `hide_from_ui=True` 的消息**——可见消息里的图片是用户自己传的，要展示。
- **只剥 `type=image_url` 且 URL 以 `data:` 开头的块**——`https://` 的图片 URL 是链接（几十字节），保留；text 块保留。
- **只改 content，不动消息本身**——消息顺序、数量、id 都不变（前端渲染依赖顺序）。

这样既砍掉了巨型 payload，又不破坏消息流结构。

### 4.4 递归 + 兜底链（serialize_lc_object）

序列化任何对象的优先级链：
1. `None` / 标量（str/int/float/bool）→ 原样。
2. `dict` → 递归每个 value。
3. `list` / `tuple` → 递归每个元素（tuple 变 list，因为 JSON 没 tuple）。
4. 有 `model_dump()` → pydantic v2。
5. 有 `dict()` → pydantic v1 / 旧对象。
6. 兜底 `str(obj)`（再不行 `repr`）。

这条链保证**任何对象都不会让序列化抛异常**——最坏退化成字符串。这对「出风口」过滤器很重要：一条脏数据不该让整个响应 500。

### 4.5 converters：鸭子类型，不强依赖 LangChain

`langchain_to_openai_message` 用 `getattr(message, "type"/"content"/"tool_calls"/...)` 鸭子类型访问，不 `isinstance(message, AIMessage)`。好处：
- 测试可用 `SimpleNamespace` 精确构造（不受 LangChain 版本字段变化影响）。
- 任何「长得像 LangChain 消息」的对象都能转。

`tool_calls` 的 `args` 是 dict 时 `json.dumps` 成字符串（OpenAI 规范要求 `arguments` 是字符串）；已经是字符串则原样。

### 4.6 为什么 converters 单列一文件

serialization 是「内部对象 → 纯 Python 结构」（剥脏数据、保类型）。converters 是「内部对象 → 外部线协议」（LangChain → OpenAI 格式，字段重命名、结构重组）。两者关注点不同：serialization 保真，converters 翻译。分文件让各自演化不互相牵制。

outline 把 converters 标「可后补，先占位」——此处按 deer 参考完整移植（纯函数、无依赖、低风险），供后续 REST 端点 / worker 直接用。当前**未**接入 RunJournal（它直接用 `model_dump`）。

---

## 5. 文件结构

```
runtime/
├── serialization.py   # serialize_lc_object / serialize_channel_values /
│                      # strip_data_url_image_blocks / serialize_channel_values_for_api /
│                      # serialize_messages_tuple / serialize(mode)
└── converters.py      # langchain_to_openai_message / langchain_to_openai_completion /
                       # langchain_messages_to_openai / _infer_finish_reason
```

依赖：**无**（纯函数，仅用 typing）。这是 M9 能插队到 M7/M8 之前的原因。

---

## 6. 关键接口 / 签名

### serialization

```python
serialize_lc_object(obj) -> Any                                  # 递归序列化任意对象
serialize_channel_values(channel_values: dict) -> dict           # 剥 __pregel_*/__interrupt__
strip_data_url_image_blocks(messages: list[dict]) -> list[dict]  # 剥 hide_from_ui 的 data: 图片块
serialize_channel_values_for_api(channel_values: dict) -> dict   # 组合：剥内部键 + 剥图片
serialize_messages_tuple(obj) -> Any                             # (chunk, metadata) -> [serialized, metadata]
serialize(obj, *, mode="") -> Any                                # mode 分发：messages/values/default
```

### converters

```python
langchain_to_openai_message(message) -> dict                     # 单条消息 → OpenAI dict
langchain_to_openai_completion(message) -> dict                  # AIMessage → OpenAI completion 响应
langchain_messages_to_openai(messages: list) -> list[dict]       # 批量
```

---

## 7. 应用方法（可跑 demo）

### 7.1 序列化图状态给前端

```python
from deerflow.runtime.serialization import serialize_channel_values_for_api

# graph 跑完，拿到 channel_values（含 __pregel_* 内部键 + base64 图片）
channel_values = await graph.aget_state(config).values
# 安全返回前端：内部键已剥、base64 图片已剥
safe = serialize_channel_values_for_api(channel_values)
```

### 7.2 按流式 mode 序列化

```python
from deerflow.runtime.serialization import serialize

async for chunk, mode in graph.astream(input, config, stream_mode=["messages", "values"]):
    payload = serialize(chunk, mode=mode)
    # payload 可直接 json.dumps 发 SSE
```

### 7.3 转成 OpenAI 格式

```python
from deerflow.runtime.converters import langchain_messages_to_openai

openai_msgs = langchain_messages_to_openai(messages)  # [{"role": "user", "content": "..."}, ...]
```

---

## 8. 与其它模块的关系（文字依赖图）

```
LangChain 消息 / LangGraph channel values
            │
            ▼
   runtime/serialization.py ──── 剥 __pregel_*/__interrupt__ / 剥 base64 图片
            │
            ├──→ （未来）runtime/runs/worker：SSE 发布前序列化
            ├──→ （未来）Gateway messages/events 端点：返回前序列化
            │
   runtime/converters.py ──────── LangChain → OpenAI 线协议
            │
            └──→ （未来）需要 OpenAI 兼容格式的端点 / SDK
```

- **被谁依赖**：未来的 worker（SSE）、消息/事件 REST 端点、任何对接 OpenAI 协议的消费方。
- **依赖谁**：无（纯函数）——这就是它能插队到 M7/M8 之前完成的原因。
- **与 RunEventStore 的区别**：RunEventStore 存「事件流」（写入侧）；serialization 是「读出侧」过滤器——把存下来的 / 内存里的对象安全送出进程。

---

## 9. 常见问题 / 排错

**Q: 序列化后某个对象变成了字符串 `"Foo(...)"`？**
A: 兜底链走到了 `str(obj)`——说明它既不是标量/dict/list，也没有 `model_dump`/`dict`。检查这个对象是不是该自己实现 `model_dump`，或者是不是不该出现在序列化输入里。

**Q: 前端拿到的状态里还有 `__interrupt__`？**
A: 用错了函数。`serialize_lc_object`（default mode）**不剥** `__pregel_*`——它保真。要剥必须用 `serialize_channel_values` 或 `serialize_channel_values_for_api`，或 `serialize(obj, mode="values")`。

**Q: 前端图片不显示了？**
A: 检查图片是不是 `hide_from_ui` 消息里的 `data:` base64——那种是故意剥的（模型内部上下文，不该给前端）。用户可见的图片（非 hide_from_ui，或 https URL）不会被剥。

**Q: 历史响应体还是很大？**
A: 可能是**可见**消息里有大 content（比如工具返回的长文本）。`strip_data_url_image_blocks` 只剥 hide_from_ui 的 base64 图片，不截断可见文本。可见大文本的截断是 event store 的 trace 截断职责（见 [run_event_store.md](run_event_store.md) §4.5），不是这里的。

**Q: converters 转出来的 `tool_calls.arguments` 是字符串不是 dict？**
A: 对的，这是 OpenAI 规范——`arguments` 必须是 JSON 字符串。`{"q": "x"}` → `'{"q": "x"}'`。消费方要 `json.loads` 回来。

**Q: tuple 序列化后变成了 list？**
A: 正常。JSON 没有 tuple 类型，`serialize_lc_object` 把 tuple 转成 list。这是 JSON 的固有约束，不是 bug。

---

> 红线索引：本模块是「出风口」过滤器，支撑红线 #1（不把阻塞/巨型 payload 推给前端）与「不泄漏内部实现键」的工程纪律。详见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part E。
