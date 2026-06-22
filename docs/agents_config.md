# 17. agents_config.md — 自定义 agent（SOUL.md 人格 + per-user 隔离）

> **一句话定位**：自定义 agent 让用户**造一个属于自己的 agent**——给它一份人格（SOUL.md）、
> 一份工具/技能白名单（config.yaml），之后每次和它对话都用这套设定。本模块负责**读写这套
> 设定文件** + 校验名字 + 按用户隔离存储。

读完 [user_context.md](user_context.md)（懂了 user_id 三态）+ [config.md](config.md)
（懂了配置系统）再看本篇最省事——自定义 agent 的存储就是「per-user 目录里两份文件」，
名字校验 + per-user 隔离是它的两个核心约束。

---

## 为什么需要自定义 agent（痛点）

默认的 lead agent 是「通用型超级助手」：什么都能干，但没有个性、没有专属知识、工具集也
固定。但很多场景你想要一个**专属 agent**：

- **代码审查员**：只读代码、用审查技能、说话严谨。
- **日报助手**：只写文件、用总结技能、语气简洁。
- **某团队的知识管家**：带特定人格（SOUL）、特定工具白名单、记住这个团队的事。

自定义 agent 就是让用户**自己定义**这么一个角色：一份 **SOUL.md** 写人格 / 价值观 / 行为
约束，一份 **config.yaml** 写它能用哪些工具组、哪些技能、用什么模型。设定存好后，每次和
这个 agent 对话（M17 lead_agent 的 custom-agent 分支），lead agent 会把 SOUL.md 注入系统
提示、按 config.yaml 限制工具/技能。

类比：默认 agent 是「公司前台」（什么都能答）；自定义 agent 是「你私人招的专员」——你写
好它的岗位说明（SOUL.md）和权限（config.yaml），它就按这套来。

---

## 核心概念（名词 + 类比）

### ① SOUL.md（人格文件）

一份 markdown，定义 agent 的「灵魂」——人格、价值观、行为约束、说话风格。例如：

```markdown
你是「代码审查员」。你只关注代码质量，不回答与代码无关的问题。
语气要直接、给出可执行的改进建议，先夸后批。
```

lead agent 把它**注入系统提示**作为附加上下文（M17 的 `{soul}` 条件段）。文件名固定
`SOUL.md`（常量 `SOUL_FILENAME`），不是任意名。

### ② config.yaml（能力配置）

一份 yaml，定义 agent 的「能干什么」：

```yaml
name: code-reviewer        # 名字（也可省略，从目录名推断）
description: 代码审查专家    # 列可用 agent 时给人看
model: deepseek-v3          # 可选：覆盖默认模型；省略 = 跟随全局
tool_groups: [file:read, bash]  # 可选：工具组白名单；省略 = 全部
skills: [review, lint]      # None=全部技能 / []=禁用 / 列表=只这些
```

`AgentConfig`（pydantic BaseModel）就是它的内存表示。

### ③ skills 的三态（容易混）

`config.yaml` 里的 `skills` 字段有**三种**语义不同的取值，别搞混：

| 取值 | 含义 |
|------|------|
| 缺省（`None`） | 加载**全部**启用的技能（默认回退） |
| `[]`（显式空列表） | **禁用**全部技能（一个都不加载） |
| `["a", "b"]` | **白名单**：只加载列出的技能 |

为什么 `None` 和 `[]` 要区分？——`None` 是「我不关心，用默认」；`[]` 是「我明确要这个
agent 没有技能」。前者是「未设置」，后者是「主动清空」。

### ④ per-user 隔离（一用户的 agent 不影响另一用户）

每个用户的 agent 存在各自的目录：

```
{base_dir}/users/{user_id}/agents/{name}/     ← per-user（当前，新写都写这）
    SOUL.md
    config.yaml
{base_dir}/agents/{name}/                      ← legacy 共享（只读回退，旧安装）
    SOUL.md
    config.yaml
```

`user_id` 来自 [user_context.md](user_context.md) 的 `get_effective_user_id()`（无鉴权回退
`"default"`）。Alice 的 `code-reviewer` 和 Bob 的 `code-reviewer` 互不可见、互不覆盖。

### ⑤ legacy 只读回退（兼容旧安装）

per-user 隔离是后来才加的。**之前**的安装把所有 agent 放在共享的 `{base_dir}/agents/{name}/`
下。为了不破坏这些旧安装，`resolve_agent_dir` / `load_agent_config` / `load_agent_soul` 都
**先查 per-user，查不到再查 legacy**。但**新写**（M15 的 setup_agent / update_agent 工具）
一律写 per-user，legacy 只读。

---

## 为什么名字要严格校验（AGENT_NAME_PATTERN）

agent 名字会拼进**文件系统路径**（`.../agents/{name}/`），所以必须校验，否则：

- `"../etc"` → 路径穿越，读写到 agent 目录之外。
- `"a/b"` → 多了一层目录。
- `"a b"`（空格）/ `"a\nb"`（换行）→ 文件名异常、shell 注入面。
- `"中文"` → 跨平台编码问题。

所以定了正则 `AGENT_NAME_PATTERN = ^[A-Za-z0-9-]+$`（红线 #32）：**只允许字母、数字、
连字符**，至少一个字符。`validate_agent_name` 用 `fullmatch`（整串匹配）强校验，被
setup_agent / update_agent / memory storage / client **共用**——校验一次，处处安全。

```python
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

def validate_agent_name(name: str | None) -> str | None:
    if name is None:           # None = 默认 agent，合法
        return None
    if not isinstance(name, str):
        raise ValueError(...)
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(...)
    return name                 # 通过校验，原样返回
```

合法名：`my-agent`、`agent123`、`CodeReviewer`、`A`。
非法名：`` ``（空）、`agent_name`（下划线）、`a/b`、`a.b`、`中文`、`../etc`。

---

## 设计原理（权衡 / 不变量 / 踩坑）

### 名称小写归一（防大小写碰撞）

`AGENT_NAME_PATTERN` 允许大写字母（`CodeReviewer` 合法），但磁盘目录一律 **`.lower()`**
归一（`Paths.agent_dir` / `user_agent_dir`）。两个原因：

1. **防碰撞**：macOS APFS 默认**大小写不敏感**——`CodeReviewer` 与 `codereviewer` 落进
   **同一个**目录，会互相覆盖。小写归一后两者映射到同一目录，不会偷偷分裂。
2. **对齐 deer**：deer 也是 `.lower()`，mini 1:1 对齐。

注意：校验后的原名（含大小写）仍保留在 `AgentConfig.name` 和 config.yaml 的 `name` 字段
里——只有磁盘目录名小写。

### resolve_agent_dir 的 #3390 防御：要 config.yaml 才认

`resolve_agent_dir` 判断「某个目录是不是这个 agent 的真目录」时，不只看目录存不存在，还要
看里面**有没有 `config.yaml`**：

```python
user_path = paths.user_agent_dir(user, name)
if user_path.exists() and (user_path / "config.yaml").exists():
    return user_path
# 否则查 legacy...
```

为什么？——memory 系统（M13）在首轮对话时会**提前**给某 agent 建一个 per-user 目录，但
那时只有 `memory.json`、还没有 `config.yaml`（用户还没 setup_agent）。如果下一回合
`resolve_agent_dir` 只看「目录存在」就返回它，`load_agent_config` 会读到「空配置」。

这个 bug 在 deer 是 #3390。修复就是「要求 config.yaml 才认 agent 目录」——只有 memory 写
入的残缺目录（无 config.yaml）会被跳过，正确回退到 legacy 或返回 per-user 占位。

### load_agent_config 剥未知字段（向前兼容）

config.yaml 喂给 pydantic `AgentConfig` 前，先剥掉**不在 model_fields 里的键**：

```python
known_fields = set(AgentConfig.model_fields.keys())  # {name, description, model, tool_groups, skills}
data = {k: v for k, v in data.items() if k in known_fields}
```

为什么？——旧版本的 config.yaml 可能有现在已经废弃的字段（如 legacy 的 `prompt_file`）。
如果不剥，pydantic 构造会因未知字段失败。剥掉后旧配置仍能加载，**向前兼容**。

### load_agent_soul：默认 agent 读 base_dir

`load_agent_soul(None)` 不报错——`None` 表示「默认 agent」，它没有专门目录，SOUL.md 读
`{base_dir}/SOUL.md`（全局人格）。返回 `None` 表示「没有 SOUL.md」（默认 agent 通常没有）。

空文件 / 纯空白也返回 `None`（`content.strip() or None`）——避免把空字符串当人格注入提示。

### list_custom_agents：并集 + per-user 覆盖

扫 per-user 根 + legacy 根，返回**并集**。同名时 per-user 先扫、进 `seen`，legacy 同名
跳过——**per-user 覆盖 legacy**。这保证：用户迁移到 per-user 后，旧的 legacy 同名 agent
不再重复出现；但用户没迁移的 legacy agent 仍可见。结果按 `name` 升序，稳定。

解析失败的目录（config.yaml 坏）记 warning 跳过，**不抛**——一个坏 agent 不该让整个列表
接口挂掉。

---

## 文件结构

```
config/
├── paths.py          # Paths.user_dir / agents_dir / agent_dir / user_agents_dir / user_agent_dir（名称 .lower() 归一）
└── agents_config.py  # 本模块：
                      #   SOUL_FILENAME = "SOUL.md"
                      #   AGENT_NAME_PATTERN = ^[A-Za-z0-9-]+$（红线 #32）
                      #   AgentConfig（pydantic：name/description/model/tool_groups/skills）
                      #   validate_agent_name（fullmatch 强校验，None 透传）
                      #   resolve_agent_dir（per-user 优先 + legacy 回退，要 config.yaml 才认 #3390）
                      #   load_agent_config（读 config.yaml，剥未知字段，缺文件 FileNotFoundError）
                      #   load_agent_soul（读 SOUL.md，strip，空→None，默认 agent 读 base_dir）
                      #   list_custom_agents（扫 per-user + legacy 并集，per-user 覆盖，排序）
```

> **为什么放在 config/ 而非 agents/?** 这是「配置加载」——读写设定文件、校验名字，没有
> 运行时 agent 逻辑。运行时（注入 SOUL 到提示、按 tool_groups 过滤工具）在 M17 lead_agent。
> 配置层只管「设定是什么、存在哪」，不关心「怎么用」。

---

## 关键接口

```python
# 常量
SOUL_FILENAME = "SOUL.md"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

# 校验（红线 #32，被 setup/update_agent + memory + client 共用）
def validate_agent_name(name: str | None) -> str | None: ...

# 配置模型（pydantic）
class AgentConfig(BaseModel):
    name: str
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None
    skills: list[str] | None = None  # None=全部 / []=无 / 列表=白名单

# 目录解析（per-user 优先 + legacy 只读回退）
def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path: ...

# 读取
def load_agent_config(name: str | None, *, user_id: str | None = None) -> AgentConfig | None: ...
def load_agent_soul(agent_name: str | None, *, user_id: str | None = None) -> str | None: ...
def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]: ...
```

`user_id` 都可选——缺省取 `get_effective_user_id()`（请求上下文，无鉴权回退 `"default"`）。

---

## 应用方法

### 创建一个自定义 agent（M15 setup_agent 工具，待落地）

用户在**引导回合**调 `setup_agent`，工具会：

1. `validate_agent_name(name)` 校验名字。
2. 往 `paths.user_agent_dir(user_id, name)` 写 `SOUL.md` + `config.yaml`。

```python
from deerflow.config.agents_config import validate_agent_name
from deerflow.config.paths import get_paths

name = validate_agent_name(user_supplied_name)  # 非法名在这就拦下
agent_dir = get_paths().user_agent_dir(user_id, name)
agent_dir.mkdir(parents=True, exist_ok=True)
(agent_dir / "SOUL.md").write_text(personality, encoding="utf-8")
(agent_dir / "config.yaml").write_text(yaml.safe_dump(config_dict), encoding="utf-8")
```

### 自更新（M15 update_agent 工具，待落地）

在**自定义 agent 的普通回合**里调 `update_agent`，工具读现有 config.yaml、部分更新、原子
写回（temp + rename）。

### lead agent 注入 SOUL（M17 custom-agent 分支，待落地）

```python
from deerflow.config.agents_config import load_agent_soul, load_agent_config

soul = load_agent_soul(agent_name, user_id=user_id)   # 拿人格
cfg = load_agent_config(agent_name, user_id=user_id)  # 拿工具/技能白名单
# soul 注入系统提示的 {soul} 段；cfg.tool_groups / cfg.skills 过滤工具与技能
```

### 列出某用户的全部 agent（管理界面）

```python
from deerflow.config.agents_config import list_custom_agents

agents = list_custom_agents(user_id="alice")  # [{name, description, model, ...}, ...]
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_agents_config.py（83 个 hermetic 测试）
```

测试约定：`DEER_FLOW_HOME` → `tmp_path`，agent 目录建临时盘不碰宿主；resolve 在前保证路径
相等断言成立（macOS tmp_path 经 `/var` → `/private/var` 符号链接）。

---

## 一个完整例子（per-user + legacy 回退）

假设 Alice 迁移前在 legacy 布局有个 `research` agent：

```
{base_dir}/agents/research/config.yaml   # legacy
{base_dir}/agents/research/SOUL.md
```

迁移后她在 per-user 又建了一个**新的** `research`：

```
{base_dir}/users/alice/agents/research/config.yaml   # per-user（新）
{base_dir}/users/alice/agents/research/SOUL.md
```

- `load_agent_config("research", user_id="alice")` → 读 **per-user**（优先）。
- `list_custom_agents(user_id="alice")` → 只返回**一个** `research`（per-user 覆盖 legacy，
  不重复）。
- Bob（没迁移）：`load_agent_config("research", user_id="bob")` → 读 **legacy**（per-user
  不存在）。

如果 Alice 的 per-user `research` 目录**只有 memory.json**（还没 setup_agent）：

- `resolve_agent_dir("research", user_id="alice")` → 回退 **legacy**（#3390：没 config.yaml
  不算 agent 目录），读到旧的 legacy 配置，不会读到空配置。

---

## 与其它模块的关系

```
config/paths
  └─ Paths.user_agent_dir / agent_dir / user_agents_dir / agents_dir（名称 .lower() 归一）
        ↑
config/agents_config（本模块）
  ├── AGENT_NAME_PATTERN / validate_agent_name ──┐
  ├── AgentConfig                                 │ 红线 #32 共用
  ├── resolve_agent_dir（per-user + legacy）       │
  ├── load_agent_config / load_agent_soul         │
  └── list_custom_agents                          │
        ↑                                          │
runtime/user_context (get_effective_user_id)       │
                                                   │
        ┌──────────────────────────────────────────┘
        ▼
M13 memory（用 AGENT_NAME_PATTERN + per-agent 存储：users/{uid}/agents/{name}/memory.json）
M15 setup_agent / update_agent 工具（写 SOUL.md + config.yaml 到 per-user 目录）
M17 lead_agent custom-agent 分支（注入 SOUL、按 tool_groups/skills 过滤）
```

- **上游**：`config/paths`（目录布局 + 名称小写归一）、`runtime/user_context`
  （`get_effective_user_id` 给 user_id 兜底）。
- **下游消费者**：M13 memory（`AGENT_NAME_PATTERN` + per-agent memory 路径，v1.2 起从本模块
  直接取，不再局部兜底）；M15 setup/update_agent 工具（读写设定）；M17 lead_agent
  custom-agent 分支（注入 SOUL + 工具/技能白名单）。
- **红线 #32**：`AGENT_NAME_PATTERN` 在 setup_agent / update_agent / memory storage / client
  共用；per-user 优先 + legacy 只读回退。

---

## 常见问题 / 排错

**Q：为什么 `CodeReviewer` 和 `codereviewer` 是同一个 agent？**
A：磁盘目录做了 `.lower()` 归一（`Paths.agent_dir` / `user_agent_dir`）——防 macOS APFS
大小写不敏感导致的碰撞。校验后的原名（含大小写）仍在 `AgentConfig.name` 保留。

**Q：per-user 目录存在但 `load_agent_config` 却报「not found」？**
A：那是个只有 `memory.json`、没有 `config.yaml` 的残缺目录（memory 系统首轮写入的）。
`resolve_agent_dir` 的 #3390 防御要求 `config.yaml` 才认 agent 目录——它会回退 legacy 或
返回 per-user 占位。你需要先 setup_agent 写 config.yaml，或检查 legacy 是否有该 agent。

**Q：config.yaml 里写了 `prompt_file`，加载会报错吗？**
A：不会。`load_agent_config` 在喂给 pydantic 前会剥掉所有不在 `AgentConfig.model_fields`
里的未知字段（向前兼容）。废弃字段静默忽略。

**Q：`skills` 不写和写成 `[]` 有什么区别？**
A：不写（`None`）= 加载**全部**启用的技能；`[]` = **禁用**全部技能。前者是「用默认」，
后者是「主动清空」。

**Q：默认 agent（`agent_name=None`）有 SOUL.md 吗？**
A：通常没有。`load_agent_soul(None)` 读 `{base_dir}/SOUL.md`（全局人格），没有就返回
`None`，lead agent 不注入 `{soul}` 段。默认 agent 靠系统提示模板本身，不需要 SOUL.md。

**Q：一个用户能影响另一个用户的 agent 吗？**
A：不能。per-user 隔离：Alice 的 agent 在 `users/alice/agents/`，Bob 的在
`users/bob/agents/`，路径完全分开。`list_custom_agents(user_id="alice")` 只看 Alice 的
per-user 目录 + 全局 legacy（legacy 是共享只读，但新写不进 legacy）。

**Q：为什么放在 `config/` 而不是 `agents/`？**
A：这是「配置加载层」——读写设定文件、校验名字，没有运行时逻辑。运行时（注入 SOUL、过滤
工具）在 M17 lead_agent。分层让「设定是什么」和「怎么用设定」解耦。
