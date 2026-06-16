---
name: example
description: 示例技能，演示 SKILL.md 协议（YAML frontmatter + 正文）。复制本目录改造成真实技能。M14（技能模块）落地后生效。
license: MIT
allowed-tools:
  - bash
  - read_file
  - write_file
---

# 示例技能

这是一个占位技能，用于演示技能系统的发现 / 加载 / 激活流程（M14 落地后生效）。

## 用途

把一段「在特定场景下希望 agent 遵循的操作流程」沉淀成一个可复用文件，而不是每次都
在对话里手写。技能通过 frontmatter 声明元数据与允许使用的工具，正文是给模型看的操作指南。

## 使用方法

- **常驻注入**：在 `extensions_config.json` 的 `enabled_skills` 里列出技能名
  （当前已默认列出 `example`，但 skill 系统尚未落地，故暂不生效）。
- **按需激活**：在对话里输入 `/example <任务描述>`，技能正文会作为隐藏上下文
  注入到当次模型调用（M14 的 SkillActivationMiddleware 负责）。

## 改造成真实技能

1. 复制本目录到 `skills/public/<你的技能名>/`（公共）或 `skills/custom/<名>/`（自定义）。
2. 改 `name` / `description` / `allowed-tools`。
3. 正文写清操作步骤、注意事项、产出格式。
4. `allowed-tools` 会收紧该技能激活时模型可调用的工具集合（白名单）。
