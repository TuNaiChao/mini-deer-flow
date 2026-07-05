# 3. config.md — 配置系统（强类型子配置 + 热重载 + $VAR 展开）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（字段 / 函数 / 行号以此为准）。

> **一句话定位**：config 是 mini 的「控制面板」——你在一个文本文件 `config.yaml` 里写「用哪个模型、开不开记忆、数据库存哪」，代码就能读到一个**带类型、带默认值、空着也能跑**的配置对象 `AppConfig`，而不用改一行代码。它是**几乎所有模块的地基**：models / memory / sandbox / persistence / tools 全都从配置读参数。

> 配套代码：[config/app_config.py](../backend/packages/harness/deerflow/config/app_config.py) · [config/paths.py](../backend/packages/harness/deerflow/config/paths.py) · [config/reload_boundary.py](../backend/packages/harness/deerflow/config/reload_boundary.py) · [config/__init__.py](../backend/packages/harness/deerflow/config/__init__.py) + 19 个 `<功能>_config.py` 子配置。配置文件实例见 [../backend/config.yaml](../backend/config.yaml) + [../config.example.yaml](../config.example.yaml)。

## 学完这篇你能回答什么（learning outcomes）

- 为什么 agent / 后端项目要把配置做成**强类型**（pydantic model）而不是 `dict`？换来什么（拼错立刻报 / 默认集中 / IDE 补全 / 类型校验）？
- **「空配置必须能启动」**（`database` 默认 `memory`）为什么是一条设计铁律？它换来了什么（开箱即跑、CI 不依赖外部）？
- **热重载**怎么实现（mtime 比较）？为什么有些字段（`database` / `checkpointer` / `sandbox`…）改了**必须重启**（startup-only 边界）？
- 为什么 **async 服务里**，`get_app_config()` 命中缓存时要**跳过 mtime stat**（blocking-IO 红线 carve-out）？
- 怎么把敏感信息（API key）放 `.env`、配置放 `config.yaml` 提交 git（`$VAR` 展开）？

> 这些都是后端 / agent 工程面试的高频点——「配置系统怎么设计」「强类型 vs 弱类型」「热重载边界」。

---

## 1. 为什么需要它

### 1.1 痛点：`dict` 配置的坑

早期 mini 的 `AppConfig` 把 `memory` / `sandbox` / `title` 等子配置都存成 `dict[str, Any]`。这有几个真实的坑：

| 场景 | dict 的坏处 | 强类型的好处 |
|------|-------------|----------------------|
| 拼错字段名 | `cfg.memory["enbled"]`（拼错）静默返回 None，bug 难找 | `cfg.memory.enbled` 直接 `AttributeError`，立刻发现 |
| 取值兜底 | 到处写 `cfg.memory.get("enabled", True) if isinstance(...dict) else True` | 直接 `cfg.memory.enabled`，IDE 还能补全 |
| 默认值散落 | 每个「读者」各写各的默认值，不一致 | 默认值**定义在子配置里**，单一真相 |
| 类型错 | `max_facts` 写成字符串不会报错，运行时崩 | pydantic 校验类型，加载时就报 |

config 模块的核心工作之一就是「**把所有子配置从 `dict` 升级成 pydantic BaseModel**」——拼错立刻报、默认值集中、IDE 补全、类型校验。

### 1.2 痛点：空配置必须能启动

mini 要保证「**一个空的 `config.yaml` 也能跑起来**」（用内存模式，不连数据库）。这要求每个子配置都有**安全默认值**：`database.backend` 默认 `"memory"`、`memory.enabled` 默认 `True`、`sandbox` 默认 `LocalSandboxProvider`……这样什么都不配也能启动，不会被某个必填字段卡住——开箱即跑、CI 不依赖外部服务。

---

## 2. 零基础先读：这些名词是什么

> 不熟悉 YAML / pydantic 的话，先读这一节。

### YAML 是什么

**YAML** 是一种「用缩进表示层级」的文本数据格式，`config.yaml` 就是它写的。长这样：

```yaml
# 顶层是键值对
log_level: info

# 缩进表示嵌套
memory:
  enabled: true
  max_facts: 100

# 列表用减号
models:
  - name: deepseek
    model: deepseek-chat
```

好处：**人类好读好写**（比 JSON 友好），又结构化。`#` 开头是注释。mini 用它当主配置文件。

### pydantic 是什么

**pydantic** 是 Python 的数据校验库。你定义一个类继承 `BaseModel`，声明字段和类型，pydantic 就帮你：

- **校验**：传错类型直接报错（如 `max_facts: int` 传字符串会拒绝）；
- **给默认值**：`max_facts: int = 100` 没传时自动用 100；
- **转换**：把 `dict`（如从 YAML 读来的）自动变成对象（`{"enabled": true}` → `MemoryConfig(enabled=True)`）。

config 模块的核心就是「**把所有子配置写成 pydantic BaseModel**」，享受上面三个好处。

### 「强类型」是什么

「强类型」= 每个数据有**明确的类型定义**，校验器帮你抓错。对比：

- **弱类型**（dict）：`cfg["memory"]["enabled"]`——键名拼错、类型乱传都不报错，运行时才崩。
- **强类型**（pydantic model）：`cfg.memory.enabled`——拼错属性名立刻报，类型也校验。

### 单例 / 热重载是什么

- **单例**：整个程序只创建**一个**配置对象，谁要用都拿同一个（避免重复读文件、配置不一致）。
- **热重载**：mini 盯着 `config.yaml` 的修改时间（mtime），**文件一改，下次读配置自动重新加载**，不用重启进程。你改完 yaml 存盘，下一条消息就生效（对「运行期可变」字段而言）。

### 环境变量 / `$VAR` 是什么

**环境变量**是操作系统层面的「键值对」（如 `DEEPSEEK_API_KEY=sk-xxx`），常用来放**敏感信息**（密钥不该写进 yaml 提交到 git）。config.yaml 里以 `$` 开头的值会被**展开**成对应环境变量：

```yaml
models:
  - api_key: $DEEPSEEK_API_KEY   # 加载时变成 .env / 环境里的真实密钥
```

这样 yaml 可以提交 git，密钥放 `.env`（gitignore）。

---

## 3. 整体结构：它在系统里的位置

config 是**最底层、无依赖**的一层——但后面几乎所有模块都读它：

```
config.yaml (文本，人写)
    │  get_app_config() 加载 + 展开 $VAR
    ▼
AppConfig (总配置对象，单例 + mtime 热重载)
    │  字段是各种子配置对象（pydantic BaseModel）
    ├─── models: list[ModelConfig]      ──→ models（→ #6）/ agent 装配（→ #25）
    ├─── memory: MemoryConfig           ──→ memory（→ #18）
    ├─── sandbox: SandboxConfig         ──→ sandbox（→ #13）
    ├─── database: DatabaseConfig       ──→ persistence（→ #7）/ checkpointer（→ #8）
    ├─── title/loop_detection/token_budget/...  ──→ middlewares（→ #24）
    └─── ...（共 19 个子配置对象）
    │
    └── paths.resolve_path / runtime_home / get_paths ──→ 所有需要数据目录的模块
    └── reload_boundary ──→ 标记哪些字段改了要重启
```

config 排在 **Phase 0 地基**，正因为它没有依赖，但后面所有模块都依赖它。

---

## 4. 核心概念

### 4.1 三层结构：`config.yaml` → `AppConfig` → 子配置

`AppConfig`（[app_config.py:42](../backend/packages/harness/deerflow/config/app_config.py#L42)）是**根**，它的每个字段都是一个**子配置对象**（pydantic BaseModel），各自有字段和默认值。从 YAML 的 `dict` 自动转换而来。

### 4.2 子配置（AppConfig 直接持有 19 个对象 + models/tools 列表）

每个子配置管一块功能，文件命名 `<功能>_config.py`。按职责分组（默认值见 [../config.example.yaml](../config.example.yaml)）：

| 分组 | 子配置 | 管什么 |
|------|--------|--------|
| **模型 / 工具** | `TokenUsageConfig` | token 用量跟踪 |
| | `ToolOutputConfig` | 工具输出预算保护（防超大输出爆 context） |
| | `ToolSearchConfig` | 工具搜索 / 延迟加载 |
| | `UploadsConfig` | 文件上传 + markitdown 转换（→ #23） |
| **技能** | `SkillsConfig` | 技能目录（→ #19） |
| | `SkillEvolutionConfig` | agent 自管理技能演进 |
| **质量 / 辅助** | `TitleConfig` | 自动标题生成 |
| | `SummarizationConfig` | 对话摘要 |
| | `MemoryConfig` | 记忆系统（→ #18） |
| | `SubagentsAppConfig` | 子代理（→ #15） |
| | `LoopDetectionConfig` | 循环检测中间件 |
| | `SafetyFinishReasonConfig` | provider 安全 finish_reason 拦截 |
| | `TokenBudgetConfig` | 单 run token 预算强制 |
| | `CircuitBreakerConfig` | LLM 调用熔断（连续失败短路） |
| **基础设施（需重启）** | `SandboxConfig` | 沙箱 provider（默认 `LocalSandboxProvider`） |
| | `DatabaseConfig` | 统一数据库后端（默认 `memory`） |
| | `RunEventsConfig` | 运行事件存储（默认 `memory`） |
| | `CheckpointerConfig \| None` | LangGraph 状态持久化（None→用 database 派生） |
| | `StreamBridgeConfig \| None` | SSE 流桥（None→内存默认） |

> 另有两个 config 层文件**不在 `AppConfig` 字段里**、独立加载：[extensions_config.py](../backend/packages/harness/deerflow/config/extensions_config.py)（MCP + 技能启用，是 dataclass，→ #20）和 [agents_config.py](../backend/packages/harness/deerflow/config/agents_config.py)（自定义 agent，→ #17）。`tracing` 当前是 `dict`（环境变量驱动，→ #16）。

### 4.3 `config_version`（版本检查）

`config.yaml` 顶部有个 `config_version: N`（[app_config.py:48](../backend/packages/harness/deerflow/config/app_config.py#L48)）。启动时 mini 拿用户的版本和 `config.example.yaml`（模板）的版本比——**用户版本更低就告警**（提醒「配置过时了，合并新字段」）。缺失视为版本 0。这样改了 schema（加了字段）能提醒用户升级配置。

### 4.4 `startup-only`（热重载边界）

热重载只对**运行期可变**字段生效。但有些**基础设施**字段（数据库引擎、checkpointer、沙箱 provider）在启动时被「捕获」成对象，改它们**必须重启进程**。这类字段在 [reload_boundary.py](../backend/packages/harness/deerflow/config/reload_boundary.py) 登记（[第 29–36 行](../backend/packages/harness/deerflow/config/reload_boundary.py#L29)），其描述以 `startup-only:` 开头：

```python
# reload_boundary.py —— 登记的 6 个需重启字段
STARTUP_ONLY_FIELDS = {
    "database":      "init_engine_from_config() 在启动时运行一次；SQLAlchemy 引擎持有连接池，不会因 config.yaml 编辑而重建。",
    "checkpointer":  "make_checkpointer() 在启动时绑定持久化 checkpointer 一次...",
    "run_events":    "make_run_event_store() 在启动时选定实现并冻结...",
    "stream_bridge": "make_stream_bridge() 在启动时构造流桥单例一次。",
    "sandbox":       "get_sandbox_provider() 缓存 provider 单例...",
    "log_level":     "apply_logging_level() 仅在启动时运行...",
}
```

其余字段（models / memory / title …）都是运行期可变，改完存盘即生效。

### 4.5 paths：路径解析（替代 runtime_paths）

[paths.py](../backend/packages/harness/deerflow/config/paths.py) 提供运行时路径 API（mini 用它**替代**上游的 `runtime_paths`，新代码不得 import `runtime_paths`）：

- `resolve_path(value, *, base=None)`（[第 107 行](../backend/packages/harness/deerflow/config/paths.py#L107)）：绝对路径原样返回，相对路径相对项目根解析。
- `project_root()`（[第 81 行](../backend/packages/harness/deerflow/config/paths.py#L81)）：运行时项目根（优先 `DEER_FLOW_PROJECT_ROOT` 环境变量，否则当前目录）。
- `runtime_home()`（[第 97 行](../backend/packages/harness/deerflow/config/paths.py#L97)）：可写状态目录（优先 `DEER_FLOW_HOME`，否则 `{project_root}/.deer-flow`）——即 **base_dir**。
- `get_paths() -> Paths`（[第 320 行](../backend/packages/harness/deerflow/config/paths.py#L320)）：返回 `Paths` 对象（含 `base_dir` / `users_dir` / 各种 per-user / per-thread 目录方法）。memory / sandbox / persistence / uploads 都用它的方法拼目录——**它是数据目录布局的唯一真相源**。

> 注意两套「根」职责不同（[paths.py 顶部注释](../backend/packages/harness/deerflow/config/paths.py#L10)）：`PROJECT_ROOT`（= backend 目录，找 `pyproject.toml` 定位，用于 config.yaml 路径）vs `project_root()`（运行时根，env 或 cwd，用于数据目录）。别混。

---

## 5. 代码走读：重要函数逐个讲

### 5.1 `get_app_config()` —— 单例 + mtime 热重载 + 事件循环 carve-out（[第 281 行](../backend/packages/harness/deerflow/config/app_config.py#L281)）

这是最常被调用的入口。逻辑（简化）：

```python
_app_config: AppConfig | None = None     # 模块级单例
_config_mtime: float | None = None

def get_app_config() -> AppConfig:
    global _app_config, _config_mtime

    # ① 已加载 + 当前在事件循环里 → 直接返缓存，不做 mtime stat（blocking-IO 红线）
    if _app_config is not None and _running_in_event_loop():
        return _app_config

    config_path = get_config_file()
    current_mtime = config_path.stat().st_mtime if config_path.exists() else None

    # ② 同步上下文下的热重载：mtime 没变就返缓存
    if _app_config is not None and current_mtime == _config_mtime:
        return _app_config

    # ③ 加载 .env + 解析 YAML（展开 $VAR）→ 构造 AppConfig
    load_dotenv(get_env_file())
    yaml_config = load_config_from_yaml(config_path)
    _app_config = AppConfig(**yaml_config)
    _config_mtime = current_mtime
    return _app_config
```

**为什么要 carve-out（第 ① 分支）？** mtime 检查要调 `config_path.stat()`（同步文件 IO）。在 `langgraph dev` 运行期，`make_lead_agent` 每个 run 都会调 `get_app_config()`——若每次都在事件循环里 `stat`，会触发 blocking-IO gate（[build.md §4.2](build.md#42-blocking-io-gate--blocking_io_runtimepy)）。所以**已加载且在事件循环里时，直接返缓存**。运行期热重载改由显式 `reload_config()` 或重启 dev server 触发。启动期（lifespan，同步上下文）首次加载不受影响，且运行期进入时几乎总命中缓存。这是「**同步阻塞 IO 不能跑在事件循环里**」这条约束在配置层的具体落地（与 [testing-setup.md §4](testing-setup.md#4-blocking-io-gate测试作者要知的用法与坑) 的 gate 配套）。

### 5.2 `_expand_env_vars()` —— `$VAR` 递归展开（[第 236 行](../backend/packages/harness/deerflow/config/app_config.py#L236)）

```python
def _expand_env_vars(value):
    if isinstance(value, str):
        return re.sub(r"\$(\w+)|\$\{(\w+)\}",
                      lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value
```

递归遍历 dict / list / str，把 `$VAR` 和 `${VAR}` 替换成 `os.environ` 的值。**未设置的变量保留占位文本**（`$FOO` 原样留下，不报错）——注意只匹配 `\w+`（字母数字下划线）。

### 5.3 `load_config_from_yaml()` —— 读 YAML + 展开（[第 252 行](../backend/packages/harness/deerflow/config/app_config.py#L252)）

`open` → `yaml.safe_load` → `_expand_env_vars`。文件不存在时打印警告并返回 `{}`（不崩）。注意它用的是同步 `open()`，所以**只在同步上下文调**（运行期走 `get_app_config` 的缓存分支）。

### 5.4 `AppConfig` 的两个 validator

- **`_coerce_null_list_sections`**（[第 163 行](../backend/packages/harness/deerflow/config/app_config.py#L163)，`mode="before"`）：YAML 里把一个列表节**全注释掉**（如 `models:` 下面只剩注释）会让 PyYAML 解析成 `None`，pydantic 会报晦涩的 `Input should be a valid list`。这个 validator 把 `None` → `[]`，让「全注释」也能正常启动。
- **`_build_name_indexes`**（[第 174 行](../backend/packages/harness/deerflow/config/app_config.py#L174)，`mode="after"`）：校验后一次性把 `models` / `tools` 列表预建成 name→config 的 dict（见 §6.3）。

### 5.5 `get_model_config` / `get_tool_config` —— O(1) 查表（[第 198 / 213 行](../backend/packages/harness/deerflow/config/app_config.py#L198)）

```python
def get_model_config(self, name: str | None) -> ModelConfig | None:
    if not self.models: return None
    if name is None: return self.models[0]        # None → 默认模型（第一个）
    return self._models_by_name.get(name)          # O(1) dict 查
```

`get_tool_config` 同理（返回原始 dict，因为 `tools` 当前是 `list[dict]`）。

---

## 6. 设计权衡与踩坑

### 6.1 为什么 `database` 默认 `memory`？

`DatabaseConfig.backend` 默认 `"memory"`，不是 `"sqlite"`。这样**空配置不依赖任何文件 / 数据库**就能启动（开发 / 测试 / CI 友好）。要持久化时显式配 `backend: sqlite`。

### 6.2 为什么 `DatabaseConfig` 派生 `sqlite_path` / `app_sqlalchemy_url`？

用户只配 `backend` + `sqlite_dir`，系统**自动派生**出 checkpointer 和 app 各自要用的具体路径 / URL：

```python
db = DatabaseConfig(backend="sqlite", sqlite_dir="/tmp/data")
db.sqlite_path          # /tmp/data/deerflow.db（checkpointer + app 共用）
db.app_sqlalchemy_url   # sqlite+aiosqlite:////tmp/data/deerflow.db
```

好处：用户配一处，派生多处，不会 checkpointer 和 app 路径对不上。

### 6.3 为什么 `get_model_config` / `get_tool_config` 要预建索引？

config.yaml 里 `models: [...]` / `tools: [...]` 是**列表**，每项有 `name`，代码常需「按名查某项」。朴素实现是**线性扫**（`for m in models: if m.name == name`）。

问题：这两个 getter 在**热路径**——`get_tool_config` 在每个 community 工具（web_search 等）每次调用时被读 2-3 次；`get_model_config` 在每次 agent 构建都调。一次对话可能触发几十次扫表，结果每次都一样（config 在两次 reload 间不变），**累积是纯浪费**。

**修法**：`_build_name_indexes`（[第 174 行](../backend/packages/harness/deerflow/config/app_config.py#L174)）在 `@model_validator(mode="after")` 里**一次性**建 name→config dict。几个关键设计点：

- **`PrivateAttr`**：索引是 `_models_by_name: dict = PrivateAttr(...)`——pydantic 的 `PrivateAttr` 让它**不参与序列化**（`model_dump()` 里不出现），因为它是派生缓存，不是配置数据。
- **`setdefault` 保首条**：用户写了两个同名 model 时，保留**先出现**的那个——与旧 `for` 循环的「首匹配」语义一致，换实现不改行为。
- **`mode="after"`**：在所有字段校验通过后才建表，保证读到的列表是已规整过的（`None` 已被 §5.4 归一成 `[]`）。
- **reload 自动刷新**：`get_app_config()` 检测到 yaml 变了会**新构一个 `AppConfig`**，新实例重新跑 `_build_name_indexes`，索引自然刷新——旧实例的索引不污染新实例。

> 经典「**空间换时间**」：多用一个 dict 的内存（几条记录），换掉热路径上的重复线性扫。还示范了 pydantic「`PrivateAttr` + `model_validator(after)` 校验后派生私有状态」的常见组合。

### 6.4 为什么 `format_field_description` 对未登记字段 raise `KeyError`？

`reload_boundary.format_field_description("xxx")`（[第 53 行](../backend/packages/harness/deerflow/config/reload_boundary.py#L53)）如果 `"xxx"` 没登记，直接 `KeyError` 而非静默返回占位符。**有意为之**——静默会让笔误绕过「登记表 ↔ schema 描述一一对应」的检查；raise 让笔误立刻暴露。

### 6.5 为什么 mini 不给子配置配模块级单例函数？

上游给 memory / title / checkpointer 等都配了**模块级单例**（`get_memory_config()` 等），因为有些代码路径直接调这些 getter 而非从 `app_config` 读。mini **统一从 `app_config` 读**（`cfg.memory.enabled`），不需要这些单例——少一层间接、少一处缓存一致性坑。所以 mini 的子配置只是**纯 pydantic model**，没有单例函数。

### 6.6 热重载靠 mtime，但有边界

`get_app_config()`（同步上下文下）每次调用比较 `config.yaml` 的 mtime 和缓存值，**变了就重新加载**。这让配置读数与 yaml 编辑保持一致，无需手动重启。但 `startup-only` 字段（§4.4）即便重载了也不会真正生效（引擎 / 单例已建好），需进程重启——这就是「热重载边界」。

> 内部追溯：本文的设计约束在上游工程记录里分别编号为红线 #25（空配置可启动）、#2615（事件循环 blocking-IO carve-out）、#3688（name 索引优化）。这些编号仅作内部对照，不影响理解。

---

## 7. 配置与用法

### 7.1 空配置启动

```python
from deerflow.config import AppConfig, get_app_config

cfg = AppConfig()                          # 直接构造（测试用，不读文件）
assert cfg.database.backend == "memory"    # 内存模式，能跑

cfg = get_app_config()                     # 从 config.yaml 加载（生产用）；文件不存在/为空也安全
```

### 7.2 在 config.yaml 里覆盖子配置

```yaml
# config.yaml
config_version: 1
database:
  backend: sqlite
  sqlite_dir: .deer-flow/data
memory:
  enabled: true
  max_facts: 200
title:
  max_words: 8
models:
  - name: deepseek
    use: langchain_deepseek:ChatDeepSeek
    model: deepseek-chat
    api_key: $DEEPSEEK_API_KEY
```

```python
cfg = get_app_config()
cfg.database.backend      # "sqlite"
cfg.memory.max_facts      # 200
cfg.title.max_words       # 8
```

### 7.3 取派生路径（persistence / checkpointer 用）

```python
cfg = get_app_config()
db = cfg.database
if db.backend == "sqlite":
    engine_url = db.app_sqlalchemy_url      # sqlite+aiosqlite:///…
```

### 7.4 单元测试里 hermetic 注入（不读磁盘）

```python
from deerflow.config import AppConfig, MemoryConfig

cfg = AppConfig(memory=MemoryConfig(enabled=False), database={"backend": "sqlite"})
assert cfg.memory.enabled is False
```

### 7.5 路径解析（memory / sandbox 用）

```python
from deerflow.config import get_paths, resolve_path

base_dir = get_paths().base_dir                       # .deer-flow（或 DEER_FLOW_HOME）
memory_file = get_paths().user_memory_file(user_id)   # {base_dir}/users/{user_id}/memory.json
config_path = resolve_path("config.yaml")             # 相对项目根
```

---

## 8. 与其它模块的关系

config 是**最底层、无依赖**的一层（仅 pydantic + yaml + 标准库）。**几乎所有模块**读配置都走 `AppConfig`：

- `models`（→ #6）读 `ModelConfig` / `cfg.models`；
- `memory`（→ #18）读 `MemoryConfig` + `paths.runtime_home`；
- `sandbox`（→ #13）读 `SandboxConfig` + `paths` 的线程目录；
- `persistence`（→ #7）读 `DatabaseConfig` 派生路径；
- `middlewares`（→ #24）读 `title.enabled` / `memory.enabled` / `loop_detection` / `token_budget` 等；
- `skills`（→ #19）读 `SkillsConfig.get_skills_path`；
- `uploads`（→ #23）读 `UploadsConfig` + `paths.sandbox_uploads_dir`。

---

## 9. 常见问题 / 排错

**Q: 改了 `config.yaml` 但没生效？**
分两种：① **运行期可变字段**（memory / title / models…）：下一条消息自动生效（mtime 热重载）。确认你改的是 `get_config_file()` 实际读取的那个文件（项目根 `config.yaml`，不是 backend 下的）。② **startup-only 字段**（database / checkpointer / sandbox / run_events / stream_bridge / log_level）：必须**重启进程**——它们在启动时被捕获成对象。看字段描述是否带 `startup-only:` 前缀（§4.4）。

**Q: 启动报 `Input should be a valid list`？**
多半是 `models:` / `tools:` 下面全注释成 `None`。`_coerce_null_list_sections` 已把 `None` → `[]`，正常不会遇到。若仍遇到，确认你跑的是新版 app_config（有该 validator）。

**Q: `AppConfig()` 空构造报「sandbox.use 必填」？**
当前不会——`AppConfig()` 给 sandbox 一个 default_factory（默认 `LocalSandboxProvider`），空构造安全（§1.2）。

**Q: `$VAR` 没展开 / 报环境变量未找到？**
`_expand_env_vars` 在变量**未设置时保留占位文本**（`$FOO` 原样留下，不报错）。如果期望它变成真实值却没变，确认：① 变量名拼写对（区分大小写）；② 变量确实设了（`.env` 被 `load_dotenv` 加载，或 shell 里 export 了）。注意只匹配 `\w+`（字母数字下划线）。

**Q: `format_field_description("xxx")` 报 KeyError？**
`"xxx"` 没在 `STARTUP_ONLY_FIELDS` 登记。这是有意的防笔误机制（§6.4）——只有真正需重启的字段才该用这个前缀。把字段加进 [reload_boundary.py](../backend/packages/harness/deerflow/config/reload_boundary.py) 的登记表，或别给普通字段套 `startup-only:`。

---

## 小结

config 的精髓是「**把配置从散落的 dict 升级成带类型、带默认、带边界的强类型系统**」。记四件事：

1. **强类型子配置**：19 个 pydantic model，拼错立刻报、默认值集中、IDE 补全。
2. **空配置可启动**：所有字段有安全默认，`database` 默认 `memory`。
3. **热重载有边界**：运行期字段改完即生效（mtime）；`startup-only` 字段（6 个）需重启——看 [reload_boundary.py](../backend/packages/harness/deerflow/config/reload_boundary.py) 登记。
4. **paths 是数据目录唯一真相源**：新代码用 `resolve_path` / `runtime_home` / `get_paths`，不要 import `runtime_paths`。

> 下一步：[#4 utils.md](utils.md)（公共工具）→ [#5 user_context.md](user_context.md)（用户隔离），Phase 0 地基收尾。
