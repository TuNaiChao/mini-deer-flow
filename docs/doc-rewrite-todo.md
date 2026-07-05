# doc-rewrite-todo.md — 模块文档重写计划（28 篇 + 1 篇零基础导论）

> 一句话：把 docs/ 下 28 篇模块文档从「对齐上游的工程日志」基调，重写为「从代码本身讲起、面向小白」的教学文档；内容可参考同仓库的 `deerflow-book/chapters`（自顶向下的概念书），但讲的是 **mini-deer-flow 自己的代码**。#1–#28 顺序不变（Phase 0→8）。本文件是逐篇重写的 playbook：**标准 / 映射 / 进度 / 验收**。

> **重写的三个终态目标**（用户原话，每条补充都得服务它们；判一篇是否「够了」就拿这三条量）：
> 1. **零基础 → 掌握全部**：小白能从「不知道 agent 是什么」一路学到「掌握 deer-flow harness 的所有知识」。
> 2. **能跑能用**：读者照文档能把 mini 真正跑起来、和 agent 对话、看到工具 / 记忆 / 子代理生效（不是只会跑测试）。
> 3. **够面试**：读完能达到 agent 岗位面试要求——能把代码实现讲成「我懂这个 agent 概念」。

> **为补齐「零基础 + 能跑 + 够面试」，除重写 28 篇外，本计划另加四样**（见下文对应小节）：
> - **#0 一篇零基础导论**（`start-here.md`，新增）——讲清「deer-flow 是什么 / 前置知识 / 怎么装怎么跑 / 第一次对话」。（→ §3 Phase 导论）
> - **每篇带 learning outcomes（面试视角）+ 一张「面试概念地图」**——把模块显式映射到 agent 面试常考点。（→ §2.4 / §3 末）
> - **前置知识显式化**——写之前先确认读者懂哪些，`start-here.md` 兜底教。（→ §2.5）
> - **README 也按教学重写**（不止改一句话定位）——去掉「对齐 v1.2 / 红线 #N」腔，改成面向小白的入口 + 学习路径。（→ §1）

---

## 0. 为什么要重写

当前 28 篇已经详实（每篇 200–630 行），但**基调是「把 deer-flow 对齐到 mini 的工程记录」**——满篇「M-build / 红线 #28 / 对齐 7a6c4a99 / Phase X 全维重审」。对**只想读懂 mini 代码**的小白，这些是噪声：他要的是「这个模块是什么、在系统哪、每个函数干嘛、怎么用」，不是「我们怎么从上游搬过来的」。

重写目标（用户原话）：

- **从基础出发、全面完整**地讲解 mini-deer-flow 的代码实现；
- **既要有重要函数的细节讲解，也要有面向整体的结构框架**；
- 说明各个**函数和模块的作用和用法**；
- **面向小白**（每个名词第一次出现都解释，见 memory `docs-audience-beginner`）；
- 内容**可参考 `deerflow-book/chapters`**（借它的自顶向下叙事框架），但落点是 mini 的代码。

**保留**：当前**顺序与编号**（#1–#28 / Phase 0–8）不变；有价值的设计权衡 / 踩坑内容保留，但**重新框定**为「实现细节 / 为什么这么设计」，去掉「对齐日志」腔调。

---

## 1. 已做的清理（删了什么 / 留了什么）

### 删除（git 跟踪，可恢复）

- `docs/legacy/`（4 文件：`README.md` / `tools.md` / `中间件.md` / `模型更换.md`）——全部已被 docs/ 根同名文档取代（`tools.md` / `middlewares.md` / `models.md`）。
- `docs/ALIGNMENT_OUTLINE.md`（810 行设计规格）+ `docs/spec-M4-persistence.md`（592 行 M4 详规）——内容属「对齐工程日志」，与「面向小白讲代码」的教学定位冲突。设计权衡改从**代码 + 各模块文档本身**取，需要时查 git 历史。
- 死链清理：指向它们的引用已全部扫清——README、本文件，以及 7 篇含「红线索引」脚注的模块文档（`architecture` / `serialization` / `stream_bridge` / `run_event_store` / `persistence` / `run_journal` / `checkpointer`）。

### 保留并教学化改造（非模块文档，但要重写）

- `README.md`——**不止是索引，是零基础读者的入口**。除每篇重写后更新「一句话定位」，还要：① 重写开头引言（去掉「对齐 deer-flow v1.2 / M-build + M0–M19 / 全面对标」这类工程腔，改成「mini 是什么、为什么读这套文档、怎么从零开始」）；② 表格里每篇一句话定位**去掉「红线 #N / 防某某」**，改成小白能懂的一句话；③ 顶部加「零基础从这里开始 → [start-here.md](start-here.md)」+ 学习路径建议（先 #0 → 想看全貌扫 #28 → 再 #1→#28）。
- `todo.md`——**项目待办 + 工作进度日志**（代码侧），与本文档（文档重写）分工独立，**不合并**（文档重写进展不记在 todo.md，只记在此）。

> 注意：文档里大量出现的 "legacy" **代码概念**（`legacy checkpointer:` 配置段、`legacy` agent 布局 `{base_dir}/agents/`、`legacy memory.json`、`legacy` 全放行行为）是真实的**向后兼容回退特性**，在 `checkpointer.md` / `agents_config.md` / `memory.md` / `skills.md` 里是正文，**不是**指 `docs/legacy/` 文件夹，切勿误删误改。

---

## 2. 重写标准（每篇都遵守——保证逐篇完成时风格一致）

### 2.1 标题（沿用 memory `docs-title-format`）

`# N. filename.md — 中文（标签）`，N 保持现编号（#1–#28）。例：`# 13. sandbox.md — 沙箱（虚拟路径 + 7 工具 + provider）`。

### 2.2 必含小节（10 节，按此顺序）

1. **一句话定位 / 这是什么**——一段大白话：这个模块解决什么问题、给谁用。（借 deerflow-book 的开篇风格）
2. **零基础先读：名词解释**——本模块会用到的每个陌生名词，第一次出现先解释（venv / checkpointer / SSE / ORM…）。mini 现有文档这块做得好，保留。
3. **整体结构：它在系统里的位置**——一张图 / 文字依赖图：本模块上接谁、下接谁、和谁并列。这是「面向整体的结构框架」，deerflow-book 强项，**要补强**。
4. **核心概念**——本模块的关键抽象（类 / 协议 / 数据结构），用例子讲。
5. **代码走读：重要函数逐个讲**——**本节是重写重点**。挑本模块所有公开 / 关键函数，每个：签名 + 它干什么 + 关键分支 / 边界 + 一段真实代码摘录（带 `[文件:行号](../backend/...)` 链接）。
6. **数据流：一次调用怎么走完**——拿一个具体场景（如「用户发一条消息」「上传一个文件」），逐步追踪它经过本模块的哪些函数、数据怎么变。
7. **配置与用法**——相关 `config.yaml` / `extensions_config.json` 字段表 + 调用示例（`make dev` / API / 代码调用）。
8. **与其它模块的关系**——依赖谁、被谁依赖；交叉链接到对应文档。
9. **设计权衡与踩坑**——保留现有「为什么这么设计 / 踩过的坑」，但**去掉「对齐上游 / 红线 #N」措辞**，改述为「实现选择 + 原因」。红线编号可作「内部追溯」脚注保留一处，不当主线。
10. **常见问题 / 排错**——FAQ。

### 2.3 写作原则

- **面向小白**：假设读者会 Python 但不熟 LangGraph / agent / 异步。每个框架名词第一次出现解释一句。
- **代码真实**：函数签名、行号链接、配置字段必须和当前代码对得上——**重写时先读源码再写，不凭记忆**。（剥 docstring / comment 后再判逻辑，见 memory `diff-mini-upstream-strip-docstrings`。）
- **mini 为主、deerflow-book 为辅**：参考 deerflow-book 的「概念框架 / 叙事顺序」，但所有代码示例、文件路径、函数名都是 **mini** 的。mini 无 Gateway / IM / deployment（见 `todo.md` §1.2 边界声明），这些章节只借概念不抄实现。
- **去工程日志腔**：不写「M-build / Phase X 重审 / 对齐 7a6c4a99 / 红线 #N（主线）」。这些是工程记录，不进教学文档（原 ALIGNMENT_OUTLINE / spec-M4 已删，需要时查 git）。
- **不破坏代码**：重写文档期间 `cd backend && make test && make lint` 保持 **1713 / 0 lint**（文档不改代码；若顺手修代码须守此基线）。

### 2.4 每篇必带「学完能回答什么」（learning outcomes，面试视角）

光讲清代码不够「够面试」——读者要能把实现讲成**概念**。每篇在 §1（一句话定位）后或文末，列 **3–5 条「学完这篇你能回答」**，尽量贴 agent 面试常考点。例（checkpointer.md）：

- 「agent 的对话记忆怎么持久化的？为什么要委托 LangGraph Saver 而不自建？」
- 「checkpointer 和应用持久化（runs/threads）有什么区别？为什么表要分离？」
- 「异步服务里同步回调 / 阻塞文件 IO 怎么做到不卡事件循环？」

这同时是**自检**：如果你写不出 3 条有深度的问题，说明这篇还没讲到「为什么」、还没够面试。

### 2.5 零基础读者的前置知识（写之前先确认读者懂这些）

mini 现有文档假设「会 Python 但不熟 LangGraph / agent / 异步」。要把这个假设**显式化 + 兜底教**：

- **`start-here.md`（#0）必须兜底教**（一页纸概念，不展开成课）：什么是 LLM / token / 上下文窗口；什么是 agent（LLM + 工具 + 循环 = ReAct 雏形）；什么是 LangGraph（图 / 节点 / 状态 / checkpointer）；Python `async/await` 与事件循环为什么重要；venv / 依赖 / `make dev` 各自是什么。
- **进阶模块可外链预读**：指向 `deerflow-book/chapters/01-what-is-deerflow.md`～`05-lead-agent.md` 作为概念预读（借框架，非抄）。
- **每个框架名词第一次出现仍就地解释一句**（沿用 memory `docs-audience-beginner`）——前置知识是「兜底」，不替代就地解释。

---

## 3. 模块重写总表（顺序 = #0→#28；#0 为新增导论，#1–#28 编号不变）

> 「deerflow-book 参考」列指向同仓库 `/Users/tu/Documents/deerflow/deerflow-book/chapters/` 的章节（**借框架，非抄实现**）。
> 「代码文件」列是本篇**必须走读**的 mini 源码（相对 `backend/packages/harness/deerflow/`）。

### Phase 导论 — 零基础起点（**新增篇，非原 28 篇之一**）

> 这一篇是为终态目标 1+2（零基础 / 能跑）补的入口。**建议第一个写**——它是读者见到的第一页，决定「读者进不进得来」。

| # | 文档 | 代码文件（mini） | deerflow-book 参考 | 状态 |
|---|------|------------------|--------------------|------|
| 0 | start-here.md | 整仓导览（`README`、`backend/Makefile`、`langgraph.json`、`config/.env.example`）+ 一次 `make dev` 实跑 | 01 what-is · 02 repo-overview · 03 quick-start | ✅ |

**必含**：① deer-flow 是什么 / mini 和上游的关系（教学版，不 port Gateway/IM/部署）；② 前置知识清单（§2.5，一页纸教完）；③ 从零装跑（clone → venv → `.env` 填 key → `make dev` → 浏览器打开 langgraph dev UI → 发第一条消息看到真实回复）；④ 「这张图看清全貌」——借 architecture.md 的全景，每个模块一句话 + 推荐读序；⑤ 常见第一跑坑（API key / 模型名 / iCloud venv，见 memory `mini-test-invoke`）。

### Phase 0 — 地基

| # | 文档 | 代码文件（mini） | deerflow-book 参考 | 状态 |
|---|------|------------------|--------------------|------|
| 1 | build.md | `backend/{Makefile, pyproject.toml, langgraph.json, ruff.toml}`, `pytest.ini`, `test/conftest.py`, `test/support/detectors/blocking_io_runtime.py` | 02 repo-overview · 03 quick-start · 23 deployment | ✅ |
| 2 | testing-setup.md | `test/conftest.py`, blocking-IO gate, `pytest.ini` | 03 quick-start | ✅ |
| 3 | config.md | `config/{app_config, paths, reload_boundary, *_config（22 个）}` | 21 config-system · 附录 B | ✅ |
| 4 | utils.md | `utils/{time, messages, network, readability}` | —（内部工具） | ✅ |
| 5 | user_context.md | `runtime/user_context.py` | —（内部） | ✅ |

### Phase 1 — 模型 + 运行时基础

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 6 | models.md | `models/{factory, vllm_provider}`, `config/model_config` | 22 model-config | ✅ |
| 7 | persistence.md | `persistence/{base, engine, json_compat, models/, run/, thread_meta/}` | —（内部 ORM） | ✅ |
| 8 | checkpointer.md | `runtime/checkpointer/{provider, async_provider}`, `config/{checkpointer_config, database_config}` | 04 langgraph-engine | ✅ |
| 9 | run_event_store.md | `runtime/events/store/{base, memory, jsonl, db}`, `persistence/models/run_event` | —（内部） | ✅ |
| 10 | run_journal.md | `runtime/journal.py` | —（内部） | ✅ |
| 11 | stream_bridge.md | `runtime/stream_bridge/{base, memory, async_provider}`, `config/stream_bridge_config` | —（内部） | ✅ |
| 12 | serialization.md | `runtime/{serialization, converters}` | —（内部） | ✅ |

### Phase 2 — 沙箱 / 子代理 / 追踪

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 13 | sandbox.md | `sandbox/{sandbox, sandbox_provider, local/, security, search, middleware, file_operation_lock, exceptions, tools}` | 13 sandbox-abstraction | ✅ |
| 14 | aio_sandbox.md | `community/aio_sandbox/{aio_sandbox, remote_backend}`, sandbox 核心 | 14 sandbox-implementations | ⬜ |
| 15 | subagents.md | `subagents/{config, executor, registry, status_contract, token_collector}`, `tools/builtins/task_tool` | 08 subagent-overview · 09 executor · 10 orchestration | ⬜ |
| 16 | tracing.md | `tracing/{factory, metadata}` | —（内部） | ⬜ |
| 17 | agents_config.md | `config/agents_config`, `skills/`（SOUL.md 协议）, `agents/factory` 自定义分支 | 05 lead-agent（自定义） | ⬜ |

### Phase 3 — 记忆

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 18 | memory.md | `agents/memory/{updater, queue, storage, message_processing, prompt, summarization_hook}` | 11 memory-architecture · 12 memory-pipeline | ⬜ |

### Phase 4 — 技能

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 19 | skills.md | `skills/{installer, parser, permissions, security_scanner, slash, tool_policy, types, validation}` | 17 skills-system · 18 custom-skills | ⬜ |

### Phase 5 — MCP + 联网

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 20 | mcp.md | `mcp/{client, cache, oauth, session_pool, tools}`, `config/extensions_config` | 16 mcp-extensions | ⬜ |
| 21 | community.md | `community/{_common, aio_sandbox/, + 各 provider}` | 15 builtin-tools（web 部分） | ⬜ |
| 22 | tools.md | `tools/{tools, types, sync, mcp_metadata, skill_manage_tool}`, `tools/builtins/` | 15 builtin-tools | ⬜ |

### Phase 5.5 — 上传

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 23 | uploads.md | `uploads/{manager, conversion}` | —（内部） | ⬜ |

### Phase 6 — 中间件

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 24 | middlewares.md | `agents/middlewares/*`（23 个中间件） | 06 middleware-pipeline · 07 context-engineering | ⬜ |

### Phase 7 — Agent 装配

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 25 | agents.md | `agents/{factory, features, thread_state, lead_agent/}` | 04 langgraph-engine · 05 lead-agent · 07 context-engineering | ⬜ |

### Phase 8 — 运行管理 + 集成装配

| # | 文档 | 代码文件 | deerflow-book 参考 | 状态 |
|---|------|----------|--------------------|------|
| 26 | runs.md | `runtime/runs/{manager, worker, schemas, naming, store/}` | 10 orchestration | ⬜ |
| 27 | runtime_store.md | `runtime/store/{provider, async_provider, _sqlite_utils}` | 04 langgraph-engine（store） | ⬜ |
| 28 | architecture.md | `runtime/lifespan.py`（RuntimeBundle）+ 全模块串联 | 01 what-is · 02 repo-overview · 19 gateway（仅 lifespan 概念） | ⬜ |

> **映射说明**：deerflow-book 是讲上游 deer-**flow**（全栈产品，含 Gateway / IM / 部署）的概念书；mini 是 harness 教学版，**不 port** Gateway / IM / deployment（见 `todo.md` §1.2）。所以映射是**部分映射**——借 deerflow-book 的「这个子系统是什么、怎么组织」的概念框架，代码示例一律换成 mini 的；deerflow-book 里 Gateway / IM / Docker 部署等章节，mini 不对应，跳过。

### 面试概念地图（把模块对到 agent 面试常考点）

> 服务终态目标 3（够面试）。读完这套文档，下面这些 agent 面试常考点你都应该能拿 mini 的实现来答——**面试前过一遍这张表**。

| 面试常考点 | 在 mini 哪篇讲 | 能怎么答（一句话） |
|-----------|---------------|--------------------|
| agent 是什么 / ReAct（推理+行动循环） | #0 start-here · #25 agents | LLM + 工具 + 循环；mini 用 LangGraph 图把「模型决策 → 调工具 → 回灌结果 → 再决策」编成状态机 |
| 工具调用 / tool-use | #22 tools · #13 sandbox | 9 内置工具 + MCP/community；`get_available_tools` 组装、按 name 去重、host-bash 过滤 |
| 长期记忆 vs 短期记忆 | #18 memory · #8 checkpointer | checkpointer = 对话短期状态快照；memory = LLM 抽取的跨会话长期事实（per-user/agent） |
| 上下文工程（防 context 爆） | #24 middlewares | ToolOutputBudget 裁工具输出、摘要前抢拍、悬空消息补齐 |
| 多智能体编排 | #15 subagents · #26 runs | 委派 + 单 scheduler pool + 5 状态契约 + token 回灌 |
| 沙箱 / 安全执行 | #13 sandbox · #14 aio_sandbox | 虚拟路径 + 文件锁；Docker/K8s 容器隔离 + 暖池 + 路径穿越防御 |
| 流式输出 / SSE | #11 stream_bridge | 生产者-消费者解耦 + 重连补播；落盘事件流(journal) vs 实时推送(bridge) 分工 |
| 可观测性 / tracing | #16 tracing · #9/#10 event | LangSmith/Langfuse 图根注入；事件存储 + token 核算 |
| MCP（外部工具协议） | #20 mcp | stdio/sse/http 三传输 + 有状态会话池 + OAuth |
| 状态管理 / 持久化 | #7 persistence · #27 runtime_store | SQLAlchemy ORM + WAL；LangGraph Store 跨线程记忆 |
| 技能 / 可扩展人格 | #19 skills · #17 agents_config | SKILL.md/SOUL.md 协议 + 安全安装 + 能力白名单 |
| **agent 评估（eval）** | **mini 无此模块** | 概念须会（数据集 / 回归 / LLM-as-judge）；mini 不实现——面试讲「我会怎么搭」，**诚实，不装懂** |
| **生产部署 / 扩缩容** | **mini 不 port** | 概念须会（Gateway / 队列 / 多节点 checkpointer=postgres）；指向 deerflow-book 19/23 |

> 凡标「mini 无 / 不 port」的，**面试时讲概念 + 讲「在 mini 里我会怎么加」**，不要把没有的说成有。

---

## 4. 执行顺序与进度

- **顺序 = 上表 #0→#28**（依赖链，不跳）。**建议先写 #0 start-here**（零基础入口，决定读者进不进得来），再 #1→#28。同一 Phase 内按表中顺序逐篇做；跨 Phase 不抢跑（Phase 1 依赖 Phase 0，以此类推——和现有依赖图一致）。
- **每次开工一篇**：把下表对应行 `⬜ → 🟡（进行中）`，做完 → `✅`，并在该篇文档顶部写一行「重写日期 / 对照代码 commit」（方便后人核对文档与代码版本）。

### 进度看板

| # | 文档 | 状态 | 备注 |
|---|------|------|------|
| 0 | start-here | ✅ | **新增篇**（零基础导论）；必含「能跑起来 + 全景图 + 读序 + 前置知识」——2026-07-05 完成 |
| 1 | build | ✅ | 2026-07-05 完成（去 M-build/红线腔 + 加 gate 代码走读 + 数据流 + learning outcomes） |
| 2 | testing-setup | ✅ | 2026-07-05 完成（保留 Python 3.14 .pth 深挖 + hermetic 约定；gate 机制 slim 后交叉链 build.md；加 fake-LLM 模式 C + FAQ） |
| 3 | config | ✅ | 2026-07-05 完成（去 M0/红线腔；勘误「17 子配置」→ 实际 19 个；补 get_app_config 事件循环 carve-out；加代码走读 + learning outcomes） |
| 4 | utils | ✅ | 2026-07-05 完成（补 network+readability 两文件——原文只讲 time/messages；勘误 message_to_text 已实现非 deferred；去 M1 腔 + learning outcomes） |
| 5 | user_context | ✅ | 2026-07-05 完成（去 M3/红线腔 + 加行号链接 + learning outcomes；保留 ContextVar 便签比喻 + 跨线程坑教学）——**Phase 0 地基全部收口** |
| 6 | models | ✅ | 2026-07-05 完成（删 Phase 1 全维重审对齐日志整块；M-label→交叉链 #16/#18/#24/#25；红线 #17→平述；全篇补行号链接——原文一条都没有；10 节模板重排，保留痛点表 + attach_tracing 双调用方图 + vllm 三钩子图；learning outcomes 6 条）——**Phase 1 开篇** |
| 7 | persistence | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + 4 项对齐清单并入正文；M6/M18/Phase 8→交叉链 #9/#26/#28；红线 #2/#10/#12/#24/#25/#28→平述；补 27 处行号链接；10 节模板重排，保留河流/水滴比喻 + WAL 三件套 + 痛点）——**14 个源码文件全部核完，原文技术声明零失实** |
| 8 | checkpointer | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + 勘误 §3.1-A 上游漂移叙述；M19/Phase 8/D.3→交叉链 #27/#28；红线 #1/#24/#1912→平述；补 28 处行号链接；10 节模板，保留存档槽比喻 + 三级优先级 + async cm vs sync singleton；learning outcomes 7 条） |
| 9 | run_event_store | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + #3686 narrative；M4/M7→交叉链 #7/#10；红线 #1/#3/#4/#10→平述；补 18 处行号链接；10 节模板，保留书/章节/页码比喻 + 双向游标分页 + memory 4 组投影；learning outcomes 7 条）——**6 个源码文件全部核完，原文技术声明零失实** |
| 10 | run_journal | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + #3658/#3697 narrative + 2 minor 对齐 + defer；红线 #8→平述；M7/M13/M16/M18/Phase 8→交叉链 #9/#11/#15/#24/#26；补 23 处行号链接；10 节模板，保留「监工」比喻 + 写入侧/存储侧 + 三类事件表；learning outcomes 7 条）——**613 行 journal.py 全核完，原文技术声明零失实** |
| 11 | stream_bridge | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + #3700 narrative；红线 #11→平述；补 27 处行号链接；10 节模板，保留快递柜比喻 + 痛点四表 + END/心跳双哨兵对比 + O(1) 续播核验教学；learning outcomes 7 条；新增 stream_bridge vs RunEventStore vs RunJournal 三栏对比表；**5 个源码文件全部核完，原文技术声明零失实**） |
| 12 | serialization | ✅ | 2026-07-05 完成（删 Phase 1 全维重审整块 + #3595 narrative；M9/M7/M8 插队→平述；红线 #1→平述；「按 deer 参考完整移植」→平述；补 22 处行号链接；10 节模板，保留「出风口净水器」比喻 + 痛点五表 + 兜底链八档表 + 两文件职责分工表 + 与存储三件套正交对比表；learning outcomes 7 条；**勘误纠 2 处失实**：原 §6 接口注释 + §8 依赖图误写「剥 `__interrupt__`」，源码 [:70] 只剥 `__pregel_*`、`__interrupt__` 故意保留；**两源码文件全核完**）——**🎉 Phase 1（#6–#12）全部完成** |
| 13 | sandbox | ✅ | 2026-07-05 完成（删 Phase 2 全维重审整块 + #3730/#3786/#3729/#3579/#2676/#3597 narrative；红线 #4/#15/#24→平述；M10/M10b/M14/M15/M16/M17/M23→交叉链 #14/#18/#19/#22/#24/#25；补 147 处行号链接；10 节模板，保留酒店门牌号比喻 + /mnt 语义锚点 + 两层防御 + 7 工具表 + book↔mini 差异表（5 工具→7、5 方法→8、uploads 只读→读写，诚实标注「借框架非抄实现」）；learning outcomes 8 条；**13 源码文件 + 审计中间件 + config 全核完，原文技术声明零失实**）——**Phase 2 开篇** |
| 14 | aio_sandbox | ⬜ | |
| 15 | subagents | ⬜ | |
| 16 | tracing | ⬜ | |
| 17 | agents_config | ⬜ | |
| 18 | memory | ⬜ | |
| 19 | skills | ⬜ | |
| 20 | mcp | ⬜ | |
| 21 | community | ⬜ | |
| 22 | tools | ⬜ | |
| 23 | uploads | ⬜ | 「下一篇」死链已临时改指 middlewares.md，重写时一并理顺 |
| 24 | middlewares | ⬜ | |
| 25 | agents | ⬜ | |
| 26 | runs | ⬜ | |
| 27 | runtime_store | ⬜ | |
| 28 | architecture | ⬜ | 收尾篇；其 §5 标题「对齐 ALIGNMENT_OUTLINE Part D」的死链已清 |

---

## 5. 单篇验收清单（每篇完成前逐项打勾）

- [ ] 10 个必含小节齐全（§2.2）。
- [ ] 「代码走读」覆盖该模块**所有公开函数 / 类**（不只挑几个），每个带签名 + 真实代码摘录 + 行号链接。
- [ ] 「整体结构」有一张依赖图（文字或 ASCII），标出本模块在系统里的位置。
- [ ] 「数据流」至少一个端到端追踪场景。
- [ ] 面向小白：陌生名词第一次出现都有解释。
- [ ] **learning outcomes**：列了 3–5 条「学完能回答」（§2.4），贴面试常考点。
- [ ] 代码引用与当前源码一致（**读了源码再写**，行号链接可点开）。
- [ ] 去掉「M-build / 对齐 7a6c4a99 / 红线 #N（主线）」工程日志腔；设计权衡改述为实现选择。
- [ ] 交叉链接到上下游模块文档（相对路径 `[xxx.md](xxx.md)`）。
- [ ] 若改了代码（一般不改）：`cd backend && make test && make lint` 仍 **1713 / 0 lint**。
- [ ] 顺手更新 `README.md` 该篇的「一句话定位」（**去掉「红线 #N」**，小白能懂）+ 本文件进度看板（⬜→✅）。
- [ ] （**#0 start-here 专属**）读者照它能**真的跑起来**并发出第一条消息看到回复；含全景图 + 推荐读序 + 第一跑坑。

---

## 6. 怎么用本文件（给未来的我 / 协作者）

> **给零基础读者的阅读策略**（写完 #0 后也写进 README 顶部）：先读 [start-here.md](start-here.md) 把它跑起来 → 想看全貌可先扫 [architecture.md](architecture.md)（#28，收尾篇但有全景图，当「地图」先看无妨）→ 再按 #1→#28 逐篇钻。**面试前过一遍 §3 末的「面试概念地图」**。

1. 挑进度看板里第一个 ⬜ 的模块（**优先 #0 start-here**）。
2. 读它的「代码文件」列里所有源码（先剥 docstring 看逻辑）+ 「deerflow-book 参考」列对应章节（借框架）。
3. 按 §2 标准重写 `docs/<module>.md`（**原地覆写**，文件名 / 编号不变）。
4. 按 §5 验收清单自检。
5. 更新本文件看板 + `README.md`「一句话定位」。
6. 提交（消息建议 `docs: rewrite <module>.md for beginner clarity`）。

> 重写期间不影响代码与运行：`make dev` 仍是能干活的 agent（见 `todo.md` 顶部基线）。
