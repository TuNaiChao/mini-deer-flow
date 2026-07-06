# 2. testing-setup.md — 测试环境与 hermetic 约定（怎么跑测试 / 怎么写测试）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`。

> **一句话定位**：本篇讲两件事——① **怎么把 mini 的测试跑起来**（以及一个藏得很深、卡了很久的环境坑，根因在 Python 3.14 的一处 site.py 改动）；② **怎么写测试**——mini 的 hermetic（自包含）约定：不读 `config.yaml`、不连网络、不调真实模型，离开任何特定环境都能跑、都 deterministic。
>
> 配套代码：[../test/conftest.py](../test/conftest.py) · [../test/support/detectors/blocking_io_runtime.py](../test/support/detectors/blocking_io_runtime.py) · [../test/blocking_io/](../test/blocking_io/) · [../pytest.ini](../pytest.ini)。前置：先读 [build.md](build.md)（工程化地基）。

## 学完这篇你能回答什么（learning outcomes）

- Python 项目的测试为什么会因为 venv / `.pth` / `site.py` **跑不起来**？怎么用两条命令（`ls -lO` + `inspect.getsource`）把根因闭环？
- 什么是 **hermetic（自包含）测试**？为什么禁真实网络 / API key / 文件系统副作用——它换来什么（可重现、不 flaky、CI 可跑）？
- async 测试里**怎么 mock 一个 LLM / 模型工厂**，既不连网又能验证业务逻辑（三种 mini 实用模式）？
- **async 服务里同步阻塞 IO 为什么要卸载**？写测试时 gate 会怎么影响你（那个「import 写错位置报成 ModuleNotFoundError」的坑）？

> 这几条都是后端 / agent 工程面试的高频点——尤其「hermetic 测试」「async 阻塞 IO」。

---

## 1. 太长不看 + 怎么跑测试

> **最基础（几个测试术语，不熟先看这）**：
> - **断言（assert）**——测试的核心动作：「我断言这个表达式为真」，不为真就报错失败。`assert add(1,2)==3` 就是断言「加法结果必须是 3」。
> - **测试运行器（test runner）**——负责「收集所有测试 → 一条条跑 → 汇报通过/失败」的工具，我们用 **pytest**。
> - **fixture**——测试的「公共道具/准备工作」（写一次、处处复用），如「每个测试要一个干净临时目录」「每个测试要一个默认 user」。省得每个测试都重写准备代码。
> - **mock / monkeypatch（打桩）**——把真实外部依赖（网络、文件、真实模型）**换成假的**，让测试不碰外部世界。pytest 的 `monkeypatch` 临时改某个属性/环境变量，测试结束自动还原。
> - **flaky（偶发失败）**——测试有时过有时不过（依赖网络/时间/执行顺序），是测试大忌；**deterministic（确定性）**——同输入永远同结果。hermetic 约定（§5）就是为消灭 flaky、保证 deterministic。
> - **CI（持续集成）**——每次提交代码，服务器自动跑全部测试。CI 机器上**必须裸跑能过**，所以测试不能依赖你本机的 API key 或网络（否则 CI 必挂）。

测试跑通靠两件事：

1. **环境**：[test/conftest.py](../test/conftest.py) 显式把 `packages/harness` 塞进 `sys.path`，绕过 Python 3.14 + uv 导致的 editable 安装失效（§3）。
2. **写法**：所有测试 hermetic——`monkeypatch` 桩化外部依赖 / 显式 `app_config=` 注入，不读 `config.yaml`、不连网、不调真实模型（§5）。

**跑测试的命令**（都在 `backend/` 下敲）：

```bash
make test               # 全量：uv run pytest ../test -v（1700+ 条，全绿 = 1713 passed）
make test-blocking-io   # 只跑阻塞 IO gate（../test/blocking_io）

# 想跑得更细（直接用 pytest）：
uv run pytest ../test/test_config.py -v               # 单个文件
uv run pytest ../test/test_config.py::test_xxx -v      # 单条测试
uv run pytest ../test -k "memory and not live" -v      # 按名字过滤
```

> 基线：`make test && make lint` → **1713 passed, 0 lint**（见 [todo.md](todo.md) 顶部）。改完代码必须保持。

---

## 2. 起点：为什么「一个都跑不了」

工程化地基（[build.md](build.md)）落地之前：

| 症状 | 直接原因 |
|------|----------|
| `uv run pytest` → `No such file or directory` | `pytest` 没声明为依赖（pyproject 没有 dev 组） |
| 即便装上 pytest，`import deerflow` 失败 | editable 安装失效（§3） |
| 老测试靠 `print` + 真实 API key 验证 | 没有 hermetic 约定，离开特定环境就跑不了 |

[build.md](build.md) 解决了第一条（声明 `pytest` / `pytest-asyncio` / `ruff` 为 dev 依赖）。第二、三条是本文重点。

---

## 3. 环境坑：Python 3.14 + uv 的 editable 安装失效

> 这是本篇最有价值的一节——一个**卡了很久、根因极隐蔽**的坑。诊断思路本身值得学。

### 3.1 现象

- `.venv/bin/python -c "import deerflow"` 时好时坏；
- `.venv/bin/python -m pytest` 稳定报 `ModuleNotFoundError: No module named 'deerflow'`，哪怕 `python -c` 刚才能 import；
- `uv sync` 每跑一次，问题就复发一次。

### 3.2 根因（两层叠加）

**第一层：uv 给 `.pth` 加了 macOS `hidden` flag。**

uv 创建 venv 时，site-packages 下的 `.pth` 文件被设置了 `UF_HIDDEN`（macOS 的隐藏属性）。用 `ls -lO` 能看到：

```
-rw-r--r--@ ... hidden ... _editable_impl_deerflow_harness.pth
-rw-r--r--@ ... hidden ... _virtualenv.pth
-rw-r--r--@ ... hidden ... distutils-precedence.pth
```

editable 安装靠这个 `.pth`（内容是源码目录路径 `/…/packages/harness`）把 harness 加入 `sys.path`，从而 `import deerflow` 才能找到 `packages/harness/deerflow/`。

**第二层：Python 3.14 的 `site.addpackage` 新增了「跳过 hidden .pth」检测。**

`python3.14 -c "import inspect, site; print(inspect.getsource(site.addpackage))"` 可以看到新增的几行：

```python
if ((getattr(st, 'st_flags', 0) & stat.UF_HIDDEN) or
    (getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_HIDDEN)):
    _trace(f"Skipping hidden .pth file: {fullname!r}")
    return
```

于是 site 启动时**跳过所有 hidden 的 .pth** → editable 的 `.pth` 不生效 → `packages/harness` 不进 `sys.path` → `import deerflow` 失败。

这是 **uv（给 .pth 加 hidden）× Python 3.14（跳过 hidden .pth）** 的兼容问题——两边单独看都「没错」，合在一起就坏。

> 这**不是 mini 独有的坑**：上游 deer-flow 同样声明 `requires-python = ">=3.12"`，在 Python 3.14 + uv 下也会撞上同一问题。上游「看起来没事」是因为它**同样**不依赖 `.pth`——它的 [Makefile](https://github.com/bytedance/deer-flow) 每个 target 也带 `PYTHONPATH=.`、它的 `tests/conftest.py` 也 `sys.path.insert(...)` 显式注入。**「conftest 显式注入 sys.path 来绕过 .pth」是两边共享的防御模式**，mini 是直接沿用。§9 会专门讲这条。

### 3.3 怎么定位的

按这个顺序排查最快：

1. **对比 `python -c` 与 `python -m pytest` 的 `sys.path`**——发现后者缺 `packages/harness`，且 `.pth` 全部失效（手写一个 `ztest.pth` 也不被处理）。
2. **看字节内容**：`python -c "print(open('.../_editable_impl_deerflow_harness.pth','rb').read())"`——内容干净（纯路径、无 BOM），排除文件本身损坏。
3. **看 macOS flags**：`ls -lO .venv/lib/python3.14/site-packages/*.pth`——`hidden` 列赫然在目。
4. **看 site.py 源码**：`inspect.getsource(site.addpackage)`——看到新增的 hidden 检测分支，根因闭环。

### 3.4 解法：conftest 显式注入 sys.path

为什么不直接 `chflags nohidden *.pth`？因为 **`uv sync` 每次都会重新生成带 hidden 的 .pth**，手动 chflags 是反复的、不可靠的。

可靠解法是在 [test/conftest.py](../test/conftest.py)（pytest 收集任何测试前必然加载）里直接把源码目录塞进 `sys.path`，彻底绕过 `.pth`（[第 25–42 行](../test/conftest.py#L25)）：

```python
_HERE = Path(__file__).resolve().parent
_BACKEND_ROOT = _HERE.parent / "backend"
_HARNESS_ROOT = _BACKEND_ROOT / "packages" / "harness"
for _p in (_HERE, _BACKEND_ROOT, _HARNESS_ROOT):   # support / backend / harness
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

这样无论 `.pth` 是否被 hidden、`python -m pytest` 还是 `uv run pytest`，`import deerflow` 都能成功。**[Makefile](../backend/Makefile) 还多一道保险**：每个 target 显式带 `PYTHONPATH=packages/harness`（见 [build.md §8.1](build.md#81-editable-pth-不稳--用-pythonpath--conftest-双重兜底)）。所以——**用 `make` 命令，别用裸 venv 的 python。**

> 排错快捷命令汇总：
> ```bash
> ls -lO .venv/lib/python3.14/site-packages/*.pth        # 看 hidden flag
> .venv/bin/python -c "import sys; print([p for p in sys.path if 'harness' in p])"
> .venv/bin/python -c "import inspect, site; print(inspect.getsource(site.addpackage)[:1200])"
> ```

---

## 4. blocking-IO gate：测试作者要知的用法与坑

> gate 的**完整实现**（逐函数走读：`BlockingError` / `_BLOCKING_TARGETS` / `_caller_in_scope` / `_make_guard` / `detect_blocking_io_strict`）在 [build.md §4.2](build.md#42-blocking-io-gate--blocking_io_runtimepy)。本篇只讲**写测试时**你要知道的几件事。

### 4.1 一句话机制

gate 在测试期把 `builtins.open` / `os.stat` / … 14 个同步阻塞原语换成「哨兵」，**当调用栈经过 `deerflow.*` 且正跑在事件循环里时**抛 `BlockingError`。它由 [test/blocking_io/conftest.py](../test/blocking_io/conftest.py) 的 hookwrapper 激活，**只对 `test/blocking_io/` 路径下的用例生效**（不会误伤别的测试）。

### 4.2 坑：gate 会误伤「在 gate 内首次 import 的模块」

gate 在整个 `pytest_runtest_protocol`（setup + call + teardown）激活。如果被测模块的 import 写在 **async 测试函数体内**（call 阶段，gate 已激活），首次 import 会触发模块体执行 → 模块体的进一步 import 让 importlib 调 `os.stat` → 此时栈顶正执行该 deerflow 模块体 → gate 判定违规 → 抛 `BlockingError`，但它在 importlib 内部被吞，**最终报成 `ModuleNotFoundError`**，极具迷惑性。

**约定**（见 [test_io_offload.py 顶部注释](../test/blocking_io/test_io_offload.py#L26)）：被测的生产模块一律在**测试模块顶部**（collect 阶段，gate 未激活）import；gate 只负责观察「运行时」的同步 IO 调用。

```python
# ✅ 对：模块顶部 import（collect 阶段，gate 没激活）
from deerflow.config.app_config import get_app_config, load_config_from_yaml

async def test_xxx(tmp_path):
    ...  # 函数体里用 get_app_config


# ❌ 错：import 写在 async 函数体里 → 首次 import 被 gate 误拦成 ModuleNotFoundError
async def test_xxx(tmp_path):
    from deerflow.config.app_config import get_app_config   # 别这样
```

### 4.3 opt-out：`@pytest.mark.allow_blocking_io`

gate 默认拦。如果你的测试**合法地**需要在事件循环里做同步 IO（比如测试本身就是验证「同步上下文」的行为），显式跳过：

```python
@pytest.mark.allow_blocking_io     # 该 marker 在 pytest.ini 注册
async def test_sync_path_works_outside_loop(tmp_path):
    ...
```

### 4.4 生产锚点：每个卸载点都该配一条测试

gate 的真正价值不是那几条 smoke 测试，而是**「生产锚点」**——后续模块（memory / skills / sqlite 路径准备…）每落地一处「必须 `asyncio.to_thread` 卸载的 IO」，就在 [test_io_offload.py](../test/blocking_io/test_io_offload.py) 加一条测试锁住它，防止未来有人误删卸载代码。例（[第 72 行](../test/blocking_io/test_io_offload.py#L72)）：

```python
async def test_get_app_config_cache_hit_in_event_loop_does_no_file_io(monkeypatch) -> None:
    """事件循环里 get_app_config 命中缓存时不得做同步文件 IO（锁住早返回不被回退）。"""
    monkeypatch.setattr(cfg_mod, "_app_config", sentinel)   # 模拟「已加载」
    monkeypatch.setattr(cfg_mod, "_config_mtime", 1234.5)
    result = get_app_config()    # gate 激活 + 在事件循环里：若仍 stat/getcwd 会抛 BlockingError
    assert result is sentinel
```

> 一个绿色的 gate 但其实什么都没抓，比没有 gate 更糟——所以 smoke 测试（`test_gate_catches...`）先保证 gate 本身能抓，再靠生产锚点保证每个卸载点不被回退。

---

## 5. hermetic 测试约定（怎么写测试）

「hermetic」= **自包含、不依赖外部环境**。mini 的测试一律 hermetic：不读 `config.yaml`、不连网络、不调真实 LLM、不留文件系统副作用。换来的是**可重现、不 flaky、CI 上裸跑就能过**。

### 5.1 风格清单

- 模块级 docstring（一句话定位 + hermetic 说明）。
- `from __future__ import annotations`。
- 每个测试聚焦一个行为，`test_<behavior>_<condition>` 命名，函数带简短 docstring。
- 用 `pytest.raises` + `assert`，**不用 print、不用 try/except 吞错误、不留 `if __name__ == "__main__"` 块**。
- 隔离手段：`monkeypatch` 桩化外部依赖、显式注入配置、用标准库 / 自造 fake 代替真实 provider。
- 需要真实网络 / API key 的集成冒烟，用 `pytest.skip()` 守卫，或单独放 `*_live.py`。

### 5.2 关键注入点（mini 的 API 都为 hermetic 留了口子）

| 被测函数 | hermetic 注入方式 |
|----------|-------------------|
| `get_available_tools(*, app_config=)` | 传 `AppConfig(tools=[])` 只取内置工具；配置工具用 `monkeypatch` 桩掉 `resolve_variable` |
| `build_middlewares(*, app_config=)` | 传 `AppConfig(title={"enabled": False})` 等控制开关；不读全局 config |
| `create_deerflow_agent(model=, tools=, middleware=)` | 传 `model=object()` + `monkeypatch` 把 `create_agent` 换成记录器 |
| `make_lead_agent(config)` | `monkeypatch` 桩化 `get_app_config` / `create_chat_model` / `get_available_tools` / `build_middlewares` / `create_agent` |
| `resolve_variable` / `resolve_class` | 成功路径用 `json:loads` 等标准库；缺包路径 `monkeypatch` 桩掉 resolver 的 importlib |
| `AppConfig` / `ModelConfig` | 直接构造（pydantic），不读文件；`$VAR` 展开用 `monkeypatch.setenv` |

### 5.3 三种可复用的 hermetic 模式

**模式 A：桩化 `create_agent`，验证组装逻辑**（不真正编译图）：

```python
def test_create_deerflow_agent_passes_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")
    create_deerflow_agent(model=object(), tools=["t1"])
    assert captured["tools"] == ["t1"]
```

**模式 B：用标准库代替真实 provider**（不连任何服务）：

```python
def test_resolve_variable_loads_attribute():
    assert resolve_variable("json:loads") is json.loads
```

**模式 C：自造 fake 模型，桩化工厂的 `resolve_class`**（不真 import 模型 provider、不连 LLM）：

mini 不用 langchain 自带的 fake，而是在测试里定义一个最小 stub，再把模型工厂的 `resolve_class` 桩成返回它（见 [test_tracing.py:255](../test/test_tracing.py#L255) / [:288](../test/test_tracing.py#L288)）：

```python
class _FakeModelClass:              # 最小 stub，满足被测代码对「模型对象」的期望
    ...

def test_models_attach_tracing(monkeypatch):
    monkeypatch.setattr(models_factory, "resolve_class", lambda use, base: _FakeModelClass)
    ...   # 走真实业务路径，但模型是假的、tracing 也桩化
```

> 三种模式递进：A 是「不进图、只验参数」、B 是「用标准库当 provider」、C 是「进真实路径但换假模型」。按被测逻辑的深度挑。

### 5.4 全局 fixture（conftest 自动提供）

写测试时你不用自己管这些——[test/conftest.py](../test/conftest.py) 的 autouse fixture 自动兜底（[第 83–138 行](../test/conftest.py#L83)）：

- `tmp_data_dir`：每份测试一个独立临时目录（持久化测试用它，互不污染）。
- `_reset_singletons_between_tests`（autouse）：每测后重置 skill storage / mcp 缓存等全局单例。
- `_auto_user_context`（autouse）：每测注入默认 user（persistence / memory 读 user_id 的来源）；opt-out 用 `@pytest.mark.no_auto_user`。

---

## 6. 现状

所有测试 hermetic，加上 [build.md](build.md) 的 harness 边界 + blocking-IO gate，**全量绿：1713 passed / 0 lint**（基线见 [todo.md](todo.md) 顶部，不在本文维护易过期的数字快照）。

---

## 7. 设计动机分析（为什么这么约定 / 作用 / 好处）

本篇三个关键约定——**conftest 注入 sys.path**、**blocking-IO gate**、**hermetic 写法**——每个都不是随便定的。读得懂这三个「为什么」，你才算理解了「一个能上 CI、多人协作、不 flaky 的测试体系」长什么样。

### 7.1 为什么用 conftest 注入 sys.path，而不是修好 .pth？

- **作用**：让 `import deerflow` 从任何姿势（`uv run pytest` / 裸 `python -m pytest` / IDE）都能成功，绕过 Python 3.14 + uv 的 hidden-.pth 兼容问题。
- **好处**：**一劳永逸**——`.pth` 是 uv 生成的，`uv sync` 每次都会重新带上 hidden flag（手动 `chflags nohidden` 是反复的、不可靠的）；而 conftest 是项目自己的代码，pytest 收集前**必然加载**，一次写死永久生效。
- **不这么设计会怎样**：靠手动 `chflags nohidden *.pth` → 每次 `uv sync` 后都得重来，团队成员/CI 上没人记得做 → `import deerflow` 随机失败，浪费时间排查。

### 7.2 为什么搞一个 blocking-IO gate（而不是靠 code review）？

- **作用**：把「async 事件循环里不得做同步阻塞 IO」这条**口头铁律**变成**一条会失败的测试**。
- **好处**：async 阻塞 IO 是**最难查的隐性 bug**——本地能跑、上线偶发卡死，极难复现。gate 让越界**当场被抓**，而且后续每个模块新落地一处卸载点，都能用「生产锚点」测试（§4.4）锁死，防止未来误删。
- **不这么设计会怎样**：靠人肉 review 守 → 总有人忘；问题只在生产高负载时偶发「服务假死」，事后排查成本极高。
- **代价**：gate 有学习成本（§4.2 那个「import 写错位置报成 ModuleNotFoundError」的坑），但比起生产假死，这点成本值得。

### 7.3 为什么测试必须 hermetic（禁网络/真实模型/文件副作用）？

- **作用**：让每个测试**自包含**——不读 `config.yaml`、不连网、不调真实 LLM、不留文件副作用。
- **好处**：① **可重现**——同一条测试在你机器、同事机器、CI 机器上跑结果完全一样；② **不 flaky**——不依赖网络通不通、API key 有没有、模型回什么；③ **快**——桩化外部依赖后，测试毫秒级完成，1700+ 条几秒跑完；④ **CI 友好**——CI 机器上裸跑就能过，不用配 key/网络。
- **不这么设计会怎样**：测试依赖真实网络/模型 → CI 上随机挂（网络抖动/key 失效/模型超时），团队慢慢失去对测试的信任，「红着也就红着」，测试形同虚设。

> 三条约定合起来回答同一个问题：**怎么让一个测试体系「机器可信赖」**——把人容易忘的（sys.path/阻塞 IO/外部依赖）都变成代码强制。

---

## 8. 实现差异（vs 上游 deer-flow 源码）

> 对照上游 `backend/tests/` 与 mini `test/`，剥 docstring/comment 后判逻辑差。结论：**测试基础设施是上游的高度忠实移植**——conftest 的 autouse fixture、soft-load 模式、marker（`no_auto_user`/`allow_blocking_io`）、gate 的 pytest 集成几乎逐行一致。真差异集中在两处：① gate 的**检测器内核**（上游 blockbuster 库 / mini 手搓）；② 一处 mini **进度性简化**（循环导入 mock 上游现役 / mini 留注释模板）。

### 差异 1：gate 的 pytest 集成逐行一致，差异只在检测器内核

gate 的 **pytest 集成**（`blocking_io/conftest.py` 的 `pytest_runtest_protocol` hookwrapper + `_is_blocking_io_item` 路径过滤 + `allow_blocking_io` opt-out）**两边逐行一致**，仅 docstring 中英。差异在**检测器内核** `blocking_io_runtime.py`：上游 44 行薄封装 `blockbuster` 库；mini 131 行零依赖手搓。→ 详见 [build.md §9 差异 3](build.md)。

### 差异 2：循环导入 mock——上游现役，mini 留注释模板

- 上游 `tests/conftest.py` **现役注入** `sys.modules["deerflow.subagents.executor"] = MagicMock()`，打断生产代码里一条真实的循环导入链（`subagents → executor → agents.thread_state → agents → lead_agent → subagent_limit_middleware → executor`），让轻量单测能独立 import。
- mini `test/conftest.py` 把同一段**注释保留为模板**（"当前 mini 无此问题，启用时取消注释"）。
- **这是落地进度差异，不是设计分叉**：mini 要么还没引入这条链、要么已用别的方式解开；将来若撞上，取消注释即可。

### 差异 3：conftest 的 sys.path 注入目标不同（因目录布局不同，模式一致）

| | 上游 deer-flow | mini |
|---|---|---|
| conftest 注入的目录 | `backend/`（让 `app` 可 import）+ `scripts/`（provisioner 测试助手） | `test/support/` + `backend/` + `packages/harness`（让 `deerflow` 可 import，无 `app`） |
| autouse fixture | `_reset_skill_storage_singleton` + `_auto_user_context`（都 try/except 软加载） | `_reset_singletons_between_tests`[重置更多：skill storage + mcp 缓存] + `_auto_user_context`（同样软加载） |
| marker | `no_auto_user` / `allow_blocking_io` | 完全一致 |

**模式一致**：都是「conftest 显式 sys.path 注入，不依赖 editable .pth」+「autouse fixture 软加载」。这条**防御模式两边共享**（§3.2 已澄清），mini 直接沿用、略作扩展（mini 的单例重置多管了 mcp 缓存）。

### 差异 4：测试目录位置（同 build.md）

上游 `backend/tests/`（在 backend 内）；mini 项目根 `test/`（在 backend 外）→ 配置上移到项目根（`ruff.toml`/`pytest.ini`）。→ 详见 [build.md §9 差异 2](build.md)。

---

> **一句话总结**：mini 的测试基础设施 = 上游的**高度忠实移植**（conftest autouse fixture / soft-load / marker / gate 的 pytest 集成逐行一致），真差异只有两处：gate 检测器内核（blockbuster 库 vs 手搓，教学简化）+ 循环导入 mock（上游现役 vs mini 留模板，进度差异）。测试写法（hermetic）和诊断方法（sys.path/.pth）两边共享同一套哲学。

---

## 9. 常见问题 / 排错

**Q: `import deerflow` 报 `ModuleNotFoundError`？**
A: 先怀疑环境（§3），再怀疑代码。用 `ls -lO .../*.pth` 看 hidden flag + `inspect.getsource(site.addpackage)` 确认。根治：用 `make` 命令（Makefile 带 `PYTHONPATH`），并确保 venv 不在 iCloud 同步目录（`UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow`，见 [start-here.md](start-here.md) §4 第 1 步）。

**Q: async 测试好像没真正跑（assert 没执行就过了）？**
A: 检查 [pytest.ini](../pytest.ini) 的 `asyncio_mode=auto` 还在不在——它让 `async def test_*` 自动套事件循环。没了的话 async 测试会被当普通函数「调用一次就过」。

**Q: 测试之间互相污染（A 跑完影响 B）？**
A: 多半是全局单例没重置。conftest 的 `_reset_singletons_between_tests` 已软加载重置 skill storage / mcp 缓存；你新加的全局单例要在这个 fixture 里追加 reset。

**Q: gate 报 `BlockingError`，但我觉得这 IO 没问题？**
A: 三种情况：① 生产代码确实该用 `asyncio.to_thread` 卸载（gate 是对的）；② 测试自身的同步 IO，挪出 async 上下文或加 `@pytest.mark.allow_blocking_io`；③ 你把生产模块的 import 写进了 async 函数体（§4.2）——挪到模块顶部。

**Q: 测试需要真实 API key / 网络，怎么放？**
A: hermetic 套件里不要它。要么用 fake（§5.3 模式 C），要么单独放 `*_live.py` / 用 `pytest.skip()` 守卫，别混进 `make test`。

---

## 10. 一句话带走

> **测试跑不通时，先怀疑环境（sys.path / .pth / site.py），再怀疑代码。** 这次卡住的根本不是业务逻辑，而是 Python 3.14 一处不起眼的 site.py 改动 × uv 的 hidden flag。诊断靠 `ls -lO` + `inspect.getsource(site.addpackage)` 两条命令闭环，修复靠 conftest 显式注入 `sys.path` 一劳永逸。写测试则牢记 **hermetic**：桩化外部依赖、显式注入、不碰真实世界。
