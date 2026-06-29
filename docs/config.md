# 3. config.md — 配置系统（类型化配置 + 热重载）

> 对应模块：**M0**（Phase 0，地基）
> 源码：`backend/packages/harness/deerflow/config/`（`app_config.py` + 17 个子配置 + `paths.py` + `reload_boundary.py`）

> **Phase 0 全维重审（2026-06-28）**：逐文件 diff 最新上游，剥 docstring 后判逻辑差。**M0 config**
> 仅 1 项真实漂移（已补）+ 1 项有意结构选择（沿用）。**补 #3688**：`AppConfig.get_model_config` /
> `get_tool_config` 旧版是 O(n) 线性扫（`for m in models` / `next(t for t in tools)`），上游已用
> `PrivateAttr` dict + `_build_name_indexes` model_validator 改 O(1)。这两个 getter 在热路径
> （`get_tool_config` 每次 community 工具调用跑 2-3 次、`get_model_config` 每次 agent 构建跑数次），
> 现 port 为 name→config dict 查表，`setdefault` 保重名首条（与旧首匹配语义一致）。详见 §5.7。
> **沿用 §5.5**：mini 不 port 上游 `checkpointer_config` 等模块级单例层（`get_checkpointer_config` /
> `ensure_config_loaded` / `load_xxx_from_dict`）——mini 统一从 `app_config` 读（M19 已认定）。
> 其余子配置（`model_config` / `checkpointer_config` / `database_config` 等）剥 docstring 后**字段与默认
> 完全等价**（mini 用「属性 + docstring」，上游用 `Field(description=...)`，行为同）。无需补丁。

---

## 1. 一句话定位

**config 是 mini 的「控制面板」**：你在一个文本文件 `config.yaml` 里写「用哪个模型、开不开记忆、数据库存哪」，代码就能读到一个**带类型、带默认值、空着也能跑**的配置对象 `AppConfig`，而不用改一行代码。

> 这是**几乎所有模块的地基**——models / memory / sandbox / persistence / tools 全都从配置读参数。把配置做对（强类型、有默认、热重载边界清晰），后面模块才能稳。

---

## 2. 为什么需要它

### 2.1 痛点：`dict` 配置的坑

M0 之前，mini 的 `AppConfig` 把 `memory` / `sandbox` / `title` 等子配置都存成 `dict[str, Any]`。这有几个真实的坑：

| 场景 | dict 的坏处 | 强类型（M0 后）的好处 |
|------|-------------|----------------------|
| 拼错字段名 | `cfg.memory["enbled"]`（拼错）静默返回 None，bug 难找 | `cfg.memory.enbled` 直接 `AttributeError`，立刻发现 |
| 取值兜底 | 到处写 `cfg.memory.get("enabled", True) if isinstance(cfg.memory, dict) else True` | 直接 `cfg.memory.enabled`，IDE 还能补全 |
| 默认值散落 | 每个「读者」各写各的默认值，不一致 | 默认值**定义在子配置里**，单一真相 |
| 类型错 | `max_facts` 写成字符串不会报错，运行时崩 | pydantic 校验类型，加载时就报 |

M0 把所有子配置从 `dict` 升级成 **pydantic BaseModel**——**拼错立刻报、默认值集中、IDE 补全、类型校验**。

### 2.2 痛点：空配置必须能启动（红线 #25）

mini 要保证「**一个空的 `config.yaml` 也能跑起来**」（用内存模式，不连数据库）。这要求每个子配置都有**安全默认值**：`database.backend` 默认 `"memory"`、`memory.enabled` 默认 `True`、`sandbox` 默认 `LocalSandboxProvider`……这样什么都不配也能启动，不会被某个必填字段卡住。

---

## 3. 零基础先读：这些名词是什么

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

它的好处：**人类好读好写**（比 JSON 友好），又结构化。`#` 开头是注释。mini 用它当主配置文件。

### pydantic 是什么

**pydantic** 是 Python 的数据校验库。你定义一个类继承 `BaseModel`，声明字段和类型，pydantic 就帮你：
- **校验**：传错类型直接报错（如 `max_facts: int` 传字符串会拒绝）；
- **给默认值**：`max_facts: int = 100` 没传时自动用 100；
- **转换**：把 `dict`（如从 YAML 读来的）自动变成对象（`{"enabled": true}` → `MemoryConfig(enabled=True)`）。

M0 的核心工作就是「**把所有子配置写成 pydantic BaseModel**」，享受上面三个好处。

### 「强类型」是什么

「强类型」= 每个数据有**明确的类型定义**，编译器/校验器帮你抓错。对比：
- **弱类型**（dict）：`cfg["memory"]["enabled"]`——键名拼错、类型乱传都不报错，运行时才崩。
- **强类型**（pydantic model）：`cfg.memory.enabled`——拼错属性名立刻报，类型也校验。

### 单例 / 热重载是什么

- **单例**：整个程序只创建**一个**配置对象，谁要用都拿同一个（避免重复读文件、配置不一致）。
- **热重载**：mini 盯着 `config.yaml` 的修改时间（mtime），**文件一改，下次读配置自动重新加载**，不用重启进程。你改完 yaml 存盘，下一条消息就生效（对「运行期可变」字段而言）。

### 环境变量 / `$VAR` 是什么

**环境变量**是操作系统层面的「键值对」（如 `OPENAI_API_KEY=sk-xxx`），常用来放**敏感信息**（密钥不该写进 yaml 提交到 git）。config.yaml 里以 `$` 开头的值会被**展开**成对应环境变量：

```yaml
models:
  - api_key: $DEEPSEEK_API_KEY   # 加载时变成 .env / 环境里的真实密钥
```

这样 yaml 可以提交 git，密钥放 `.env`（gitignore）。

---

## 4. 核心概念

### 4.1 三层结构：`config.yaml` → `AppConfig` → 子配置

```
config.yaml (文本，人写)
    │  get_app_config() 加载 + 展开 $VAR
    ▼
AppConfig (总配置对象，单例)
    │  字段是各种子配置对象
    ├── memory: MemoryConfig(enabled=True, max_facts=100, ...)
    ├── database: DatabaseConfig(backend="memory", ...)
    ├── title: TitleConfig(enabled=True, max_words=6, ...)
    ├── sandbox: SandboxConfig(use="...LocalSandboxProvider", ...)
    ├── models: list[ModelConfig]
    └── ...（共 17 个子配置 + models/tools 列表）
```

`AppConfig` 是**根**，它的每个字段（`memory` / `database` / `title` …）都是一个**子配置对象**（pydantic BaseModel），各自有字段和默认值。

### 4.2 子配置（17 个）

每个子配置管一块功能，文件命名 `<功能>_config.py`：

| 子配置 | 管什么 | 关键默认 |
|--------|--------|----------|
| `DatabaseConfig` | 数据库后端 | `backend="memory"`（红线 #25）|
| `MemoryConfig` | 记忆系统 | `enabled=True`、`max_facts=100` |
| `TitleConfig` | 自动标题 | `enabled=True`、`max_words=6` |
| `SandboxConfig` | 代码沙箱 | `use=LocalSandboxProvider` |
| `LoopDetectionConfig` | 循环检测 | `enabled=True`、`hard_limit=5` |
| `SubagentsAppConfig` | 子代理 | `enabled=True`、`max_concurrent=3` |
| `SummarizationConfig` | 对话摘要 | `enabled=False` |
| `CheckpointerConfig` | LangGraph 状态持久化 | `None`（用 database 派生）|
| `RunEventsConfig` | 运行事件存储 | `backend="memory"` |
| `StreamBridgeConfig` | SSE 流桥 | `None`（内存默认）|
| `TokenUsageConfig` | token 用量 | `enabled=True` |
| `ToolOutputConfig` | 工具输出预算 | `externalize_min_chars=12000` |
| `ToolSearchConfig` | 延迟工具加载 | `enabled=False` |
| `SkillsConfig` | 技能目录 | `path` 默认项目根 `skills` |
| `SkillEvolutionConfig` | agent 改技能 | `enabled=False` |
| `SafetyFinishReasonConfig` | 安全 finish_reason 拦截 | `enabled=True` |

### 4.3 `config_version`（版本检查）

`config.yaml` 顶部有个 `config_version: N`。启动时 mini 会拿用户的版本和 `config.example.yaml`（模板）的版本比——**用户版本更低就告警**（「你的配置过时了，跑 `make config-upgrade` 合并新字段」）。缺失视为版本 0。这样改了 schema（加了字段）能提醒用户升级配置。

### 4.4 `startup-only`（热重载边界）

热重载只对**运行期可变**字段生效。但有些**基础设施**字段（数据库引擎、checkpointer、沙箱 provider）在启动时被「捕获」成对象，改它们**必须重启进程**。这类字段在 `reload_boundary.py` 登记，其描述以 `startup-only:` 开头：

```python
# app_config.py
database: DatabaseConfig = Field(
    ...,
    description=format_field_description("database", field_doc="..."),
    # → "startup-only: init_engine_from_config() 在启动时运行一次...\n\n..."
)
```

mini 登记的 startup-only 字段：`database` / `checkpointer` / `run_events` / `stream_bridge` / `sandbox` / `log_level`。其余字段（models / memory / title …）都是运行期可变，改完存盘即生效。

### 4.5 paths：路径解析

`config/paths.py` 提供运行时路径 API（mini 用它**替代** deer 的 `runtime_paths`，新代码不得 import `runtime_paths`）：

- `resolve_path(value)`：绝对路径原样返回，相对路径相对项目根解析。
- `project_root()`：运行时项目根（优先 `DEER_FLOW_PROJECT_ROOT` 环境变量，否则当前目录）。
- `runtime_home()`：可写状态目录（优先 `DEER_FLOW_HOME`，否则 `{project_root}/.deer-flow`）——即 **base_dir**。
- `get_paths()`：返回 `Paths` 对象（含 `base_dir` / `users_dir`），memory / sandbox / persistence 用 base_dir 拼用户级目录。

> 注意两套「根」：`PROJECT_ROOT`（= backend 目录，找 pyproject.toml 定位，用于 config.yaml 路径）vs `project_root()`（运行时根，env 或 cwd，用于数据目录）。职责不同，别混。

---

## 5. 设计原理（讲清楚每个「为什么」）

### 5.1 为什么 database 默认 `memory`？（红线 #25）

`DatabaseConfig.backend` 默认 `"memory"`，不是 `"sqlite"`。这样**空配置不依赖任何文件 / 数据库**就能启动（适合开发 / 测试 / CI）。要持久化时显式配 `backend: sqlite`。deer 也是 memory 默认。

### 5.2 为什么 `DatabaseConfig` 派生 `sqlite_path` / `app_sqlalchemy_url`？

用户只配 `backend` + `sqlite_dir`，系统**自动派生**出 checkpointer 和 app 各自要用的具体路径 / URL：

```python
db = DatabaseConfig(backend="sqlite", sqlite_dir="/tmp/data")
db.sqlite_path          # /tmp/data/deerflow.db（checkpointer + app 共用）
db.app_sqlalchemy_url   # sqlite+aiosqlite:////tmp/data/deerflow.db
```

好处：用户配一处，派生多处，不会 checkpointer 和 app 路径对不上。WAL 模式让两者共用一个文件也安全（并发读 + 单写不阻塞）。

### 5.3 为什么 `None` 列表节要归一成 `[]`？

YAML 里把一个列表节**全注释掉**（如 `models:` 下面只剩注释），PyYAML 会解析成 `None`。pydantic 看到 `None` 给 `list` 字段会报晦涩的 `Input should be a valid list`。`_coerce_null_list_sections` validator 把 `None` → `[]`，让「全注释」也能正常启动（对齐 `default_factory=list`）。

### 5.4 为什么 `format_field_description` 对未登记字段 raise `KeyError`？

`reload_boundary.format_field_description("xxx")` 如果 `"xxx"` 没登记，直接 `KeyError` 而非静默返回占位符。这是有意的——**静默会让笔误绕过漂移测试**（登记表和 schema 描述本该一一对应）。raise 让笔误立刻暴露。

### 5.5 为什么 mini 不移植 deer 的 `load_xxx_from_dict` 单例函数？

deer 给 memory / title / checkpointer 等都配了**模块级单例**（`get_memory_config()` 等），因为 deer 有些代码路径直接调这些 getter 而非从 `app_config` 读。mini **统一从 `app_config` 读**（`cfg.memory.enabled`），不需要这些单例——少一层间接、少一处缓存一致性坑。所以 mini 的子配置只是**纯 pydantic model**，没有单例函数。

### 5.6 热重载靠 mtime

`get_app_config()` 每次调用时比较 `config.yaml` 的 mtime 和缓存值，**变了就重新加载**。这让 Gateway 和 LangGraph 的配置读数与 yaml 编辑保持一致，无需手动重启。但 `startup-only` 字段即便重载了也不会真正生效（引擎已建好），需要进程重启——这就是「热重载边界」。

### 5.7 为什么 `get_model_config` / `get_tool_config` 要预建索引？（#3688）

config.yaml 里 `models: [...]` 和 `tools: [...]` 是**列表**，每项有 `name`。代码常需要「按名查某项」——`get_model_config("deepseek-chat")`、`get_tool_config("web_search")`。

**朴素实现是线性扫**（旧版 mini 就是这样）：

```python
def get_model_config(self, name):
    for m in self.models:        # 遍历整个 models 列表
        if m.name == name:
            return m
    return None
```

列表有 N 项就要扫 N 次。问题：这两个 getter 在**热路径**——

- `get_tool_config` 在每个 community 工具（web_search / web_fetch / image_search）**每次调用**时被 `_common.py::get_tool_extras` 读 2-3 次（取 api_key / 超时 / proxy 等额外字段）；
- `get_model_config` 在每次 agent 构建（`create_chat_model`）+ 每个绑定工具的中间件（`tool_error_handling` / `lead_agent`）都调一次。

一次对话可能触发几十次扫表。models/tools 列表通常很短（几个），单次扫开销可忽略，但**累积起来是纯浪费**——结果每次都一样（config 在两次 reload 间不变）。

**#3688 的修法**：在 `AppConfig` 校验完成后（`@model_validator(mode="after")`），**一次性**把列表预建成 name→config 的 dict：

```python
@model_validator(mode="after")
def _build_name_indexes(self):
    models_by_name = {}
    for model in self.models:
        models_by_name.setdefault(model.name, model)   # 重名保留首条
    self._models_by_name = models_by_name
    # tools 同理
    return self

def get_model_config(self, name):
    ...
    return self._models_by_name.get(name)   # O(1) dict 查
```

几个关键设计点：

- **`PrivateAttr`**：索引是 `_models_by_name: dict = PrivateAttr(...)`。pydantic 的 `PrivateAttr` 让它**不参与序列化**（`model_dump()` 里不出现）——它是派生缓存，不是配置数据，不该被 dump 出来再 load 回去。
- **`setdefault` 保首条**：如果用户在 yaml 里写了两个同名 model，`setdefault` 保留**先出现**的那个——与旧 `for` 循环的「首匹配」语义完全一致，不会因为换实现而改变行为。
- **model_validator `after`**：在所有字段校验通过后才建表，保证读到的 `self.models` / `self.tools` 是已规整过的（`None` 已被 §5.3 归一成 `[]`）。
- **reload 自动刷新**：`get_app_config()` 检测到 yaml 变了会**新构一个 `AppConfig`**，新实例的 `_build_name_indexes` 重新跑，索引自然刷新——旧实例的索引不会污染新实例（见 `test_fresh_config_does_not_inherit_stale_index`）。

> 这是「**用空间换时间**」的经典优化：多用一个 dict 的内存（几条记录），换掉热路径上的重复线性扫。对教学版而言，它还示范了 pydantic 的 `PrivateAttr` + `model_validator(after)` 这对常见组合「**校验后派生私有状态**」。

---

## 6. 文件结构

```
config/
├── __init__.py              # 导出 AppConfig + 全部子配置 + paths API + reload_boundary + tracing
├── app_config.py            # AppConfig（总配置）+ get_app_config（单例+mtime 热重载）
├── model_config.py          # ModelConfig（模型档案）
├── paths.py                 # resolve_path / project_root / runtime_home / get_paths（替代 runtime_paths）
├── reload_boundary.py       # STARTUP_ONLY_FIELDS + format_field_description（热重载边界）
├── extensions_config.py     # ExtensionsConfig（MCP + 技能启用，dataclass）+ is_skill_enabled
├── database_config.py       # DatabaseConfig（+ 派生 sqlite_path / app_sqlalchemy_url）
├── checkpointer_config.py   # CheckpointerConfig（LangGraph 状态持久化）
├── run_events_config.py     # RunEventsConfig（运行事件存储）
├── stream_bridge_config.py  # StreamBridgeConfig（SSE 流桥）
├── memory_config.py         # MemoryConfig
├── title_config.py          # TitleConfig
├── summarization_config.py  # SummarizationConfig（+ ContextSize）
├── loop_detection_config.py # LoopDetectionConfig（+ ToolFreqOverride + validator）
├── token_usage_config.py    # TokenUsageConfig
├── tool_output_config.py    # ToolOutputConfig（输出预算）
├── tool_search_config.py    # ToolSearchConfig
├── safety_finish_reason_config.py  # SafetyFinishReasonConfig（+ SafetyDetectorConfig）
├── sandbox_config.py        # SandboxConfig（+ VolumeMountConfig）
├── subagents_config.py      # SubagentsAppConfig
├── skills_config.py         # SkillsConfig（+ get_skills_path）
└── skill_evolution_config.py # SkillEvolutionConfig
```

---

## 7. 关键接口 / 签名

### 总配置与单例

```python
class AppConfig(BaseModel):
    config_version: int = 0
    log_level: str = "info"
    models: list[ModelConfig] = []
    memory: MemoryConfig            # 各子配置，默认 default_factory
    database: DatabaseConfig        # 默认 memory
    sandbox: SandboxConfig          # 默认 LocalSandboxProvider
    checkpointer: CheckpointerConfig | None = None
    stream_bridge: StreamBridgeConfig | None = None
    # ... 其余子配置

    def get_model_config(self, name: str | None) -> ModelConfig | None   # #3688：O(1) dict 查（name=None→首个）
    def get_tool_config(self, name: str) -> dict[str, Any] | None        # #3688：O(1) dict 查

def get_app_config() -> AppConfig        # 单例 + mtime 热重载
def reload_config() -> AppConfig         # 强制重载
def load_config_from_yaml(path=None) -> dict  # 加载 + 展开 $VAR
```

### 路径

```python
def resolve_path(value, *, base=None) -> Path   # 绝对原样，相对项目根
def project_root() -> Path                       # env DEER_FLOW_PROJECT_ROOT 或 cwd
def runtime_home() -> Path                       # env DEER_FLOW_HOME 或 {root}/.deer-flow
def get_paths() -> Paths                         # {base_dir, users_dir}
```

### 热重载边界

```python
STARTUP_ONLY_FIELDS: dict[str, str]   # {字段路径: 原因}
def is_startup_only_field(path: str) -> bool
def format_field_description(path, *, field_doc=None) -> str  # 产 "startup-only: ..." 前缀
```

### DatabaseConfig 派生

```python
db.sqlite_path            # {sqlite_dir}/deerflow.db
db.checkpointer_sqlite_path  # = sqlite_path
db.app_sqlalchemy_url     # sqlite → sqlite+aiosqlite:///…；postgres → postgresql+asyncpg://…
```

---

## 8. 应用方法

### 8.1 空配置启动（红线 #25）

```python
from deerflow.config import AppConfig, get_app_config

# 方式一：直接构造（测试用，不读文件）
cfg = AppConfig()
assert cfg.database.backend == "memory"   # 内存模式，能跑

# 方式二：从 config.yaml 加载（生产用）
cfg = get_app_config()   # 文件不存在/为空也安全
```

### 8.2 在 config.yaml 里覆盖子配置

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

### 8.3 取派生路径（persistence / checkpointer 用）

```python
cfg = get_app_config()
db = cfg.database
if db.backend == "sqlite":
    engine_url = db.app_sqlalchemy_url   # sqlite+aiosqlite:///…
    cp_path = db.checkpointer_sqlite_path
```

### 8.4 单元测试里 hermetic 注入（不读磁盘）

```python
from deerflow.config import AppConfig, MemoryConfig

cfg = AppConfig(memory=MemoryConfig(enabled=False), database={"backend": "sqlite"})
assert cfg.memory.enabled is False
```

### 8.5 路径解析（memory / sandbox 用）

```python
from deerflow.config import get_paths, resolve_path

base_dir = get_paths().base_dir          # .deer-flow（或 DEER_FLOW_HOME）
memory_file = base_dir / "users" / user_id / "memory.json"
config_path = resolve_path("config.yaml")  # 相对项目根
```

---

## 9. 与其它模块的关系

```
config.yaml ──get_app_config()──→ AppConfig (单例 + mtime 热重载)
                                    │
   ┌────────────────┬───────────────┼────────────────┬─────────────┐
   ▼                ▼               ▼                ▼             ▼
 models         memory          sandbox          persistence    tools
 (ModelConfig)  (MemoryConfig)  (SandboxConfig)  (DatabaseConfig)(M15)
   │                │               │                │
   └─ paths.resolve_path / runtime_home 被所有需要数据目录的模块用
   └─ reload_boundary 标记哪些字段改了要重启
```

- **依赖**：无（最底层，仅 pydantic + yaml + 标准库）。
- **被依赖**：**几乎所有模块**读配置都走 `AppConfig`：
  - `models`（M-models）读 `ModelConfig` / `cfg.models`；
  - `memory`（M13）读 `MemoryConfig` + `paths.runtime_home`；
  - `sandbox`（M10）读 `SandboxConfig`；
  - `persistence`（M4）读 `DatabaseConfig` 派生路径；
  - `middlewares`（M16）读 `title.enabled` / `memory.enabled` / `loop_detection.enabled`；
  - `skills`（M14）读 `SkillsConfig.get_skills_path`。

> 这就是为什么 config 排在 **Phase 0 地基**——它没有依赖，但后面所有模块都依赖它。

---

## 10. 常见问题 / 排错

### Q1：改了 `config.yaml` 但没生效？

分两种：
- **运行期可变字段**（memory / title / models …）：下一条消息自动生效（mtime 热重载）。确认你改的是 `get_config_file()` 实际读取的那个文件（项目根 `mini-deer-flow/config.yaml`，不是 backend 下的）。
- **startup-only 字段**（database / checkpointer / sandbox …）：必须**重启进程**——它们在启动时被捕获成对象，热重载不会重建。看字段描述是否带 `startup-only:` 前缀。

### Q2：启动报 `Input should be a valid list`

多半是 `models:` / `tools:` 下面全注释成 `None`。M0 已加 `_coerce_null_list_sections` 把 `None` → `[]`，正常不会遇到。若仍遇到，确认你跑的是新版 app_config（有该 validator）。

### Q3：`AppConfig()` 空构造就报「sandbox.use 必填」？

旧版会。M0 后 `AppConfig()` 给 sandbox 一个 default_factory（默认 `LocalSandboxProvider`），空构造安全。若报错说明用的是旧版 app_config。

### Q4：`is_skill_enabled` 为什么没配的技能也返回 True？

对齐 deer 语义：**未显式配置的 public / custom 技能默认启用**（开箱即用）。显式列入 `enabled_skills` 的一定启用；其它 public/custom 默认放行。M14 skills 落地后会改成每次重读 `extensions_config.json` 的精确状态。

### Q5：`$VAR` 没展开 / 报环境变量未找到？

mini 的 `_expand_env_vars` 在变量**未设置时保留占位文本**（`$FOO` 原样留下，不报错）。如果你期望它变成真实值却没变，确认：① 环境变量名拼写对（区分大小写）；② 变量确实设了（`.env` 文件被 `load_dotenv` 加载，或 shell 里 export 了）。注意 mini 用正则 `\$(\w+)`，只匹配字母数字下划线。

### Q6：`format_field_description("xxx")` 报 KeyError？

`"xxx"` 没在 `STARTUP_ONLY_FIELDS` 登记。这是有意的防笔误机制——只有真正需重启的字段才该用这个前缀。把字段加进 `reload_boundary.py` 的登记表，或别给普通字段套 `startup-only:`。

---

## 小结

config 的精髓是「**把配置从散落的 dict 升级成带类型、带默认、带边界的强类型系统**」。记住四件事：

1. **强类型子配置**：17 个 pydantic model，拼错立刻报、默认值集中、IDE 补全。
2. **空配置可启动**：所有字段有安全默认，`database` 默认 `memory`（红线 #25）。
3. **热重载有边界**：运行期字段改完即生效；`startup-only` 字段（database/checkpointer/sandbox/…）需重启——看 `reload_boundary.py` 登记。
4. **paths 替代 runtime_paths**：新代码用 `resolve_path` / `runtime_home` / `get_paths`，不要 import `runtime_paths`。

上一个文档：`docs/user_context.md`（用户隔离）。M0 完成后，Phase 0 地基（build + utils + reflection + user_context + config）全部就位，下一步进入 **Phase 1**：persistence（M4）/ checkpointer（M5）/ events（M6）等持久化与运行时基础。
