---
name: repo-analysis-tree
description: >
  
  分析代码仓库并生成模块结构树（单亲层级分解视图）。当用户提到以下场景时触发：模块结构、包结构、目录结构、仓库分解、能力分解、功能树、这个仓库分几层/几大块、层次结构。产出带源码证据锚定的
  Markdown 报告：Mermaid 层级树 + 每大块职责一句话 + 证据表。
license: MulanPSL-2.0
compatibility: 本地 git + 代码仓库；Node.js（验证脚本）；Mermaid 渲染环境
metadata:
  skill_type: debugging
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-analysis-tree：模块结构树视图

# 什么时候用本 skill

用户想看仓库的**分解结构**："这仓库分几块、每块管什么"。树表达单亲包含（一个节点只属于一个父）；跨模块的横向依赖不归本图（那是 repo-analysis-arch 的事）。

**执行链**（三个公共 skill，按阶段加载，缺一不可）：
1. 读仓前 → 同级 `repo-view-engine/SKILL.md`（考古五步法 + 证据规则）
2. IR 落盘后 → 同级 `repo-view-verify/SKILL.md`（verify-sources.mjs 自验，`ok:true` 才继续）
3. 渲染交付前 → 同级 `repo-view-render/SKILL.md`（IR 公共约定 + Mermaid 映射 + 报告契约）

# 领域判定规则（本 skill 的核心资产）

## 分组语义（从高到低）

1. **构建/包边界**：Maven 模块、Cargo crate、Go package、Python 包声明——构建边界优先于目录直觉
2. **目录**：构建单元内部再按目录分
3. **禁止按文件类型分**：`utils/`、`models/`、`handlers/` 这类按技术切层的目录，若真实职责单一可保留，否则合并成语义节点

## 深度与广度控制

- **3~4 层**为宜：根（仓库）→ 大块（3~7 个）→ 子模块 →（必要时）核心文件组
- 单亲铁律：一个节点只有一个 parent；一个模块真的被两个父使用，说明它该上提一层
- 每个节点 label = 真实职责名（从代码/构建声明读出），**不是目录名照抄**；注记里一句话说明该块管什么

## 大仓折叠

- 只展开到读者关心的层；不关心的子树折叠成单节点，注记"含 N 个子模块，未展开"
- 测试/文档/构建脚手架默认折进一个"支撑设施"节点，不与核心代码平级展开

## 自检

任何一个叶节点被问"你管什么"，注记必须已有一句话回答；答不出说明这层分错了。

# IR 骨架

```json
{
  "diagram": "tree",
  "meta": { "title": "…", "repository": { "url": "…", "revision": "<40位SHA>" }, "worktree": "clean" },
  "elements": [
    { "id": "root", "label": "my-repo", "sublabel": "一句话定位", "sources": [{ "path": "pyproject.toml", "line": 6, "end_line": 8, "label": "项目定义" }] },
    { "id": "serving", "label": "serving", "parent": "root", "sublabel": "服务编排核心", "sources": [{ "path": "src/serving/__init__.py", "line": 9, "end_line": 9, "label": "包职责" }] }
  ],
  "notes": ["…"]
}
```

# Mermaid 映射

```
flowchart TD
  ROOT["my-repo<br/>本地推理服务"]
  ROOT --> CLI["cli<br/>三模式入口"]
  ROOT --> SERVING["serving<br/>服务编排核心"]
  SERVING --> ENG["engine · 异步门面"]
  SERVING --> SCHED["sched · 连续批处理"]
  ROOT --> INFRA["支撑设施（折叠：config/观测/工具）"]
```

- 根在上，`flowchart TD` 纵向展开；单亲链
- 节点 label 用"名字 + 职责短语"两行（`<br/>` 分隔）
- 折叠节点在 label 里注明折叠范围

# 输出

按执行链交付：IR 落盘 → repo-view-verify 自验 → `<repo>-tree-view.md`（报告契约见 repo-view-render）。
