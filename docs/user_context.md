# 5. user_context.md — 用户上下文（三态 user_id，用户隔离基石）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（函数 / 行号以此为准）。

> **一句话定位**：user_context 是「**当前这条请求 / 这次任务，是哪个用户发起的**」的单一真相源。它把「当前用户」存在一个特殊的容器里（ContextVar），让 memory、persistence、sandbox 这些需要「按用户分桶」的模块**不用层层透传 `user_id` 参数**就能拿到当前用户。这是**多用户隔离的地基**：没有它，A 用户的记忆可能写进 B 用户的文件。

> 配套代码：[runtime/user_context.py](../backend/packages/harness/deerflow/runtime/user_context.py) · 导出经 [runtime/__init__.py](../backend/packages/harness/deerflow/runtime/__init__.py)。配套测试：conftest 的 `_auto_user_context` autouse fixture（[test/conftest.py:113](../test/conftest.py#L113)）。

## 学完这篇你能回答什么（learning outcomes）

- **多用户隔离**怎么实现？为什么用 **ContextVar**（task 级）而不是全局变量 / thread-local？
- ⚠️ **ContextVar 不自动跨「真正的线程」**——后台任务（Timer / 线程池）里为什么会丢用户？怎么修（入队时显式抄出）？
- 仓库方法的 `user_id` 形参为什么是**三态**（`AUTO` / `str` / `None`）？为什么 `AUTO` 查不到 contextvar 要 **raise** 而不是回退 default？
- 为什么 `User.id` 是 UUID 时，在边界必须 `str()` 化（SQLite 驱动不支持 UUID 绑定 VARCHAR）？
- 工具 / 中间件取 user_id 为什么走 **`resolve_runtime_user_id`** 三优先级（`runtime.context` > contextvar > default）？

> 这些都是后端 / agent 工程面试的高频点——「并发下的用户隔离」「ContextVar vs thread-local」「跨线程上下文传递」。

---

## 1. 为什么需要它

### 1.1 痛点：`user_id` 参数会传染

假设没有 user_context，每个需要「按用户存数据」的函数都得带个 `user_id` 参数：

```python
def save_memory(text, user_id): ...
def load_memory(user_id): ...
def acquire_sandbox(thread_id, user_id): ...
```

调用链一深，`user_id` 就要**一层层手动传**，漏传一处就是跨用户数据泄漏。而且 `user_id` 从哪来（HTTP 请求头？配置？硬编码？）每个入口都不一样。

user_context 把「当前用户」放进一个**请求级容器**，任何函数直接 `get_effective_user_id()` 就能拿到，不用透传。

### 1.2 痛点：无鉴权时也得能跑

本地开发 / 测试 / 迁移脚本里没有「登录用户」，但代码还要能跑。user_context 提供 `DEFAULT_USER_ID = "default"` 兜底（[第 85 行](../backend/packages/harness/deerflow/runtime/user_context.py#L85)）——没人登录时，所有数据归到 `default` 这个虚拟用户名下，不崩。

---

## 2. 零基础先读：这些名词是什么

> 这节讲的概念（ContextVar、并发隔离、Protocol）会贯穿后续 memory / persistence / sandbox 模块，务必先读懂。

### 2.1 什么是「用户隔离」？

mini 会给每个用户存一堆数据：记忆（memory.json）、对话线程、沙箱工作目录、上传的文件……这些**必须按用户分开**——A 用户绝对不能看到 B 用户的记忆。物理上靠「路径里带 user_id」实现（布局见 [config.md §4.5 Paths](config.md#45-paths路径解析替代-runtime_paths)）：

```
.deer-flow/users/
  ├── user-A/memory.json        ← A 的记忆
  ├── user-A/threads/...        ← A 的对话
  └── user-B/memory.json        ← B 的记忆（A 看不到）
```

user_context 的职责就是：**告诉这些模块「当前是哪个 user_id」**，从而拼出正确的路径。

### 2.2 什么是 ContextVar？（关键概念）

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

mini 用 asyncio（单线程并发），一个线程里同时跑多个用户的请求。**只有 ContextVar 能做到「每个请求 task 各自一份用户」**。所以 user_context 必须用 ContextVar（[第 44 行](../backend/packages/harness/deerflow/runtime/user_context.py#L44)）。

### 2.3 ⚠️ 大坑：ContextVar **不自动**跨「真正的线程」

这是真实踩过的坑（后台 memory 更新场景），务必记住：

> ContextVar 在 **asyncio task** 之间自动隔离/继承（`asyncio.create_task` / `asyncio.to_thread` 会**继承**父任务上下文），但在**手动开的新线程**（如 `threading.Timer`）里**默认拿不到**。

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

**解决办法**：在**加入队列的那一刻**就把 `user_id` **显式抄出来**（`user_id = get_effective_user_id()`），存进队列数据里，后台线程直接用这个抄出来的值，不再依赖 ContextVar。这正是 memory 模块（→ #18）要做的事。

> 一句话：**ContextVar 只在「asyncio task」这个尺度可靠；一旦跨到真正的线程，必须显式传递 user_id。**

### 2.4 什么是 Protocol（鸭子类型）？

`CurrentUser` 被定义成一个 `Protocol`（[第 34 行](../backend/packages/harness/deerflow/runtime/user_context.py#L34)）：它声明「我需要一个有 `.id` 属性的对象」，但**不规定这个对象的具体类型**。任何带 `.id` 属性的东西（测试用的 `SimpleNamespace(id=...)`、未来的真实 `User` 类）都「长得像」CurrentUser，就能用。

这叫**鸭子类型**（「走起来像鸭子、叫起来像鸭子，那就是鸭子」）。好处：user_context 这个底层模块**不需要 import** 上层的真实 `User` 类，避免了循环依赖。mini 没有独立的 app 层，所以测试里用 `SimpleNamespace` 当用户即可。

### 2.5 三态 `user_id`：AUTO / str / None

仓库方法（persistence 等）的 `user_id` 参数有**三种取值**，驱动不同行为：

| 取值 | 含义 | 行为 |
|------|------|------|
| `AUTO`（哨兵，默认） | 「我没指定，你自己从 contextvar 查」 | 查 contextvar；查不到就 **raise**（逼你显式处理） |
| 显式字符串 `"user-1"` | 「就用这个」 | 直接用，覆盖 contextvar |
| `None` | 「我**故意**不要隔离」 | 不加 user_id 过滤（迁移脚本 / 管理 CLI 用） |

为什么 `AUTO` 查不到要 raise 而不是回退 default？因为这是**仓库层**——如果默默回退 default，A 用户的数据可能被错误归到 default 名下，是个**隐蔽的数据泄漏 bug**。宁可报错让你发现。而 `get_effective_user_id()`（文件路径用）则回退 default，因为路径总得有个值。

---

## 3. 整体结构：它在系统里的位置

user_context 是**用户隔离的根**，被所有「按用户分桶」的模块读取：

```
runtime/user_context.py（ContextVar 持有当前用户）
   │
   ├─→ 谁「写」(set_current_user)：
   │     • conftest autouse fixture（测试，注入 test-user-autouse）
   │     • 集成层 lifespan / 入口（鉴权成功后写入，→ #28 architecture）
   │
   └─→ 谁「读」(get_effective_user_id / resolve_runtime_user_id / resolve_user_id)：
         • persistence（→ #7）：run / thread_meta 按 user_id 存
         • memory（→ #18）：记忆按 user_id 分文件（+ 后台线程显式抄出）
         • sandbox（→ #13）：沙箱目录按 user_id 隔离
         • checkpointer（→ #8）：checkpoint 按 user_id 分
         • tools / middlewares（→ #22 / #24）：持久化用户级状态走 resolve_runtime_user_id
```

它排在 **Phase 0 地基**，是用户隔离的根，persistence / memory / sandbox 都依赖它，必须先于它们就位。

---

## 4. 代码走读：重要函数逐个讲

### 4.1 读写当前用户（[第 47–78 行](../backend/packages/harness/deerflow/runtime/user_context.py#L47)）

```python
def set_current_user(user: CurrentUser) -> Token:        # 为当前 task 设置用户，返回 reset token
    return _current_user.set(user)

def reset_current_user(token: Token) -> None:            # 恢复到 token 捕获时的旧用户
    _current_user.reset(token)

def get_current_user() -> CurrentUser | None:            # 安全：未设置返回 None
    return _current_user.get()

def require_current_user() -> CurrentUser:               # 严格：未设置 raise RuntimeError
    user = _current_user.get()
    if user is None:
        raise RuntimeError("repository accessed without user context")
    return user
```

`set` / `reset` 成对用（`try/finally`），`get`（宽容）/ `require`（严格）服务不同场景（§5.4）。

### 4.2 取有效 user_id 的三个函数

三个「取 user_id」函数，按**严格度**和**用途**分：

| 函数 | 行号 | 用途 | 无用户时 |
|------|------|------|----------|
| `get_effective_user_id()` | [第 88 行](../backend/packages/harness/deerflow/runtime/user_context.py#L88) | 拼文件路径 | 回退 `"default"`（不抛错） |
| `resolve_runtime_user_id(runtime)` | [第 100 行](../backend/packages/harness/deerflow/runtime/user_context.py#L100) | 工具 / 中间件持久化用户数据 | 三优先级兜底到 `"default"` |
| `resolve_user_id(value, ...)` | [第 148 行](../backend/packages/harness/deerflow/runtime/user_context.py#L148) | 仓库方法的三态形参 | `AUTO` 无用户时 **raise** |

`resolve_runtime_user_id` 的三优先级（[第 115–120 行](../backend/packages/harness/deerflow/runtime/user_context.py#L115)）：

```python
def resolve_runtime_user_id(runtime: object | None) -> str:
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        ctx_user_id = context.get("user_id")      # ① runtime.context["user_id"]（最高，跨边界不丢）
        if ctx_user_id:
            return str(ctx_user_id)
    return get_effective_user_id()                 # ② contextvar → ③ "default"
```

### 4.3 三态哨兵 `AUTO` + `resolve_user_id`（[第 131–170 行](../backend/packages/harness/deerflow/runtime/user_context.py#L131)）

```python
class _AutoSentinel:           # 单例哨兵，__repr__ 是 "<AUTO>"
    ...

AUTO: Final[_AutoSentinel] = _AutoSentinel()

def resolve_user_id(value, *, method_name="repository method") -> str | None:
    if isinstance(value, _AutoSentinel):          # AUTO → 查 contextvar
        user = _current_user.get()
        if user is None:
            raise RuntimeError(f"{method_name} called with user_id=AUTO but no user context ...")
        return str(user.id)                       # ⚠ 边界 str() 化（UUID → str，见 §5.3）
    return value                                  # str → 原样；None → None（不隔离）
```

---

## 5. 设计权衡与踩坑

### 5.1 为什么用 ContextVar 而非 thread-local？

见 §2.2 表格。asyncio 单线程多 task，thread-local 会让同一个线程里的多个用户请求**共用一个值**，互相覆盖。ContextVar 是 task 级，天然隔离。

### 5.2 `resolve_runtime_user_id` 的三优先级为什么这么排？

```
1. runtime.context["user_id"]   ← 最高：由集成层从鉴权后写入，能跨 contextvar 丢失的边界
2. _current_user ContextVar     ← 中：请求入口写入，task 内可靠
3. DEFAULT_USER_ID "default"    ← 兜底：无鉴权时不崩
```

为什么 `runtime.context` 优先级最高？因为有些场景 contextvar **会丢失**（§2.3 的真线程问题、未来的跨进程驱动）。`runtime.context` 是显式注入到运行时对象里的，**更不容易丢**。所以工具持久化用户数据时，调 `resolve_runtime_user_id(runtime)` 而非直接 `get_effective_user_id()`，多一层保护。

### 5.3 为什么所有 user_id 在边界处都 `str()` 化？

真实系统里 `User.id` 可能是 `UUID` 对象（如 `UUID("12345678-...")`）。但持久层把 `user_id` 存成数据库的 `VARCHAR` 列，而 SQLite 的异步驱动 **无法把原生 UUID 绑定到 VARCHAR 列**（会报 `type 'UUID' is not supported`，[第 166–169 行注释](../backend/packages/harness/deerflow/runtime/user_context.py#L166)）。

所以在「**离开 Python、进入存储 / 序列化**」的边界，user_context 统一 `str(user.id)` 转成字符串（`resolve_user_id` / `resolve_runtime_user_id` 出口都做了）。调用方不用担心类型，也不用每个调用点自己转。

### 5.4 `_AutoSentinel` 为什么用单例哨兵而不是 `None` 当默认？

如果用 `None` 当 `user_id` 的默认值，就和「显式 None（故意不隔离）」**撞车**了——分不清「用户没传」和「用户故意传 None」。所以用一个**独特的哨兵对象** `AUTO` 当默认值，它和任何真实值都不相等，能精确区分三态。哨兵用 `__new__` 做成单例（[第 134–139 行](../backend/packages/harness/deerflow/runtime/user_context.py#L134)），保证 `AUTO is AUTO` 全进程唯一。

### 5.5 为什么 `require_current_user` raise，`get_effective_user_id` 回退？

- **`require_current_user()`**：仓库层用。没用户说明**调用链有 bug**（该鉴权的地方没鉴权），必须 raise 暴露问题，不能默默吞掉。
- **`get_effective_user_id()`**：拼文件路径用。路径总得有个 user_id 才能写盘，没用户时回退 `default` 让程序能继续跑（本地开发 / 测试场景）。

**「严格」用在数据安全（raise 暴露泄漏），「宽容」用在可用性（default 保证不崩）**——这是有意的权衡。

> 内部追溯：本文的设计约束在上游工程记录里分别编号为红线 #10（边界 str() 化防 UUID→VARCHAR）、#2615（后台线程 ContextVar 丢失 → 显式抄出）。这些编号仅作内部对照，不影响理解。

---

## 6. 应用方法

### 6.1 集成层 / 测试：设置当前用户

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

### 6.2 业务模块：读取当前用户

```python
from deerflow.runtime import get_effective_user_id

user_id = get_effective_user_id()                          # 无用户也安全，回退 default
memory_path = base_dir / "users" / user_id / "memory.json"
```

### 6.3 工具 / 中间件：用 runtime 真相源

```python
from deerflow.runtime import resolve_runtime_user_id

def my_tool(runtime):
    user_id = resolve_runtime_user_id(runtime)             # runtime.context > contextvar > default
    save_per_user(user_id, ...)
```

### 6.4 仓库方法：三态形参

```python
from deerflow.runtime import AUTO, resolve_user_id

def list_items(self, *, user_id: str | None = AUTO):
    resolved = resolve_user_id(user_id, method_name="list_items")
    if resolved is None:                                    # None → 不加 user_id 过滤（迁移 / CLI）
        query = select(Item)
    else:
        query = select(Item).where(Item.user_id == resolved)
```

### 6.5 后台线程：显式抄出 user_id（避免 §2.3 的坑）

```python
from deerflow.runtime import get_effective_user_id

# ❌ 错误：Timer 线程里 contextvar 已丢失
Timer(30, lambda: save(get_effective_user_id())).start()

# ✅ 正确：请求时抄出，后台线程用抄出来的值
captured_uid = get_effective_user_id()
Timer(30, lambda: save(captured_uid)).start()
```

---

## 7. 常见问题 / 排错

**Q: 后台任务里 `get_effective_user_id()` 返回 `"default"`，但当前明明是 user-A？**
你踩了 §2.3 的坑：后台任务跑在**真正的线程**（Timer / 线程池）里，contextvar 没带过去。解决办法：在**入队时**就把 user_id 显式抄出来，后台线程用抄出来的值。memory 队列（→ #18）就是这么做的。

**Q: 测试报 `RuntimeError: repository accessed without user context`？**
你在仓库方法里用了 `require_current_user()` 或 `resolve_user_id(AUTO)`，但当前测试没设用户。三个办法：① 依赖 conftest 的 autouse（默认每个测试有 test-user-autouse）；② 测试内显式 `set_current_user(...)`（记得 try/finally reset）；③ 仓库调用时显式传 `user_id="..."` / `user_id=None`。

**Q: `isinstance(my_obj, CurrentUser)` 报错或总返回 False？**
`CurrentUser` 是 `@runtime_checkable` Protocol，`isinstance` 只检查**属性是否存在**（`.id`），不检查类型。确保对象有 `.id` 属性。

**Q: `User.id` 是 UUID，存数据库报 `type 'UUID' is not supported`？**
你没在边界 `str()` 化。user_context 的 `get_effective_user_id` / `resolve_user_id` / `resolve_runtime_user_id` **都已经**对返回值 `str()`（§5.3）。如果你绕过它们直接用 `current_user.id`，自己补 `str()`。

**Q: `reset_current_user(token)` 报 `ValueError`？**
`token` 不是最近一次 `set_current_user` 返回的 token（嵌套 set 后必须按**相反顺序** reset）。检查是否有 set 后没 reset、或 reset 顺序错了。本模块测试用 try/finally 保证配对。

---

## 小结

user_context 的精髓是「**用 ContextVar 做 task 级用户隔离的单一真相源**」。记四件事：

1. **ContextVar 是 task 级**：asyncio 多请求天然隔离，但**不跨真正的线程**（后台任务必须显式抄出 user_id，§2.3）。
2. **三态 `user_id`**：AUTO（查 contextvar，无则 raise）/ str（覆盖）/ None（不隔离）——哨兵 `AUTO` 避免和 None 撞车。
3. **`resolve_runtime_user_id` 三优先级**：`runtime.context` > contextvar > `default`，跨边界也不丢用户。
4. **边界 `str()` 化**：UUID 不能直接进 VARCHAR，user_context 出口统一转字符串（§5.3）。

> 🎉 **Phase 0 地基全部完成**（#1 build · #2 testing-setup · #3 config · #4 utils · #5 user_context）。下一步进入 **Phase 1**：[#6 models.md](models.md)（模型工厂）→ #7 persistence → #8 checkpointer …
