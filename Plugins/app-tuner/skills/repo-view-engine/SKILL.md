---
name: repo-view-engine
description: >
  代码仓视图系列的考古方法论（被 repo-analysis-arch / repo-analysis-process /
  repo-analysis-sequence / repo-analysis-class / repo-analysis-tree /
  repo-analysis-dataflow 六个分析 skill
  在读仓阶段引用）。唯一职责：怎么读代码仓、怎么落源码证据——考古五步法、停止条件、证据的事实性规则。不含路由、不含验证、不含渲染。本 skill
  是执行链第一环，一般不直接响应用户请求。
license: MulanPSL-2.0
compatibility: 任意支持 Agent Skills 的运行时；需要本地 git 用于考古取证
metadata:
  skill_type: knowledge_base
  category: code_analysis
  domain: repo_view
  version: "1.0.0"
---

# repo-view-engine：考古方法论

# 定位

六个分析 skill（repo-analysis-*）各自持有图型判定规则，本 skill 是它们共同的**读仓方法论**。执行链共三环，各是一个独立 skill：

```
① repo-view-engine（本 skill）   读仓方法论：考古五步法 + 证据规则
② repo-view-verify                 IR 落盘后：verify-sources.mjs 自验，ok:true 才可交付
③ repo-view-render                 渲染与报告：IR 结构约定 + Mermaid 映射 + 报告契约
```

本 skill 只负责第 ① 环。证据"怎么记"在本 skill；"是不是真的"归 verify；"怎么呈现"归 render。

# 考古五步法（所有 viz 读仓前必须执行）

## 1. 冻结身份

```
git rev-parse HEAD          # 40 位 SHA
git remote get-url origin   # 去掉 URL 内凭据
git status --short          # 脏工作树必须声明
```

- SHA 与无凭据 URL 写进 IR 的 `meta.repository`，并写进报告头部
- **工作树是脏的必须处理**：未提交字节不得当作 HEAD 的证据。标准做法是开干净 worktree 取证：
  `git worktree add <repo>-head HEAD`，考古与验证都指向 worktree（其 HEAD 与原仓相同）
- 报告头部如实声明工作树状态

## 2. 切片定位

- 从 manifest / 入口文件 / 构建配置 / 注册表定位候选单元
- **读一小片连通切片，不要全仓扫描找标签**
- 每次额外读文件必须回答一个"能改变这张图的未解决问题"，回答不了就停止读取
- 大仓可分派子任务并行考古（结构 / 运行时 / 调用链各一路），要求所有证据都是 `相对路径:行号` 格式

## 3. 追责分离

- 区分三种角色：**发请求的 controller / 执行的 runtime / 收数据的 store**
- 禁止从文件邻近、命名相似推断运行时关系
- 导出但没被调用的函数 = optional capability，不画成必需边

## 4. 边读边落证据

- 每个主元素记录仓库相对路径 + 行区间（`line`~`end_line`），每元素最多 3 条
- round-trip 关系（请求/返回、写/读）两个方向分开追、分开引用
- 子任务考古的结论交叉核对：同一代码位置两个来源说法不一致时，以实读为准并修正

## 5. 命名不确定性

- 未解决的问题写在受影响元素旁（例："`writeFile` 在此被调用；落盘时序未知"）
- 禁止把标签、包描述、README 宣称当作已观察事实
- 静态不可知的行为（条件性创建、运行时分支）标注 enabling condition

## 停止条件

当每个被请求的责任、关系、边界都有源码支撑，且剩余未知不再能改变图的覆盖时，停止探索。没有任何节点数、边数、引用数指标要凑。

# 证据规则（事实性约定）

- `sources[]`：每主元素 1~3 条 `{path, line, end_line, label}`，仓库相对 POSIX 路径
- 目录级引用（只有 path 无行号）允许但弱化：只证明目录在钉定 revision 存在，不证明其中每个文件的行为
- 40 位 revision 强制；行号区间必须来自真实读到的源码行，禁止估算
- 证据原则：**宁可断言缺席，不可编造**；验证不通过时修引用，绝不美化断言
