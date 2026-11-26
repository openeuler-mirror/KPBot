---
name: repo-view-skills
description: >
  repo-view / repo-analysis 代码仓视图系列的总门面与索引。当用户笼统地说"分析这个代码仓画个图 / 看看架构 /
  画张视图 / 出张图"而未指明具体视图类型时，本 skill 做系列路由：按问题形状分派到六个分析 skill
  之一，并说明三环执行链（读仓方法论 → 证据验证 → 渲染报告）与交付物形态。用户已指明视图类型的请求直接触发对应
  repo-analysis-* skill，不经过本 skill。本 skill 不含任何判定规则与流程细节。
license: MulanPSL-2.0
compatibility: 任意支持 Agent Skills 的运行时；纯知识型路由，无外部依赖
metadata:
  skill_type: knowledge_base
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-view-skills：系列门面与路由

# 定位

本 skill 是系列目录与路由器：回答"有哪些 skill、什么时候用哪个、一次任务怎么串起来"。所有方法论与流程细节都在被索引的 skill 里，此处不复述。

# 系列全家福（10 个 skill）

**基础设施四件（repo-view-*）——公共流程，不含图型知识：**

| skill | 唯一职责 | 什么时候被用到 |
|---|---|---|
| `repo-view-skills`（本 skill） | 系列索引与路由 | 用户笼统要"一张图"时 |
| `repo-view-engine` | 读仓方法论：考古五步法 + 停止条件 + 证据的事实性规则 | 每个分析任务的第一环 |
| `repo-view-verify` | 证据验证：verify-sources.mjs 用本地 git 逐条核实 revision/路径/行号，`ok:true` 是交付前置条件 | IR 落盘后的第二环 |
| `repo-view-render` | 呈现契约：IR 公共骨架 + 六图型 Mermaid 映射总表 + 排版守则 + Markdown 报告骨架 + 回复契约 | 写 IR 与交付时的第三环 |

**分析六件（repo-analysis-*）——各持一种视图的领域判定规则：**

| skill | 回答什么问题 |
|---|---|
| `repo-analysis-arch` | 这个仓库由什么组成：组件、依赖、边界（架构图） |
| `repo-analysis-process` | 运行时有哪些进程线程、怎么通信（进程/线程图） |
| `repo-analysis-sequence` | A 怎么调用 B、一次请求的完整交互（时序图） |
| `repo-analysis-class` | 谁继承谁、谁实现谁、谁持有谁（类图） |
| `repo-analysis-tree` | 仓库分几块、每块管什么（模块结构树） |
| `repo-analysis-dataflow` | 数据从哪来、经过什么变换、到哪去（数据流图） |

# 路由表（用户没指明图型时）

| 用户问题形状 | 分派到 |
|---|---|
| "整体架构 / 组件关系 / 系统结构 / 这个项目由什么组成" | repo-analysis-arch |
| "有哪些进程线程 / master-worker / 并发模型 / IPC 关系" | repo-analysis-process |
| "A 怎么调用 B / 调用链 / 一次请求的流程 / 交互" | repo-analysis-sequence |
| "类继承 / 接口实现 / 类型关系 / 组合聚合" | repo-analysis-class |
| "包结构 / 目录结构 / 模块分解 / 能力分解" | repo-analysis-tree |
| "数据流 / 管道 / 数据从哪来到哪去 / ETL" | repo-analysis-dataflow |

歧义时向用户确认一次；多种视图都要时逐个走各自 skill，每张图独立交付。

# 执行链（一次分析任务的完整串联）

```
用户请求
  │
  ├─ 笼统请求 → repo-view-skills（本 skill）路由
  ▼
repo-analysis-<type>          领域判定规则（组件/线程/消息/关系怎么认）
  │
  │  ① 读仓    repo-view-engine     考古五步法 + 证据怎么记
  │  ② 验证    repo-view-verify     verify-sources.mjs，ok:true 才放行
  │  ③ 呈现    repo-view-render     IR 约定 + Mermaid 映射 + 报告契约
  ▼
交付：<repo>-<type>-view.md（Mermaid 图 + 钉定 commit 的证据表 + 不确定项 + 裁剪说明 + 验证回执）
```

三条纪律贯穿链条：验证不过不交付（verify 的红线）、两轮修复上限（verify 的纪律）、回复只给一行结论加报告路径（render 的回复契约）。

# 使用注意

- 基础设施三件（engine/verify/render）不直接响应用户请求，只被分析 skill 按环引用
- 交付物是**可 diff、可 PR 的 Markdown 报告**，证据全部钉定 40 位 commit 与行号
