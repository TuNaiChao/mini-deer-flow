# 17. agents_config.md — 自定义 agent（SOUL.md 人格 + per-user 隔离）

> 📝 重写于 2026-07-05 · 对照代码 commit ffc5e5d · **2026-07-05 复审**（更面向小白 + 加「实现差异 vs 上游 deer-flow 源码」）

> **一句话定位**：自定义 agent 让用户**造一个属于自己的 agent**——给它一份人格（SOUL.md）、
> 一份工具/技能白名单（config.yaml），之后每次和它对话都用这套设定。本模块负责**读写这套
> 设定文件** + 校验名字 + 按用户隔离存储。

> **配套代码**：[config/agents_config.py](../backend/packages/harness/deerflow/config/agents_config.py)（228 行）+ [config/paths.py](../backend/packages/harness/deerflow/config/paths.py)（目录布局 + 名称 `.lower()` 归一，:147-188）+ 消费方 [agents/lead_agent/agent.py](../backend/packages/harness/deerflow/agents/lead_agent/agent.py)（custom-agent 分支）+ [agents/lead_agent/prompt.py](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py)（`{soul}` 段）
> **配套测试**：[test/test_agents_config.py](../test/test_agents_config.py)（63 个 hermetic 测试，562 行；`DEER_FLOW_HOME` → tmp_path 不碰宿主）
> **参考**：deerflow-book [05-lead-agent.md](../deerflow-book/chapters/05-lead-agent.md)（覆盖消费方 `make_lead_agent` 工厂；本模块是它读的「配置层」，机制一致）
> 本文面向「刚接触 agent 定制 / 人格系统 / per-user 隔离的小白」。读完 [user_context.md](user_context.md)（懂了 user_id 三态）+ [config.md](config.md)（懂了配置系统）再看本篇最省事——自定义 agent 的存储就是「per-user 目录里两份文件」，名字校验 + per-user 隔离是它的两个核心约束。每个名词第一次出现都会解释。

---

## 学完能回答（learning outcomes）

1. 为什么需要自定义 agent？默认 lead agent（「公司前台」）缺什么？SOUL.md 和 config.yaml 各定义什么？
2. `skills` 字段的**三态**（`None` / `[]` / 列表）各是什么语义？为什么 `None` 和 `[]` 必须区分？
3. per-user 隔离怎么实现？Alice 和 Bob 同名 agent 为什么互不可见、互不覆盖？`user_id` 哪来？
4. legacy 只读回退为什么存在？**新写**走哪、**读**走哪？为什么不直接废弃 legacy？
5. agent 名字为什么要严格校验（`^[A-Za-z0-9-]+$`）？不校验会怎样（路径穿越 / shell 注入 / 编码）？`validate_agent_name` 被**哪些**调用方共用？
6. 为什么磁盘目录要 `.lower()` 归一？（macOS APFS 大小写不敏感→碰撞）原名大小写去哪了？
7. `resolve_agent_dir` 的 #3390 防御：为什么要求目录里**有 config.yaml** 才算 agent 目录？（memory 首轮写残缺目录 → 误读空配置）
8. `load_agent_config` 为什么喂给 pydantic 前要**剥未知字段**？（向前兼容废弃字段）配置层和运行时层（lead_agent）怎么分工？

---

## §1 为什么需要自定义 agent（痛点）

默认的 lead agent 是「通用型超级助手」：什么都能干，但没有个性、没有专属知识、工具集也固定。但很多场景你想要一个**专属 agent**：

- **代码审查员**：只读代码、用审查技能、说话严谨。
- **日报助手**：只写文件、用总结技能、语气简洁。
- **某团队的知识管家**：带特定人格（SOUL）、特定工具白名单、记住这个团队的事。

自定义 agent 就是让用户**自己定义**这么一个角色：一份 **SOUL.md** 写人格 / 价值观 / 行为约束，一份 **config.yaml** 写它能用哪些工具组、哪些技能、用什么模型。设定存好后，每次和这个 agent 对话，lead agent 会把 SOUL.md 注入系统提示、按 config.yaml 限制工具/技能（[lead_agent/agent.py:123](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L123)）。

**类比**：默认 agent 是「公司前台」（什么都能答）；自定义 agent 是「你私人招的专员」——你写好它的岗位说明（SOUL.md）和权限（config.yaml），它就按这套来。

---

## §2 零基础名词（先认这些词）

> 本篇假设你已读过 [user_context.md](user_context.md)（user_id 三态）+ [config.md](config.md)（配置系统）。这里补自定义 agent 相关的词。

- **SOUL.md**：一份 markdown，定义 agent 的「灵魂」——人格、价值观、行为约束、说话风格。lead agent 把它**注入系统提示**（包进 `<soul>` 标签，[prompt.py:715](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L715)）。文件名固定 `SOUL.md`（常量 `SOUL_FILENAME`，[agents_config.py:34](../backend/packages/harness/deerflow/config/agents_config.py#L34)）。
- **config.yaml**：一份 yaml，定义 agent 的「能干什么」——工具组白名单 / 技能白名单 / 模型 / 描述。`AgentConfig`（pydantic）是它的内存表示。
- **per-user 隔离**：每个用户的 agent 存在各自的 `users/{user_id}/agents/{name}/` 目录，互不影响。`user_id` 来自 `get_effective_user_id()`（无鉴权回退 `"default"`，见 [user_context.md](user_context.md)）。
- **legacy 只读回退**：旧的共享布局 `{base_dir}/agents/{name}/`，为兼容 per-user 隔离之前的安装保留——只读，新写一律走 per-user。
- **引导回合 / 普通回合**：`setup_agent` 工具只在**引导回合**可用（创建 agent）；`update_agent` 只在**自定义 agent 的普通回合**可用（自更新）。
- **pydantic / BaseModel**：Python 的数据校验库。写一个继承 `BaseModel` 的类、列好字段，pydantic 自动帮你**校验类型 + 转换 + 报错**。`AgentConfig`（§4.2）就是 pydantic 模型——config.yaml 读成 dict 喂给它，字段类型不对它会拦。
- **YAML**：一种「用缩进表示层级」的配置文件格式（比 JSON 好写、带注释）。config.yaml 就是 YAML。**类比**：YAML 像大纲笔记，缩进多少层就是第几级。
- **正则表达式（regex）/ `fullmatch`**：描述字符串模式的「通配符升级版」。`^[A-Za-z0-9-]+$` 意思是「从开头(`^`)到结尾(`$`)全是字母/数字/连字符」。`fullmatch` 要求**整串**都匹配（不是只匹配一段），校验更严。
- **原子写（temp + rename）**：写文件时先写到一个临时文件、写好再把临时文件**改名**成目标名。改名在操作系统层面是「瞬间完成」的，故读的一方要么看到旧版、要么看到完整新版，绝不会读到写一半的残缺文件。
- **路径穿越（path traversal）**：用 `../` 这种片段让路径「跳出」本该待的目录。如 agent 名 `../etc` 会让 `.../agents/../etc/` 指向 agent 目录之外。名字校验就是为了堵这个。

---

## §3 整体结构（两份文件 + 两层目录）

### 两份文件（一个自定义 agent = 一个目录里两份文件）

```
{agent_dir}/
├── SOUL.md        # 人格（注入系统提示的 <soul> 段）
└── config.yaml    # 能力（name/description/model/tool_groups/skills）
```

### 两层目录布局（per-user 优先 + legacy 只读回退）

```
{base_dir}/users/{user_id}/agents/{name}/     ← per-user（当前，新写都写这）
    SOUL.md
    config.yaml
{base_dir}/agents/{name}/                      ← legacy 共享（只读回退，旧安装）
    SOUL.md
    config.yaml
```

对应 [paths.py](../backend/packages/harness/deerflow/config/paths.py) 的方法：

| 方法 | 路径 | 用途 |
|------|------|------|
| `agents_dir`（[:168](../backend/packages/harness/deerflow/config/paths.py#L168)） | `{base_dir}/agents/` | legacy 根 |
| `agent_dir(name)`（[:175](../backend/packages/harness/deerflow/config/paths.py#L175)） | `{base_dir}/agents/{name.lower()}/` | legacy per-agent |
| `user_agents_dir(user_id)`（[:179](../backend/packages/harness/deerflow/config/paths.py#L179)） | `{base_dir}/users/{user_id}/agents/` | per-user 根 |
| `user_agent_dir(user_id, name)`（[:183](../backend/packages/harness/deerflow/config/paths.py#L183)） | `{base_dir}/users/{user_id}/agents/{name.lower()}/` | per-user per-agent |

注意：目录名一律 `.lower()` 归一（§6 详谈）。

### 文件结构

```
config/
├── paths.py          # Paths.user_agent_dir / agent_dir / user_agents_dir / agents_dir（名称 .lower() 归一）
└── agents_config.py  # 本模块：SOUL_FILENAME / AGENT_NAME_PATTERN / AgentConfig /
                      #   validate_agent_name / resolve_agent_dir / load_agent_config /
                      #   load_agent_soul / list_custom_agents
```

> **为什么放在 `config/` 而非 `agents/`？** 这是「配置加载」——读写设定文件、校验名字，没有运行时 agent 逻辑。运行时（注入 SOUL 到提示、按 tool_groups 过滤工具）在 [#25 agents.md](agents.md) 的 lead_agent。配置层只管「设定是什么、存在哪」，不关心「怎么用」。

---

## §4 核心概念

### 4.1 SOUL.md（人格文件）

一份 markdown，定义 agent 的「灵魂」。例如：

```markdown
你是「代码审查员」。你只关注代码质量，不回答与代码无关的问题。
语气要直接、给出可执行的改进建议，先夸后批。
```

lead agent 把它**注入系统提示**作为附加上下文（prompt 模板的 `{soul}` 条件段，[prompt.py:392](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L392)）。文件名固定 `SOUL.md`（常量 `SOUL_FILENAME`，不是任意名）。

### 4.2 config.yaml（能力配置）

```yaml
name: code-reviewer        # 名字（也可省略，从目录名推断）
description: 代码审查专家    # 列可用 agent 时给人看
model: deepseek-v3          # 可选：覆盖默认模型；省略 = 跟随全局
tool_groups: [file:read, bash]  # 可选：工具组白名单；省略 = 全部
skills: [review, lint]      # None=全部技能 / []=禁用 / 列表=只这些
```

`AgentConfig`（[agents_config.py:59](../backend/packages/harness/deerflow/config/agents_config.py#L59)）就是它的内存表示（pydantic BaseModel）。

### 4.3 skills 的三态（容易混）

`config.yaml` 里的 `skills` 字段有**三种**语义不同的取值，别搞混：

| 取值 | 含义 |
|------|------|
| 缺省（`None`） | 加载**全部**启用的技能（默认回退） |
| `[]`（显式空列表） | **禁用**全部技能（一个都不加载） |
| `["a", "b"]` | **白名单**：只加载列出的技能 |

为什么 `None` 和 `[]` 要区分？——`None` 是「我不关心，用默认」；`[]` 是「我明确要这个 agent 没有技能」。前者是「未设置」，后者是「主动清空」。

### 4.4 per-user 隔离

每个用户的 agent 存在各自的目录（[paths.py:183](../backend/packages/harness/deerflow/config/paths.py#L183)）。`user_id` 来自 `get_effective_user_id()`（无鉴权回退 `"default"`）。Alice 的 `code-reviewer` 和 Bob 的 `code-reviewer` 互不可见、互不覆盖。

### 4.5 legacy 只读回退（兼容旧安装）

per-user 隔离是后来才加的。**之前**的安装把所有 agent 放在共享的 `{base_dir}/agents/{name}/` 下。为了不破坏这些旧安装，`resolve_agent_dir` / `load_agent_config` / `load_agent_soul` 都**先查 per-user，查不到再查 legacy**。但**新写**（`setup_agent` / `update_agent` 工具）一律写 per-user，legacy 只读。

### 4.6 为什么名字要严格校验（AGENT_NAME_PATTERN）

agent 名字会拼进**文件系统路径**（`.../agents/{name}/`），所以必须校验，否则：

- `"../etc"` → 路径穿越，读写到 agent 目录之外。
- `"a/b"` → 多了一层目录。
- `"a b"`（空格）/ `"a\nb"`（换行）→ 文件名异常、shell 注入面。
- `"中文"` → 跨平台编码问题。

所以定了正则 `AGENT_NAME_PATTERN = ^[A-Za-z0-9-]+$`（[agents_config.py:38](../backend/packages/harness/deerflow/config/agents_config.py#L38)）：**只允许字母、数字、连字符**，至少一个字符。`validate_agent_name` 用 `fullmatch`（整串匹配）强校验，被 setup_agent / update_agent / memory storage / client **共用**——校验一次，处处安全。

合法名：`my-agent`、`agent123`、`CodeReviewer`、`A`。
非法名：`` ``（空）、`agent_name`（下划线）、`a/b`、`a.b`、`中文`、`../etc`。

---

## §5 代码走读

### 5.1 `validate_agent_name`：校验名字

```python
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")          # agents_config.py:38

def validate_agent_name(name: str | None) -> str | None:     # :41
    if name is None:           # None = 默认 agent，合法
        return None
    if not isinstance(name, str):
        raise ValueError(...)
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(...)
    return name                 # 通过校验，原样返回（保留大小写）
```

通过校验的原名（含大小写）原样返回——磁盘目录的小写归一在 `Paths` 里做（§6），原名保留在 `AgentConfig.name`。

### 5.2 `resolve_agent_dir`：per-user 优先 + legacy 回退（#3390 防御）

```python
def resolve_agent_dir(name: str, *, user_id=None) -> Path:    # agents_config.py:80
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()
    user_path = paths.user_agent_dir(effective_user, name)
    if user_path.exists() and (user_path / "config.yaml").exists():   # 要 config.yaml 才认
        return user_path
    legacy_path = paths.agent_dir(name)
    if legacy_path.exists() and (legacy_path / "config.yaml").exists():
        return legacy_path
    return user_path    # 都不存在 → 返回 per-user 路径，让调用方写进新布局
```

**#3390 防御**（源码注释标 #3390，[agents_config.py:89](../backend/packages/harness/deerflow/config/agents_config.py#L89)）：判断「某目录是不是真 agent 目录」时，不只看目录存不存在，还要看里面**有没有 `config.yaml`**。为什么？——memory 系统（[#18 memory.md](memory.md)）在首轮对话时会**提前**给某 agent 建一个 per-user 目录，但那时只有 `memory.json`、还没有 `config.yaml`（用户还没 setup_agent）。如果下一回合 `resolve_agent_dir` 只看「目录存在」就返回它，`load_agent_config` 会读到「空配置」。要求 config.yaml 才认 → memory 写入的残缺目录被跳过，正确回退 legacy 或返回 per-user 占位。

### 5.3 `load_agent_config`：剥未知字段（向前兼容）

```python
def load_agent_config(name, *, user_id=None) -> AgentConfig | None:   # agents_config.py:114
    if name is None: return None
    name = validate_agent_name(name)
    agent_dir = resolve_agent_dir(name, user_id=user_id)
    # ... 读 config.yaml → data dict ...
    if "name" not in data: data["name"] = name      # 无 name 字段用目录名兜底
    known_fields = set(AgentConfig.model_fields.keys())   # 剥未知字段
    data = {k: v for k, v in data.items() if k in known_fields}
    return AgentConfig(**data)
```

**剥未知字段**（[agents_config.py:155](../backend/packages/harness/deerflow/config/agents_config.py#L155)）：旧版本的 config.yaml 可能有现已废弃的字段（如 legacy 的 `prompt_file`）。不剥的话 pydantic 构造会因未知字段失败。剥掉后旧配置仍能加载，**向前兼容**。

### 5.4 `load_agent_soul`：默认 agent 读 base_dir

```python
def load_agent_soul(agent_name, *, user_id=None) -> str | None:   # agents_config.py:161
    if agent_name:
        agent_dir = resolve_agent_dir(agent_name, user_id=user_id)
    else:
        agent_dir = get_paths().base_dir    # None = 默认 agent，读 {base_dir}/SOUL.md
    soul_path = agent_dir / SOUL_FILENAME
    if not soul_path.exists(): return None
    content = soul_path.read_text(encoding="utf-8").strip()
    return content or None    # 空文件/纯空白 → None
```

`load_agent_soul(None)` 不报错——`None` 表示「默认 agent」，读 `{base_dir}/SOUL.md`（全局人格），没有就返回 `None`。空文件 / 纯空白也返回 `None`（`content.strip() or None`）——避免把空字符串当人格注入提示。

### 5.5 `list_custom_agents`：并集 + per-user 覆盖

```python
def list_custom_agents(*, user_id=None) -> list[AgentConfig]:   # agents_config.py:184
    # 扫 per-user 根 + legacy 根，返回并集
    for root in (user_root, legacy_root):
        for entry in sorted(root.iterdir()):
            if entry.name in seen: continue      # per-user 先扫进 seen，legacy 同名跳过
            if not (entry / "config.yaml").exists(): continue   # 要 config.yaml 才算
            # ... load_agent_config ...
            seen.add(entry.name)
    agents.sort(key=lambda a: a.name)    # 按 name 升序，稳定
```

同名时 **per-user 覆盖 legacy**（per-user 先扫、进 `seen`，legacy 同名跳过）。这保证：用户迁移到 per-user 后，旧的 legacy 同名 agent 不再重复出现；但用户没迁移的 legacy agent 仍可见。解析失败的目录（config.yaml 坏）记 warning 跳过，**不抛**——一个坏 agent 不该让整个列表接口挂掉。

### 5.6 lead_agent 注入分支（消费方）

`make_lead_agent` 的 custom-agent 分支（[lead_agent/agent.py:123](../backend/packages/harness/deerflow/agents/lead_agent/agent.py#L123)）消费本模块：

```python
agent_name = validate_agent_name(cfg.get("agent_name"))         # :123 校验名字
agent_config = load_agent_config(agent_name) if not is_bootstrap else None  # :125 加载 config
# ...
"tool_groups": agent_config.tool_groups if agent_config else None,  # :164 工具组白名单
# ...
extra_tools = [update_agent] if agent_name else []             # :209 自定义 agent 才有 update_agent
raw_tools = get_available_tools(model_name=..., groups=agent_config.tool_groups if agent_config else None, ...)  # :211 过滤工具
```

SOUL.md 经 prompt 模板的 `{soul}` 段注入（[prompt.py:715](../backend/packages/harness/deerflow/agents/lead_agent/prompt.py#L715) `get_agent_soul` 读 SOUL.md 包进 `<soul>` 标签）。详见 [#25 agents.md](agents.md)。

### 5.7 数据流：从 setup_agent 到下一回合注入

把上面串起来——Alice 第一次创建一个 `code-reviewer` agent，然后开新对话用它：

```
【引导回合】Alice 调 setup_agent(name="code-reviewer", soul="你是代码审查员…", config={tool_groups:[file:read], skills:[review]})
   │
   ① validate_agent_name("code-reviewer") → 通过（^[A-Za-z0-9-]+$）
   ② paths.user_agent_dir("alice", "code-reviewer") → {base_dir}/users/alice/agents/code-reviewer/
   ③ 写 SOUL.md + config.yaml 进该目录（原子写：temp + rename）
   │
【下一回合】Alice 开新对话，lead agent 装配
   │
   ④ make_lead_agent custom-agent 分支：load_agent_config("code-reviewer", user_id="alice")
        resolve_agent_dir → per-user 目录（有 config.yaml，#3390 认）
        读 config.yaml → 剥未知字段 → AgentConfig(tool_groups=[file:read], skills=[review])
   ⑤ load_agent_soul("code-reviewer") → 读 SOUL.md 内容
   ⑥ soul 经 prompt {soul} 段包进 <soul> 标签注入系统提示；cfg.tool_groups / cfg.skills 过滤工具与技能
   │
   ⑦ agent 用「代码审查员」人格 + 只能 file:read 工具组 + 只能 review 技能 跑
```

**两个关键点**：① **写**（步①-③）和**读**（步④-⑥）用同一套名字校验 + 同一套目录解析，故写在哪、读时就从哪找；② per-user 目录是「Alice 专属」——Bob 走同样流程会解析到 `users/bob/agents/code-reviewer/`（不存在 → 回退 legacy 或占位），互不干扰。

---

## §6 设计权衡（为什么这么设计）

| 权衡 | 选择 | 理由 |
|------|------|------|
| **名字严格校验** | `^[A-Za-z0-9-]+$` + `fullmatch` | 名字拼进文件系统路径，防穿越 / shell 注入 / 编码问题；setup/update_agent + memory + client 共用 |
| **磁盘目录 `.lower()` 归一** | `agent_dir` / `user_agent_dir` 都 `.lower()` | macOS APFS 默认大小写不敏感——`CodeReviewer` 与 `codereviewer` 落进同一目录会互相覆盖；小写归一后映射一致 |
| **原名保留大小写** | 校验后原名原样返回，进 `AgentConfig.name` | 用户可见名保留可读性；只有磁盘目录名小写 |
| **#3390：要 config.yaml 才认** | `resolve_agent_dir` 检查 config.yaml 存在 | memory 首轮写残缺目录（只有 memory.json），不防就会误读空配置 |
| **剥未知字段** | 喂 pydantic 前过滤 `model_fields` | 旧 config.yaml 有废弃字段（如 prompt_file），不剥则 pydantic 构造失败；向前兼容 |
| **load_soul 默认读 base_dir** | `None` → `{base_dir}/SOUL.md` | 默认 agent 无专属目录，读全局人格；空→None 避免空字符串注入提示 |
| **list 并集 + per-user 覆盖** | per-user 先扫进 seen，legacy 同名跳过 | 迁移后同名不重复；未迁移的 legacy 仍可见；坏目录 warning 跳过不抛 |
| **per-user 优先 + legacy 只读回退** | 读先 per-user 后 legacy；新写只 per-user | 兼容旧安装不破坏；新数据进隔离布局 |
| **配置层 vs 运行时层分离** | agents_config 在 `config/`，注入在 `lead_agent` | 「设定是什么/存在哪」与「怎么用设定」解耦 |

---

## §7 配置用法

### 创建一个自定义 agent（setup_agent 工具，引导回合）

用户在**引导回合**调 `setup_agent`，工具会：① `validate_agent_name(name)` 校验名字；② 往 `paths.user_agent_dir(user_id, name)` 写 `SOUL.md` + `config.yaml`。

```python
from deerflow.config.agents_config import validate_agent_name
from deerflow.config.paths import get_paths

name = validate_agent_name(user_supplied_name)  # 非法名在这就拦下
agent_dir = get_paths().user_agent_dir(user_id, name)
agent_dir.mkdir(parents=True, exist_ok=True)
(agent_dir / "SOUL.md").write_text(personality, encoding="utf-8")
(agent_dir / "config.yaml").write_text(yaml.safe_dump(config_dict), encoding="utf-8")
```

### 自更新（update_agent 工具，自定义 agent 普通回合）

在**自定义 agent 的普通回合**里调 `update_agent`，工具读现有 config.yaml、部分更新、原子写回（temp + rename）。

### lead agent 注入（custom-agent 分支）

```python
from deerflow.config.agents_config import load_agent_soul, load_agent_config

soul = load_agent_soul(agent_name, user_id=user_id)   # 拿人格（None 则不注入）
cfg = load_agent_config(agent_name, user_id=user_id)  # 拿工具/技能白名单
# soul 经 prompt {soul} 段注入；cfg.tool_groups / cfg.skills 过滤工具与技能
```

### 列出某用户的全部 agent（管理界面）

```python
from deerflow.config.agents_config import list_custom_agents

agents = list_custom_agents(user_id="alice")  # [{name, description, model, ...}, ...]
```

### 跑测试

```bash
cd backend && make test    # 含 test/test_agents_config.py（63 个 hermetic 测试）
```

测试约定（[test_agents_config.py](../test/test_agents_config.py)）：`DEER_FLOW_HOME` → `tmp_path`，agent 目录建临时盘不碰宿主；resolve 在前保证路径相等断言成立（macOS tmp_path 经 `/var` → `/private/var` 符号链接）。

---

### 一个完整例子（per-user + legacy 回退）

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
- `list_custom_agents(user_id="alice")` → 只返回**一个** `research`（per-user 覆盖 legacy，不重复）。
- Bob（没迁移）：`load_agent_config("research", user_id="bob")` → 读 **legacy**（per-user 不存在）。

如果 Alice 的 per-user `research` 目录**只有 memory.json**（还没 setup_agent）：

- `resolve_agent_dir("research", user_id="alice")` → 回退 **legacy**（#3390：没 config.yaml 不算 agent 目录），读到旧的 legacy 配置，不会读到空配置。

---

## §8 与其它模块的关系

```
config/paths (#3：目录布局 + 名称 .lower() 归一)
  └─ Paths.user_agent_dir / agent_dir / user_agents_dir / agents_dir
        ↑
config/agents_config（本模块 #17）
  ├── SOUL_FILENAME / AGENT_NAME_PATTERN / validate_agent_name
  ├── AgentConfig（pydantic）
  ├── resolve_agent_dir（per-user + legacy，#3390 要 config.yaml）
  ├── load_agent_config（剥未知字段）/ load_agent_soul（默认读 base_dir）
  └── list_custom_agents（并集 + per-user 覆盖）
        ↑                                          ↑
runtime/user_context (#5：get_effective_user_id)   │ AGENT_NAME_PATTERN 共用
                                                   │
        ┌──────────────────────────────────────────┘
        ▼
#18 memory（用 AGENT_NAME_PATTERN + per-agent 存储：users/{uid}/agents/{name}/memory.json）
#22 tools（setup_agent / update_agent 工具：写 SOUL.md + config.yaml 到 per-user 目录）
#25 agents（lead_agent custom-agent 分支：注入 SOUL + 按 tool_groups/skills 过滤）
```

- **上游**：[#3 config.md](config.md) 的 `config/paths`（目录布局 + 名称小写归一）、[#5 user_context.md](user_context.md) 的 `get_effective_user_id`（给 user_id 兜底）。
- **下游消费者**：[#18 memory.md](memory.md)（`AGENT_NAME_PATTERN` + per-agent memory 路径）、[#22 tools.md](tools.md) 的 setup/update_agent 工具（读写设定）、[#25 agents.md](agents.md) 的 lead_agent custom-agent 分支（注入 SOUL + 工具/技能白名单）。
- **不 port**：上游 Gateway 专属的 `config/agents_api_config.py`（REST API 层配置）——mini 不做 Gateway REST 层。

---

## §9 实现差异（vs 上游 deer-flow 源码）

> 对照基线 = `deer-flow/backend/packages/harness/deerflow/config/agents_config.py` + `config/paths.py`（agent 目录部分）。已**剥 docstring/comment 后**判逻辑差。结论：**`agents_config.py` 是上游的忠实移植**——0 逻辑差（唯一差是 mini 顶部加 `from __future__ import annotations`）；agent 目录解析也一致。真差异主要在共享的 `paths.py` 广泛简化。

### 9.1 一致的部分

| 维度 | 上游 deer-flow | mini |
|---|---|---|
| `agents_config.py` 全部函数（validate_agent_name / resolve_agent_dir / load_agent_config / load_agent_soul / list_custom_agents） | 有 | **0 逻辑差** |
| `AGENT_NAME_PATTERN` / `SOUL_FILENAME` 常量 | 有 | **相同** |
| `AgentConfig`（pydantic 字段） | 有 | **相同** |
| #3390 防御（要 config.yaml 才认 agent 目录） | 有 | **相同** |
| 剥未知字段 / `.lower()` 归一 / per-user+legacy 回退 | 有 | **相同** |
| `paths.py` 的 `agent_dir` / `user_agent_dir` 解析 | 有 | **一致** |

### 9.2 mini 砍的 / 简化的

- **`paths.py` 广泛简化（245 行差，但多与 agent 目录无关）**：上游 `paths.py` 多了一组 **ACP workspace 路径**（`acp_workspace_dir` / `host_acp_workspace_dir`——同 [#13 sandbox.md](sandbox.md) / [#14 aio_sandbox.md](aio_sandbox.md) 的 ACP 简化）、一组 **host_sandbox 宿主侧路径**（`host_sandbox_user_data_dir` / `host_sandbox_work_dir` 等）、以及 **user_id sanitize**（`make_safe_user_id` / `prepare_user_dir_for_raw_id` / `_validate_user_id`）。mini 把这些砍掉（mini 不 port ACP、沙箱宿主路径层更薄、user_id 在别处兜底）。**agent 目录解析本身不受影响**（`agent_dir` / `user_agent_dir` 两侧一致）。
- **`config/agents_api_config.py`（Gateway REST 层配置）**：上游有，mini 不 port（mini 不做 Gateway REST 层，见 §8）。

### 9.3 mini 新增的（paths.py）

- mini `paths.py` 多了**项目发现类**方法（`find_project_root` / `get_config_file` / `get_env_file` / `project_root` / `runtime_home`）——帮 mini 定位本地 config / .env / 运行主目录（mini 走 `langgraph dev` 本地跑，需要这些自发现逻辑；上游经 Gateway 部署，路径由部署配置给定）。

### 9.4 一句话总结

agents_config 本身的设计原则是「**忠实移植**」：校验 / 解析 / 加载 / 列举 / 常量与上游 deer-flow **完全一致**（`agents_config.py` 0 逻辑差）。差异不在本模块，而在共享的 `paths.py`——mini 砍掉 ACP workspace / host_sandbox / user_id sanitize 那批路径方法（同 #13/#14 的简化模式），加了项目发现类方法。读完 mini 这篇，迁到上游 agents_config 几乎零认知差。

---

## §10 常见问题 / 排错

**Q：为什么 `CodeReviewer` 和 `codereviewer` 是同一个 agent？**
A：磁盘目录做了 `.lower()` 归一（[paths.py:177](../backend/packages/harness/deerflow/config/paths.py#L177) / [:188](../backend/packages/harness/deerflow/config/paths.py#L188)）——防 macOS APFS 大小写不敏感导致的碰撞。校验后的原名（含大小写）仍在 `AgentConfig.name` 保留。

**Q：per-user 目录存在但 `load_agent_config` 却报「not found」？**
A：那是个只有 `memory.json`、没有 `config.yaml` 的残缺目录（memory 系统首轮写入的）。`resolve_agent_dir` 的 #3390 防御要求 `config.yaml` 才认 agent 目录——它会回退 legacy 或返回 per-user 占位。你需要先 setup_agent 写 config.yaml，或检查 legacy 是否有该 agent。

**Q：config.yaml 里写了 `prompt_file`，加载会报错吗？**
A：不会。`load_agent_config` 在喂给 pydantic 前会剥掉所有不在 `AgentConfig.model_fields` 里的未知字段（向前兼容，[agents_config.py:155](../backend/packages/harness/deerflow/config/agents_config.py#L155)）。废弃字段静默忽略。

**Q：`skills` 不写和写成 `[]` 有什么区别？**
A：不写（`None`）= 加载**全部**启用的技能；`[]` = **禁用**全部技能。前者是「用默认」，后者是「主动清空」。

**Q：默认 agent（`agent_name=None`）有 SOUL.md 吗？**
A：通常没有。`load_agent_soul(None)` 读 `{base_dir}/SOUL.md`（全局人格），没有就返回 `None`，lead agent 不注入 `{soul}` 段。默认 agent 靠系统提示模板本身，不需要 SOUL.md。

**Q：一个用户能影响另一个用户的 agent 吗？**
A：不能。per-user 隔离：Alice 的 agent 在 `users/alice/agents/`，Bob 的在 `users/bob/agents/`，路径完全分开。`list_custom_agents(user_id="alice")` 只看 Alice 的 per-user 目录 + 全局 legacy（legacy 是共享只读，但新写不进 legacy）。

**Q：为什么放在 `config/` 而不是 `agents/`？**
A：这是「配置加载层」——读写设定文件、校验名字，没有运行时逻辑。运行时（注入 SOUL、过滤工具）在 [#25 agents.md](agents.md) 的 lead_agent。分层让「设定是什么」和「怎么用设定」解耦。

**Q：非法名字（如 `a/b`、`中文`）在哪被拦下？**
A：`validate_agent_name` 的 `AGENT_NAME_PATTERN.fullmatch`（[agents_config.py:54](../backend/packages/harness/deerflow/config/agents_config.py#L54)）。它被 setup_agent / update_agent / memory storage / client 共用——任何写 agent 目录的路径都先过这一关，校验一次处处安全。

---

## §11 小结

自定义 agent 让用户造一个属于自己的 agent：一份 SOUL.md（人格）+ 一份 config.yaml（能力白名单），存在 per-user 目录。本模块（[agents_config.py](../backend/packages/harness/deerflow/config/agents_config.py)）是**配置加载层**，只管「设定是什么、存在哪」：

- **校验**：`AGENT_NAME_PATTERN`（`^[A-Za-z0-9-]+$`）+ `fullmatch`，防路径穿越/注入，多模块共用。
- **解析**：`resolve_agent_dir` per-user 优先 + legacy 只读回退，#3390 要求 config.yaml 才认（防 memory 残缺目录误读空配置）。
- **加载**：`load_agent_config` 剥未知字段向前兼容；`load_agent_soul` 默认 agent 读 base_dir、空→None。
- **列举**：`list_custom_agents` 并集 + per-user 覆盖 legacy + 坏目录 warning 跳过。
- **归一**：磁盘目录 `.lower()` 防大小写碰撞，原名保留在 `AgentConfig.name`。

运行时（注入 SOUL、按 tool_groups/skills 过滤工具）在 [#25 agents.md](agents.md) 的 lead_agent custom-agent 分支。配置层与运行时层分离，让「设定是什么」和「怎么用设定」解耦。

> 上一篇：[#16 tracing.md](tracing.md)（链路追踪——图根注入回调 + Langfuse 元数据）
> 下一篇：[#18 memory.md](memory.md)（记忆系统——本模块 AGENT_NAME_PATTERN + per-agent 存储的下游消费者）
