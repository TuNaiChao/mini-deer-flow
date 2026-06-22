# 22. tools.md — 工具系统（9 内置工具 + 五类来源 + 去重 + 条件加载）

> **一句话定位**：本模块是 agent「能做什么」的总装车间——把来自**五个来源**的工具（配置定义 / 内置 /
> MCP / ACP / community）汇总、去重、按条件绑定，喂给 LangGraph agent。`get_available_tools()` 是
> 所有工具的统一入口，被 lead agent factory（M17）调用。

读完 [community.md](community.md)（懂了「联网 provider 经 `tools[].use:` 加载」）再看本篇最省事——
community 工具是「配置定义工具」来源的一员，本篇讲清楚**所有**来源怎么汇总进 agent 工具集。

---

## 工具的五类来源

agent 的工具不是写死的，而是运行时从五个地方组装：

| 来源 | 怎么来 | 例子 |
|------|--------|------|
| **① 配置定义** | `config.yaml` 的 `tools[]`，每条 `use: "模块:变量"` 经 `resolve_variable` 动态加载 | sandbox 的 bash/read_file、community 的 web_search |
| **② 内置** | 代码里 `@tool` 装饰的，按条件绑定 | present_files、ask_clarification、view_image、task |
| **③ MCP** | 启用的 MCP 服务器（M20），`get_cached_mcp_tools()` 发现 | filesystem_read_file、playwright_navigate |
| **④ ACP** | 配置了 `acp_agents` 才加（soft-load `acp` 包） | invoke_acp_agent（调外部 codex/claude agent） |
| **⑤ （延迟）** | MCP 工具默认延迟，经 `tool_search` 按需提升 | 同 ③，但 agent 先只看名字 |

```
config.yaml tools[]  ──resolve_variable──→  配置工具
内置 @tool（条件）   ──────────────────────→  内置工具
extensions_config mcpServers ──M20──→        MCP 工具（tag_mcp_tool 标记）
config acp_agents   ──soft-load acp──→       ACP 工具
                         │
                         ▼
              get_available_tools() 汇总 + 去重 + sync 包装
                         │
                         ▼
              assemble_deferred_tools()（MCP 延迟，仅 tool_search.enabled）
                         │
                         ▼
                  agent 工具集 → 绑给 LangGraph
```

---

## 9 内置工具逐一

| 工具 | 作用 | 绑定条件 | 依赖 |
|------|------|----------|------|
| `present_files` | 把 `/mnt/user-data/outputs` 下的文件展示给用户 | **始终** | — |
| `ask_clarification` | 请求用户澄清（被 ClarificationMiddleware 中断） | **始终** | — |
| `view_image` | 读图片成 base64 注入状态（vision 模型看图） | `supports_vision=True` | models |
| `task` | 委派任务给子代理（后台执行 + 5s 轮询 + SSE） | `subagent_enabled=True` | subagents（M11） |
| `skill_manage` | agent 自管理技能（create/patch/edit/delete/...） | `skill_evolution.enabled=True` | skills（M14） |
| `tool_search` | 按需提升延迟 MCP 工具的完整 schema | `tool_search.enabled=True` + 有 MCP 工具 | mcp（M20） |
| `setup_agent` | 初始化时持久化新自定义 agent 的 SOUL.md+config | `is_bootstrap=True`（M17 factory 绑） | agents_config（M22） |
| `update_agent` | 自定义 agent 更新自身 SOUL.md/config | `agent_name` 已设且非 bootstrap（M17 绑） | agents_config（M22） |
| `invoke_acp_agent` | 调外部 ACP 兼容 agent（codex/claude） | 配置了 `acp_agents`（soft-load `acp`） | acp 包（可选） |

注意 `setup_agent` / `update_agent` **不**由 `get_available_tools` 加载——它们由 lead agent factory
（M17）按运行时上下文（`is_bootstrap` / `agent_name`）绑定。`get_available_tools` 只管前 7 个 + 配置/MCP/ACP。

---

## `get_available_tools()` 的组装流程

```python
def get_available_tools(groups=None, include_mcp=True, model_name=None,
                        subagent_enabled=False, *, app_config=None) -> list[BaseTool]:
    config = app_config or get_app_config()

    # ① 配置定义工具：resolve_variable(cfg["use"], BaseTool) 动态加载
    tool_configs = [t for t in config.tools if groups is None or t["group"] in groups]
    if not is_host_bash_allowed(config):       # host-bash 过滤（LocalSandbox 活跃时不暴露）
        tool_configs = [t for t in tool_configs if not _is_host_bash_tool(t)]
    loaded = [resolve_variable(cfg["use"], BaseTool) for cfg in tool_configs]
    # name 不一致告警（#1803 根因）
    for cfg, t in zip(tool_configs, loaded):
        if cfg.get("name") != t.name: logger.warning(...)

    # ② 内置工具（条件）
    builtins = [present_file, ask_clarification]
    if config.skill_evolution.enabled: builtins.append(skill_manage)
    if subagent_enabled: builtins.append(task)
    if model_config.supports_vision: builtins.append(view_image)

    # ③ MCP 工具（M20，读盘最新 extensions_config）
    mcp = get_cached_mcp_tools() if include_mcp and has_enabled_servers else []
    for t in mcp: tag_mcp_tool(t)              # 标记，供 tool_search 延迟装配识别

    # ④ ACP 工具（配置了才加，soft-load acp）
    acp = [build_invoke_acp_agent_tool(config.acp_agents)] if config.acp_agents else []

    # 按 name 去重：config > builtins > MCP > ACP（防 #1803）
    return dedupe(loaded + builtins + mcp + acp)
```

### 关键不变量

**① 按 name 去重（config > builtins > MCP > ACP）**——重名让 LLM 收到模糊/拼接的 function schema
（issue #1803：LLM schema 里一个名，路由认另一个名 → "not a valid tool"）。config 工具优先（用户显式配的），
重名跳过 + 告警。

**② host-bash 过滤**——`LocalSandboxProvider` 活跃时（本地模式，非安全边界）不暴露宿主 bash 工具，
防 agent 绕过沙箱直接在宿主执行命令。`is_host_bash_allowed(config)` 判断当前 provider 是否非 local。

**③ name 不一致告警**——`config.yaml` 的 `tools[].name` 与工具对象的 `.name` 不一致时告警。这是 #1803
的根因：LLM 看到的工具名（来自 schema）和路由识别的名（来自 `.name`）不同。绑定用工具自己的 `.name`。

**④ sync 包装**——async-only 工具（如 MCP 工具、jina_ai）补 `func` 同步入口（`make_sync_tool_wrapper`），
让同步 agent 调用路径（`DeerFlowClient.chat()`）能调它们。

---

## `tools[].use:` 加载机制（reflection）

配置定义工具靠 [reflection](reflection) 的 `resolve_variable` 加载：

```yaml
# config.yaml
tools:
  - name: web_search
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"   # 模块路径:变量名
    max_results: 5
```

`resolve_variable("deerflow.community.ddg_search.tools:web_search_tool", BaseTool)`：
1. `import deerflow.community.ddg_search.tools`（模块）；
2. 取出 `web_search_tool` 变量（`@tool` 装饰的 BaseTool）；
3. 校验它是 `BaseTool` 子类。

**换 provider 只改 `use:` 一行**（ddg → tavily），agent 代码零改动——这是 community（M21）provider
框架可插拔的基石。`name`/`group`/`max_results` 等「额外字段」经 `AppConfig.get_tool_config(name)` 读出，
传给 provider 当参数。

---

## MCP 工具标记 + 延迟装配（tool_search）

MCP 工具可能几十上百个（一个 MCP 服务器暴露一堆工具）。全绑给模型会**撑爆上下文** + 让工具 schema
模糊。所以 MCP 工具默认**延迟**：

1. **加载时标记**：`get_available_tools` 加载 MCP 工具后调 `tag_mcp_tool(t)`，给它打 `deerflow_mcp` 元数据标记；
2. **延迟装配**（agent 构建处，`assemble_deferred_tools`）：把带标记的工具放进 `DeferredToolCatalog`，
   agent 只看到它们的**名字**（列在 `<available-deferred-tools>` 段），看不到完整 schema；
3. **按需提升**：agent 用 `tool_search` 工具查询，取回匹配工具的完整 schema（写入图状态 `promoted`），
   下一步起这些工具就可调用。

```
MCP 工具（tag_mcp_tool 标记）
   │  assemble_deferred_tools(filtered, enabled=tool_search.enabled)
   ▼
DeferredToolSetup(tool_search_tool, deferred_names, catalog_hash)
   │  tool_search_tool 追加进工具集
   │  deferred_names 写入 <available-deferred-tools> 段
   ▼
agent 调 tool_search("select:read_file") → Command(promoted={catalog_hash, names:[read_file]})
   │  DeferredToolFilterMiddleware（M16）按 thread 提升延迟工具
   ▼
read_file 工具变为可调用
```

**fail-closed**：若 `tool_search.enabled=True`、有 MCP 工具通过策略过滤、但没恢复出延迟集合，**抛错**
而非静默把完整 schema 绑给模型（防泄露不该用的工具）。

`catalog_hash` 把 per-thread 提升记录 scope 到当前目录——目录变了（工具集变了）就当未提升，防误用。

### tool_search 的三种查询

| 查询 | 含义 |
|------|------|
| `select:Read,Edit` | 按精确名字取（逗号分隔） |
| `notebook jupyter` | 关键词正则搜索（名字命中得分高于描述命中），最多 5 个 |
| `+slack send` | 名字必须含 `slack`，再按剩余词 `send` 排序 |

非法正则（如未闭合括号）降级为字面匹配，不抛错。

---

## sync 包装（async 工具的同步入口）

DeerFlowClient 的同步流式路径（`chat()`）需要 `BaseTool.func`（同步入口），但很多工具是 async
（MCP 工具、jina_ai、skill_manage）。`make_sync_tool_wrapper(coro, name)` 把协程包成同步函数：

- **当前线程有运行中的事件循环** → 不能嵌套 `asyncio.run`，卸到专用线程池在新循环里跑（复制 contextvar）；
- **无运行中的循环** → 直接 `asyncio.run`。

M15 扩展：`_get_runnable_config_param(coro)` 检测协程是否声明了 `RunnableConfig` 参数；若有，包装暴露
`config: RunnableConfig` 参数（LangChain 据此注入运行时配置），再转发到协程的 config 参数名——覆盖
`invoke_acp_agent` 这类配置敏感工具（它需要 `config["configurable"]["thread_id"]` 算工作目录）。

---

## 文件结构

```
tools/
├── __init__.py                  # 导出 get_available_tools
├── tools.py                     # get_available_tools（汇总+去重+host-bash+条件+MCP tag+ACP）+ _is_host_bash_tool + _ensure_sync_invocable_tool
├── mcp_metadata.py              # tag_mcp_tool / is_mcp_tool / MCP_TOOL_METADATA_KEY（MCP 标记单一真相源）
├── sync.py                      # make_sync_tool_wrapper + _get_runnable_config_param（async→sync + RunnableConfig 注入）
├── types.py                     # Runtime = ToolRuntime[dict[str, Any], ThreadState]
├── skill_manage_tool.py         # skill_manage（agent 自管理技能，仅 skill_evolution.enabled）
└── builtins/
    ├── __init__.py              # 导出 9 工具
    ├── present_file_tool.py     # present_files（始终）
    ├── clarification_tool.py    # ask_clarification（始终，占位；真正中断 ClarificationMiddleware）
    ├── view_image_tool.py       # view_image（仅 supports_vision；路径白名单+魔数+20MB）
    ├── task_tool.py             # task（仅 subagent_enabled；后台执行+轮询+SSE+token 缓存）
    ├── tool_search.py           # tool_search + DeferredToolCatalog + assemble_deferred_tools + get_deferred_tools_prompt_section
    ├── setup_agent_tool.py      # setup_agent（仅 is_bootstrap；M17 绑）
    ├── update_agent_tool.py     # update_agent（仅 agent_name 非 bootstrap；M17 绑）
    └── invoke_acp_agent_tool.py # invoke_acp_agent（soft-load acp；per-thread 工作区 + MCP servers 透传）
```

---

## 关键接口

```python
# tools.py
def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]: ...

# mcp_metadata
MCP_TOOL_METADATA_KEY = "deerflow_mcp"
def tag_mcp_tool(tool: BaseTool) -> BaseTool: ...   # 标记 + 返回（链式）
def is_mcp_tool(tool: BaseTool) -> bool: ...

# sync
def _get_runnable_config_param(func) -> str | None: ...
def make_sync_tool_wrapper(coro, tool_name: str) -> Callable: ...

# tool_search
class DeferredToolCatalog:                          # 不可变目录，纯搜索
    def search(self, query: str) -> list[BaseTool]: ...
    @property
    def names(self) -> frozenset[str]: ...
    @property
    def hash(self) -> str: ...                       # 目录内容哈希（scope 提升）
class DeferredToolSetup:                            # tool_search_tool / deferred_names / catalog_hash
    ...
def build_tool_search_tool(catalog) -> BaseTool: ...
def assemble_deferred_tools(filtered_tools, *, enabled) -> tuple[list[BaseTool], DeferredToolSetup]: ...  # fail-closed
def get_deferred_tools_prompt_section(*, deferred_names) -> str: ...   # <available-deferred-tools> 段

# invoke_acp_agent
def build_invoke_acp_agent_tool(agents: dict) -> BaseTool: ...

# config
class AppConfig:
    def get_tool_config(self, name: str) -> dict | None: ...   # 按 tools[].name 查（M21 引入）
```

---

## 设计原理（权衡 / 不变量）

### 为什么按 name 去重且 config 优先

重名工具让 LLM 收到模糊/拼接的 function schema（#1803）。config 工具是用户**显式**配的，最该保留；
builtins 是兜底；MCP/ACP 是扩展。优先级 `config > builtins > MCP > ACP` 让用户能覆盖内置同名工具。

### 为什么 host-bash 要过滤

`LocalSandboxProvider`（本地模式）**不是安全边界**——agent 调 host-bash 会直接在宿主执行命令。本地开发
默认用 local 沙箱（虚拟路径翻译），所以不暴露 host-bash 工具，强制 agent 走沙箱的 `bash`（在隔离目录执行）。
切到 AIO 沙箱（Docker/K8s 容器隔离，真安全边界）时 `is_host_bash_allowed=True`，host-bash 工具才暴露。

### 为什么 MCP 工具要延迟（tool_search）

一个 MCP 服务器可能暴露 20+ 工具（Playwright 有 navigate/click/type/screenshot/...）。全绑给模型：
① 工具 schema 占大量 token；② 模型在几十个工具里选容易选错。延迟让模型先只看名字，需要时用 tool_search
取完整 schema——按需加载，省 token + 选更准。

### 为什么 tool_search 要 fail-closed

若启用延迟、有 MCP 工具通过了策略过滤、但没恢复出延迟集合（异常状态），宁可**抛错**也不静默把完整
schema 绑给模型——防 agent 用到它不该用的工具。这是安全兜底。

### 为什么 setup/update_agent 不在 get_available_tools

它们的绑定条件是**运行时上下文**（`is_bootstrap` / `agent_name`），不是配置开关。`get_available_tools`
在 agent 构建时调一次，但 setup/update 的条件在**每次对话**变（用户在哪个 agent 的对话里）。所以由
lead agent factory（M17）按 `runtime.context` 动态绑定，不走 `get_available_tools`。

### 为什么 invoke_acp_agent soft-load acp

ACP 依赖 `agent-client-protocol` 包，多数部署不装。soft-load（模块顶层不 import，函数内 try/except）
让工具能构造（描述列出配置的 agent），真正调用才检测；缺包返可操作安装提示。其它工具不受影响。

---

## 与其它模块的关系

```
config/app_config (tools[] + get_tool_config + skill_evolution + tool_search + acp_agents + models.supports_vision)
   │
reflection (resolve_variable 加载 tools[].use:)
   │
   ├─ sandbox (bash 等工具，M10) ── 经 tools[].use: 加载
   ├─ community (web 工具，M21) ── 经 tools[].use: 加载
   ├─ mcp (MCP 工具，M20) ── get_cached_mcp_tools + tag_mcp_tool
   ├─ subagents (task 工具，M11)
   ├─ skills (skill_manage，M14)
   ├─ agents_config (setup/update_agent，M22)
   ▼
tools/get_available_tools（汇总 + 去重 + 条件 + sync 包装）
   │
   ├─ tool_search.assemble_deferred_tools（MCP 延迟，仅 tool_search.enabled）
   ▼
agents/lead_agent (M17 factory 绑定工具集 + setup/update_agent 按上下文)
   │
   ▼ 消费者：M16 DeferredToolFilterMiddleware（按 thread 提升延迟工具）
```

- **上游**：config（工具配置 + 条件开关）、reflection（`tools[].use:` 加载）、sandbox/community/mcp/
  subagents/skills/agents_config（各工具来源）。
- **下游消费者**：M17 lead_agent factory（调 `get_available_tools` 组装工具集 + 按 `runtime.context`
  绑 setup/update_agent）、M16 `DeferredToolFilterMiddleware`（按 thread 提升延迟工具）。

---

## 常见问题 / 排错

**Q：我配了 web_search 但 agent 没这个工具？**
A：三查：① `config.yaml` 的 `tools[]` 有 `use: "deerflow.community.ddg_search.tools:web_search_tool"`；
② `resolve_variable` 能 import（模块路径对、SDK 装了）；③ 没被 host-bash 过滤或 groups 过滤掉。

**Q：日志报 "Tool name mismatch"？**
A：`config.yaml` 的 `tools[].name` 与工具 `.name` 不一致。这是 #1803 根因——绑定用工具自己的 `.name`，
但你的 config name 应改成一致避免困惑。

**Q：日志报 "Duplicate tool name skipped"？**
A：两个工具同名（如 config 和内置都叫 `bash`）。去重保留优先级高的（config > builtins > MCP > ACP），
低优先级的被跳过。改名或删重复配置。

**Q：MCP 工具很多，模型选不过来？**
A：开 `tool_search.enabled: true`。MCP 工具变延迟（agent 只看名字），用 `tool_search` 按需取 schema。
省 token + 选更准。

**Q：agent 看不到 setup_agent / update_agent？**
A：正常。它们由 lead_agent factory（M17）按运行时上下文绑定：setup_agent 仅 `is_bootstrap=True`
（初始化新建 agent 流程），update_agent 仅在自定义 agent 的对话里（`runtime.context["agent_name"]` 已设）。
默认 lead agent 对话不绑这俩。

**Q：view_image 为什么有时有有时没有？**
A：仅 `supports_vision=True` 的模型才绑。换非 vision 模型时 view_image 自动消失（条件加载）。

**Q：invoke_acp_agent 调用报 "not installed"？**
A：`agent-client-protocol` 包没装。`pip install agent-client-protocol`。ACP 是可选能力，不装不影响其它工具。

---

## 应用方法

### 配置工具集（config.yaml）

```yaml
tools:
  # sandbox 工具
  - {name: bash, group: exec, use: "deerflow.sandbox.tools:bash_tool"}
  - {name: read_file, group: exec, use: "deerflow.sandbox.tools:read_file_tool"}
  # community 联网工具（M21）
  - name: web_search
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"
    max_results: 5
  - name: web_fetch
    group: search
    use: "deerflow.community.jina_ai.tools:web_fetch_tool"

tool_groups:
  - {name: exec}
  - {name: search}

# 条件工具的开关
skill_evolution:
  enabled: false          # true 才挂 skill_manage
tool_search:
  enabled: false          # true 才延迟 MCP 工具
# subagent_enabled 是运行时参数（runtime config），不是 config.yaml 字段
# view_image 按 model.supports_vision 自动绑
# acp_agents: {codex: {command: codex-acp, description: ...}}  # 配了才挂 invoke_acp_agent
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_tools.py（78 个 hermetic 测试）
```

测试约定：`get_available_tools(app_config=...)` 注入显式配置（不读全局 config.yaml）；配置工具加载用
monkeypatch 桩 `resolve_variable`；MCP/ACP 缺包软加载；setup/update_agent / skill_manage 用
`DEER_FLOW_HOME`→tmp_path 隔离 + LocalSkillStorage(host_path=tmp) 绕单例；runtime 用 SimpleNamespace
鸭子对象（直接调 `.func()` 绕 args_schema 的 Runtime 校验）。
