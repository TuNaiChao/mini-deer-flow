# mini-deer-flow 学习文档

> **零基础从这里开始 → [start-here.md](start-here.md)（#0）**：用一页纸讲清 LLM / agent / LangGraph / harness 是什么，并**手把手带你把 mini 在自己电脑上跑起来、发出第一条消息看到回复**。
>
> 这份索引按「依赖顺序」列出全部模块的学习文档（#0–#28）。每篇都**面向小白**，每个名词第一次出现都会解释；每篇标题已标它在顺序里的位置（如 `# 9. run_event_store.md — ...`），单独打开也能知道排第几。
>
> **阅读策略**：先 [start-here.md](start-here.md) 跑通 → 想看全貌先扫 [architecture.md](architecture.md)（#28，当「地图」先看无妨）→ 再按 #1→#28 逐篇钻。**面试前过一遍本页末尾的「[面试概念地图](#面试概念地图agent-岗位常考点--mini-哪篇)」**。

---

## Phase 导论 — 零基础起点

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 0 | [start-here.md](start-here.md) | 零基础从这里开始——概念一页纸（LLM / agent / LangGraph / harness）+ 手把手把 mini 跑起来发第一条消息 + 全景图 + 第一跑坑 |

---

## Phase 0 — 地基（先读，让「能跑」成立）

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 1 | [build.md](build.md) | 工程化地基（依赖 / 测试 / lint / dev server + 阻塞 IO gate + harness 边界）——跳过它后面全卡环境 |
| 2 | [testing-setup.md](testing-setup.md) | 测试怎么跑通 + 怎么写 hermetic 测试（Python 3.14 site.py 踩坑深度诊断 + 不连网/不调真实模型的约定） |
| 3 | [config.md](config.md) | 配置系统（19 个强类型子配置 + mtime 热重载 + startup-only 边界 + `$VAR` 展开）——几乎所有模块都读它 |
| 4 | [utils.md](utils.md) | 公共工具（时间戳归一 + 消息文本抽取 + 端口分配 + HTML 可读性提取） |
| 5 | [user_context.md](user_context.md) | 用户上下文（ContextVar 三态 user_id，用户隔离的基石） |

## Phase 1 — 模型 + 运行时基础

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 6 | [models.md](models.md) | 模型工厂（配置驱动 + 反射加载；thinking 四路径关闭 / stream 超时放宽 / stream_usage 默认开 / attach_tracing 双调用方；vLLM 推理模型 VllmChatModel 保 reasoning 字段） |
| 7 | [persistence.md](persistence.md) | 应用持久化层（SQLAlchemy ORM + WAL 三件套并发；app 表与 checkpointer 物理分离；memory no-op；三态 user_id 隔离；rowcount recovery + 幂等 put；token_usage_by_model 按真计费模型归桶；跨方言 JSON 过滤防注入） |
| 8 | [checkpointer.md](checkpointer.md) | 检查点工厂（委托 LangGraph Saver 不自建；三级优先级 legacy>database>默认；async cm / sync 单例；sqlite 父目录保护 + 阻塞 IO 卸载 + 缺包可操作提示 + postgres 连接池 keepalive；与 persistence 共用 .db 但表分离） |
| 9 | [run_event_store.md](run_event_store.md) | 运行事件存储（消息+轨迹+lifecycle 同接口按 category 分；seq 单调[memory计数器/jsonl每线程锁/db FOR UPDATE或advisory + UNIQUE 兜底]；双向游标分页；路径穿越防御；db trace 截断 + JSON 往返 + UUID→str stamp；memory 4 组投影） |
| 10 | [run_journal.md](run_journal.md) | RunJournal（RunEventStore 写入侧：LangChain 回调采集 + token 核算[run_id 去重防双计 + 按 caller/模型分桶]；同步回调→异步刷盘桥接[loop.create_task + 失败回插]；不实现 on_llm_new_token；progress 注入无循环依赖） |
| 11 | [stream_bridge.md](stream_bridge.md) | 流桥（worker↔SSE 解耦：每 run asyncio.Condition + 事件日志[非单消费 Queue，支持多消费者回放] + 有界窗口 queue_maxsize=256 淘汰最旧 + {ts_ms}-{seq} 内嵌 offset → O(1) 重连定位+id 核验 + END/心跳双哨兵[is 判等] + 心跳防代理掐断；仅内存实现，redis NotImplementedError） |
| 12 | [serialization.md](serialization.md) | 序列化与消息转换（对象出进程的单一真相源：递归兜底链[8 档永不抛] + 剥 `__pregel_*`[保留 `__interrupt__` 给 SDK 识别中断] + Interrupt→{value,id} 规范化 + 剥 hide_from_ui 的 base64 图片块[三条件精确剥]；converters 鸭子类型 LangChain→OpenAI 线协议[arguments 转字符串/空 content 设 null]）——**🎉 Phase 1 收官** |

## Phase 2 — 沙箱 / 子代理 / 追踪

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 13 | [sandbox.md](sandbox.md) | 沙箱（虚拟路径 `/mnt/user-data` 翻译 + 7 工具[bash/ls/glob/grep/read_file/write_file/str_replace] + Sandbox ABC 8 方法 + provider 单例[加锁 4 位点/回调锁外/per-thread LRU 256] + 两层路径防御[belt-and-suspenders] + 反解析防泄露 + write_file 80KB 上限 + 同路径写串行化[WeakValueDictionary] + SandboxAuditMiddleware 三档[block/warn/pass+heredoc fail-closed]；本地模式非安全边界 → 引出 #14 AIO 容器隔离。**vs 上游 deer-flow 源码**：核心完全一致[7 工具/8 ABC/24 忽略模式/6 异常]，差异全是砍 ACP workspace + MCP allowed-paths 注入 + 简化 bash 校验/max_results 配置的教学子集）——**Phase 2 开篇** |
| 14 | [aio_sandbox.md](aio_sandbox.md) | AIO 沙箱[本地模式非安全边界的真正解药]（三层：provider 管「谁在用」[四级缓存：进程内→暖池→文件锁内 discover→create] + backend 管「怎么起」[LocalContainerBackend Docker/Apple CLI 端口重试+名冲突发现+2-subprocess 枚举 / RemoteSandboxBackend K8s provisioner 薄 HTTP] + AioSandbox 管「怎么操作」[命令 self._lock 串行+ErrorObservation 新 session 重试[建/拆] / close 沿属性链关 httpx / download_file 显式防穿越+100MB]；确定性 sandbox_id=sha256(thread_id)[:8]+fcntl.flock 跨进程发现；暖池免冷启动；replicas 软上限淘汰暖池最老；idle 回收销毁前锁内再验；启动无条件收养孤儿；SIGTERM/SIGINT/SIGHUP+atexit 优雅关闭；soft-load agent_sandbox；与 Local 同 8 抽象方法接口，切 provider 只改 config。**vs 上游 deer-flow 源码**：核心护栏[四层缓存/flock/reconcile/shutdown/ErrorObservation/close]上游全有=忠实移植，差异仅 user_id 下沉路径层 + mini 加 needs_upload_permission_adjustment/reset 等小 helper） |
| 15 | [subagents.md](subagents.md) | 子代理（委派 + 单 `_scheduler_pool`(3) + 持久隔离事件循环[复用共享 async client] + 6 状态机[try_set_terminal 原子终态] + 协作取消[astream 迭代边界查 cancel_event] + checkpointer=False 一次性 + 5 终态契约[status_contract+共享 fixture 替代字符串前缀匹配] + 内置/自定义/per-agent 三层合并[全局 timeout/max_turns 只覆盖内置] + bash 按 host-bash 隐藏 + token 按 caller+模型归桶回灌父 RunJournal[#3658 全链已实现] + 流式 AI 消息 O(1) 去重[#3687] + system_prompt 合成单条。**vs 上游 deer-flow 源码**：忠实移植——单池+隔离循环/6 状态/协作取消/token 归桶/system_prompt 单条/150·60 max_turns 上游全有，registry+status_contract 0 行差，差异仅 mini 加 `_resolve_subagent_runtime_middlewares` 等 helper + task_tool 细节） |
| 16 | [tracing.md](tracing.md) | 链路追踪（两个正交 helper：`build_tracing_callbacks`[构造 LangChainTracer/Langfuse CallbackHandler，未配置返回 [] 零开销，失败响亮报错] + `inject_langfuse_metadata`[thread_id→session/user_id→user/assistant_id→trace_name/env+model→tags，setdefault 调用方优先]；图内 `attach_tracing=False` 不变量[图根统一注入防重复 span + 元数据剥离]；**4 调用点矩阵**：lead agent 造回调 / run worker 注元数据 / 子代理两者都做[#3611 归属父 thread] / 独立调用方模型级兜底；models 懒导入；env 驱动非 pydantic TracingConfig；langfuse soft-load。**vs 上游 deer-flow 源码**：忠实移植——两 helper/provider/元数据映射/图内不变量全一致[metadata.py 0 行差、factory.py 仅注释翻译]，差异仅 mini 不 port 嵌入式 client.py[上游第 5 注入点，Gateway 层]） |
| 17 | [agents_config.md](agents_config.md) | 自定义 agent[配置加载层]（SOUL.md 人格 → prompt `{soul}` 段注入 + config.yaml 能力[AgentConfig: name/description/model/tool_groups/skills，skills 三态 None/[]/白名单]；per-user 隔离 `users/{uid}/agents/{name}/` + legacy `agents/{name}/` 只读回退；AGENT_NAME_PATTERN `^[A-Za-z0-9-]+$` fullmatch 严格校验[防穿越/注入，setup/update_agent+memory+client 共用]；磁盘目录 `.lower()` 归一[防 APFS 大小写碰撞]；resolve_agent_dir #3390 要 config.yaml 才认[防 memory 残缺目录误读空配置]；load_agent_config 剥未知字段向前兼容；list_custom_agents 并集+per-user 覆盖 legacy；配置层与 lead_agent 运行时层分离。**vs 上游 deer-flow 源码**：agents_config.py 忠实移植[0 逻辑差，仅多 `from __future__`]，差异在共享 paths.py——砍 ACP workspace/host_sandbox/user_id sanitize 路径方法 + 加项目发现类方法；另不 port agents_api_config.py[Gateway REST 层]）——**Phase 2 收官** |

## Phase 3 — 记忆

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 18 | [memory.md](memory.md) | 记忆系统（跨会话记住用户：LLM 抽取对话事实 + 去抖队列合并 + per-user/agent 原子存储 + token 预算注入；同步 LLM 路径避跨循环连接复用 + ID-swap 保前缀缓存 + guaranteed 保底注入纠正类 fact） |

## Phase 4 — 技能

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 19 | [skills.md](skills.md) | 技能系统（SKILL.md 协议 + 发现/按需激活/安全安装 + allowed-tools 白名单收紧 + slash 严格语法 + LLM 安全审查 + 路径穿越防御 + 后台刷新缓存） |

## Phase 5 — MCP 集成 + 联网

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 20 | [mcp.md](mcp.md) | MCP 集成（外部工具协议 / stdio·sse·http 三传输 + owner-task 有状态会话池[仅 stdio] + OAuth skew 刷新 + mtime 缓存失效 + soft-load） |
| 21 | [community.md](community.md) | 联网能力（搜索/抓取 provider 框架 / 12 provider[ddg·tavily·jina 核心 + 9 全量/占位] + `tools[].use:` 加载 + 结果归一 + 4KB 截断 + CJK region 推断 + readability 软加载兜底 + SDK 软加载） |
| 22 | [tools.md](tools.md) | 工具系统（9 内置工具 + 五类来源[config/builtin/MCP/ACP/community] + `get_available_tools` 组装 + 按 name 去重[config>builtins>MCP>ACP，防重名 schema 模糊] + host-bash 过滤 + 条件加载 + sync 包装 + tool_search 延迟装配[fail-closed] + MCP 标记） |

## Phase 5.5 — 上传

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 23 | [uploads.md](uploads.md) | 文件上传（路径安全两道防线[normalize_filename + validate_path_traversal] + symlink 防御[O_NOFOLLOW 防沙箱逃逸] + markitdown/pymupdf4llm soft-load + PDF 双策略 + 事件循环内复用 worker + per-user per-thread 隔离 + 伴随 .md 清理） |

## Phase 6 — 中间件

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 24 | [middlewares.md](middlewares.md) | 中间件链（agent 的行为骨架：26 槽位实装 25[跳 Guardrail] + AgentMiddleware 钩子[洋葱 wrap_* vs 队列 before/after_*] + 顺序契约[InputSanitization 最外层 / ThreadData→Sandbox / Clarification 末位 / Safety·TokenBudget 在 Loop 后] + config 驱动 gating + GraphBubbleUp 透传 + 延迟注入保 AIMessage→ToolMessage 配对 + 关键中间件详解[InputSanitization 防注入 / ToolOutputBudget 防爆 / Dangling 补悬空 / LLM 熔断重试 / 双层循环检测 / 已见账本预算 / 安全终止拦截 / Clarification 中断]） |

## Phase 7 — Agent 装配

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 25 | [agents.md](agents.md) | Agent 装配（SDK + config 双入口[create_deerflow_agent features 驱动 / make_lead_agent config 驱动] + RuntimeFeatures 声明式 flag[True/False/实例三态] + @Next/@Prev 锚点定位 + thread_state 类型化 reducer[fail-closed merge_sandbox / merge_promoted catalog_hash scope] + tracing 图根注入[配套 attach_tracing=False] + 工具策略过滤 + 延迟装配 fail-closed + bootstrap/custom/默认三分支 + 条件段 prompt 保 prefix-cache） |

## Phase 8 — 运行管理 + 集成装配

| # | 文档 | 一句话定位 |
|---|------|-----------|
| 26 | [runs.md](runs.md) | 运行管理（RunManager 状态机 + run_agent 后台执行 + RunRecord/RunStatus 生命周期 + 并发模型[asyncio 锁+线程索引/create_or_reject 原子消除 TOCTOU/busy 重试不藏永久失败/orphan 恢复/shutdown drain 只标未 settle/幂等 cancel] + worker[runtime/journal 注入/rollback 深拷贝 pending_writes 快照/abort interrupt+rollback/LLM 兜底扫 chunk] + RunContext+RunStore + token 按真计费模型归桶） |
| 27 | [runtime_store.md](runtime_store.md) | LangGraph Store 工厂（与 checkpointer 平行的跨线程记忆；Store vs checkpointer 对比 + 三入口[make_store 异步/get_store 同步双检锁单例/store_context 同步一次性] + 后端镜像 checkpointer[memory/sqlite/postgres + soft-load 带安装提示] + worker 怎么用 + None→InMemoryStore 开箱即用 + 共用 _sqlite_utils） |
| 28 | [architecture.md](architecture.md) | 集成装配总览（runtime_lifespan 把所有单件串成 RuntimeBundle + 装配/drain 顺序[先 drain 在途 run 再关 checkpointer，防 PoolClosed] + AsyncExitStack 并行起·LIFO 关 + 一次请求完整路径 + 后端选择矩阵 + 全模块关系。**Phase 0–8 文档完结篇**） |

---

**🎉 主线全部完成**：Phase 0–8（#0 导论 + #1–#28）全部 ✅，按新标准（11 节模板 + §9 设计动机 + §10
实现差异 vs 上游源码 + §1 小白名词 + learning outcomes）重写。从 [start-here.md](start-here.md)
（零基础起点）→ [build.md](build.md)（怎么跑起来）一路读到 [architecture.md](architecture.md)
（怎么拼成系统），覆盖地基 → 模型/运行时 → 沙箱/子代理/追踪 → 记忆 → 技能 → MCP/联网 → 工具 →
上传 → 中间件 → agent 装配 → 运行管理 → Store → 集成。仅剩 Guardrail 与 DeerFlowClient 两个真正可选
模块（依赖独立外部包，按需）。

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

## 面试概念地图（agent 岗位常考点 → mini 哪篇）

> 面试前过这张表：左列是 agent / LLM 工程岗位的高频考点，中列是 mini 里讲透它的文档。每个考点都能在对应文档的 **§9 设计动机分析** + **§10 实现差异（vs 上游源码）** 里找到「为什么这么设计 / 不这么设计会怎样」的深度回答——这才是「够面试」的关键。末尾「mini 不覆盖」一节诚实标注了边界，别装懂。

### A. LangGraph 与状态图

| 考点 | mini 文档 |
|------|----------|
| 状态图 / checkpoint / reducer 合并协议 | [checkpointer.md](checkpointer.md)（#8）· [agents.md](agents.md)（#25，ThreadState 5 个 reducer）· [runs.md](runs.md)（#26，rollback） |
| 中断 / human-in-the-loop / `Command(goto=END)` | [middlewares.md](middlewares.md)（#24，Clarification）· [runs.md](runs.md)（#26） |
| 子图 / 子代理委派 / 并发 / 协作取消 | [subagents.md](subagents.md)（#15） |

### B. Agent 装配与运行管理

| 考点 | mini 文档 |
|------|----------|
| agent 装配（模型+工具+中间件+提示词四件套） | [agents.md](agents.md)（#25，双入口） |
| 中间件 / 钩子 / 洋葱模型 / 顺序契约 | [middlewares.md](middlewares.md)（#24，26 槽位链） |
| run 状态机 / 并发安全 / 取消 / 关停 drain | [runs.md](runs.md)（#26，RunManager + worker） |
| 集成装配 / lifespan / 依赖注入 / drain 顺序 | [architecture.md](architecture.md)（#28） |

### C. 工具与扩展性

| 考点 | mini 文档 |
|------|----------|
| 工具系统 / 五来源 / name 去重 / 延迟装配 | [tools.md](tools.md)（#22） |
| MCP 协议 / 三传输 / 有状态会话池 / OAuth | [mcp.md](mcp.md)（#20） |
| 技能系统 / SKILL.md 协议 / 五层安全安装 | [skills.md](skills.md)（#19） |
| 文件上传 / 路径安全 / 文档转换 | [uploads.md](uploads.md)（#23） |
| 自定义 agent / 配置驱动 / per-user 隔离 | [agents_config.md](agents_config.md)（#17）· [agents.md](agents.md)（#25） |

### D. 安全

| 考点 | mini 文档 |
|------|----------|
| 沙箱隔离 / 容器 / 虚拟路径翻译 | [sandbox.md](sandbox.md)（#13）· [aio_sandbox.md](aio_sandbox.md)（#14） |
| 提示词注入防御（保留标签转义） | [middlewares.md](middlewares.md)（#24，InputSanitization） |
| 路径穿越 / symlink / TOCTOU 防御 | [uploads.md](uploads.md)（#23）· [sandbox.md](sandbox.md)（#13）· [skills.md](skills.md)（#19） |
| 沙箱命令审计 / fail-closed / heredoc 守卫 | [middlewares.md](middlewares.md)（#24，SandboxAudit）· [sandbox.md](sandbox.md)（#13） |

### E. 可观测性

| 考点 | mini 文档 |
|------|----------|
| 链路追踪（LangSmith / Langfuse / 图根注入 + propagate） | [tracing.md](tracing.md)（#16） |
| 事件存储 / 消息+轨迹 / 双向游标分页 | [run_event_store.md](run_event_store.md)（#9） |
| journal / token 核算 / 回调采集 / 跨循环刷盘 | [run_journal.md](run_journal.md)（#10） |
| 流式 / SSE / 断线续播 / O(1) 重连定位 | [stream_bridge.md](stream_bridge.md)（#11） |

### F. 性能与可靠性

| 考点 | mini 文档 |
|------|----------|
| 熔断 / 重试 / 错误分类 / 指数退避 | [middlewares.md](middlewares.md)（#24，LLMErrorHandling）· [runs.md](runs.md)（#26，SQLite busy 重试） |
| 循环检测（哈希层 + 频率层双层） | [middlewares.md](middlewares.md)（#24，LoopDetection） |
| token 预算 / 按真计费模型归桶 / 已见账本 | [middlewares.md](middlewares.md)（#24，TokenBudget）· [runs.md](runs.md)（#26） |
| 前缀缓存 / 系统提示静态化 / ID-swap 注入 | [agents.md](agents.md)（#25，DynamicContext）· [middlewares.md](middlewares.md)（#24） |
| 序列化 / 出进程 8 档兜底链 / 剥 pregel | [serialization.md](serialization.md)（#12） |
| 阻塞 IO / 事件循环 / `asyncio.to_thread` | [build.md](build.md)（#1，blocking-IO gate）· 贯穿各篇 |

### G. 数据层

| 考点 | mini 文档 |
|------|----------|
| 配置系统 / 19 个强类型子配置 / mtime 热重载边界 | [config.md](config.md)（#3） |
| 持久化 / SQLAlchemy ORM / WAL 三件套 / 跨方言 JSON | [persistence.md](persistence.md)（#7） |
| Store（跨线程记忆）vs checkpointer / 三入口 / 双检锁 | [runtime_store.md](runtime_store.md)（#27）· [checkpointer.md](checkpointer.md)（#8） |
| 用户隔离 / ContextVar 三态 / 跨真线程坑 | [user_context.md](user_context.md)（#5） |
| 记忆系统（跨会话事实抽取 + 去抖 + ID-swap 注入） | [memory.md](memory.md)（#18） |
| 模型工厂 / 反射加载 / thinking 四路径 / vLLM | [models.md](models.md)（#6） |

### H. 工程化

| 考点 | mini 文档 |
|------|----------|
| 构建系统 / Makefile / ruff / 依赖管理 | [build.md](build.md)（#1） |
| 测试工程 / hermetic 约定 / Python 3.14 .pth 坑 | [testing-setup.md](testing-setup.md)（#2） |
| 公共工具 / 时间戳归一 / 端口分配 / readability | [utils.md](utils.md)（#4） |

### 诚实标注：mini 不覆盖的考点（面试时别说做过）

| 考点 | 状态 | 说明 |
|------|------|------|
| Gateway / REST API / FastAPI 服务层 | mini 无 | 上游 `backend/app/gateway/`，mini 不 port；见 [architecture.md §10](architecture.md)（#28） |
| IM 渠道集成（飞书 / Slack / Telegram / Discord / DingTalk） | mini 无 | 上游 `backend/app/channels/`，mini 不 port |
| Guardrail（工具调用前授权 / policy provider） | 不实装 | 依赖独立 `guardrails` 包；见 [middlewares.md §10.1](middlewares.md)（#24） |
| DeerFlowClient（嵌入式 in-process client） | 可选 | 上游 `client.py`，mini 列为「真正可选」 |
| 部署 / Docker / nginx / provisioner / K8s | mini 无 | 上游 `docker/` + `scripts/deploy.sh`，mini 不 port |
| Alembic 数据库迁移 | mini 无 | 上游有完整 migrations，mini 开发期删 `.db` 让 `create_all` 重建；见 [persistence.md §9](persistence.md)（#7） |
| 终端 UI（TUI / textual） | mini 无 | 上游 `tui/`，可选依赖 |

> **怎么用**：面试官问「你做过 X 吗」——先在心里查这张表。在 mini 覆盖范围内的，去对应文档的 §9/§10 把「为什么这么设计」讲出来（不是背机制，是讲权衡）；不在覆盖范围内的，诚实说「这块我没深入，但我知道它是 Y」——比硬装懂强。

---

## 其它文档（非教学，按需查）

- [todo.md](todo.md) — **项目待办 + 工作进度日志**（代码侧待做 / 下次开工看这）

> 分工：**查项目待做 / 工作进度** → [todo.md](todo.md)；**学某个模块** → 上面 #0–#28；**面试复习** → 上面「面试概念地图」。
