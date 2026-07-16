# 文档阅读指引 / Reading Guide

## 文档关系概览

```text
docs/
  architecture-4plus1.md   ← 唯一架构设计文档（4+1 视图）
  report-template.md       ← 报告模板（由 report-schema.md 定义字段）

skills/server-application-optimization/
  SKILL.md                 ← 主 skill（编排器，AI Agent 首先加载）
  references/
    application-agent-architecture.md ← 架构图到三阶段重构的映射
    workflow.md            ← 主流程展开（SKILL.md 的详细版）
    input-contract.md      ← 输入输出字段定义（核心 + 子 skill 专属）
    user-interaction-gates.md      ← 用户主动询问、确认和分支路由门控
    prerequisites.md       ← 工具依赖、权限、降级策略（唯一权威来源）
    environment-diagnosis.md       ← 环境备份后的诊断规则
    checklist.md           ← 各阶段执行检查清单
    candidate-skill-list.md        ← 根据采集信息生成候选优化 skill 列表的规则
    knowledge-technique-routing.md ← 知识库技术层到子 skill 的映射
    candidate-skill-analysis.md     ← 候选 skill 分析清单
    subagent-orchestration.md      ← 分析/执行验证 subagent 任务包与结果契约
    iteration-execution.md         ← 迭代执行规则（轮次/单 skill 停止/二进制门控）
    optimization-decision-tree.md  ← 优化方向选择与叠加关系
    review-restore-archive.md      ← review、环境还原和案例归档
    platform-tuning-notes.md       ← NUMA/THP/ARM 平台注意事项
    database-analysis.md           ← MySQL/InnoDB 专项与 AHI 决策
    external-library-replacement-integration.md  ← library-replacement 接入
    external-network-io-integration.md           ← network-io-performance 接入
    report-schema.md       ← 报告字段定义（report-template.md 的 schema）
    examples.md            ← 端到端优化场景走查示例
    reading-guide.md       ← 本文件
  subskills/               ← 子 skill（各专项分析逻辑）
    bios-optimization/SKILL.md
    os-optimization/SKILL.md
    network-optimization/SKILL.md
    cpu-affinity-optimization/SKILL.md
    performance-library-selection/SKILL.md
    compiler-optimization/SKILL.md
    application-config-optimization/SKILL.md
    accelerator-optimization/SKILL.md
    hardware-upgrade-analysis/SKILL.md
    other-optimization/SKILL.md
    io-memory-network-bottleneck-analysis/SKILL.md
    database-workload-analysis/SKILL.md
  scripts/                 ← 辅助脚本（环境备份、瓶颈检测等）
  agents/
    openai.yaml            ← OpenAI Agent 配置

ref-skills/                ← 仓库内置的第三方 skill
  compiler-option-optimization/
  library-replacement/
  network-io-performance/
  cpu-affinity-optimization/

.claude/skills/            ← Claude Code 轻量入口
.opencode/skills/          ← OpenCode 轻量入口
.agents/skills/            ← 通用 Agent 轻量入口
Codex / Cursor             ← 可直接读取 skills/server-application-optimization/ 主源
```

## 推荐阅读顺序

### 对 AI Agent

1. `SKILL.md` — 获取编排逻辑和门控规则
2. `references/prerequisites.md` — 检查工具依赖和权限
3. 按当前阶段加载对应 reference；候选 skill 分析阶段由 subagent 加载对应 subskill
4. 按需加载 `references/input-contract.md` 和 `references/report-schema.md`

### 对人类读者（理解项目）

1. `docs/architecture-4plus1.md` — 理解目标范围、架构决策和主流程
2. `SKILL.md` — 理解主流程入口
3. 按兴趣阅读 subskill 和 reference

### 对贡献者（修改项目）

1. `SKILL.md` — 理解主流程
2. `references/reading-guide.md` — 理解文档关系（本文件）
3. `references/input-contract.md` — 理解接口约定
4. 对应 subskill — 理解专项逻辑

## 单点定义原则

为避免冗余导致的不一致，以下知识点只在指定文件中定义：

| 知识点 | 唯一定义位置 | 其他文件应引用 |
|--------|-------------|--------------|
| 工具依赖与降级策略 | `prerequisites.md` | 其他文件只引用"详见 prerequisites.md" |
| 环境备份后的诊断规则 | `environment-diagnosis.md` | workflow.md 和 checklist.md 引用 |
| 用户主动询问和确认门控 | `user-interaction-gates.md` | workflow.md、checklist.md 和 SKILL.md 引用 |
| 主流程详细步骤 | `workflow.md` | SKILL.md 保留摘要 |
| 候选优化 skill 列表规则 | `candidate-skill-list.md` | workflow.md 和 candidate-skill-analysis.md 引用 |
| 知识库技术到子 skill 的映射 | `knowledge-technique-routing.md` | 候选列表和子 skill 引用 |
| 迭代执行规则 | `iteration-execution.md` | workflow.md 和 SKILL.md 引用 |
| Subagent 编排契约 | `subagent-orchestration.md` | 候选 skill 分析和执行验证阶段引用 |
| 输入输出字段 | `input-contract.md` | 子 skill 文档引用"详见 input-contract.md" |
| 报告字段定义 | `report-schema.md` | report-template.md 映射到该 schema |
| 优化方向选择 | `optimization-decision-tree.md` | 主流程引用"详见决策树" |
| review/恢复/归档 | `review-restore-archive.md` | 报告生成和收尾阶段引用 |
| 数据库 AHI 决策 | `database-analysis.md` | database-workload-analysis 子 skill 引用 |
| 外部 skill 接入 | 对应 external-*-integration.md | 子 skill 引用"详见接入说明" |

## 必读 vs 按需查阅

**必读（理解主流程）：**
- SKILL.md
- workflow.md
- prerequisites.md

**按需查阅（进入特定阶段时）：**
- candidate-skill-list.md（性能信息采集后）
- subagent-orchestration.md（进入候选 skill 分析或执行验证时）
- candidate-skill-analysis.md（进入候选 skill 分析时）
- 对应 subskill SKILL.md（由 subagent 或降级执行者读取）
- input-contract.md（需要字段定义时）
- user-interaction-gates.md（启动询问、BMC/Redfish、环境诊断和基线确认时）
- checklist.md（执行检查时）
- environment-diagnosis.md（环境备份后诊断时）
- optimization-decision-tree.md（选择优化动作时）
- report-schema.md（生成报告时）
- review-restore-archive.md（review、环境还原和归档时）

**特定场景：**
- database-analysis.md — 数据库型工作负载
- platform-tuning-notes.md — ARM/容器/虚拟化环境
- external-*-integration.md — 外部 skill 集成
- examples.md — 端到端优化场景走查示例
