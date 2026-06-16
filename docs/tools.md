# 工具(Tools)体系

本文档说明 DeerFlow 的工具体系——位于 `backend/packages/harness/deerflow/tools/`,重点是 `builtins/` 下开箱即用的两个内置工具,以及它们背后的四项共享机制:`Runtime` 注入、`@tool` 的 `parse_docstring` / `return_direct`、`Command` 状态更新、`artifacts` 产物字段。

---

## 设计理念:工具即「图的控制流 + 状态原语」

理解这套工具体系的关键认知是:**这里的工具不是普通「返回数据给模型的 RPC」**。在 LangChain 1.0 的 `create_agent` + LangGraph 架构里,工具本身也是图里的一个节点,因此它可以:

- **写图的共享状态**(返回 `Command` 更新 `ThreadState`);
- **改变控制流**(打断执行、跳转节点);
- **被中间件拦截**(占位工具 + middleware 实现真正逻辑)。

`builtins/` 下的两个工具正是上述能力的两次典型示范。

---

## 目录结构

```
backend/packages/harness/deerflow/tools/
├── types.py                       # Runtime 类型(运行时参数注入)
└── builtins/
    ├── __init__.py                # 导出两个内置工具
    ├── present_file_tool.py       # present_files —— 产物展示
    └── clarification_tool.py      # ask_clarification —— 澄清/打断
```

| 文件 | 暴露的工具名 | 示范的模式 |
| --- | --- | --- |
| `types.py` | `Runtime` 类型 | 运行时参数注入类型 |
| `builtins/present_file_tool.py` | `present_files` | **运行时注入 + 状态副作用** |
| `builtins/clarification_tool.py` | `ask_clarification` | **占位工具 + 中间件拦截** |

---

## 共享机制一:`Runtime`(运行时参数注入)

定义在 `tools/types.py`:

```python
from langchain.tools import ToolRuntime
from deerflow.agents.thread_state import ThreadState

# 具体的 Runtime 类型:上下文用 dict[str, Any],状态用 ThreadState。
Runtime = ToolRuntime[dict[str, Any], ThreadState]
```

`ToolRuntime[ContextT, StateT]` 是 LangChain 1.0 新 `@tool` API 的**特殊参数类型**。当工具函数声明一个参数类型为 `ToolRuntime`(或它的具体子类 `Runtime`)时,会发生两件事:

1. **对模型不可见** —— 它不会出现在发给 LLM 的工具 JSON schema 里,模型无法也不需要填写;
2. **由 LangGraph 自动注入** —— 调用工具时,框架把当前执行上下文(`thread_id` + 当前 `ThreadState`)塞进去。

这是连接「无状态工具」与「正在运行的线程」的桥梁:工具因此能读到 `ThreadState` 里的 `artifacts`、`thread_data`(目录路径)、`sandbox` 等线程级数据。

> **为什么用 `dict` 而不是无界 `ContextT`**:避开 Pydantic 在 `model_dump()` 工具 `args_schema` 时的 `PydanticSerializationUnexpectedValue` 警告。

---

## 共享机制二:`@tool` 装饰器的两个关键参数

两个内置工具的装饰器都带了这两个参数,含义如下。

### `parse_docstring=True` —— 从 docstring 自动生成工具说明

设为 `True` 时,LangChain 会**解析函数的 Google/Sphinx 风格 docstring**,自动抽取:

- 工具的**描述文字**(docstring 主体)—— 发给模型,告诉它「这工具干嘛、何时用」;
- 每个**参数的说明**(从 `Args:` 段)—— 拼进工具 JSON schema 的 `description` 字段。

不设它,就得手写 `description` 和 `args_schema`。设了它,**docstring 既是给人看的注释,也是给模型看的工具说明书**,两者合一。

### `return_direct=True` —— 工具结果即终答,不再回模型

普通 ReAct 流程:工具执行 → 结果作为 `ToolMessage` 塞回消息历史 → **再调一次模型**让它解读/总结并决定下一步。

设了 `return_direct=True` 后:工具输出**短路**——它直接成为本次 Agent 调用的最终返回,**不再触发后续模型调用**,Agent loop 就此停止。

> 判断要不要加:`return_direct` 适合「工具本身就是终点」的场景(如澄清打断)。`present_files` 故意**不加**它,因为跑完工具后模型还要回一句「已为你展示文件 XX」,需要 loop 继续。

---

## 共享机制三:`Command`(工具如何写状态)

`Command` 是 LangGraph 的核心控制对象,来自 `langgraph.types.Command`。工具(或图的节点)返回 `Command`,等于在交差(返回值)的同时,递给图的总调度一张**「指令单」**,上面写着「请帮我顺手做这些图级操作」。

`Command` 有三大能力:

| 字段 | 作用 | `present_files` 用了吗 |
| --- | --- | :---: |
| `update` | 更新图的 state,值走对应字段的 reducer 合并 | ✅ |
| `goto` | 改变控制流——下一个该跳到哪个节点 | ❌ |
| `resume` | 恢复 human-in-the-loop——把用户输入传回被 interrupt 的执行 | ❌ |

### 为什么 `present_files` 必须用 `Command`,而不能 `return filepath`

对比两种写法的执行链:

**❌ `return filepath`(普通字符串)**:

```
工具执行 → filepath 被包成一条 ToolMessage → 塞进 messages 历史给模型读
       → state.artifacts 永远不会被更新 ❌
       → UI 读 state.artifacts 拿不到任何文件,「展示」目的落空
```

**✅ `return Command(update={"artifacts": [filepath]})`**:

```
工具执行 → LangGraph 拦截 Command → 把 update 应用到 state
       → 走 ThreadState.artifacts 绑定的 _merge_artifacts reducer(去重保序)
       → state.artifacts 被更新 ✅
       → 同时生成一条 ToolMessage,让模型 loop 继续(模型会接着说"已展示文件")
```

`Command` 是「让工具产生**持久化、跨步骤可见的副作用**」的唯一手段。

> **类比**:把 Agent 图想成一条流水线。普通工具函数像工人「干完活汇报一句」(返回值进 messages);`return Command(...)` 像工人干完活还往**中央登记簿**(state)上写了一笔,这条记录下游工位和前台 UI 都能读到——而且登记簿有自己的规矩(reducer)。

---

## 共享机制四:`artifacts`(工具产物的落点)

`artifacts` 是 `ThreadState` 里专门记录「**Agent 产出的、要交付给用户的成果文件**」的字段。

定义在 `agents/thread_state.py`:

```python
artifacts: NotRequired[Annotated[list[str], _merge_artifacts]]
"""Agent 输出的文件路径列表(使用去重 reducer)"""
```

要点:

- **类型是 `list[str]`** —— 一组**文件路径字符串**,不是文件内容本身;
- **绑了 `_merge_artifacts` reducer** —— 写入时自动去重保序;
- **属于 `ThreadState` 的扩展字段** —— 不在对话历史里,而在图的共享状态。

reducer 实现(去重保序):

```python
def _merge_artifacts(existing, new):
    combined = (existing or []) + (new or [])
    return list(dict.fromkeys(combined))  # 去重保序
```

### 为什么只存路径,不存内容

文件可能很大(几十页报告、高清图、代码包),塞进 `messages` 会爆 token、污染对话历史。`artifacts` 只登记**路径**,UI 按需读取文件内容来渲染。这正好和 `present_file_tool.py` 里的安全边界 `OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs"` 呼应——只有沙箱输出目录下的文件才有资格成为 artifact。

### 谁写、谁读

| | 角色 |
| --- | --- |
| **写入方** | 目前只有 `present_files`(通过 `Command(update={"artifacts": [filepath]})`) |
| **读取方** | UI 层订阅 `state.artifacts`,渲染成「产物面板」 |

> Agent 跑一通会产生很多中间文件,但**只有它主动调用 `present_files`「声明这是要交付给你的」的文件,才会进 `artifacts`**。这是「Agent 自主选择把哪些产出展示给用户」的机制,类似 Manus / ChatGPT Artifacts / Cline 里的右侧产物栏。

---

## 内置工具一:`present_files`(产物展示)

文件:`builtins/present_file_tool.py`。

**用途**:把 Agent 生成的输出文件展示给用户。安全边界是 `/mnt/user-data/outputs/`——只有该目录下的文件才能展示。展示的路径写进 `ThreadState.artifacts`,UI 读这个字段来渲染。

**完整实现**:

```python
OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs"

@tool("present_files", parse_docstring=True)
def present_file_tool(
    runtime: Runtime,
    filepath: str,
) -> Command:
    """Present one generated output file to the user.

    Only files under /mnt/user-data/outputs can be presented. The presented
    file path is recorded in thread artifacts so the UI can show it.

    Args:
        runtime: 注入的运行时上下文(thread_id + state),由 LangGraph 自动提供,模型不可见。
        filepath: 要展示的文件路径,必须在 /mnt/user-data/outputs/ 下。
    """
    # 1. 校验路径在允许范围内
    if not filepath.startswith(OUTPUTS_VIRTUAL_PREFIX):
        raise ValueError(f"只能展示 {OUTPUTS_VIRTUAL_PREFIX}/ 下的文件,收到: {filepath}")

    # 2. 通过 runtime.state 拿到当前线程状态
    if runtime.state is None:
        raise ValueError("线程状态不可用")

    # 3. 返回 Command 更新 artifacts(触发 ThreadState 的 merge_artifacts reducer)
    return Command(update={"artifacts": [filepath]})
```

**运行机制(三步)**:

1. **路径校验**:不在允许前缀下就抛 `ValueError`(沙箱安全边界);
2. **拿状态**:通过 `runtime.state` 取当前线程状态,为 `None` 则报错;
3. **返回 `Command`**:把 `{filepath}` 合并进 state 的 `artifacts`,触发 `_merge_artifacts` reducer。

注意它**没有** `return_direct`,所以工具跑完后模型还会继续 loop(通常会接着说「已为你展示文件」)。`present_files` 的真正业务核心在第 3 步那一行 `Command`——它不靠返回值,而靠这张「指令单」把文件路径写进线程状态。

---

## 内置工具二:`ask_clarification`(澄清 / 打断)

文件:`builtins/clarification_tool.py`。

**用途**:Agent 需要向用户追问时调用——缺信息、需求歧义、方案选择、风险确认、建议确认。

**完整实现**:

```python
@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed. ..."""
    # 占位实现。真正的逻辑由 ClarificationMiddleware 拦截此工具调用并中断执行。
    return "Clarification request processed by middleware"
```

**核心设计:占位工具 + 中间件拦截**

这个工具**自己不做真正的逻辑**——它只负责「声明一次澄清请求」,真正的中断(human-in-the-loop)由(规划中的)`ClarificationMiddleware` 拦截这个工具调用并打断 Agent 执行。源码注释里反复强调这点。

- **结构化参数**:`clarification_type` 是 5 选 1 的枚举,可选 `options`,这样中间件 / UI 能把这些字段渲染成「带选项的提问卡片」,Agent 挂起直到用户回答。
- **带 `return_direct=True`**:工具返回的是占位字符串 `"Clarification request processed by middleware"`,若还让模型「解读」它,模型会基于这个无意义文本继续编一段回答。`return_direct=True` 让工具一调用完就停,把控制权交给 `ClarificationMiddleware` 去做真正的人类打断。

> **诚实标注**:`ClarificationMiddleware` 目前仅在注释中作为设计意图出现(「阶段3」),尚未在仓库中实现。`ask_clarification` 是为它预留的占位工具。

---

## 两个工具的设计对照

| 维度 | `present_files` | `ask_clarification` |
| --- | --- | --- |
| 模型可见参数 | `filepath` | `question` / `type` / `context` / `options` |
| 模型不可见 | `runtime`(注入) | 无 |
| 主要「副作用」 | **写 state**(`Command` → `artifacts` reducer) | **打断执行**(交给 middleware) |
| 返回值 | `Command`(图状态更新) | 占位字符串(给 middleware 看) |
| `return_direct` | ❌ 要 loop 继续 | ✅ 立刻停 |
| 真正动作发生在哪 | 工具函数体内 + UI 订阅 state | **不在工具里**,在 `ClarificationMiddleware` |

---

## `ThreadState` 里的相关字段一览

`agents/thread_state.py` 继承 LangChain 的 `AgentState`(自带 `messages`),并扩展了以下字段。`artifacts` 是「产物/输出」类的唯一代表:

| 类别 | 字段 | 说明 |
| --- | --- | --- |
| 对话流 | `messages` | 消息历史(继承自 `AgentState`,LangGraph 自动管理) |
| **产物(输出)** | **`artifacts`** | `list[str]`,Agent 成果文件路径列表,去重 reducer |
| 任务清单 | `todos` | 待办事项列表,替换 reducer(空列表 = 清空) |
| 输入 | `uploaded_files` | 用户上传的文件信息列表 |
| 多模态 | `viewed_images` | Agent 已查看的图片(Base64),由 `ViewImageMiddleware` 写入,浅合并 reducer |
| 元数据 | `title` | 线程标题(由 `TitleMiddleware` 生成) |
| 沙箱 | `sandbox` | `{"sandbox_id": str \| None}` |
| 线程数据 | `thread_data` | `{"workspace_path", "uploads_path", "outputs_path"}` |

> 字段是否带 `Annotated[..., reducer]` 决定了它的合并语义:带 reducer 的(如 `artifacts`、`todos`、`viewed_images`)有自定义合并逻辑;不带的则是普通覆盖。

---

## 如何新增一个工具

根据需求,对照两个内置工具的模式选型:

| 需求 | 参考范例 | 关键做法 |
| --- | --- | --- |
| 工具需要访问线程上下文(state / thread_id) | `present_files` | 声明 `runtime: Runtime` 参数,框架自动注入,模型不可见 |
| 工具要产生持久化副作用 | `present_files` | 返回 `Command(update={...})`,值走对应字段的 reducer |
| 工具本身就是终点(不想让模型继续 loop) | `ask_clarification` | 装饰器加 `return_direct=True` |
| 工具只是「信号」,真正逻辑跨步骤/需打断 | `ask_clarification` | 占位实现 + 中间件拦截 |
| 任何工具都要给模型清晰的说明 | 两者 | 写好 Google 风格 docstring + `parse_docstring=True` |

**最小骨架(状态副作用型)**:

```python
from langchain.tools import tool
from langgraph.types import Command
from deerflow.tools.types import Runtime

@tool("my_tool", parse_docstring=True)
def my_tool(runtime: Runtime, some_arg: str) -> Command:
    """One-line description for the model.

    Longer explanation of when to use it.

    Args:
        runtime: 注入的运行时上下文,模型不可见。
        some_arg: 参数说明,会被解析进工具 schema。
    """
    state = runtime.state            # 拿到当前 ThreadState
    ...
    return Command(update={"artifacts": [some_arg]})  # 或其他 state 字段
```

写完后,在构造 Agent 时把它加进 `create_deerflow_agent(..., tools=[...])` 的工具列表(见 `agents/factory.py`)。
