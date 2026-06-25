# todo.md — mini-deer-flow 进度与待办

> **基线**：`cd backend && make test && make lint` → **1477 passed, 1 skipped, 0 lint**。
> **三交付** = 代码 + hermetic 测试（`test/test_<module>.py`）+ 学习文档（`docs/<module>.md`）。
> **设计规格**（文件清单 / 依赖 / 红线）查 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part C；**全模块学习文档**见 [README.md](README.md)（#1–#28）。

## 图例

✅ 完成 · 🔶 部分 · 📋 规格 · ⬜ 未开始

---

## 一、已完成（主线对标全部完成）

**Phase 0–8 全部 ✅**，对齐 deer-flow v1.2「全面对标、不裁剪核心功能」。harness 层核心（模型/沙箱/子代理/记忆/技能/MCP/联网/工具/上传/中间件 23 步/agent 装配/运行管理/Store/集成）零差距，1477 测试 + 28 篇文档。

| Phase | 模块 | 文档 |
|-------|------|------|
| 0 | M-build 工程化 · M0 config · M1 utils · M2 reflection · M3 user_context | build / testing-setup / config / utils / user_context |
| 1 | M-models · M4 persistence · M5 checkpointer · M6 events/store · M7 journal · M8 stream_bridge · M9 serialization | models / persistence / checkpointer / run_event_store / run_journal / stream_bridge / serialization |
| 2 | M10 sandbox(7 工具) · M10b AIO 沙箱 · M11 subagents · M12 tracing · M22 agents_config | sandbox / aio_sandbox / subagents / tracing / agents_config |
| 3 | M13 memory | memory |
| 4 | M14 skills | skills |
| 5 | M20 mcp · M21 community(12 provider) · M15 tools(9 内置) | mcp / community / tools |
| 5.5 | M23 uploads | uploads |
| 6 | M16 middlewares（23 步生产链） | middlewares |
| 7 | M17 agent（SDK + config 双入口 + custom-agent 分支） | agents |
| 8 | M18 runs（RunManager + worker）· M19 store + 集成装配（lifespan / langgraph.json / config.example） | runs / runtime_store / architecture |

**质量加固**：Phase 1（M4–M9）+ Phase 7–8（M17/M18/M19/集成）已完成四维审查（设计 / bug / 适配 / 测试文档），结论均为「零严重 bug + 测试缺口 + 文档措辞，已补齐」。

---

## 二、未完成内容提纲（与 deer-flow/backend 差距，按可做性排序）

> 逐模块全量对比 `deer-flow/backend` 的结论。**主线 agent 核心零差距**；差距的 ~90% 代码量是 mini 设计上不 port 的 Gateway 层。

### 2.1 真正可选（依赖独立外部包，按需）

| 项 | deer 代码 | 依赖 | 说明 |
|----|----------|------|------|
| **Guardrail 中间件** | `guardrails/`（4 文件 191 行：provider + builtin + middleware） | `guardrails` / `neMo-guardrails` | M16 第 7 步跳过位；Pre-tool-call 授权（Allowlist/OAP/自定义 provider） |
| **DeerFlowClient** | `client.py`（1327 行） | — | 嵌入式 in-process 客户端；mini 走 langgraph dev |

### 2.2 真实可用性缺口（建议补，按优先级）

| 优先级 | 项 | 缺什么 | 为什么补 |
|--------|----|--------|----------|
| 🔴 高 | `models/vllm_provider.py` | `VllmChatModel`（子类化 ChatOpenAI，保留 vLLM `reasoning` 字段在 full response / streaming delta / follow-up tool-call turn） | **config.example 路径 C 注释引用 `deerflow.models.vllm_provider:VllmChatModel` 但 mini 无实现**——用户配 vLLM 会 ImportError。dangling 引用，必须消 |
| 🟡 中 | `models/patched_*.py`（5） | DeepSeek / MIMO / MiniMax / OpenAI / StepFun 的流式 + reasoning 字段 workaround | 原生 langchain 类能跑，但这些 provider 有兼容 rough edges；视用户用哪些 |
| 🟡 中 | `scripts/migrate_user_isolation.py` | legacy `memory.json`/`threads/`/`agents/` → per-user 布局迁移 | mini 已是 per-user（M13/M22），但用户**从 deer-flow 迁过来**时有用 |
| 🟢 低 | `config/tracing_config.py` | `TracingConfig` 单例 + `reset_tracing_config` + `get_enabled_providers` | mini tracing 走 env 直读够用；缺配置单例 + 重置能力（管理层 nice-to-have） |
| 🟢 低 | `config/runtime_paths.py` | `project_root` / `runtime_home`（`DEER_FLOW_HOME` env 覆盖） | mini 用 `config/paths.py`；缺 env 覆盖能力 |
| 🟢 低 | `models/claude_provider.py` / `mindie_provider.py` / `openai_codex_provider.py` / `credential_loader.py` / `assistant_payload_replay.py` | 额外 provider 适配 / 凭证加载 / payload 回放 | langchain 已有 `ChatAnthropic` 等；mini 经 env/`$VAR` 直读；低 |

### 2.3 设计上不 port（mini 是 harness 教学版，非全栈产品）

| 项 | 规模 | 性质 |
|----|------|------|
| **`app/` Gateway 层** | 61 .py（17 API 路由 + 13 文件 auth[JWT+密码+多 provider] + 16 文件 IM 渠道[feishu/slack/telegram/discord/dingtalk/wechat/wecom]） | FastAPI 应用层；mini 走 `langgraph dev` 或基于 `runtime_lifespan` bundle 自搭 |
| **连带持久化** | `persistence/channel_connections/` / `feedback/` / `user/`（各 3 文件） | Gateway 专属 |
| **连带 config** | `config/channel_connections_config.py` / `suggestions_config.py` / `agents_api_config.py` | Gateway 专属 |
| **DB 迁移** | `persistence/migrations/`（alembic） | mini 走 `create_all`（教学简化） |
| **顶层文件** | `Dockerfile` / `debug.py` / `sitecustomize.py` / `ruff.toml` | 部署 / 调试；mini 用 pyproject 内 ruff |
| **backend/docs/** | 30 篇运维文档（API/AUTH_DESIGN/IM_CHANNEL/STREAMING…） | 多数对应不做的 Gateway；少数产品化时可参考 |

> **若要做薄 Gateway demo**（~3 文件，~200 行）：`app/main.py`（挂 `runtime_lifespan` bundle）+ `app/routers/runs.py`（POST `/threads/{id}/runs/stream` SSE + `/wait`）+ `app/routers/threads.py`（列表 + DELETE）。**不碰** auth / IM channels / feedback / artifacts。仅当要把 mini 当产品底座时才做——否则用上游 deer-flow 更省事。

---

## 三、质量加固（四维代码审查 + 第五维：文档深度审查）

> **统一要求——适用于全部 Phase，含已审的 Phase 1 + 7–8：**
>
> 1. **同步检查对应文档**。审查某个模块时，必须同时审 `docs/<module>.md`，**不能只审代码**。
> 2. **文档深度标准（新增第五维）**：
>    - **面向小白**——每个名词第一次出现都要解释，不假设读者会 LangGraph / deer-flow；
>    - **逐文件分析**——讲清该模块下**每个代码文件的作用**（不是只贴一段代码片段），每个文件一段「它做什么、为什么单独成文件」；
>    - **代码架构**——画出 / 讲清**各模块下的代码架构**：文件关系、调用链、数据流、状态机，谁依赖谁、谁触发谁；
>    - **目标**：读者看完能复述「这个文件做什么、在模块里处在哪一环、为什么这么拆」。
> 3. **已审 Phase 也要重做**。Phase 1（M4–M9）+ Phase 7–8（M17/M18/M19/集成）之前只做了
>    「设计对齐 / bug / 适配正确性 / 测试」**四维代码审查**，**缺文档深度维度**；按新标准补审其对应的
>    9 篇文档（persistence / checkpointer / run_event_store / run_journal / stream_bridge /
>    serialization / agents / runs / runtime_store + architecture），**代码侧「零严重 bug」的结论不变，仅补文档**。

### 3.1 审查范围与优先级（代码 + 文档双维度）

| 档 | Phase / 模块 | 代码审查 | 文档审查 | 重点（代码 + 文档） |
|----|-------------|---------|---------|---------------------|
| 🔴 高 | Phase 2: **M16 middlewares** | ⬜ | ⬜ | 23 步链顺序契约 / GraphBubbleUp 透传（#15）/ 倒序 after_model（Safety 在 Loop 后）/ Clarification 末位（#14）；wrap_* 异常吞噬、config gating 组合 |
| 🔴 高 | Phase 2: **M11 subagents** | ⬜ | ⬜ | 持久隔离事件循环（#34）/ 协作取消在 astream 边界 / 并发（#35）/ 5 状态契约；单 pool 非双池、token 回灌去重 |
| 🔴 高 | Phase 5: **M15 tools** | ⬜ | ⬜ | name 去重防 #1803 / soft-load（#24）/ tool_search 延迟装配 fail-closed / host-bash 过滤 |
| 🟡 中 | Phase 3: M13 memory · Phase 4: M14 skills | ⬜ | ⬜ | 同步 LLM 路径（#2615）/ user_id 跨 Timer（#20）/ JSON 部分更新 fail-closed（#21）；installer zip 炸弹 + symlink（#23）/ 安全扫描保守回退 |
| 🟡 中 | Phase 5: M20 mcp · M21 community · Phase 5.5: M23 uploads | ⬜ | ⬜ | OAuth skew（#30）/ session_pool（#29）/ mtime 缓存失效；token 刷新竞态 |
| 🟡 中 | Phase 2: M12 tracing · M22 agents_config | ⬜ | ⬜ | 追踪图根注入（#17）/ agents_config 三分支装配 |
| 🟢 低 | Phase 2: M10 / M10b sandbox | ⬜ | ⬜ | 路径翻译 / 跨进程锁（#33）/ 优雅关闭；已较成熟 |
| 🟢 补审 | **Phase 1（M4–M9）+ Phase 7–8（M17/M18/M19/集成）** | ✅ 已审（零严重 bug） | ⬜ **仅补文档** | 9 篇文档按「面向小白 / 逐文件 / 架构图」标准重审，代码不改 |

**经验**：两轮代码审查结论都是「零严重 bug + 测试缺口 + 文档措辞」。代码侧建议优先做高 ROI 三件（M16 / M11 / M15）；**文档侧——全部 Phase 都要按新标准过一遍**（含已审的 Phase 1 + 7–8，仅补文档不改代码）。

---

## 四、下次开工

**推荐主线**（按价值/紧急度）：

1. 🔴 **补 `models/vllm_provider.py`**（§2.2 高优先）——消除 config.example 的 dangling 引用（当前配 vLLM 会 ImportError）。从 deer-flow `models/vllm_provider.py` 移植 `VllmChatModel`（子类化 `ChatOpenAI`，保留 vLLM `reasoning` 字段在 full response / streaming delta / follow-up tool-call turn）+ 补 hermetic 测试 + config.example 路径 C 验证。**最小、最具体的「现在就坏」缺口。**

2. 🔴 **M16 middlewares 全维审查（代码四维 + 文档第五维）**（§3.1 高 ROI）——23 步链最复杂、最易藏 bug。对照 deer `agents/middlewares/` 查顺序契约 / GraphBubbleUp 透传 / config gating 组合，补测试缺口；**同步把 `docs/middlewares.md` 按 §三 标准（面向小白 + 逐文件 + 架构图）重写**。

3. 🟡 **M11 subagents + M15 tools 全维审查**（§3.1 高 ROI 后续）——持久事件循环 + name 去重 fail-closed；同样带文档维度（重写 `docs/subagents.md` / `docs/tools.md`）。

4. 🟢 **Phase 1 + 7–8 文档深度补审**（§3.1 补审档）——代码已审过（零严重 bug），仅把 9 篇文档（persistence / checkpointer / run_event_store / run_journal / stream_bridge / serialization / agents / runs / runtime_store + architecture）按新标准补一遍。可穿插在 2/3 之后或并行。

**先做 1（vllm_provider）还是 2（M16 审查）？** 1 是「修复 dangling 引用」（小、紧急），2 是「质量加固」（大、不紧急）。建议**先 1 后 2**——先把坏的修了，再做加固。

> 每完成一项，回 §二 / §三 / §3.1 把对应行标 ✅，更新本「下次开工」段。
