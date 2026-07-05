# 0. start-here.md — 零基础从这里开始（把 mini 跑起来）

> 📝 重写于 2026-07-05 · 对照代码 commit `ffc5e5d`（文中的命令 / 文件路径以此为准）。

> 这是读这套文档的**第一页**。读完它你应该能：
> ① 用大白话说清「deer-flow / mini / LangGraph / agent / harness」各是什么；
> ② **在自己电脑上把 mini 真的跑起来**，发一条消息，看到 AI 真的回复你；
> ③ 看懂「一条消息从发出到回复」背后大致经过哪几层，并知道接下来按什么顺序读。
>
> 本文**故意不展开任何模块的细节**——那是 #1–#28 每篇的活。这里只给你三样东西：**能跑起来 + 看清全貌 + 知道下一步**。
>
> 配套文件：[README.md](README.md)（文档索引）· [../backend/Makefile](../backend/Makefile)（跑起来的命令）· [../backend/langgraph.json](../backend/langgraph.json)（agent 入口）· [../.env.example](../.env.example)（API key 模板）· [../backend/config.yaml](../backend/config.yaml)（模型 / 工具配置）

---

## 学完这篇你能回答什么（learning outcomes）

- 「agent」「LLM」「LangGraph」「harness」分别是什么？mini 和上游 deer-flow 差在哪、为什么？
- 怎么把 mini 在自己机器上跑起来，第一条消息发出去**看到真实回复**（不是只会跑测试）。
- 一次「用户发消息 → AI 回复」背后大致经过哪几层？（给后面 #25 agents / #28 architecture 埋个引子）
- 这套文档按什么顺序读最省事？面试前该重点看哪张表？

> 这几条都贴 agent 面试常考点——本文是你接触「用代码实现一个 agent」的起点。

---

## 1. 先搞懂几个词（零基础前置知识，一页纸教完）

下面这些词后面每篇文档都会用到。先有个一页纸印象即可，**不用背**——后面遇到还会再解释。

### 1.1 LLM / token / 上下文窗口

- **LLM（大语言模型）**：就是 ChatGPT、DeepSeek 背后那个「给一段文字、它接着往下写」的程序。你可以把它想成一个**极其擅长接话的超级打字员**：你喂它一段对话，它猜下一句该说什么。
- **token**：LLM 不是按「字」而是按「token」（大致是词或词的一截）来数文字的。1 个汉字大约 1–2 个 token。**LLM 按用了多少 token 收钱**，所以 agent 里到处在「省 token」。
- **上下文窗口（context window）**：LLM 一次能「记住」多长的对话，比如 8K / 128K token。对话超长就得**摘要 / 裁剪**，否则塞不下——这是后面 [middlewares.md](middlewares.md) 的核心痛点。

### 1.2 agent = LLM + 工具 + 循环（ReAct 的雏形）

光有个会接话的 LLM 还不算 agent。**agent 的关键：让 LLM 能「调工具」并「循环」**。

举个具体例子体会一下。你问 agent：「帮我看看当前目录有哪些文件」。

1. LLM 想：这要用到「列目录」的工具。于是它**输出一个工具调用**（不是直接回答）：`调用 ls_tool`。
2. 系统真的去执行 `ls_tool`，拿到结果：`["a.txt", "b.py"]`。
3. 把这个结果**塞回对话**给 LLM 看。
4. LLM 看到「哦，有两个文件」，**这才组织出最终回答**：「当前目录有 a.txt 和 b.py」。

这个**「思考 → 调工具 → 看结果 → 再思考」的循环**，就是 agent 的灵魂，学名叫 **ReAct（Reasoning + Acting，推理 + 行动）**。deer-flow / mini 干的所有事，本质都是在**把这个循环工程化**：给 LLM 配哪些工具、怎么把工具结果塞回去、怎么防止它无限循环、怎么记住之前说过的话……

> 面试一句话：**agent = LLM + 工具 + 循环；LangGraph 把这个循环编成了一个「状态机」（下一步讲）。**

### 1.3 LangGraph = 用「图」来组织上面那个循环

**LangGraph** 是一个开源框架（LangChain 团队出品），专门用来写 agent。它的核心思路是把 agent 画成一张**图（graph）**：

- **节点（node）**：图里的一个个步骤。比如「调 LLM」是一个节点，「执行工具」是另一个节点。
- **边（edge）**：步骤之间的跳转。比如「LLM 说要调工具 → 跳到工具节点」「工具执行完 → 跳回 LLM 节点」。
- **状态（state）**：图在跑的过程中一直带着的那份数据——最核心的就是**对话消息列表**（messages）。每个节点都能读它、改它。
- **checkpointer（检查点）**：图每走一步，把当前状态**存个快照**。这样对话能跨轮恢复、能回放、断了能续。→ 详见 [checkpointer.md](checkpointer.md)。

mini 的入口 `make_lead_agent`（见 [langgraph.json](../backend/langgraph.json)）就是**用 LangGraph 拼出来的这么一张图**。

> **harness 是什么？** 这个词在项目里反复出现。harness 本意是「马具 / 挽具」——套在马上让你能驾驭它的那套装备。在软件里，**agent harness = 一整套让你能「驱动、观察、控制」一个 agent 的脚手架**：怎么装配置、怎么跑、怎么存记忆、怎么限流、怎么追踪…… deer-flow 自称 "super agent harness"，mini 是它的教学版。**读完这 28 篇，你就读懂了一个完整的 agent harness 长什么样。**

### 1.4 Python 的 async / await 与「事件循环」（为什么重要）

mini 的代码里到处是 `async def` / `await`。你只需要知道：

- 普通代码（同步）是「排队执行」：前一件事没做完，后面全卡着等。
- **async（异步）**：程序在「等一个慢操作」（比如等 LLM 网络返回、等读文件）时，**可以先去干别的事**，等好了再回来接着处理。这样一台机器能同时处理很多请求。
- **事件循环（event loop）**：调度这些「谁先好了谁先跑」的总指挥。`asyncio.run(main())` 就是启动它。
- **关键约束**：在 async 代码里，**不能直接做很慢的同步操作**（比如直接读写一个大文件），否则会把事件循环卡死，整个服务假死。mini 有专门的测试守这条红线（→ [build.md](build.md) / [testing-setup.md](testing-setup.md)）。

不用现在就会写 async——知道「mini 是异步的，所以有些代码长得怪（各种 `to_thread`、`async with`）是为了不卡死」就够了。

### 1.5 venv / 依赖 / make（跑起来要懂的工程名词）

- **venv（虚拟环境）**：Python 项目的「独立小房间」。每个项目用自己的依赖版本，互不干扰。mini 用 `uv`（一个超快的 Python 工具）来建和管理它。
- **依赖（dependencies）**：项目用到的别人写好的包（比如 langgraph、langchain）。`uv sync` 就是「按清单把这些包装进 venv」。
- **make / Makefile**：一个老牌的「任务快捷方式」工具。[Makefile](../backend/Makefile) 里定义了 `dev` / `test` / `lint` 等命令——你敲 `make dev`，它就帮你跑一串底层命令（设环境变量 + 启动服务），省得你手敲一长串。

> 这一节是「兜底」。后面任何词第一次出现还会就地解释一句（本仓库文档约定）。

---

## 2. deer-flow 是什么，mini 又是什么

**deer-flow**（[bytedance/deer-flow](https://github.com/bytedance/deer-flow)）是字节开源的一个 **super agent harness**——一个能编排子代理、有记忆、能在沙箱里干活、靠可扩展「技能」驱动的通用 agent 框架。简单说：**一套「搭一个能干活的 AI 助手」的完整工程实现**。

**mini-deer-flow** 是它的**教学化重写版**：

- **同样的核心**：用更小的代码量、更细的讲解，把 deer-flow 的 harness 层重新写一遍，**行为上对标、不裁剪核心功能**（沙箱 / 子代理 / 记忆 / 工具 / 技能 / MCP / 联网 / 上传 / 自定义 agent 全在）。
- **故意不 port（不搬）的部分**：deer-flow 还有一层 **Gateway**（FastAPI 写的对外 Web 服务：REST API、用户认证、IM 渠道集成、Docker 部署）。mini 是**教学版，不要这层**——mini 直接用 `langgraph dev`（开发服务器）跑，或基于 [`runtime_lifespan`](architecture.md) 自己搭。所以 mini **没有**：对外 REST API、登录鉴权、IM 渠道、Docker 部署、生产级扩缩容。
- **衍生关系**：mini 大量代码衍生自 / 移植自 deer-flow（都 MIT 协议，已保留上游版权声明，见 [../NOTICE](../NOTICE)）。mini 是学习用途，**不是** deer-flow 的替代分支，也不受 Bytedance 认可。生产用请用上游 deer-flow。

> 一句话：**deer-flow 是「一个能上生产的完整 agent 产品」；mini 是「把它的核心拆开、讲给你看的教学版」——能跑、能学，但不包上生产。**

---

## 3. 整仓导览（目录里都有什么）

clone 下来后，项目根目录长这样（只列和「跑起来 / 学」有关的）：

```
mini-deer-flow/
├── README.md              # 项目说明（本教学文档的上一层入口）
├── .env.example           # API key 模板（你照着填 → backend/.env）
├── config.example.yaml    # 配置模板（注释很全，讲清每个字段）
├── docs/                  # ← 你正在读的教学文档（#0–#28 都在这）
├── test/                  # 1700+ 个测试
├── skills/                # 技能目录（SKILL.md 协议，→ #19）
├── contracts/             # 契约 / 类型定义
└── backend/               # ← 跑起来都在这
    ├── Makefile           # dev / test / lint 命令
    ├── langgraph.json     # agent 入口（告诉 langgraph：图是 make_lead_agent）
    ├── config.yaml        # 实际生效的配置（模型 / 工具 / 记忆 …）
    ├── .env               # 你的 API key（从 .env.example 复制，自己填，别上传 git）
    ├── extensions_config.json  # MCP 扩展配置（→ #20）
    └── packages/harness/deerflow/   # ← 所有源码在这
        ├── agents/   config/   models/   persistence/   runtime/
        ├── sandbox/  subagents/  tools/   skills/   mcp/
        ├── community/  uploads/  tracing/  utils/   reflection/
        └── ...
```

记住三个关键文件就够开工：

| 文件 | 干嘛的 | 你什么时候碰它 |
|------|--------|----------------|
| `backend/.env` | 放你的 API key | 第一次跑：填 `DEEPSEEK_API_KEY` |
| [../backend/config.yaml](../backend/config.yaml) | 配模型 / 工具 / 记忆等 | 换模型、开关功能时改 |
| [../backend/Makefile](../backend/Makefile) | `make dev/test/lint` | 每天跑命令 |

源码主体在 [../backend/packages/harness/deerflow/](../backend/packages/harness/deerflow/)——28 篇文档就是逐个拆这个目录下的模块讲给你听。

---

## 4. 从零装跑（手把手，照着做到看到第一条回复）

> 目标：跑通 `make dev`，在浏览器里和 agent 说上话，看到它真的回复你。**预计 10–15 分钟**（大部分时间在装依赖）。

### 前置：你机器上要有

- **Python 3.12+**（[langgraph.json](../backend/langgraph.json) 指定 3.12；3.14 也行）
- **[uv](https://docs.astral.sh/uv/)**：装依赖用的超快工具。没装：`curl -LsSf https://astral.sh/uv/install.sh | sh`（macOS/Linux）或 `brew install uv`。
- **一个 LLM API key**：默认用 DeepSeek（便宜、国内好申请）。去 [DeepSeek 开放平台](https://platform.deepseek.com/) 注册 → 创建 API key。也支持 Qwen-VL / OpenAI / 本地 Ollama / vLLM（换法见 [../config.example.yaml](../config.example.yaml) 注释）。

### 第 1 步：把虚拟环境挪出 iCloud（**macOS 用户必做，否则后面随机崩溃**）

⚠ 这是本机踩过最大的坑。本项目在 `~/Documents` 下，而 macOS 会把 `~/Documents` 接到 iCloud 去同步。Python 虚拟环境里有**几万个碎文件**，被 iCloud 一同步 / 清理，就随机报 `ModuleNotFoundError`（某个包文件被云端「优化」掉了）。

**根治**：把虚拟环境建到**不同步**的 home 目录下。把这行加进你的 `~/.zshrc`（一次性，以后都生效）：

```bash
export UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow
```

然后 `source ~/.zshrc`（或重开终端）让它生效。之后所有 `make` 命令都自动用这个目录，不再碰 iCloud。

> 不在 macOS / 不在 iCloud 同步目录？跳过这步。

### 第 2 步：装依赖

```bash
cd backend
make install      # 等同于 uv sync，把所有依赖装进 ~/.venvs/mini-deer-flow
```

第一次会下一阵子（langgraph / langchain / sqlalchemy 一堆包）。没有红字报错就成。

### 第 3 步：填你的 API key

先回到**项目根目录**（如果你还在 `backend/`，先 `cd ..`），把模板复制成实际生效的环境文件：

```bash
cp .env.example backend/.env
```

然后**编辑 `backend/.env`**，把 DeepSeek key 填进去：

```
DEEPSEEK_API_KEY=sk-你刚才申请的那个key
```

> 为什么是 `backend/.env`？因为 [langgraph.json](../backend/langgraph.json) 里写了 `"env": ".env"`，`langgraph dev` 从 `backend/` 启动时会读这个文件、把里面的变量喂给程序。而 [backend/config.yaml](../backend/config.yaml) 里模型配置写的是 `api_key: $DEEPSEEK_API_KEY`——`$DEEPSEEK_API_KEY` 会自动替换成你 `.env` 里填的值。所以**填对位置很重要**：填在 `backend/.env`，不是项目根目录。

### 第 4 步：（可选但建议）确认模型名

打开 [../backend/config.yaml](../backend/config.yaml)，看 `models:` 下第一个模型（`name: deepseek`）的 `model:` 字段，默认是：

```yaml
model: deepseek-v4-pro
```

**如果你跑起来报「model not found / 不存在的模型」**——说明你的 DeepSeek 账号还没开通 V4 Pro。把它改成 DeepSeek 当前通用的模型名：

```yaml
model: deepseek-chat
```

（这步不一定要改；先按默认试，报错再回来改。这是最常见的「第一跑坑」之一，见 §6。）

### 第 5 步：启动！

```bash
cd backend
make dev
```

终端会打印一串日志，最后给你一个本地 URL，类似：

```
✨ LangGraph dev server ready: http://127.0.0.1:2024
```

（端口号看终端实际打印；默认是 2024。）

### 第 6 步：在浏览器里发第一条消息

1. **打开那个 URL**（http://127.0.0.1:2024）。你会看到 **LangGraph Studio**——一个可视化的 agent 调试界面。
2. 顶部选图表（graph）：选 **`lead_agent`**（这是 mini 唯一的图，见 [langgraph.json](../backend/langgraph.json)）。
3. 在输入框里**随便发一句**，比如：「你好，你是谁？」
4. 点发送 / Run。

**预期看到**：对话区里，AI **真的回了你一段话**（流式地一个字一个字冒出来）。🎉 恭喜——你已经把 mini 跑起来了，一个真实的、会调 LLM、能循环推理的 agent 在你电脑上活着了。

### 第 7 步（进阶）：看它「真的在干活」

光聊天不过瘾？试试让它**调工具**。config.yaml 默认开了 7 个沙箱工具（bash / 读写文件 / 搜索）。发这句：

> 在沙箱里建一个文件 hello.txt，内容写「mini 跑起来了」，然后列目录给我看。

你会看到 agent **自己决定调用 `write_file_tool` → 再调 `ls_tool`**，最后告诉你结果。这就是 §1.2 讲的「ReAct 循环」在你眼前真实发生——**工具调用、结果回灌、再推理**，全都能在 Studio 里看到。这正是「能跑能用」的目标达成。

---

## 5. 全景图：一条消息从发出到回复，经过哪几层？

跑起来后，你可能好奇：我发的那句话，到底在 mini 内部走了多远？这里给你一张**极简全景**（细节都在 #25 agents / #28 architecture）。

```
你发的消息
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│ LangGraph dev server（langgraph.json 指向 make_lead_agent） │
└──────────────────────────────────────────────────────────┘
   │
   ▼
┌──────────────┐    每轮：LLM 决策 →（要调工具？）
│ lead_agent   │─────────────────────────────────────┐
│ (一张        │                                       │
│  LangGraph 图)│   ◄── 工具结果回灌，LLM 再决策 ──────┘
└──────────────┘
   │  依赖一整套「harness」单件（启动时由 runtime_lifespan 装好）：
   │
   ├── models/        挑哪个 LLM、怎么连（→ #6）
   ├── tools/         9 内置工具 + MCP/联网（→ #22）
   ├── sandbox/       工具在隔离环境里干活（→ #13 / #14）
   ├── memory/        跨会话记住你的事实（→ #18）
   ├── checkpointer/  对话状态存快照（→ #8）
   ├── middlewares/   23 步处理链（裁 token / 防循环 / 安全…）（→ #24）
   ├── subagents/     委派子任务给别的 agent（→ #15）
   └── tracing/       链路追踪（→ #16）
   │
   ▼
AI 的回复（流式吐回浏览器）
```

**记一句话**：你发的话 → 进 `lead_agent` 这张图 → 图在「harness 单件」（模型 / 工具 / 沙箱 / 记忆 / 中间件…）的支撑下循环推理 → 回复流式吐回来。**28 篇文档，就是把这个框里的每一格拆开讲。**

### 28 篇怎么排？推荐读序

按依赖顺序读最省事（先读「让能跑成立」的，再读「让能干活」的）：

1. **先把这一篇（#0）跑通** ← 你在这
2. 想立刻看全貌？先扫 [architecture.md](architecture.md)（#28，收尾篇但有全景图，当「地图」先看无妨）
3. 然后从 **#1 [build.md](build.md)** 起，按 [README.md](README.md) 的 #1→#28 顺序逐篇钻：
   - Phase 0 地基（build / config / utils / user_context）→
   - Phase 1 模型 + 运行时（models / persistence / checkpointer / events / journal / stream_bridge / serialization）→
   - Phase 2 沙箱 / 子代理 / 追踪 → Phase 3 记忆 → Phase 4 技能 → Phase 5 MCP + 联网 + 工具 → Phase 5.5 上传 → Phase 6 中间件 → Phase 7 agent 装配 → Phase 8 运行管理 + Store + 集成

**面试前**：过一遍 [doc-rewrite-todo.md](doc-rewrite-todo.md) 末尾的「**面试概念地图**」——它把 agent 面试常考点对到了 mini 哪篇讲、一句话怎么答。

---

## 6. 常见第一跑坑（排错速查）

跑不通先看这里，90% 的第一跑问题都在这：

| 现象 | 原因 | 解决 |
|------|------|------|
| 装依赖 / 跑 dev 随机报 `ModuleNotFoundError`（且每次缺的包不一样） | **venv 在 iCloud 同步目录**，文件被云端清掉了 | 回 §4 第 1 步，设 `UV_PROJECT_ENVIRONMENT=~/.venvs/mini-deer-flow` 挪出 iCloud，重装 |
| 发消息没回复 / 报 401 / 认证失败 | API key 没填或填错位置 | 确认填在 **`backend/.env`**（不是项目根目录），`DEEPSEEK_API_KEY=sk-...` 没多余空格 / 引号 |
| 报「model not found / 不存在的模型 / invalid model」 | config.yaml 里 `model: deepseek-v4-pro` 你的账号没开通 | 改成 `model: deepseek-chat`（§4 第 4 步） |
| `import deerflow` 失败 | 用了裸 venv 的 python（editable install 不稳） | **用 `make` 命令**（`make dev/test` 自带 `PYTHONPATH=packages/harness`）；冒烟脚本要带 `PYTHONPATH=packages/harness` |
| `make dev` 报端口被占 | 2024 端口已有别的进程 | 关掉占用进程，或按 langgraph dev 提示换端口 |
| 跑起来了但 agent「只会聊天、不会干活」 | config.yaml 的 `tools:` 段被注释了 | 确认 `tools:` 下有那 7 个沙箱工具（默认有）；详见 [tools.md](tools.md) |

> 更底层的排错（venv / editable install / Python 版本）见 [build.md](build.md) 和 [testing-setup.md](testing-setup.md)。

---

## 7. 下一步读什么

- **想看「怎么把代码跑起来 + 测试怎么跑 + lint 是什么」** → [#1 build.md](build.md)（工程化地基）
- **想先看「整个系统怎么拼起来」** → [#28 architecture.md](architecture.md)（全景图，可先当地图扫）
- **想找某个模块** → [README.md](README.md)（按 #1–#28 索引）
- **想看项目待办 / 工作进度** → [todo.md](todo.md)
- **想看这套文档本身的重写进度** → [doc-rewrite-todo.md](doc-rewrite-todo.md)

---

## 附：想先补概念，可预读 deerflow-book

本文是「一页纸兜底」。想系统补 agent / LangGraph 概念，可以读同仓库的概念书 [`deerflow-book/chapters/`](../../deerflow-book/chapters/) 的前 5 章（**借概念框架**，实现仍以 mini 为准；deer-flow 里 Gateway / IM / 部署等 mini 没有）：

- [01 what-is-deerflow](../../deerflow-book/chapters/01-what-is-deerflow.md) — deer-flow 到底是什么
- [02 repo-overview](../../deerflow-book/chapters/02-repo-overview.md) — 仓库结构总览
- [03 quick-start](../../deerflow-book/chapters/03-quick-start.md) — 快速上手
- [04 langgraph-engine](../../deerflow-book/chapters/04-langgraph-engine.md) — LangGraph 引擎（图 / checkpointer / store）
- [05 lead-agent](../../deerflow-book/chapters/05-lead-agent.md) — 主 agent 怎么搭

---

> 跑通了？下一步去 [#1 build.md](build.md)，搞懂「你刚才敲的那些 `make` 命令背后到底干了什么」。
