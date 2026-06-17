# todo.md — mini-deer-flow 实现进度

> 这份文档**只跟踪「做到哪了」**。每个模块要做成什么样（文件清单 / 依赖 / 可靠性要点 / 红线）查 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) 的 **Part C**（M-build / M0–M19 各小节）。
>
> **三交付** = 代码 + hermetic 测试（`test/test_<module>.py`）+ 学习文档（`docs/<module>.md`）。三齐 + 测试绿才算一个模块完成。
>
> **基线命令**：`cd backend && make test && make lint` → 当前 **358 passed, 1 skipped, 0 lint 错误**。

## 图例

- ✅ 完成 — 三交付齐、测试绿
- 🔶 部分 — 有代码/进度，但未达 outline 完整规格
- 📋 规格 — 设计文档写了，代码未落地
- ⬜ 未开始

---

## 总览

| 状态 | 数量 | 模块 |
|------|------|------|
| ✅ 完成 | 13 | M-build、M0 config、M1 utils、M2 reflection、M3 user_context、M-models、M4 persistence、M5 checkpointer、M6 events/store、M7 journal、M8 stream_bridge、M9 serialization、测试质量基线 |
| 🔶 部分 | 3 | M15 tools（仅 2 内置）、M16 middlewares（7/23）、M17 agent（精简版） |
| 📋 规格 | 0 | — |
| ⬜ 未开始 | 8 | M10–M12、M13 记忆（仅桩）、M14 skills、M18–M19 + 集成 |

**Phase 0 + Phase 1 全部完成**（build + config + utils + reflection + user_context + models + persistence + checkpointer + events/store + journal + stream_bridge + serialization）。**Phase 1 运行时地基就位**。**下次起点**：进入 **Phase 2**，从 **M10 sandbox** 起步。

---

## 模块进度总表（按 Phase，一眼看完全部）

| Phase | 模块 | 状态 | 代码 | 测试 | 文档 |
|-------|------|------|------|------|------|
| 0 | M-build 工程化 | ✅ | ✅ | ✅ | ✅ build.md + testing-setup.md |
| 0 | M0 config（配置加载） | ✅ | ✅ | ✅ | ✅ config.md |
| 0 | M1 utils | ✅ | ✅ | ✅ | ✅ utils.md |
| 0 | M2 reflection | ✅ | ✅（既有） | ✅ | —（对齐 deer） |
| 0 | M3 user_context | ✅ | ✅ | ✅ | ✅ user_context.md |
| 1 | M-models（模型） | ✅ | ✅ | ✅ | ✅ models.md |
| 1 | M4 persistence（持久化） | ✅ | ✅ | ✅ | ✅ persistence.md |
| 1 | M5 checkpointer | ✅ | ✅ | ✅ | ✅ checkpointer.md |
| 1 | M6 events/store | ✅ | ✅ | ✅ | ✅ run_event_store.md |
| 1 | M7 journal | ✅ | ✅ | ✅ | ✅ run_journal.md |
| 1 | M8 stream_bridge | ✅ | ✅ | ✅ | ✅ stream_bridge.md |
| 1 | M9 serialization | ✅ | ✅ | ✅ | ✅ serialization.md |
| 2 | M10 sandbox | ⬜ | — | — | — |
| 2 | M11 subagents | ⬜ | — | — | — |
| 2 | M12 tracing | ⬜ | — | — | — |
| 3 | M13 记忆 | ⬜ | 桩 | — | — |
| 4 | M14 skills | ⬜ | — | — | — |
| 5 | M15 tools | 🔶 | 部分 | 部分 | tools.md（待更新） |
| 6 | M16 middlewares | 🔶 | 部分（7/23） | 部分 | 中间件.md（待更新） |
| 7 | M17 agent | 🔶 | 部分（精简） | 部分 | — |
| 8 | M18 runs（运行管理） | ⬜ | — | — | — |
| 8 | M19 runtime/store | ⬜ | — | — | — |
| 8 | 集成装配 | ⬜ | — | — | — |

---

## ✅ 已完成（详情）

### M-build 工程化（Phase 0）
- **代码**：pyproject extras + dev 依赖(pytest/ruff) + Makefile + conftest（Python 3.14 sys.path 适配）+ 根 `pytest.ini`/`ruff.toml`（配置单源，覆盖 backend+test）
- **测试**：`test_harness_boundary.py`（harness 不得 import app.\*）+ blocking-io gate（**inline 实现，不引 blockbuster**）smoke
- **文档**：`docs/build.md` + `docs/testing-setup.md`
- **基线**：130 passed, 0 lint 错误

### M0 config（配置类型化，Phase 0）
- **代码**：17 个子配置 pydantic 化（database/memory/title/sandbox/loop_detection/summarization/checkpointer/run_events/stream_bridge/token_usage/tool_output/tool_search/safety_finish_reason/subagents/skills/skill_evolution + reload_boundary）+ `DatabaseConfig` 派生 sqlite_path/app_sqlalchemy_url + `paths.py` 补 resolve_path/runtime_home/get_paths（**替代 runtime_paths**）+ `extensions_config.is_skill_enabled` + `AppConfig` 字段类型化 + `config_version` + None→[] validator
- **测试**：`test_config.py`（子配置默认值、空配置可启动红线 #25、database 派生、reload_boundary、paths、is_skill_enabled、loop_detection validator）
- **文档**：`docs/config.md`
- **改动**：`build_middlewares` 的 `isinstance(x, dict)` 防御写法改为属性访问（`.enabled`）

### M1 utils（公共工具，Phase 0）
- **代码**：`utils/time.py`（`now_iso` / `coerce_iso`——归一 UTC ISO，兼容历史 unix 时间戳）+ `utils/messages.py`（`message_content_to_text` / `get_original_user_content_text`）
- **测试**：`test_utils.py`（23 个，now_iso 时区、coerce_iso 各分支、消息三态抽取）
- **文档**：`docs/utils.md`

### M3 user_context（用户上下文，Phase 0）
- **代码**：`runtime/user_context.py`（ContextVar 三态 AUTO/str/None + `resolve_runtime_user_id` 三优先级 runtime.context > contextvar > default + `DEFAULT_USER_ID` + UUID→str 边界）
- **测试**：`test_user_context.py`（23 个，三态解析、无 contextvar 回退、runtime 优先级、UUID 强转）
- **文档**：`docs/user_context.md`
- **配套**：conftest 的 `_auto_user_context` autouse fixture 已自动激活（M3 落地后每个测试注入默认用户）

### M-models 模型（Phase 1）
- **代码**：`models/factory.py`（thinking 四路径 + reasoning_effort 门控 + stream 默认值 + attach_tracing 懒加载）
- **测试**：`test_model.py`（hermetic，`_Recorder` + monkeypatch）
- **文档**：`docs/models.md`

### M4 persistence（持久化，Phase 1）
- **前置（runs 基类层）**：`runtime/runs/schemas.py`（`RunStatus`/`DisconnectMode` 枚举）+ `runtime/runs/store/base.py`（`RunStore` ABC）。把 ABC 提前到 Phase 1 以打破「持久化 → 运行管理」循环依赖。
- **代码**：`persistence/`（`base.Base(DeclarativeBase)` + `to_dict` + `engine`[init/close/get_session_factory，sqlite WAL 红线 #2 + memory no-op + auto-create + postgres auto-create-db] + `json_compat`[跨方言 JSON 过滤] + `models`[RunEventRow/RunRow] + `run/sql.RunRepository(RunStore)` + `thread_meta`[ThreadMetaStore ABC + MemoryThreadMetaStore 包 LangGraph BaseStore + ThreadMetaRepository + make_thread_store 工厂]）
- **测试**：`test_persistence.py`（52 个，hermetic 全走 tmp sqlite：WAL PRAGMA 生效 + WAL 持久化文件头、memory no-op、auto-create、RunRepository CRUD + user 隔离 + rowcount 红线 #12 + 幂等 put + UUID→str 红线 #10 + aggregate_tokens、ThreadMeta 增删查改 + metadata 合并 + json_match search + check_access 双模式 + InvalidMetadataFilterError、MemoryThreadMetaStore 等价 + coerce_iso、make_thread_store 工厂、json_compat 校验器、Base.to_dict/repr、runs 枚举）
- **文档**：`docs/persistence.md`
- **改动**：harness `pyproject.toml` 新增 core 依赖 `sqlalchemy[asyncio]` + `aiosqlite`（对齐 deer；postgres 驱动 asyncpg 仍为可选 extra）
- **裁剪**：feedback / user / channel_connections / migrations(Alembic) 本期不做

### M5 checkpointer（检查点工厂，Phase 1）
- **代码**：`runtime/store/_sqlite_utils.py`（`resolve_sqlite_conn_str` + `ensure_sqlite_parent_dir`，红线 #1912 父目录保护）+ `runtime/checkpointer/provider.py`（同步 `_sync_checkpointer_cm` + `get_checkpointer` 单例 + `reset_checkpointer` + `checkpointer_context` + 安装提示常量）+ `runtime/checkpointer/async_provider.py`（`make_checkpointer(app_config)` async cm，三级优先级 legacy `checkpointer:` > `database:`(非memory) > InMemorySaver；sqlite 路径 `asyncio.to_thread` 卸载红线 #1；postgres 连接池 keepalive）。**委托 LangGraph 内置 Saver 不自建**。
- **测试**：`test_checkpointer.py`（26 个，hermetic：`_sqlite_utils` 各分支、make_checkpointer memory 默认/显式/sqlite(legacy+database) 真实 aput→aget_tuple 往返/优先级/database-memory 回退/postgres 缺包提示/未知类型、to_thread 卸载契约、同步单例+reset+context、同步 sqlite setup+put+get_tuple 往返、父目录保护、缺包提示可操作性）
- **文档**：`docs/checkpointer.md`
- **改动**：harness `pyproject.toml` postgres extra 补 `psycopg-pool>=3.3`（deer 对齐）；backend `pyproject.toml` dev 组补 `langgraph-checkpoint-sqlite`（测试做真实 sqlite 往返用；生产 memory 模式不需，故放 dev 而非 core，红线 #24）

### M6 events/store（运行事件存储，Phase 1）
- **代码**：`runtime/events/store/base.py`（`RunEventStore` ABC，8 方法）+ `memory.py`（`MemoryRunEventStore` + message 投影 bisect 优化）+ `jsonl.py`（`JsonlRunEventStore`：`_SAFE_ID_PATTERN` 路径穿越防御红线 #4 + 每线程 `asyncio.Lock` 串行化红线 #3 + lazy seq 跨实例加载 + 全 IO `asyncio.to_thread` 红线 #1）+ `db.py`（`DbRunEventStore`：`SELECT max(seq) FOR UPDATE` / postgres `pg_advisory_xact_lock` 红线 #3 + trace 按 `max_trace_content` 截断 + JSON content 往返 + user_id stamp UUID→str 红线 #10）+ `__init__.make_run_event_store` 工厂（memory/jsonl/db；db engine 未就绪回退 memory）
- **测试**：`test_events.py`（38 个，hermetic：memory seq 单调/分页/投影一致性/delete 清计数器；jsonl 跨实例持久化/路径穿越拒绝/并发写锁/跨 run 统一 seq/delete 清计数器锁/IO 卸载；db FOR UPDATE 单调/trace 截断/JSON 往返/user_id stamp+UUID→str/用户隔离/双向游标分页/put_batch 跨 thread 拒绝/UNIQUE 约束兜底；工厂各后端 + 未知后端报错）
- **文档**：`docs/run_event_store.md`
- **踩坑修正**：db 并发同-thread 写测试——sqlite 上 `FOR UPDATE` 是 no-op，靠 `UNIQUE(thread_id, seq)` 约束兜底；生产靠 RunJournal `put_batch` 单事务避开并发，postgres 靠 advisory lock。测试改为验证约束兜底 + 不同 thread 并发 OK

### M9 serialization（序列化 + 转换，Phase 1，纯函数无依赖）
- **代码**：`runtime/serialization.py`（`serialize_lc_object` 递归序列化 + `model_dump`/`dict`/`str` 兜底链；`serialize_channel_values` 剥 `__pregel_*`/`__interrupt__` 内部键；`strip_data_url_image_blocks` 只剥 hide_from_ui 消息的 `data:` image_url 块、保顺序/数量；`serialize_channel_values_for_api` 组合；`serialize_messages_tuple`/`serialize(mode)`）+ `runtime/converters.py`（`langchain_to_openai_message` 鸭子类型转 OpenAI 格式 + tool_calls args JSON 序列化 + `langchain_to_openai_completion` + `_infer_finish_reason` + 批量）。**纯函数无依赖**，故能插队到 M7/M8 之前。
- **测试**：`test_serialization.py`（41 个，hermetic 无 IO：lc_object 标量/递归/pydantic/兜底；channel_values 剥 pregel+interrupt + 保留普通下划线键 + 递归；strip 只剥 hide_from_ui 的 data: 图片、保顺序数量、留 https/非隐藏；for_api 组合；messages_tuple；mode 分发；converters human/ai 文本/tool_calls/system/tool + list content + string args + 未知 role + finish_reason 推断 + completion 含/不含 usage）
- **文档**：`docs/serialization.md`
- **决策**：outline 标 converters「可后补先占位」——此处按 deer 参考完整移植（纯函数低风险），供后续端点直接用；当前未接入 RunJournal（它直接用 model_dump）

### M8 stream_bridge（流桥，Phase 1）
- **代码**：`runtime/stream_bridge/base.py`（`StreamEvent` frozen dataclass + `HEARTBEAT_SENTINEL`/`END_SENTINEL` + `StreamBridge` ABC + no-op `close`）+ `memory.py`（`MemoryStreamBridge`：每 run `_RunStream`(`events`+`asyncio.Condition`+`ended`+`start_offset`) + `queue_maxsize=256` 有界窗口 eviction 红线 #11 + id=`{ts_ms}-{seq}` + `_resolve_start_offset` Last-Event-ID 重连 + 落后从 start_offset 恢复 + 心跳防代理掐断）+ `async_provider.make_stream_bridge(app_config)` async cm（memory/redis；redis NotImplementedError）。
- **测试**：`test_stream_bridge.py`（22 个，hermetic 纯 asyncio：基础 publish/subscribe+END、id 格式单调、有界 evict（maxsize 淘汰+start_offset 前移）、Last-Event-ID 续播（首条/中间/过期回放最早）、落后恢复（洪水发布后从 start_offset 恢复+丢失不补）、心跳（idle 发心跳/有事件不挡）、迟到订阅者回放+续接、cleanup(+delay)/close、make_stream_bridge 工厂 memory/redis/未知类型、StreamEvent frozen + 哨兵 + ABC 不可实例化）
- **文档**：`docs/stream_bridge.md`
- **适配**：`async_provider` 用 `get_app_config().stream_bridge` 而非 deer 的 `get_stream_bridge_config()` 单例（M0 既定，省一层间接）
- **踩坑修正**：`memory.py._get_or_create_stream` 笔误 `not not in` → 改 `not in`

### M7 journal（RunJournal 事件采集，Phase 1 收尾）
- **代码**：`runtime/journal.py`（`RunJournal(BaseCallbackHandler)`，单文件 ~600 行）：回调 `on_chain_start/end/error`（根链发 run.start/end，嵌套忽略）、`on_chat_model_start`（抽首条 human + 跳过 summary + 存 llm_request）、`on_llm_end`（存 llm_response + token 按 run_id 去重防双计 + caller 分桶 lead/subagent/middleware + error fallback 检测 + total 从 input+output 补算）、`on_tool_end`（ToolMessage/Command.update.messages）；`_put`→buffer→达阈值 `_flush_sync`（同步回调内 `get_running_loop` + `create_task` 调度 async put_batch，无循环留 buffer）+ `_pending_flush_tasks` 防并发写（红线 #8）+ 失败 batch 回插；`_schedule_progress_flush` 节流（progress_reporter 注入，无模块循环）；`record_external_llm_usage_records`（外部 token 去重）、`record_middleware`、`get_completion_data`、`had_llm_error_fallback`。
- **测试**：`test_journal.py`（36 个，hermetic：caller 识别、token 分桶 + 同 run_id 去重 + total 补算 + track_tokens 关闭 + 多 run 累加、sync→async flush 达阈值触发/未达留 buffer/flush 排空、失败 batch 回插重试不丢、error fallback 检测+降级到 reason/text、record_middleware 落盘、首条 human 抽取+只抽一次+跳 summary+截断、external usage 累加+去重+补算+跳 0、last_ai 只 lead 非空更新、生命周期 chain/tool 回调、message_count）
- **文档**：`docs/run_journal.md`
- **踩坑修正**：langchain `AIMessage` 要求 `usage_metadata.total_tokens` 字段在，测「total 补算」分支时传 `total_tokens=0`（非缺省）触发

### M2 reflection（Phase 0）
- **代码**：`reflection/resolver.py`（`resolve_class` + `resolve_variable`，既有）
- **测试**：`test_reflection.py`（hermetic，stdlib + monkeypatch importlib）

### 测试质量基线
- 6 个旧测试重写为 hermetic（reflection/config/tools/middlewares/agent/agent_with_middlewares），对齐 deer 风格

### Phase 1 质量审查加固（对照 deer-flow 四维审查）
对照 deer-flow 参考对 Phase 1 六模块做「设计思想对齐 + bug + 适配正确性 + 测试/文档质量」四维审查。**结论：实现层面零严重 bug，六模块全部高度对齐**（persistence/checkpointer/journal/stream_bridge/serialization 5/5，events/store 4/5），设计思想（三态 user_id / 委托 Saver / seq 单调锁 / sync→async 桥 / 有界窗口 / 单一序列化真相源）全部正确落地。问题集中在测试覆盖缺口 + 个别文档措辞，本次全部补齐，**生产代码零改动**：
- **M7 journal +8 测试**：progress 节流（snapshot 上报 / interval 内不重复 / flush 取消 delayed 不额外上报——原审查重点项却零覆盖）、on_tool_end 的 `Command.update.messages` 分支、on_llm_error、latency_ms（有/无 on_chat_model_start）、多 batch 抽首条 human。加固 2 处弱断言（message_count `>=1`→`==1`、external_records 补 total_input/output 防双计 false-pass）。
- **M6 events/store +5 测试**：postgres advisory-lock SQL 分支（FakeSession，锁住 `pg_advisory_xact_lock` + 聚合 SELECT 不带 FOR UPDATE——sqlite 上无法验证、只在生产暴露的正确性盲区）、message/trace 交错 cursor 边界（锁住 bisect_left/right 排他语义）、list-content 往返（`content_is_dict` 是 dict 专属 flag）。
- **文档措辞 2 处**：run_journal.md「on_chat_model_start 存 llm_request」→实际发 `llm.human.input`；persistence.md RunStore ABC 签名补 user_id 三态注脚。（stream_bridge `__init__` 审查报的「based on asyncio.Queue」实测为误报，mini 实为 `asyncio.Condition`，未改。）
- **基线**：345 → **358 passed, 1 skipped, 0 lint 错误**。

---

## 待办（按 Phase，下次从这里继续）

> 铁律：严格按 Phase 0→8 顺序，每完成一步跑 `make test && make lint` 确认绿。每条只给「做什么 + 测试 + 参考」；完整文件清单/依赖/红线查 ALIGNMENT_OUTLINE.md Part C 对应小节。

### Phase 0 — 地基（✅ 全部完成）

~~M-build / M0 config / M1 utils / M2 reflection / M3 user_context~~ — 全部 ✅。

### Phase 1 — 持久化 + 运行时基础（✅ 全部完成）

- ~~**M4 持久化**~~（下次起点，大件）— ✅ 已完成（见「已完成详情」）。
- ~~**M5 checkpointer**~~（下次起点）— ✅ 已完成（见「已完成详情」）。
- ~~**M6 events/store**~~（下次起点）— ✅ 已完成（见「已完成详情」）。
- ~~**M9 serialization**~~（下次起点，纯函数无依赖，可插队）— ✅ 已完成（见「已完成详情」）。
- ~~**M8 stream_bridge**~~（下次起点）— ✅ 已完成（见「已完成详情」）。
- ~~**M7 journal**~~（下次起点，Phase 1 收尾）— ✅ 已完成（见「已完成详情」）。

### Phase 2 — 沙箱 / 子代理 / 追踪

- ⬜ **M10 sandbox** — `sandbox/`（ABC + provider + 5 工具 + middleware + local provider，虚拟路径翻译）。
- ⬜ **M11 subagents** — `subagents/`（registry/status_contract/executor 双线程池 + builtins）。
- ⬜ **M12 tracing** — `tracing/`（factory 图根注入 + metadata；落地后 M-models 的 `attach_tracing=True` 路径自动生效）。

### Phase 3 — 记忆

- ⬜ **M13 记忆** — `agents/memory/`（storage/queue/updater/prompt）+ **重写** `MemoryMiddleware`（真队列，当前是桩 `_update_queue=[]`）+ `DynamicContextMiddleware`。测试 `test_memory.py`。参考 deer `tests/test_memory_*.py`。

### Phase 4 — skill

- ⬜ **M14 skill** — `skills/`（parser/validation/slash/tool_policy/installer/storage）+ `SkillActivationMiddleware` + lead_agent 集成。测试 `test_skills.py`。

### Phase 5 — tool

- 🔶 **M15 tool** — 补 `view_image`/`task`/`mcp_metadata`/`sync` 包装 + 重写 `get_available_tools`（host-bash 过滤 + name 去重红线 #18 + 条件加载）。**现有仅 2 内置工具**（present_file + ask_clarification）。测试 `test_tools.py` 扩展。

### Phase 6 — 中间件

- 🔶 **M16 中间件** — 新增 16 + 重做 8，`build_middlewares` 重写为 **23 步**（顺序见 outline Part D；Clarification 永远最后红线 #14；ThreadData→Sandbox）。所有 `wrap_tool_call` 必须 `raise GraphBubbleUp`（红线 #15）。**现有 7 个教学版**。测试 `test_middlewares.py` 扩展。

### Phase 7 — agent

- 🔶 **M17 agent** — `features.py`（RuntimeFeatures + @Next/@Prev）+ `thread_state` 类型化 reducer（fail-closed 红线 #16）+ factory（features/extra_middleware/plan_mode）+ lead_agent（tracing 图根注入 + 工具策略）+ prompt（条件段 gating）。**现有精简版**。测试 `test_agent.py` 扩展。

### Phase 8 — 运行管理 + 集成

- ⬜ **M18 runs** — `runs/`（naming + store/memory + manager[asyncio 锁/busy 重试红线 #2/orphan 恢复红线 #7/shutdown drain 红线 #6] + worker[注入 runtime/journal、rollback 红线 #5、abort、LLM 兜底]）。测试 `test_run_manager.py` + `test_worker.py`。
- ⬜ **M19 runtime/store** — `runtime/store/`（LangGraph BaseStore 工厂，先做 memory）。测试 `test_store.py`。
- ⬜ **集成装配** — lifespan（init_engine + make_checkpointer + make_stream_bridge + make_thread_store + RunManager + reconcile + shutdown drain）+ 对齐 `langgraph.json`（补 checkpointer 段）+ `config.example.yaml` 增补（bump config_version）。端到端冒烟 `test_integration.py`。

### 可选模块（按需，不阻塞主线）

- ⬜ M-opt-community（web 搜索，建议先移植 1 个让 agent 联网）/ ⬜ M-opt-mcp / ⬜ M-opt-uploads / ⬜ M-opt-agents_config / ⬜ DeerFlowClient

---

## 下次开工

1. `cd backend && make test && make lint` 确认 **358 passed + 0 lint 错误**基线绿。
2. **Phase 1 全部完成**，进入 **Phase 2**，从 **M10 sandbox** 起步（沙箱执行隔离）：`sandbox/`（抽象 `Sandbox` + `SandboxProvider` 单例 + 5 工具 bash/ls/read_file/write_file/str_replace + `SandboxMiddleware` + `security.is_host_bash_allowed` + `sandbox/local/LocalSandboxProvider`，虚拟路径 `/mnt/user-data/{workspace,uploads,outputs}` + `/mnt/skills` 翻译、每线程 `local:{thread_id}`、LRU 上限防泄漏）。依赖 config/sandbox_config + runtime/user_context + config/paths。**裁剪**：Docker/AIO provisioner 不做。
3. 按 outline B.1 线性顺序继续：M10 → M11 subagents（骨架）→ M12 tracing。Phase 2 三模块相互独立可并行，但单线最稳。
4. 每完成一个模块，回到本文件把对应行的状态 / 代码 / 测试 / 文档列打 ✅。

> Phase 2 三个模块（sandbox/subagents/tracing）相互独立，可并行；单线 M10→M11→M12 最稳妥。
