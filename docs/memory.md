# 18. memory.md — 记忆系统（LLM 抽取 + 去抖队列 + per-user 原子存储 + 注入）

> **一句话定位**：记忆让 agent **跨会话记住用户**——把对话里关于用户的事实/偏好/上下文抽出来
> 存成 `memory.json`，下次对话时注入系统提示，实现个性化。本模块负责抽取（LLM）+ 去抖队列
> + per-user 原子存储 + 预算受限的注入。

读完 [user_context.md](user_context.md)（懂了 user_id 三态隔离）+ [agents_config.md](agents_config.md)
（懂了 per-user 目录布局）+ [models.md](models.md)（懂了 `create_chat_model`）再看本篇最省事——
记忆就是「按 user_id 存的 JSON 文件 + 一个 LLM 抽取管线 + 一个注入器」。

---

## 为什么需要记忆（痛点）

默认的 lead agent 是**无状态的**：每个新会话从零开始，不知道你是谁、你喜欢什么、你在做什么项目。
你每次都得重新自我介绍。长对话里它也记不住你三页前说过的偏好。

记忆系统解决这个：

- **跨会话**：今天你说「我用 Python」，明天新开对话它还知道。
- **跨午夜**：今天聊的背景，明天它仍记得（不需要你重述）。
- **个性化**：知道你的角色、技术栈、沟通偏好，回答更贴你。

类比：默认 agent 是「每天换一个新前台」；有记忆的 agent 是「你的长期私人助理」——它有个小本子
（memory.json），每次聊完把重要的记一笔，下次见面先翻小本子。

---

## 数据结构（memory.json 长什么样）

```json
{
  "version": "1.0",
  "lastUpdated": "2026-06-21T10:30:00Z",
  "user": {
    "workContext":      { "summary": "工程师，做 LangGraph 项目", "updatedAt": "..." },
    "personalContext":  { "summary": "中英双语，偏好简洁",         "updatedAt": "..." },
    "topOfMind":        { "summary": "正在重构记忆模块",            "updatedAt": "..." }
  },
  "history": {
    "recentMonths":      { "summary": "近期在研究 agent 架构", "updatedAt": "..." },
    "earlierContext":    { "summary": "...",                   "updatedAt": "..." },
    "longTermBackground":{ "summary": "...",                   "updatedAt": "..." }
  },
  "facts": [
    { "id": "fact_a1b2c3d4", "content": "偏好用中文回答", "category": "preference",
      "confidence": 0.9, "createdAt": "...", "source": "thread-42" }
  ]
}
```

三块：

- **user**（当前状态，精炼）：工作 / 个人 / 当前关注点（topOfMind 最常更新）。
- **history**（时间线，段落）：近几月 / 更早 / 长期背景。
- **facts**（离散事实，带置信度）：具体可量化的事，如「16k+ GitHub stars」「偏好深色模式」。

fact 的 `category`：`preference`（偏好）/ `knowledge`（专长）/ `context`（背景）/ `behavior`
（行为模式）/ `goal`（目标）/ `correction`（纠正，含 `sourceError` 描述要避免的错误）。

---

## 核心流水线（四步）

```
对话结束
   │  MemoryMiddleware（after_agent）
   ▼
①过滤：只留 user 输入 + 最终 AI 回复（跳工具调用 / 纯上传）
   │  + 检测 correction/reinforcement 信号
   ▼
②去抖队列：debounce 30s，同 (thread,user,agent) 合并；user_id 跨 Timer 捕获
   │  定时器触发（后台线程）
   ▼
③LLM 抽取：同步 model.invoke()（专用线程池，防 #2615）→ JSON → 容错解析 + 归一化
   │  + fact 去重 + max_facts 裁剪 + 上传记忆剔除
   ▼
④原子写：temp + rename（防写一半崩溃）→ memory.json（per-user 隔离）

下次对话
   │  DynamicContextMiddleware（before_agent）
   ▼
⑤注入：读 memory.json → format_memory_for_injection（token 预算截断）→ <system-reminder>
```

---

## 核心概念（名词 + 类比）

### ① per-user / per-agent 隔离

记忆按 `(user_id, agent_name)` 分桶存（见 [agents_config.md](agents_config.md) 的目录布局）：

```
{base_dir}/users/{user_id}/memory.json                      ← 全局记忆
{base_dir}/users/{user_id}/agents/{name}/memory.json        ← per-agent 记忆
{base_dir}/memory.json                                       ← legacy 全局（只读回退）
```

Alice 的记忆和 Bob 的完全隔离。自定义 agent（如 `code-reviewer`）还能有**自己专属的**记忆
（per-agent），不和默认 agent 的全局记忆混。

`agent_name` 用 `AGENT_NAME_PATTERN`（红线 #32，从 [agents_config](agents_config.md) 取）校验
后才拼路径——防穿越。

### ② 去抖队列（debounce）

用户连续发 5 条消息，每条都触发 `MemoryMiddleware`。如果每条都调一次 LLM 抽取，既慢又费钱。
去抖队列的解法：**攒着，30s 内没新消息再统一处理**。同一 `(thread_id, user_id, agent_name)`
的多条入队会**合并**成一条（最新消息覆盖旧的，correction/reinforcement 标志取「或」）。

类比：你给助理发 5 条微信，助理不会每条都记一笔，而是等你不发了（30s 静默），把你这 5 条
说的整体记一笔。

### ③ 同步 LLM 路径防 #2615（关键红线）

这是最容易踩的坑。langchain 的 async httpx `AsyncClient` / 连接池是**全局缓存**的（经
`@lru_cache`），和 lead agent 共享。如果记忆更新用 `asyncio.run()` 起一个新事件循环跑
`model.ainvoke()`，会发生**跨事件循环连接复用**——连接池里的连接被两个循环抢着用，直接炸
（issue #2615）。

解法（红线）：记忆更新一律走**同步** `model.invoke()`（同步 HTTP，独立连接池），不创建第二个
事件循环。在事件循环里被调时（如 LangGraph node），用专用 `ThreadPoolExecutor` 卸载，调
`future.result()` 阻塞等结果——但仍跑的是同步 `invoke`，不碰 async 客户端池。

```
错（炸）：asyncio.run() → 新循环 → model.ainvoke() → 抢共享 async 连接池 → 跨循环复用 💥
对（本模块）：ThreadPoolExecutor → model.invoke()（同步）→ 独立同步连接池 → 不碰 async 池 ✅
```

### ④ 原子写（temp + rename）

记忆是用户长期积累的宝贵数据，写一半崩溃（断电 / 进程被杀）会留下**半截 JSON**，下次读解析失败
→ 全丢。原子写：先写到 `memory.<uuid>.tmp`，写完再 `replace` 成 `memory.json`。
`replace` 在 POSIX 上是原子的——要么旧文件完整，要么新文件完整，不会有中间态。

### ⑤ user_id 跨 Timer 捕获

`MemoryUpdateQueue` 的去抖用 `threading.Timer`——它在**另一个线程**触发处理。Python 的
`ContextVar`（user_id 的载体）**不跨裸线程传播**。如果在 Timer 回调里才取 `get_effective_user_id()`，
拿到的是默认值（`"default"`），不是当前用户的。

解法：在 `add()` 入队时（请求上下文还活着）就把 `user_id` **显式存进** `ConversationContext`，
Timer 回调直接用存好的值。这是个隐蔽但致命的 bug——不捕获就会把所有人的记忆写进 `default` 桶。

### ⑥ 注入预算（token budget）

记忆注入到系统提示会**吃掉**上下文窗口。不能无脑全塞。`format_memory_for_injection` 按
`max_injection_tokens`（默认 2000）预算截断：

- 先放 user/history 段。
- facts 按**置信度降序**逐条加，加到预算耗尽就停。
- 整体仍超预算时按 token/字符比例截断尾部加 `...`。

token 计数有两种策略（`memory.token_counting`）：

- `tiktoken`（默认）：精确，但首次用可能从公共网络下载 BPE 数据，网络受限环境会**长时间阻塞**
  （issue #3402/#3429）。有**冷却降级**：失败缓存 600s，期间走字符估算；可设 `char` 完全跳过。
- `char`：无网络的 CJK 感知字符估算（英文 ~4 字符/token，CJK ~2 字符/token），从不碰 tiktoken。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### DynamicContextMiddleware 的 ID-swap（缓存友好）

动态上下文（日期 + 记忆）要注入，但基础系统提示词要保持**静态**以最大化前缀缓存复用。如果每轮
都改系统提示词，缓存命中率暴跌。

deer 的解法（mini 1:1 对齐）：把动态部分作为**独立 HumanMessage** 注入，用 **ID-swap** 技术冻结
首条用户消息：

- 首轮：把完整提醒（记忆 + 日期）作为新 HumanMessage，**复用首条用户消息的 ID**（`id=stable_id`），
  让 LangGraph 的 `add_messages` 原地替换它（保位置）。原内容用派生 ID `"{stable_id}__user"`
  紧随其后 append。
- 之后：首条消息（现在是提醒）**内容永不变** → 整个会话前缀稳定 → 每轮命中缓存。
- 跨午夜：检测日期变化，给当前轮注入轻量日期更新提醒（同 ID-swap 技术）。

注入格式：

```
<system-reminder>
<memory>
...
</memory>

<current_date>2026-06-21, Saturday</current_date>
</system-reminder>
```

### abefore_agent 的 to_thread + 5s 超时

`_inject` 做同步文件 IO（读 memory.json）+ 可能阻塞的网络调用（首次 tiktoken BPE 下载）。
在 async hook 里直接跑会**阻塞事件循环**，饿死所有并发 HTTP 处理器（鉴权、SSE 心跳）。

解法：`asyncio.to_thread(self._inject, state)` 卸载 + `asyncio.wait_for(..., timeout=5.0)`。
超时优雅降级（不注入记忆/日期）而非挂起——若启动预热静默失败，首个请求的冷 tiktoken 下载可阻塞
数十分钟，限时让它降级。

### JSON 容错解析

LLM 被要求只返 JSON，但有些 provider 仍会把 JSON 包在思考痕迹 / 散文 / markdown 代码围栏里。
`_parse_memory_update_response` 用 `json.JSONDecoder.raw_decode` 从每个 `{` 开始尝试解析，
找到第一个含全部必需顶层键（`user/history/newFacts/factsToRemove`）的合法对象。

**不安全部分更新防御**：如果 `factsToRemove` 非空但 `newFacts` 含非法条目，直接抛
`JSONDecodeError`——避免「删了旧 fact 但新 fact 是坏的」这种半残状态。

### fact 去重 + 置信度阈值 + max_facts 裁剪

- **去重**：新 fact 的 content 经 `casefold()`（大小写无关）与已有比对，重复不加。
- **置信度阈值**：低于 `fact_confidence_threshold`（默认 0.7）的 fact 丢弃——LLM 推测的不该进
  长期记忆。
- **max_facts 裁剪**：超 `max_facts`（默认 100）时按置信度降序留 top——低置信度的老 fact 让位给
  高置信度的新 fact。

### 上传记忆剔除（session-scoped 不该进长期记忆）

用户上传的文件是**本次会话**的，下个会话就没了。如果记忆里存了「用户上传了 report.pdf」，
下次 agent 会去找这个不存在的文件。`_strip_upload_mentions_from_memory` 用收窄的正则从所有
摘要和 fact 里移除上传相关句子——但故意收窄，不误删「User works with CSV files」这类合法 fact。

### correction / reinforcement 信号

`detect_correction` / `detect_reinforcement`（中英双语模式）在最近 6 条里识别显式纠正
（「不对」「that's wrong」）或正向强化（「完全正确」「exactly right」）。命中时给 LLM 抽取 prompt
加 hint，让它把纠正记成高置信度（≥0.95）的 `correction` fact（含 `sourceError` 描述要避免的错误）。

---

## 文件结构

```
agents/memory/
├── __init__.py            # 导出全部公共 API
├── storage.py             # MemoryStorage ABC + FileMemoryStorage（mtime 缓存 + 原子写）+ get_memory_storage + create_empty_memory + utc_now_iso_z
├── message_processing.py  # filter_messages_for_memory（过滤）+ detect_correction/detect_reinforcement（信号检测）
├── queue.py               # ConversationContext + MemoryUpdateQueue（去抖合并 + user_id 跨 Timer 捕获）+ get_memory_queue
├── prompt.py              # MEMORY_UPDATE_PROMPT + format_memory_for_injection（预算截断）+ format_conversation_for_update + _count_tokens（tiktoken 冷却降级）
└── updater.py             # MemoryUpdater（同步 LLM 路径 #2615）+ fact CRUD + JSON 容错解析 + 去重 + max_facts + 上传剔除

agents/middlewares/
├── memory_middleware.py        # after_agent：过滤→检测→捕获 user_id→queue.add
└── dynamic_context_middleware.py  # before_agent：ID-swap 注入记忆+日期 + 跨午夜 + to_thread 5s 超时

agents/lead_agent/prompt.py     # _get_memory_context（延迟导入 memory，吞异常返 ""）
config/paths.py                # Paths.memory_file / user_memory_file / agent_memory_file / user_agent_memory_file
config/memory_config.py        # MemoryConfig + get_memory_config()（get_app_config().memory 访问器）
```

> **summarization_hook.py 未建**：它依赖 M16 SummarizationMiddleware（摘要前抢拍记忆），M16 未
> 落地，待 M16 接入时补。

---

## 关键接口

```python
# 存储
class MemoryStorage(abc.ABC):
    def load(self, agent_name=None, *, user_id=None) -> dict: ...
    def reload(self, agent_name=None, *, user_id=None) -> dict: ...
    def save(self, memory_data, agent_name=None, *, user_id=None) -> bool: ...

# 队列
class MemoryUpdateQueue:
    def add(self, thread_id, messages, agent_name=None, user_id=None, correction_detected=False, reinforcement_detected=False): ...
    def add_nowait(...): ...      # 立即处理（0s 定时器）
    def flush(self): ...          # 强制立即处理
    @property
    def pending_count(self) -> int: ...

# 更新器
class MemoryUpdater:
    def update_memory(self, messages, thread_id=None, agent_name=None, ..., user_id=None) -> bool: ...  # 同步路径
    async def aupdate_memory(...) -> bool: ...  # asyncio.to_thread 委托同步路径

# fact CRUD
def create_memory_fact(content, category="context", confidence=0.5, agent_name=None, *, user_id=None) -> dict: ...
def delete_memory_fact(fact_id, ...) -> dict: ...      # KeyError if missing
def update_memory_fact(fact_id, content=None, ..., user_id=None) -> dict: ...

# 注入
def format_memory_for_injection(memory_data, max_tokens=2000, *, use_tiktoken=True) -> str: ...

# 系统 prompt 集成
def _get_memory_context(agent_name=None, *, app_config=None) -> str: ...  # 包 <memory>...</memory>，禁用/空/异常→""
```

---

## 应用方法

### 配置（config.yaml → `memory`）

```yaml
memory:
  enabled: true                # 主开关（关了不抽取也不注入）
  injection_enabled: true      # 注入开关（关了只抽取不注入）
  storage_path: ""             # 空=per-user 隔离；绝对路径=所有用户共享一文件（退出隔离）
  debounce_seconds: 30         # 去抖秒数
  model_name: null             # 抽取用的模型；null=默认模型
  max_facts: 100               # 最多存的事实条数
  fact_confidence_threshold: 0.7  # 存储事实的最低置信度
  max_injection_tokens: 2000   # 注入最多占用的 token
  token_counting: tiktoken     # tiktoken（精确）/ char（无网络）
```

### 在 build_middlewares 里挂载

mini 的 [middlewares/__init__.py](../backend/packages/harness/deerflow/agents/middlewares/__init__.py)
已按配置挂载：`DynamicContextMiddleware()`（注入）在模型调用前，`MemoryMiddleware()`（抽取排队）
在 agent 执行后。开关从 `cfg.memory.enabled` 读。

### 跑测试

```bash
cd backend && make test    # 含 test/test_memory.py（94 个 hermetic 测试）
```

测试约定：`DEER_FLOW_HOME`→tmp_path；autouse 重置 queue + storage 单例防跨测试污染；updater 用
fake model（不碰真 LLM）；`get_memory_config` 经 `_patch_mem_config` 注入所有消费模块（import 绑定
不传播，必须逐模块 patch）；`get_config`（middleware 的 LangGraph 配置兜底）经 monkeypatch 替身。

---

## 与其它模块的关系

```
config/memory_config (MemoryConfig + get_memory_config ← get_app_config().memory)
config/paths (Paths.user_memory_file / user_agent_memory_file / agent_memory_file / memory_file)
config/agents_config (AGENT_NAME_PATTERN 校验 agent_name，红线 #32)
   │
agents/memory
   ├── storage.FileMemoryStorage (mtime 缓存 + 原子写 + per-user/agent 隔离)
   ├── message_processing (filter + correction/reinforcement 检测)
   ├── queue.MemoryUpdateQueue (去抖合并 + user_id 跨 Timer 捕获)
   ├── prompt (format_memory_for_injection + tiktoken 冷却降级)
   └── updater.MemoryUpdater (同步 LLM 路径 #2615 + fact CRUD + JSON 容错)
        ↑ create_chat_model(attach_tracing=True)（独立调用方，模型级回调）
   │
agents/middlewares
   ├── memory_middleware (after_agent → queue.add)
   └── dynamic_context_middleware (before_agent → ID-swap 注入 + to_thread 5s 超时)
        ↑ _get_memory_context (lead_agent/prompt，延迟导入 memory)
   │
runtime/user_context (get_effective_user_id → per-user 隔离的 user_id 来源)
```

- **上游**：`config`（memory_config + paths + agents_config 的 `AGENT_NAME_PATTERN`）、
  `models`（`create_chat_model`，记忆更新是独立调用方故 `attach_tracing=True`）、
  `runtime/user_context`（per-user 隔离的 user_id）。
- **下游消费者**：M16 SummarizationMiddleware（`summarization_hook` 待 M16 落地时接入，摘要前抢拍
  即将被压缩的消息进记忆）；M17 lead_agent（`_get_memory_context` 注入系统提示）；Gateway
  `/api/memory` 端点（CRUD 记忆，读/重载/清/增删 fact）。
- **依赖 M22**：`AGENT_NAME_PATTERN` + per-agent 存储路径从 agents_config 直接取（v1.2 起不再
  局部兜底）。

---

## 常见问题 / 排错

**Q：为什么记忆更新用同步 `model.invoke()` 而不是 async？**
A：防 issue #2615。langchain 的 async httpx 客户端池是全局缓存且与 lead agent 共享的。在记忆
更新里起第二个事件循环跑 `ainvoke` 会跨循环复用连接→炸。同步 `invoke` 用独立同步连接池，不碰
async 池。在事件循环里被调时用专用 `ThreadPoolExecutor` 卸载（但仍跑同步 invoke）。

**Q：用户连发 5 条消息，会调 5 次 LLM 抽取吗？**
A：不会。去抖队列把同一 `(thread, user, agent)` 的多次入队**合并**成一条，等 30s 静默后统一处理
一次。correction/reinforcement 标志取「或」。

**Q：为什么 `user_id` 要在入队时捕获，不能在 Timer 回调里取？**
A：`threading.Timer` 在另一线程触发，`ContextVar`（user_id 载体）**不跨裸线程传播**。回调里取会
拿到默认值 `"default"`，把所有人的记忆写进 default 桶。入队时（请求上下文活着）显式存进
`ConversationContext` 才对。

**Q：tiktoken 首次用很慢 / 卡住怎么办？**
A：tiktoken 首次要从公共网络下载 BPE 数据，网络受限环境（GFW 后）可阻塞数十分钟。两个办法：
① 设 `memory.token_counting: char` 完全跳过 tiktoken（无网络 CJK 感知估算）；② 失败有 600s
冷却降级，期间走字符估算，冷却后自愈。生产建议启动时 `warm_tiktoken_cache()` 预热。

**Q：记忆文件写一半崩溃会丢数据吗？**
A：不会。原子写：先写 `memory.<uuid>.tmp`，再 `replace` 成 `memory.json`。`replace` 在 POSIX
上原子——要么旧完整要么新完整，无中间态。损坏的 JSON 读时回退空结构（不抛）。

**Q：上传的文件会被记进长期记忆吗？**
A：不会。上传文件是 session 级的。`_strip_upload_mentions_from_memory` 用收窄正则从摘要和 fact
里移除上传相关句子（但故意不误删「works with CSV files」这类合法 fact）。`filter_messages_for_memory`
也会跳过纯上传消息及其紧跟的 AI 回复。

**Q：记忆会无限增长吗？**
A：不会。`max_facts`（默认 100）上限，超了按置信度降序留 top——低置信度老 fact 让位给高置信度
新 fact。注入也有 `max_injection_tokens`（默认 2000）预算截断，不会吃光上下文窗口。

**Q：Alice 的自定义 agent 记忆会影响 Bob 吗？**
A：不会。per-user + per-agent 隔离：Alice 的 `code-reviewer` 记忆在
`users/alice/agents/code-reviewer/memory.json`，Bob 的在 `users/bob/agents/code-reviewer/`，
完全分开。

**Q：`_get_memory_context` 出错会让 agent 起不来吗？**
A：不会。它吞掉所有异常返回 `""`（记忆是 nice-to-have，不能让它挂起 agent 启动）。`DynamicContextMiddleware`
的注入也有 5s 超时降级——tiktoken 卡住时跳过注入而非挂起请求。
