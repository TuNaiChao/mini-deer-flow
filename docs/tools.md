# 22. tools.md — 工具系统（9 内置工具 + 五类来源 + 去重 + 条件加载）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/tools/`（15 文件，2174 行）。

> **一句话定位**：本模块是 agent「能做什么」的总装车间——把来自**五个来源**的工具（配置定义 / 内置 / MCP / ACP / community）汇总、去重、按条件绑定，喂给 LangGraph agent。`get_available_tools()` 是所有工具的统一入口，被 lead agent factory 调用。

> **先读谁最省事**：[community.md](community.md)（懂「联网 provider 经 `tools[].use:` 加载」）+ [mcp.md](mcp.md)（懂 MCP 工具）。community 是「配置定义工具」来源的一员，本篇讲清楚**所有**来源怎么汇总进 agent 工具集。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「agent 的工具从哪来？怎么做到可插拔？」** —— 五类来源（配置定义/内置/MCP/ACP/community）汇总。能讲清配置驱动加载（`tools[].use:` 经 `resolve_variable` 动态 import）、为什么这比写死好。
2. **「多个来源可能有同名工具，怎么处理？为什么？」** —— 按 name 去重，优先级 config > builtins > MCP > ACP。能讲清重名为什么会让 LLM 收到模糊 schema（一个名 schema、路由认另一个名 → "not a valid tool"）。
3. **「工具几十个全绑给模型有什么问题？怎么解？」** —— 占大量 token + 选错。能讲清延迟装配（tool_search：先只看名字、按需取完整 schema）。
4. **「异步工具怎么给同步调用路径用？」** —— `make_sync_tool_wrapper`：检测事件循环，有循环就卸线程池跑新循环、无循环直接 `asyncio.run`。
5. **「为什么有些工具是条件绑定的（vision/bootstrap/subagent）？」** —— 绑定条件是运行时上下文（模型能力/是否引导/是否子代理），不是配置开关。能讲清 setup/update_agent 为什么不在 `get_available_tools`。
6. **「本地沙箱模式下为什么不暴露 host-bash？」** —— LocalSandbox 不是安全边界，host-bash 会绕过沙箱在宿主执行。切到容器沙箱才暴露。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **工具（tool）** | agent 能调用的一个函数（有名字、参数说明、返回结果）。LLM 决定调哪个、传什么参数。 |
| **`@tool` 装饰器** | LangChain 的装饰器，把一个 Python 函数变成 `BaseTool`（自动从函数签名+docstring 生成参数 schema 给 LLM 看）。 |
| **function schema** | 告诉 LLM「有哪些工具、每个工具叫什么、接受什么参数」的结构化描述。LLM 据此决定调哪个工具。 |
| **动态加载 / 反射** | 程序运行时「按字符串路径」import 一个模块/取出一个变量。本模块 `resolve_variable("模块:变量")` 用它加载配置定义的工具。 |
| **同步 / 异步 / 事件循环** | 同步调用一直等返回；异步（`async/await`）立刻返回未来结果、事件循环在等待时干别的。本模块要把异步工具包成同步入口。 |
| **`asyncio.run` 嵌套限制** | 一个事件循环正在跑时，不能再调 `asyncio.run`（会报「loop already running」）。所以有运行循环时要卸到新线程跑新循环。 |
| **contextvar** | 「上下文变量」——绑给当前执行流的值（如 user_id）。跨线程要 `copy_context()` 手动复制，否则丢失。 |
| **context manager** | `__aenter__`/`__aexit__` 成对的资源管理。`async with` 进/出它。 |
| **token / 上下文窗口** | LLM 按-token-计费，一次能塞的 token 有上限。工具 schema 也吃 token，所以工具太多要延迟。 |
| **path traversal（路径穿越）** | 用 `../` 或绝对路径逃出限定目录。present_files 的路径校验防这个。 |
| **base64** | 把二进制（图片字节）编码成文本的方式。view_image 把图片转 base64 塞进消息让 vision 模型看。 |
| **soft-load（软加载）** | import 放函数内 + `try/except ImportError`，依赖包没装时不崩。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **配置定义工具** | `config.yaml` 的 `tools[]` 每条 `use: "模块:变量"`，经 `resolve_variable` 加载。 |
| **内置工具** | 代码里 `@tool` 装饰的（present_files/ask_clarification/task/view_image/...），按条件绑定。 |
| **MCP 工具** | 启用的 MCP 服务器发现的外部工具（见 [mcp.md](mcp.md)），加载后 `tag_mcp_tool` 标记。 |
| **ACP 工具** | 配置了 `acp_agents` 才加（调外部 codex/claude agent，soft-load `acp` 包）。 |
| **延迟工具（deferred）** | MCP 工具默认延迟，agent 先只看名字，用 `tool_search` 按需取完整 schema。 |
| **host-bash** | 直接在宿主机执行的 bash（绕过沙箱）。本地沙箱模式下过滤掉。 |
| **`RunnableConfig`** | LangChain 的运行时配置对象（含 `configurable.thread_id` 等）。某些工具需要它。 |

---

## §3 整体结构：它在系统里的位置

```
config.yaml tools[]  ──resolve_variable──→  配置定义工具（sandbox/community）
内置 @tool（条件）   ──────────────────────→  内置工具（present_files/ask_clarification/task/...）
extensions_config mcpServers ──mcp.md──→     MCP 工具（tag_mcp_tool 标记）
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

**工具的五类来源**：

| 来源 | 怎么来 | 例子 |
|------|--------|------|
| **① 配置定义** | `config.yaml` 的 `tools[]`，每条 `use: "模块:变量"` 经 `resolve_variable` 动态加载 | sandbox 的 bash/read_file、community 的 web_search |
| **② 内置** | 代码里 `@tool` 装饰的，按条件绑定 | present_files、ask_clarification、view_image、task |
| **③ MCP** | 启用的 MCP 服务器，`get_cached_mcp_tools()` 发现 | filesystem_read_file、playwright_navigate |
| **④ ACP** | 配置了 `acp_agents` 才加（soft-load `acp`） | invoke_acp_agent（调外部 codex/claude） |
| **⑤ 延迟** | MCP 工具默认延迟，经 `tool_search` 按需提升 | 同 ③，但 agent 先只看名字 |

**15 个文件的职责切分**（为什么这么拆见 [§9 设计动机](#9-设计动机分析为什么这么设计作用好处)）：

```
tools/
├── __init__.py                  # 导出 get_available_tools
├── tools.py                     # get_available_tools（汇总+去重+host-bash+条件+MCP tag+ACP）
├── mcp_metadata.py              # tag_mcp_tool / is_mcp_tool / MCP_TOOL_METADATA_KEY（标记单一真相源）
├── sync.py                      # make_sync_tool_wrapper + _get_runnable_config_param（async→sync）
├── types.py                     # Runtime = ToolRuntime[dict, ThreadState]
├── skill_manage_tool.py         # skill_manage（agent 自管理技能，仅 skill_evolution.enabled）
└── builtins/
    ├── __init__.py              # 导出 9 工具
    ├── present_file_tool.py     # present_files（始终；多文件 + 路径归一化 + 穿越校验）
    ├── clarification_tool.py    # ask_clarification（始终；中断交 ClarificationMiddleware）
    ├── view_image_tool.py       # view_image（仅 supports_vision；路径白名单+魔数+20MB）
    ├── task_tool.py             # task（仅 subagent_enabled；详见 subagents.md）
    ├── tool_search.py           # tool_search + DeferredToolCatalog + assemble_deferred_tools
    ├── setup_agent_tool.py      # setup_agent（仅 is_bootstrap；lead_agent factory 绑）
    ├── update_agent_tool.py     # update_agent（仅 agent_name 非 bootstrap；factory 绑）
    └── invoke_acp_agent_tool.py # invoke_acp_agent（soft-load acp；per-thread 工作区）
```

**面试概念地图**：本篇对应「工具系统 / tool-use」「可插拔架构（配置驱动 + 反射）」「上下文工程（延迟工具防 token 爆）」面试常考点。`deerflow-book` 的 `15-builtin-tools.md` 是可选概念预读。

---

## §4 核心概念：9 个内置工具

| 工具 | 作用 | 绑定条件 | 依赖 |
|------|------|----------|------|
| `present_files` | 把 `/mnt/user-data/outputs` 下的文件（**多文件**，路径归一化+穿越校验）展示给用户 | **始终** | config/paths |
| `ask_clarification` | 请求用户澄清（被 ClarificationMiddleware 中断） | **始终** | — |
| `view_image` | 读图片成 base64 注入状态（vision 模型看图） | `supports_vision=True` | models |
| `task` | 委派任务给子代理（后台执行+轮询+SSE） | `subagent_enabled=True` | [subagents](subagents.md) |
| `skill_manage` | agent 自管理技能（create/patch/edit/delete） | `skill_evolution.enabled=True` | [skills](skills.md) |
| `tool_search` | 按需提升延迟 MCP 工具的完整 schema | `tool_search.enabled=True` + 有 MCP 工具 | [mcp](mcp.md) |
| `setup_agent` | 初始化时持久化新自定义 agent 的 SOUL.md+config | `is_bootstrap=True`（factory 绑） | [agents_config](agents_config.md) |
| `update_agent` | 自定义 agent 更新自身 SOUL.md/config | `agent_name` 已设且非 bootstrap | agents_config |
| `invoke_acp_agent` | 调外部 ACP 兼容 agent（codex/claude） | 配置了 `acp_agents`（soft-load `acp`） | acp 包（可选） |

注意 `setup_agent` / `update_agent` **不**由 `get_available_tools` 加载——它们由 lead agent factory 按运行时上下文（`is_bootstrap` / `agent_name`）绑定（见 [§9.5](#95-为什么-setupupdate_agent-不在-get_available_tools)）。

---

## §5 代码走读：重要函数逐个讲

### §5.1 tools.py —— 装配车间（`get_available_tools`）

**`get_available_tools`** [tools.py:64](../backend/packages/harness/deerflow/tools/tools.py#L64) 是所有工具的统一入口，四步组装：

```python
# ① 配置定义工具：resolve_variable(cfg["use"], BaseTool) 动态加载
tool_configs = [t for t in config.tools if groups is None or t.get("group") in groups]
if not is_host_bash_allowed(config):       # host-bash 过滤（LocalSandbox 活跃时不暴露）
    tool_configs = [t for t in tool_configs if not _is_host_bash_tool(t)]
loaded = [(cfg, resolve_variable(cfg["use"], BaseTool)) for cfg in tool_configs]
for cfg, t in loaded:                       # name 不一致告警
    if cfg.get("name") != t.name: logger.warning(...)

# ② 内置工具（条件）
builtins = [present_file, ask_clarification]
if config.skill_evolution.enabled: builtins.append(skill_manage)
if subagent_enabled: builtins.append(task)
if model_config.supports_vision: builtins.append(view_image)

# ③ MCP 工具（读盘最新 extensions_config）
mcp = get_cached_mcp_tools() if include_mcp and has_enabled_servers else []
for t in mcp: tag_mcp_tool(t)               # 标记，供 tool_search 延迟装配识别

# ④ ACP 工具（配置了才加，soft-load acp）
acp = [build_invoke_acp_agent_tool(config.acp_agents)] if config.acp_agents else []

# 按 name 去重：config > builtins > MCP > ACP
return dedupe(loaded + builtins + mcp + acp)
```

`_is_host_bash_tool` [tools.py:46](../backend/packages/harness/deerflow/tools/tools.py#L46) 判断是否宿主 bash 工具；`_ensure_sync_invocable_tool` [tools.py:57](../backend/packages/harness/deerflow/tools/tools.py#L57) 给 async-only 工具补 `func` 同步入口。

### §5.2 sync.py —— 异步工具的同步包装

**`make_sync_tool_wrapper`** [sync.py:55](../backend/packages/harness/deerflow/tools/sync.py#L55)：把协程包成同步函数——

- **当前线程有运行中的事件循环** → 不能嵌套 `asyncio.run`，卸到 `_SYNC_TOOL_EXECUTOR` [sync.py:30](../backend/packages/harness/deerflow/tools/sync.py#L30) 线程池在新循环里跑（`copy_context()` 复制 contextvar 防跨线程丢失）；
- **无运行中的循环** → 直接 `asyncio.run`。

**`_get_runnable_config_param`** [sync.py:34](../backend/packages/harness/deerflow/tools/sync.py#L34)：检测协程是否声明了 `RunnableConfig` 类型参数；若有，包装暴露 `config: RunnableConfig` 参数（LangChain 据此注入运行时配置），再转发到协程的 config 参数名——覆盖 `invoke_acp_agent` 这类配置敏感工具（它需要 `config["configurable"]["thread_id"]` 算工作目录）。

### §5.3 mcp_metadata.py —— MCP 标记的单一真相源

`MCP_TOOL_METADATA_KEY = "deerflow_mcp"` [mcp_metadata.py:18](../backend/packages/harness/deerflow/tools/mcp_metadata.py#L18)。**`tag_mcp_tool`** [L21](../backend/packages/harness/deerflow/tools/mcp_metadata.py#L21) 在加载处写入（就地修改+返回，链式）；**`is_mcp_tool`** [L27](../backend/packages/harness/deerflow/tools/mcp_metadata.py#L27) 在延迟装配处读取。把 key/写入器/谓词集中在这里，让魔法字符串只活在一处——设计为叶子模块（只依赖 `BaseTool`），任何模块都能 import 而不循环。

### §5.4 tool_search.py —— 延迟工具目录 + 按需提升

**`DeferredToolCatalog`** [tool_search.py:56](../backend/packages/harness/deerflow/tools/builtins/tool_search.py#L56)：不可变、可搜索的延迟工具目录（纯搜索，无副作用）。**`search`** [L75](../backend/packages/harness/deerflow/tools/builtins/tool_search.py#L75) 三种查询：`select:Read,Edit`（按精确名字）/ 关键词正则搜索（名字命中得分高于描述命中，最多 5 个）/ `+slack send`（名字必须含 `slack` 再按剩余词排序）。非法正则降级字面匹配，不抛错。

**`assemble_deferred_tools`** [L168](../backend/packages/harness/deerflow/tools/builtins/tool_search.py#L168)：从**策略过滤后**的工具列表装配目录 + tool_search 工具，**fail-closed**——若启用延迟、有 MCP 工具通过过滤、但没恢复出延迟集合，抛错而非静默绑完整 schema。`catalog.hash` 把 per-thread 提升记录 scope 到当前目录，目录变了就当未提升。

### §5.5 present_file_tool.py —— 多文件展示 + 穿越校验

**`present_file_tool`** [present_file_tool.py:94](../backend/packages/harness/deerflow/tools/builtins/present_file_tool.py#L94) 收**路径列表** `filepaths: list[str]`（不是单个）。**`_normalize_presented_filepath`** [L52](../backend/packages/harness/deerflow/tools/builtins/present_file_tool.py#L52)：每条路径先解析到物理宿主路径，再强制落在当前线程的 `outputs` 目录之下（`resolve_virtual_path` [L80](../backend/packages/harness/deerflow/tools/builtins/present_file_tool.py#L80) 含穿越校验，挡 `..` 和前缀混淆），回写规范虚拟路径。接受两种输入——虚拟沙箱路径（agent 视角）或宿主绝对路径。失败返 `ToolMessage` 不中断 run。

---

## §6 数据流：一次调用怎么走完

### §6.1 数据流 A：agent 启动 → 组装工具集

```
① make_lead_agent 调 get_available_tools(groups, include_mcp=True, model_name=..., subagent_enabled=...)
② 四步组装（见 §5.1）：
   ├─ 配置工具：for cfg in config.tools: resolve_variable(cfg["use"], BaseTool)
   ├─ 内置工具：present_files + ask_clarification +（条件）skill_manage/task/view_image
   ├─ MCP 工具：get_cached_mcp_tools() → tag_mcp_tool 标记
   └─ ACP 工具：build_invoke_acp_agent_tool(config.acp_agents)（配了才加）
③ 按 name 去重（config > builtins > MCP > ACP）→ async-only 工具补 func 同步入口
④ （若 tool_search.enabled）assemble_deferred_tools：MCP 工具延迟，只 tool_search 进工具集
⑤ 工具集绑给 LangGraph agent
```

### §6.2 数据流 B：agent 用 tool_search 按需提升 MCP 工具

```
① agent 工具集里有 tool_search + <available-deferred-tools>名字列表（filesystem_read_file 等）
② agent 调 tool_search("select:read_file")
   └─ catalog.search("select:read_file") → 匹配的延迟工具
   └─ return Command(promoted={catalog_hash, names:[read_file]})  ← 写入图状态
③ DeferredToolFilterMiddleware 按 thread 提升延迟工具
   └─ 下一步起 read_file 工具变为可调用（完整 schema 进工具集）
④ agent 调 read_file(...) 读文件
```

---

## §7 配置与用法

### §7.1 配置工具集（`config.yaml`）

```yaml
tools:
  # sandbox 工具
  - {name: bash, group: exec, use: "deerflow.sandbox.tools:bash_tool"}
  - {name: read_file, group: exec, use: "deerflow.sandbox.tools:read_file_tool"}
  # community 联网工具
  - name: web_search
    group: search
    use: "deerflow.community.ddg_search.tools:web_search_tool"
    max_results: 5

tool_groups:
  - {name: exec}
  - {name: search}

# 条件工具开关
skill_evolution:
  enabled: false          # true 才挂 skill_manage
tool_search:
  enabled: false          # true 才延迟 MCP 工具
# subagent_enabled 是运行时参数（runtime config），不是 config.yaml 字段
# view_image 按 model.supports_vision 自动绑
# acp_agents: {codex: {command: codex-acp, description: ...}}  # 配了才挂 invoke_acp_agent
```

### §7.2 跑测试

```bash
cd backend && make test    # 含 test/test_tools.py（87 个 hermetic 测试）
```

测试约定：`get_available_tools(app_config=...)` 注入显式配置（不读全局 config.yaml）；配置工具加载用 monkeypatch 桩 `resolve_variable`；MCP/ACP 缺包软加载；runtime 用 `SimpleNamespace` 鸭子对象（直接调 `.func()` 绕 args_schema 的 Runtime 校验）。

---

## §8 与其它模块的关系

```
config/app_config (tools[] + get_tool_config + skill_evolution + tool_search + acp_agents + models.supports_vision)
   │
reflection (resolve_variable 加载 tools[].use:)
   │
   ├─ sandbox (bash 等工具) ── 经 tools[].use: 加载
   ├─ community (web 工具) ── 经 tools[].use: 加载
   ├─ mcp (MCP 工具) ── get_cached_mcp_tools + tag_mcp_tool
   ├─ subagents (task 工具)
   ├─ skills (skill_manage)
   ├─ agents_config (setup/update_agent)
   ▼
tools/get_available_tools（汇总 + 去重 + 条件 + sync 包装）
   │
   ├─ tool_search.assemble_deferred_tools（MCP 延迟，仅 tool_search.enabled）
   ▼
agents/lead_agent (factory 绑定工具集 + setup/update_agent 按上下文)
   │
   ▼ 消费者：DeferredToolFilterMiddleware（按 thread 提升延迟工具）
```

- **上游**：[config](config.md)（工具配置 + 条件开关）、`reflection`（`tools[].use:` 加载）、[sandbox](sandbox.md)/[community](community.md)/[mcp](mcp.md)/[subagents](subagents.md)/[skills](skills.md)/[agents_config](agents_config.md)（各工具来源）。
- **下游消费者**：[agents](agents.md) lead_agent factory（调 `get_available_tools` 组装工具集 + 按 `runtime.context` 绑 setup/update_agent）、`DeferredToolFilterMiddleware`（按 thread 提升延迟工具）。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **五来源 + 配置驱动加载** | 工具来源多样（内置/外部/用户配） | 换工具只改 `use:` 一行，agent 零改动 | 工具写死 → 加/换要改代码 |
| **按 name 去重（config 优先）** | 重名让 LLM 收到模糊 schema | 用户显式配的覆盖内置同名 | 重名 → "not a valid tool" |
| **host-bash 过滤** | LocalSandbox 不是安全边界 | 本地模式强制走沙箱 bash | host-bash 绕沙箱在宿主执行 |
| **MCP 工具延迟（tool_search）** | 一个 MCP server 暴露 20+ 工具 | 先只看名字省 token，按需取 schema | 全绑 → 占大量 token + 选错 |
| **tool_search fail-closed** | 异常状态不能泄露工具 | 宁可抛错也不静默绑完整 schema | agent 用到不该用的工具 |
| **sync 包装** | async 工具要给同步路径用 | 检测循环，有则卸线程池 | 嵌套 asyncio.run 崩 |
| **条件绑定** | 绑定条件是运行时上下文 | vision/bootstrap/subagent 按需挂 | 全挂 → 非 vision 模型也带 view_image 浪费 |
| **`mcp_metadata` 单一真相源** | 标记 key 三处共用 | 魔法字符串只活一处 | 三处各定义 → 漂移 |

### §9.1 为什么按 name 去重且 config 优先

**动机**：重名工具让 LLM 收到模糊/拼接的 function schema——LLM schema 里一个名，路由认另一个名，调时 "not a valid tool"。

**作用 / 好处**：按 name 去重，优先级 `config > builtins > MCP > ACP`。config 工具是用户**显式**配的，最该保留——这让用户能覆盖内置同名工具（如自定义一个 `bash`）。重名跳过 + 告警。`name 不一致告警`（config name ≠ tool .name）也提示这个根因。

**不这么设计会怎样**：重名 → LLM 困惑 → 调用失败；不让 config 优先 → 用户无法覆盖内置工具。

### §9.2 为什么 host-bash 要过滤

**动机**：`LocalSandboxProvider`（本地模式）**不是安全边界**——agent 调 host-bash 会直接在宿主执行命令（绕过沙箱）。

**作用 / 好处**：本地开发默认用 local 沙箱（虚拟路径翻译），所以不暴露 host-bash 工具，强制 agent 走沙箱的 `bash`（在隔离目录执行）。`is_host_bash_allowed(config)` 判断当前 provider 是否非 local。切到 AIO 沙箱（Docker/K8s 容器隔离，真安全边界）时 host-bash 才暴露。

**不这么设计会怎样**：本地模式暴露 host-bash → agent 绕沙箱在宿主执行任意命令（危险）。

### §9.3 为什么 MCP 工具要延迟（tool_search）

**动机**：一个 MCP 服务器可能暴露 20+ 工具（Playwright 有 navigate/click/type/screenshot/...）。全绑给模型：① 工具 schema 占大量 token；② 模型在几十个工具里选容易选错。

**作用 / 好处**：延迟让模型先只看名字（列在 `<available-deferred-tools>` 段），需要时用 `tool_search` 取完整 schema——按需加载，省 token + 选更准。`catalog_hash` 把提升记录 scope 到当前目录，目录变了就当未提升（防误用）。

**不这么设计会怎样**：全绑 → 占大量 token + 选错；不 scope → 工具集变了还用旧的提升记录。

### §9.4 为什么 tool_search 要 fail-closed

**动机**：若启用延迟、有 MCP 工具通过策略过滤、但没恢复出延迟集合（异常状态）。

**作用 / 好处**：宁可**抛错**也不静默把完整 schema 绑给模型——防 agent 用到它不该用的工具。这是安全兜底。

**不这么设计会怎样**：异常时静默绑完整 schema → agent 可能用到不该用的工具（安全漏洞）。

### §9.5 为什么 setup/update_agent 不在 get_available_tools

**动机**：它们的绑定条件是**运行时上下文**（`is_bootstrap` / `agent_name`），不是配置开关。

**作用 / 好处**：`get_available_tools` 在 agent 构建时调一次，但 setup/update 的条件在**每次对话**变（用户在哪个 agent 的对话里）。所以由 lead_agent factory 按 `runtime.context` 动态绑定，不走 `get_available_tools`——setup_agent 仅 `is_bootstrap=True`（初始化新建 agent 流程），update_agent 仅在自定义 agent 的对话里。

**不这么设计会怎样**：放 `get_available_tools` → 条件在构建时固定 → 每个对话都带这俩或都不带（错）。

### §9.6 为什么 15 文件拆分

每个文件管**一种独立责任**（见 [§3 文件结构](#3-整体结构它在系统里的位置)）。`mcp_metadata` 是叶子模块（标记单一真相源，防三处漂移）；`sync` 是横切机制（被 tools.py/mcp/skill_manage 三处复用）；`tool_search` 是独立机制（catalog/setup/fail-closed/hash-scoped）；一工具一文件便于单独测试与按条件挂载。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/tools/`（与 mini 同布局，15 文件）。**先剥 docstring/comment 再判逻辑差**。

**总结论：高度忠实移植，差异集中在「tools-as-dict（mini）vs ToolConfig（上游）」+ Gateway auth 不 port。**

| 文件 | 剥后 mini/up | 逻辑差 |
|---|---|---|
| `types.py` | 3 / 3 | **0 逻辑差** |
| `mcp_metadata.py` | 7 / 7 | **0 逻辑差**（标记 key/写入器/谓词逐字节同） |
| `skill_manage_tool.py` | 185 / 185 | **0 逻辑差** |
| `clarification_tool.py` | 15 / 15 | **0 逻辑差** |
| `tool_search.py` | 100 / 100 | **0 逻辑差**——DeferredToolCatalog/search 三种查询/assemble fail-closed/catalog_hash 全一致（diff 全是 docstring 中英） |
| `sync.py` | 48 / 47 | **0 逻辑差**——`make_sync_tool_wrapper` 事件循环检测/线程池卸载/contextvar 复制/`_get_runnable_config_param` 全一致 |
| `present_file_tool.py` | 61 / 67 | **0 逻辑差**——多文件 + `_normalize_presented_filepath` 路径归一化 + `resolve_virtual_path` 穿越校验 + `ToolMessage` 反馈全一致（diff 是 docstring + paths API 名差） |
| `setup_agent_tool.py` / `update_agent_tool.py` | 64/64, 149/155 | **0 逻辑差**（docstring 中英） |
| `view_image_tool.py` | 109 / 115 | **0 逻辑差**——diff 全是格式（mini 多行 vs 上游单行 Command）+ mini 加 `import logging`/`logger` + 类型注解差（mini `# type: ignore` 软加载 vs 上游 `ThreadDataState` 强类型）。`f"Error: {e}"` vs `{str(e)}` 等价 |
| `tools.py` | 103 / 107 | **近 0**——mini 用 dict 取值（`cfg["use"]`/`cfg.get("name")`，因 mini `config.tools` 是松散 `list[dict]`），上游用属性取值（`cfg.use`，上游是 `list[ToolConfig]` pydantic）。见 [config.md](config.md) §9。**功能等价**。另：mini 的 ACP 来源直接 `getattr(config, "acp_agents", {})`，上游分支 `get_acp_agents()` 访问器（mini 简化） |
| `invoke_acp_agent_tool.py` | 165 / 168 | **结构差但等价**——① ACP 工作区路径：mini 内联 `paths.base_dir / "users" / user_id / "threads" / thread_id / "acp-workspace"`，上游用 `paths.acp_workspace_dir(...)` 方法（mini paths.py 无此方法，同 [#13 sandbox](sandbox.md)「无 ACP workspace」）；② MCP servers 构建：mini 复用 `build_servers_config` helper，上游内联读 `get_enabled_mcp_servers()`（等价）；③ mini 加 `_INSTALL_HINT` + `_agent_attr` dict-or-attr helper |
| `task_tool.py` | 279 / 298 | **刻意 divergence**——上游从 `parent_context` 抽 `user_role`/`oauth_provider`/`oauth_id`/`run_id` 传给子代理 executor；**mini 不传**（这些是 Gateway auth 字段，mini 不 port，同 [#15 subagents](subagents.md) 结论）。其余是格式差 |
| `builtins/__init__.py` | 29 / 13 | mini **多导出**（`DeferredToolSetup`/`build_deferred_tool_setup`/`get_deferred_tools_prompt_section` 等）——API 面差异 |

**为什么这样？** tools 模块是**装配编排**——把各来源工具汇总喂给 agent，输入（config）和输出（工具列表）都不依赖 Gateway/IM。多数文件靠**反射加载**（resolve_variable）、**标记单一真相源**（mcp_metadata）、**横切包装**（sync）解耦，故忠实。两处真差异都有据：① **task_tool 不传 Gateway auth**（`user_role`/`oauth_provider`/`oauth_id`）——mini 不 port Gateway auth，刻意不传；② **invoke_acp 工作区路径内联**——mini paths.py 无 `acp_workspace_dir` 方法（同 #13 sandbox「mini 无 ACP workspace」）。`tools.py` 的 dict-vs-ToolConfig 是 mini 的配置层已知选择（[config.md](config.md) §9），功能等价。

---

## §11 常见问题 / 排错

**Q：我配了 web_search 但 agent 没这个工具？**
A：三查：① `config.yaml` 的 `tools[]` 有 `use: "deerflow.community.ddg_search.tools:web_search_tool"`；② `resolve_variable` 能 import（模块路径对、SDK 装了）；③ 没被 host-bash 过滤或 groups 过滤掉。

**Q：日志报 "Tool name mismatch"？**
A：`config.yaml` 的 `tools[].name` 与工具 `.name` 不一致。绑定用工具自己的 `.name`——把 config name 改成一致避免困惑。

**Q：日志报 "Duplicate tool name skipped"？**
A：两个工具同名（如 config 和内置都叫 `bash`）。去重保留优先级高的（config > builtins > MCP > ACP），低优先级的被跳过。改名或删重复配置。

**Q：MCP 工具很多，模型选不过来？**
A：开 `tool_search.enabled: true`。MCP 工具变延迟（agent 只看名字），用 `tool_search` 按需取 schema。省 token + 选更准。

**Q：agent 看不到 setup_agent / update_agent？**
A：正常。它们由 lead_agent factory 按运行时上下文绑定：setup_agent 仅 `is_bootstrap=True`（初始化新建 agent 流程），update_agent 仅在自定义 agent 的对话里（`runtime.context["agent_name"]` 已设）。默认 lead agent 对话不绑这俩。

**Q：view_image 为什么有时有有时没有？**
A：仅 `supports_vision=True` 的模型才绑。换非 vision 模型时 view_image 自动消失（条件加载）。

**Q：present_files 报 "Only files in /mnt/user-data/outputs can be presented"？**
A：`present_files` 收**路径列表**（`filepaths=[...]`，不再是单个），且每条路径会归一化 + 校验落在当前线程的 `outputs` 目录之下。把文件先写到 `/mnt/user-data/outputs/`（沙箱内视角）再展示；传 `../etc/passwd` 这类穿越路径或非 outputs 文件会被拒（返错误 ToolMessage，不中断 run）。

**Q：present_files 收虚拟路径还是宿主路径？**
A：都行。`_normalize_presented_filepath` 同时接受沙箱虚拟路径（`/mnt/user-data/outputs/x.md`）和宿主侧绝对路径，都解析到物理路径、校验在 `outputs` 下、回写成规范虚拟路径写进 artifacts。

**Q：invoke_acp_agent 调用报 "not installed"？**
A：`agent-client-protocol` 包没装。`pip install agent-client-protocol`。ACP 是可选能力，不装不影响其它工具。
