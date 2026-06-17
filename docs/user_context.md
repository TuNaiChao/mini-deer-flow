# 5. user_context.md — 用户上下文（三态 user_id）

> 对应模块：**M3**（Phase 0，地基）
> 源码：`backend/packages/harness/deerflow/runtime/user_context.py`、`runtime/__init__.py`

---

## 1. 一句话定位

**user_context 是「当前这条请求 / 这次任务，是哪个用户发起的」的单一真相源**。它把「当前用户」存在一个特殊的容器里（ContextVar），让 memory、persistence、sandbox 这些需要「按用户分桶」的模块**不用层层透传 `user_id` 参数**就能拿到当前用户。

> 这是**多用户隔离的地基**：没有它，A 用户的记忆可能写进 B 用户的文件、A 的沙箱可能看到 B 的数据。有了它，所有「用户级」数据天然按当前用户分开。

---

## 2. 为什么需要它

### 2.1 痛点：`user_id` 参数会传染

假设没有 user_context，每个需要「按用户存数据」的函数都得带个 `user_id` 参数：

```python
def save_memory(text, user_id): ...
def load_memory(user_id): ...
def acquire_sandbox(thread_id, user_id): ...
def write_artifact(path, user_id): ...
```

调用链一深，`user_id` 就要**一层层手动传**，漏传一处就是跨用户数据泄漏。而且 `user_id` 从哪来（HTTP 请求头？配置？硬编码？）每个入口都不一样。

user_context 把「当前用户」放进一个**请求级容器**，任何函数直接 `get_effective_user_id()` 就能拿到，不用透传。

### 2.2 痛点：无鉴权时也得能跑

本地开发 / 测试 / 迁移脚本里没有「登录用户」，但代码还要能跑。user_context 提供 `DEFAULT_USER_ID = "default"` 兜底——没人登录时，所有数据归到 `default` 这个虚拟用户名下，不崩。

---

## 3. 核心概念（先把名词讲明白）

> 这节讲的概念（ContextVar、并发隔离、Protocol）会贯穿后续 memory / persistence / sandbox 模块，务必先读懂。

### 3.1 什么是「用户隔离」？

DeerFlow 会给每个用户存一堆数据：记忆（memory.json）、对话线程、沙箱工作目录、上传的文件……这些**必须按用户分开**——A 用户绝对不能看到 B 用户的记忆。物理上靠「路径里带 user_id」实现：

```
backend/.deer-flow/users/
  ├── user-A/memory.json        ← A 的记忆
  ├── user-A/threads/...        ← A 的对话
  └── user-B/memory.json        ← B 的记忆（A 看不到）
```

user_context 的职责就是：**告诉这些模块「当前是哪个 user_id」**，从而拼出正确的路径。

### 3.2 什么是 ContextVar？（关键概念）

**ContextVar**（上下文变量）是 Python 标准库提供的「**任务级**变量容器」。你可以把它理解成：

> 一个「**当前执行流专属**的盒子」。每个并发的执行流（asyncio 里叫 task）有自己独立的盒子，互不干扰。

打个比方——它像「**每个客服电话工位上各自贴的一张便签**」：

- 客服 A 的工位便签写着「当前客户=张三」，客服 B 的工位便签写着「当前客户=李四」；
- A 处理张三的事时只看自己工位的便签，不会被 B 干扰；
- 这张便签就是 ContextVar，**每个工位（task）一份**。

**关键区别**：

| 容器类型 | 作用范围 | 多并发会串吗？ |
|----------|----------|----------------|
| 全局变量 `user_id = "x"` | 整个进程 | ❌ 会！A 改了 B 也看到 |
| 线程局部变量（thread-local） | 每个线程 | asyncio 单线程多 task 会串 |
| **ContextVar** | 每个 task（更细） | ✅ 不会，task 间隔离 |

DeerFlow 用 asyncio（单线程并发），一个线程里同时跑多个用户的请求。**只有 ContextVar 能做到「每个请求 task 各自一份用户」**。所以 user_context 必须用 ContextVar。

### 3.3 ⚠️ 大坑：ContextVar **不自动**跨「真正的线程」

这是 deer 真实踩过的坑（issue #2615 类问题），务必记住：

> ContextVar 在 **asyncio task** 之间自动隔离/继承，但在**手动开的新线程**（如 `threading.Timer`、`ThreadPoolExecutor`）里**默认拿不到**。

什么意思？memory 更新用的是后台定时器（`threading.Timer`，真线程）：

```python
# 请求 task 里（ContextVar 有 user-A）
queue.add(conversation)
# ↓ 30 秒后，Timer 线程触发
Timer(30, process_in_background).start()
# ↓ Timer 线程里 ContextVar 是空的！不知道 user 是谁
def process_in_background():
    get_effective_user_id()  # → "default"（错了！应该是 user-A）
```

**解决办法**：在**加入队列的那一刻**就把 `user_id` **显式抄出来**（`user_id = get_effective_user_id()`），存进队列数据里，后台线程直接用这个抄出来的值，不再依赖 ContextVar。这正是 `MemoryMiddleware`（M13）要做的事。

> 一句话：**ContextVar 只在「asyncio task」这个尺度可靠；一旦跨到真正的线程，必须显式传递 user_id。**

### 3.4 什么是 Protocol（鸭子类型）？

`CurrentUser` 被定义成一个 `Protocol`：它声明「我需要一个有 `.id` 属性的对象」，但**不规定这个对象的具体类型**。任何带 `.id` 属性的东西（测试用的 `SimpleNamespace(id=...)`、未来的真实 `User` 类）都「长得像」CurrentUser，就能用。

这叫**鸭子类型**（「走起来像鸭子、叫起来像鸭子，那就是鸭子」）。好处：user_context 这个底层模块**不需要 import** 上层的真实 `User` 类，避免了循环依赖。mini 没有独立的 app 层，所以测试里用 `SimpleNamespace` 当用户即可。

### 3.5 三态 `user_id`：AUTO / str / None

仓库方法（persistence 等）的 `user_id` 参数有**三种取值**，驱动不同行为：

| 取值 | 含义 | 行为 |
|------|------|------|
| `AUTO`（哨兵，默认） | 「我没指定，你自己从 contextvar 查」 | 查 contextvar；查不到就 **raise**（逼你显式处理） |
| 显式字符串 `"user-1"` | 「就用这个」 | 直接用，覆盖 contextvar |
| `None` | 「我**故意**不要隔离」 | 不加 user_id 过滤（迁移脚本 / 管理 CLI 用） |

为什么 `AUTO` 查不到要 raise 而不是回退 default？因为这是**仓库层**——如果默默回退 default，A 用户的数据可能被错误归到 default 名下，是个**隐蔽的数据泄漏 bug**。宁可报错让你发现。而 `get_effective_user_id()`（文件路径用）则回退 default，因为路径总得有个值。

---

## 4. 设计原理（讲清楚每个「为什么」）

### 4.1 为什么用 ContextVar 而非 thread-local？

见 §3.2 表格。asyncio 单线程多 task，thread-local 会让同一个线程里的多个用户请求**共用一个值**，互相覆盖。ContextVar 是 task 级，天然隔离。

### 4.2 `resolve_runtime_user_id` 的三优先级为什么这么排？

工具 / 中间件取 user_id 时，有三个来源，按权威性从高到低：

```
1. runtime.context["user_id"]   ← 最高：由集成层从鉴权后写入，能跨 contextvar 丢失的边界
2. _current_user ContextVar     ← 中：请求入口写入，task 内可靠
3. DEFAULT_USER_ID "default"    ← 兜底：无鉴权时不崩
```

为什么 `runtime.context` 优先级最高？因为有些场景 contextvar **会丢失**（见 §3.3 的真线程问题、未来的跨进程驱动）。`runtime.context` 是显式注入到运行时对象里的，**更不容易丢**。所以工具持久化用户数据时，调 `resolve_runtime_user_id(runtime)` 而非直接 `get_effective_user_id()`，多一层保护。

### 4.3 为什么所有 user_id 在边界处都 `str()` 化？（红线 #10）

真实系统里 `User.id` 可能是 `UUID` 对象（如 `UUID("12345678-...")`）。但持久层把 `user_id` 存成数据库的 `VARCHAR` 列，而 SQLite 的异步驱动 **无法把原生 UUID 绑定到 VARCHAR 列**（会报 `type 'UUID' is not supported`）。

所以在「**离开 Python、进入存储/序列化**」的边界，user_context 统一 `str(user.id)` 转成字符串。这样调用方不用担心类型，也不用每个调用点自己转。

### 4.4 `_AutoSentinel` 为什么用单例哨兵而不是 `None` 当默认？

如果用 `None` 当 `user_id` 的默认值，就和「显式 None（故意不隔离）」**撞车**了——分不清「用户没传」和「用户故意传 None」。所以用一个**独特的哨兵对象** `AUTO` 当默认值，它和任何真实值都不相等，能精确区分三态。哨兵用 `__new__` 做成单例，保证 `AUTO is AUTO` 全进程唯一。

### 4.5 为什么 `require_current_user` raise，`get_effective_user_id` 回退？

两个函数服务不同场景：

- **`require_current_user()`**：仓库层用。没用户说明**调用链有 bug**（该鉴权的地方没鉴权），必须 raise 暴露问题，不能默默吞掉。
- **`get_effective_user_id()`**：拼文件路径用。路径总得有个 user_id 才能写盘，没用户时回退 `default` 让程序能继续跑（本地开发 / 测试场景）。

**「严格」用在数据安全（raise 暴露泄漏），「宽容」用在可用性（default 保证不崩）**——这是有意的权衡。

---

## 5. 文件结构

```
runtime/
├── __init__.py         # 导出 user_context 的全部公开接口（方便 from deerflow.runtime import ...）
└── user_context.py     # 本模块主体
```

> `runtime/` 包后续 Phase 会陆续加入 checkpointer / events / journal / stream_bridge / serialization / runs 等运行时组件，user_context 是其中最先落地的一个。

---

## 6. 关键接口 / 签名

### 读写当前用户

```python
CurrentUser  # Protocol：任何带 .id: str 的对象（鸭子类型）

def set_current_user(user: CurrentUser) -> Token
    # 为当前 task 设置用户；返回 token，用完在 finally 里 reset_current_user(token)

def reset_current_user(token: Token) -> None
    # 恢复到 token 捕获时的旧用户

def get_current_user() -> CurrentUser | None
    # 当前用户；未设置返回 None（安全，不抛错）

def require_current_user() -> CurrentUser
    # 当前用户；未设置 raise RuntimeError（严格）
```

### 取有效 user_id

```python
DEFAULT_USER_ID = "default"

def get_effective_user_id() -> str
    # 当前用户 id 的 str；未设置返回 "default"（不抛错，文件路径用）

def resolve_runtime_user_id(runtime: object | None) -> str
    # 单一真相源：runtime.context["user_id"] > contextvar > "default"（工具用）
```

### 三态哨兵

```python
AUTO  # 单例哨兵，表示「从 contextvar 解析」

def resolve_user_id(value: str | None | AUTO, *, method_name="...") -> str | None
    # AUTO → 查 contextvar（无则 raise）；str → 原样；None → None（不隔离）
```

---

## 7. 应用方法

### 7.1 集成层 / 测试：设置当前用户

```python
from deerflow.runtime import set_current_user, reset_current_user
from types import SimpleNamespace

token = set_current_user(SimpleNamespace(id="user-A"))
try:
    # 这一整段执行流里，get_effective_user_id() 都返回 "user-A"
    do_work()
finally:
    reset_current_user(token)   # 恢复，别漏
```

### 7.2 业务模块：读取当前用户

```python
from deerflow.runtime import get_effective_user_id

# 拼用户级路径（无用户也安全，回退 default）
user_id = get_effective_user_id()
memory_path = base_dir / "users" / user_id / "memory.json"
```

### 7.3 工具 / 中间件：用 runtime 真相源

```python
from deerflow.runtime import resolve_runtime_user_id

def my_tool(runtime):
    user_id = resolve_runtime_user_id(runtime)
    # 优先 runtime.context["user_id"]，再 contextvar，再 default
    save_per_user(user_id, ...)
```

### 7.4 仓库方法：三态形参

```python
from deerflow.runtime import AUTO, resolve_user_id

def list_items(self, *, user_id: str | None = AUTO):
    resolved = resolve_user_id(user_id, method_name="list_items")
    if resolved is None:
        # 不加 user_id 过滤（迁移 / CLI 场景）
        query = select(Item)
    else:
        query = select(Item).where(Item.user_id == resolved)
```

### 7.5 后台线程：显式抄出 user_id（避免 §3.3 的坑）

```python
from deerflow.runtime import get_effective_user_id

# ❌ 错误：Timer 线程里 contextvar 已丢失
Timer(30, lambda: save(get_effective_user_id())).start()

# ✅ 正确：请求时抄出，后台线程用抄出来的值
captured_uid = get_effective_user_id()
Timer(30, lambda: save(captured_uid)).start()
```

---

## 8. 与其它模块的关系

```
runtime/user_context.py（本模块，ContextVar 持有当前用户）
   │
   ├─→ 谁来「写」(set_current_user)：
   │     • conftest 的 autouse fixture（测试，注入 test-user-autouse）
   │     • 未来集成层 lifespan / 入口（Phase 8）
   │
   └─→ 谁来「读」(get_effective_user_id / resolve_runtime_user_id)：
         • persistence (M4)：run / thread_meta 按 user_id 存
         • memory (M13)：记忆按 user_id 分文件（+ 后台线程显式抄出）
         • sandbox (M10)：沙箱目录按 user_id 隔离
         • checkpointer (M5)：checkpoint 按 user_id 分
```

- **依赖**：仅 langgraph（`runtime.context` 的来源；当前为鸭子类型读取，不强依赖）。
- **被依赖**：所有需要「按用户隔离」的持久化 / 记忆 / 沙箱模块。
- **配套**：conftest 的 `_auto_user_context` autouse fixture 已软加载本模块（M3 落地前 ImportError 跳过，落地后自动给每个测试注入默认用户）。

> 这就是为什么 user_context 排在 **Phase 0 地基**——它是用户隔离的根，persistence / memory / sandbox 都依赖它，必须先于它们就位。

---

## 9. 常见问题 / 排错

### Q1：后台任务里 `get_effective_user_id()` 返回 `"default"`，但当前明明是 user-A

你踩了 §3.3 的坑：后台任务跑在**真正的线程**（Timer / 线程池）里，contextvar 没带过去。解决办法：在**入队时**就把 user_id 显式抄出来，后台线程用抄出来的值。memory 队列（M13）就是这么做的。

### Q2：测试报 `RuntimeError: repository accessed without user context`

你在仓库方法里用了 `require_current_user()` 或 `resolve_user_id(AUTO)`，但当前测试没设用户。两个办法：
- 给测试加默认用户：依赖 conftest 的 autouse（默认每个测试有 test-user-autouse）；
- 或测试内显式 `set_current_user(...)`（记得 try/finally reset）；
- 或仓库调用时显式传 `user_id="..."` / `user_id=None`。

### Q3：`isinstance(my_obj, CurrentUser)` 报错或总返回 False

`CurrentUser` 是 `@runtime_checkable` Protocol，`isinstance` 只检查**属性是否存在**，不检查类型。确保对象有 `.id` 属性。注意：`runtime_checkable` Protocol 的 `isinstance` 只验证属性**存在**（旧 Python 版本不验证方法签名）。

### Q4：`User.id` 是 UUID，存数据库报 `type 'UUID' is not supported`

你没在边界 `str()` 化。user_context 的 `get_effective_user_id` / `resolve_user_id` / `resolve_runtime_user_id` **都已经**对返回值 `str()`。如果你绕过它们直接用 `current_user.id`，自己补 `str()`（红线 #10）。

### Q5：`reset_current_user(token)` 报 `ValueError`

`token` 不是最近一次 `set_current_user` 返回的 token（嵌套 set 后必须按**相反顺序** reset）。检查是否有 set 后没 reset、或 reset 顺序错了。本模块测试用 try/finally 保证配对。

---

## 小结

user_context 的精髓是「**用 ContextVar 做 task 级用户隔离的单一真相源**」。记住四件事：

1. **ContextVar 是 task 级**：asyncio 多请求天然隔离，但**不跨真正的线程**（后台任务必须显式抄出 user_id）。
2. **三态 `user_id`**：AUTO（查 contextvar，无则 raise）/ str（覆盖）/ None（不隔离）——哨兵 `AUTO` 避免和 None 撞车。
3. **`resolve_runtime_user_id` 三优先级**：`runtime.context` > contextvar > `default`，跨边界也不丢用户。
4. **边界 `str()` 化**：UUID 不能直接进 VARCHAR，user_context 出口统一转字符串（红线 #10）。

上一个文档：`docs/utils.md`（时间与消息工具）。下一个要读的文档：`docs/config.md`（配置类型化）。
