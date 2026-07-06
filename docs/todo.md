# todo.md — mini-deer-flow 待办

> **基线**：`cd backend && make test && make lint` → **1713 passed, 0 lint**。mini 已端到端实跑验证为可用 agent（真 key 实跑：能聊天 + bash/读写文件 + 多轮记忆）。详细历史查 git log / memory。
> **三交付** = 代码 + hermetic 测试 + 学习文档。任何代码改动后基线必须保持 **1713 / 0 lint**。
> **文档重写已完成**（#0 导论 + #1–#28 = 29 篇全部 ✅，全按新标准 11 节 + §9 设计动机 + §10 实现差异）。本文件只管代码侧。

---

## 1. 项目待做（代码侧）

### 1.1 维护（持续，低强度）

- **跟进上游漂移**：deer-flow 上游会继续更新。下次有新提交时，按 memory `upstream-deerflow-drifted.md` 流程重审（`cd deer-flow && git log` → 逐文件 diff → 六维重审：代码对齐 + hermetic 测试）。这是「跟上」，不是新工作。
- **回归基线**：任何代码改动后 `cd backend && make test && make lint` 保持 **1713 passed / 0 lint**。

### 1.2 可选增量（按需——有真实需求才做，非必要）

> 把 mini 从「教学/对齐复刻」往「能拿去用」推的可选增量，由真实需求触发。每项遵循三交付。基座已能跑（✅），按需挑。

| 优先级 | 项 | 触发条件 | 规模 | 说明 |
|--------|----|---------|------|------|
| 🟡 中 | **薄 Gateway demo** | 要把 mini 当产品底座（非 `langgraph dev`）| 中（~3 文件 ~200 行）| `app/main.py`（挂 `runtime_lifespan`）+ `app/routers/runs.py`（POST `/threads/{id}/runs/stream` SSE + `/wait`）+ `app/routers/threads.py`（列表 + DELETE）。**不碰** auth / IM 渠道 / feedback / artifacts |
| 🟡 中 | **patched providers** | 你用的模型有兼容 rough edges | 各小 | `models/patched_*.py`：DeepSeek / MIMO / MiniMax / OpenAI / StepFun 流式 + reasoning 字段 workaround。原生 langchain 类能跑但有边界 case，**按实际用的模型挑补** |
| 🟡 中 | **`migrate_user_isolation.py`** | 从真 deer-flow 迁历史数据到 mini | 小 | legacy `memory.json` / `threads/` / `agents/` → per-user 布局。mini 已是 per-user；仅当带历史数据迁过来时有用 |
| 🟢 低 | **sqlite checkpointer** | 要对话历史**跨重启**保留 | 小 | 改 `database.backend` / `checkpointer.type: sqlite`（`langgraph-checkpoint-sqlite` 已在 dev deps）。默认 memory backend 重启即清——这是当前唯一设计性短板 |
| 🟢 低 | **Guardrail 中间件** | 要 Pre-tool-call 授权（Allowlist / OAP / 自定义 provider）| 中（~4 文件 ~191 行）| `guardrails/`（provider + builtin + middleware）；中间件链预留的跳过位。需 `guardrails` / `neMo-guardrails` 包 |
| 🟢 低 | **DeerFlowClient** | 要嵌入式 in-process 客户端（不走 langgraph dev）| 大（~1327 行）| `client.py`：进程内客户端，返回类型对齐 Gateway API schema。mini 当前走 `langgraph dev`，不需要 |
| 🟢 低 | **额外 provider 适配** | 要用这些特定 provider | 各小 | `claude_provider` / `mindie_provider` / `openai_codex_provider` / `credential_loader`。langchain 已有 `ChatAnthropic` 等替代，mini 经 env / `$VAR` 直读 |
| 🟢 低 | **`config/tracing_config.py` 单例** | 要管理层动态重置 tracing | 小 | `TracingConfig` 单例 + `reset_tracing_config` + `get_enabled_providers`。mini tracing 走 env 直读够用；nice-to-have |

> **判断原则**（按「你拿 mini 干什么」选）：
> - **当教学/对齐参考** → 一项都不用做。
> - **当自用 agent 底座** → 按你用的模型挑 patched providers。
> - **当产品底座** → 薄 Gateway demo + 按需 providers。
> - **从 deer-flow 迁数据** → migrate 脚本。

> **设计上不 port**（边界声明，**非待办**）：`app/` Gateway 层（17 路由 + auth + IM 渠道）、连带持久化 / config、alembic 迁移、顶层 `Dockerfile` / `debug.py` 等。mini 是 harness 教学版，走 `langgraph dev` 或基于 `runtime_lifespan` 自搭（见 §1.2 薄 Gateway demo）。用户隔离已由 `get_effective_user_id()` 正确处理。

---

## 2. 工作进度日志（下次开工先看这里）

> 每次开工 / 收工追加一行：日期 / 做了什么 / 下次接着做。技术细节查 git log / memory，本表只记「做到哪、下一步」。

| 日期 | 做了什么 | 下次接着做 |
|------|----------|-----------|
| 2026-07-06 | 文档重写全部完成（#0 + #1–#28 = 29 篇）；删 `doc-rewrite-todo.md`（playbook 任务结束），「面试概念地图」移入 `README.md`；29 篇教学 doc 全 0 工程日志腔 token | 代码侧无必须做的；按需挑 §1.2 |
| 2026-07-05 | docs/ 清理：删 `legacy/`（4 文件）+ `ALIGNMENT_OUTLINE.md` + `spec-M4-persistence.md`，死链全清；精简 todo.md（剥离已完成段、剥离文档重写）；文档重写 playbook 另立为 `doc-rewrite-todo.md`（已于 2026-07-06 删除） | 按需挑 §1.2 代码增量 |
| 2026-07-03 | 端到端冒烟全绿：真 key 实跑拿到 LLM 真实回复（「2+2=4」）；修 2 个 blocking-IO bug + 2 个环境问题 + 1 个 `tools:` 配置坑；基线 1711 → **1713**。环境根治：`UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow`（iCloud 坑） | （已完成，留作上下文）|

---

> 一句话：代码侧**无必须做的**——mini 已是能干活的 agent（已实跑证实：聊天 + bash/读写文件 + 多轮记忆）。增量按需挑 §1.2。文档重写已完成（29 篇）。
