# todo.md — mini-deer-flow 待办

> **基线**：`cd backend && make test && make lint` → **1711 passed, 1 skipped, 0 lint**。
> **对齐状态**：与上游 deer-flow `7a6c4a99`（2026-06-26）对齐**已完成**——全 Phase 重审（13 模块全 ✅）+ 附加专项 pick-list（8/8 ✅）。基线从 1477 → 1711（+234 测试）。本文件**不再记录已完成历史**，只列**还需做的事**。历史细节查 git log / memory。
> **三交付** = 代码 + hermetic 测试（`test/test_<module>.py`）+ 学习文档（`docs/<module>.md`）。任何改动后 `make test && make lint` 必须保持 **1711 / 0 lint**。
> **设计规格**（文件清单 / 依赖 / 红线）查 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) Part C；**全模块学习文档**见 [README.md](README.md)（#1–#28）。

---

## 0. 当前状态（背景：为什么待办是这些）

### 0.1 静态核实清单（已确认 ✓）

| 项 | 状态 | 证据 |
|----|------|------|
| 单元测试 | ✅ | `make test` → **1711 passed, 1 skipped** |
| lint / format | ✅ | `ruff check` + `ruff format --check` 全绿 |
| 上游对齐 | ✅ | 全 13 模块重审 ✅；HEAD 仍是上游 `7a6c4a99`（2026-06-26），漂移清单以来零新提交 |
| 图注册 | ✅ | `langgraph.json` 注册 `lead_agent: deerflow.agents:make_lead_agent` + checkpointer 段 |
| `langgraph dev` | ✅ | `langgraph-cli` 0.4.29 已装；`make dev` = `langgraph dev` |
| 配置模板 | ✅ | `config.example.yaml`（项目根）含 `models:` 段（DeepSeek V4 Pro 等模板）|
| harness 边界 | ✅ | harness 不 import `app.*`（AST 扫描测试强制）|

### 0.2 能否正常使用 / 能否当 agent？—— 诚实评估

**代码层 ✅ 齐全。** agent 全栈都在——`make_lead_agent`（23+ 步中间件链）+ 7 沙箱工具 + 内置工具 + MCP/community/skill 装配 + memory + 持久化（checkpointer + RunStore + RunEventStore）+ runs 管理器 + SSE 流桥 + serialization + tracing。1711 条 hermetic 测试覆盖各模块契约。

**运行层 ⚠️ 未经端到端验证。** 关键 caveat——**hermetic 单测绿 ≠ 系统能跑**。单测里 LLM / DB / MCP 子进程 / 网络**全 mock**，下列集成层问题单测抓不到，**至今没跑过一次真实 `langgraph dev` 对话**：真实 `config.yaml` 加载 / 真 LLM API 调用 + tracing / `runtime_lifespan` 对真 sqlite 的启动序 / SSE 真实流式 / 真沙箱文件操作 / Studio UI 能否发现图并发起 run。

**结论：很可能能跑（代码 + 装配都齐全），但「能跑」目前是个未验证的推断，不是已证实的事实。** 集成层大概率有 1–N 个小修需要。**不补 §1 这步，「能当 agent 用」停留在理论。**

---

## 1. 端到端冒烟验证（**最高优先**，未做）

这是对全部对齐工作的最终验收，也是唯一能回答「到底能不能跑」的事。按顺序做：

1. **最小 config 起跑**
   - `cp config.example.yaml backend/config.yaml`，配**一个**真实 model（OpenAI 最简：`api_key: $OPENAI_API_KEY`，或本地 vLLM）
   - `cd backend && make dev` → 确认 langgraph dev server 起来、Studio（`http://localhost:2026` 或 langgraph 给的端口）能发现 `lead_agent` 图
   - 若起不来：查 config 加载报错 / `make_lead_agent` 反射 / lifespan 初始化——记录并修

2. **一轮真实对话**
   - Studio 里发一条消息 → 确认真 LLM 回复 + 消息落库（checkpointer）
   - 触发一个沙箱工具（如 `bash` `ls`）→ 确认 LocalSandbox 路径翻译 + 文件落 `DEER_FLOW_HOME`
   - 触发 `present_files` / `ask_clarification` → 确认内置工具 + 中断恢复

3. **可选能力冒烟**（按需）
   - 子代理（`task` 工具）→ 确认 subagent 隔离循环 + token 归桶
   - MCP 工具（配一个 stdio server）→ 确认 #3597 路径翻译 + session_pool
   - 多轮 + 重启 → 确认 checkpointer 恢复 + memory 注入

4. **blocking-IO gate**
   - `make test-blocking-io` 全绿（验证「同步阻塞 IO 不跑在事件循环里」红线在真实代码路径上成立）

**冒烟若失败要补什么**：大概率是集成层小修（不是补模块——模块都在），如 config 字段缺失 / lifespan 序 / Studio 兼容 / 真 LLM 边界 case。**每发现一个就按三交付修（代码 + hermetic 测试复现 + 文档）**，记到本文件末尾「冒烟修复记录」。

---

## 2. 维护（持续，低强度，不计入完成度）

- **跟进上游漂移**：deer-flow 上游会继续更新。下次有新提交时，按 memory `upstream-deerflow-drifted.md` 的流程（`cd deer-flow && git log` → 逐文件 diff → 六维重审[代码对齐 + 面向小白文档 + hermetic 测试]）。这是「跟上」，不是新工作。
- **回归基线**：任何改动后 `cd backend && make test && make lint` 必须保持 **1711 passed, 0 lint**。

---

## 3. 扩展方向（按需，有真实需求才做——非必要、不计入完成度）

> 与 §1 冒烟验证不同：这些是**把 mini 从「教学/对齐复刻」往「能拿去用」推**的可选增量，由真实需求触发。每项仍遵循三交付。**建议先做 §1 冒烟、确认基座能跑，再按需挑这里的项目。**

| 优先级 | 项 | 触发条件 | 规模 | 说明 |
|--------|----|---------|------|------|
| 🟡 中 | **薄 Gateway demo** | 要把 mini 当产品底座（非 `langgraph dev`）| 中（~3 文件 ~200 行）| `app/main.py`（挂 `runtime_lifespan`）+ `app/routers/runs.py`（POST `/threads/{id}/runs/stream` SSE + `/wait`）+ `app/routers/threads.py`（列表 + DELETE）。**不碰** auth / IM 渠道 / feedback / artifacts |
| 🟡 中 | **patched providers** | 你用的模型有兼容 rough edges | 各小 | `models/patched_*.py`：DeepSeek / MIMO / MiniMax / OpenAI / StepFun 的流式 + reasoning 字段 workaround。原生 langchain 类能跑但有边界 case。**按实际用的模型挑补** |
| 🟡 中 | **`migrate_user_isolation.py`** | 从真 deer-flow 迁历史数据到 mini | 小 | legacy `memory.json`/`threads/`/`agents/` → per-user 布局。mini 已是 per-user；仅当带历史数据迁过来时有用 |
| 🟢 低 | **Guardrail 中间件** | 要 Pre-tool-call 授权（Allowlist/OAP/自定义 provider）| 中（~4 文件 ~191 行）| `guardrails/`（provider + builtin + middleware）；中间件链预留的跳过位。需 `guardrails` / `neMo-guardrails` 包 |
| 🟢 低 | **DeerFlowClient** | 要嵌入式 in-process 客户端（不走 langgraph dev）| 大（~1327 行）| `client.py`：进程内客户端，返回类型对齐 Gateway API schema。mini 当前走 `langgraph dev`，不需要 |
| 🟢 低 | **额外 provider 适配** | 要用这些特定 provider | 各小 | `claude_provider` / `mindie_provider` / `openai_codex_provider` / `credential_loader` / `assistant_payload_replay`。langchain 已有 `ChatAnthropic` 等替代，mini 经 env/`$VAR` 直读 |
| 🟢 低 | **`config/tracing_config.py` 单例** | 要管理层动态重置 tracing | 小 | `TracingConfig` 单例 + `reset_tracing_config` + `get_enabled_providers`。mini tracing 走 env 直读够用；nice-to-have |
| 🟢 低 | **教学深化** | 要发挥 mini 的教学定位 | 中 | 29 篇文档（README 索引已有）串成「按 X 天学完」有序学习路径 + 5 分钟 quickstart。文档工作量 |

> **判断原则**（按「你拿 mini 干什么」选）：
> - **当教学/对齐参考** → 一项都不用做（§1 冒烟足矣）
> - **当自用 agent 底座** → 按你用的模型挑 patched providers
> - **当产品底座** → 薄 Gateway demo + 按需 providers
> - **从 deer-flow 迁数据** → migrate 脚本

---

## 4. 设计上不 port（边界声明，**非待办**）

mini 是 harness 教学版，非全栈产品。以下按设计**不补**，列此仅防后人误以为是遗漏：

| 项 | 规模 | 为什么不 port |
|----|------|---------------|
| **`app/` Gateway 层** | 61 .py（17 API 路由 + 13 文件 auth + 16 文件 IM 渠道）| FastAPI 应用层；mini 走 `langgraph dev` 或基于 `runtime_lifespan` 自搭（见 §3 薄 Gateway demo）|
| **连带持久化** | `persistence/channel_connections/` / `feedback/` / `user/` | Gateway 专属 |
| **连带 config** | `channel_connections_config` / `suggestions_config` / `agents_api_config` | Gateway 专属 |
| **DB 迁移** | `persistence/migrations/`（alembic）| mini 走 `create_all`（教学简化）|
| **IM channel owner-scoping** | #3729（sandbox `acquire(*, user_id)`）/ #3579（uploads `user_id` kwarg）/ #3294（mcp auth interceptor）/ #2676（task_tool 用户上下文）| Gateway auth 概念；mini 经 `get_effective_user_id()` 已正确按用户隔离 |
| **顶层文件** | `Dockerfile` / `debug.py` / `sitecustomize.py` / `ruff.toml` | 部署/调试；mini 用 pyproject 内 ruff |

---

## 5. 总判断

| 维度 | 判断 |
|------|------|
| 代码完整度（vs 上游 harness）| ✅ 完整（对齐 `7a6c4a99`，Gateway 层按设计不 port）|
| 单测覆盖 | ✅ 1711 条 hermetic |
| **能否当 agent 用** | ⚠️ **代码齐全但端到端未验证——需先做 §1 冒烟才能下结论** |
| 扩展增量 | ⬜ 全部可选（§3），按「你拿 mini 干什么」触发，非必要不做 |
| 下一步 | **§1 端到端冒烟**（最高优先，是全部对齐工作的最终验收）；冒烟绿后再按需挑 §3 |

> 一句话：**对齐上游的工程工作做完了；「能不能当 agent 跑」还要一次真实 `langgraph dev` 对话来证明（§1）。** 扩展增量（§3）是「拿 mini 干什么」驱动的可选项，不是完成度的一部分——建议先把基座跑通再挑。

---

## 冒烟修复记录（§1 执行时滚动追加）

> 每次 §1 冒烟发现一个集成层问题，按三交付修后在此记一行：日期 / 现象 / 根因 / 修复（代码 + 测试 + 文档）/ 基线变化。

（尚无记录——§1 未开始）
