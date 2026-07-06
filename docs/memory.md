# 18. memory.md — 记忆系统（LLM 抽取 + 去抖队列 + per-user 原子存储 + 注入）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/agents/memory/`（7 文件，2089 行）。

> **一句话定位**：记忆让 agent **跨会话记住用户**——把对话里关于用户的事实/偏好/上下文用 LLM 抽出来，存成按用户隔离的 `memory.json`，下次对话时按 token 预算注入系统提示，实现个性化。本模块是一条「抽取 → 去抖 → 原子存储 → 预算注入」的完整管线，外加 fact 的增删改查。

> **先读谁最省事**：[user_context.md](user_context.md)（懂 `user_id` 怎么按请求隔离）+ [agents_config.md](agents_config.md)（懂 per-user 目录布局 + `AGENT_NAME_PATTERN`）+ [models.md](models.md)（懂 `create_chat_model`）。读完后，记忆就是「按 `user_id` 存的 JSON 文件 + 一条 LLM 抽取管线 + 一个注入器」。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「agent 的长期记忆和短期记忆分别是什么？为什么这么分？」** —— 短期=checkpointer（对话状态快照，见 [#8](checkpointer.md)）；长期=本模块（LLM 抽取的跨会话事实）。能讲清为什么长期记忆要「LLM 抽取」而不是「原样存对话」。
2. **「异步服务里要起一个新事件循环跑额外的 LLM 调用，会有什么坑？怎么解？」** —— 跨事件循环连接复用（共享的全局 httpx 连接池被两个循环抢）。能讲清为什么用「同步 `invoke` + 线程池卸载」而不是 `asyncio.run`。
3. **「为什么记忆更新要去做抖（debounce）而不是每条消息立刻处理？」** —— 用户连发 N 条只该记一笔。能讲清合并语义、`threading.Timer` 跨线程与 `ContextVar` 不传播的坑。
4. **「记忆数据怎么持久化才不会写一半崩溃丢光？」** —— 原子写（temp + rename）+ mtime 缓存。能讲清 `replace` 在 POSIX 上的原子性、为什么浅拷贝后再加 `lastUpdated`。
5. **「把动态内容（日期/记忆）注入系统提示，怎么不破坏前缀缓存？」** —— ID-swap 技术冻结首条消息。能讲清 `add_messages` 的 ID 替换语义、为什么动态部分要作为独立 `HumanMessage`。
6. **「token 预算紧张时，怎么保证『用户纠正过我的错误』这条记忆不被挤掉？」** —— guaranteed 类别独立预算 + 结构感知截断（Facts 块作受保护后缀）。
7. **「多用户共用一个 agent，记忆怎么隔离？又怎么保证用户名/agent 名不被拿来做路径穿越攻击？」** —— per-user/per-agent 分桶 + `AGENT_NAME_PATTERN` 校验。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **JSON** | 一种用文本存「嵌套字典/列表」的格式。`memory.json` 就是把 Python 的 dict 存成文件，下次读回来还是 dict。 |
| **进程 / 线程 / 事件循环** | 一个**进程**是一个跑起来的程序；一个进程里有多个**线程**可以并发干活；**事件循环**（event loop）是 Python 异步（`async/await`）的核心——它在一个线程里轮流推进很多「等待中」的任务（如等网络回包），谁的等好了就跑谁。**事件循环跑在某个线程里**，这一点本篇会反复用到。 |
| **同步 vs 异步调用** | **同步**：调一个函数，一直卡在那等它返回（如 `model.invoke()`）。**异步**：调一个函数立刻返回一个「未来结果」（awaitable），你 `await` 它时事件循环可以同时去干别的（如 `model.ainvoke()`）。 |
| **ContextVar** | Python 的「上下文变量」——可以给「当前这条请求」绑一个值（如 `user_id`），同一请求里的任意函数都能读到它，而不会和别的并发请求串。本篇的坑在于：它**不跨新开的裸线程**传播。 |
| `threading.Timer` | 「X 秒后在**另一个线程**里执行一个函数」的工具。本篇用它做去抖定时器，但它在新线程跑，正是上面 ContextVar 不传播的来源。 |
| **ThreadPoolExecutor** | 一个「线程池」——预先开好几个工作线程，你把任务 `submit()` 进去，它在某个工作线程里跑，你拿 `future.result()` 阻塞等结果。本篇用它把「阻塞的同步 LLM 调用」从事件循环里卸载出去。 |
| **原子写 / rename** | 把数据写到文件，最怕写一半进程崩了留下「半截文件」。**原子写**=先写到临时文件 `xxx.tmp`，全写完再 `replace`（重命名）成正式文件名。`replace` 在类 Unix 系统上是**原子**的：要么还是旧文件、要么已经是完整的新文件，不存在「半个新文件」的中间态。 |
| **mtime** | 文件的「最后修改时间」（modification time）。本篇拿它做缓存：只有文件的 mtime 变了才重新读盘，省掉每轮都解析 JSON。 |
| **ABC**（抽象基类） | 一个「只定接口、不写实现」的父类（`abc.ABC` + `@abc.abstractmethod`）。子类必须实现那些抽象方法才能实例化。本篇 `MemoryStorage` 是 ABC，`FileMemoryStorage` 是它的文件实现——这样以后想换 SQLite 后端，只需新写一个子类。 |
| **token / 上下文窗口** | LLM 不按「字」计费，按 **token**（大致是一个词或几个字符）算。「上下文窗口」是一次能塞给模型的 token 上限。记忆注入会吃掉这个窗口，所以要按预算截断。 |
| **前缀缓存** | 很多模型厂商会把「你上一次发的请求前缀」缓存住，下次前缀一样就直接复用、更快更省。所以系统提示词越稳定，缓存命中率越高——本篇的 ID-swap 就是为保住这个稳定性。 |
| **daemon 线程** | 「守护线程」——进程退出时它会被直接丢下不等它跑完。本篇的定时器线程是 daemon：进程突然退出时，还没刷盘的记忆更新会丢（best-effort，可接受）。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **fact** | 一条离散的、可量化的事实，如「偏好中文回答」「16k+ stars」。带 `category`（类别）/`confidence`（置信度 0–1）/`source`（来源线程）。和「段落摘要」相对。 |
| **去抖（debounce）** | 「攒着，等安静一会儿再一起处理」。用户连发 5 条消息，不每条都处理，等 30s 没新消息再统一处理一次。 |
| **correction / reinforcement 信号** | 用正则在最近对话里识别「用户纠正了你」（如「不对」「that's wrong」）或「用户夸你了」（如「完全正确」），命中就提示抽取 LLM 重点记一笔。 |
| **guaranteed 注入** | 给特定类别（默认 `correction`）的 fact 单独留一份 token 预算，保证它在你上下文很挤时也一定被注入——避免「用户纠正过的错误」在 token 紧张时被静默丢掉。 |
| **ID-swap** | 注入动态内容时，把内容塞进一条**复用了已有消息 ID** 的新消息，让框架的 `add_messages`「原地替换」而不是追加——从而把动态内容固定在某个位置、保持前缀稳定。 |
| **per-user / per-agent 隔离** | 记忆按 `(user_id, agent_name)` 分桶存到不同文件/目录，Alice 的记忆和 Bob 的完全分开，自定义 agent 还有自己专属的记忆。 |

---

## §3 整体结构：它在系统里的位置

记忆是一条**横跨两个时机**的管线——抽取发生在「一轮对话之后」，注入发生在「下一轮对话之前」：

```
        ┌──────────── 抽取侧（写记忆）────────────┐          ┌──── 注入侧（读记忆）────┐
        │                                         │          │                          │
对话结束 │  MemoryMiddleware (after_agent)         │ 下次对话 │  DynamicContextMiddleware│
        │   │ filter_messages_for_memory          │   开始   │   (before_agent)         │
        │   │ detect_correction/reinforcement     │          │   │ _get_memory_context  │
        │   │ get_effective_user_id()  ← user_context        │   │   load memory.json   │
        │   ▼                                     │          │   ▼   format_memory_for_injection
        │  MemoryUpdateQueue (去抖 30s)            │          │       （token 预算截断）  │
        │   │ Timer 触发 → 后台线程                │          │   ▼   ID-swap 注入首条消息│
        │   ▼                                     │          │  <system-reminder>       │
        │  MemoryUpdater (同步 model.invoke)       │          │   <memory>…</memory>      │
        │   │ LLM 抽 JSON → 容错解析 → 归一化        │          │   <current_date>…        │
        │   ▼                                     │          │  </system-reminder>      │
        │  _apply_updates (去重/置信度/max_facts)   │          └──────────────────────────┘
        │   ▼                                     │
        │  FileMemoryStorage.save (原子写)         │
        │   → users/{user_id}/memory.json          │
        └─────────────────────────────────────────┘
                          │
        fact CRUD（增删改）: create/delete/update_memory_fact —— Gateway / 工具可直调，同样经 storage
```

**七个文件的职责切分**（为什么这么拆见 [§9 设计动机](#9-设计动机分析为什么这么设计作用好处)）：

```
agents/memory/
├── __init__.py            # 导出全部公共 API（比上游多导出几个 back-compat 包装）
├── storage.py             # MemoryStorage(ABC) + FileMemoryStorage（mtime 缓存 + 原子写 + per-user/agent 隔离）
├── message_processing.py  # filter_messages_for_memory（过滤）+ detect_correction/detect_reinforcement（信号检测）
├── queue.py               # ConversationContext + MemoryUpdateQueue（去抖合并 + user_id 跨 Timer 捕获）
├── prompt.py              # MEMORY_UPDATE_PROMPT + format_memory_for_injection（预算截断）+ _count_tokens（tiktoken 冷却降级）
├── updater.py             # MemoryUpdater（同步 LLM 路径）+ fact CRUD + JSON 容错解析 + 去重/max_facts + 上传剔除
└── summarization_hook.py  # memory_flush_hook：摘要压缩消息前抢拍进记忆队列（防细节永久丢失）

（接入点，不在本包内）
agents/middlewares/memory_middleware.py        # after_agent：过滤→检测→捕获 user_id→queue.add
agents/middlewares/dynamic_context_middleware.py # before_agent：ID-swap 注入记忆+日期 + 跨午夜 + to_thread 5s 超时
agents/lead_agent/prompt.py                    # _get_memory_context（延迟导入 memory，吞异常返 ""）
config/memory_config.py                        # MemoryConfig + get_memory_config()（AppConfig.memory 访问器）
config/paths.py                                # Paths.memory_file / user_memory_file / agent_memory_file / user_agent_memory_file
```

**面试概念地图**：本篇对应「长期记忆 vs 短期记忆」「上下文工程」「异步/并发设计」三个面试常考点（见 [README.md](README.md) 面试地图）。`deerflow-book` 的 `11-memory-architecture.md` / `12-memory-pipeline.md` 是可选的概念预读（借它自顶向下的讲法，实现看本篇的代码）。

---

## §4 核心概念：memory.json 长什么样

记忆文件是一个三段式 JSON：

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
    "recentMonths":       { "summary": "近期在研究 agent 架构", "updatedAt": "..." },
    "earlierContext":     { "summary": "...",                   "updatedAt": "..." },
    "longTermBackground": { "summary": "...",                   "updatedAt": "..." }
  },
  "facts": [
    { "id": "fact_a1b2c3d4", "content": "偏好用中文回答", "category": "preference",
      "confidence": 0.9, "createdAt": "...", "source": "thread-42" }
  ]
}
```

- **user**（当前状态，精炼）：工作 / 个人 / 当前关注点（`topOfMind` 最常更新）。
- **history**（时间线，段落）：近几月 / 更早 / 长期背景。
- **facts**（离散事实，带置信度）：具体可量化的事，如「16k+ stars」「偏好深色模式」。

**为什么是这三段、不是一锅？** 这是对「记忆」做了分层抽象：`user`/`history` 是**段落摘要**（连续的、需要 LLM 重写的散文），`facts` 是**离散条目**（一条一条、可增删可按置信度排序）。两类的更新和注入策略完全不同——段落要 `shouldUpdate`+非空才覆写，事实要逐条去重、卡置信度阈值、按 `max_facts` 裁剪。

fact 的 `category` 取值：`preference`（偏好）/ `knowledge`（专长）/ `context`（背景）/ `behavior`（行为模式）/ `goal`（目标）/ `correction`（纠正，含 `sourceError` 描述要避免的错误）。`correction` 类别是 [§9 guaranteed 注入](#9-设计动机分析为什么这么设计作用好处) 的默认保护对象。

**空记忆结构**由 [create_empty_memory()](../backend/packages/harness/deerflow/agents/memory/storage.py#L40) 造，三个段都预填了空 summary，保证下游代码永远能 `current_memory["user"]["topOfMind"]` 而不 KeyError。

---

## §5 代码走读：重要函数逐个讲

### §5.1 storage.py —— 存储层（mtime 缓存 + 原子写 + 隔离）

**`utc_now_iso_z()`** [storage.py:35](../backend/packages/harness/deerflow/agents/memory/storage.py#L35)：当前 UTC 时间的 ISO 字符串 + `Z` 后缀。所有时间戳统一格式，和历史的「naive UTC」输出保持一致。

**`FileMemoryStorage.load()`** [storage.py:141](../backend/packages/harness/deerflow/agents/memory/storage.py#L141) —— 带缓存的读：

```python
def load(self, agent_name=None, *, user_id=None) -> dict:
    file_path = self._get_memory_file_path(agent_name, user_id=user_id)
    cache_key = self._cache_key(agent_name, user_id=user_id)
    current_mtime = file_path.stat().st_mtime if file_path.exists() else None  # 问 OS：文件改过没
    with self._cache_lock:
        cached = self._memory_cache.get(cache_key)
        if cached is not None and cached[1] == current_mtime:   # mtime 没变 → 直接返回缓存
            return cached[0]
    memory_data = self._load_memory_from_file(agent_name, user_id=user_id)  # 变了才读盘
    with self._cache_lock:
        self._memory_cache[cache_key] = (memory_data, current_mtime)        # 回填
    return memory_data
```

**为什么用 mtime 而不是纯内存缓存？** 因为记忆文件可能被**外部**改（你手动编辑 `memory.json`、或 fact CRUD 直写）。mtime 是 OS 维护的「文件最后改动时间」，几乎零成本（一次 `stat` 系统调用），却能让缓存自动失效——改了盘上文件，下次 load 就重读。缓存键是 `(user_id, agent_name)` 元组，每个桶独立缓存。

**`FileMemoryStorage.save()`** [storage.py:178](../backend/packages/harness/deerflow/agents/memory/storage.py#L178) —— 原子写：

```python
def save(self, memory_data, agent_name=None, *, user_id=None) -> bool:
    file_path = self._get_memory_file_path(agent_name, user_id=user_id)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        memory_data = {**memory_data, "lastUpdated": utc_now_iso_z()}  # 浅拷贝 + 加时间戳
        temp_path = file_path.with_suffix(f".{uuid.uuid4().hex}.tmp")   # 临时文件名带 uuid
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(memory_data, f, indent=2, ensure_ascii=False)
        temp_path.replace(file_path)    # ← 原子的重命名
        # ... 刷缓存（新 mtime）
        return True
    except OSError as e:
        logger.error(...); return False
```

两个细节的动机：① **先浅拷贝再加 `lastUpdated`**——不修改调用方传进来的 dict（避免副作用），也保证写盘成功前缓存里的引用不被悄悄改；② **临时文件名带 uuid**——多个并发写不会撞同一个临时文件名。

**`_get_memory_file_path()`** [storage.py:95](../backend/packages/harness/deerflow/agents/memory/storage.py#L95) —— 路径解析优先级（这就是「隔离」的实现）：

1. `user_id` + `agent_name` → `users/{user_id}/agents/{name}/memory.json`（per-user per-agent）
2. `user_id`（无 agent_name）→ `users/{user_id}/memory.json`（per-user 全局）；若配了绝对 `storage_path` 则退出隔离
3. 仅 `agent_name`（无 user_id）→ legacy per-agent 文件（只读回退）
4. 都没有 → legacy 全局 `memory.json`（只读回退）

**`_validate_agent_name()`** [storage.py:88](../backend/packages/harness/deerflow/agents/memory/storage.py#L88)：拼路径前用 `AGENT_NAME_PATTERN`（从 [agents_config](agents_config.md) 取）校验 agent 名——防止 `../../etc` 这类名字做路径穿越。`get_memory_storage()` [storage.py:215](../backend/packages/harness/deerflow/agents/memory/storage.py#L215) 是带双重检查锁的单例工厂，加载失败回退 `FileMemoryStorage`。

### §5.2 message_processing.py —— 过滤 + 信号检测（纯函数）

**`filter_messages_for_memory()`** [message_processing.py:64](../backend/packages/harness/deerflow/agents/memory/message_processing.py#L64) —— 决定哪些消息值得喂给抽取 LLM。规则：

- **human 消息**：带 `hide_from_ui` 标记的**直接跳过**（这些是中间件注入的框架内部文本——TodoMiddleware 的待办提醒、ViewImageMiddleware、DynamicContextMiddleware 的 `__memory` 载荷等。它们进记忆会污染长期记忆，`__memory` 还会自我放大循环）；然后剥 `<uploaded_files>` 块，剥光后为空则**跳过它及其后紧跟的 AI 回复**（纯上传轮不贡献记忆）。
- **ai 消息**：没有 `tool_calls` 才留（有 tool_calls 是中间步骤，不是最终回复）。

关键代码：

```python
if msg_type == "human":
    if getattr(msg, "additional_kwargs", {}).get("hide_from_ui"):
        continue                      # 框架内部文本，绝不进记忆 LLM
    content_str = extract_message_text(msg)
    if "<uploaded_files>" in content_str:
        stripped = _UPLOAD_BLOCK_RE.sub("", content_str).strip()
        if not stripped:
            skip_next_ai = True       # 纯上传 → 连带跳过下一条 AI
            continue
        ...
elif msg_type == "ai":
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:                # 只留最终回复
        if skip_next_ai: skip_next_ai = False; continue
        filtered.append(msg)
```

**`detect_correction()` / `detect_reinforcement()`** [message_processing.py:108](../backend/packages/harness/deerflow/agents/memory/message_processing.py#L108)：在最近 6 条 human 消息里用中英双语正则（`_CORRECTION_PATTERNS` / `_REINFORCEMENT_PATTERNS`）扫显式信号（「不对」「that's wrong」「完全正确」「exactly right」）。命中就给抽取 LLM 加 hint。

### §5.3 queue.py —— 去抖队列（合并 + user_id 跨 Timer 捕获）

**`ConversationContext`** [queue.py:27](../backend/packages/harness/deerflow/agents/memory/queue.py#L27)：一个 dataclass，装一轮待处理的对话（thread_id / messages / agent_name / **user_id** / correction/reinforcement 标志）。

**`add()`** [queue.py:61](../backend/packages/harness/deerflow/agents/memory/queue.py#L61)：入队 + 重置去抖定时器。核心在 `_enqueue_locked()` [queue.py:125](../backend/packages/harness/deerflow/agents/memory/queue.py#L125) 的**合并**：

```python
queue_key = self._queue_key(thread_id, user_id, agent_name)   # (thread, user, agent)
existing_context = next((c for c in self._queue if same_key(c, queue_key)), None)
# correction/reinforcement 标志取「或」（任一轮命中就保留）
merged_correction = correction_detected or (existing.correction_detected if existing else False)
# 把同 key 的旧条目删掉，append 新条目（最新消息覆盖旧的）
self._queue = [c for c in self._queue if not same_key(c, queue_key)]
self._queue.append(context)
```

**为什么合并？** 同一 `(thread, user, agent)` 的多次入队，处理最新这一条就够了（它包含全部新消息），correction/reinforcement 用「或」合并保证信号不丢。`_reset_timer()` [queue.py:154](../backend/packages/harness/deerflow/agents/memory/queue.py#L154) 每次入队都把定时器重置成 `debounce_seconds`——所以「30s 内不断有新消息」会一直推迟处理，直到真正静默 30s。

**`_process_queue()`** [queue.py:173](../backend/packages/harness/deerflow/agents/memory/queue.py#L173)：Timer 回调，在**另一线程**触发。延迟导入 `MemoryUpdater`（防循环依赖），逐条调 `update_memory`，多条之间 sleep 0.5s 防限流。用 `self._processing` 标志防重入：另一个 worker 正在跑时，重新调度 0s 定时器保留「立即冲刷」语义。

**`add_nowait()`** [queue.py:98](../backend/packages/harness/deerflow/agents/memory/queue.py#L98)：0s 定时器，立即后台处理——供 summarization_hook 在「摘要删消息前」抢拍用。

### §5.4 updater.py —— 抽取 + 应用 + fact CRUD（最重的文件）

**同步 LLM 路径**（本模块最关键的设计）由模块级线程池 [updater.py:43](../backend/packages/harness/deerflow/agents/memory/updater.py#L43) 支撑：

```python
_SYNC_MEMORY_UPDATER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="memory-updater-sync")
atexit.register(lambda: _SYNC_MEMORY_UPDATER_EXECUTOR.shutdown(wait=False))
```

**`update_memory()`** [updater.py:528](../backend/packages/harness/deerflow/agents/memory/updater.py#L528) —— 同步入口，做事件循环感知分流：

```python
def update_memory(self, messages, thread_id=None, agent_name=None, ..., user_id=None) -> bool:
    try:
        loop = asyncio.get_running_loop()      # 当前线程有没有正在跑的事件循环？
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # 在事件循环里被调 → 卸载到线程池（但仍跑同步 invoke）
        future = _SYNC_MEMORY_UPDATER_EXECUTOR.submit(self._do_update_memory_sync, ...)
        return future.result()                 # 阻塞等结果，但不卡调用方的循环
    # 没事件循环 → 直接同步跑
    return self._do_update_memory_sync(...)
```

**`_do_update_memory_sync()`** [updater.py:485](../backend/packages/harness/deerflow/agents/memory/updater.py#L485)：构造 prompt → `model.invoke()`（**同步**）→ 解析应用。注意 `model.invoke(prompt, config={"run_name": "memory_agent"})` [updater.py:513](../backend/packages/harness/deerflow/agents/memory/updater.py#L513)——记忆更新是**独立调用方**，故走自己的回调链。`aupdate_memory()` [updater.py:460](../backend/packages/harness/deerflow/agents/memory/updater.py#L460) 是异步壳，用 `asyncio.to_thread` 委托给同一个同步路径。

**`_parse_memory_update_response()`** [updater.py:309](../backend/packages/harness/deerflow/agents/memory/updater.py#L309) —— JSON 容错解析：即便要求只返 JSON，有些 provider 会把它包在思考痕迹/markdown 围栏里。用 `json.JSONDecoder.raw_decode` 从每个 `{` 开始尝试，找到第一个含全部四个必需顶层键（`user/history/newFacts/factsToRemove`）的合法对象。**不安全部分更新防御** [updater.py:294](../backend/packages/harness/deerflow/agents/memory/updater.py#L294)：`factsToRemove` 非空但 `newFacts` 有坏条目时直接抛 `JSONDecodeError`——避免「删了旧 fact 但新 fact 是坏的」这种半残状态。

**`_apply_updates()`** [updater.py:575](../backend/packages/harness/deerflow/agents/memory/updater.py#L575) —— 把 LLM 产出应用到记忆（fail-closed）：

- user/history 段需 `shouldUpdate` + 非空 `summary` 才覆写；
- 删 `factsToRemove` 里的 fact；
- 加新 fact：卡 `fact_confidence_threshold`（默认 0.7）、去重（content 经 `_fact_content_key` [updater.py:365](../backend/packages/harness/deerflow/agents/memory/updater.py#L365) 的 `casefold()` 比对）、**空白 fact 跳过**（`fact_key is None` 时 `continue`，两条独立守卫）；
- 超 `max_facts`（默认 100）按置信度降序留 top。

**`_strip_upload_mentions_from_memory()`** [updater.py:342](../backend/packages/harness/deerflow/agents/memory/updater.py#L342)：用收窄的正则 `_UPLOAD_SENTENCE_RE` [updater.py:331](../backend/packages/harness/deerflow/agents/memory/updater.py#L331) 从摘要和 fact 里移除上传相关句子——上传文件是 session 级的，记进长期记忆会让 agent 下次去找不存在的文件。正则故意收窄，不误删「works with CSV files」这类合法 fact。

**fact CRUD**：`create_memory_fact` [updater.py:97](../backend/packages/harness/deerflow/agents/memory/updater.py#L97) / `delete_memory_fact` [updater.py:134](../backend/packages/harness/deerflow/agents/memory/updater.py#L134)（id 不存在抛 KeyError）/ `update_memory_fact` [updater.py:151](../backend/packages/harness/deerflow/agents/memory/updater.py#L151)——都经 storage 持久化，`_validate_confidence` [updater.py:90](../backend/packages/harness/deerflow/agents/memory/updater.py#L90) 保证置信度是合法的 [0,1] 浮点（拒 NaN/inf）。

### §5.5 prompt.py —— 注入格式化 + token 计数（tiktoken 冷却降级）

**`format_memory_for_injection()`** [prompt.py:378](../backend/packages/harness/deerflow/agents/memory/prompt.py#L378) —— 把记忆格式化成注入串，按 token 预算截断：

- 先放 user/history 段；
- facts 分两阶段选（guaranteed → 常规）：guaranteed 类别从独立的 `guaranteed_token_budget` 选、放在 Facts 块最前；常规 fact 竞争 `max_tokens` 主预算；
- 超预算时**结构感知截断**——把 Facts 块当**受保护后缀**，只截前面的 user/history 段，绝不静默丢掉 guaranteed fact。

签名：

```python
def format_memory_for_injection(memory_data, max_tokens=2000, *, use_tiktoken=True,
        guaranteed_categories: list[str] | None = None,
        guaranteed_token_budget: int = 500) -> str:
```

**`_count_tokens()`** [prompt.py:253](../backend/packages/harness/deerflow/agents/memory/prompt.py#L253) —— token 计数，两种策略：
- `tiktoken`（默认）：精确，但首次用可能从公共网络下载 BPE 数据，网络受限环境会长时间阻塞。有**冷却降级**：`_get_tiktoken_encoding()` [prompt.py:195](../backend/packages/harness/deerflow/agents/memory/prompt.py#L195) 失败后记时间戳，600s（`_TIKTOKEN_RETRY_COOLDOWN_S`）内立即回退、不重触发阻塞下载，冷却后自愈。
- `char`：无网络的 CJK 感知字符估算 `_char_based_token_estimate()` [prompt.py:236](../backend/packages/harness/deerflow/agents/memory/prompt.py#L236)（英文 ~4 字符/token、CJK ~2 字符/token），从不碰 tiktoken。

**`warm_tiktoken_cache()`** [prompt.py:276](../backend/packages/harness/deerflow/agents/memory/prompt.py#L276)：启动时在线程外预热，首个请求不阻塞在 BPE 下载。

**`format_conversation_for_update()`** [prompt.py:598](../backend/packages/harness/deerflow/agents/memory/prompt.py#L598)：把对话格式化成抽取 prompt 输入——human 消息剥 `<uploaded_files>`、超长截断到 1000 字符。

### §5.6 summarization_hook.py —— 摘要前抢拍

**`memory_flush_hook()`** [summarization_hook.py:27](../backend/packages/harness/deerflow/agents/memory/summarization_hook.py#L27)：挂在 SummarizationMiddleware 的 `before_summarization`——消息被压成摘要（删掉原文）前，把它们经 `filter_messages_for_memory` 过滤后用 `queue.add_nowait` 抢拍进记忆队列。是「对话被压成摘要但仍记住了细节」的关键衔接。`resolve_runtime_user_id(event.runtime)` 解析这次运行的 user_id。

---

## §6 数据流：一次调用怎么走完

### §6.1 数据流 A：用户说「我用 Python 写后端，偏好简洁的代码」→ 一天后还记得

```
① 用户发消息 ─→ lead agent 正常回复（这轮完全不碰记忆）
② MemoryMiddleware.after_agent 触发（一轮结束）
   ├─ filter_messages_for_memory(messages)        → 只留 user 输入 + AI 最终回复
   ├─ detect_correction / detect_reinforcement    → 无命中
   ├─ get_effective_user_id()                      → "alice"
   └─ queue.add(thread_id, filtered, agent_name=None, user_id="alice")
        ├─ _enqueue_locked：合并同 (thread, alice, None) 的旧条目
        └─ _reset_timer：设 30s 定时器
③ 用户 30s 内没再发消息 ─→ Timer 在后台线程触发 _process_queue
   ├─ MemoryUpdater().update_memory(messages, user_id="alice")
   │    ├─ 当前在事件循环里 → submit 到 _SYNC_MEMORY_UPDATER_EXECUTOR → future.result()
   │    └─ _do_update_memory_sync：
   │         ├─ get_memory_data(agent_name=None, user_id="alice")  → 读现有记忆（mtime 缓存命中）
   │         ├─ model.invoke(MEMORY_UPDATE_PROMPT.format(...))     ← 同步 HTTP，独立连接池
   │         ├─ _parse_memory_update_response(response.content)    → JSON → 归一化
   │         ├─ _apply_updates(current_memory, update_data)        → 加 fact（去重/置信度）
   │         ├─ _strip_upload_mentions_from_memory(...)            → 剔上传
   │         └─ storage.save(updated, user_id="alice")             → 原子写 users/alice/memory.json
   └─ （写完，单例缓存刷新成新 mtime）

④ 第二天，用户新开对话 ─→ DynamicContextMiddleware.abefore_agent 触发（一轮开始前）
   └─ asyncio.to_thread(_inject, state) + wait_for(5s 超时)
        ├─ _build_full_reminder()
        │    ├─ _get_memory_context(agent_name, app_config)
        │    │    ├─ get_memory_data(user_id=get_effective_user_id())  → 读 users/alice/memory.json
        │    │    ├─ format_memory_for_injection(..., guaranteed_categories=["correction"])
        │    │    │      → "User Context:\n- Work: 工程师…\nFacts:\n- [preference|0.90] 偏好简洁的代码"
        │    │    └─ 包 <memory>…</memory>
        │    └─ 拼 <system-reminder><memory>…</memory><current_date>2026-06-22, Monday</current_date></system-reminder>
        └─ _make_reminder_and_user_messages(首条用户消息, reminder)
             ├─ reminder_msg：id=首条消息的原 id（ID-swap，add_messages 原地替换）
             └─ user_msg：id="{原id}__user"（原内容紧随其后）
   → lead agent 这次「看到」了记忆，回复时就个性化了
```

### §6.2 数据流 B：用户纠正「不对，我用的是 Rust」→ correction fact 被高置信度记录 + guaranteed 保底注入

```
① 用户：「不对，我用的是 Rust」
② filter → detect_correction 命中（正则 "不对"）→ correction_detected=True
③ queue.add(..., correction_detected=True)  → 合并时取「或」
④ 30s 后抽取：_build_correction_hint 给 prompt 加 hint（correction_detected 分支）
   → LLM 产出 {category:"correction", confidence:0.95, sourceError:"误认为用户用 Python"}
   → _apply_updates：confidence 0.95 ≥ 0.7 阈值 → 加入 facts（旧「用 Python」若还在可被 factsToRemove 删）
⑤ 下次注入：format_memory_for_injection
   ├─ 阶段1：correction 类别 fact 从 guaranteed_token_budget(500) 先选、放最前
   ├─ 阶段2：常规 fact 竞争剩余 max_tokens
   └─ 即便上下文挤到常规 fact 全被截断，correction fact 仍保底注入（受保护后缀）
```

---

## §7 配置与用法

### §7.1 配置（`config.yaml` → `memory` 段，对应 `MemoryConfig`）

| 字段 | 默认 | 作用 |
|---|---|---|
| `enabled` | `true` | 主开关（关了不抽取也不注入） |
| `injection_enabled` | `true` | 注入开关（关了只抽取不注入） |
| `storage_path` | `""` | 空=per-user 隔离；绝对路径=所有用户共享一文件（退出隔离）；相对路径相对 base_dir |
| `storage_class` | `...FileMemoryStorage` | 存储后端类路径（可换成自定义 SQLite 等，须是 `MemoryStorage` 子类） |
| `debounce_seconds` | `30` | 去抖秒数（1–300） |
| `model_name` | `null` | 抽取用的模型；null=默认模型 |
| `max_facts` | `100` | 最多存的事实条数（10–500） |
| `fact_confidence_threshold` | `0.7` | 存储事实的最低置信度（0–1） |
| `max_injection_tokens` | `2000` | 注入最多占用的 token（100–8000） |
| `token_counting` | `tiktoken` | `tiktoken`（精确）/ `char`（无网络） |
| `guaranteed_categories` | `["correction"]` | 保底注入的 fact 类别（独立预算） |
| `guaranteed_token_budget` | `500` | guaranteed 类别的 token 上限（50–2000） |

`get_memory_config()` [memory_config.py](../backend/packages/harness/deerflow/config/memory_config.py) 是 `get_app_config().memory` 的便捷访问器——mini 不为 memory 维护独立单例，所有子配置挂在 `AppConfig` 上走热重载边界。

### §7.2 挂载与运行

- **自动挂载**：[middlewares/__init__.py](../backend/packages/harness/deerflow/agents/middlewares/__init__.py) 已按配置挂载——`DynamicContextMiddleware`（注入，模型调用前，步骤 11）、`SummarizationMiddleware`（可选，步骤 13）、`MemoryMiddleware`（抽取排队，agent 执行后，步骤 17）。开关从 `cfg.memory.enabled` 读。
- **跑测试**：`cd backend && make test`，含 `test/test_memory.py`（**102 个 hermetic 测试**）。约定：`DEER_FLOW_HOME`→tmp_path；autouse 重置 queue + storage 单例防跨测试污染；updater 用 fake model（不碰真 LLM）。

---

## §8 与其它模块的关系

```
config/memory_config (MemoryConfig + get_memory_config ← AppConfig.memory)
config/paths (Paths.user_memory_file / user_agent_memory_file / agent_memory_file / memory_file)
config/agents_config (AGENT_NAME_PATTERN 校验 agent_name)
   │
agents/memory
   ├── storage.FileMemoryStorage (mtime 缓存 + 原子写 + per-user/agent 隔离)
   ├── message_processing (filter + correction/reinforcement 检测)
   ├── queue.MemoryUpdateQueue (去抖合并 + user_id 跨 Timer 捕获)
   ├── prompt (format_memory_for_injection + tiktoken 冷却降级 + guaranteed 注入)
   ├── updater.MemoryUpdater (同步 LLM 路径 + fact CRUD + JSON 容错)
   └── summarization_hook.memory_flush_hook (摘要前抢拍)
        ↑ create_chat_model(attach_tracing=True)（独立调用方，模型级回调）
   │
agents/middlewares
   ├── memory_middleware (after_agent → queue.add)
   ├── dynamic_context_middleware (before_agent → ID-swap 注入 + to_thread 5s 超时)
   │     ↑ _get_memory_context (lead_agent/prompt，延迟导入 memory)
   └── summarization_middleware (before_summarization → memory_flush_hook)
   │
runtime/user_context (get_effective_user_id → per-user 隔离的 user_id 来源)
```

- **上游**：[config](config.md)（memory_config + paths + agents_config 的 `AGENT_NAME_PATTERN`）、[models](models.md)（`create_chat_model`，记忆更新是独立调用方故走模型级回调）、[runtime/user_context](user_context.md)（per-user 的 user_id）。
- **下游消费者**：[middlewares](middlewares.md)（DynamicContextMiddleware 注入、MemoryMiddleware 抽取、SummarizationMiddleware 抢拍）、[agents_config](agents_config.md)（`AGENT_NAME_PATTERN` + per-agent 存储路径的直接来源）。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **同步 `model.invoke` + 线程池卸载** | langchain 的 async httpx 连接池是全局缓存、和 lead agent 共享；起第二个事件循环跑 `ainvoke` 会跨循环复用连接 | 记忆更新的 LLM 调用用独立同步连接池，永不碰 async 池 | `asyncio.run` → 新循环 → 跨循环抢连接 → 炸 |
| **去抖 + 合并** | 用户连发 N 条，每条都调 LLM 既慢又费钱 | N 条合并成 1 次抽取，省钱省时 | 每条一次 LLM：账单爆炸 + 限流 |
| **user_id 入队时捕获** | Timer 在另一线程触发，ContextVar 不跨裸线程传播 | 后台处理时拿到正确的 user，记忆存对桶 | 回调里才取 → 拿到默认值 → 全员记忆混进 default 桶 |
| **原子写（temp+rename）** | 写一半崩溃留半截 JSON，下次解析失败全丢 | `replace` 原子：要么旧完整要么新完整 | 断电 → 半截文件 → 记忆全毁 |
| **mtime 缓存** | 每轮都解析 JSON 太慢；但文件可能被外部改 | stat 几乎零成本，却自动失效缓存 | 纯内存缓存 → 外部改了读不到；每轮读盘 → 慢 |
| **ID-swap 注入** | 动态内容改系统提示会破坏前缀缓存 | 动态部分作独立消息、首条 ID 冻结，整个会话前缀稳定 | 每轮改系统提示 → 缓存命中率暴跌 |
| **guaranteed 独立预算** | token 紧张时按置信度截断会丢掉「用户纠正」 | correction fact 从独立预算保底注入 | 用户纠正过的错误又被犯 |
| **per-user/agent 分桶 + 校验** | 多用户共用一个 agent；agent 名拼进路径 | 隔离 + 防穿越 | 用户记忆串味；`../` 名做路径穿越 |
| **同步 LLM 抽取而非原样存对话** | 原样存对话 = 无界增长 + 噪声 | LLM 提炼成结构化 fact，可控、可注入 | 记忆膨胀失控、注入吃光上下文 |

### §9.1 为什么记忆更新走「同步 `invoke` + 线程池」而不是 async

**动机**：解决跨事件循环连接复用。langchain 的 async httpx `AsyncClient` / 连接池是**全局缓存**的（经 `@lru_cache`），和 lead agent 共享同一个。如果记忆更新用 `asyncio.run()` 起一个**新事件循环**跑 `model.ainvoke()`，会出现「同一个连接被两个事件循环抢着用」——连接状态错乱直接炸（这是上游 issue #2615 描述的真实 bug）。

**作用 / 好处**：记忆更新一律走**同步** `model.invoke()`（同步 HTTP，独立连接池），不创建第二个事件循环。在事件循环里被调时（如 LangGraph node），用专用 `ThreadPoolExecutor` 卸载、`future.result()` 阻塞等结果——但仍跑同步 invoke，不碰 async 池。

**不这么设计会怎样**：

```
错（炸）：asyncio.run() → 新事件循环 → model.ainvoke() → 抢共享 async 连接池 → 跨循环复用 💥
对（本模块）：ThreadPoolExecutor → model.invoke()（同步）→ 独立同步连接池 → 不碰 async 池 ✅
```

### §9.2 为什么 user_id 必须在「入队时」捕获

**动机**：`MemoryUpdateQueue` 的去抖用 `threading.Timer`，它在**另一个线程**触发 `_process_queue`。而 `user_id` 的载体是 `ContextVar`（[user_context.md](user_context.md)）——它**不跨裸线程传播**。

**作用 / 好处**：在 `add()` 入队时（请求上下文还活着）就把 `user_id` 显式存进 `ConversationContext`，Timer 回调直接用存好的值。

**不这么设计会怎样**：在回调里才取 `get_effective_user_id()`，拿到的是默认值 `"default"`——所有人的记忆全写进 `default` 桶。这是隐蔽但致命的 bug。

### §9.3 为什么去抖 + 合并

**动机**：用户连续发 5 条消息，每条都触发 `MemoryMiddleware`。每条都调一次 LLM 抽取既慢又费钱，而且分 5 次记不如整体记一笔准。

**作用 / 好处**：攒着，30s 内没新消息再统一处理。同一 `(thread_id, user_id, agent_name)` 的多次入队合并成一条（最新覆盖旧的，correction/reinforcement 标志取「或」）。

**不这么设计会怎样**：每条一次 LLM——账单爆炸、被限流、且 5 次抽取的 fact 可能重复冲突。

### §9.4 为什么原子写 + mtime 缓存

**动机（原子写）**：记忆是用户长期积累的宝贵数据，写一半崩溃（断电/进程被杀）留半截 JSON，下次读解析失败→全丢。
**动机（mtime 缓存）**：注入每轮都要读记忆，每轮解析 JSON 太慢；但记忆文件可能被外部（手动编辑/fact CRUD）改，纯内存缓存会读到旧数据。

**作用 / 好处**：原子写先写 `memory.<uuid>.tmp` 再 `replace`（POSIX 原子）。mtime 缓存用一次 `stat` 判断文件改没改，几乎零成本却自动失效。

**不这么设计会怎样**：直接覆写 → 断电半截文件 → 全丢；纯内存缓存 → 外部改了读不到。

### §9.5 为什么 ID-swap 注入（缓存友好）

**动机**：动态上下文（日期 + 记忆）要注入，但基础系统提示词要保持**静态**以最大化前缀缓存复用。每轮都改系统提示，缓存命中率暴跌。

**作用 / 好处**：把动态部分作为**独立 HumanMessage** 注入，用 **ID-swap** 冻结首条用户消息——
- 首轮：完整提醒（记忆+日期）作为新 HumanMessage，**复用首条用户消息的 ID**（`id=stable_id`），让 LangGraph 的 `add_messages`（按 ID 去重/替换）原地替换它（保位置）；原内容用派生 ID `"{stable_id}__user"` 紧随其后 append。
- 之后：首条消息（现在是提醒）**内容永不变** → 整个会话前缀稳定 → 每轮命中缓存。
- 跨午夜：检测日期变化，给当前轮注入轻量日期更新提醒（同 ID-swap 技术）。

**不这么设计会怎样**：每轮把记忆/日期拼进系统提示 → 系统提示每轮变 → 前缀缓存全失效 → 又慢又贵。

### §9.6 为什么 guaranteed 注入（保底通道）

**动机**：`format_memory_for_injection` 按置信度排序 + 预算截断。但「用户纠正过的错误」这类 fact 价值极高，不该在 token 紧张时被静默丢掉。

**作用 / 好处**：给指定类别（默认 `correction`）从独立的 `guaranteed_token_budget` 先选、放 Facts 块最前；常规 fact 竞争主预算。超预算时 Facts 块作**受保护后缀**，只截前面的段。常见情况总输出仍落在 `max_tokens` 内（guaranteed 行挤占常规行）；仅当 guaranteed 行单独顶过 `max_tokens` 时预算才叠加、安全截断上限相应抬高。

**不这么设计会怎样**：统一置信度截断 → 上下文一挤 → 「用户纠正过的错误」被丢 → 同样的错误又被犯。

### §9.7 为什么 seven-file 拆分

- **`storage.py`** 单独：存储 I/O（读/写/缓存/原子性）独立于「抽什么/怎么抽」，换后端（如 SQLite）只改这一文件。
- **`message_processing.py`** 单独：消息筛选是纯函数、无副作用，便于单测，且被 queue（入队前过滤）与 middleware 共享。
- **`queue.py`** 单独：去抖 + 跨线程状态是独立于「更新逻辑」的并发控制关注点，隔离 `threading.Timer` 的复杂性。
- **`prompt.py`** 单独：prompt 文本 + token 计数是与逻辑无关的「内容 + 度量」层，调 prompt 不碰更新/存储代码。
- **`updater.py`** 单独：这是最重、改动最频的文件（抽取 + 应用 + CRUD 全在这），与存储/队列/筛选解耦。
- **`summarization_hook.py`** 单独：摘要前抢拍是跨模块衔接（memory ↔ summarization middleware），单独隔离防耦合扩散。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/agents/memory/`（与 mini 同布局，7 文件）。**先剥 docstring/comment 再判逻辑差**（mini 是中文 docstring、上游是英文，行数差不等于逻辑差）。

**总结论：高度忠实移植，近 0 逻辑差。** 剥 docstring 后逐文件比对：

| 文件 | mini 行 | 上游行 | 剥 docstring 后逻辑差 |
|---|---|---|---|
| `storage.py` | 249 | 231 | **0 逻辑差**（docstring 中英 + 个别行内注释）——mtime 缓存/原子写/路径解析优先级/单例工厂逐行一致 |
| `queue.py` | 280 | 287 | **0 逻辑差**（剥后 190=190；仅 `def __init__(self) -> None:` vs `def __init__(self):` 注解差）——去抖合并/Timer/processing 重入/`add_nowait` 全一致 |
| `message_processing.py` | 129 | 116 | **0 逻辑差**（docstring 差）——`filter_messages_for_memory` 的 hide_from_ui 跳过/纯上传连带跳过、correction/reinforcement 中英双语正则全一致 |
| `summarization_hook.py` | 49 | 34 | **0 逻辑差**（docstring + import 单行/多行格式差） |
| `updater.py` | 672 | 709 | **近 0 逻辑差**（剥后 481 vs 479）——唯一差：mini 多一个 2 行 `_create_empty_memory()` back-compat 包装（`return create_empty_memory()`）。同步线程池/事件循环感知分流/JSON 容错/不安全部分更新防御/`_apply_updates` 去重+空白跳过+max_facts/上传剔除 **逐行一致** |
| `prompt.py` | 636 | 728 | **近 0 逻辑差**（剥后 405 vs 406）——差异全是**等价改写**：① CJK 字符范围 mini 用**字面量字符**（`"一" <= ch <= "鿿"`）、上游用 **`\u` 转义**（`"一" <= ch <= "鿿"`，另两段同样 `぀..ヿ` / `가..힣`）——**同一范围、0 功能差**，纯书写风格；② type hint mini 加引号 `"tiktoken.Encoding \| None"`、上游不加；③ mini 把 `facts_header`/`all_fact_lines` 初始化提到 try/if 外做防御性引用、上游在内部初始化（**无行为差**）；④ 上游多了个 `guaranteed_line_budget` 别名变量（等价）。**两边都有完整的 guaranteed 注入（#3592）** |
| `__init__.py` | 74 | 57 | mini **多导出几个符号**：`create_empty_memory`/`utc_now_iso_z`/`create_memory_fact`/`import_memory_data`/`update_memory_fact`/`memory_flush_hook`——mini 把这些放公共 API 面，上游没全导出。纯 API 面差异 |

**为什么这么干净？** 记忆模块是**纯应用层逻辑**——它的输入（对话消息）和输出（memory.json + 注入串）都不依赖 Gateway/IM/auth。抽掉上层后，核心管线（抽取/去抖/存储/注入）靠**抽象解耦**（`MemoryStorage` ABC、纯函数过滤、延迟导入防循环），底层零改动。这与 [user_context.md](user_context.md)、[serialization.md](serialization.md)、[run_journal.md](run_journal.md) 是同一类「砍 Gateway 一行不改」的忠实移植。

**唯一实质差异**：mini 的 `__init__.py` 公共 API 面更宽（多导出 back-compat 包装 + CRUD 函数），以及几处等价改写（CJK 字面量、type hint 引号、init 提前）——都是教学版的可读性偏好，**无行为差异**。

---

## §11 常见问题 / 排错

**Q：为什么记忆更新用同步 `model.invoke()` 而不是 async？**
A：防跨事件循环连接复用（上游 issue #2615）。langchain 的 async httpx 客户端池是全局缓存且与 lead agent 共享的。在记忆更新里起第二个事件循环跑 `ainvoke` 会跨循环复用连接→炸。同步 `invoke` 用独立同步连接池，不碰 async 池。在事件循环里被调时用专用 `ThreadPoolExecutor` 卸载（但仍跑同步 invoke）。

**Q：用户连发 5 条消息，会调 5 次 LLM 抽取吗？**
A：不会。去抖队列把同一 `(thread, user, agent)` 的多次入队**合并**成一条，等 30s 静默后统一处理一次。correction/reinforcement 标志取「或」。

**Q：为什么 `user_id` 要在入队时捕获，不能在 Timer 回调里取？**
A：`threading.Timer` 在另一线程触发，`ContextVar`（user_id 载体）**不跨裸线程传播**。回调里取会拿到默认值 `"default"`，把所有人的记忆写进 default 桶。入队时（请求上下文活着）显式存进 `ConversationContext` 才对（见 §9.2）。

**Q：tiktoken 首次用很慢 / 卡住怎么办？**
A：tiktoken 首次要从公共网络下载 BPE 数据，网络受限环境可阻塞数十分钟。两个办法：① 设 `memory.token_counting: char` 完全跳过 tiktoken（无网络 CJK 感知估算）；② 失败有 600s 冷却降级，期间走字符估算，冷却后自愈。生产建议启动时 `warm_tiktoken_cache()` 预热。

**Q：记忆文件写一半崩溃会丢数据吗？**
A：不会。原子写先写 `memory.<uuid>.tmp` 再 `replace` 成 `memory.json`。`replace` 在 POSIX 上原子——要么旧完整要么新完整，无中间态。损坏的 JSON 读时回退空结构（不抛）。

**Q：上传的文件会被记进长期记忆吗？**
A：不会。上传文件是 session 级的。`_strip_upload_mentions_from_memory` 用收窄正则从摘要和 fact 里移除上传相关句子（但故意不误删「works with CSV files」这类合法 fact）。`filter_messages_for_memory` 也会跳过纯上传消息及其紧跟的 AI 回复。

**Q：记忆会无限增长吗？**
A：不会。`max_facts`（默认 100）上限，超了按置信度降序留 top。注入也有 `max_injection_tokens`（默认 2000）预算截断。

**Q：correction fact 会在 token 预算紧张时被优先保底注入吗？**
A：会。guaranteed 注入：`correction`（及 `guaranteed_categories` 配的类别）从独立的 `guaranteed_token_budget`（默认 500）先选、放 Facts 块最前；超预算时 Facts 块作受保护后缀，只截前面的段。配置在 `memory.guaranteed_categories` / `guaranteed_token_budget`，`format_memory_for_injection` + `lead_agent/prompt._get_memory_context` 全链已接通。

**Q：`hide_from_ui` 的消息会进记忆吗？**
A：不会。`filter_messages_for_memory` 直接跳过带 `hide_from_ui` 的 human 消息（TodoMiddleware 的待办提醒、ViewImageMiddleware、DynamicContextMiddleware 的 `__memory` 载荷等框架内部文本）——否则会污染长期记忆，`__memory` 还可能自放大。

**Q：`_get_memory_context` 出错会让 agent 起不来吗？**
A：不会。它吞掉所有异常返回 `""`（记忆是 nice-to-have，不能让它挂起 agent 启动）。`DynamicContextMiddleware` 的注入也有 5s 超时降级（`asyncio.to_thread` + `wait_for`）——tiktoken 卡住时跳过注入而非挂起请求。

**Q：摘要把旧消息压成摘要后，里面的细节会丢吗？**
A：不会丢。`memory_flush_hook` 挂在 SummarizationMiddleware 的 `before_summarization`，在消息被删前用 `queue.add_nowait` 抢拍进记忆队列，让 LLM 抽取成 fact 存进 `memory.json`。

**Q：Alice 的自定义 agent 记忆会影响 Bob 吗？**
A：不会。per-user + per-agent 隔离：Alice 的 `code-reviewer` 记忆在 `users/alice/agents/code-reviewer/memory.json`，Bob 的在 `users/bob/agents/code-reviewer/`，完全分开。
