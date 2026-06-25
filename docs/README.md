# mini-deer-flow 学习文档

> mini-deer-flow 是 deer-flow 的**教学化对齐版**：用更小的代码量、更细的讲解重写 deer-flow，但**行为上全面对标、不裁剪核心功能**（v1.2 起：沙箱 / subagent / memory / tool / skill / mcp / 联网 / 上传 / 自定义 agent 全部纳入主线，见 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) v1.2 修订日志）。这份索引按「依赖顺序」列出**已完成**模块的学习文档——从「怎么把代码跑起来」一路读到「运行时基础全部就位」。每篇都**面向小白**，每个名词第一次出现都会解释。
>
> 按 **1 → 28** 顺序读最省事。每篇开头标题已标注它在顺序里的位置（如 `# 9. run_event_store.md — ...`），单独打开某篇也能知道它排第几。

---

## Phase 0 — 地基（先读，让「能跑」成立）

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 1 | [build.md](build.md) | 工程化基础设施——让「写完代码能跑测试、能 lint」成立（跳过它后面全卡环境） |
| 2 | [testing-setup.md](testing-setup.md) | 测试怎么跑通（Python 3.14 site.py 踩坑 + hermetic 约定） |
| 3 | [config.md](config.md) | 配置系统（类型化 + 热重载）——几乎所有模块都读它 |
| 4 | [utils.md](utils.md) | 公共工具（时间归一 + 消息文本抽取） |
| 5 | [user_context.md](user_context.md) | 用户上下文（三态 user_id）——用户隔离的基石 |

## Phase 1 — 模型 + 运行时基础

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 6 | [models.md](models.md) | 模型工厂（thinking / tracing 能力门控 + stream 超时放宽） |
| 7 | [persistence.md](persistence.md) | 应用持久化层（SQLAlchemy ORM + WAL 并发） |
| 8 | [checkpointer.md](checkpointer.md) | 检查点工厂（委托 LangGraph Saver，不自建） |
| 9 | [run_event_store.md](run_event_store.md) | 运行事件存储（消息 + 轨迹，seq 单调 + 路径穿越防御） |
| 10 | [run_journal.md](run_journal.md) | RunJournal（LangChain 回调 → 事件采集 + token 核算） |
| 11 | [stream_bridge.md](stream_bridge.md) | 流桥（SSE 生产者-消费者解耦 + 重连补播） |
| 12 | [serialization.md](serialization.md) | 序列化与消息转换（LangChain/LangGraph → JSON 单一真相源） |

## Phase 2 — 沙箱 / 子代理 / 追踪

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 13 | [sandbox.md](sandbox.md) | 沙箱（虚拟路径 `/mnt/user-data` + 7 工具 + provider + 命令审计；本地模式非安全边界 → 引出 M10b AIO） |
| 14 | [aio_sandbox.md](aio_sandbox.md) | AIO 沙箱（Docker/K8s 容器隔离 + 暖池 + 跨进程文件锁发现 + idle 回收 + 优雅关闭；soft-load `agent_sandbox`） |
| 15 | [subagents.md](subagents.md) | 子代理（委派 + 单 scheduler pool + 持久隔离事件循环[非双池] + 5 状态契约 + 自定义/per-agent 覆盖 + token 回灌） |
| 16 | [tracing.md](tracing.md) | 链路追踪（LangSmith/Langfuse 图根注入 + Langfuse 元数据映射 + models attach_tracing 懒导入联动；未配置零开销） |
| 17 | [agents_config.md](agents_config.md) | 自定义 agent（SOUL.md 人格 + config.yaml 能力白名单 + per-user 隔离 + legacy 只读回退 + AGENT_NAME_PATTERN 校验 + 名称小写归一 + #3390 防御） |

## Phase 3 — 记忆

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 18 | [memory.md](memory.md) | 记忆系统（LLM 抽取 + 去抖队列 + per-user/agent 原子存储 + 同步 LLM 路径防 #2615 + ID-swap 注入 + tiktoken 冷却降级 + 上传剔除） |

## Phase 4 — 技能

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 19 | [skills.md](skills.md) | 技能系统（SKILL.md 协议 + 发现/按需激活/安全安装 + allowed-tools 白名单收紧 + slash 严格语法 + LLM 安全审查 + 路径穿越防御 + 后台刷新缓存） |

## Phase 5 — MCP 集成 + 联网

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 20 | [mcp.md](mcp.md) | MCP 集成（外部工具协议 / stdio·sse·http 三传输 + owner-task 有状态会话池[仅 stdio] + OAuth skew 刷新 + mtime 缓存失效 + soft-load） |
| 21 | [community.md](community.md) | 联网能力（搜索/抓取 provider 框架 / 12 provider[ddg·tavily·jina 核心 + 9 全量/占位] + `tools[].use:` 加载 + 结果归一 + 4KB 截断 + CJK region 推断 + readability 软加载兜底 + SDK 软加载） |
| 22 | [tools.md](tools.md) | 工具系统（9 内置工具 + 五类来源[config/builtin/MCP/ACP/community] + `get_available_tools` 组装 + 按 name 去重[config>builtins>MCP>ACP，防 #1803] + host-bash 过滤 + 条件加载 + sync 包装 + tool_search 延迟装配[fail-closed] + MCP 标记） |

## Phase 5.5 — 上传

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 23 | [uploads.md](uploads.md) | 文件上传（路径安全两道防线[normalize_filename + validate_path_traversal] + symlink 防御[O_NOFOLLOW 防沙箱逃逸] + markitdown/pymupdf4llm soft-load + PDF 双策略 + 事件循环内复用 worker + per-user per-thread 隔离 + 伴随 .md 清理） |

## Phase 6 — 中间件

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 24 | [middlewares.md](middlewares.md) | 中间件链（23 步生产链 + AgentMiddleware 钩子机制 + 顺序契约[ThreadData→Uploads→Sandbox / Clarification 末位 / Safety 在 Loop 后] + config 驱动 gating + GraphBubbleUp 透传 + 关键中间件详解[ToolOutputBudget 防爆 / Dangling 补悬空 / LLM 熔断重试 / 循环双层检测 / 安全终止拦截 / 延迟工具过滤 / 子代理限流 / 摘要前抢拍]） |

## Phase 7 — Agent 装配

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 25 | [agents.md](agents.md) | Agent 装配（SDK + config 双入口[create_deerflow_agent features 驱动 / make_lead_agent config 驱动] + RuntimeFeatures 声明式 flag + @Next/@Prev 锚点定位 + thread_state 类型化 reducer[fail-closed merge_sandbox 红线 #16 / merge_promoted catalog_hash scope] + tracing 图根注入红线 #17 + 工具策略过滤 + 延迟装配 fail-closed + bootstrap/custom/默认三分支 + 条件段 prompt 保 prefix-cache） |

## Phase 8 — 运行管理 + 集成装配

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 26 | [runs.md](runs.md) | 运行管理（RunManager 状态机 + run_agent 后台执行 + RunRecord/RunStatus 生命周期 + 并发模型[asyncio 锁+线程索引/create_or_reject 原子消除 TOCTOU/busy 重试红线 #2/orphan 恢复红线 #7/shutdown drain 红线 #6/幂等 cancel] + worker[runtime/journal 注入/rollback 快照红线 #5/abort interrupt+rollback/LLM 兜底扫 chunk] + RunContext+RunStore） |
| 27 | [runtime_store.md](runtime_store.md) | LangGraph Store 工厂（与 checkpointer 平行的跨线程记忆；Store vs checkpointer 对比 + 三入口[make_store 异步/get_store 同步单例/store_context 同步一次性] + 后端镜像 checkpointer[memory/sqlite/postgres + soft-load] + worker 怎么用 + None→InMemoryStore 红线 #25） |
| 28 | [architecture.md](architecture.md) | 集成装配总览（runtime_lifespan 把所有单件串成 RuntimeBundle + 装配/drain 顺序[红线 #6 先 drain 再关 checkpointer] + 一次请求完整路径 + 后端选择矩阵 + Part D 集成清单对照 + 全模块关系。**Phase 0–8 文档完结篇**） |

---

**🎉 主线全部完成**：Phase 0–8（M-build + M0–M19 + M10b/M20–M23）全部 ✅，对齐 deer-flow v1.2
「全面对标、不裁剪核心功能」。从 [build.md](build.md)（怎么跑起来）一路读到
[architecture.md](architecture.md)（怎么拼成系统）。仅剩 Guardrail / DeerFlowClient 两个真正可选模块
（依赖独立外部包，按需）。

---

## 为什么是这个顺序（依赖链）

地基先于业务：`build`（能跑）→ `config`（配置源）→ `utils`（时间/消息工具）→ `user_context`（用户隔离）。

然后是存储与运行时（每层依赖前一层）：

```
models ──┐
         ├─→ persistence（ORM 地基）
         │        ├─→ checkpointer（依赖 persistence 的 sqlite_utils）
         │        └─→ run_event_store（依赖 persistence 的 RunEventRow）
         │                  ↑
         │         run_journal（写入侧采集器，依赖 run_event_store）
         │
         ├─→ stream_bridge（独立，仅依赖 config）
         └─→ serialization（纯函数，放最后——理解「为什么剥 pregel / image」）
```

记三句话就够：

1. **persistence 是存储地基**——checkpointer / run_event_store 都建在它上面。
2. **run_event_store 是存储侧**，**run_journal 是它的写入侧**（采集器）；一个管「记到哪」，一个管「记什么、token 怎么算」。
3. **stream_bridge 和 serialization 相对独立**，但理解它们要先知道消息 / 状态长什么样，所以放后面。

---

## 其它文档（非教学，按需查）

- [todo.md](todo.md) — **进度看板**（做到哪了 / 下次开工什么）
- [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) — **设计规格**（每个模块要做成什么样：文件清单 / 依赖 / 红线）
- [spec-M4-persistence.md](spec-M4-persistence.md) — M4 persistence 详细规格
- [legacy/](legacy/) — 旧版 / 待重写的文档归档（`tools.md` / `中间件.md` 等）

> 三者分工：**查进度** → [todo.md](todo.md)；**查设计规格** → [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md)；**学某个模块** → 上面 1–22。
