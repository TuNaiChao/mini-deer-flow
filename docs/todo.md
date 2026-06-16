# todo.md — mini-deer-flow 实现进度

> 这份文档**只跟踪「做到哪了」**。每个模块要做成什么样（文件清单 / 依赖 / 可靠性要点 / 红线）查 [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md) 的 **Part C**（M-build / M0–M19 各小节）。
>
> **三交付** = 代码 + hermetic 测试（`test/test_<module>.py`）+ 学习文档（`docs/<module>.md`）。三齐 + 测试绿才算一个模块完成。
>
> **基线命令**：`cd backend && make test && make lint` → 当前 **62 passed, 1 skipped, 0 lint 错误**。

## 图例

- ✅ 完成 — 三交付齐、测试绿
- 🔶 部分 — 有代码/进度，但未达 outline 完整规格
- 📋 规格 — 设计文档写了，代码未落地
- ⬜ 未开始

---

## 总览

| 状态 | 数量 | 模块 |
|------|------|------|
| ✅ 完成 | 4 | M-build、M-models、M2 reflection、测试质量基线 |
| 🔶 部分 | 4 | M0 config、M15 tools（仅 2 内置）、M16 middlewares（7/23）、M17 agent（精简版） |
| 📋 规格 | 1 | M4 persistence（spec-M4 已写，代码未落地） |
| ⬜ 未开始 | 11 | M1 utils、M3 user_context、M5–M9、M10–M12、M13 记忆（仅桩）、M14 skills、M18–M19 + 集成 |

**下次起点**：Phase 0 剩余 → **M1 utils + M3 user_context**（小件热身）→ **M0 配置类型化**（大件）。

---

## 模块进度总表（按 Phase，一眼看完全部）

| Phase | 模块 | 状态 | 代码 | 测试 | 文档 |
|-------|------|------|------|------|------|
| 0 | M-build 工程化 | ✅ | ✅ | ✅ | ✅ build.md + testing-setup.md |
| 0 | M0 config（配置加载） | 🔶 | 部分 | 部分 | — |
| 0 | M1 utils | ⬜ | — | — | — |
| 0 | M2 reflection | ✅ | ✅（既有） | ✅ | —（对齐 deer） |
| 0 | M3 user_context | ⬜ | — | — | — |
| 1 | M-models（模型） | ✅ | ✅ | ✅ | ✅ models.md |
| 1 | M4 persistence（持久化） | 📋 | — | — | spec-M4（规格） |
| 1 | M5 checkpointer | ⬜ | — | — | — |
| 1 | M6 events/store | ⬜ | — | — | — |
| 1 | M7 journal | ⬜ | — | — | — |
| 1 | M8 stream_bridge | ⬜ | — | — | — |
| 1 | M9 serialization | ⬜ | — | — | — |
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
- **基线**：62 passed, 0 lint 错误

### M-models 模型（Phase 1）
- **代码**：`models/factory.py`（thinking 四路径 + reasoning_effort 门控 + stream 默认值 + attach_tracing 懒加载）
- **测试**：`test_model.py`（hermetic，`_Recorder` + monkeypatch）
- **文档**：`docs/models.md`
- **注**：原 2 处 `reasoning_effort` 失败已修（测试补 `supports_reasoning_effort=True`；factory 行为与 deer 一致，无需改）

### M2 reflection（Phase 0）
- **代码**：`reflection/resolver.py`（`resolve_class` + `resolve_variable`，既有）
- **测试**：`test_reflection.py`（hermetic，stdlib + monkeypatch importlib）

### 测试质量基线
- 6 个旧测试重写为 hermetic（reflection/config/tools/middlewares/agent/agent_with_middlewares），对齐 deer 风格

---

## 待办（按 Phase，下次从这里继续）

> 铁律：严格按 Phase 0→8 顺序，每完成一步跑 `make test && make lint` 确认绿。每条只给「做什么 + 测试 + 参考」；完整文件清单/依赖/红线查 ALIGNMENT_OUTLINE.md Part C 对应小节。

### Phase 0 — 地基（配置加载 / utils / user_context）

- ⬜ **M1 utils** — `utils/time.py`（`now_iso` / `coerce_iso`）+ `utils/messages.py`（`get_original_user_content_text`）。测试 `test_utils.py`。参考 deer `tests/test_utils_time.py`。
- ⬜ **M3 user_context** — `runtime/user_context.py`（三态 AUTO：contextvar → langgraph config → `"default"`）。测试 `test_user_context.py`。
- 🔶 **M0 配置类型化** — `config/` 下 ~18 个子配置 pydantic 化（database/checkpointer/run_events/stream_bridge/memory/title/…）+ `config_version` + `paths.resolve_path` + `extensions_config.is_skill_enabled`。测试 `test_config.py` 扩展。参考 deer `config/*_config.py`。

### Phase 1 — 模型（已完成）+ 持久化 + 运行时基础

- ✅ M-models（完成）
- 📋 **M4 持久化** — `persistence/`（base/engine/models + run/sql.RunRepository + thread_meta）+ **前置** `runs/schemas.py` + `runs/store/base.py`（RunStore ABC）。SQLite WAL + memory no-op。测试 `test_persistence.py`。参考 `spec-M4-persistence.md`。
- ⬜ **M5 checkpointer** — `runtime/checkpointer/`（委托 LangGraph Saver 不自建）+ `runtime/store/_sqlite_utils.py`。测试 `test_checkpointer.py`。
- ⬜ **M6 events/store** — `runtime/events/store/`（base + memory/jsonl/db，seq 单调 + 路径穿越防御）。测试 `test_events.py`。
- ⬜ **M9 serialization** — `runtime/serialization.py`（剥 `__pregel_*`/base64 image_url）。测试 `test_serialization.py`。
- ⬜ **M8 stream_bridge** — `runtime/stream_bridge/`（有界窗口 + Last-Event-ID 重连）。测试 `test_stream_bridge.py`。
- ⬜ **M7 journal** — `runtime/journal.py`（RunJournal → event_store 写入 + token 核算）。测试 `test_journal.py`。

### Phase 2 — 沙箱 / 子代理 / 追踪

- ⬜ **M10 sandbox** — `sandbox/`（ABC + provider + 5 工具 + middleware + local provider，虚拟路径翻译）。
- ⬜ **M11 subagents** — `subagents/`（registry/status_contract/executor 双线程池 + builtins）。
- ⬜ **M12 tracing** — `tracing/`（factory 图根注入 + metadata）。

### Phase 3 — 记忆

- ⬜ **M13 记忆** — `agents/memory/`（storage/queue/updater/prompt）+ **重写** `MemoryMiddleware`（真队列，当前是桩 `_update_queue=[]`）+ `DynamicContextMiddleware`。测试 `test_memory.py`。参考 deer `tests/test_memory_*.py`。

### Phase 4 — skill

- ⬜ **M14 skill** — `skills/`（parser/validation/slash/tool_policy/installer/storage）+ `SkillActivationMiddleware` + lead_agent 集成。测试 `test_skills.py`。

### Phase 5 — tool

- 🔶 **M15 tool** — 补 `view_image`/`task`/`mcp_metadata`/`sync` 包装 + 重写 `get_available_tools`（host-bash 过滤 + name 去重 + 条件加载）。**现有仅 2 内置工具**（present_file + ask_clarification）。测试 `test_tools.py` 扩展。

### Phase 6 — 中间件

- 🔶 **M16 中间件** — 新增 16 + 重做 8，`build_middlewares` 重写为 **23 步**（顺序见 outline Part D；Clarification 永远最后；ThreadData→Sandbox）。所有 `wrap_tool_call` 必须 `raise GraphBubbleUp`。**现有 7 个教学版**。测试 `test_middlewares.py` 扩展。

### Phase 7 — agent

- 🔶 **M17 agent** — `features.py`（RuntimeFeatures + @Next/@Prev）+ `thread_state` 类型化 reducer（fail-closed）+ factory（features/extra_middleware/plan_mode）+ lead_agent（tracing 图根注入 + 工具策略）+ prompt（条件段 gating）。**现有精简版**。测试 `test_agent.py` 扩展。

### Phase 8 — 运行管理 + 集成

- ⬜ **M18 runs** — `runs/`（naming + store/memory + manager[asyncio 锁/busy 重试/reconcile/shutdown drain] + worker[注入 runtime/journal、rollback、abort、LLM 兜底]）。测试 `test_run_manager.py` + `test_worker.py`。
- ⬜ **M19 runtime/store** — `runtime/store/`（LangGraph BaseStore 工厂，先做 memory）。测试 `test_store.py`。
- ⬜ **集成装配** — lifespan（init_engine + make_checkpointer + make_stream_bridge + make_thread_store + RunManager + reconcile + shutdown drain）+ 对齐 `langgraph.json`（补 checkpointer 段）+ `config.example.yaml` 增补（bump config_version）。端到端冒烟 `test_integration.py`。

### 可选模块（按需，不阻塞主线）

- ⬜ M-opt-community（web 搜索，建议先移植 1 个让 agent 联网）/ ⬜ M-opt-mcp / ⬜ M-opt-uploads / ⬜ M-opt-agents_config / ⬜ DeerFlowClient

---

## 下次开工

1. `cd backend && make test && make lint` 确认 **62 passed + 0 lint 错误**基线绿。
2. 从 **M1 utils** 起步（最小，2 个文件），走通一次完整三交付闭环（代码 → hermetic 测试 → `docs/utils.md`）。
3. 接 **M3 user_context**，再啃 **M0 配置类型化**。
4. 每完成一个模块，回到本文件把对应行的状态 / 代码 / 测试 / 文档列打 ✅。
