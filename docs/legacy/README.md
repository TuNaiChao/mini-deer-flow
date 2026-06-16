# legacy — 旧文档归档

这里放 mini-deer-flow **早期的、未按新规范**的学习文档，仅作历史参考。它们会在对应模块落地时按新模板重写，搬到 `docs/` 根（命名规范见 [../ALIGNMENT_OUTLINE.md](../ALIGNMENT_OUTLINE.md) 执行约定第 8 条）。

## 文档清单

| 文件 | 对应模块 | 去向 |
|------|----------|------|
| `tools.md` | M15 tools | M15 落地时按新模板重写为 `docs/tools.md` |
| `中间件.md` | M16 middlewares | M16 落地时重写为 `docs/middlewares.md`（中文文件名不规范，废弃） |
| `模型更换.md` | M-models | 已被 `docs/models.md` 取代 |

## 为什么归档

- 命名不规范（中文文件名 `中间件.md` / `模型更换.md`）；
- 内容是旧版实现说明，未按「面向小白、从基础讲起」的新模板写；
- 留在 `docs/` 根会和新规范文档混在一起，误导读者以为它们是当前有效文档。

新文档规范（详见 ALIGNMENT_OUTLINE.md 执行约定第 8 条）：
- 模块文档放 `docs/` 根，命名 `<module>.md`（kebab-case 英文，如 `build.md` / `models.md`）；
- 面向小白、从基础讲起，每个名词都解释（范例：`docs/build.md` 的「零基础先读」节）。
