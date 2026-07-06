# 19. skills.md — 技能系统（SKILL.md 协议 / 发现 / 激活 / 安装 / allowed-tools 收紧）

> **重写日期**：2026-07-05。**对照代码**：`backend/packages/harness/deerflow/skills/`（12 文件，1354 行）。

> **一句话定位**：技能让 agent 复用「特定场景的操作流程」——把一段「希望 agent 遵循的步骤」沉淀成一份 SKILL.md 文件，而不是每次在对话里手写。本模块负责**发现 / 解析 / 按需激活 / 安全安装**技能，并用 `allowed-tools` 收紧技能激活时的工具集。

> **先读谁最省事**：[sandbox.md](sandbox.md)（懂工具与沙箱）+ [memory.md](memory.md)（懂「把内容注入模型调用」）。读完后，技能就是「一个目录 + 一份 SKILL.md」，激活时把 SKILL.md 注入当次模型调用，像记忆注入一样——只是技能是**人写的操作流程**，记忆是**LLM 抽取的用户事实**。

---

## §1 学完这篇你能回答什么（learning outcomes · 面试视角）

1. **「技能（skill）和记忆（memory）有什么区别？为什么这么分？」** —— 技能是关于**任务**的复用流程（人工写、显式激活）；记忆是关于**用户**的事实（LLM 抽、自动积累）。能讲清两者的触发、生命周期、注入时机为何不同。
2. **「让 agent 安装用户上传的第三方『技能包』，安全上有哪些攻击面？怎么防？」** —— zip 炸弹 / symlink 逃逸 / 路径穿越 / 恶意 SKILL.md prompt 注入。能讲清 512MB 上限、跳 symlink、`is_relative_to` 校验、LLM 审查 + 保守回退。
3. **「怎么把第三方写的内容安全地嵌进发给模型的提示？不能被 prompt 注入。」** —— `html.escape` 转义动态内容 + XML 结构包裹，让技能正文只能当文本看，不能逃逸当指令。
4. **「进程级单例在冷启动并发下会有什么竞态？怎么修？」** —— 多请求同时首次调都看到 `None`，各构一份。能讲清双重检查锁（double-checked locking）、为什么选「锁内构造」而非「锁外构造再丢败者」（无 teardown 钩子）。
5. **「技能怎么影响 agent 的工具集？`allowed-tools` 不写 vs 写 `[]` 有什么区别？」** —— 并集收紧语义；None=全放行（legacy），`[]`=禁全部，一旦有声明就按所有声明并集过滤。
6. **「怎么把扫盘 IO 从请求路径上挪走，又不读到过期数据？」** —— 进程级缓存 + daemon 线程后台刷新（miss 立即返空、下次读到预热结果）+ enabled 状态每次重读（他进程改动立即生效）。

---

## §2 零基础先读：名词解释

### §2.1 计算机基础层（不熟这些先看这段）

| 名词 | 一句话解释 |
|---|---|
| **frontmatter** | 文件开头一段用 `---` 围起来的元数据块。SKILL.md 用它存 name/description/allowed-tools 等「配置」，正文才是给模型看的操作指南。 |
| **YAML** | 一种缩进表示嵌套的配置格式（比 JSON 好写）。frontmatter 里用的就是它，`yaml.safe_load` 解析成 Python dict。 |
| **ZIP / 归档** | 把多个文件压成一个 `.zip` 文件。`.skill` 文件本质就是个 ZIP——一个技能目录的打包。 |
| **zip 炸弹** | 一个很小的 ZIP，解压后膨胀成巨大文件（如 42KB 解出 4.5PB），撑爆磁盘/内存的攻击。防御=强制总解压上限。 |
| **symlink（符号链接）** | 一种「快捷方式」文件，它指向另一个路径。危险在于：归档里放个 symlink 指向 `/etc/passwd`，解压后读它就绕过权限读到系统文件。 |
| **路径穿越（path traversal）** | 用 `../` 或绝对路径，让「本应落在目录内」的文件写到目录外（如 zip 成员叫 `../../etc/cron.d/evil`）。 |
| **文件权限位** | 类 Unix 系统给每个文件的三组 rwx 权限（属主/组/其他）。`0o555`=`r-xr-xr-x`（目录只读）、`0o444`=`r--r--r--`（文件只读）、`0o700`=`rwx------`（仅属主）。本模块用权限位把技能目录收紧成「沙箱只读」。 |
| **原子操作** | 「要么全做、要么像没做过」的操作。本模块的「原子搬入」=先预占目标目录、再搬内容、失败回滚清理，保证不留半截目录。 |
| **daemon 线程** | 「守护线程」——进程退出时不等它跑完直接丢下。本模块用它后台刷新技能缓存（best-effort，挂了不影响主流程）。 |
| **双重检查锁（double-checked locking）** | 单例模式防并发竞态的经典手法：先无锁快速检查→加锁→再检查一次（防止多个线程都过了第一道检查）。 |
| **模板方法（template method）** | 父类定好「算法骨架」（步骤顺序），把其中「介质相关」的几步声明成抽象方法，子类只填那几步。本模块 `SkillStorage.load_skills` 就是模板方法。 |
| **ABC（抽象基类）** | 一个「只定接口、不写实现」的父类。`SkillStorage(ABC)` 定抽象方法，`LocalSkillStorage` 是它的本地 FS 实现。 |
| **反射（reflection）** | 程序在运行时「按字符串名字」加载一个类。本模块 `resolve_class("...LocalSkillStorage")` 用反射造 storage，换后端只改配置字符串。 |
| **lru_cache** | 「最近最少使用」缓存装饰器——同样的输入直接返回缓存结果，不重算。本模块用它缓存渲染好的技能提示段。 |
| **html.escape** | 把 `<`、`>`、`&` 等特殊字符转义成 `&lt;` 等，让文本不会被当成 HTML/XML 结构。本模块用它防技能内容注入指令。 |

### §2.2 本模块名词

| 名词 | 解释 |
|---|---|
| **技能（skill）** | 一个目录 + 一份 SKILL.md，代表某场景的操作流程。分 `public`（内置只读）和 `custom`（用户可编辑）。 |
| **常驻注入** | 系统提示里列出可用技能名，agent 判断匹配时自己 `read_file` 读 SKILL.md（渐进加载）。 |
| **按需激活** | 用户输 `/skill-name <任务>`，运行时直接把该 SKILL.md 全文注入当次模型调用。 |
| **allowed-tools** | 技能 frontmatter 里声明的工具白名单，激活时收紧模型的可用工具集。 |
| **slash 保留字** | `/new` `/help` `/memory` `/models` `/status` `/bootstrap` 是控制命令，不能当技能名。 |
| **安全审查（scan）** | 用 LLM 审查技能内容，分类 `allow`/`warn`/`block`，模型不可用时保守回退 `block`。 |

---

## §3 整体结构：它在系统里的位置

技能系统有**四条触发路径**，都汇聚到 `get_or_new_skill_storage()` 这个单例入口，再分流到各自的消费者：

```
                         ┌─────────────────────────────────────────┐
   config.yaml           │ config/skills_config.py (SkillsConfig)   │
   extensions_config.json│   path / container_path / get_skills_path()
                         │ config/extensions_config.py              │
                         │   is_skill_enabled() ← enabled_skills    │
                         └────────────────────┬────────────────────┘
                                              │
                                              ▼
   ┌─────────────┐   ┌────────────────────────────────────────────┐
   │ lead_agent/  │   │ skills/storage/__init__.py  ← 单例入口       │
   │ prompt.py    │──▶│   get_or_new_skill_storage()  双重检查锁    │
   │ (常驻注入)   │   │     └─反射→ LocalSkillStorage              │
   └─────────────┘   └────────────────────┬─────────────────────────┘
   ┌─────────────┐                        │
   │ subagents/  │─── get_or_new ─────────┤
   │ executor    │                        │
   └─────────────┘                        ▼
   ┌─────────────────────────┐   ┌──────────────────────────────┐
   │ SkillActivationMiddleware│   │ skill_storage.py (ABC 模板)   │
   │ (按需激活 /skill-name)   │   │   load_skills() 扫 public+    │
   │ ① parse_slash_skill_     │   │   custom，合并 enabled 状态   │
   │   reference (严格语法)   │   │   validate_skill_name /       │
   │ ② resolve_slash_skill    │◀──│   validate_relative_path /    │
   │   (启用+白名单内)        │   │   ensure_safe_support_path    │
   │ ③ _read_skill_content    │   └──────────────────────────────┘
   │   (穿越拒绝+html.escape) │   ┌──────────────────────────────┐
   │ ④ 注入隐藏 HumanMessage  │   │ parser.py / validation.py    │
   └─────────────────────────┘   │   parse_skill_file (YAML fm)  │
                                 └──────────────────────────────┘
   ┌─────────────────────────┐   ┌──────────────────────────────┐
   │ installer.py (.skill ZIP)│   │ tool_policy.py               │
   │ ① safe_extract (穿越/    │   │   filter_tools_by_skill_     │──▶ agent 工具集收紧
   │   symlink/炸弹防御)      │   │   allowed_tools              │
   │ ② _validate_frontmatter  │   └──────────────────────────────┘
   │ ③ scan_archive (LLM 审)  │
   │ ④ _move_staged (原子搬入)│
   └───────────┬─────────────┘
               │ 依赖
               ▼
   permissions.py (0o555/0o444 沙箱只读) + security_scanner.py (allow/warn/block)
        ↑ create_chat_model（审查模型，独立调用方）
```

**四个文件的职责切分**（为什么这么拆见 [§9 设计动机](#9-设计动机分析为什么这么设计作用好处)）：

```
skills/
├── __init__.py            # 导出公共 API（比上游多导出几个）
├── types.py               # Skill dataclass + SkillCategory + SKILL_MD_FILE
├── parser.py              # parse_skill_file（YAML frontmatter）+ parse_allowed_tools
├── validation.py          # _validate_skill_frontmatter（命名约定 / 未知 key / 长度）
├── slash.py               # parse/resolve slash skill + RESERVED_SLASH_SKILL_NAMES（严格语法 + 保留字）
├── tool_policy.py         # allowed_tool_names_for_skills + filter_tools_by_skill_allowed_tools（白名单收紧）
├── permissions.py         # make_skill_*_sandbox_readable（目录 0o555 / 文件 0o444，跳 symlink）
├── security_scanner.py    # scan_skill_content（LLM allow/warn/block + 容错解析 + 保守回退）
├── installer.py           # .skill ZIP 安装（穿越/symlink/zip 炸弹防御 + LLM 审 + 原子搬入 + 异常类）
└── storage/
    ├── __init__.py        # get_or_new_skill_storage（单例 + 反射工厂 + 双重检查锁）+ reset_skill_storage
    ├── skill_storage.py   # SkillStorage ABC（load_skills 模板方法 + 路径校验 + 名称校验）
    └── local_skill_storage.py  # LocalSkillStorage（本地 FS 实现）

（接入点，不在本包内）
agents/middlewares/skill_activation_middleware.py  # SkillActivationMiddleware（注入 + 幂等 + 穿越拒绝 + html.escape）
agents/lead_agent/prompt.py     # get_skills_prompt_section + 后台刷新缓存 + clear/refresh
config/skills_config.py         # SkillsConfig（path / container_path / get_skills_path）
config/extensions_config.py     # ExtensionsConfig.is_skill_enabled
config/skill_evolution_config.py  # SkillEvolutionConfig（enabled + moderation_model_name）
```

**面试概念地图**：本篇对应「可扩展人格 / 技能系统」「安全执行 / prompt 注入防御」「并发设计（单例竞态）」三个面试常考点。`deerflow-book` 的 `17-skills-system.md` / `18-custom-skills.md` 是可选概念预读（借它自顶向下的讲法，实现看本篇代码）。

---

## §4 核心概念：一份技能长什么样

一个技能 = **一个目录 + 一份 SKILL.md**：

```
skills/public/example/        ← 技能目录
├── SKILL.md                  ← 主文件（必需）
├── references/               ← 可选：参考资源
├── templates/                ← 可选：模板
└── scripts/                  ← 可选：脚本
```

SKILL.md = **YAML frontmatter**（`---` 围栏）+ **正文**（给模型看的操作指南）：

```markdown
---
name: example                          # 必填，hyphen-case（小写+数字+连字符），≤64 字符
description: 示例技能，演示协议          # 必填，≤1024 字符，无尖括号
license: MIT                           # 可选
allowed-tools:                         # 可选：收紧该技能激活时的工具白名单
  - bash
  - read_file
  - write_file
metadata: {}                           # 可选
compatibility: ">=1.0"                 # 可选
version: "1.0"                         # 可选
author: someone                        # 可选
---

# 示例技能

这里是给模型看的操作指南正文……
```

**内存表示**是 [Skill](../backend/packages/harness/deerflow/skills/types.py#L25) dataclass（name/description/license/skill_dir/skill_file/relative_path/category/allowed_tools/enabled）。`allowed_tools` 三态是关键：`None`=不限制（legacy 全放行）、`[]`=不允许任何工具、给列表=只允许这些（白名单）。

两个类别（[SkillCategory](../backend/packages/harness/deerflow/skills/types.py#L14)）：
- **public**（`skills/public/`）：平台内置技能，**只读**。
- **custom**（`skills/custom/`）：用户自建技能，可编辑 / 删除。

`get_container_path()` [types.py:49](../backend/packages/harness/deerflow/skills/types.py#L49) 把宿主路径翻译成沙箱容器内路径（`/mnt/skills/public/<name>/SKILL.md`）——这是技能在沙箱里「看得到」的路径。

---

## §5 代码走读：重要函数逐个讲

### §5.1 types.py / parser.py / validation.py —— SKILL.md 协议层

**`parse_skill_file()`** [parser.py:63](../backend/packages/harness/deerflow/skills/parser.py#L63)：读文件 → 用正则抽 `---` 围栏之间的 YAML frontmatter → `yaml.safe_load` → 校验 name/description 非空 → 组 `Skill`。解析失败返 `None`（宽松，记 error 不抛）。`parse_allowed_tools()` [parser.py:41](../backend/packages/harness/deerflow/skills/parser.py#L41)：解析 `allowed-tools` 字段（None=未声明 / list=白名单）。

**`_validate_skill_frontmatter()`** [validation.py:18](../backend/packages/harness/deerflow/skills/validation.py#L18)：**安装前**校验 frontmatter 结构合法性——有没有 `---` 围栏、key 是否都在 `ALLOWED_FRONTMATTER_PROPERTIES` [validation.py:15](../backend/packages/harness/deerflow/skills/validation.py#L15) 内、name 是否 hyphen-case、长度上限。返回 `(ok, msg, name)`。

**为什么 parser 和 validation 分开？** parser 是「读懂 SKILL.md」、偏宽松（失败返 None）；validation 是「装入前把关节」、偏严格（给作者明确报错，含行号 + 引号提示）。失败语义不同。

### §5.2 slash.py —— `/skill-name` 语法解析

**`parse_slash_skill_reference()`** [slash.py:36](../backend/packages/harness/deerflow/skills/slash.py#L36)：用严格正则 `_SLASH_SKILL_RE` [slash.py:16](../backend/packages/harness/deerflow/skills/slash.py#L16)（`^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)`）解析 `/name <任务>`，**跳过 6 个保留控制命令**（`RESERVED_SLASH_SKILL_NAMES` [slash.py:14](../backend/packages/harness/deerflow/skills/slash.py#L14)：bootstrap/help/memory/models/new/status）。**`resolve_slash_skill()`** [slash.py:53](../backend/packages/harness/deerflow/skills/slash.py#L53)：在技能列表里找**启用且白名单内**的匹配。

### §5.3 tool_policy.py —— allowed-tools 工具白名单

**`allowed_tool_names_for_skills()`** [tool_policy.py:20](../backend/packages/harness/deerflow/skills/tool_policy.py#L20)：返回所有技能 `allowed-tools` 声明的**并集**——

```python
allowed: set[str] = set()
has_explicit_declaration = False
for skill in skills:
    if skill.allowed_tools is None:    # 没声明的技能跳过
        continue
    has_explicit_declaration = True
    allowed.update(skill.allowed_tools)
if not has_explicit_declaration:
    return None                         # 全都没声明 → None = legacy 全放行
return allowed
```

**关键语义**：None=全放行（无技能声明）；一旦有声明 → 只留声明并集，**没声明的技能不贡献工具**（而非禁用其他技能的限制）。`filter_tools_by_skill_allowed_tools()` [tool_policy.py:44](../backend/packages/harness/deerflow/skills/tool_policy.py#L44) 按白名单过滤工具列表。

### §5.4 storage/skill_storage.py —— SkillStorage ABC（模板方法）

`SkillStorage(ABC)` [skill_storage.py:27](../backend/packages/harness/deerflow/skills/storage/skill_storage.py#L27) 把存储分成两层：
- **模板方法流**（本基类写一遍）：`load_skills()` [skill_storage.py:180](../backend/packages/harness/deerflow/skills/storage/skill_storage.py#L180) 扫文件 → parser 解析 → 合并 enabled → 排序；静态路径校验。
- **抽象原子操作**（子类填）：`_iter_skill_files` / `read_custom_skill` / `write_custom_skill` / `ainstall_skill_from_archive` / `delete_custom_skill` / `append_history` / `read_history`。

`load_skills` 每次重读 extensions_config 的 enabled 状态（他进程改动立即生效）：

```python
for category, category_root, md_path in self._iter_skill_files():
    skill = parse_skill_file(md_path, category=category, ...)
    if skill: skills_by_name[skill.name] = skill
# 从 extensions config 合并 enabled 状态（每次重读）
extensions_config = ExtensionsConfig.from_file()
for skill in skills: skill.enabled = extensions_config.is_skill_enabled(skill.name, skill.category)
skills.sort(key=lambda s: s.name)
```

三个静态校验（防穿越）：`validate_skill_name` [skill_storage.py:42](../backend/packages/harness/deerflow/skills/storage/skill_storage.py#L42)（hyphen-case ≤64）、`validate_relative_path` [skill_storage.py:52](../backend/packages/harness/deerflow/skills/storage/skill_storage.py#L52)（resolve + relative_to）、`ensure_safe_support_path` [skill_storage.py:81](../backend/packages/harness/deerflow/skills/storage/skill_storage.py#L81)（支持文件必须落在 `references/templates/scripts/assets` 白名单子目录内）。

### §5.5 storage/local_skill_storage.py —— 本地 FS 实现 + 安装编排

`LocalSkillStorage(SkillStorage)` [local_skill_storage.py:38](../backend/packages/harness/deerflow/skills/storage/local_skill_storage.py#L38)：把抽象操作落到本地文件系统。`_iter_skill_files()` [local_skill_storage.py:70](../backend/packages/harness/deerflow/skills/storage/local_skill_storage.py#L70) 用 `os.walk` 扫 `public/`+`custom/`（跳 dotfile 目录）。

**`ainstall_skill_archive()`** [local_skill_storage.py:102](../backend/packages/harness/deerflow/skills/storage/local_skill_storage.py#L102) 是安装的三阶段编排（审查是 async LLM，留事件循环；文件系统阶段跑 worker 线程）：

```python
tmp = await asyncio.to_thread(tempfile.mkdtemp)
try:
    skill_dir, skill_name, target = await asyncio.to_thread(self._prepare_skill_archive, ...)  # ① 解压+校验
    await _scan_skill_archive_contents_or_raise(skill_dir, skill_name)                         # ② LLM 审查
    await asyncio.to_thread(self._commit_skill_install, ...)                                   # ③ 原子搬入
finally:
    await asyncio.wait_for(asyncio.to_thread(self._cleanup_install_tmp, tmp), timeout=5.0)     # 尽力清理
```

### §5.6 installer.py —— .skill ZIP 安装的安全流水线

**解压防护 `safe_extract_skill_archive()`** [installer.py:75](../backend/packages/harness/deerflow/skills/installer.py#L75)：

```python
for info in zip_ref.infolist():
    if is_unsafe_zip_member(info):           # 拒绝对路径 / ".."
        raise ValueError(...)
    if is_symlink_member(info):              # 跳 symlink（不物化）
        logger.warning(...); continue
    member_path = dest_root.joinpath(...)
    if not member_path.resolve().is_relative_to(dest_root):   # 每成员 resolve 后须在 dest 内
        raise ValueError(...)
    with zip_ref.open(info) as src, member_path.open("wb") as dst:
        while chunk := src.read(65536):
            total_written += len(chunk)
            if total_written > max_total_size:                # 512MB zip 炸弹上限
                raise ValueError("...")
            dst.write(chunk)
```

`is_unsafe_zip_member()` [installer.py:36](../backend/packages/harness/deerflow/skills/installer.py#L36) 拒绝对路径与穿越；`is_symlink_member()` [installer.py:54](../backend/packages/harness/deerflow/skills/installer.py#L54) 据 `external_attr` 高 16 位的 `S_IFLNK` 标记识别 symlink。

**审查 `_scan_skill_archive_contents_or_raise()`** [installer.py:172](../backend/packages/harness/deerflow/skills/installer.py#L172)：SKILL.md + scripts/* + references/templates 下的文本文件逐个审；嵌套 SKILL.md 禁止。`_scan_skill_file_or_raise()` [installer.py:141](../backend/packages/harness/deerflow/skills/installer.py#L141)：block / 可执行非 allow / 非法决定 → 抛 `SkillSecurityScanError`。

**原子搬入 `_move_staged_skill_into_reserved_target()`** [installer.py:123](../backend/packages/harness/deerflow/skills/installer.py#L123)：先 `target.mkdir(mode=0o700)` 预占（reserved=True），再搬入暂存内容，再 `make_skill_tree_sandbox_readable` 收紧权限。失败（如目标已存在 → `SkillAlreadyExistsError`）finally 清理预占目录。两个异常类 `SkillAlreadyExistsError` [installer.py:28](../backend/packages/harness/deerflow/skills/installer.py#L28) / `SkillSecurityScanError` [installer.py:32](../backend/packages/harness/deerflow/skills/installer.py#L32)。

### §5.7 permissions.py —— 沙箱只读权限

`make_skill_path_sandbox_readable()` [permissions.py:11](../backend/packages/harness/deerflow/skills/permissions.py#L11)：剥 sandbox 组/其他写位（`~(S_IWGRP|S_IWOTH)`），按文件/目录补只读模式（目录 `0o555` / 文件 `0o444`），**跳过 symlink**。`make_skill_tree_sandbox_readable()` [permissions.py:23](../backend/packages/harness/deerflow/skills/permissions.py#L23) 递归整棵子树。

### §5.8 security_scanner.py —— LLM 内容安全审查

`scan_skill_content()` [security_scanner.py:77](../backend/packages/harness/deerflow/skills/security_scanner.py#L77)：调 LLM 审一段文本，返 `ScanResult(decision, reason)` [security_scanner.py:22](../backend/packages/harness/deerflow/skills/security_scanner.py#L22)。block 明显的 prompt 注入 / 系统角色覆盖 / 提权 / 数据外泄 / 不安全可执行代码。**模型不可用或输出不可解析时保守回退 `block`**（宁可误杀不可放过）。`_extract_json_object()` [security_scanner.py:30](../backend/packages/harness/deerflow/skills/security_scanner.py#L30) 容错解析（剥 markdown 围栏 + 花括号配平）。

### §5.9 storage/__init__.py —— 单例 + 反射工厂（双重检查锁）

`get_or_new_skill_storage()` [storage/__init__.py:23](../backend/packages/harness/deerflow/skills/storage/__init__.py#L23)：返回 storage 实例——给了 `skills_path`/`app_config` 就建新实例（请求级配置不污染单例），否则返回进程单例。核心是**双重检查锁**（[storage/__init__.py:70-74](../backend/packages/harness/deerflow/skills/storage/__init__.py#L70)）：

```python
app_config_now = get_app_config()           # 先无锁读 config（只读、无共享突变）
with _skill_storage_lock:                    # 进锁
    if _default_skill_storage is None or _default_skill_storage_config is not app_config_now:
        _default_skill_storage = _make_storage(app_config_now.skills, **kwargs)  # 锁内构造
        _default_skill_storage_config = app_config_now
    return _default_skill_storage
```

`reset_skill_storage()` [storage/__init__.py:77](../backend/packages/harness/deerflow/skills/storage/__init__.py#L77) 也在锁内清空，不会清到一半被并发读到。详见 [§9.4](#94-为什么双重检查锁单例并发)。

---

## §6 数据流：一次调用怎么走完

### §6.1 数据流 A：用户输 `/code-review 帮我审查这个 PR` → SKILL.md 注入当次调用

```
① 用户消息 "/code-review 帮我审查这个 PR"
② SkillActivationMiddleware.awrap_model_call 触发（模型调用前）
   ├─ _resolve_activation(text)
   │    ├─ parse_slash_skill_reference(text)  → SlashSkillReference(name="code-review", ...)
   │    │    （跳过保留字；name 命中 hyphen-case 严格正则）
   │    ├─ resolve_slash_skill(..., available_skills=白名单)  → 找到启用且白名单内的技能
   │    └─ _read_skill_content(skill_file, skills_root)
   │         ├─ resolved_file.relative_to(resolved_root)  ← 读盘穿越拒绝
   │         └─ read_text → SKILL.md 全文
   ├─ _build_activation_reminder(activation)
   │    ├─ html.escape(user_request, quote=False)         ← 防注入
   │    └─ html.escape(skill_content, quote=False)        ← 防注入
   └─ 把激活消息（含 SKILL.md 全文）作为隐藏 HumanMessage 注入目标消息前
        （靠 __slash_activation ID + additional_kwargs 标志，幂等不重复注入）

注入的提示（节选）：
<slash_skill_activation>
The user explicitly activated the `code-review` skill for this turn.
<user_request>帮我审查这个 PR</user_request>
<skill name="code-review" category="public" path="/mnt/skills/public/code-review/SKILL.md" sha256="...">
<skill_content encoding="xml-escaped">&lt;技能正文，已转义&gt;</skill_content>
</skill>
</slash_skill_activation>

③ 模型这轮「看到」了 SKILL.md，照里面的操作指南审查 PR
```

未安装 / 未启用 / 不在白名单 → 返回友好的 AIMessage 失败提示（不让 agent 困惑）。

### §6.2 数据流 B：安装一个 `.skill` ZIP → 安全流水线

```
① storage.ainstall_skill_from_archive("my-tool.skill")
② _prepare_skill_archive（worker 线程，离事件循环）
   ├─ .skill 后缀校验 + ZipFile 打开
   ├─ safe_extract_skill_archive(zf, tmp_path)
   │    ├─ is_unsafe_zip_member → 拒穿越/绝对路径
   │    ├─ is_symlink_member → 跳 symlink
   │    ├─ 每成员 is_relative_to(dest_root) → 拒逃逸
   │    └─ total_written > 512MB → 拒 zip 炸弹
   ├─ resolve_skill_dir_from_archive → 定位技能根
   └─ _validate_skill_frontmatter → name/description/命名约定/未知 key
③ _scan_skill_archive_contents_or_raise（留事件循环，LLM 审）
   ├─ SKILL.md 审（executable=False）
   ├─ scripts/* 审（executable=True，须 allow）
   └─ references/templates 文本审 → 任一 block 抛 SkillSecurityScanError
④ _commit_skill_install（worker 线程）
   ├─ 暂存 copytree
   ├─ _move_staged_skill_into_reserved_target：预占 0o700 → 搬入 → make_skill_tree_sandbox_readable（0o555/0o444）
   └─ 失败 finally 清理预占目录
⑤ 返回 {"success": True, "skill_name": "my-tool", ...}
```

---

## §7 配置与用法

### §7.1 配置（`config.yaml` → `skills` 段）

| 字段 | 作用 |
|---|---|
| `skills.path` | 技能根目录（绝对或相对 base_dir）；空则用默认 `skills/` 或 `DEER_FLOW_SKILLS_PATH` 环境变量 |
| `skills.container_path` | 沙箱容器内技能根（默认 `/mnt/skills`） |
| `skills.use` | storage 后端类路径（默认 `LocalSkillStorage`，反射加载） |

`extensions_config.json` 的 `enabled_skills` 列表控制启用状态；未列入的 public/custom 默认启用（开箱即用）。`skill_evolution.enabled`（agent 自演化）+ `skill_evolution.moderation_model_name`（专用审查模型）控制 create/patch/edit 能力。

### §7.2 创建 / 激活 / 安装

```bash
# 创建公共技能（只读）
mkdir -p skills/public/my-skill && cat > skills/public/my-skill/SKILL.md <<'EOF'
---
name: my-skill
description: 演示技能
allowed-tools: [bash, read_file]
---
# 我的技能
操作步骤……
EOF
```

```python
# 安装 .skill 归档
from deerflow.skills.storage import get_or_new_skill_storage
result = get_or_new_skill_storage().install_skill_from_archive("path/to/skill.skill")
# {"success": True, "skill_name": "...", "message": "..."}
```

激活：对话里输 `/my-skill 帮我做这件事`，SkillActivationMiddleware 自动注入 SKILL.md 全文。

### §7.3 跑测试

```bash
cd backend && make test    # 含 test/test_skills.py（85 个 hermetic 测试）
```

测试约定：`LocalSkillStorage(host_path=tmp)` 直接构造（绕单例）；installer/security 经 monkeypatch scan；单例并发测试用「桩 storage + barrier + 慢构造」撑开竞态窗口验证锁。

---

## §8 与其它模块的关系

```
config/skills_config (SkillsConfig.path / container_path / get_skills_path)
config/extensions_config (is_skill_enabled ← enabled_skills)
config/skill_evolution_config (enabled / moderation_model_name)
   │
skills
   ├── parser/validation/types (SKILL.md 协议)
   ├── slash (严格 /skill-name 语法 + 保留字)
   ├── tool_policy (allowed-tools 白名单收紧)
   ├── storage (load_skills 模板 + 路径校验 + LocalSkillStorage)
   ├── permissions (沙箱只读)
   ├── security_scanner (LLM allow/warn/block)
   └── installer (.skill ZIP 安全安装)
        ↑ create_chat_model（审查模型，独立调用方）
   │
agents/middlewares/skill_activation_middleware (wrap_model_call：注入 SKILL.md)
agents/lead_agent/prompt (get_skills_prompt_section + 后台刷新缓存)
   │
▼ 消费者：skill_manage 工具（自演化 create/patch/edit/delete，依赖 skill_evolution.enabled）
          lead_agent（filter_tools_by_skill_allowed_tools 收紧工具集 + 可见技能白名单）
          subagents executor（_load_skills 经单例加载，失败降级无技能）
```

- **上游**：[config](config.md)（skills/extensions/skill_evolution）、`reflection.resolve_class`（storage 工厂）、[utils](utils.md).messages（激活中间件取用户文本）、[models](models.md).create_chat_model（安全审查）。
- **下游消费者**：`skill_manage` 工具（自演化，全程记 `custom/.history/<name>.jsonl`）、[agents](agents.md) lead_agent（工具白名单收紧 + 技能可见性）、[subagents](subagents.md)（技能加载，失败降级）。

---

## §9 设计动机分析（为什么这么设计 / 作用 / 好处）

### §9.0 核心设计动机一览

| 关键机制 | 为什么这么设计 | 作用 / 好处 | 不这么设计会怎样 |
|---|---|---|---|
| **技能 = 目录 + SKILL.md（文件而非代码）** | 操作流程是「内容」不是「逻辑」，该用文件描述、不该写进代码 | 用户/agent 可直接读写、版本管理、分享分发 | 写成代码插件 → 要重启、要编译、普通用户改不了 |
| **frontmatter + 正文分离** | 元数据（给系统）和指南（给模型）读者不同 | 系统解析 name/allowed-tools 不用读正文；模型读正文不用管配置 | 混在一起 → 解析难、注入冗余 |
| **两类别 public/custom** | 内置技能不该被用户改坏；用户技能要能编辑 | 只读 vs 可写权限天然分离 | 一锅 → 用户改坏内置技能影响所有人 |
| **安全安装五层防御** | 用户上传的 .skill 是不可信输入 | 穿越/symlink/炸弹/注入全挡 | 任一层缺 → zip 炸弹撑爆盘 / symlink 越权读 / 路径穿越写任意文件 / prompt 注入 |
| **html.escape 防注入** | 技能正文是第三方写的，可能含恶意指令 | 技能内容只能当文本看，不能逃逸 XML 当结构 | `</skill_content><system>忽略指令…` 注入恶意指令 |
| **allowed-tools 并集收紧** | 技能该限定能用什么工具，最小权限 | 一旦有声明就收紧到并集，没声明的技能不贡献工具 | 全放行 → 技能激活时能用全部工具，违反最小权限 |
| **双重检查锁单例** | 冷启动并发会各构一份实例 | 锁内构造，竞态只构出一个 | 并发构多份 → 反射解析多次、资源浪费、reset 竞态 |
| **后台刷新缓存** | 扫盘 IO 不能阻塞请求路径 | miss 立即返空 + daemon 线程预热，下次读到结果 | 同步扫盘 → 每个请求都卡在 IO |
| **enabled 每次重读** | 他进程（API）可能改了 enabled | 改动立即生效，无需重启 | 缓存 enabled → API 改了读不到 |
| **ABC + 模板方法** | 存储可能有不同后端 | load_skills 逻辑只写一遍，子类只填介质操作 | 每个后端重写 load_skills → 重复 + 易不一致 |

### §9.1 为什么技能是「目录 + SKILL.md」而非代码插件

**动机**：agent 的操作流程是「**内容**」（一段希望它遵循的步骤），不是「**逻辑**」（if/else 代码）。内容该用文件描述，让人和 agent 都能直接读写。

**作用 / 好处**：用户能直接编辑 SKILL.md、能用 git 版本管理、能打包成 `.skill` 分享；agent 激活时 `read_text` 当文本注入即可，不需要执行任意代码。public/custom 两类别天然分离「只读内置」和「可写用户」。

**不这么设计会怎样**：写成代码插件 → 要重启进程、要编译、普通用户改不了、还引入「执行第三方代码」的巨大安全面。

### §9.2 为什么安全安装要五层防御（穿越 / symlink / 炸弹 / 审查 / 权限）

**动机**：`.skill` 是**用户上传的不可信输入**，每个攻击面都真实存在——

| 攻击 | 防御 | 不防会怎样 |
|---|---|---|
| zip 成员 `../../etc/cron.d/evil` | `is_unsafe_zip_member` 拒穿越/绝对路径 + 每成员 `is_relative_to(dest)` | 写任意系统文件 |
| symlink 指向 `/etc/passwd` | `is_symlink_member` 跳过（不物化） | 越权读系统文件 |
| 42KB 解出 4.5PB | 512MB 总解压上限 | 撑爆磁盘/内存 |
| SKILL.md 含 `</skill_content><system>…` | LLM 审查 block + 激活时 `html.escape` | prompt 注入 |
| 沙箱内 agent 改技能 | 安装后权限收紧 `0o555/0o444` | 改写影响所有用户 |

**作用 / 好处**：这五层是 belt-and-suspenders（双保险）——即使一层漏了，下一层还挡着。审查 LLM 不可用时**保守回退 block**（宁可误杀不可放过），可执行内容尤其严格（必须 `allow`）。

### §9.3 为什么 allowed-tools 用「并集收紧」语义

**动机**：技能激活时应该**最小权限**——只给完成任务必需的工具。但多个技能同时加载，怎么合并它们的限制？

**作用 / 好处**：`allowed_tool_names_for_skills` 取所有**声明了** allowed-tools 的技能的并集。三种情况：① 全都没声明 → None → 全放行（legacy，向后兼容）；② 有声明 → 收紧到并集；③ `allowed-tools: []`（显式空）→ 该技能激活时不允许任何工具。**没声明的技能不贡献工具**（而非禁用其他技能的显式限制）。

**不这么设计会怎样**：全放行 → 违反最小权限，技能激活时能调危险工具；取交集 → 太严，多技能协作时啥都用不了。

### §9.4 为什么双重检查锁（单例并发）

**动机**：`get_or_new_skill_storage()` 返回进程单例。冷启动并发场景有竞态——多个请求同时第一次调，都看到 `_default_skill_storage is None`，于是都进构建分支，**各构一份**实例（每份都反射解析类、建存储、占资源）。更糟的是 `reset_skill_storage()` 若在并发读当口清空全局，读到的就是 None。

**作用 / 好处**：双重检查锁——先无锁读 `app_config_now`（只读 config、无共享突变），再进锁复查条件。第一个拿到锁的构实例，后到的看到就绪直接复用。

**为什么「锁内构造」而非「锁外构造再丢败者」**：`SkillStorage` 没有 `teardown()` 钩子，锁外构造的败者实例无法被清理（可能持有文件句柄/缓存）。所以选「锁内构造」镜像 `get_memory_storage()`，而非 sandbox_provider 的「锁外构造再丢败者」模式。`app_config_now` 的读取留在锁外是因为它只是一次 config 读取、不涉及共享状态突变，放锁外减少临界区。

**不这么设计会怎样**：并发构多份 → 反射解析多次、资源浪费；reset 竞态 → 并发读拿到 None 崩溃。

### §9.5 为什么后台刷新缓存 + enabled 每次重读

**动机**：`get_skills_prompt_section` 渲染系统提示的技能段，`load_skills` 扫盘是 IO，不能阻塞请求路径。但 enabled 状态可能被**他进程**（如 API 改了 `extensions_config.json`）改动，需要立即生效。

**作用 / 好处**：两层分离——
- **渲染段**（签名 = 技能元组 + 白名单 + 容器路径 + 自演化段）走 `lru_cache`，命中直接返回；
- **enabled 判定**每次重读 `extensions_config.json`（不在缓存层）；
- **扫盘**用进程级缓存 + daemon 线程后台刷新（miss 立即返 `[]` 并触发 `_refresh_enabled_skills_cache_worker`，下次读到预热结果，按 AppConfig 身份隔离）；
- 技能变更后 `clear_skills_system_prompt_cache` 失效。

**不这么设计会怎样**：同步扫盘 → 每请求卡 IO；缓存 enabled → API 改了读不到；不缓存渲染段 → 每请求重渲染。

### §9.6 为什么 12 文件拆分

每个文件管**一种独立责任**——改一处不牵连另一处（见 [§3 文件结构](#3-整体结构它在系统里的位置)）。最底层 `types.py` 纯数据定义（被几乎所有文件 import，放底层避免循环依赖）；最复杂 `installer.py` 独立成文件便于隔离安全测试；`storage/` 用 ABC + 模板方法让 `load_skills` 逻辑只写一遍。

---

## §10 实现差异（vs 上游 deer-flow 源码）

> 对照 `deer-flow/backend/packages/harness/deerflow/skills/`（与 mini 同布局，12 文件）。**先剥 docstring/comment 再判逻辑差**（mini 中文 docstring、上游英文，行数差不等于逻辑差）。

**总结论：高度忠实移植，近 0 逻辑差。** 剥 docstring 后逐文件比对：

| 文件 | 剥后 mini/up | 逻辑差 |
|---|---|---|
| `installer.py` | 133 / 133 | **0 逻辑差（逐字节相同）**——穿越/symlink/512MB 炸弹/原子搬入/审查全一致。安全最敏感的文件反最干净 |
| `security_scanner.py` | 88 / 88 | **0 逻辑差**——allow/warn/block + 容错解析 + 保守回退全一致 |
| `validation.py` | 55 / 55 | **0 逻辑差** |
| `slash.py` | 45 / 45 | **0 逻辑差**——严格正则 + 6 保留字全一致 |
| `permissions.py` | 24 / 24 | **0 逻辑差**——0o555/0o444 + 跳 symlink 全一致 |
| `skill_storage.py` | 126 / 126 | **0 逻辑差**（一个 `# noqa` 注释尾巴差） |
| `local_skill_storage.py` | 173 / 173 | **0 逻辑差**——唯一 import 差：mini `from deerflow.config.paths import resolve_path`、上游 `from deerflow.config.runtime_paths import resolve_path`。因 mini 把上游 `runtime_paths.py` 合并进了 `paths.py`（见 [config.md](config.md) §9），函数本身一致 |
| `parser.py` | 83 / 83 | **0 逻辑差**（一句注释中英） |
| `storage/__init__.py` | 46 / 46 | **0 逻辑差**——双重检查锁（`_skill_storage_lock` + 锁内双检 + 锁内 reset）两边都有，mini 已与上游一致 |
| `types.py` | 31 / 31 | **0 逻辑差**——差异全是行内注释中英 + 一处 mini 用 `SKILL_MD_FILE` 常量、上游用 `"SKILL.md"` 字面量（**等价**） |
| `tool_policy.py` | 34 / 31 | **0 逻辑差**——剥后差异全是 docstring 中英（stripper 对多行 docstring 的已知局限），并集收紧逻辑逐行一致 |
| `__init__.py` | 26 / 14 | mini **多导出公共符号**：`ALLOWED_FRONTMATTER_PROPERTIES` / `_validate_skill_frontmatter` / `parse_allowed_tools` / `allowed_tool_names_for_skills` / `filter_tools_by_skill_allowed_tools` / `SlashSkillReference` / `ResolvedSlashSkill`——mini 把这些放公共 API 面，上游没全导出。纯 API 面差异 |

**为什么这么干净？** 技能模块是**纯业务逻辑 + 文件 IO**——它的输入（SKILL.md / .skill ZIP）和输出（Skill 对象 + 注入串 + 安装结果）都不依赖 Gateway/IM/auth。抽掉上层后，靠 **ABC + 模板方法**（`SkillStorage`）、**纯函数**（parser/validation/slash/tool_policy）、**反射工厂**（storage `__init__`）解耦，底层零改动。这与 [memory.md](memory.md)、[user_context.md](user_context.md) 是同一类「砍 Gateway 一行不改」的忠实移植。

**唯一实质差异**：① `resolve_path` 的 import 路径（mini `config.paths` / 上游 `config.runtime_paths`，因 mini 合并了文件，函数一致）；② mini `__init__` 公共 API 面更宽。都是组织/可读性偏好，**无行为差异**。

---

## §11 常见问题 / 排错

**Q：技能和记忆有什么区别？**
A：记忆是「关于**用户**的事实」（个性化，LLM 自动抽取）；技能是「关于**任务**的操作流程」（复用，人工写）。记忆自动积累、自动注入；技能手动创建、显式激活（`/skill-name`）或 agent 自取（`read_file`）。两者都注入，但触发与生命周期不同。

**Q：`/new` `/help` 这些为什么不能当技能名？**
A：它们是控制命令（`RESERVED_SLASH_SKILL_NAMES`）。slash 解析遇到保留字直接返回 None，交给别的处理器。防止技能名撞控制命令导致行为混乱。

**Q：allowed-tools 不写和写成 `[]` 有什么区别？**
A：不写（`None`）= 不限制（该技能激活时全部工具可用）；`[]` = 不允许任何工具。一旦**任何**已加载技能声明了 allowed-tools，工具集就收紧到所有声明的并集——没声明的技能不贡献工具。

**Q：技能内容能注入恶意指令吗？**
A：激活时所有动态内容（技能正文 + 用户文本）经 `html.escape` 转义，技能内容只能当文本看，不能逃逸 XML 结构注入指令。安装时还有 LLM 安全审查（block prompt 注入 / 提权 / 恶意可执行）。

**Q：装一个 .skill 会被 zip 炸弹炸吗？**
A：不会。`safe_extract_skill_archive` 强制 512MB 总解压上限，超了抛错。还拒穿越成员、跳 symlink。

**Q：归档里的 symlink 会被解压吗？**
A：不会。`is_symlink_member` 据 `external_attr` 高 16 位的 `S_IFLNK` 标记识别 symlink 成员，遇到就跳过（只记 warning，不物化、不抛错）。防止 symlink 把 `/etc/passwd` 指到技能目录外造成越权读。

**Q：技能目录能被沙箱内 agent 改写吗？**
A：不能。安装后权限收紧（目录 `0o555` / 文件 `0o444`，剥 sandbox 写位），跳 symlink。改写要走 `skill_manage` 工具（受安全审查）。

**Q：激活后每轮都重新注入 SKILL.md 吗？**
A：不。幂等——同一目标 HumanMessage 只注入一次（靠 `__slash_activation` ID + additional_kwargs 标志检测）。后续轮次看到已有激活上下文就跳过。

**Q：技能改了，系统提示里的技能列表会立即更新吗？**
A：会。`load_skills` 每次重读 extensions_config 的 enabled 状态。但渲染段有 `lru_cache`——写技能后须调 `clear_skills_system_prompt_cache()` 失效缓存，下个请求才看到新列表。

**Q：安全审查的 LLM 不可用会怎样？**
A：保守回退 `block`（可执行内容尤其严格）。宁可误杀不可放过——审查坏了就当内容不安全，拒绝写入。配置 `skill_evolution.moderation_model_name` 可指定专用审查模型。

**Q：多个请求同时第一次加载技能，会建多份 storage 吗？**
A：不会。`get_or_new_skill_storage()` 用 `_skill_storage_lock` + 锁内双检——并发冷启动只有一个调用方能进构建分支，其余看到实例已就绪直接复用。`reset_skill_storage()` 也在锁内清空。`SkillStorage` 无 `teardown()` 钩子，所以选「锁内构造」（败者无法被清理）而非 sandbox_provider 的「锁外构造再丢败者」。
