# 12. serialization.md — 序列化与消息转换（LangChain/LangGraph → JSON 单一真相源）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d

> **一句话定位**：`serialization` + `converters` 是「把内存里的 LangChain/LangGraph 对象，变成能安全发给前端 / 存进 JSON 的纯 Python 结构」的**单一真相源**——顺手剥掉引擎内部脏键、剥掉巨型 base64 图片，还能按需翻成 OpenAI 线协议。

> **配套代码**：[runtime/serialization.py](../backend/packages/harness/deerflow/runtime/serialization.py)（143 行）+ [runtime/converters.py](../backend/packages/harness/deerflow/runtime/converters.py)（133 行）
> **配套测试**：[test/test_serialization.py](../test/test_serialization.py)
> 本文面向「刚接触 JSON 序列化 / 线协议的小白」。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么不能直接 `json.dumps(langchain_message)`？`serialize_lc_object` 的递归兜底链有哪几档？为什么**任何对象**都不会让它抛异常？
2. 序列化 LangGraph channel values 时，为什么 `__pregel_*` 键必须剥、`__interrupt__` 键却必须留？（前者是引擎内部账本，后者是 SDK 识别「人在环路」中断的信号）
3. `Interrupt` 对象为什么不能靠 `model_dump`/`dict` 序列化、必须单列一步？它的 `__slots__` 属性和兜底链有什么关系？
4. `strip_data_url_image_blocks` 的三个「只剥」精确条件是什么？为什么不能把可见消息里的图片也剥了、为什么不能动消息本身的顺序？
5. `serialization.py`（内部对象→纯结构，保真）和 `converters.py`（内部对象→OpenAI 线协议，翻译）为什么分成两个文件？各自关注点有什么不同？
6. `converters` 为什么用 `getattr` 鸭子类型访问，而不 `isinstance(message, AIMessage)`？这对测试和兼容性有什么好处？
7. OpenAI 规范里 `tool_calls.arguments` 为什么是字符串而非 dict？空文本的 assistant 消息 `content` 为什么要设成 `null`？

---

## §1 为什么需要它（痛点 / 故障场景）

先看「没有它」会怎样（五种故障）：

| 故障 | 后果 | 怎么解 |
|------|------|--------|
| **`json.dumps` 直接炸** | LangChain 消息是 pydantic 对象，`json.dumps(message)` 报 `TypeError: not JSON serializable`。每个调用方各自 `message.model_dump()` 还会产生格式漂移（字段名 / 嵌套深度不一致） | `serialize_lc_object` 递归序列化，单一真相源 |
| **内部键泄漏给前端** | LangGraph channel values 里有 `__pregel_node_finished` 这类**引擎内部键**，发到前端既没用又暴露实现细节，还可能让前端解析崩 | `serialize_channel_values` 剥 `__pregel_*` |
| **畸形对象炸序列化** | LangGraph 的 `Interrupt`（中断点）是 `__slots__` 类，没有 `model_dump`/`dict`/`__dict__`——`json.dumps` 直接炸，连 `str()` 兜底都只能产出无用字符串 | `serialize_lc_object` 专门识别 `Interrupt` → `{value, id}` |
| **响应体爆炸** | `ViewImageMiddleware` 把**完整 base64 图片**塞进 `hide_from_ui` 的 human 消息当模型上下文。历史回放端点若原样返回，一条消息可能几 MB——前端卡死、流量浪费 | `strip_data_url_image_blocks` 剥 base64 图片块 |
| **格式不兼容外部 API** | 内部用 LangChain 消息，但要对接 OpenAI 兼容协议（前端 / SDK 期望 `{"role":"assistant","tool_calls":[...]}`），不转就对接不上 | `converters.py` 翻成 OpenAI 格式 |

一句话：**统一递归序列化**（含 Interrupt 规范化）+ **剥 `__pregel_*`**（保留 `__interrupt__`）+ **剥 base64 图片** + **LangChain↔OpenAI 翻译**。

---

## §2 零基础名词（第一次出现都解释）

**JSON 序列化（serialization）**：把内存对象转成可存储 / 可传输的格式。JSON 只认 7 种类型——`dict` / `list` / `str` / `int` / `float` / `bool` / `null`。pydantic 对象、tuple、自定义类都不在其中，必须先转。`serialize_lc_object` 递归地把任意嵌套结构转成这 7 种。

**LangChain 消息 / `model_dump`**：LangChain 的消息（`HumanMessage` / `AIMessage` / `SystemMessage` / `ToolMessage`）是 **pydantic v2 模型**。转 JSON 的标准方式是 `.model_dump()`（v1 是 `.dict()`），返回一个普通 dict。

**channel values / `__pregel_*`**：LangGraph 图运行时，状态存在一组 **channel（频道）** 里。`channel_values` 是「所有频道的当前值」这个 dict，是图状态的全貌。其中混着引擎自己的内部键，都以 `__pregel_` 开头（Pregel 是 LangGraph 底层执行模型的名字）。这些是**引擎内部账本**，对前端毫无意义。

**`__interrupt__`（中断状态）**：LangGraph 表示「人在环路」中断（工具确认 / `ask_clarification`）时，会在 channel values 里放一个 `__interrupt__` 键，值是 `Interrupt` 对象列表。**这个键故意不剥**——LangGraph SDK 靠它从 values chunk 识别中断事件；剥了 SDK 就识别不出，前端拿不到「需要用户输入」的信号。

**`hide_from_ui`**：消息 `additional_kwargs` 里的一个布尔标记，表示「这条消息只给模型看、不给前端用户看」（比如注入的系统上下文、base64 图片）。

**`data:` scheme URL**：把数据**内联**在 URL 里的写法，如 `data:image/png;base64,iVBORw0K...`。base64 图片会很长（一张图几十 KB 到几 MB）。`strip_data_url_image_blocks` 专门剥这种块。

**messages-mode tuple**：LangGraph 流式输出有几种 `stream_mode`。`messages` 模式下，每个 chunk 是一个二元组 `(message_chunk, metadata)`。序列化时把 chunk 递归序列化、metadata 原样保留（它已经是 dict）。

**OpenAI Chat Completions 格式**：OpenAI 的消息格式是事实上的行业标准——`{"role": "user"|"assistant"|"system"|"tool", "content": "..."}`，带工具调用时还有 `tool_calls`。很多前端 / SDK 按这个格式对接。

---

## §3 整体结构

```
runtime/
├── serialization.py   # 内部对象 → 纯 Python 结构（剥脏键、剥图片、保真）
│                      #   serialize_lc_object / serialize_channel_values /
│                      #   strip_data_url_image_blocks / serialize_channel_values_for_api /
│                      #   serialize_messages_tuple / serialize(mode)
└── converters.py      # 内部对象 → OpenAI 线协议（字段重命名、结构重组）
                       #   langchain_to_openai_message / langchain_to_openai_completion /
                       #   langchain_messages_to_openai / _infer_finish_reason
```

依赖：**无**（纯函数，仅用 `typing` + converters 用 `json`）。正因为零依赖，它在依赖链里最独立——可以放最后讲，也可以最早实现。

---

## §4 核心概念

### 4.1 「出风口」过滤器

把 serialization 想成进程的「出风口过滤网」：所有要**离开进程**的对象（发给前端、写进 JSON、转给外部 API）都从这儿过一遍，滤掉三类脏东西——

1. **引擎内部键**（`__pregel_*`）：实现细节，泄漏出去没用还危险。
2. **畸形对象**（`Interrupt` 等 `__slots__` 类）：裸 `json.dumps` 会炸。
3. **巨型 payload**（base64 图片）：响应体爆炸。

同时把对象**统一规范化**成 JSON 七种类型。

### 4.2 单一真相源（single source of truth）

所有「对象出进程」的地方（worker SSE 发布、REST 端点）都调 `serialize` / `serialize_channel_values_for_api`，而不是各自 `model_dump`。三个好处：
- **格式统一**——前端不用应对 N 种字段命名。
- **剥内部键的逻辑只写一处**——改 `__pregel_*` 规则只改 `serialize_channel_values`。
- **剥图片的逻辑只写一处**——防某个端点漏剥导致响应爆炸。

### 4.3 递归 + 兜底链（核心机制）

`serialize_lc_object` 序列化任何对象的优先级链（[serialization.py:22-58](../backend/packages/harness/deerflow/runtime/serialization.py#L22-L58)），从上往下试：

| 档 | 条件 | 处理 |
|----|------|------|
| 1 | `None` | 返回 `None` |
| 2 | `str/int/float/bool` | 原样（[:26-27](../backend/packages/harness/deerflow/runtime/serialization.py#L26-L27)） |
| 3 | `dict` | 递归每个 value（[:28-29](../backend/packages/harness/deerflow/runtime/serialization.py#L28-L29)） |
| 4 | `list`/`tuple` | 递归每个元素（tuple→list，JSON 没 tuple）（[:30-31](../backend/packages/harness/deerflow/runtime/serialization.py#L30-L31)） |
| 5 | 有 `model_dump()` | pydantic v2（[:33-37](../backend/packages/harness/deerflow/runtime/serialization.py#L33-L37)） |
| 6 | 有 `dict()` | pydantic v1 / 旧对象（[:39-43](../backend/packages/harness/deerflow/runtime/serialization.py#L39-L43)） |
| 7 | 是 LangGraph `Interrupt` | 规范化成 `{"value": ..., "id": ...}` 再递归（[:47-53](../backend/packages/harness/deerflow/runtime/serialization.py#L47-L53)） |
| 8 | 兜底 | `str(obj)`，再不行 `repr(obj)`（[:55-58](../backend/packages/harness/deerflow/runtime/serialization.py#L55-L58)） |

最后兜底 `str()`/`repr()` 保证**任何对象都不会让序列化抛异常**——对「出风口」过滤器很关键：一条脏数据不该让整个响应 500。

### 4.4 两文件职责分工

| | serialization.py | converters.py |
|---|---|---|
| **做什么** | 内部对象 → 纯 Python 结构 | 内部对象 → OpenAI 线协议 |
| **关注点** | **保真**（剥脏数据、保类型、不丢字段） | **翻译**（字段重命名、结构重组、补 OpenAI 规范字段） |
| **是否改语义** | 不改（只规范化载体） | 改（`human`→`user`、`ai`→`assistant`、补 `tool_calls`/`finish_reason`/`usage`） |

分文件让各自演化不互相牵制——剥 `__pregel_*` 的规则变了不会影响 OpenAI 翻译，反之亦然。

---

## §5 代码走读（逐函数）

### 5.1 `serialize_lc_object` —— 递归兜底链（见 §4.3 表）

[serialization.py:22-58](../backend/packages/harness/deerflow/runtime/serialization.py#L22-L58)。逐档 try，逐档兜底。`Interrupt` 分支是软 import（[:47-50](../backend/packages/harness/deerflow/runtime/serialization.py#L47-L50) `try/except ImportError`）——langgraph 在 mini 是硬依赖，软 import 纯属防御性兼容。

### 5.2 `serialize_channel_values` —— 剥 `__pregel_*`，**保留** `__interrupt__`

[serialization.py:61-73](../backend/packages/harness/deerflow/runtime/serialization.py#L61-L73)：

```python
def serialize_channel_values(channel_values):
    result = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_"):   # 只剥这个精确前缀
            continue
        result[key] = serialize_lc_object(value)
    return result
```

两个要点：
- **只剥 `__pregel_` 前缀**——普通的双下划线自定义键（`__custom__`）保留，用户自己的 state 不被误删。
- **`__interrupt__` 不剥**（它不以 `__pregel_` 开头）——它的值（`Interrupt` 对象列表）由 `serialize_lc_object` 的第 7 档（§4.3）规范化成 `{"value":..., "id":...}`，既不泄漏引擎内部结构，又保住 SDK 的中断检测。

### 5.3 `strip_data_url_image_blocks` —— 剥 base64 图片块

[serialization.py:76-106](../backend/packages/harness/deerflow/runtime/serialization.py#L76-L106)。三个「只剥」精确条件：

1. **只动 `hide_from_ui=True` 的消息**（[:93-96](../backend/packages/harness/deerflow/runtime/serialization.py#L93-L96)）——可见消息里的图片是用户自己传的，要展示。
2. **只剥 `type=="image_url"` 且 URL 以 `data:` 开头的块**（[:104](../backend/packages/harness/deerflow/runtime/serialization.py#L104)）——`https://` 图片 URL 是链接（几十字节），保留；text 块保留。
3. **只改 content，不动消息本身**——`{**msg, "content": filtered}`（[:105](../backend/packages/harness/deerflow/runtime/serialization.py#L105)），消息顺序、数量、id 都不变（前端渲染依赖顺序）。

体积账：1MB 的 PNG base64 后约 1.33MB，一次对话若有 5 张图被注入，`hide_from_ui` 消息就 ~7MB；历史端点返回整个线程 → 几十 MB 响应。剥掉后既砍 payload 又不破坏消息流结构。

### 5.4 `serialize_channel_values_for_api` —— 组合便利封装

[serialization.py:109-119](../backend/packages/harness/deerflow/runtime/serialization.py#L109-L119)：先 `serialize_channel_values`（剥内部键），若结果有 `messages` 列表再 `strip_data_url_image_blocks`（剥图片）。所有返回 channel values 给前端的 REST 端点都用它。

### 5.5 `serialize_messages_tuple` —— messages-mode 二元组

[serialization.py:122-127](../backend/packages/harness/deerflow/runtime/serialization.py#L122-L127)：`(chunk, metadata)` → `[serialize_lc_object(chunk), metadata if isinstance(metadata, dict) else {}]`。chunk 递归序列化，metadata 原样（它已是 dict）；非 dict 的 metadata 退化为 `{}`。

### 5.6 `serialize` —— mode 分发

[serialization.py:130-143](../backend/packages/harness/deerflow/runtime/serialization.py#L130-L143)：

| mode | 行为 |
|------|------|
| `"messages"` | `serialize_messages_tuple(obj)` |
| `"values"` | `serialize_channel_values_for_api(obj)` if dict else `serialize_lc_object(obj)`——values 快照把完整 state 流给前端，必须像 REST 端点一样剥 base64 图片（[:140-142](../backend/packages/harness/deerflow/runtime/serialization.py#L140-L142)） |
| 其它 | `serialize_lc_object(obj)`（保真，不剥任何键） |

> 注意：**只有 `serialize_channel_values` / `serialize_channel_values_for_api` / `serialize(mode="values")` 才剥 `__pregel_*`**。裸 `serialize_lc_object` 或 `serialize(mode="")` **不剥**——它保真。这是 FAQ 里「为什么前端还看到 `__interrupt__`」的根因。

### 5.7 converters：`langchain_to_openai_message`

[converters.py:24-74](../backend/packages/harness/deerflow/runtime/converters.py#L24-L74)，按 role 分三路（role 由 [:37](../backend/packages/harness/deerflow/runtime/converters.py#L37) `_ROLE_MAP` 从 `message.type` 映射：`human→user`/`ai→assistant`/`system→system`/`tool→tool`）：

- **tool**（[:40-45](../backend/packages/harness/deerflow/runtime/converters.py#L40-L45)）→ `{"role":"tool", "tool_call_id":..., "content":...}`。
- **assistant**（[:47-71](../backend/packages/harness/deerflow/runtime/converters.py#L47-L71)）→ 有 `tool_calls` 时逐个转成 OpenAI 的 `{id, type:"function", function:{name, arguments}}`，其中 `arguments` 是 `json.dumps(args)`（已是 str 则原样，[:61](../backend/packages/harness/deerflow/runtime/converters.py#L61)）；**无文本内容时 content 设 `null`**（OpenAI 规范，[:66](../backend/packages/harness/deerflow/runtime/converters.py#L66)）；无 tool_calls 则 `content` 原样。
- **user/system/unknown**（[:74](../backend/packages/harness/deerflow/runtime/converters.py#L74)）→ `{"role":..., "content":...}`。

全程用 `getattr(message, "type"/"content"/"tool_calls"/...)` **鸭子类型**访问，不 `isinstance(message, AIMessage)`——好处见 §6.5。

### 5.8 converters：completion + finish_reason + batch

- `_infer_finish_reason`（[converters.py:77-90](../backend/packages/harness/deerflow/runtime/converters.py#L77-L90)）：有 tool_calls→`"tool_calls"`，否则查 `response_metadata.finish_reason`，都没有→`"stop"`。
- `langchain_to_openai_completion`（[converters.py:93-128](../backend/packages/harness/deerflow/runtime/converters.py#L93-L128)）：把 AIMessage + 元数据组装成完整 OpenAI 响应——`{id, model(从 response_metadata.model_name), choices:[{index:0, message, finish_reason}], usage(从 usage_metadata: input→prompt_tokens, output→completion_tokens)}`。
- `langchain_messages_to_openai`（[converters.py:131-133](../backend/packages/harness/deerflow/runtime/converters.py#L131-L133)）：列表推导批量转。

---

## §6 设计权衡（不变量 / 踩坑）

### 6.1 剥 `__pregel_*` vs 保留 `__interrupt__`

| 键 | 处理 | 为什么 |
|----|------|--------|
| `__pregel_*` | **剥** | 引擎内部账本（如 `__pregel_node_finished` 记节点完成）。对前端无意义、暴露实现细节、可能让前端解析崩。官方 LangGraph Platform API 也不返回这些 |
| `__interrupt__` | **保留** | LangGraph SDK 据此从 values chunk 识别「人在环路」中断事件（工具确认 / clarification）。剥了前端拿不到「需要用户输入」的信号 |

规则：**键以 `__pregel_` 开头就剥**（精确前缀），其余一律保留——包括用户自己的双下划线键和 `__interrupt__`。

### 6.2 `Interrupt` 为什么必须单列一步（§4.3 第 7 档）

`Interrupt` 是 `__slots__` 类——**没有 `model_dump`、没有 `dict`、连 `__dict__` 都没有**。所以它跳过第 5/6 档，若不专门接住，会一路落到第 8 档 `str()`，产出 `<Interrupt ...>` 这种对前端毫无用处的字符串，SDK 也解析不出中断内容。专门识别后，它变成干净的 `{"value": ..., "id": ...}`（[serialization.py:52-53](../backend/packages/harness/deerflow/runtime/serialization.py#L52-L53)）。

### 6.3 base64 图片剥离的精确策略

见 §5.3 三条件。核心是「**只剥该剥的**」——hide_from_ui 的内部图片剥，用户可见图片留；`data:` 内联图剥，`https://` 链接图留；只动 content 不动消息结构。这样砍掉巨型 payload 的同时不破坏消息流。

### 6.4 兜底链保证「永不抛异常」

最坏退化成 `str(obj)`/`repr(obj)`。对「出风口」过滤器很关键：一条脏数据不该让整个响应 500。代价：兜底产出的字符串可能是无意义的——但那比崩了好，至少能定位是哪个对象没实现 `model_dump`。

### 6.5 converters 鸭子类型，不强依赖 LangChain

[converters.py:36-38](../backend/packages/harness/deerflow/runtime/converters.py#L36-L38) 用 `getattr` 而非 `isinstance`。好处：
- **测试可用 `SimpleNamespace` / dict 精确构造**——不受 LangChain 版本字段变化影响，也不用导入真实的消息类。
- **任何「长得像 LangChain 消息」的对象都能转**——鸭子类型，宽进严出。

### 6.6 converters 当前未接入 RunJournal

[converters.py:1-5](../backend/packages/harness/deerflow/runtime/converters.py#L1-L5) docstring 注明：RunJournal 直接用 `message.model_dump()`，不走 converters。converters 供**需要 OpenAI 线协议格式**的消费方（未来的 REST 端点 / worker）用。这是个「先实现、待接入」的纯函数模块——零依赖、零副作用，提前实现没有风险。

---

## §7 配置与用法

### 7.1 序列化图状态给前端（最常用）

```python
from deerflow.runtime.serialization import serialize_channel_values_for_api

# graph 跑完，拿到 channel_values（含 __pregel_* 内部键 + base64 图片 + Interrupt）
channel_values = await graph.aget_state(config).values
# 安全返回前端：内部键已剥、base64 图片已剥、Interrupt 已规范化
safe = serialize_channel_values_for_api(channel_values)
```

### 7.2 按流式 mode 序列化

```python
from deerflow.runtime.serialization import serialize

async for chunk, mode in graph.astream(input, config, stream_mode=["messages", "values"]):
    payload = serialize(chunk, mode=mode)   # messages→二元组；values→剥键+剥图片
    # payload 可直接 json.dumps 发 SSE
```

### 7.3 转成 OpenAI 格式

```python
from deerflow.runtime.converters import langchain_messages_to_openai

openai_msgs = langchain_messages_to_openai(messages)
# [{"role": "user", "content": "..."}, {"role": "assistant", "content": null, "tool_calls": [...]}, ...]
```

### 7.4 单条 AIMessage → 完整 completion 响应

```python
from deerflow.runtime.converters import langchain_to_openai_completion

resp = langchain_to_openai_completion(ai_message)
# {"id":..., "model":..., "choices":[{"index":0, "message":..., "finish_reason":"stop"}], "usage":{...}}
```

---

## §8 与其它模块的关系

```
LangChain 消息 / LangGraph channel values（内存对象）
            │
            ▼
   runtime/serialization.py ──── 剥 __pregel_*（保留 __interrupt__）/ 剥 base64 图片 / Interrupt 规范化
            │
            ├──→ [#26 runs](runs.md) worker：SSE 发布前序列化
            ├──→ 消息 / 历史 / run-wait REST 端点：返回前序列化
            │
   runtime/converters.py ──────── LangChain → OpenAI 线协议
            │
            └──→ 需要 OpenAI 兼容格式的端点 / SDK
```

- **被谁依赖**：[#26 runs.md](runs.md) 的 worker（SSE 发布前序列化）、消息 / 历史 / run-wait REST 端点（返回前序列化）、任何对接 OpenAI 协议的消费方。
- **依赖谁**：无（纯函数）——这正是它在依赖链里最独立、可放最后讲的原因。

### 与运行时存储三件套的区别（一次讲清）

serialization 是**「读出侧 / 出风口」过滤器**，跟运行时存储三件套是**正交**的：

| | serialization（本篇） | [#9 RunEventStore](run_event_store.md) | [#10 RunJournal](run_journal.md) | [#11 stream_bridge](stream_bridge.md) |
|---|---|---|---|---|
| 角色 | 对象出进程前的过滤器 | 持久化事件存储 | 写入侧采集器 | 实时传输桥 |
| 方向 | 读出 / 输出 | 写入 + 读出 | 写入 | 写入 + 读出 |
| 改语义 | 规范化载体（剥脏键/图片） | 不改 | 不改 | 不改 |

worker 把对象**存进** RunEventStore / 推过 stream_bridge 前，会先用 serialization 把它们**规范化**——所以 serialization 是它们的「前置净水器」。反过来，从 RunEventStore 读出历史返回前端时，也要再过一次 `serialize_channel_values_for_api` 剥 base64 图片。

---

## §9 常见问题 / 排错

**Q: 序列化后某个对象变成了字符串 `"Foo(...)"`？**
A: 兜底链走到了第 8 档 `str(obj)`——说明它既不是标量/dict/list，也没有 `model_dump`/`dict`，更不是 `Interrupt`。检查这个对象是不是该自己实现 `model_dump`，或者是不是不该出现在序列化输入里。

**Q: 前端拿到的状态里还有 `__interrupt__`？是 bug 吗？**
A: 不是——**故意保留的**（§6.1）。LangGraph SDK 靠它识别「人在环路」中断。如果你看到的是 `__pregel_*`，那才是该剥没剥——检查是不是用了裸 `serialize_lc_object`（它不剥键）。要剥必须用 `serialize_channel_values` / `serialize_channel_values_for_api` / `serialize(mode="values")`。

**Q: 前端图片不显示了？**
A: 检查图片是不是 `hide_from_ui` 消息里的 `data:` base64——那种是故意剥的（模型内部上下文，不该给前端）。用户可见的图片（非 hide_from_ui，或 https URL）不会被剥。

**Q: 历史响应体还是很大？**
A: 可能是**可见**消息里有大 content（比如工具返回的长文本）。`strip_data_url_image_blocks` 只剥 hide_from_ui 的 base64 图片，不截断可见文本。可见大文本的截断是 event store 的 trace 截断职责（见 [#9 run_event_store.md](run_event_store.md)），不是这里的。

**Q: converters 转出来的 `tool_calls.arguments` 是字符串不是 dict？**
A: 对的，OpenAI 规范——`arguments` 必须是 JSON 字符串。`{"q":"x"}` → `'{"q": "x"}'`（[converters.py:61](../backend/packages/harness/deerflow/runtime/converters.py#L61)）。消费方要 `json.loads` 回来。

**Q: assistant 消息有 tool_calls 时 content 是 `null`？**
A: 对的，OpenAI 规范——纯工具调用、无文本时 content 设 `null`（[converters.py:66](../backend/packages/harness/deerflow/runtime/converters.py#L66)）。有文本就保留文本。

**Q: tuple 序列化后变成了 list？**
A: 正常。JSON 没有 tuple 类型，第 4 档把 tuple 转成 list。这是 JSON 的固有约束，不是 bug。

---

## §10 小结

serialization + converters 是「对象出进程」的**单一真相源**，一个保真、一个翻译：

- **serialization.py** = 出风口净水器：递归兜底链（8 档，永不抛异常）+ 剥 `__pregel_*`（保留 `__interrupt__`）+ 剥 hide_from_ui 的 base64 图片 + `Interrupt`→`{value,id}`。
- **converters.py** = 线协议翻译器：LangChain 消息 → OpenAI 格式（鸭子类型、补 `tool_calls`/`finish_reason`/`usage`、`arguments` 转字符串、空 content 设 null）。

记三句就够：
1. **剥 `__pregel_*`，留 `__interrupt__`**——前者引擎账本，后者 SDK 中断信号。
2. **兜底链永不抛**——最坏退化成字符串，脏数据不让整个响应 500。
3. **serialization 保真，converters 翻译**——两个关注点，两个文件。

它是运行时存储三件套（RunEventStore / RunJournal / stream_bridge）的「前置净水器」——对象存进 / 推出 / 读出进程前都先过它一道。读完这篇，Phase 1（模型 + 运行时基础）就齐了。

---

> 上一篇：[#11 stream_bridge.md](stream_bridge.md)（流桥——实时传输事件流） · 下一篇：[#13 sandbox.md](sandbox.md)（沙箱——虚拟路径 `/mnt/user-data` + 7 工具 + 本地模式非安全边界，引出 #14 AIO 容器隔离）

**🎉 Phase 1（模型 + 运行时基础，#6–#12）全部完成**：models · persistence · checkpointer · run_event_store · run_journal · stream_bridge · serialization。接下来进入 Phase 2（沙箱 / 子代理 / 追踪）。
