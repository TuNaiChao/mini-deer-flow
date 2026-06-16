# testing-setup.md — 测试怎么跑通的（环境踩坑 + hermetic 约定）

> 一句话定位：记录把 mini-deer-flow 的测试**从「一个都跑不了」跑到「全绿」**过程中踩到的环境坑与解法，以及由此确立的 hermetic 测试约定。这份经验不是显而易见的——它卡了相当长时间，根因藏在 Python 3.14 的一处 site.py 改动里。

---

## 0. 最终结论（太长不看）

测试跑通靠两件事：

1. **环境**：在 [test/conftest.py](../test/conftest.py) 里显式 `sys.path.insert(0, packages/harness)`，绕过 Python 3.14 + uv 导致的 editable 安装失效（见 §2）。
2. **写法**：所有测试 hermetic——用 `monkeypatch` / 显式 `app_config=` 注入，不读 `config.yaml`、不连网络、不调真实模型（见 §4）。

```bash
cd mini-deer-flow/backend
make test          # 等价于 uv run pytest ../test -v（test/ 在 backend 外）
```

---

## 1. 起点状态：为什么「一个都跑不了」

M-build 之前：

| 症状 | 直接原因 |
|------|----------|
| `uv run pytest` → `No such file or directory` | `pytest` 没声明为依赖（pyproject 里没有 dev 组） |
| 即便装上 pytest，`import deerflow` 失败 | editable 安装失效（§2） |
| 老测试靠 `print` + 真实 API key 验证 | 没有 hermetic 约定，离开特定环境就跑不了 |

M-build 解决了第一条（声明 `pytest`/`pytest-asyncio`/`ruff` 为 dev 依赖）。第二、三条是本文重点。

---

## 2. 环境坑：Python 3.14 + uv 的 editable 安装失效

### 2.1 现象

- `.venv/bin/python -c "import deerflow"` 时好时坏；
- `.venv/bin/python -m pytest` 稳定报 `ModuleNotFoundError: No module named 'deerflow'`，哪怕 `python -c` 刚才能 import；
- `uv sync` 每跑一次，问题就复发一次。

### 2.2 根因（两层叠加）

**第一层：uv 给 `.pth` 加了 macOS `hidden` flag。**

uv 创建 venv 时，site-packages 下的 `.pth` 文件被设置了 `UF_HIDDEN`（macOS 的隐藏属性）。用 `ls -lO` 可以看到：

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

这是 **uv（给 .pth 加 hidden）× Python 3.14（跳过 hidden .pth）** 的兼容问题，两边单独看都「没错」，合在一起就坏。deer-flow 不受影响，因为它用的是更早的 Python 版本。

### 2.3 怎么定位的

按这个顺序排查最快：

1. **对比 `python -c` 与 `python -m pytest` 的 `sys.path`**——发现后者缺 `packages/harness`，且 `.pth` 全部失效（手写一个 `ztest.pth` 也不被处理）。
2. **看字节内容**：`python -c "print(open('.../_editable_impl_deerflow_harness.pth','rb').read())"`——内容干净（纯路径、无 BOM），排除文件本身损坏。
3. **看 macOS flags**：`ls -lO .venv/lib/python3.14/site-packages/*.pth`——`hidden` 列赫然在目。
4. **看 site.py 源码**：`inspect.getsource(site.addpackage)`——看到新增的 hidden 检测分支，根因闭环。

### 2.4 解法：conftest 显式注入 sys.path

为什么不直接 `chflags nohidden *.pth`？因为 **`uv sync` 每次都会重新生成带 hidden 的 .pth**，手动 chflags 是反复的、不可靠的。

可靠解法是在 [test/conftest.py](../test/conftest.py)（pytest 收集任何测试前必然加载）里直接把源码目录塞进 `sys.path`，彻底绕过 `.pth`：

```python
# test/ 位于 backend 外（项目根/test）；backend 在项目根下
_HERE = Path(__file__).resolve().parent
_BACKEND_ROOT = _HERE.parent / "backend"
_HARNESS_ROOT = _BACKEND_ROOT / "packages" / "harness"
for _p in (_HERE, _BACKEND_ROOT, _HARNESS_ROOT):  # support / backend / harness
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

这样无论 `.pth` 是否被 hidden、`python -m pytest` 还是 `uv run pytest`，`import deerflow` 都能成功。这是**对 mini 这个特定环境（Python 3.14）的适配**，deer-flow 不需要（它的 .pth 正常）。

> 排错快捷命令汇总：
> ```bash
> ls -lO .venv/lib/python3.14/site-packages/*.pth        # 看 hidden flag
> .venv/bin/python -c "import sys; print([p for p in sys.path if 'harness' in p])"
> .venv/bin/python -c "import inspect, site; print(inspect.getsource(site.addpackage)[:1200])"
> ```

---

## 3. blocking-IO gate：用 inline 实现，不引 blockbuster

deer-flow 的 blocking-IO gate 依赖第三方库 `blockbuster`。mini 选择**纯 Python inline 实现**（[test/support/detectors/blocking_io_runtime.py](../test/support/detectors/blocking_io_runtime.py)），零额外依赖。

### 3.1 机制（与 blockbuster 等价）

1. 进入上下文时 patch 一组同步阻塞原语（`builtins.open`、`os.stat`、`os.listdir`、`os.walk`、`time.sleep`、…），每个包一层 guard。
2. guard 被调用时判定：① 当前是否在运行中的 asyncio 事件循环里（`asyncio.get_running_loop()`，不在则放行）；② 调用栈是否经过 `deerflow.*`（遍历栈帧 `__name__`）。两者同时满足 → 抛 `BlockingError`。
3. 退出时（含异常）在 finally 里还原所有 patch。

效果：把「同步阻塞 IO 不能跑在事件循环里、尤其不能发自 deerflow 业务代码」变成可断言的事实。第三方库自身的同步 IO 不算违规（栈不经过 `deerflow`）。

### 3.2 踩坑：gate 会误伤「在 gate 内首次 import 的模块」

gate 在整个 `pytest_runtest_protocol`（setup+call+teardown）激活。如果被测模块的 import 写在 **async 测试函数体内**（call 阶段，gate 已激活），首次 import 会触发模块体执行 → 模块体的进一步 import 让 importlib 调 `os.stat` → 此时栈顶正执行该 deerflow 模块体 → gate 判定违规 → 抛 `BlockingError`，但它在 importlib 内部被吞，最终报成 `ModuleNotFoundError`，极具迷惑性。

**约定**：被测的生产模块一律在**测试模块顶部**（collect 阶段，gate 未激活）import，gate 只负责观察「运行时」的同步 IO 调用。deer-flow 的 gate 测试也遵循这一点。

---

## 4. hermetic 测试约定（参考 deer-flow）

mini 的旧测试普遍 `print` 调试 + 依赖真实 API key + `if __name__ == "__main__"` + `try/except` 吞错误，离开特定环境就跑不了。重写后对齐 deer-flow 的 hermetic 风格：

### 4.1 风格清单

- 模块级 docstring（一句话定位 + hermetic 说明）。
- `from __future__ import annotations`。
- 每个测试聚焦一个行为，`test_<behavior>_<condition>` 命名，函数有简短 docstring。
- 用 `pytest.raises` + `assert`，**不用 print、不用 try/except 吞错误、不留 `if __name__` 块**。
- 隔离手段：`monkeypatch` 桩化外部依赖、显式注入配置、用标准库模块代替真实 provider。
- 集成冒烟（需 config.yaml/网络/API key）用 `pytest.skip()` 守卫，或单独放 `*_live.py`。

### 4.2 关键注入点（mini 现有 API 都支持 hermetic）

| 被测函数 | hermetic 注入方式 |
|----------|-------------------|
| `get_available_tools(*, app_config=)` | 传 `AppConfig(tools=[])` 只取内置工具；配置工具用 `monkeypatch` 桩掉 `resolve_variable` |
| `build_middlewares(*, app_config=)` | 传 `AppConfig(title={"enabled": False})` 等控制开关；不读全局 config |
| `create_deerflow_agent(model=, tools=, middleware=)` | 传 `model=object()` + `monkeypatch` 把 `langchain.agents.create_agent` 换成记录器 |
| `make_lead_agent(config)` | `monkeypatch` 桩化 `get_app_config`/`create_chat_model`/`get_available_tools`/`build_middlewares`/`create_agent` |
| `resolve_variable` / `resolve_class` | 成功路径用 `json:loads` 等标准库；缺包路径 `monkeypatch` 桩掉 `resolver.importlib` |
| `AppConfig` / `ModelConfig` | 直接构造（pydantic），不读文件；`_expand_env_vars` 用 `monkeypatch.setenv` |

### 4.3 两个可复用的 hermetic 模式

**模式 A：桩化 `create_agent`，验证组装逻辑**（不真正编译图）：

```python
def test_create_deerflow_agent_passes_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(factory_module, "create_agent", lambda **k: captured.update(k) or "g")
    create_deerflow_agent(model=object(), tools=["t1"])
    assert captured["tools"] == ["t1"]
```

**模式 B：用标准库代替真实 provider**（reflection 测试）：

```python
def test_resolve_variable_loads_attribute():
    assert resolve_variable("json:loads") is json.loads
```

---

## 5. 当前测试状态

所有测试已重写为 hermetic（reflection / config / tools / middlewares / agent / agent_with_middlewares / model），加上 M-build 的 boundary + blocking-io gate 测试，**全量绿**。

> 各测试文件的最新状态与通过数见 [todo.md](todo.md)——不在本文维护易过时的状态快照。
>
> 历史注记：test_model.py 曾有 2 处 `reasoning_effort` 失败（测试缺 `supports_reasoning_effort=True`，factory 行为与 deer 一致无需改），现已修复。

---

## 6. 一句话带走

> **测试跑不通时，先怀疑环境（sys.path / .pth / site.py），再怀疑代码。** 这次卡住的根本不是业务逻辑，而是 Python 3.14 一处不起眼的 site.py 改动 × uv 的 hidden flag。诊断靠 `ls -lO` + `inspect.getsource(site.addpackage)` 两条命令闭环，修复靠 conftest 显式注入 sys.path 一劳永逸。
