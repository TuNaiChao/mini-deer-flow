# 1. build.md — 工程化基础设施（M-build）

> 一句话定位：M-build 是 mini-deer-flow 对齐 deer-flow 的**第 0 步**——它不实现任何业务功能，只解决一个问题：**让「写完代码能跑测试、能 lint」这件事成立**。跳过它，后面所有模块都会撞上「代码写完了但环境跑不起来」的坑（M-models 已踩过：`uv` 锁住了、包没装）。

> **Phase 0 全维重审（2026-06-28）**：diff `Makefile` + `pyproject.toml` + `langgraph.json` vs 最新上游。
> mini 的 6 个 target（`install` / `dev` / `test` / `test-blocking-io` / `lint` / `format`）与上游**命名与语义对齐**。
> **有意不 port** 的上游 target 均依赖 mini 不做的层：`dev`/`gateway`（上游跑 `uvicorn app.gateway.app:app`，
> mini 无 Gateway 故 `dev` 跑 `langgraph dev` 读 `langgraph.json`）；`migrate-rev`（alembic，mini 走 `create_all`
> 教学简化）；`detect-blocking-io`（上游 `scripts/detect_blocking_io_static.py` + `tests/blocking_io/` 严格
> 运行时 gate，mini 是教学版无此 gate）。`lint`/`format`：上游用 `uvx ruff`，mini 用 `uv run ruff`（等价，
> mini 已锁 ruff 于 dev 依赖）。`pyproject.toml` 的 ruff 配置（line-length=240、双引号、3.12+）一致。
> 无需补丁。

---

## 零基础先读：这些名词是什么

> 不熟悉 Python 工程化工具的话，先读这一节，再往下看。每一条都用大白话讲。

### 测试（test）是什么

写完一段代码，怎么确认它"对"？最原始的办法是手动跑一遍、看输出。**测试**就是把"手动验证"写成一小段代码，交给机器自动跑。比如你写了个加法函数 `add(a, b)`，就配一条测试 `assert add(1, 2) == 3`。以后每次改代码，跑一遍全部测试，立刻知道有没有把原本好的功能改坏。

我们用 **pytest** 这个工具来组织和运行测试。`make test` = 跑全部测试，全过（俗称"全绿"）才算代码没问题。

### lint 是什么

**lint** 的英文原意是"衣服上的绒毛/毛絮"——在编程里，引申为**代码里那些微小、不影响程序运行、但不干净的瑕疵**。

**lint 工具**就像"代码的拼写检查器"：它**不运行你的代码**，而是直接读代码的文本，静态地挑毛病，例如：

- 定义了却从没用过的变量 / import（多半是写错了，或是该删的废代码）；
- `import` 语句顺序乱七八糟（影响别人阅读）；
- 一行写了 300 个字符（得左右拖滚动条才看得全）；
- 拼写错误、潜在的错误用法……

这些一般**不是 bug**（程序照样能跑），但会让代码难读、难协作，还可能藏着隐患——比如一个"没用的变量"，可能本该被用、只是名字拼错了。**整个团队统一跑 lint，等于大家写出来的代码风格一致、少踩坑。**

### ruff 是什么

**ruff** 是 Python 生态里目前最流行的"代码检查 + 格式化"工具（用 Rust 写的，所以特别快）。它干两件事：

- `ruff check` —— **检查**（也就是 lint）：扫出上面说的那些"瑕疵"，只报告，默认不改你的代码（除非你加 `--fix` 让它自动修能修的）。
- `ruff format` —— **格式化**：自动重排版（空格、换行、引号），让代码长得"标准"，省去人肉调格式。

我们的项目用 ruff 同时做这两件事。

### `make lint` 和 `make format` 的区别

- `make lint` = 只**检查**不改正：`ruff check`（找问题）+ `ruff format --check`（检查格式是否达标）。有问题就报错退出——用来"卡"住不规范的代码。
- `make format` = 会**自动改**：`ruff check --fix`（自动修 import 排序等）+ `ruff format`（自动重排格式）。

一句话口诀：**lint 是体检（只看报告），format 是治疗（会动手改）**。日常写完代码先 `make format` 自动整理，再 `make lint` 确认全绿。

### Makefile / make 是什么

`uv run pytest ../test -v` 这种命令又长又难记。**Makefile** 就是"长命令的短名字清单"——写一行 `test: <长命令>`，以后只要敲 `make test` 就等于执行那条长命令。本项目的命令清单在 [backend/Makefile](../backend/Makefile)。

### venv / uv / workspace 是什么

- **venv（虚拟环境）**：Python 的"隔离沙箱"。每个项目用自己的 `.venv/` 目录装依赖，互不干扰——A 项目能用 langgraph 1.2，B 项目能用 1.0，各装各的。
- **uv**：一个极快的 Python 包管理器（替代老的 pip）。我们用它装依赖、跑命令：`uv sync` 装包、`uv run <命令>` 在项目环境里跑命令。
- **workspace（工作区）**：把多个相关的 Python 包放在一起统一管理。本项目 `backend/` 是 workspace 根，`packages/harness`（框架包 `deerflow-harness`）是成员。`uv sync` 一次性把根和成员都装好。

---

读完这些，再往下看「为什么需要 M-build」就顺畅了。

---

## 1. 为什么需要它（痛点场景）

在 M-build 之前，mini 的状态是：

| 问题 | 现象 | 后果 |
|------|------|------|
| 没声明 `pytest` | `uv run pytest` → `No such file or directory` | 任何测试都跑不了 |
| 没声明 `ruff` | 无法统一代码风格 | 多人/多 AI 协作时风格漂移 |
| 没声明可选 extras | 后续 `langgraph-checkpoint-sqlite` / `langchain-mcp-adapters` 等缺包时直接 `ImportError` 崩溃 | 想用 sqlite 持久化就得装一堆东西，不知道装哪个 |
| harness 边界只是「口头约定」 | 没有强制检查 | 谁都可能不小心让框架层 `import app.*`，破坏可发布性 |
| 「阻塞 IO 必须卸载」只是口头红线 | 没有强制检查 | 有人删掉一处 `asyncio.to_thread`，事件循环被卡死，测试照样绿 |

M-build 把后三条「口头红线」变成 **CI 强制测试**（红线 #28），把前三条变成 **一条命令可跑**。

---

## 2. 核心概念

- **uv workspace**：`backend/pyproject.toml` 是 workspace 根，`packages/harness` 是成员。`uv sync` 一次性把根 + 成员的依赖装进 `.venv`，并把 `deerflow-harness` 以 editable 方式安装（改代码即时生效，无需重装）。
- **extras（可选依赖）**：把「不是所有人都需要」的重依赖（postgres、mcp、tiktoken、uploads…）拆成 `[project.optional-dependencies]` 里的命名组。默认不装，需要时 `uv sync --extra postgres`。
- **软加载（soft import）**：模块代码里对这些可选依赖一律 `try/except ImportError`，缺包时回退到内存/默认实现并打印「可操作安装提示」。extras 命名必须与 install hint 一致。
- **harness 边界**：`packages/harness/deerflow/` 是可发布的框架包，**永远不得** `import app.*`。由 AST 扫描测试强制。
- **blocking-IO gate**：纯 Python inline 实现（无第三方依赖），在测试期 patch 底层阻塞函数（`open`/`os.stat`/...），仅当调用栈经过 `deerflow.*` 且运行在事件循环上时抛 `BlockingError`。把「同步阻塞 IO 不能跑在事件循环里」变成可断言的事实。**实现机制与「gate 误伤运行时 import」踩坑见 [testing-setup.md §3](testing-setup.md)。**

---

## 3. 设计原理（权衡与踩坑）

### 3.1 为什么 dev 依赖放 `[dependency-groups] dev` 而不是 `[project.optional-dependencies]`

`dependency-groups`（PEP 735）是 uv 原生的「开发依赖组」机制，语义就是「只在开发/CI 用，不进发布产物」。`optional-dependencies` 语义是「用户可选装的功能扩展」。pytest/ruff/blockbuster 属于前者，sqlite/postgres 属于后者——分开放，语义清晰，也对齐 deer-flow。

### 3.2 为什么 ruff 配置放在项目根的 `ruff.toml`

`ruff` 对每个被检查的文件，是**从该文件所在位置一路向上查找最近的配置**。因为 `test/` 在 backend 外，如果把配置留在 `backend/pyproject.toml`，就会出现：`backend/` 下的文件读到 backend 配置、`test/` 下的文件读不到（回退 ruff 默认值）——两边行宽等规则不一致。把配置放到项目根的 [ruff.toml](../ruff.toml) 后，backend 和 test 的所有文件向上查找都命中同一份配置，**单一真相源**。pytest 配置同理放在根的 `pytest.ini`（原因相同：test 移出 backend 后 rootdir 上移到项目根，配置须在根才被读到，详见 `docs/testing-setup.md`）。

### 3.3 langgraph 下限锁到 `>=1.1`（红线 #26）

后续模块要用 `Runtime` / `ToolRuntime` / `configurable["__pregel_runtime"]` / `Command(goto=END)` / `get_config`，这些 API 在 langgraph 1.1 才稳定。当前实际装的是 1.2.5，满足。锁下限是为了防止有人降级到 1.0 导致这些 API 缺失。

### 3.4 blocking-IO gate 为什么「scoped to deerflow」

inline gate（`detect_blocking_io_strict`）的 `scanned_modules=("deerflow",)` 意味着**只有调用栈经过 deerflow 业务代码**的阻塞 IO 才算违规。pytest 自身、langchain、第三方库内部的同步 IO 不算——否则测试根本没法写（import 一个包就触发）。deer-flow（用 blockbuster）扫描 `app` + `deerflow`，mini 没有 app 层，所以只扫 `deerflow`。机制等价，只是不引外部库。

### 3.5 gate smoke 测试为什么存在

> 一个绿色的 gate 但其实什么都没抓，比没有 gate 更糟。

`test_io_offload.py::test_gate_catches_unoffloaded_blocking_io_from_deerflow_module` 故意**不**卸载、直接调 `deerflow.config.app_config.load_config_from_yaml`（内部同步 `open()`），断言会抛 `BlockingError`。如果哪天 `scanned_modules` 配错、detector 被误删、或 conftest 的 hookwrapper 失效——这个测试会先红，提醒你 gate 本身坏了。这是保护其它所有 gate 测试的「元测试」。

### 3.6 conftest 的 autouse fixture 全部软加载

`_reset_singletons_between_tests` 和 `_auto_user_context` 都用 `try/except ImportError`。原因：M-build 是 Phase 0，被它们保护的模块（`deerflow.skills.storage`、`deerflow.runtime.user_context`）要到 Phase 3/4 才落地。如果硬 import，M-build 自己的测试就跑不起来。软加载 = 模块落地后自动生效，不落地就静默跳过。

### 3.7 conftest 的 sys.modules mock 模板

留了一段注释掉的模板（针对未来 `deerflow.subagents.executor` 的循环导入）。循环导入是 deer-flow 真实踩过的坑：`subagents → executor → thread_state → agents → lead_agent → subagent_limit_middleware → executor`。单测轻量模块时，在 conftest 预注入一个 mock 就能打断循环，不必改生产代码。M-build 阶段 mini 还没这问题，先留模板。

---

## 4. 文件结构

```
mini-deer-flow/
├── pytest.ini                              # 【新】项目根 pytest 配置（test/ 在 backend 外，配置须在根）
├── ruff.toml                               # 【新】项目根 lint/format 配置（单一真相源，覆盖 backend+test）
├── test/                                   # 【新·已移到 backend 外】所有测试
│   ├── conftest.py                         #   sys.path 适配 + mock 模板 + tmp_data_dir + autouse
│   ├── test_harness_boundary.py            #   AST 扫描：harness 不得 import app.*
│   ├── support/detectors/blocking_io_runtime.py  # inline blocking-IO gate（不引 blockbuster）
│   └── blocking_io/                        #   gate 回归（conftest 激活 + test_io_offload smoke）
├── skills/public/example/SKILL.md          # 【新】技能协议示例（M14 落地后生效）
├── docs/build.md                           # 本文件
└── backend/
    ├── pyproject.toml                      # 【改】workspace 根：dev 依赖（pytest/ruff）+ uv workspace
    ├── Makefile                            # 【新】install/dev/test/lint/format（test 路径用 ../test）
    ├── extensions_config.json              # 【改】mcp_servers/skills 示例 + $VAR 占位
    ├── langgraph.json                      # （checkpointer 段在 D.3 集成阶段补）
    └── packages/harness/pyproject.toml     # 【改】langgraph>=1.1 + extras(sqlite/postgres/...)
```

---

## 5. 关键接口 / 签名

### Makefile 目标

```bash
make install          # uv sync（装全部依赖）
make dev              # uv run langgraph dev（启动开发 server）
make test             # uv run pytest ../test -v（全量；test/ 在 backend 外）
make test-blocking-io # uv run pytest ../test/blocking_io（仅 gate）
make lint             # ruff check .. + ruff format --check ..（覆盖 backend+test）
make format           # ruff check .. --fix + ruff format ..
```

### harness extras（`packages/harness/pyproject.toml`）

| extra | 包 | 何时需要 |
|-------|----|----------|
| `sqlite` | `langgraph-checkpoint-sqlite` | sqlite checkpointer / run store |
| `postgres` | `asyncpg` + `langgraph-checkpoint-postgres` + `psycopg` | postgres 持久化 |
| `aiosqlite` | `aiosqlite` | 异步 sqlite engine |
| `mcp` | `langchain-mcp-adapters` | MCP 工具加载 |
| `tiktoken` | `tiktoken` | memory token 计数（精确模式） |
| `uploads` | `markitdown` | 文件上传转换 |

安装：`uv sync --extra postgres`（或 `uv add 'deerflow-harness[postgres]'`）。

### blocking-IO gate（`test/support/detectors/blocking_io_runtime.py`）

```python
@contextmanager
def detect_blocking_io_strict(scanned_modules=("deerflow",)) -> Iterator[None]:
    """激活限定在 deerflow.* 调用栈的 blocking-IO 检测（纯 Python inline，无第三方依赖）。"""
```

仅在 `test/blocking_io/conftest.py` 的 `pytest_runtest_protocol` hookwrapper 里用，
业务代码不直接调用。`BlockingError` 是 gate 抛的异常类型（本模块自定义，**不是** blockbuster）。

### gate 的 opt-out

```python
@pytest.mark.allow_blocking_io   # 跳过 gate（marker 在项目根 pytest.ini 注册）
async def test_xxx(): ...
```

---

## 6. 应用方法（可跑 demo）

```bash
cd mini-deer-flow/backend

# 1. 装依赖（首次或改了 pyproject 后）
make install

# 2. 跑全量测试 —— 必须全绿
make test

# 3. 单独跑 blocking-IO gate
make test-blocking-io

# 4. lint
make lint

# 5. 自动修格式
make format
```

---

## 7. 与其它模块的关系

M-build 是**所有模块的地基**：

- **M0 config / M1 utils / M3 user_context**（Phase 0）——它们的测试靠 `make test` 跑起来，靠 conftest 的 `tmp_data_dir` / autouse fixture 隔离。
- **M4 persistence / M5 checkpointer**——靠 `sqlite` / `aiosqlite` / `postgres` extras + 软加载策略。
- **M13 memory / M14 skills**——落地的每一处「必须 `asyncio.to_thread` 卸载的 IO」都要在 `test/blocking_io/test_io_offload.py` 加一个生产锚点。
- **M11 subagents**——落地后启用 conftest 的 sys.modules mock 模板解循环导入。

文字依赖图：

```
build(pyproject/Makefile/conftest/gate) ── 全局基础设施
   ↓ 被所有后续模块的「测试可跑」依赖
config / utils / user_context (Phase 0)
   ↓
models / persistence / runtime (Phase 1) ... 一直到 runs/集成 (Phase 8)
```

---

## 8. 常见问题 / 排错

**Q: `make test` 报 `ModuleNotFoundError: No module named 'deerflow'`？**
A: 正常不会遇到——`test/conftest.py` 已显式把 `packages/harness` 加进 `sys.path` 绕过了这个坑。若仍遇到，根因是 Python 3.14 + uv 的 `.pth` hidden 兼容问题（uv 给 `.pth` 加了 macOS hidden flag，Python 3.14 的 site.py 跳过 hidden `.pth`，editable 安装失效）。完整诊断与解法见 `docs/testing-setup.md` §2，重点检查 conftest 的 sys.path 注入是否还在。

**Q: `uv sync` 报某个 extra 装不上？**
A: extras 是可选的，默认 `uv sync` 不装。需要时 `uv sync --extra <name>`。业务代码对该包是软加载，不装也能跑（回退内存/默认）。

**Q: blocking-IO gate 误报，把合法的同步 IO 也拦了？**
A: 检查调用栈是否真的经过 `deerflow.*`。若是测试自身的 IO（如写临时文件），要么挪出 async 上下文，要么给该测试加 `@pytest.mark.allow_blocking_io`。若是生产代码，说明该 IO 确实需要 `asyncio.to_thread` 卸载——这正是 gate 的目的。

**Q: gate smoke 测试红了（`test_gate_catches...` 不再抛 BlockingError）？**
A: gate 本身坏了。依次检查：① `test/support/detectors/blocking_io_runtime.py` 的 `_BLOCKING_TARGETS` 还在吗；② `scanned_modules` 还是 `("deerflow",)` 吗；③ `test/blocking_io/conftest.py` 的 hookwrapper 还在吗。

**Q: `test_harness_boundary.py` 红了？**
A: 你在 harness 包里 `import app.*` 了。把那个依赖移到 app 层，或通过依赖注入/回调让框架层不直接依赖 app。
