# mini-deer-flow 学习文档

> mini-deer-flow 是 deer-flow 的**教学化精简对齐版**。这份索引按「依赖顺序」列出 12 篇学习文档——从「怎么把代码跑起来」一路读到「运行时基础全部就位」。每篇都**面向小白**，每个名词第一次出现都会解释。
>
> 按 **1 → 12** 顺序读最省事。每篇开头标题已标注它在顺序里的位置（如 `# 9. run_event_store.md — ...`），单独打开某篇也能知道它排第几。

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

> 三者分工：**查进度** → [todo.md](todo.md)；**查设计规格** → [ALIGNMENT_OUTLINE.md](ALIGNMENT_OUTLINE.md)；**学某个模块** → 上面 1–12。
