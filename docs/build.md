# 1. build.md — 工程化地基（依赖管理 + 测试 + lint + dev server）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（命令 / 文件路径 / 行号以此为准）。

> **一句话定位**：本篇不讲任何 agent 业务功能，只讲一件事——**怎么让「写完的代码能跑起来、能跑测试、能 lint、能起开发服务器」成立**。它是整个项目的地基：跳过它，后面所有模块都会撞上「代码写完了，但环境跑不起来」。

> 配套代码：[../backend/Makefile](../backend/Makefile) · [../backend/pyproject.toml](../backend/pyproject.toml) · [../backend/packages/harness/pyproject.toml](../backend/packages/harness/pyproject.toml) · [../ruff.toml](../ruff.toml) · [../pytest.ini](../pytest.ini) · [../test/conftest.py](../test/conftest.py) · [../test/support/detectors/blocking_io_runtime.py](../test/support/detectors/blocking_io_runtime.py) · [../test/blocking_io/conftest.py](../test/blocking_io/conftest.py) · [../test/test_harness_boundary.py](../test/test_harness_boundary.py)

## 学完这篇你能回答什么（learning outcomes）

- Python 项目的「工程化地基」通常包含哪几样（依赖管理 / 测试 / lint / dev server），各自解决什么痛点？
- 为什么一个**可发布的框架包**要和**应用层**物理隔离？怎么用测试把这条边界钉死（harness 边界）？
- **async 服务里为什么不能直接做同步阻塞 IO**（读写文件等）？怎么用一段测试把「必须用 `asyncio.to_thread` 卸载」这条约束变成**可断言的事实**（blocking-IO gate）？
- **可选依赖（extras + 软加载）** 解决了什么问题？和「全塞进核心依赖」相比有什么取舍？

> 这几条都是 agent / 后端工程面试的高频考点——尤其「async 里阻塞 IO」「依赖边界」。

---

## 1. 零基础先读：这些名词是什么

> 不熟悉 Python 工程化工具的话，先读这一节，再往下看。每条都用大白话讲。

### 测试（test）是什么

写完一段代码，怎么确认它「对」？最原始的办法是手动跑一遍、看输出。**测试**就是把「手动验证」写成一小段代码，交给机器自动跑。比如你写了个加法函数 `add(a, b)`，就配一条测试 `assert add(1, 2) == 3`。以后每次改代码，跑一遍全部测试，立刻知道有没有把原本好的功能改坏。

我们用 **pytest** 这个工具来组织和运行测试。`make test` = 跑全部测试，全过（俗称「全绿」）才算代码没问题。本项目有 **1700+ 条测试**。

### lint 是什么

**lint** 的英文原意是「衣服上的绒毛 / 毛絮」——在编程里引申为**代码里那些微小、不影响程序运行、但不干净的瑕疵**。

**lint 工具**就像「代码的拼写检查器」：它**不运行你的代码**，而是直接读代码文本，静态地挑毛病，例如：

- 定义了却从没用过的变量 / import（多半是写错了，或是该删的废代码）；
- `import` 语句顺序乱七八糟（影响别人阅读）；
- 一行写了 300 个字符（得左右拖滚动条才看得全）；
- 拼写错误、潜在的错误用法……

这些一般**不是 bug**（程序照样能跑），但会让代码难读、难协作，还可能藏着隐患——比如一个「没用的变量」，可能本该被用、只是名字拼错了。**整个团队统一跑 lint，等于大家写出来的代码风格一致、少踩坑。**

### ruff 是什么

**ruff** 是 Python 生态里目前最流行的「代码检查 + 格式化」工具（用 Rust 写的，所以特别快）。它干两件事：

- `ruff check` —— **检查**（也就是 lint）：扫出上面说的那些「瑕疵」，只报告，默认不改你的代码（除非你加 `--fix` 让它自动修能修的）。
- `ruff format` —— **格式化**：自动重排版（空格、换行、引号），让代码长得「标准」，省去人肉调格式。

我们的项目用 ruff 同时做这两件事。

### `make lint` 和 `make format` 的区别

- `make lint` = 只**检查**不改：`ruff check`（找问题）+ `ruff format --check`（检查格式是否达标）。**两步都过才算「0 lint」**——有人只验了 `ruff check` 就说「0 lint」，其实 `ruff format --check` 还可能没过。
- `make format` = 会**自动改**：`ruff check --fix`（自动修 import 排序等）+ `ruff format`（自动重排格式）。

一句话口诀：**lint 是体检（只看报告），format 是治疗（会动手改）**。日常写完代码先 `make format` 自动整理，再 `make lint` 确认全绿。

### Makefile / make 是什么

`uv run pytest ../test -v` 这种命令又长又难记。**Makefile** 就是「长命令的短名字清单」——写一行 `test: <长命令>`，以后只要敲 `make test` 就等于执行那条长命令。本项目的命令清单在 [../backend/Makefile](../backend/Makefile)。

### venv / uv / workspace 是什么

- **venv（虚拟环境）**：Python 的「隔离小房间」。每个项目用自己的 venv 装依赖，互不干扰——A 项目能用 langgraph 1.2，B 项目能用 1.0，各装各的。
- **uv**：一个极快的 Python 包管理器（替代老的 pip）。我们用它装依赖、跑命令：`uv sync` 装包、`uv run <命令>` 在项目环境里跑命令。
- **workspace（工作区）**：把多个相关的 Python 包放在一起统一管理。本项目 `backend/` 是 workspace 根，`packages/harness`（框架包 `deerflow-harness`）是成员。`uv sync` 一次性把根和成员都装好。

### dev server / `langgraph dev` 是什么

**dev server（开发服务器）**：在本地起一个「能运行你的 agent、还带调试界面」的服务。mini 用 `langgraph dev` 这个命令启动——它会读 [../backend/langgraph.json](../backend/langgraph.json)，把里面的 `make_lead_agent` 图加载起来，并给你一个浏览器界面（LangGraph Studio）让你发消息调试。详见 [start-here.md](start-here.md) §4。

### extras / 软加载（soft import）是什么

- **extras（可选依赖）**：把「不是所有人都需要」的重依赖（postgres 驱动、MCP 适配器、tiktoken…）拆成命名的可选组。默认不装，需要时 `uv sync --extra postgres`。
- **软加载（soft import）**：模块代码里对这些可选依赖一律 `try: import X except ImportError:`，**缺包时不崩溃**，而是回退到内存 / 默认实现，并打印一条「装哪个包就行」的提示。详见 §3 / §8。

---

读完这些，再往下看「为什么需要它」就顺畅了。

---

## 2. 整体结构：它在系统里的位置

build 这一层**不实现任何 agent 功能**，它是所有功能模块脚下的「地面」：

```
            ┌──────────────────────────────────────────┐
            │   build（工程化地基）= 本篇                │
            │  pyproject.toml × 2（根 + harness）       │
            │  + Makefile + ruff.toml + pytest.ini      │
            │  + test/conftest.py + blocking-IO gate    │
            └──────────────────────────────────────────┘
                 │  让「能装 / 能跑 / 能测 / 能 lint」成立
   ┌─────────────┼─────────────┬─────────────┬─────────────┐
   ▼             ▼             ▼             ▼             ▼
 config        models      persistence     agents       ... 所有模块
 (utils/                   (checkpointer/  (sandbox/
  user_context)             memory/...)    subagents/...)
                 │
                 ▼  每个功能模块落地时都要回头用到本篇：
                 · 新依赖 → 写进 extras + 软加载
                 · 新增同步阻塞 IO → 包 asyncio.to_thread + 在 test/blocking_io/ 加锚点测试
                 · 新代码 → 过 make lint
```

一句话：**build 是「让其它一切可运行 / 可验证」的约定与脚手架**。它由五块拼成：

| 块 | 文件 | 解决什么 |
|----|------|----------|
| 依赖声明 | [pyproject.toml](../backend/pyproject.toml) × 2 | 装什么、版本锁多少、哪些可选 |
| 命令快捷方式 | [Makefile](../backend/Makefile) | 一条短命令干一串长活 |
| lint 配置 | [ruff.toml](../ruff.toml) | 代码风格统一 |
| 测试配置 | [pytest.ini](../pytest.ini) + [test/conftest.py](../test/conftest.py) | 测试怎么收集、怎么隔离 |
| 两条「口头红线」的强制化 | [test/blocking_io/](../test/blocking_io/)（阻塞 IO gate）+ [test_harness_boundary.py](../test/test_harness_boundary.py)（harness 边界） | 把「不能怎样」变成测试 |

---

## 3. 核心概念

- **uv workspace**：[backend/pyproject.toml](../backend/pyproject.toml) 是 workspace 根（`[tool.uv.workspace] members = ["packages/harness"]`），`packages/harness` 是成员。`uv sync` 一次性把根 + 成员的依赖装进 venv，并把 `deerflow-harness` 以 editable 方式安装（改源码即时生效，理论上无需重装）。
- **extras（可选依赖）**：把重依赖拆成 `[project.optional-dependencies]` 里的命名组。默认不装，需要时 `uv sync --extra postgres`。见 §6 的 7 个 extras 表。
- **软加载（soft import）**：业务代码对可选依赖一律 `try/except ImportError`，缺包时回退到内存 / 默认实现 + 打印「可操作安装提示」。**extras 命名必须和 install hint 一致**，否则提示装错包。
- **harness 边界**：`packages/harness/deerflow/` 是可发布的框架包，**永远不得 `import app.*`**（不能依赖任何应用 / Gateway 代码）。由 [test_harness_boundary.py](../test/test_harness_boundary.py) 的 AST 扫描守这条线（§4.5）。
- **blocking-IO gate**：一段纯 Python 的检测上下文（无第三方库），在测试期把底层阻塞函数（`open`/`os.stat`/…）临时替换成「哨兵」，**当调用栈经过 `deerflow.*` 且正跑在事件循环里时**抛 `BlockingError`。把「同步阻塞 IO 不能发自业务代码跑在事件循环里」变成可断言的事实。实现见 §4.2。

---

## 4. 代码走读：重要函数逐个讲

### 4.1 Makefile —— 6 个 target

[../backend/Makefile](../backend/Makefile) 全部内容就是 6 个快捷命令，每个都显式带了 `PYTHONPATH=packages/harness`（原因见 §8 踩坑）：

| target | 实际跑的 | 干什么 |
|--------|----------|--------|
| `install` | `uv sync` | 装全部依赖 |
| `dev` | `uv run langgraph dev` | 起 dev server（读 langgraph.json） |
| `test` | `uv run pytest ../test -v` | 全量测试（test/ 在 backend 外，用 `../test`） |
| `test-blocking-io` | `uv run pytest ../test/blocking_io -q --tb=short` | 只跑阻塞 IO gate |
| `lint` | `ruff check ..` **+** `ruff format --check ..` | 两步检查（都过才算 0 lint） |
| `format` | `ruff check .. --fix` **+** `ruff format ..` | 自动修 |

> 注意 `lint` / `format` 的路径是 `..`（项目根）——因为 [ruff.toml](../ruff.toml) 在项目根，要同时覆盖 `backend/` 和 `test/`（§8.2）。

Makefile 顶部还有两大段**注释**，把两个最容易踩的环境坑写在最显眼处：① editable `.pth` 不稳 → 用 `PYTHONPATH` 兜；② 项目在 iCloud 同步目录 → venv 挪到 `~/.venvs/mini-deer-flow`。这两条在 [start-here.md](start-here.md) §4 / §6 也讲了。

### 4.2 blocking-IO gate —— [blocking_io_runtime.py](../test/support/detectors/blocking_io_runtime.py)

这是本篇**最值得读**的代码——它把一条「async 编程的铁律」变成了能跑的测试。整个文件 132 行，零第三方依赖。逐个看：

#### `BlockingError`（[第 36 行](../test/support/detectors/blocking_io_runtime.py#L36)）

```python
class BlockingError(RuntimeError):
    """在事件循环里、发自业务代码的同步阻塞 IO 调用。"""
```

gate 抛的异常类型。继承 `RuntimeError`（不是 `Exception` 的子类名 `BlockingIOError`——那是 builtin，别搞混）。

#### `_BLOCKING_TARGETS`（[第 49–64 行](../test/support/detectors/blocking_io_runtime.py#L49)）

被「下哨」的 14 个同步阻塞原语清单：`builtins.open`、`os.stat/lstat/fstat/listdir/scandir/read/write/mkdir/makedirs/walk/getcwd`、`time.sleep`、`select.select`。这些都是「会卡住事件循环」的同步 IO。socket 系列不在列（签名复杂，文件 IO 用不到）。

#### `_caller_in_scope(start_frame, scanned_modules)`（[第 70–81 行](../test/support/detectors/blocking_io_runtime.py#L70)）

```python
def _caller_in_scope(start_frame, scanned_modules):
    frame = start_frame
    depth = 0
    while frame is not None and depth < _MAX_STACK_DEPTH:   # 最多往上爬 50 层
        module = frame.f_globals.get("__name__", "")
        for prefix in scanned_modules:
            if module == prefix or module.startswith(prefix + "."):
                return True
        frame = frame.f_back
        depth += 1
    return False
```

从某个栈帧往上爬，看**调用栈里有没有经过业务代码**（模块名以 `deerflow.` 开头）。这是 gate 的核心判定之一——只抓「发自业务代码」的阻塞 IO，pytest / langchain / 第三方库自己内部的同步 IO 不算违规。

#### `_make_guard(display_name, original_fn, scanned_modules)`（[第 84–104 行](../test/support/detectors/blocking_io_runtime.py#L84)）

给每个原语包一层「哨兵」：

```python
def _make_guard(display_name, original_fn, scanned_modules):
    def guard(*args, **kwargs):
        # ① 不在运行中的事件循环里 → 同步上下文，直接放行
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return original_fn(*args, **kwargs)
        # ② 在事件循环里：调用栈经过业务代码 → 违规
        caller = sys._getframe(1)   # guard 的直接调用者
        if caller is not None and _caller_in_scope(caller, scanned_modules):
            raise BlockingError(f"同步阻塞 IO '{display_name}' 在事件循环里被调用…请用 asyncio.to_thread 卸载。")
        return original_fn(*args, **kwargs)
    return guard
```

两个条件**同时满足**才抛：① 正在事件循环里；② 调用栈经过 `deerflow.*`。缺一个都放行——所以同步测试代码、第三方库内部 IO 都不会被误伤。

#### `detect_blocking_io_strict(scanned_modules=...)`（[第 107–128 行](../test/support/detectors/blocking_io_runtime.py#L107)）

```python
@contextmanager
def detect_blocking_io_strict(scanned_modules=_DEFAULT_SCANNED_MODULES):
    scanned = tuple(scanned_modules)
    saved = {}
    try:
        for module, attr in _BLOCKING_TARGETS:          # 把 14 个原语换成 guard
            original_fn = getattr(module, attr)
            saved[(module, attr)] = original_fn
            setattr(module, attr, _make_guard(...))
        yield                                               # 这期间跑测试
    finally:
        for (module, attr), original_fn in saved.items():  # 无论是否异常，还原
            setattr(module, attr, original_fn)
```

一个上下文管理器：`with` 进去时把 14 个原语换成哨兵，`with` 出来时（`finally`，异常也还原）全部还原，**绝不污染全局**。

### 4.3 gate 怎么被激活 —— [test/blocking_io/conftest.py](../test/blocking_io/conftest.py)

gate 不会自己跑——要靠 pytest 的 **hookwrapper** 把它包在每个测试外面（[第 27–34 行](../test/blocking_io/conftest.py#L27)）：

```python
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _is_blocking_io_item(item) or item.get_closest_marker("allow_blocking_io") is not None:
        yield
        return
    with detect_blocking_io_strict():   # 只对 test/blocking_io/ 下的用例激活
        yield
```

关键：**作用域限制**。pytest 一加载这个 conftest，hookwrapper 就全局生效，所以必须用 `_is_blocking_io_item`（判断测试文件路径在不在 `test/blocking_io/` 下）过滤，否则会在无关测试上误触发。`@pytest.mark.allow_blocking_io` 是显式 opt-out。

### 4.4 全局 conftest —— [test/conftest.py](../test/conftest.py)

整个测试套件的公共配置，三块：

1. **sys.path 兜底**（[第 25–42 行](../test/conftest.py#L25)）：显式把 `backend/` 和 `packages/harness` 加进 `sys.path`。原因见 §8.1（editable `.pth` 在 Python 3.14 + uv 下失效）——这样无论 `make test` 还是直接 `pytest` 都能 `import deerflow`。
2. **`tmp_data_dir` fixture**（[第 66–75 行](../test/conftest.py#L66)）：给每份测试一个独立临时目录，避免跨测试污染（memory.json / 沙箱目录等）。
3. **两个 autouse fixture，都软加载**（[第 83–138 行](../test/conftest.py#L83)）：`_reset_singletons_between_tests`（每测后重置 skill storage / mcp 缓存等单例）和 `_auto_user_context`（每测注入默认 user）。**都用 `try/except ImportError`**——因为它们保护的模块（skills / mcp / user_context）在 Phase 0 时还没落地，硬 import 会让本篇自己的测试都跑不起来；模块落地后自动生效。opt-out：`@pytest.mark.no_auto_user`。

> 还留了一段注释掉的「循环导入 mock 模板」（[第 44–58 行](../test/conftest.py#L44)），供未来 subagents / agents 出现循环导入时启用——当前用不到。

### 4.5 harness 边界 —— [test_harness_boundary.py](../test/test_harness_boundary.py)

一个 AST 扫描测试，钉死「框架包不得依赖应用层」：

- [第 22 行](../test/test_harness_boundary.py#L22)：`BANNED_PREFIXES = ("app",)`——禁止 `import app...` / `from app...`。
- [第 25–43 行](../test/test_harness_boundary.py#L25)：`_collect_imports` 用 `ast.parse` 解析每个 `.py`，抽出所有 import 语句的（行号, 模块路径）。
- [第 46–56 行](../test/test_harness_boundary.py#L46)：`test_harness_does_not_import_app` 遍历 `packages/harness/deerflow/` 下所有 `.py`，发现任何 `app.*` import 就失败。

> **诚实说明现状**：mini 目前**没有 `app/` 目录**（不 port Gateway），所以这个测试**当前恒通过**——它是一个**占位 + 未来护栏**。一旦哪天引入 `app/` 层，任何 `from app...` 都会立刻让本测试变红，把「harness 边界」从口头约定变成 CI 强制。不是「现在就在抓」，是「一旦越界立刻被抓」。

---

## 5. 数据流：一次调用怎么走完

### 场景 A：你敲 `make test`，背后发生了什么

```
cd backend && make test
   │  Makefile 把 target 展开成：
   ▼
PYTHONPATH=packages/harness uv run pytest ../test -v
   │
   ├── PYTHONPATH=packages/harness → deerflow 可 import（绕过坏掉的 .pth，§8.1）
   ├── uv run → 用 ~/.venvs/mini-deer-flow 的解释器跑 pytest
   ▼
pytest 读 ../pytest.ini（rootdir=项目根）
   ├── testpaths=test → 收集 test/ 下所有 test_*.py（1700+ 条）
   ├── asyncio_mode=auto → async def test_* 自动套事件循环跑
   ▼
test/conftest.py 在收集前执行
   ├── 注入 sys.path（_HARNESS_ROOT）→ import deerflow 稳了
   ├── 注册 autouse fixtures（单例重置 + 默认 user）
   ▼
逐条跑测试 → 全绿 = 1713 passed
```

### 场景 B：一个阻塞 IO 违规，怎么被 gate 当场抓住

假设 `test/blocking_io/test_io_offload.py` 里有一条：在 async 函数里**直接**调 `deerflow` 的某个同步 `open()`（没卸载到线程）：

```
1. pytest 准备跑这条 test/blocking_io/ 下的用例
   ▼
2. test/blocking_io/conftest.py 的 hookwrapper 发现：
   路径在 blocking_io/ 下 + 没有 allow_blocking_io marker
   → with detect_blocking_io_strict(): yield
   ▼
3. detect_blocking_io_strict 进入：把 builtins.open / os.stat / … 14 个原语
   临时替换成 guard（saved 保存原件）
   ▼
4. 测试函数体跑起来（async，在事件循环里）→ 调用 deerflow.xxx 的同步 open()
   ▼
5. guard 被触发：
   ① asyncio.get_running_loop() 成功 → 确实在事件循环里
   ② sys._getframe(1) 往上爬栈 → 命中 deerflow.* 帧 → _caller_in_scope=True
   ▼
6. raise BlockingError("同步阻塞 IO 'open' …请用 asyncio.to_thread 卸载。")
   ▼
7. 测试里 with pytest.raises(BlockingError): 捕获 → 测试通过（gate 工作正常）
   ▼
8. detect_blocking_io_strict 的 finally：把 14 个原语全部还原（异常也还原）
   → 不污染下一条测试
```

这就是 gate 的价值：**它让「这条 IO 必须卸载」从一句口头提醒，变成了一条会失败的测试**。后续每个模块（memory / skills / sqlite…）落地新的同步 IO 时，都要在这里加一条「生产锚点」测试锁住卸载点（§7）。

---

## 6. 配置与用法

### 命令清单（在 `backend/` 下敲）

```bash
make install          # 装依赖（首次 / 改了 pyproject 后）
make dev              # 起 dev server（langgraph dev）
make test             # 全量测试，必须全绿
make test-blocking-io # 只跑阻塞 IO gate
make lint             # 检查（ruff check + ruff format --check，两步都过）
make format           # 自动修格式
```

### harness 的 7 个 extras（[packages/harness/pyproject.toml](../backend/packages/harness/pyproject.toml)）

| extra | 装的包 | 何时需要 |
|-------|--------|----------|
| `sqlite` | `langgraph-checkpoint-sqlite` | sqlite checkpointer / store |
| `postgres` | `asyncpg` + `langgraph-checkpoint-postgres` + `psycopg[binary]` + `psycopg-pool` | postgres 持久化 |
| `aiosqlite` | `aiosqlite` | 异步 sqlite 驱动（其实已是 core 依赖，extra 保留为透传） |
| `mcp` | `langchain-mcp-adapters` | MCP 外部工具加载（→ #20） |
| `tiktoken` | `tiktoken` | memory 精确 token 计数（→ #18） |
| `uploads` | `markitdown` | 文件上传转换（→ #23） |
| `aio_sandbox` | `agent-sandbox` + `requests` | AIO 容器沙箱（→ #14） |

安装：`cd backend && uv sync --extra postgres`。根 [pyproject.toml](../backend/pyproject.toml) 透传了 `postgres` / `sqlite` / `aiosqlite` / `mcp` 四个常用项；业务代码对每个可选包都是软加载，**不装也能跑**（回退内存 / 默认 + 打印安装提示）。

### ruff 配置（[ruff.toml](../ruff.toml)）

| 字段 | 值 | 含义 |
|------|----|------|
| `line-length` | `240` | 一行最多 240 字符（比常见 80/120 宽，因为对齐上游风格） |
| `target-version` | `py312` | 按 Python 3.12 语法检查 |
| `[lint] select` | `["E","F","I"]` | E=pycodestyle 错误、F=pyflakes（未用变量等）、I=isort（import 排序） |
| `[format] quote-style` | `double` | 统一双引号 |

### pytest 配置（[pytest.ini](../pytest.ini)）

| 字段 | 值 | 含义 |
|------|----|------|
| `testpaths` | `test` | 测试都在项目根的 `test/` 下 |
| `asyncio_mode` | `auto` | `async def test_*` 自动当 asyncio 测试，无需逐个标 |
| `markers` | `no_auto_user` / `allow_blocking_io` | 关闭默认 user fixture / 跳过 gate |

---

## 7. 与其它模块的关系

本篇是**所有模块的地基**——后面每个模块落地都要回头用到它：

- **config / utils / user_context**（Phase 0 同期）——测试靠 `make test` 跑、靠 conftest 的 `tmp_data_dir` / autouse fixture 隔离。
- **persistence / checkpointer**（Phase 1）——靠 `sqlite` / `aiosqlite` / `postgres` extras + 软加载策略。
- **memory / skills / sandbox / sqlite 路径准备**——落地的每一处「必须 `asyncio.to_thread` 卸载的 IO」都要在 `test/blocking_io/test_io_offload.py` 加一条**生产锚点**测试，锁住那个卸载点不被未来误删。
- **subagents / agents**——若出现循环导入，启用 conftest 的 sys.modules mock 模板（§4.4）解开，不必改生产代码。

文字依赖图：

```
build（pyproject / Makefile / conftest / gate / 边界测试）── 全局基础设施
   ↓ 被所有后续模块的「测试可跑 / lint 可过」依赖
config / utils / user_context（Phase 0）
   ↓
models / persistence / runtime（Phase 1）…… 一直到 runs / 集成（Phase 8）
```

---

## 8. 设计权衡与踩坑

### 8.1 editable `.pth` 不稳 → 用 `PYTHONPATH` + conftest 双重兜底

Python 3.14 + uv 创建的 venv，会给 site-packages 下的 `.pth` 文件加 macOS `hidden` flag，而 Python 3.14 的 `site.py` 会**跳过 hidden `.pth`**——导致 editable 安装的 `deerflow-harness` 失效，`import deerflow` 报 `ModuleNotFoundError`。而且 `uv run` 重新 sync 后 `.pth` 还会回退到不可用态。

**两道兜底**：① [Makefile](../backend/Makefile) 每个 target 显式带 `PYTHONPATH=packages/harness`（所以 `make` 命令稳定）；② [test/conftest.py](../test/conftest.py) 在收集前把 `packages/harness` 加进 `sys.path`（直接 `pytest` 也稳）。**结论：用 `make` 命令，别用裸 venv 的 python。** 另外项目在 iCloud 同步目录会让 venv 海量小文件被同步清掉——根治是 `export UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow`（详见 [start-here.md](start-here.md) §4 第 1 步）。

### 8.2 配置为什么放在项目根（不在 backend/）

`ruff` 对每个被检查的文件，是**从该文件所在位置一路向上找最近的配置**。`test/` 在 `backend/` 外——如果配置留在 `backend/pyproject.toml`，就会出现：`backend/` 下文件读到配置、`test/` 下文件读不到（回退 ruff 默认值），两边行宽等规则不一致。把配置放到项目根的 [ruff.toml](../ruff.toml) 后，两边向上查找都命中同一份——**单一真相源**。pytest 配置同理放在根的 [pytest.ini](../pytest.ini)（test 移出 backend 后 rootdir 上移到项目根）。

### 8.3 dev 依赖放 `[dependency-groups]`，功能扩展放 `optional-dependencies`

`dependency-groups`（PEP 735）是 uv 原生的「开发依赖组」——语义是「只在开发 / CI 用，不进发布产物」。`optional-dependencies` 语义是「用户可选装的功能扩展」。pytest / ruff 属于前者，sqlite / postgres 属于后者——分开放，语义清晰。

### 8.4 langgraph 锁下限 `>=1.1`

后续模块要用 `Runtime` / `ToolRuntime` / `configurable["__pregel_runtime"]` / `Command(goto=END)` 等 API，这些在 langgraph 1.1 才稳定。锁下限防降级到 1.0 导致 API 缺失。

### 8.5 gate 为什么「scoped to deerflow」

gate 的 `scanned_modules=("deerflow",)` 意味着**只有调用栈经过 deerflow 业务代码**的阻塞 IO 才算违规。pytest 自身、langchain、第三方库内部的同步 IO 不算——否则测试根本没法写（import 一个包就触发）。mini 没有 app 层，所以只扫 `deerflow`。

### 8.6 gate smoke 测试为什么必须存在

> 一个绿色的 gate 但其实什么都没抓，比没有 gate 更糟。

`test_io_offload.py::test_gate_catches_unoffloaded_blocking_io_from_deerflow_module` 故意**不**卸载、直接在 async 里调 `deerflow.config.app_config.load_config_from_yaml`（内部同步 `open()`），断言会抛 `BlockingError`。如果哪天 `scanned_modules` 配错、detector 被误删、或 hookwrapper 失效——这个「元测试」会先红，提醒 gate 本身坏了。

### 8.7 conftest 的 autouse fixture 全部软加载

`_reset_singletons_between_tests` 和 `_auto_user_context` 都用 `try/except ImportError`。原因：本篇是 Phase 0，被它们保护的模块（skills / mcp / user_context）要到后面 Phase 才落地。硬 import 会让本篇自己的测试跑不起来。软加载 = 模块落地后自动生效，不落地就静默跳过。

> 内部追溯：本篇的设计约束在上游 deer-flow 的工程记录里分别编号为红线 #24（缺包软加载 + 可操作提示）、#25（空配置可起步）、#26（langgraph 下限）、#28（边界 / gate 的强制化）。这些编号仅作内部对照，不影响理解。

---

## 9. 常见问题 / 排错

**Q: `make test` 报 `ModuleNotFoundError: No module named 'deerflow'`？**
A: 正常不会遇到——Makefile 自带 `PYTHONPATH=packages/harness`、conftest 也补了 `sys.path`，双重兜底。若仍遇到，根因多半是 venv 本身坏了（Python 3.14 + uv 的 hidden `.pth` 问题，§8.1），或 venv 在 iCloud 同步目录被清掉了——把 venv 挪到 `~/.venvs/mini-deer-flow` 重装。

**Q: `uv sync` 报某个 extra 装不上？**
A: extras 是可选的，默认 `uv sync` 不装。需要时 `uv sync --extra <name>`。业务代码对该包是软加载，不装也能跑（回退内存 / 默认）。

**Q: `make lint` 报 `Would reformat`？**
A: 那是 `ruff format --check` 没过（格式不达标）——「0 lint」要求 `ruff check` **和** `ruff format --check` 两步都绿。直接 `make format` 自动修（纯空白调整，安全），再 `make lint` 确认。

**Q: blocking-IO gate 误报，把合法的同步 IO 也拦了？**
A: 检查调用栈是否真的经过 `deerflow.*`。若是测试自身的 IO（如写临时文件），要么挪出 async 上下文，要么给该测试加 `@pytest.mark.allow_blocking_io`。若是生产代码，说明该 IO 确实需要 `asyncio.to_thread` 卸载——这正是 gate 的目的。

**Q: gate smoke 测试红了（`test_gate_catches...` 不再抛 BlockingError）？**
A: gate 本身坏了。依次检查：① `_BLOCKING_TARGETS` 还在吗；② `scanned_modules` 还是 `("deerflow",)` 吗；③ `test/blocking_io/conftest.py` 的 hookwrapper 还在吗。

**Q: `test_harness_boundary.py` 红了？**
A: 你在 harness 包里 `import app.*` 了。把那个依赖移到 app 层，或通过依赖注入 / 回调让框架层不直接依赖 app。（注意：mini 当前没有 `app/` 目录，所以这测试现在恒过——它红了一定是你新引入了 app 层且越界了。）
