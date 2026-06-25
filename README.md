# mini-deer-flow

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

> **mini-deer-flow** 是 [bytedance/deer-flow](https://github.com/bytedance/deer-flow) 的**教学化重写版**——
> 用更小的代码量、更细的讲解重写 deer-flow 这个 LangGraph 超级 agent harness，但**行为上全面对标、不裁剪核心功能**。
> 面向小白：每个名词第一次出现都会解释。

这是**学习用途**的项目，不是 deer-flow 的替代分支，也不附属于或受 Bytedance 认可。生产使用请用
[上游 deer-flow](https://github.com/bytedance/deer-flow)。衍生关系与版权声明见 [NOTICE](./NOTICE)。

---

## 这是什么

deer-flow 是一个开源 super agent harness（编排子代理 / 记忆 / 沙箱完成各种任务，靠可扩展技能驱动）。
mini-deer-flow 把它的**核心 harness 层**重写一遍，目标是**让人看懂**：

- **28 篇教学文档**（[docs/](./docs)，简体中文，按依赖顺序 #1–#28，从「怎么跑起来」一路到「怎么拼成系统」）；
- **harness 全模块对齐**：模型工厂 / 沙箱[本地+AIO 容器] / 子代理 / 链路追踪 / 记忆 / 技能 / MCP / 联网 /
  工具 / 上传 / 中间件（23 步生产链）/ agent 装配（SDK + config 双入口）/ 运行管理（RunManager + worker）/
  Store 工厂 / 集成装配；
- **~1477 个 hermetic 测试**（`cd backend && make test`）。

详见 [docs/README.md](./docs/README.md)（文档索引 + 依赖链）+ [docs/todo.md](./docs/todo.md)（进度看板）+
[docs/ALIGNMENT_OUTLINE.md](./docs/ALIGNMENT_OUTLINE.md)（设计规格）。

## 与上游 deer-flow 的关系

- **范围**：mini 只重写 harness 层（`packages/harness/deerflow/`）。deer-flow 的 FastAPI Gateway（`app/`：
  REST API / 认证 / IM 渠道集成）**不在 mini 范围**——mini 走 `langgraph dev` 或基于
  [`runtime_lifespan`](./backend/packages/harness/deerflow/runtime/lifespan.py) 的 bundle 自行搭。
- **差距**：完整对比见 [docs/todo.md](./docs/todo.md) 的「与 deer-flow/backend 的差距分析」段。主线 agent
  核心零差距；~90% 的代码量差距是 mini 设计上不 port 的 Gateway 层。
- **版权**：大量代码衍生自 / 移植自 deer-flow。按 MIT，已保留上游版权声明（见 [LICENSE](./LICENSE)）。

## 快速开始

```bash
# 在项目根目录
uv venv
uv sync --no-install-project      # 装 harness 依赖

source .venv/bin/activate

# 跑测试 + lint（验证环境）
cd backend && make test && make lint

# 启动 agent（需先复制 config.example.yaml → config.yaml 并填 API key）
cp config.example.yaml config.yaml
cd backend && langgraph dev
```

## 文档

按 **1 → 28** 顺序读最省事（[docs/README.md](./docs/README.md)）：

- **Phase 0 地基**：build / config / utils / user_context
- **Phase 1 模型 + 运行时基础**：models / persistence / checkpointer / events / journal / stream_bridge / serialization
- **Phase 2 沙箱 / 子代理 / 追踪**：sandbox / aio_sandbox / subagents / tracing / agents_config
- **Phase 3 记忆** · **Phase 4 技能** · **Phase 5 MCP + 联网 + 工具** · **Phase 5.5 上传**
- **Phase 6 中间件**（23 步）· **Phase 7 agent 装配** · **Phase 8 运行管理 + Store + 集成**

## License

[MIT License](./LICENSE)。

本项目的代码大量衍生自 [bytedance/deer-flow](https://github.com/bytedance/deer-flow)（同样 MIT）。按上游
许可要求，已保留其版权声明：

> Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
> Copyright (c) 2025-2026 DeerFlow Authors
> Copyright (c) 2026 mini-deer-flow contributors

详见 [LICENSE](./LICENSE) + [NOTICE](./NOTICE)。

## Acknowledgments

- **[bytedance/deer-flow](https://github.com/bytedance/deer-flow)** —— 本项目的上游来源与设计蓝本。
  mini-deer-flow 的全部核心模块设计、红线、行为契约均对齐 deer-flow v1.2「全面对标」。
