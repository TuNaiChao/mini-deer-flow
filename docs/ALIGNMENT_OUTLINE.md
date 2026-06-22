# mini-deer-flow 对齐 deer-flow 总修改提纲

> **版本：v1.2（已修订）** — 在 v1.1 基础上执行**「全面对标」指令**：沙箱 / subagent / memory / tool / skill / **mcp** 等模块**全面对标 deer-flow，不裁剪核心功能**；把原「可选/裁剪」的 mcp / community(联网) / uploads / agents_config / AIO 沙箱**全部提升为主线模块**（M10b / M20–M23），并修正 v1.1 的两处事实错误（沙箱工具数 5→7、subagent 执行器设计）。修订记录见文末「修订日志」。
>
> 本文档是 **master outline**：综合 agent / tools / skills / middleware / memory / models / runtime / persistence / 工程化 / mcp / community 全部模块的对齐分析，给出可被后续 AI 直接执行的文件级修改计划。
>
> **目标**：让 mini-deer-flow 在行为上**全面对标** deer-flow，且**每个模块可用、可靠**（有默认值、IO 卸载、错误兜底、可测试）。
>
> **执行约定**：
> 1. 每个模块完成后必须：① 代码 ② 对应测试文件 ③ 学习文档（面向小白，讲设计原理与应用方法）。
> 2. 参考实现：`../deer-flow/backend/packages/harness/deerflow/` 下同名文件，标注「移植」的文件可基本照搬，标注「适配」的需按 mini 现状调整。
> 3. 代码风格对齐 mini 现有：中文 docstring、`ruff`（行宽 240）、双引号、Python 3.12+ 类型注解。
> 4. **可靠性底线**：空 `config.yaml` 必须能以 `memory` 模式启动；任何阻塞 IO 必须走 `asyncio.to_thread`；任何 `wrap_tool_call` 必须 `raise GraphBubbleUp`。
> 5. 测试目录：`test/test_<module>.py`（**项目根，backend 外**，配 `pytest.ini`）；学习文档目录：`docs/<module>.md`。
> 6. 跨阶段不破坏既有：每完成一个 Phase 跑一次 `test/` 全量（`cd backend && make test`）。
> 7. **langgraph 版本要求**：需支持 `Runtime` / `ToolRuntime` / `configurable["__pregel_runtime"]` / `Command(goto=END)` / `get_config`（langgraph ≥ 1.1）。在 `packages/harness/pyproject.toml` 锁定下限。
> 8. **文档命名与位置**（重要）：每个模块的学习文档放 `docs/` 根，命名 `<module>.md`（**kebab-case 英文，禁止中文文件名**，如 `build.md` / `models.md` / `utils.md` / `user_context.md`）。文档**面向小白、从基础讲起**——每个名词第一次出现都要解释，范例见 `docs/build.md` 的「零基础先读」节。**旧版 / 待重写**的文档归档在 `docs/legacy/`（如 `legacy/中间件.md`），不要放回 `docs/` 根。三者分工：**改设计规格** → 本文件；**查/更新进度** → `docs/todo.md`；**写模块文档** → `docs/<module>.md`。

---

## 目录

> 📌 **进度跟踪（做到哪了 / 下次做什么）→ [todo.md](todo.md)**
> 本文件是**设计规格**（做成什么样），todo.md 是**进度看板**（做到哪了）。

- [Part A — 全局约定与依赖图](#part-a--全局约定与依赖图)
- [Part B — 推荐落地顺序（Phase 0–8 + 可选）](#part-b--推荐落地顺序phase-08--可选)
- [Part C — 分模块详细提纲](#part-c--分模块详细提纲)
- [Part D — 集成装配清单](#part-d--集成装配清单)
- [Part E — 可靠性总红线](#part-e--可靠性总红线)
- [Part F — 交付物检查清单](#part-f--交付物检查清单)
- [Part G — 修订日志](#part-g--修订日志)

---

## Part A — 全局约定与依赖图

### A.1 模块依赖图（决定落地顺序）

> **v1.2 起**：原「可选/裁剪」的 mcp / community / uploads / agents_config / AIO 沙箱**已全部纳入主线**（M10b / M20–M23），不再是「留接口不做」。下列依赖图已把它们并入。

```
build(pyproject/Makefile/conftest/gate) ── 全局基础设施
config(类型化, 含 model_config) ──┬─→ utils/time, reflection.resolve_class, paths.resolve_path
                                 ├─→ runtime/user_context ─────────────┐
models(factory 升级) ◄── config/model_config ──┐                      │
                                                 │                      │
   persistence(base/engine/models) ◄── runs/schemas + runs/store/base(ABC, 提前到 Phase 1)
                                                 │                      │
   runtime(checkpointer/events/journal/stream_bridge/serialization) ◄───┤
                                                 │                      │
   sandbox(local, 7 工具) ───────────────────────┤                      │
   sandbox/aio_sandbox(M10b, 生产隔离) ◄─────────┤                      │
   subagents(自定义子代理 + 隔离事件循环) ◄──────┤                      │
   tracing ◄── models(attach_tracing) ───────────┤                      │
   config/agents_config(M22, SOUL.md/AGENT_NAME_PATTERN) ─┐            │
   agents/memory ◄── models/create_chat_model + agents_config ─────────┤
   skills(含 skill_evolution) ──────────────────────────────────────────┤
   mcp(M20, cache/tools/session_pool/oauth) ──┐                          │
   community(M21, web_search/web_fetch) ──────┤                          │
   uploads(M23, markitdown) ──────────────────┤                          │
                                            ▼                           ▼
   tools(9 builtins + mcp_metadata + sync + skill_manage) ─┐
   agents/middlewares(新增+重做, 23 步含 Uploads) ──────────┤
   agents(factory/features/thread_state/                    │
          lead_agent/custom-agent 分支/build_middlewares) ◄─┘
                      │
   runs(manager/worker/store.memory) ◄── agent factory + 全部基础
                      │
   集成装配(lifespan + langgraph.json) ◄── 全部
```

> **依赖要点**：① M22 agents_config 必须先于 M13 memory（`AGENT_NAME_PATTERN`）与 M15 setup/update_agent 工具；② M20 mcp 必须先于/同期于 M15（`tool_search` 延迟工具）与 M16 `DeferredToolFilterMiddleware`；③ M23 uploads 必须先于 M16 第 3 步 `UploadsMiddleware`；④ M21 community 由 M15 经 `tools[].use:` 路径加载，agent 联网依赖它。

### A.2 两条硬约束

1. **Harness/App 边界**：`packages/harness/deerflow/` 内**禁止** import `app.*`（deer 用 `test_harness_boundary.py` 强制）。mini 暂无 app 层，但新增代码不得假设任何 FastAPI/Gateway 存在。
2. **可选依赖软加载**：sqlite/postgres/mcp/tiktoken/markitdown/asyncpg/aiosqlite 等扩展包一律 `try/except ImportError` + 回退内存/默认 + 可操作安装提示。对应 extras 必须在 `pyproject.toml` 声明（见 M-build）。

---

## Part B — 推荐落地顺序（Phase 0–8，全面对标，无核心裁剪）

| Phase | 模块 | 产出 | 可独立验证 |
|-------|------|------|-----------|
| **0 地基 + 工程化** | config 类型化（M0）、utils（M1）、reflection（M2）、user_context（M3）、**build（M-build）** | 强类型配置 + 公共工具 + 测试/打包基础设施 | ✅ 各自单测 + `make test/lint` 可跑 |
| **1 模型 + 持久化/运行时基础** | **models（M-models）**、persistence（M4，含 runs/schemas + runs/store/base）、checkpointer（M5）、events/store（M6）、journal（M7）、stream_bridge（M8）、serialization（M9） | 模型工厂 + 存储 + 采集 + 流桥 | ✅ 各自单测 |
| **2 沙箱/子代理/追踪/自定义 agent** | sandbox local（**M10，7 工具**）、**AIO 沙箱（M10b，生产隔离）**、subagents（**M11，自定义子代理**）、tracing（M12）、**agents_config（M22，SOUL.md/AGENT_NAME_PATTERN）** | 执行隔离 + 委派 + 链路追踪 + 自定义 agent | ✅ 单测 |
| **3 记忆** | agents/memory（M13） + memory_middleware/dynamic_context 重做 | 真记忆写入/注入（用 M22 的 AGENT_NAME_PATTERN） | ✅ `test_memory` |
| **4 技能** | skills（M14） + skill_activation + skill_evolution + prompt 集成 | /skill 激活 + allowed-tools + 自演化 | ✅ `test_skills` |
| **工具 + MCP + 联网** | tools（**M15，9 内置工具**）、**mcp（M20）**、**community（M21，web 搜索/抓取）** | 完整工具集 + MCP 工具 + 联网能力 | ✅ `test_tools/test_mcp/test_community` |
| **5.5 上传** | **uploads（M23）** + markitdown 转换 | 文件上传 + 文档解析（为 M16 UploadsMiddleware 铺路） | ✅ `test_uploads` |
| **6 中间件** | middlewares（M16）新增 16 + 重做 8（**23 步全部启用，含 Uploads**） | 23 步中间件链 | ✅ `test_middlewares` |
| **7 Agent 装配** | agents（M17）factory/features/thread_state/lead_agent（**含 custom-agent 分支**）/prompt/build_middlewares | SDK + config 双入口 | ✅ `test_agent` |
| **8 运行管理 + 集成** | runs manager/worker/store.memory（M18）、runtime/store（M19）、lifespan、langgraph.json | 端到端可跑 | ✅ `test_run_manager/worker/integration` |
| **可选（按需，不阻塞主线）** | guardrail 中间件、DeerFlowClient | 内容审查中间件 / 嵌入式客户端 | 视需求 |

> **关键顺序修正（v1.1）**：
> - **models 必须在 Phase 1**（M12 tracing 依赖 `attach_tracing`、M13 memory 依赖 `create_chat_model`、M17 依赖 `app_config`/`reasoning_effort`）。
> - **`runs/schemas.py` + `runs/store/base.py`（RunStore ABC）提前到 Phase 1**（persistence 的 `RunRepository(RunStore)` 继承它）；`runs/store/memory.py` + `manager.py` + `worker.py` 留 Phase 8。
>
> **关键顺序修正（v1.2，全面对标）**：
> - **mcp / community / uploads / agents_config / AIO 沙箱不再是「可选/裁剪」**，全部纳入主线（M10b / M20–M23）。它们是 6 大模块（sandbox/subagent/memory/tool/skill/mcp）的**直接依赖**，必须按依赖图先于/同期于消费者落地，否则消费者只能留 stub。
> - **M22 agents_config 提前到 Phase 2**（先于 M13 memory 与 M15 setup/update_agent）——消除 v1.1 的「AGENT_NAME_PATTERN 局部兜底」权宜。
> - **M20 mcp 提前到 Phase 5（与 M15 同期/先行）**——M15 的 `tool_search` 延迟工具、M16 的 `DeferredToolFilterMiddleware` 都依赖它。

### B.1 模块级线性修改顺序（推荐执行序）

> 这是比上面 Phase 表更细的**单线执行序列**——把每个模块按依赖关系排成一条龙，后续 AI 可从上到下逐个做。标注：✅已完成 / 🔶部分 / ⬜未开始。每步后括号是「为什么排这里」。
> **铁律**：每完成一步，跑一次 `test/` 全量（`cd backend && make test`），绿了再下一步。

**Phase 0 — 地基 + 工程化（最先，让「能跑测试」成立）**

1. **M-build** 工程化 ⬜ —— **必须最先**：pyproject extras + 包安装修正 + conftest + boundary/gate 测试。不先做这步，后面所有模块都「写完跑不了」（M-models 已踩此坑：uv 锁/包未装）。
2. **M0** config 类型化 🔶 —— 几乎所有模块读配置；补全子配置 schema + `config_version` + paths helpers。
3. **M1** utils（time/messages）⬜ —— 极小；persistence/memory 都要 `coerce_iso`/`now_iso`/`get_original_user_content_text`。
4. **M3** user_context ⬜ —— persistence/memory/sandbox/checkpointer 的 user_id 来源（三态 AUTO）。
   （**M2** reflection ✅ 已具备 `resolve_class`，**跳过**。）
   → 此时可回头验证 M-models 测试跑绿（M-build 修好了环境）。

**Phase 1 — 模型 + 持久化/运行时基础（叶子基础设施）**

5. **M-models** ✅ —— 已完成，仅待验证。
6. **M4** persistence ⬜（含 `runs/schemas.py` + `runs/store/base.py` 前置）—— 存储地基；RunStore ABC 必须先于 RunRepository（已修正的循环依赖）。
7. **M5** checkpointer ⬜ —— 依赖 config/database_config + sqlite_utils；委托 LangGraph Saver。
8. **M6** events/store ⬜ —— 依赖 M4 的 `RunEventRow`；消息/轨迹存储（memory/jsonl/db）。
9. **M9** serialization ⬜ —— 纯函数，无依赖；worker 与未来 REST 要用，先建好。
10. **M8** stream_bridge ⬜ —— 纯组件，仅依赖 config；SSE 桥接。
11. **M7** journal ⬜ —— 依赖 M6（写入 RunEventStore）；callback 采集器。

**Phase 2 — 沙箱 / 子代理 / 追踪 / 自定义 agent**

12. **M10** sandbox（local）⬜ —— 依赖 config/sandbox_config + user_context + paths；提供 **7 工具**（bash/ls/**glob**/**grep**/read_file/write_file/str_replace）+ SandboxMiddleware + SandboxAuditMiddleware。
13. **M10b** AIO 沙箱（生产隔离）⬜ —— 依赖 M10；`community/aio_sandbox/`（Docker/K8s provisioner），让 untrusted 代码有真实隔离边界（v1.2 恢复，不再裁剪）。
14. **M11** subagents（**自定义子代理**）⬜ —— registry/status_contract/**executor（单 scheduler pool + 持久化隔离事件循环）** + builtins(general-purpose/bash) + **config 自定义子代理 + per-agent overrides** + token_collector；真实 agent 构造依赖 M17，骨架可先建。
15. **M12** tracing ⬜ —— 依赖 user_context；落地后 M-models 的 `attach_tracing=True` 路径自动生效。
16. **M22** agents_config（自定义 agent）⬜ —— SOUL.md + AgentConfig + `AGENT_NAME_PATTERN` + validate/resolve/load/list；**先于 M13/M15**（被 memory 与 setup/update_agent 工具依赖）。

**Phase 3 — 记忆**

17. **M13** memory ⬜ —— 依赖 M-models（`create_chat_model`）+ config/memory_config + user_context + paths + **M22（AGENT_NAME_PATTERN）**；重做 MemoryMiddleware + DynamicContextMiddleware。

**Phase 4 — 技能**

18. **M14** skills ⬜ —— 依赖 config/skills_config + skill_evolution_config + extensions_config + reflection + utils/messages；含 SkillActivationMiddleware + skill_manage + 自演化。

**Phase 5 — 工具 + MCP + 联网**

19. **M20** mcp ⬜ —— 依赖 extensions_config（mcpServers）+ reflection；cache/tools/session_pool/oauth/client；**先于/同期于 M15**（tool_search 依赖）。
20. **M21** community ⬜ —— web 搜索/抓取 provider 框架 + 核心 provider（ddg_search/tavily/jina_ai），其余 provider 软加载；由 M15 经 `tools[].use:` 加载。
21. **M15** tools ⬜ —— 依赖 sandbox/subagents/models/config/**M20 mcp**/**M22 agents_config**；补全 **9 内置工具** + MCP 标记 + sync + skill_manage + tool_search + 去重。

**Phase 5.5 — 上传**

22. **M23** uploads ⬜ —— 依赖 config/paths + markitdown（软加载）；文件上传 + markitdown 转换 + 安全（O_NOFOLLOW/路径穿越）；**先于 M16 第 3 步**。

**Phase 6 — 中间件**

23. **M16** middlewares ⬜ —— 依赖几乎所有业务模块（sandbox/subagents/skills/memory/tracing/**uploads**）；新增 16 + 重做 8，重写 `build_middlewares` 为 23 步（**含 UploadsMiddleware，不再跳过**）。

**Phase 7 — Agent 装配**

24. **M17** agents ⬜ —— 依赖 Phase 2–6 + tools + checkpointer；factory/features/thread_state/lead_agent（**含 custom-agent 分支，依赖 M22**）/prompt。完成后 M11 的真实 agent 构造才可用。

**Phase 8 — 运行管理 + 集成**

25. **M18** runs（manager/worker/store.memory）⬜ —— 依赖 M17 agent factory + 全部 runtime 基础；run 生命周期 + lifespan 装配。
26. **M19** runtime/store ⬜ —— LangGraph BaseStore 工厂（memory 版即可）。
27. **集成装配** ⬜ —— lifespan（init_engine + make_checkpointer + make_stream_bridge + make_thread_store + RunManager + reconcile + shutdown）+ 对齐 langgraph.json + config.example.yaml。端到端冒烟。

**可选模块（任何时候按需，不阻塞主线）**

- **guardrail 中间件**（依赖 guardrails 模块，本期不做）/ **DeerFlowClient**（嵌入式客户端，mini 走 langgraph dev + 未来 Gateway，不做）。

> v1.2 起 mcp/community/uploads/agents_config/AIO 沙箱**已进入主线**（见上 M10b/M20–M23），不再在此列「不做」。
> 并行提示：Phase 1 内 M9/M8 与 M5–M7 无强依赖，可并行；Phase 2 内 M10/M11/M12/M22 相互独立可并行。但**单线序列**最稳妥，避免合并冲突与依赖错配。

---

## Part C — 分模块详细提纲

> 每个模块的统一小节：现状 / 目标 / 文件清单（新建 N / 修改 M）/ 关键实现要点 / 依赖 / 可靠性要点 / 测试文件 / 学习文档。

---

### M-build. 工程化基础设施 — Phase 0

- **现状**：`backend/pyproject.toml` 仅声明 `fastapi/uvicorn + workspace`；无 Makefile、无 `test/conftest.py`、无 harness boundary / blocking-io gate、无样例文件。
- **目标**：补齐打包 extras、测试基础设施、边界/IO 强制测试、样例。
- **文件清单**：
  - **修改**：`backend/pyproject.toml`（或 `packages/harness/pyproject.toml`）—— 声明 `[project.optional-dependencies]`：`sqlite`(`langgraph-checkpoint-sqlite`)、`postgres`(`langgraph-checkpoint-postgres`、`psycopg`、`asyncpg`)、`aiosqlite`、`mcp`(`langchain-mcp-adapters`)、`vision`/`tiktoken`、`uploads`(`markitdown`)；锁 langgraph 下限（≥1.1，需支持 `Runtime`/`ToolRuntime`/`__pregel_runtime`）。
  - **新建**：根 `Makefile`（`test`/`lint`/`format`/`dev` 目标，对齐 deer 命名）、`packages/harness/pyproject.toml` 的 `[tool.ruff]`（line-length=240）。
  - **新建**：`backend/test/conftest.py`（sys.modules mock 模板，解循环导入；公共 fixtures：临时 base_dir、mock model）。
  - **新建**：`backend/test/test_harness_boundary.py`（断言 `packages/harness/deerflow/` 下无 `import app.*`；mini 暂无 app 层时作为占位与未来护栏）。
  - **新建**：`backend/test/blocking_io/test_io_offload.py`（至少一个锚点：`LocalSkillStorage.load_skills` / memory JSON / sqlite 路径 必须经 `asyncio.to_thread`）。
  - **新建样例**：`mini-deer-flow/.env.example`（已有则对齐）、`backend/extensions_config.json`（补 `mcpServers`/`skills` map 示例，含 `$VAR` 占位）、`skills/public/<示例>/SKILL.md`。
- **关键要点**：extras 命名与各模块 install hints 一致；conftest 提供 `tmp_data_dir` fixture 供所有持久化测试隔离。
- **依赖**：无（最先做）。
- **可靠性**：gate 测试让「IO 卸载」「harness 边界」从口头红线变为 CI 强制。
- **测试**：`backend/test/test_harness_boundary.py` + `backend/test/blocking_io/test_io_offload.py`。
- **学习文档**：`docs/build.md` —— pyproject extras 设计、uv workspace、ruff 配置、conftest 与循环导入、为什么需要 boundary/gate 测试。

---

### M0. config（配置类型化）— Phase 0

- **现状**：[app_config.py](../backend/packages/harness/deerflow/config/app_config.py) 用 `dict[str, Any]` 兜底 `database/memory/title/loop_detection/sandbox` 等；`build_middlewares` 到处 `isinstance(x, dict)` 判断；`model_config` 缺 when_thinking_enabled 等。
- **目标**：全部子配置强类型化（pydantic BaseModel），带默认值，空配置可启动。
- **文件清单**：
  - **新建**：`config/database_config.py`、`checkpointer_config.py`、`run_events_config.py`、`stream_bridge_config.py`、`memory_config.py`、`title_config.py`、`summarization_config.py`、`loop_detection_config.py`、`token_usage_config.py`、`tool_output_config.py`、`tool_search_config.py`、`safety_finish_reason_config.py`、`sandbox_config.py`、`subagents_config.py`、`skills_config.py`、`skill_evolution_config.py`、`reload_boundary.py`
  - **修改**：`config/app_config.py`（字段改类型化对象 + `get_model_config()` + `config_version` + 保留 mtime 热重载）、`config/__init__.py`（导出）、`config/paths.py`（补 `resolve_path` + `get_paths()` 单例，含 base_dir/memory_file/user_memory_file/user_agent_memory_file/agent_memory_file）、`config/model_config.py`（补 `when_thinking_enabled`/`use_responses_api`/`output_version`/`reasoning_effort`）
  - **适配**：`config/extensions_config.py` 补 `is_skill_enabled(name, category) -> bool`（沿用 mini 的 dataclass 风格）
  - **明确替代**：deer 用 `config/runtime_paths.py`（project_root/resolve_path/existing_project_file）；mini **一律以 `config/paths.py` 的 `resolve_path`/`PROJECT_ROOT` 替代**，新增代码不得 import `runtime_paths`。
- **关键要点**：`DatabaseConfig` 派生 `sqlite_path/checkpointer_sqlite_path/app_sqlalchemy_url`；`MemoryConfig` 全字段默认值；`reload_boundary.STARTUP_ONLY_FIELDS` 列表。
- **依赖**：无（最底层）。
- **可靠性**：默认 `backend="memory"`；`$VAR` 环境变量展开保留；**改 schema 时同步 bump `config.example.yaml` 的 `config_version`**。
- **测试**：`backend/test/test_config.py`（扩展现有）—— 子配置默认值、空 YAML 启动、env 展开、get_model_config 回退、mtime 热重载、model_config 新字段。
- **学习文档**：`docs/config.md` —— 配置层级、强类型 vs dict 的取舍、热重载边界、env 展开、config_version、runtime_paths→paths 替代说明。

---

### M1. utils（公共工具）— Phase 0

- **现状**：无 `utils/`。
- **目标**：提供两个模块共享的时间与消息工具。
- **文件清单**：
  - **新建**：`utils/__init__.py`、`utils/time.py`（`now_iso()`、`coerce_iso(val)`）、`utils/messages.py`（`get_original_user_content_text(content, additional_kwargs)`）
- **关键要点**：`coerce_iso` 把 SQLite 读回的 naive datetime 归一为 UTC ISO；`get_original_user_content_text` 处理 list-of-blocks 与 `hide_from_ui` 标记。
- **依赖**：无。
- **测试**：`backend/test/test_utils.py` —— `now_iso` 带时区、`coerce_iso` 边界、消息文本抽取（str/list/dict）。
- **学习文档**：`docs/utils.md` —— 为什么需要 coerce_iso（SQLite 时区丢失）、消息内容的三种形态。

---

### M2. reflection（反射）— Phase 0

- **现状**：[resolver.py](../backend/packages/harness/deerflow/reflection/resolver.py) 仅 `resolve_variable`。
- **目标**：补 `resolve_class(path, base_class)`。
- **文件清单**：**修改** `reflection/resolver.py`（补 resolve_class：import_module + getattr + `issubclass(base_class)` 校验 + 可操作安装提示）、`reflection/__init__.py`（导出）。
- **依赖**：无。
- **测试**：`backend/test/test_reflection.py`（扩展现有）—— resolve_class 成功/非子类报错/模块缺失提示。
- **学习文档**：`docs/reflection.md` —— 反射加载如何让 config 的 `use: "module:var"` 生效、与 import 的区别。

---

### M3. runtime/user_context（用户上下文）— Phase 0

- **现状**：无。
- **目标**：用户隔离基石（memory/thread_data/checkpointer 的 user_id 来源）。
- **文件清单**：**新建** `runtime/__init__.py`(占位)、`runtime/user_context.py`。
- **关键要点**：`get_effective_user_id()`（优先 `runtime.context["user_id"]` → langgraph `get_config().configurable.user_id` → 回退 `"default"`）、`resolve_runtime_user_id(runtime)`、`AUTO`/`_AutoSentinel`/`resolve_user_id(user_id, method_name)` 三态、`get_current_user()`。
- **依赖**：langgraph（`get_config`）。
- **可靠性**：无鉴权回退 `"default"`；contextvar 未设时返回 None 而非报错。
- **测试**：`backend/test/test_user_context.py` —— 三态解析、无 contextvar 回退、default 常量。
- **学习文档**：`docs/user_context.md` —— 为什么 ContextVar 不跨线程（memory queue 必须显式传 user_id）、三态 AUTO 的设计。

---

### M-models. models（模型工厂升级）— Phase 1

- **现状**：[models/factory.py](../backend/packages/harness/deerflow/models/factory.py) 签名 `create_chat_model(name=None, *, thinking_enabled=False, **kwargs)`，无 `app_config`/`attach_tracing`/`reasoning_effort`，无 thinking 覆盖/stream 默认值/缺包提示。
- **目标**：对齐 deer factory，支撑 tracing/memory/lead_agent 的调用。
- **文件清单**：
  - **修改**：`models/factory.py`（升级 `create_chat_model(name=None, *, thinking_enabled=False, reasoning_effort=None, app_config=None, attach_tracing=True)`）
  - **修改**：`models/__init__.py`（导出 `create_chat_model`；移除或保留 `get_default_model` 视依赖）
  - **可选新建**：`models/patched_openai.py`、`models/vllm_provider.py`、`models/claude_provider.py`、`models/credential_loader.py` 等（按需，本期可不做）
- **关键要点（移植 deer）**：
  - `_deep_merge_dicts`：合并 `when_thinking_enabled` 覆盖（vLLM Qwen `chat_template_kwargs.enable_thinking`、旧 `thinking` 别名归一）。
  - `_enable_stream_usage_by_default`：OpenAI 兼容网关默认 `stream_usage=True`（否则 TokenUsageMiddleware 拿不到用量）。
  - `_apply_stream_chunk_timeout_default`：默认 `stream_chunk_timeout=240s`（推理模型首 chunk 可达 90–150s，防 StreamChunkTimeoutError）。
  - supports_thinking/vision 校验（不支持时 thinking 降级为 False 并告警）。
  - `attach_tracing=True` 时模型级挂 `build_tracing_callbacks()`（独立调用方，如 MemoryUpdater）；`False`（in-graph 调用）跳过，由图根注入。
  - 缺 provider 模块 → 可操作安装提示（如 `uv add langchain-google-genai`）。
- **依赖**：config/model_config、reflection.resolve_class、tracing（M12，`attach_tracing=True` 路径；为避免循环，tracing 模块须先于 models 的 import 时调用——tracing 在 Phase 2，但 models 在 Phase 1：**解决**：models 内 `attach_tracing` 路径用**懒导入** `from deerflow.tracing import build_tracing_callbacks`，且默认 `attach_tracing=False`，使 Phase 1 单测不依赖 tracing；Phase 2 tracing 落地后自动生效）。
- **可靠性**：缺包软加载；thinking/vision 不匹配自动降级；推理模型超时阈值放宽。
- **测试**：`backend/test/test_model.py`（扩展现有）—— 基本创建、app_config 注入、attach_tracing 开关、thinking 覆盖合并、stream_usage/stream_chunk_timeout 默认值、supports 校验降级、缺包提示。
- **学习文档**：`docs/models.md` —— 反射实例化、thinking/vision 能力门控、attach_tracing 的两个调用方、stream 超时为何要放宽。
- **裁剪**：provider 子类（patched_deepseek/mimo/minimax/stepfun、mindie、openai_codex、assistant_payload_replay）**本期不做**。

---

### M4. persistence（SQLAlchemy 持久化层）— Phase 1

- **现状**：无。
- **目标**：给 RunStore/RunEventStore/ThreadMetaStore 的 db 实现提供 ORM + engine；并**提前落地 runs 基类层**。
- **文件清单**：
  - **前置新建（runs 基类层，Phase 1）**：`runtime/runs/__init__.py`(占位)、`runs/schemas.py`（`RunStatus`/`DisconnectMode`）、`runs/store/__init__.py`(占位)、`runs/store/base.py`（`RunStore` ABC）。理由：`RunRepository(RunStore)` 继承它。
  - **新建**：`persistence/__init__.py`、`base.py`（`Base(DeclarativeBase)` + `to_dict(exclude)`）、`engine.py`（`init_engine/init_engine_from_config/get_session_factory/get_engine/close_engine`）、`json_compat.py`
  - **新建**：`persistence/models/__init__.py`、`models/run_event.py`（`RunEventRow`）、`models/run.py`（`RunRow`）
  - **新建**：`persistence/run/__init__.py`、`run/model.py`、`run/sql.py`（`RunRepository(RunStore)`）
  - **新建**：`persistence/thread_meta/__init__.py`、`thread_meta/base.py`（`ThreadMetaStore` ABC）、`thread_meta/memory.py`、`thread_meta/sql.py`、`thread_meta/model.py`（`ThreadMetaRow`）
- **关键要点**：engine sqlite 连接级 `PRAGMA WAL/synchronous=NORMAL/foreign_keys=ON`；postgres `pool_pre_ping` + asyncpg 缺失报可操作错 + `_auto_create_postgres_db`；memory 模式 no-op，`get_session_factory()` 返回 None；`create_all` 在 models 未全建时 no-op。
- **依赖**：config/database_config、utils/time、runtime/user_context、`runs/store/base`（同 Phase 1 前置）。
- **可靠性**：WAL 并发读写；json_serializer `ensure_ascii=False`；`create_all` try/except 自动建库。
- **测试**：`backend/test/test_persistence.py` —— engine sqlite WAL 生效、memory no-op、auto-create、RunEventRow/RunRow CRUD、ThreadMeta 增删查、UUID→str 边界、RunStore ABC 契约。
- **学习文档**：`docs/persistence.md` —— ORM/DeclarativeBase、WAL 为何并发安全、checkpointer 表与 app 表的物理分离、memory 降级策略、runs 基类为何提前。
- **裁剪**：`feedback/`、`user/`、`channel_connections/`、`migrations/`(Alembic) **本期不做**。

---

### M5. runtime/checkpointer（检查点工厂）— Phase 1

- **现状**：无（langgraph.json 现无 checkpointer 段）。
- **目标**：委托 LangGraph 内置 Saver，async cm + sync 单例。
- **文件清单**：
  - **新建**：`runtime/store/__init__.py`、`runtime/store/_sqlite_utils.py`（`resolve_sqlite_conn_str`、`ensure_sqlite_parent_dir`）
  - **新建**：`runtime/checkpointer/__init__.py`、`checkpointer/provider.py`（sync `get_checkpointer` 单例 + `reset_checkpointer` + `checkpointer_context` + install hints 常量）、`checkpointer/async_provider.py`（`make_checkpointer(app_config)`）
- **关键要点**：优先级 legacy `checkpointer:` > 统一 `database:`(非 memory) > InMemorySaver；sqlite 路径准备 `await asyncio.to_thread`；postgres 池 keepalive（`keepalives_idle=60`、`check_connection`、`prepare_threshold=0`）；`await saver.setup()`；**不手写 BaseCheckpointSaver 子类**。
- **依赖**：config/database_config + checkpointer_config、store/_sqlite_utils。
- **可靠性**：#1912 父目录保护；缺包报可操作错。
- **测试**：`backend/test/test_checkpointer.py` —— memory/sqlite 切换、ensure_sqlite_parent_dir、缺包提示、setup 后 aput/aget_tuple、同步单例 reset。
- **学习文档**：`docs/checkpointer.md` —— 为什么用 context manager 而非工厂返回实例、委托 Saver 而非自建、langgraph.json checkpointer.path。

---

### M6. runtime/events/store（运行事件存储）— Phase 1

- **现状**：无。
- **目标**：消息+轨迹统一存储接口，3 后端（memory/jsonl/db）。
- **文件清单**：
  - **新建**：`runtime/events/__init__.py`、`events/store/__init__.py`、`store/base.py`（`RunEventStore` ABC，8 方法）、`store/memory.py`、`store/jsonl.py`、`store/db.py`（`DbRunEventStore`，依赖 `persistence/models/run_event`）
- **关键要点**：seq 线程内严格递增；jsonl 路径 `.deer-flow/threads/{tid}/runs/{rid}.jsonl` + `_SAFE_ID_PATTERN` 防穿越 + 每线程 `asyncio.Lock` + lazy seq + 全 IO `to_thread`；db 用 `FOR UPDATE`(sqlite)/`pg_advisory_xact_lock`(postgres) 保单调 seq + trace 截断 `max_trace_content` + JSON content 往返 + `user_id` UUID→str stamp。
- **依赖**：config/run_events_config、persistence/models/run_event、runtime/user_context。
- **可靠性**：阻塞 IO 卸载；并发 seq 单调；路径穿越防御。
- **测试**：`backend/test/test_events.py` —— seq 单调、路径穿越拒绝、并发写锁、jsonl delete 清计数器/锁、db FOR UPDATE/advisory、trace 截断、JSON 往返、双向游标分页。
- **学习文档**：`docs/run_event_store.md` —— message vs trace 的 category 区分、seq 单调为何要锁、jsonl 单进程限制、db 的 advisory lock。

---

### M7. runtime/journal（RunJournal 采集器）— Phase 1

- **现状**：无。
- **目标**：LangChain 回调 → RunEventStore 写入侧采集 + token 核算。
- **文件清单**：**新建** `runtime/journal.py`（`RunJournal(BaseCallbackHandler)`）。
- **关键要点**：`__init__(run_id, thread_id, event_store, *, track_token_usage, flush_threshold=20, progress_reporter: Callable|None, progress_flush_interval=5.0)`；回调 `on_chain_start/end/error`、`on_chat_model_start`（首条 human 抽取处）、`on_llm_end`（token 分桶 + 按 run_id 去重防双计）、`on_tool_end`；`_put`→buffer→达阈值 `_flush_sync`（同步回调内检测事件循环→`create_task`，无循环留 buffer）；`_pending_flush_tasks` 防并发写同库；失败 batch 回插；`_schedule_progress_flush` 节流；`record_external_llm_usage_records`、`record_middleware`、`get_completion_data`、`had_llm_error_fallback`。
- **依赖**：runtime/events/store（硬）；`progress_reporter` 是 **Phase 8 worker 注入的 callable**（`run_manager.update_run_progress`），journal 模块本身只收 `Callable`，**无模块级循环依赖**。
- **可靠性**：同步回调不阻塞事件循环；flush 在途去重；error fallback 检测供 worker 判状态。
- **测试**：`backend/test/test_journal.py` —— token 分桶去重、sync→async flush、失败回插、progress 节流（用 mock callable）、error fallback、record_middleware。
- **学习文档**：`docs/run_journal.md` —— RunJournal 与 RunEventStore 的「写入侧/存储侧」关系、为什么 on_chat_model_start 抽首条 human、caller tag 识别、progress_reporter 的注入时机。

---

### M8. runtime/stream_bridge（SSE 流桥接）— Phase 1

- **现状**：无。
- **目标**：生产者-消费者解耦，有界回放 + 重连。
- **文件清单**：**新建** `runtime/stream_bridge/__init__.py`、`stream_bridge/base.py`（`StreamEvent` + 哨兵 + `StreamBridge` ABC + no-op `close`）、`stream_bridge/memory.py`（`MemoryStreamBridge`）、`stream_bridge/async_provider.py`（`make_stream_bridge`）。
- **关键要点**：`queue_maxsize=256` 有界窗口 + eviction + `start_offset`；id=`"{ts_ms}-{seq}"`；`_resolve_start_offset` 支持 Last-Event-ID 重连；落后窗口从 start_offset 恢复；心跳；`close()`。
- **依赖**：config/stream_bridge_config。
- **可靠性**：长 run 不爆内存；重连补播；心跳防代理掐断。
- **测试**：`backend/test/test_stream_bridge.py` —— 有界 evict、Last-Event-ID 重连、落后恢复、心跳、END 终止、close。
- **学习文档**：`docs/stream_bridge.md` —— 生产者-消费者为何解耦、Last-Event-ID 重连、有界窗口与 start_offset、心跳的必要性。

---

### M9. runtime/serialization + converters（序列化）— Phase 1

- **现状**：无。
- **目标**：LangChain/LangGraph 对象 → JSON 的单一真相源 + 消息↔事件转换。
- **文件清单**：**新建** `runtime/serialization.py`（`serialize_lc_object`/`serialize_channel_values`/`strip_data_url_image_blocks`/`serialize_messages_tuple`/`serialize(obj, mode)`）、`runtime/converters.py`（消息/事件互转辅助，**可后补**，先占位）。
- **关键要点**：剥 `__pregel_*`/`__interrupt__`；剥 hide_from_ui 的 base64 image_url。
- **依赖**：无（仅类型）。
- **测试**：`backend/test/test_serialization.py` —— 各模式序列化、剥离内部键、image_url 剥离保留顺序。
- **学习文档**：`docs/serialization.md` —— 为什么统一序列化、`__pregel_*` 为何要剥、base64 图片剥离的体积问题、converters 何时需要。

---

### M10. sandbox（沙箱，local provider）— Phase 2

- **现状**：**部分代码已落地**（`sandbox/tools.py` ~1150 行、`sandbox/local/local_sandbox.py` ~620 行、`sandbox/middleware.py`/`sandbox.py`/`sandbox_provider.py`/`security.py` 已存在，共 ~2300 行），但**缺件**：`sandbox/local/local_sandbox_provider.py`（**provider 未落地**——config 引用的 `deerflow.sandbox.local:LocalSandboxProvider` 不存在）、`sandbox/search.py`（glob/grep 工具实现）、`sandbox/file_operation_lock.py`、`sandbox/exceptions.py`、`sandbox/local/list_dir.py`、`sandbox/__init__.py` 导出；且**无 `test_sandbox.py`、无 `docs/sandbox.md`**。⚠️ v1.1 误记「5 工具」——deer 实为 **7 工具**（缺 glob/grep 的实现）。
- **目标**：本地代码执行隔离 + **7 工具**（bash/ls/glob/grep/read_file/write_file/str_replace）+ 双中间件（Sandbox + SandboxAudit）。
- **文件清单**：
  - **新建**：`sandbox/__init__.py`、`sandbox/exceptions.py`（7 个异常类：SandboxError/SandboxNotFoundError/SandboxRuntimeError/SandboxCommandError/SandboxFileError/SandboxPermissionError/SandboxFileNotFoundError，带结构化 details）、`sandbox/file_operation_lock.py`（per-(sandbox,path) `threading.Lock` + WeakValueDictionary 自动回收）、`sandbox/search.py`（`find_glob_matches`/`find_grep_matches`/`GrepMatch` + 57 项忽略模式 + 二进制检测）
  - **新建/补全**：`sandbox/sandbox.py`（抽象 `Sandbox`：execute_command/read_file/download_file/list_dir/write_file/glob/grep/update_file）、`sandbox/sandbox_provider.py`（`SandboxProvider` ABC + sync/async acquire + `uses_thread_data_mounts`/`needs_upload_permission_adjustment` + 单例 `get_sandbox_provider`/`reset`/`shutdown`/`set_sandbox_provider`）、`sandbox/tools.py`（**7 工具** + `validate_local_tool_path`/`validate_local_bash_command_paths`/`mask_local_paths_in_output`/`replace_virtual_path*`/`ensure_sandbox_initialized`，工具经 `Runtime` 读 state 里的 sandbox + async 变体）、`sandbox/middleware.py`（**`SandboxMiddleware`** lazy_init 写 sandbox_id 到 state + **`SandboxAuditMiddleware`** 审计 bash/文件操作）、`sandbox/security.py`（`is_host_bash_allowed`/`uses_local_sandbox_provider`/错误提示常量）
  - **新建**：`sandbox/local/__init__.py`、`sandbox/local/list_dir.py`（目录树 max 2 层）、`sandbox/local/local_sandbox.py`（`LocalSandbox`：路径翻译 + 反向解析 + 只读校验 + 跨平台 shell 探测 + `is_local_sandbox`）、**`sandbox/local/local_sandbox_provider.py`**（`LocalSandboxProvider`：`uses_thread_data_mounts=True`、每线程 `local:{thread_id}`、`PathMapping` 虚拟→物理、LRU 上限默认 256、acquire/release/reset/shutdown）
- **关键要点**：虚拟路径 `/mnt/user-data/{workspace,uploads,outputs}`、`/mnt/skills`、`/mnt/acp-workspace` + 自定义 `mounts` → 物理目录翻译（最长前缀匹配）；每线程 `local:{thread_id}`；`SandboxMiddleware(lazy_init=True)` 用 `wrap_tool_call` 把 sandbox_id 回写 state（local 变更需 reducer 可见）；glob/grep 的 `max_results` 上限 + 截断提示；write_file 80KB 非追加上限；download_file 100MB 上限；路径穿越 `..` 拒绝；输出 mask 物理路径。
- **依赖**：config/sandbox_config（含 `allow_host_bash`/`bash_output_max_chars`/`read_file_output_max_chars`/`ls_output_max_chars` + AIO 字段 image/port/replicas/container_prefix/idle_timeout/mounts/environment）、runtime/user_context、config/paths、agents/thread_state（ThreadDataState）。
- **可靠性**：路径翻译作 defense-in-depth；LRU 上限防泄漏；file_operation_lock 串行同文件写；权限错误可操作提示。
- **测试**：`backend/test/test_sandbox.py` —— acquire/release（含 LRU 淘汰）、虚拟路径翻译（含只读校验 + `..` 拒绝）、bash 执行 + 输出 mask、**7 工具**（含 glob/grep max_results 截断）、host-bash 过滤、SandboxMiddleware lazy_init 回写、SandboxAuditMiddleware 审计、Runtime 注入读 sandbox、异常类 details。
- **学习文档**：`docs/sandbox.md` —— 虚拟路径系统、provider 模式、为什么 local 模式要 path translation、双中间件职责、工具如何从 state 取 sandbox、local provider 为何不是安全边界（引出 M10b）。
- **（v1.2 取消裁剪）**：Docker/AIO provisioner 见 **M10b**，不再是「不做」。

---

### M10b. AIO 沙箱（生产隔离，Docker/K8s）— Phase 2（v1.2 恢复）

- **现状**：无。v1.1 标「裁剪不做」——v1.2 **恢复**为独立模块：LocalSandboxProvider 不是安全边界，生产/多租户/untrusted 代码必须有真实容器隔离。
- **目标**：HTTP 容器化沙箱 + 暖池 + 跨进程发现 + 优雅关闭，对齐 deer `community/aio_sandbox/`。
- **文件清单**：
  - **新建**：`community/__init__.py`、`community/aio_sandbox/__init__.py`、`community/aio_sandbox/sandbox_info.py`（`SandboxInfo` dataclass）、`community/aio_sandbox/backend.py`（`SandboxBackend` ABC：create/destroy/discover/list_running/is_alive + `wait_for_sandbox_ready`/`_async` 就绪轮询）、`community/aio_sandbox/local_backend.py`（`LocalContainerBackend`：Docker/Apple Container，端口自分配 + `{prefix}-{sandbox_id}` 命名 + 卷挂载 + 环境变量 + 健康检查）、`community/aio_sandbox/remote_backend.py`（`RemoteSandboxBackend`：K8s/provisioner_url 动态创建 pod，无本地生命周期）、`community/aio_sandbox/aio_sandbox.py`（`AioSandbox`：HTTP client + 线程锁串行命令 + `ErrorObservation` 重试 + `download_file` 分块 100MB + glob/grep 远端搜本端滤）、`community/aio_sandbox/aio_sandbox_provider.py`（`AioSandboxProvider`：in-process 缓存 + 暖池 + 文件锁跨进程发现 + 暖池复用 + idle 超时回收 + 启动收养孤儿 + SIGTERM/SIGINT/SIGHUP 优雅关闭 + `provisioner_url` 选 remote 否则 local）
- **关键要点**：缓存层级 in-process → warm_pool → backend discover → create；跨进程 `{thread_id}/{sandbox_id}.lock` 排他锁 + per-thread 串行；release 移入 warm_pool 并关 HTTP client 防套接字泄漏；destroy 关 client；shutdown 清全部 + 停 idle 检查线程；`is_host_bash_allowed` 对非 local provider 返回 True。
- **依赖**：M10（Sandbox/SandboxProvider 抽象 + security）、config/sandbox_config（image/port/replicas/container_prefix/idle_timeout/mounts/environment/provisioner_url）、`agent_sandbox` SDK（软加载，可操作安装提示）。
- **可靠性**：`asyncio.to_thread` 卸载所有阻塞 IO；HTTP client 生命周期管理防泄漏；session 损坏自愈；启动收养孤儿容器。
- **测试**：`backend/test/test_aio_sandbox.py`（hermetic，mock HTTP/Docker client）—— acquire 三级缓存命中/暖池复用/跨进程发现/创建、release 入暖池、destroy 关 client、idle 超时回收、shutdown 清理、remote vs local backend 选择、ErrorObservation 重试、优雅关闭信号。
- **学习文档**：`docs/aio_sandbox.md` —— 为什么需要容器隔离（local 非边界）、暖池设计、跨进程文件锁、Docker vs K8s 后端、优雅关闭与孤儿收养。
- **软加载**：`agent_sandbox`/`docker`/`aiohttp` 缺包时 AIO provider 不可用，回退 local（可操作安装提示）。

---

### M11. subagents（子代理，含自定义子代理）— Phase 2

- **现状**：无。
- **目标**：`task` 工具委派 + 后台执行 + 限流 + **config 自定义子代理 + per-agent 覆盖**（v1.2 恢复全面对标）。
- **文件清单**：
  - **新建**：`subagents/__init__.py`（导出 `SubagentConfig`/`SubagentExecutor`/`SubagentResult`/registry 函数）、`subagents/config.py`（`SubagentConfig` dataclass：name/description/system_prompt/tools/disallowed_tools/skills/model/max_turns/timeout_seconds + `resolve_subagent_model_name`[inherit 或显式]）、`subagents/registry.py`（`BUILTIN_SUBAGENTS` + `_build_custom_subagent_config`(从 config.yaml `subagents.custom_agents`) + `get_subagent_config`(built-in→custom→per-agent override 合并) + `list_subagents`/`get_subagent_names`/`get_available_subagent_names`(按 sandbox 安全过滤隐藏 bash)）、`subagents/executor.py`（`SubagentStatus` 枚举[PENDING/RUNNING/COMPLETED/FAILED/CANCELLED/TIMED_OUT] + `SubagentResult` + `SubagentExecutor` + `_create_agent`/`_load_skills`/`_apply_skill_allowed_tools`/`_build_initial_state`/`_aexecute` + `execute`/`execute_async`）、`subagents/status_contract.py`（`SUBAGENT_STATUS_KEY`/`SUBAGENT_ERROR_KEY`/`SUBAGENT_STATUS_VALUES`(5 值) + `extract_subagent_status`(按结果文本前缀映射) + `make_subagent_additional_kwargs`）、`subagents/token_collector.py`（`SubagentTokenCollector(BaseCallbackHandler)` 收 LLM usage 回灌父 RunJournal）
  - **新建**：`subagents/builtins/__init__.py`（`BUILTIN_SUBAGENTS` dict）、`subagents/builtins/general_purpose.py`（`GENERAL_PURPOSE_CONFIG`：继承除 task/ask_clarification/present_files 外全部工具、max_turns=150、多步探索+动作）、`subagents/builtins/bash_agent.py`（`BASH_AGENT_CONFIG`：仅 sandbox 5 工具 bash/ls/read_file/write_file/str_replace、disallowed task/clarification/present、max_turns=60、host-bash 未允许则隐藏）
  - **新建**：`contracts/subagent_status_contract.json`（5 状态后端↔前端契约）
- **关键要点（⚠️ 修正 v1.1「双线程池」错误）**：deer 实际是 **单 `_scheduler_pool = ThreadPoolExecutor(max_workers=3)` + 持久化隔离事件循环**（`_isolated_subagent_loop`：daemon 线程上 `asyncio.new_event_loop()` 常驻，复用共享 async client[httpx 等]，atexit 注册清理）——**不是** `_scheduler_pool + _execution_pool` 双池。`MAX_CONCURRENT_SUBAGENTS=3` 由 `SubagentLimitMiddleware`(M16 第 19 步) 在模型响应后截断 task 调用保证；子代理图 `checkpointer=False`（一次性）；默认超时 built-in 1800s（custom 用自身值）；协作取消在 astream 迭代边界检查（工具调用中不可中断）；轮询 5s + 安全网 `(timeout+60)//5` 次后 `polling_timed_out`；SSE 事件 `task_started/running/completed/failed/cancelled/timed_out`。
- **自定义子代理（v1.2 恢复）**：`config/subagents_config.py` 的 `SubagentsAppConfig` 支持 `custom_agents` dict（用户定义子代理：description/system_prompt/tools[或 null 全部]/disallowed_tools/skills[null 全部 / [] 无 / 具体白名单]/model[inherit 或显式]/max_turns/timeout_seconds）+ `agents` dict（per-agent overrides：timeout_seconds/max_turns/model/skills）+ helper `get_timeout_for`/`get_model_for`/`get_max_turns_for`/`get_skills_for`。`task` 工具的 `subagent_type` 可选 built-in 或任意自定义名。
- **依赖**：agents（创建子代理图，Phase 7 —— executor 在 Phase 8 worker 中被 task_tool 调用；Phase 2 建 registry/status_contract/executor/builtins/token_collector 骨架，真实 agent 构造依赖 Phase 7）、sandbox（bash 子代理）、skills（_load_skills）、config/subagents_config。
- **可靠性**：超时 1800s；隔离事件循环保 async client；状态契约统一（5 值）；token 按 tool_call_id 缓存由 TokenUsageMiddleware 合并防双计；协作取消 + 延迟清理。
- **测试**：`backend/test/test_subagents.py` —— 注册（built-in+custom+per-agent override 合并）、status_contract(13 fixture 全映射 + make_additional_kwargs)、executor 成功/超时/polling 超时/失败/取消（mock agent）、隔离事件循环复用、token_collector 收集、限流截断（与 SubagentLimitMiddleware 联动）、bash 子代理 host-bash 未允许时隐藏。
- **学习文档**：`docs/subagents.md` —— 单 scheduler pool + 持久化隔离事件循环设计（为何不双池、为何复用 async client）、为何 checkpointer=False、并发上限由中间件+执行器共同保证、自定义子代理与 per-agent 覆盖、token 回灌。
- **（v1.2 取消裁剪）**：bash 子代理依赖 sandbox（已纳入 M10），不再「可不做」。

---

### M12. tracing（链路追踪）— Phase 2

- **现状**：无。
- **目标**：LangSmith/Langfuse 在图根注入回调。
- **文件清单**：**新建** `tracing/__init__.py`、`tracing/factory.py`（`build_tracing_callbacks()`）、`tracing/metadata.py`（`build_langfuse_trace_metadata`、`inject_langfuse_metadata(config, ...)`）。
- **关键要点**：仅在设了 `LANGSMITH_TRACING`/`LANGFUSE_TRACING` 环境变量时返回回调；metadata 字段映射（session_id=thread_id、user_id、trace_name=assistant_id、tags）；**in-graph 的 `create_chat_model` 一律 `attach_tracing=False`**；models factory 的 `attach_tracing=True` 路径懒导入本模块。
- **依赖**：runtime/user_context。
- **可靠性**：未配置时返回空列表（零开销）。
- **测试**：`backend/test/test_tracing.py` —— 环境变量开关、metadata 映射、未配置返回空、models attach_tracing 联动。
- **学习文档**：`docs/tracing.md` —— 为什么回调必须在图根（propagate_attributes）、attach_tracing=False 防重复 span、与 models 的懒导入关系。

---

### M13. agents/memory（记忆模块）— Phase 3

- **现状**：[memory_middleware.py](../backend/packages/harness/deerflow/agents/middlewares/memory_middleware.py) 是不可用桩（`self._update_queue=[]`）；无 `memory/` 子包。
- **目标**：真记忆——LLM 抽取 + 去抖队列 + 按用户原子存储 + 注入。
- **文件清单**：
  - **新建**：`agents/memory/__init__.py`、`storage.py`（`MemoryStorage` ABC + `FileMemoryStorage` + `get_memory_storage` + `create_empty_memory` + `utc_now_iso_z`）、`queue.py`（`ConversationContext` + `MemoryUpdateQueue` + `get_memory_queue`）、`updater.py`（`MemoryUpdater` + 公开函数）、`prompt.py`（`MEMORY_UPDATE_PROMPT` + `format_memory_for_injection` + tiktoken 冷却降级）、`message_processing.py`、`summarization_hook.py`（可选，依赖摘要中间件）
  - **修改**：`agents/middlewares/memory_middleware.py`（重写：filter→detect→捕获 user_id→`get_memory_queue().add`）、`agents/middlewares/dynamic_context_middleware.py`（重写：before_agent + ID-swap 冻结首条 HumanMessage + 记忆注入 + 跨午夜 + async to_thread 5s 超时）、`agents/lead_agent/prompt.py`（移植 `_get_memory_context`）
- **关键要点**：同步 LLM 路径（防 #2615）+ 专用线程池；原子写 temp+rename；user_id 跨 Timer 显式捕获；JSON 部分更新 fail-closed；上传记忆剔除；tiktoken 冷却降级 + char 模式。
- **依赖**：**models（M-models，`create_chat_model`）**、config/memory_config、runtime/user_context、config/paths、**config/agents_config（M22，`AGENT_NAME_PATTERN` + `validate_agent_name`，v1.2 起从 agents_config 直接取，不再局部兜底）**。
- **测试**：`backend/test/test_memory.py` —— storage load/save/reload/per-user 隔离/原子写/损坏 JSON 回退；queue 去抖合并/user_id 跨线程/add_nowait；updater JSON 容错/事实去重/max_facts 裁剪/上传剔除/correction；middleware enabled 跳过/无 thread_id 跳过/user_id 捕获；injection token 截断/char 模式。
- **学习文档**：`docs/memory.md` —— 数据结构、去抖队列、按用户隔离、同步 LLM 防连接池污染、注入预算。

---

### M14. skills（技能模块）— Phase 4

- **现状**：**完全缺失**（README 标记 TODO）。
- **目标**：SKILL.md 发现/解析/激活/安装 + allowed-tools 工具策略 + 提示词注入。
- **文件清单**：
  - **新建**：`skills/__init__.py`、`types.py`、`parser.py`、`validation.py`、`slash.py`、`tool_policy.py`、`permissions.py`（chmod helper，local 模式可 no-op）、`security_scanner.py`（stub，默认 allow，可插拔 LLM）、`installer.py`（`.skill` ZIP 安装 + 安全防护 + 异常类）、`storage/__init__.py`（`get_or_new_skill_storage`）、`storage/skill_storage.py`（`SkillStorage` ABC + `load_skills` 模板）、`storage/local_skill_storage.py`（`LocalSkillStorage`）
  - **新建**：`agents/middlewares/skill_activation_middleware.py`（`SkillActivationMiddleware`）
  - **修改**：`config/extensions_config.py`（补 `is_skill_enabled`）、`agents/lead_agent/prompt.py`（移植 `get_skills_prompt_section` + **后台刷新缓存**：`_enabled_skills_cache` 进程级单例 + `_enabled_skills_refresh_event` 协调 + `_enabled_skills_by_config_cache` 按 AppConfig 隔离 + `refresh_skills_system_prompt_cache_async` 失效 + `get_cached_enabled_skills` 非阻塞读 miss 触发后台刷新）、`agents/lead_agent/agent.py`（`filter_tools_by_skill_allowed_tools` + `_load_enabled_skills_for_tool_policy` + `_available_skill_names`[bootstrap 仅 setup_agent / custom-agent 用白名单]）
  - **新建目录**：`mini-deer-flow/skills/public/<示例>/SKILL.md`
- **关键要点**：slash 严格语法 + 保留字过滤（`RESERVED_SLASH_SKILL_NAMES = {bootstrap,help,memory,models,new,status}`）；激活读盘必 `relative_to(skills_root)` 防穿越 + html.escape 防注入；安装 zip 炸弹防御（512MB 硬上限）+ symlink 跳过 + macOS `__MACOSX`/dotfile 过滤 + 预占目标原子搬入；enabled 状态每次重读 extensions_config；`load_skills` 文件 IO `to_thread`；**LLM 安全扫描**（`security_scanner`：allow/warn/block，可执行文件须 allow，不可用回退 block）；**权限收紧**（`permissions.make_skill_*_sandbox_readable`：目录 0o555/文件 0o444，跳 symlink）；**自演化**（`skill_evolution.enabled` 时挂 `skill_manage` 工具[见 M15]，agent 可 create/patch/edit/delete/write_file/remove_file 自定义技能，全程记 `.history/<name>.jsonl`）。
- **依赖**：config/skills_config + extensions_config、reflection.resolve_class、utils/messages、config/paths（`resolve_path` 替代 runtime_paths）。
- **测试**：`backend/test/test_skills.py` —— parser/validation/slash/tool_policy/storage load+穿越拒绝+async 卸载/activation 注入+幂等+未启用失败+读盘穿越拒绝/installer zip 炸弹+symlink+已存在。
- **学习文档**：`docs/skills.md` —— SKILL.md 协议、发现/激活/安装三流、allowed-tools 收紧、slash 与提示词注入两条路径。

---

### M15. tools（工具模块，9 内置工具）— Phase 5

- **现状**：仅 `present_file_tool` + `ask_clarification_tool`；`get_available_tools` 精简。
- **目标**：对齐 deer 的工具组装——**9 内置工具全收** + MCP 标记 + sync 包装 + 去重 + 条件加载（v1.2 全面对标，不再裁剪）。
- **文件清单**：
  - **新建**：`tools/builtins/view_image_tool.py`（`view_image`，仅 supports_vision；路径白名单 `/mnt/user-data/{workspace,uploads,outputs}`；20MB 上限；jpg/jpeg/png/webp 魔数校验）、`tools/builtins/task_tool.py`（`task`，仅 subagent_enabled；后台执行 + 5s 轮询 + SSE 事件 + token 按 tool_call_id 缓存 + 延迟清理）、`tools/builtins/setup_agent_tool.py`（`setup_agent`，**仅 is_bootstrap=True** 绑定；写 SOUL.md+config.yaml 到 per-user 目录；依赖 **M22 agents_config**）、`tools/builtins/update_agent_tool.py`（`update_agent`，**仅 agent_name 且非 bootstrap** 绑定；部分更新 + 原子 temp+rename + model 校验 + per-user 隔离；依赖 **M22**）、`tools/builtins/tool_search.py`（`tool_search`[build_tool_search_tool] + `DeferredToolCatalog`/`DeferredToolSetup` + `assemble_deferred_tools`[fail-closed] + `get_deferred_tools_prompt_section`；仅 tool_search.enabled；查询 select:/关键词/+token；依赖 **M20 mcp**）、`tools/builtins/invoke_acp_agent_tool.py`（`invoke_acp_agent`[build_invoke_acp_agent_tool]；per-thread `/mnt/acp-workspace`(ro) + 传 MCP servers 给 ACP + 权限自动批准；**soft-load `acp` 包**，缺包不挂工具）、`tools/skill_manage_tool.py`（`skill_manage`：create/patch/edit/delete/write_file/remove_file + per-skill async 锁 + 安全扫描 + 历史记录；仅 skill_evolution.enabled）、`tools/mcp_metadata.py`（`tag_mcp_tool`/`is_mcp_tool`/`MCP_TOOL_METADATA_KEY="deerflow_mcp"`）、`tools/sync.py`（`make_sync_tool_wrapper` + `_get_runnable_config_param`）
  - **修改**：`tools/builtins/__init__.py`（导出 9 工具）、`tools/tools.py`（`get_available_tools(groups, include_mcp, model_name, subagent_enabled, app_config)` 重写：config 工具经 `resolve_variable` 加载 + host-bash 过滤[`_is_host_bash_tool`/`is_host_bash_allowed`] + name-mismatch 告警 + sync 包装[`_ensure_sync_invocable_tool`] + 条件加 builtins[always present_file/ask_clarification；skill_evolution→skill_manage；subagent→task；supports_vision→view_image；bootstrap→setup_agent；agent_name 且非 bootstrap→update_agent] + MCP 加载[`get_cached_mcp_tools`+`tag_mcp_tool`，try/except 软加载] + ACP[配置了才加] + 按 name 去重[config>builtins>MCP>ACP，防 #1803]）、`tools/types.py`（`Runtime = ToolRuntime[dict[str, Any], ThreadState]`）
- **关键要点**：按 name 去重（config 优先，防 #1803）；view_image 仅 supports_vision；task 仅 subagent_enabled；setup_agent 仅 bootstrap；update_agent 仅 custom-agent；tool_search/skill_manage 按 config 开关；MCP/ACP 缺包软加载 + 可操作安装提示。
- **依赖**：sandbox（bash 等工具，**M10**）、subagents（task_tool，**M11**）、models（supports_vision，M-models）、skills（skill_manage，**M14**）、**M20 mcp**（tool_search/MCP 加载）、**M22 agents_config**（setup/update_agent）、config（tool_search/skill_evolution）、reflection、community（M21，经 `tools[].use:` 加载 web 工具）。
- **测试**：`backend/test/test_tools.py`（扩展现有）—— 去重（config>builtins>MCP>ACP）、host-bash 过滤、view_image 条件、name-mismatch 告警、sync 包装、MCP 缺包回退、setup/update_agent 绑定条件、tool_search 延迟装配、skill_manage 绑定。
- **学习文档**：`docs/tools.md`（旧版在 `docs/legacy/tools.md`，M15 落地时按新模板重写到 `docs/tools.md`）—— 工具来源（config/builtin/MCP/ACP/community 五类）、9 内置工具逐一、去重必要性、条件加载、MCP/ACP 软加载。
- **（v1.2 取消裁剪）**：`invoke_acp_agent_tool` 改为 **soft-load**（依赖 `agent-client-protocol` 包，缺包不挂工具但保留实现），不再「不做」。

---

### M16. agents/middlewares（中间件）— Phase 6

- **现状**：8 个（DynamicContext/Memory 是教学桩），缺 16 个。
- **目标**：23 步生产中间件链，全部 config 驱动。
- **文件清单**：
  - **新建（核心档）**：`thread_data_middleware.py`、`dangling_tool_call_middleware.py`、`tool_output_budget_middleware.py`、`todo_middleware.py`、`tool_call_metadata.py`
  - **新建（业务档，v1.2 全部启用）**：`uploads_middleware.py`（**依赖 M23 uploads，v1.2 已纳入主线——不再跳过**）、`sandbox_audit_middleware.py`（依赖 sandbox）、`summarization_middleware.py`、`token_usage_middleware.py`、`view_image_middleware.py`、`deferred_tool_filter_middleware.py`（依赖 tool_search/M20，**v1.2 已纳入——不再可选**）、`subagent_limit_middleware.py`（依赖 subagents，`MAX_CONCURRENT_SUBAGENTS=3` 截断）、`safety_finish_reason_middleware.py` + `safety_termination_detectors.py`、`skill_activation_middleware.py`（已在 M14）
  - **重做现有**：`memory_middleware.py`（M13）、`dynamic_context_middleware.py`（M13）、`tool_error_handling_middleware.py`（补 task-status stamping[`_stamp_task_subagent_status`]）、`clarification_middleware.py`（补 options/context）、`title_middleware.py`（config 驱动 + 结构化内容归一）、`llm_error_handling_middleware.py`（对齐）、`loop_detection_middleware.py`（`from_config`）
  - **修改**：`middlewares/__init__.py`（`build_middlewares` 重写为 23 步，见 Part D）
- **关键要点**：Clarification 必须最后；ThreadData→Sandbox 顺序；所有 `wrap_tool_call` 必须 `raise GraphBubbleUp`；DynamicContext 用 `before_agent` + ID-swap；LoopDetection `from_config`；SubagentLimit after-model 截断 task 调用。
- **依赖**：几乎所有业务模块（sandbox/subagents/skills/memory/tracing/config）。
- **测试**：`backend/test/test_middlewares.py`（**已存在，需扩展**）—— 顺序断言（Clarification 末位、ThreadData 先于 Sandbox）、GraphBubbleUp 不被吞、各中间件 enable/默认、ID-swap 注入、todo plan_mode。
- **学习文档**：`docs/middlewares.md`（旧版在 `docs/legacy/中间件.md`，M16 落地时按新模板重写到 `docs/middlewares.md`）—— 中间件 hook 机制、23 步顺序与设计理由、AgentMiddleware 生命周期。
- **裁剪**：guardrail 中间件（依赖 guardrails 模块）**本期不做**。

---

### M17. agents（Agent 装配）— Phase 7

- **现状**：factory 纯参数无 features；thread_state 缺类型化 reducer + promoted；lead_agent 精简；prompt 15 行模板。
- **目标**：SDK + config 双入口，行为对齐 deer。
- **文件清单**：
  - **新建**：`agents/features.py`（`RuntimeFeatures` + `@Next`/`@Prev`）
  - **修改**：`agents/thread_state.py`（类型化 `SandboxState`/`ThreadDataState`/`ViewedImageData`/`PromotedTools` + fail-closed `merge_sandbox` + `merge_promoted` + `promoted` 字段）、`agents/factory.py`（`create_deerflow_agent` 加 features/extra_middleware/plan_mode/checkpointer/name + `_assemble_from_features` + `_insert_extra`）、`agents/lead_agent/agent.py`（`_get_runtime_config` + `_resolve_model_name` + tracing 图根注入 + 工具策略过滤 + deferred 装配 + **bootstrap/custom-agent 分支[依赖 M22 agents_config，v1.2 已纳入——不再可选]** + `_available_skill_names`[bootstrap 仅 setup_agent / custom-agent 白名单]）、`agents/lead_agent/prompt.py`（移植 deer `SYSTEM_PROMPT_TEMPLATE`，见下方条件段）、`agents/__init__.py`
- **关键要点**：tracing 回调图根注入 + 所有 in-graph `create_chat_model` 传 `attach_tracing=False`；`@Next`/`@Prev` 锚定插入 + Clarification 末位不变量；prompt 保持静态（记忆/日期交由 DynamicContext）。
- **依赖**：**models（M-models）**、Phase 2–6 全部 + tools + runtime（checkpointer）+ **M22 agents_config（custom-agent 分支 + setup/update_agent 工具绑定）**。
- **prompt 条件段（S11 明确）**：`SYSTEM_PROMPT_TEMPLATE` 的占位按 feature 填充——`{skills_section}`(M14，enabled skills 非空)、`{deferred_tools_section}`(tool_search.enabled/M20)、`{subagent_section}`/`{subagent_reminder}`/`{subagent_thinking}`(subagent_enabled)、`{soul}`+`{self_update_section}`(agent_name，**依赖 M22 agents_config，v1.2 已纳入**)、`{acp_section}`(ACP 工具配置了才填)。未启用段返回 `""`。
- **测试**：`backend/test/test_agent.py`（**已存在，扩展**）+ `test_agent_with_middlewares.py`（**已存在**）—— factory features 装配、`_insert_extra` 冲突检测、Clarification 末位不变量、make_lead_agent config 驱动、model_name 回退、tracing 注入、prompt 条件段拼接。
- **学习文档**：`docs/agents.md` —— factory 双模式、RuntimeFeatures、thread_state reducer 为何 fail-closed、prompt 静态化与 prefix-cache、条件段 gating。

---

### M18. runtime/runs（运行管理）— Phase 8

- **现状**：无（runs 基类层已在 M4/Phase 1 落地：`runs/schemas.py` + `runs/store/base.py`）。
- **目标**：完整 run 生命周期（创建/取消/rollback/恢复/drain）。
- **文件清单**：
  - **新建**：`runs/naming.py`（`resolve_root_run_name`）、`runs/store/memory.py`（`MemoryRunStore`）、`runs/manager.py`（`RunManager` + `RunRecord` + `ConflictError`/`UnsupportedStrategyError`）、`runs/worker.py`（`RunContext` + `run_agent`）
  - **修改**：`runtime/runs/__init__.py`（聚合导出）、`runtime/__init__.py`（聚合导出）
- **关键要点**：RunManager asyncio 锁 + 线程索引 + SQLite busy 重试；`create_or_reject`(reject/interrupt/rollback)；幂等 cancel；`reconcile_orphaned_inflight_runs` 启动恢复；`shutdown(timeout=5)` drain（#3373）；worker 注入 `__pregel_runtime`/`__run_journal`、run 前 checkpoint 快照供 rollback、abort、LLM 兜底、标题回写 thread_meta、bridge cleanup。
- **依赖**：agent factory（Phase 7）、runtime/checkpointer/events/journal/stream_bridge/serialization/user_context、persistence、tracing、**runs/schemas + runs/store/base（Phase 1 已建）**。
- **可靠性**：见 Part E 全部红线。
- **测试**：`backend/test/test_run_manager.py`（create_or_reject/cancel 幂等/orphan 恢复/shutdown drain/busy 重试/store-only hydrate）、`backend/test/test_worker.py`（多模式 astream/rollback/abort/LLM 兜底/journal flush/bridge cleanup/runtime ctx 注入）。
- **学习文档**：`docs/runs.md` —— RunRecord 字段、RunManager 并发模型、multitask 策略、rollback 快照、shutdown drain 为何必须在关 checkpointer 前、基类提前的设计。

---

### M19. runtime/store（LangGraph Store）— Phase 8（可选）

- **现状**：无。
- **目标**：与 checkpointer 平行的 BaseStore 工厂。
- **文件清单**：**新建** `runtime/store/__init__.py`、`store/provider.py`（`get_store` 单例 + `store_context` + `reset_store`）、`store/async_provider.py`（`make_store`）。`_sqlite_utils.py` 已在 M5 建。
- **关键要点**：memory/sqlite/postgres；worker 的 `RunContext.store` 与 `agent.store = store` 已预留。
- **依赖**：config/database_config。
- **裁剪**：先只做 memory 实现；sqlite/postgres 后补。
- **测试**：`backend/test/test_store.py` —— memory get/set、缺包回退。
- **学习文档**：`docs/runtime_store.md` —— LangGraph Store 与 checkpointer 的区别（跨线程记忆 vs 状态快照）。

---

### M20. mcp（Model Context Protocol 集成）— Phase 5（v1.2 纳入主线）

- **现状**：无（v1.1 标「留接口不做」——v1.2 **恢复**：M15 tool_search 与 M16 DeferredToolFilter 都依赖它）。
- **目标**：外部 MCP 服务器工具发现 + 调用，支持 stdio/sse/http 三种传输 + OAuth + 有状态会话池 + mtime 缓存失效。
- **文件清单**：
  - **新建**：`mcp/__init__.py`（导出 `get_cached_mcp_tools`/`reset_mcp_tools_cache`/`build_servers_config`/`MCPSessionPool`/`build_oauth_tool_interceptor`）、`mcp/cache.py`（`_mcp_tools_cache` 全局 + `_get_config_mtime`(extensions_config mtime) + `get_cached_mcp_tools`(检测 mtime 变化自动重新初始化) + `reset_mcp_tools_cache`(关闭持久会话)）、`mcp/client.py`（`build_servers_config(extensions_config)`：把 extensions_config.json 的 `mcpServers` 转 `langchain_mcp_adapters.MultiServerMCPClient` 配置 + `mcpInterceptors` 解析为 builder）、`mcp/tools.py`（`MultiServerMCPClient` 封装 + `tool_interceptors`[OAuth + 自定义] + `tool_name_prefix=True` + `await client.get_tools()`）、`mcp/oauth.py`（`McpOAuthConfig`：token_url/grant_type/client_id/secret/refresh_token/scope/audience/refresh_skew_seconds + `build_oauth_tool_interceptor`(注入 Authorization 头到工具调用与 server 连接)）、`mcp/session_pool.py`（`MCPSessionPool`：按 `(server_name, thread_id)` 维护有状态 stdio 会话[Playwright 等需跨调用保活]，LRU 上限 256，每会话由专属 `_run_session` task 持有 anyio cancel-scope，threading.Lock 保护注册表）
- **关键要点**：stdio/sse/http 三传输；OAuth 仅 sse/http；session_pool 仅 stdio（http/sse TaskGroup 清理问题排除）；mtime 失效让改 `extensions_config.json` 无需重启；`tag_mcp_tool` 标记供 tool_search 延迟装配。
- **依赖**：extensions_config（mcpServers + mcpInterceptors）、reflection（自定义 interceptor builder）、M15（tag_mcp_tool）。
- **可靠性**：`langchain-mcp-adapters` 缺包软加载 + 可操作安装提示；session 池 LRU 防泄漏；OAuth token 刷新 skew。
- **测试**：`backend/test/test_mcp.py`（hermetic，mock MCP client）—— build_servers_config 各传输、cache mtime 失效与复用、get_tools、OAuth 头注入、session_pool 复用同 (server,thread)/不同 thread 隔离/LRU 淘汰、缺包回退。
- **学习文档**：`docs/mcp.md` —— MCP 协议简介、stdio/sse/http 区别、为何要会话池（有状态工具）、mtime 失效、OAuth、与 tool_search 延迟装配的关系。
- **软加载**：`langchain-mcp-adapters` / `mcp` 缺包时 MCP 工具不可用但不影响其它工具（可操作安装提示）。

---

### M21. community（联网搜索 / 抓取 provider 框架）— Phase 5（v1.2 纳入主线）

- **现状**：无（v1.1 标「留接口不做」——v1.2 **恢复**：agent 联网能力的核心来源）。
- **目标**：web 搜索/抓取 provider 框架 + 核心 provider 实现；其余 provider 软加载，按需启用。对齐 deer `community/`（12 个 provider）。
- **文件清单**：
  - **新建**：`community/__init__.py`、`community/_common.py`（共享：结果归一化[title/url/snippet]、4KB 内容截断、async httpx 封装、超时/代理/headers 通用参数）
  - **核心 provider（本期实现）**：`community/ddg_search/`（`web_search_tool`：DuckDuckGo，**无需 API key**；backend auto/duckduckgo/wikipedia；CJK 自动推断 Wikipedia region；region/safesearch/time_range 参数）、`community/tavily/`（`web_search_tool`+`web_fetch_tool`：Tavily Client；`api_key`/`max_results`；归一化结果 + 4KB 抓取）、`community/jina_ai/`（`web_fetch_tool`：Jina Reader API；async HTML 抓取 + 可读性提取 + markdown 输出；`JINA_API_KEY` 可选）
  - **软加载 provider（留 `tools[].use:` 路径 + try/except，按需实现，不阻塞主线）**：`community/firecrawl/`（web_search+web_fetch，api_key）、`community/brave/`（web_search，`BRAVE_SEARCH_API_KEY`，REST API max 20）、`community/exa/`（web_search+web_fetch，api_key）、`community/serper/`（web_search，Google 经 Serper，`SERPER_API_KEY`）、`community/searxng/`（web_search，自托管 base_url）、`community/browserless/`（web_fetch，headless Chrome 渲染 + 资源拦截）、`community/image_search/`（image_search，DuckDuckGo 图搜，size/color/type/layout/license 过滤）、`community/infoquest/`（web_search+web_fetch+image_search，`INFOQUEST_API_KEY`，同步 requests）
- **关键要点**：所有 provider 经 M15 的 `tools[].use: "deerflow.community.<provider>.tools:<tool>"` 路径加载（reflection.resolve_variable）；结果统一 `{title,url,snippet}` JSON；抓取统一 4KB 截断；CJK 感知（ddg Wikipedia region 自动推断）；provider 间无耦合，逐个独立软加载。
- **依赖**：reflection、config（tools[]）、可选第三方 SDK（`duckduckgo-search`/`tavily-python`/`httpx`/`firecrawl-py` 等，各自软加载）。
- **可靠性**：每个 SDK 独立 try/except ImportError + 可操作安装提示；4KB 截断防爆；超时统一。
- **测试**：`backend/test/test_community.py`（hermetic，mock httpx/SDK）—— ddg_search backend 选择 + CJK 推断 + 归一化、tavily search/fetch 截断、jina fetch markdown、缺包 provider 软加载跳过、4KB 截断、超时。
- **学习文档**：`docs/community.md` —— 为什么需要联网、provider 框架设计、`tools[].use:` 加载机制、各 provider 取舍（免费 ddg / 强 tavily / 抓取 jina）、CJK 处理。
- **裁剪**：12 provider 中本期**实现 3 个核心**（ddg/tavily/jina），其余 9 个留 `tools[].use:` 路径 + try/except 占位，需求出现时逐个补（**不删接口**）。

---

### M22. config/agents_config（自定义 agent SOUL.md/config）— Phase 2（v1.2 纳入主线）

- **现状**：无（v1.1 标「不做」并让 memory 局部兜底 AGENT_NAME_PATTERN——v1.2 **恢复**：被 M13 memory 与 M15 setup/update_agent 工具依赖，必须先做）。
- **目标**：自定义 agent 配置（SOUL.md 人格 + config.yaml）+ per-user 存储 + 名称校验 + legacy 回退。
- **文件清单**：
  - **新建**：`config/agents_config.py`（`SOUL_FILENAME="SOUL.md"` + `AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")` + `AgentConfig`(name/description/model/tool_groups/skills[None 全部 / [] 无 / 白名单]) + `validate_agent_name`(enforce pattern) + `resolve_agent_dir`(per-user 优先 + legacy 只读回退) + `load_agent_config`(读 config.yaml，剥未知字段如 legacy prompt_file) + `load_agent_soul`(读 SOUL.md，默认 agent 读 base_dir) + `list_custom_agents`(扫 per-user + legacy 并集，per-user 覆盖 legacy)）
  - **存储约定**：per-user `{base_dir}/users/{user_id}/agents/{name}/`（SOUL.md + config.yaml）；legacy `{base_dir}/agents/{name}/`（只读回退，pre-user-isolation 安装）。
- **关键要点**：`AGENT_NAME_PATTERN` 严格校验（字母数字+连字符）；per-user 隔离（一用户的 agent 不影响另一用户）；legacy 只读回退兼容旧安装；`validate_agent_name` 被 client/setup_agent/update_agent/memory storage 共用。
- **依赖**：config/paths、utils/time。
- **可靠性**：缺 SOUL.md/config.yaml 返回 None 而非报错；unknown 字段剥离（向前兼容）。
- **测试**：`backend/test/test_agents_config.py` —— name 校验（合法/非法）、resolve_agent_dir per-user 优先 + legacy 回退、load_agent_config 剥未知字段、load_agent_soul 默认/自定义、list_custom_agents 并集 + 覆盖、AGENT_NAME_PATTERN 边界。
- **学习文档**：`docs/agents_config.md` —— 自定义 agent 是什么（SOUL.md 人格 + config.yaml 工具/技能白名单）、per-user 隔离、为何要 AGENT_NAME_PATTERN、setup/update_agent 工具如何用它、memory 如何按 agent 隔离。
- **被依赖**：M13 memory（AGENT_NAME_PATTERN + per-agent 存储）、M15 setup_agent/update_agent 工具、M17 lead_agent custom-agent 分支、DeerFlowClient（可选）。

---

### M23. uploads（文件上传 + markitdown 转换）— Phase 5.5（v1.2 纳入主线）

- **现状**：无（v1.1 标「不做」并让 M16 UploadsMiddleware 同步不做——v1.2 **恢复**：中间件第 3 步依赖它）。
- **目标**：文件上传 + markitdown 文档转换（PDF/PPT/Excel/Word→markdown）+ 安全加固（路径穿越 + symlink + 唯一文件名）。
- **文件清单**：
  - **新建**：`uploads/__init__.py`（导出 `get_uploads_dir`/`ensure_uploads_dir`/`normalize_filename`/`PathTraversalError`/`claim_unique_filename`/`validate_path_traversal`/`list_files_in_dir`/`delete_file_safe`/`upload_artifact_url`/`upload_virtual_path`/`enrich_file_listing`/`validate_thread_id`/`open_upload_file_no_symlink`/`write_upload_file_no_symlink`）、`uploads/manager.py`（上传管理逻辑：`open_upload_file_no_symlink`(POSIX `O_NOFOLLOW` / Windows lstat+fstat 双校验防 symlink 跟随) + `write_upload_file_no_symlink`(流式写) + markitdown 转换 worker（事件循环内复用）+ 伴随 `.md` 删除时清理）
  - **关键函数**：`normalize_filename`（仅 basename + 拒 `..`/`\` + 255 UTF-8 字节上限）、`claim_unique_filename`（`file.txt`→`file_1.txt` 自动重命名）、`validate_path_traversal`（`resolve().relative_to()` 校验，越界抛 `PathTraversalError`）、`list_files_in_dir`（仅文件，返回 filename/size/path/extension/modified）、`delete_file_safe`（穿越校验 + 可选伴随 `.md` 清理）、`enrich_file_listing`（补 virtual_path/artifact_url）
- **关键要点**：虚拟路径 `/mnt/user-data/uploads/`（agent 可见）↔ 物理 `{base_dir}/users/{user_id}/threads/{thread_id}/user-data/uploads/`（per-user per-thread 隔离）；**symlink 拒绝**（`O_NOFOLLOW`）防沙箱逃逸；markitdown 把二进制文档转 markdown 供 agent 阅读。
- **依赖**：config/paths、markitdown（软加载）。
- **可靠性**：markitdown 缺包时跳过转换（保留原文件，可操作安装提示）；所有路径走穿越校验；symlink 防御。
- **测试**：`backend/test/test_uploads.py`（hermetic，tmp 目录）—— normalize_filename 边界、claim_unique_filename 重命名、validate_path_traversal 拒绝越界、open_upload_file_no_symlink 拒 symlink（POSIX）、list_files_in_dir、delete_file_safe + 伴随清理、markitdown 转换 + 缺包回退、per-user per-thread 隔离。
- **学习文档**：`docs/uploads.md` —— 上传流程、虚拟路径与物理路径、symlink 为何危险、markitdown 转换、与 UploadsMiddleware(M16) 的关系。
- **软加载**：`markitdown` 缺包时转换跳过但上传仍可用（可操作安装提示）。

---

### 真正可选（不阻塞主线，按需）

> 以下两项**确实可选**，依赖独立外部模块，本期不做（与「全面对标」6 大模块无关）：

| 模块 | 路径 | 作用 | 本期处理 |
|------|------|------|----------|
| **Guardrail 中间件** | `agents/middlewares/guardrail_middleware.py` | 内容审查（依赖 `guardrails` / `neMo-guardrails` 独立模块） | 不做；M16 第 7 步跳过，留 `try/except` 接口 |
| **DeerFlowClient** | `client.py` | 嵌入式 in-process 客户端 | 不做（mini 走 langgraph dev + 未来 Gateway） |

> v1.2 起 mcp / community / uploads / agents_config / AIO 沙箱**已全部进入主线**（M10b / M20–M23），不再在此列「不做」。

---

## Part D — 集成装配清单

### D.1 build_middlewares 23 步顺序（middlewares/__init__.py 重写后）

```
 1. ToolOutputBudgetMiddleware          ← 防爆最先
 2. ThreadDataMiddleware                ← 建隔离目录(lazy_init)
 3. UploadsMiddleware                   ← v1.2 启用（依赖 M23 uploads，已纳入主线）
 4. SandboxMiddleware                   ← 必须在 ThreadData 之后
 5. DanglingToolCallMiddleware          ← 补缺响应, 在模型前
 6. LLMErrorHandlingMiddleware
 7. GuardrailMiddleware                 [依赖 guardrails 模块, 真正可选 → 跳过]
 8. SandboxAuditMiddleware              ← 依赖 sandbox
 9. ToolErrorHandlingMiddleware         ← 异常转ToolMessage(保留GraphBubbleUp!)
10. DynamicContextMiddleware            ← before_agent ID-swap 注入日期+记忆
11. SkillActivationMiddleware           ← /skill 激活
12. SummarizationMiddleware             ← 可选(config 驱动)
13. TodoMiddleware                      [plan_mode]
14. TokenUsageMiddleware                [可选]
15. TitleMiddleware                     ← config 驱动
16. MemoryMiddleware                    ← 真实队列(需 memory 子包)
17. ViewImageMiddleware                 [supports_vision]
18. DeferredToolFilterMiddleware        ← v1.2 启用（依赖 tool_search/M20，已纳入主线）
19. SubagentLimitMiddleware             ← subagent (MAX_CONCURRENT=3 截断)
20. LoopDetectionMiddleware             ← from_config
21. custom_middlewares
22. SafetyFinishReasonMiddleware        [可选]
23. ClarificationMiddleware             ⚠️ 永远最后
```

### D.2 lifespan 装配（或 CLI 入口）

```python
async with make_checkpointer(cfg) as cp, make_stream_bridge(cfg) as bridge:
    await init_engine_from_config(cfg.database)           # memory→no-op
    sf = get_session_factory()
    run_store    = RunRepository(sf) if sf else MemoryRunStore()
    event_store  = build_event_store(cfg.run_events, sf)  # memory/jsonl/db
    thread_store = build_thread_store(cfg.database, sf)
    run_manager  = RunManager(store=run_store)
    await run_manager.reconcile_orphaned_inflight_runs(error="Restarted")
    app.state.update(checkpointer=cp, stream_bridge=bridge, run_manager=run_manager,
                     event_store=event_store, thread_store=thread_store)
    yield
    await run_manager.shutdown(timeout=5)   # #3373 先 drain
    await close_engine()
```

### D.3 langgraph.json（对齐现有文件）

mini 已有 [backend/langgraph.json](../backend/langgraph.json)（含 `graphs.lead_agent`，**现无 checkpointer 段**）。**对齐操作**：补 `checkpointer` 段：

```json
{
  "$schema": "https://langgra.ph/schema.json",
  "python_version": "3.12",
  "dependencies": ["."],
  "env": ".env",
  "graphs": { "lead_agent": "deerflow.agents:make_lead_agent" },
  "checkpointer": {
    "path": "./packages/harness/deerflow/runtime/checkpointer/async_provider.py:make_checkpointer"
  }
}
```

### D.4 config.example.yaml 增补

补 `database`(backend: memory)、`run_events`、`stream_bridge`、`skills`(path/container_path)、`skill_evolution`(enabled: false)、`subagents`(timeout_seconds/custom_agents/agents)、`tool_output`、`token_usage`、`safety_finish_reason` 等段，全部带安全默认值；改 schema 时同步 bump `config_version`。**v1.2 新增**：`extensions_config.json` 的 `mcpServers`（stdio/sse/http 示例 + `$VAR` 占位）、`mcpInterceptors`、`skills` 启用 map 示例；`tools[]` 增 community provider 用法示例（`use: "deerflow.community.ddg_search.tools:web_search_tool"`）。

### D.5 build_event_store / build_thread_store 工厂（runtime 内）

- `build_event_store(cfg.run_events, sf)`：`memory`→MemoryRunEventStore；`jsonl`→JsonlRunEventStore(base_dir)；`db`→DbRunEventStore(sf, max_trace_content)。
- `build_thread_store(cfg.database, sf)`：memory→内存实现；sqlite/postgres→ThreadMetaRepository(sf)。

---

## Part E — 可靠性总红线（每个模块移植时不可丢）

1. **阻塞 IO 必须 `asyncio.to_thread`**：jsonl/memory JSON/tiktoken/skill 加载/sqlite 路径/uploads 扫描。
2. **SQLite WAL + busy 重试**：engine PRAGMA WAL；`RunManager._call_store_with_retry` 对 busy/locked 指数退避。
3. **seq 单调**：jsonl 进程内锁；db `FOR UPDATE`/`pg_advisory_xact_lock`。
4. **路径穿越防御**：jsonl `_SAFE_ID_PATTERN`、skill `validate_relative_path` `resolve().relative_to()`、skill 激活读盘 `relative_to(skills_root)`。
5. **rollback 快照**：run 前捕获 checkpoint 全量（含 pending_writes 深拷贝）。
6. **shutdown drain（#3373）**：关 checkpointer 池前 bounded `asyncio.wait` 在途 run。
7. **orphan 恢复**：启动标 error 无本地 worker 的 pending/running 行。
8. **RunJournal sync→async flush 去重**：`_pending_flush_tasks` 防并发写；失败 batch 回插。
9. **store-only hydrate**：从 RunStore 还原的 record 无 task/abort_event，cancel 返回 409。
10. **UUID→str 边界**：`user_id` 入 VARCHAR 列前 `str()`。
11. **bridge 有界回放**：`queue_maxsize=256` + `start_offset`。
12. **rowcount 驱动 recovery**：`update_status/update_run_completion` 返回 False → 内存 snapshot 重建行。
13. **`_persist_new_run_to_store` 失败回滚内存记录**。
14. **ClarificationMiddleware 永远最后**；**ThreadData→Sandbox** 顺序。
15. **所有 `wrap_tool_call` 必须 `raise GraphBubbleUp`**。
16. **`merge_sandbox` 冲突即报错**（fail-closed）。
17. **in-graph `create_chat_model` 一律 `attach_tracing=False`**；独立调用方 `attach_tracing=True`（模型级挂回调）。
18. **工具按 name 去重**（防 #1803）。
19. **memory 同步 LLM 路径 + 专用线程池**（防 #2615）。
20. **memory user_id 跨 Timer 显式捕获**（ContextVar 不跨线程）。
21. **memory JSON 部分更新 fail-closed**（factsToRemove + 损坏 newFacts 拒绝）。
22. **tiktoken 冷却降级 + char 模式**（GFW 网络受限）。
23. **skill 安装：zip 炸弹防御 + symlink 跳过 + 预占目标原子搬入**。
24. **可选依赖软加载 + 可操作安装提示**（sqlite/postgres/mcp/tiktoken/markitdown）；extras 在 pyproject 声明。
25. **空 config.yaml 必须以 memory 模式启动**（每模块默认值）。
26. **langgraph 版本下限**：须支持 `Runtime`/`ToolRuntime`/`__pregel_runtime`/`Command`/`get_config`（≥1.1）。
27. **模型 stream 超时放宽**：OpenAI 兼容默认 `stream_chunk_timeout=240s` + `stream_usage=True`（防推理模型首 chunk 超时 + token 用量缺失）。
28. **harness 边界 + blocking-io gate 有强制测试**（M-build），红线不止口头。
29. **MCP session_pool**（v1.2）：有状态 stdio 会话按 `(server_name, thread_id)` 隔离，每会话专属 task 持有 cancel-scope，LRU 256 防泄漏；http/sse 不入池。
30. **MCP/OAuth token 刷新 skew**（v1.2）：HTTP/SSE server 的 OAuth token 提前 `refresh_skew_seconds` 刷新，注入 Authorization 头到工具调用与连接。
31. **uploads symlink 拒绝**（v1.2）：`open_upload_file_no_symlink` POSIX 用 `O_NOFOLLOW`、Windows 用 lstat+fstat 双校验，防 symlink 跟随致沙箱逃逸；所有路径走 `validate_path_traversal`。
32. **agents_config 名称严格校验**（v1.2）：`AGENT_NAME_PATTERN = ^[A-Za-z0-9-]+$` 在 setup_agent/update_agent/memory storage/client 共用；per-user 优先 + legacy 只读回退。
33. **AIO 沙箱跨进程锁 + 优雅关闭**（v1.2）：`{thread_id}/{sandbox_id}.lock` 排他锁 + 暖池复用 + idle 超时回收 + SIGTERM/SIGINT/SIGHUP shutdown；release/destroy 关 HTTP client 防套接字泄漏。
34. **subagent 隔离事件循环**（v1.2）：daemon 线程持久 `asyncio.new_event_loop()` 复用共享 async client，atexit 注册清理——**不是**双线程池；协作取消在 astream 迭代边界。
35. **自定义子代理 per-agent override**（v1.2）：`get_subagent_config` 合并顺序 built-in→custom→per-agent override；并发上限由 SubagentLimitMiddleware(3) + 执行器共同保证。
36. **skill_manage 历史记录**（v1.2）：所有 create/patch/edit/delete/write_file/remove_file 记 `.history/<name>.jsonl`（action/author/thread_id/file_path/prev/new/scanner decision），per-skill async 锁防并发写。

---

## Part F — 交付物检查清单

每个模块必须完成三件交付，勾选后才能进入下一 Phase。**标注「已存在·更新」的旧文档须按新模板重写。**

| 模块 | 代码 | 测试文件 | 学习文档 |
|------|------|----------|----------|
| M-build 工程化 | ✅ | `test/test_harness_boundary.py` + `test/blocking_io/test_io_offload.py` | `docs/build.md` |
| M0 config | ✅ | `test/test_config.py` | `docs/config.md` |
| M1 utils | ✅ | `test/test_utils.py` | `docs/utils.md` |
| M2 reflection | ✅ | `test/test_reflection.py` | `docs/reflection.md` |
| M3 user_context | ✅ | `test/test_user_context.py` | `docs/user_context.md` |
| M-models 模型 | ✅ 已完成（测试待验证） | `test/test_model.py` | `docs/models.md` |
| M4 persistence | ✅ | `test/test_persistence.py` | `docs/persistence.md` |
| M5 checkpointer | ✅ | `test/test_checkpointer.py` | `docs/checkpointer.md` |
| M6 events/store | ✅ | `test/test_events.py` | `docs/run_event_store.md` |
| M7 journal | ✅ | `test/test_journal.py` | `docs/run_journal.md` |
| M8 stream_bridge | ✅ | `test/test_stream_bridge.py` | `docs/stream_bridge.md` |
| M9 serialization | ✅ | `test/test_serialization.py` | `docs/serialization.md` |
| M10 sandbox | ✅ | `test/test_sandbox.py` | `docs/sandbox.md` |
| M10b AIO 沙箱（v1.2） | ✅ | `test/test_aio_sandbox.py` | `docs/aio_sandbox.md` |
| M11 subagents | ✅ | `test/test_subagents.py` | `docs/subagents.md` |
| M12 tracing | ✅ | `test/test_tracing.py` | `docs/tracing.md` |
| M13 memory | ✅ | `test/test_memory.py` | `docs/memory.md` |
| M14 skills | ✅ | `test/test_skills.py` | `docs/skills.md` |
| M15 tools（9 内置） | ✅ | `test/test_tools.py` | `docs/tools.md`（旧版 `legacy/tools.md`，待重写） |
| M16 middlewares（23 步含 Uploads） | ✅ | `test/test_middlewares.py` | `docs/middlewares.md`（旧版 `legacy/中间件.md`，待重写） |
| M17 agents（含 custom-agent 分支） | ✅ | `test/test_agent.py` + `test_agent_with_middlewares.py` | `docs/agents.md` |
| M18 runs | ✅ | `test/test_run_manager.py` + `test_worker.py` | `docs/runs.md` |
| M19 runtime/store | ✅ | `test/test_store.py` | `docs/runtime_store.md` |
| M20 mcp（v1.2） | ✅ | `test/test_mcp.py` | `docs/mcp.md` |
| M21 community（v1.2，3 核心 + 9 软加载） | ✅ | `test/test_community.py` | `docs/community.md` |
| M22 agents_config（v1.2） | ✅ | `test/test_agents_config.py` | `docs/agents_config.md` |
| M23 uploads（v1.2） | ✅ | `test/test_uploads.py` | `docs/uploads.md` |
| 集成 | ✅ | `test/test_integration.py` | `docs/architecture.md` |
| Guardrail 中间件 / DeerFlowClient | （真正可选，留接口） | — | （需求时补） |

### 学习文档统一要求（面向小白）

每份 `docs/<module>.md` 至少包含：
1. **一句话定位**；2. **为什么需要它**（痛点/故障场景）；3. **核心概念**（名词+类比）；4. **设计原理**（权衡、不变量、踩坑——引用 Part E 红线编号）；5. **文件结构**；6. **关键接口/签名**；7. **应用方法**（配置/调用/可跑 demo）；8. **与其它模块的关系**（文字依赖图）；9. **常见问题/排错**。

---

## Part G — 修订日志

- **v1.2（本次，全面对标）**：
  - 🔴 **核心指令变更**：执行「**全面对标 deer-flow，不裁剪核心功能**」——沙箱/subagent/memory/tool/skill/mcp 等模块对齐 deer 完整实现。
  - 🔴 **恢复 5 个原「可选/裁剪」模块为主线**：**M10b AIO 沙箱**（生产容器隔离，原裁剪）、**M20 mcp**（原留接口）、**M21 community**（原留接口）、**M22 agents_config**（原不做）、**M23 uploads**（原不做）。依赖关系：M22→M13/M15；M20→M15/M16；M23→M16；M21→M15。全部进 Phase 2/5/5.5，并进 Part A 依赖图 / Part B Phase 表 / Part F 检查清单。
  - 🔴 **修正 2 处事实错误**：① 沙箱工具 **5→7**（v1.1 漏 glob/grep，补 `search.py`/`file_operation_lock.py`/`exceptions.py`/`local/list_dir.py`）；② subagent 执行器**非「双线程池」**，实为**单 scheduler pool(3) + 持久化隔离事件循环**(daemon 线程)。
  - 🟠 **M10 sandbox 现状勘误**：mini 已有 ~2300 行部分代码（缺 `local_sandbox_provider.py`/`search.py`/`file_operation_lock.py`/`exceptions.py` + 无测试/文档），v1.1 误记「无」。
  - 🟡 **M11 subagents 补全**：自定义子代理（config `subagents.custom_agents`）+ per-agent overrides（timeout/max_turns/model/skills）+ `token_collector.py` + 5 状态契约 + contracts json。
  - 🟡 **M15 tools 补全 9 内置工具**：view_image/task/skill_manage/tool_search/setup_agent/update_agent/invoke_acp_agent（原多标「可选/不做」）；invoke_acp_agent 改 **soft-load** 而非「不做」。
  - 🟡 **M16 中间件**：第 3 步 Uploads、第 18 步 DeferredToolFilter **取消「跳过」**（依赖已纳入主线）；仅 Guardrail 真正可选。
  - 🟡 **M13/M17 解除权宜**：删除 memory「AGENT_NAME_PATTERN 局部兜底」、M17「custom-agent 分支可选」——改为依赖已恢复的 M22。
  - ⚪ 新增红线 #29–#36（mcp session_pool/oauth、uploads symlink、agents_config 校验、AIO 跨进程锁/优雅关闭、subagent 隔离事件循环、自定义子代理 override、skill_manage 历史）；Part D.4 补 extensions_config mcpServers/community 用法；Part F 检查清单补 M10b/M20–M23 行。
- **v1.1（已修订）**：
  - 🔴 新增 **M-models 模型模块**（Phase 1），修正 M12/M13/M17 对 models 的依赖；补 config/model_config 对齐。
  - 🔴 修正**执行顺序循环依赖**：`runs/schemas.py` + `runs/store/base.py`（RunStore ABC）提前到 Phase 1（M4 内），`runs/store/memory.py`/manager/worker 留 Phase 8。
  - 🟡 新增 **M-build 工程化模块**（pyproject extras / Makefile / conftest / harness boundary 测试 / blocking-io gate / 样例文件 / langgraph 版本下限）。
  - 🟡 新增**可选模块清单**（M-opt-community / mcp / uploads / agents_config / DeerFlowClient），明确「本期不做/留接口」及各自被谁依赖。
  - 🟡 M17 补 **prompt 条件段 gating**（skills/deferred/subagent/soul/self_update/acp 各自 feature 开关）。
  - 🟡 D.3 改为「**对齐现有 langgraph.json**」（mini 已有 graphs 条目，仅缺 checkpointer 段）；补 D.5 build_event_store/build_thread_store 工厂。
  - ⚪ 新增红线 #26（langgraph 版本）、#27（stream 超时/用量）、#28（boundary/gate 强制测试）；M0 补 config_version bump；明确 runtime_paths→paths.resolve_path 替代；M7 修正 journal 依赖措辞（progress_reporter 在 worker 注入，无模块循环）。
  - ⚪ Part F 标注「已存在·更新」的旧文档（tools.md / 中间件.md / 模型更换.md，现已归档到 `docs/legacy/`）。
- **v1**：初版，覆盖 M0–M19 共 20 模块 + 25 红线。

> 📌 **实现进度与待办清单已拆分到 [todo.md](todo.md)**：总览统计 + 模块进度总表（一眼看完所有模块的 ✅/🔶/📋/⬜）+ 按 Phase 的待办 + 下次开工步骤。
> 本文件（ALIGNMENT_OUTLINE.md）专注**设计规格**：Part C 各模块的文件清单 / 依赖 / 可靠性要点 / 测试要求，Part D 集成装配，Part E 红线规则。改设计来这，查进度去 todo.md。

---

## 执行建议（给后续 AI）

1. **严格按 Phase 0→8 顺序**，每完成一个 Phase 跑全量 `backend/test/`（含 boundary/gate）。
2. **先地基后业务**：Phase 0（含 build）+ Phase 1（含 models + runs 基类）是所有模块的依赖，必须最先完成且测试充分。
3. **每模块三交付闭环**：代码→测试→学习文档，再进下一个。
4. **v1.2 全面对标，无核心裁剪**：mcp/community/uploads/agents_config/AIO 沙箱**已全部进主线**（M10b/M20–M23），按依赖顺序落地；真正只剩 Guardrail 中间件 + DeerFlowClient 两项按需。
5. **遇到 deer 与 mini 命名/路径差异**：以 mini 现有结构为准做适配，不照搬 deer 的 `app/` 层与 `runtime_paths`。
6. **每写完一个模块的学习文档**，回头校验文档与代码一致（签名、字段、默认值）。

> 参考实现目录：`../deer-flow/backend/packages/harness/deerflow/`（同路径同名文件即「移植」源）。
