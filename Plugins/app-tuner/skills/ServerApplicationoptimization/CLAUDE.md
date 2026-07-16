# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

服务器应用优化 Agent skill 框架。将服务器应用性能优化流程沉淀为可被 Claude Code、Codex、Cursor、OpenCode 和其他编程 Agent 加载和复用的 skill 结构。当前版本为"框架 + 文档 + 子 skill + 辅助脚本"，部分优化逻辑仍为占位实现。

## Architecture

### 单一主源 + 多平台轻量入口

主源位于 `skills/server-application-optimization/`。轻量入口为薄跳转层（仅含 frontmatter 和主源路径指向），各平台通过轻量入口或直接主源加载 skill：

- `skills/server-application-optimization/` — 主源（唯一修改目标）
- `.claude/skills/server-application-optimization/` — Claude Code Dynamic Workflows 入口（薄跳转 + 运行时契约）
- `.opencode/skills/server-application-optimization/` — OpenCode 入口（薄跳转）
- `.agents/skills/server-application-optimization/` — 通用 Agent 入口（薄跳转）
- Codex / Cursor — 直接读取主源目录

**轻量入口只做发现和跳转，不复制主源内容。修改只需改主源。**

### 编排型 Skill 架构

主 skill (`SKILL.md`) 是编排器，不承载全部实现逻辑。采用渐进式加载：

- 启动门控强制加载 `application-agent-architecture.md`、`input-contract.md`、`checklist.md`
- 子 skill 的 SKILL.md **禁止在启动阶段提前批量读取**，由动态 skill 分析阶段的各 subagent 在独立上下文中加载
- 其余 references 按阶段按需加载

### Claude Code Dynamic Workflows

Claude Code 入口不再把优化过程当作固定线性清单执行，而是按 Dynamic Workflows 运行。主 skill 负责维护全局状态、门控、候选 skill 列表、coverage 计划和报告 trace；具体优化能力按性能证据动态加载。

基础 workflow 仍按上游门控推进：

用户界面确认 → 环境诊断与备份 → 基线确认 → 性能采集与瓶颈识别 → 候选 skill 列表生成 → 串行迭代验证 → coverage → 报告输出 → review/环境还原 → 案例归档

动态部分由 `candidate_skill_list` 和 coverage 阶段决定：

- 启动时只加载主源和 Phase 1 强制 references，不提前批量加载所有子 skill。
- 基线确认和深度证据采集完成后，读取 `references/candidate-skill-list.md` 生成候选列表。
- 优先加载候选列表命中的 subskill，例如第三方 so 热点命中 `performance-library-selection`，网络热点函数命中 `network-optimization`。
- 每个候选或 coverage workflow 独立记录轮次收益、停止条件、风险、回退和 `workflow_trace`。
- 单个 workflow 停止但仍有候选或 coverage 项时，继续下一个 workflow；不得提前全量加载所有 subskill。

Claude Code 必须持续维护 `current_workflow_state`：

```json
{
  "current_gate": "candidate-routing",
  "completed_gates": ["scenario-intake", "environment-baseline", "bottleneck-detection"],
  "blocked_gate": null,
  "next_gate": "candidate-skill-iteration",
  "current_run_id": "run-YYYYMMDD-HHMMSS",
  "evidence_status": "current",
  "candidate_skill_list": [],
  "coverage_skill_list": [],
  "workflow_trace": []
}
```

### 动态候选子 skill

每个子 skill 位于 `skills/server-application-optimization/subskills/<name>/SKILL.md`：

- `io-memory-network-bottleneck-analysis` — 非 CPU 瓶颈预筛（Step 6 加载）
- `cpu-affinity-optimization`、`bios-optimization`、`os-optimization`、`network-optimization`、`application-config-optimization`、`compiler-optimization`、`performance-library-selection`、`accelerator-optimization`、`hardware-upgrade-analysis`、`other-optimization` — 按 `candidate_skill_list` 优先加载，候选完成后按 coverage 阶段补齐结论
- `database-workload-analysis` — 由 `application-config-optimization` 按需引用，不单独路由

### 外部 skill 集成

仓库内置 `ref-skills/` 下的第三方 skill（`library-replacement`、`network-io-performance`、`cpu-affinity-optimization`），通过检测脚本自动发现，缺失时显式降级并记录 `fallback_reason`。

### 单点定义原则

关键知识点只在指定文件中定义一次（见 `references/reading-guide.md`）：

| 知识点 | 唯一定义位置 |
|--------|-------------|
| 工具依赖与降级策略 | `references/prerequisites.md` |
| 主流程详细步骤 | `references/workflow.md` |
| 输入输出字段 | `references/input-contract.md` |
| 报告字段定义 | `references/report-schema.md` |
| Subagent 编排契约 | `references/subagent-orchestration.md` |
| 优化方向路由 | `references/optimization-decision-tree.md` |

## Development Commands

无构建步骤。验证变更的方式：

```bash
# Shell 脚本静态检查（推荐安装 shellcheck）
shellcheck skills/server-application-optimization/scripts/*.sh

# 环境备份测试
skills/server-application-optimization/scripts/backup_environment.sh ./output/env

# 报告目录初始化
skills/server-application-optimization/scripts/init_report.sh ./output/report demo-scenario

# 占位 benchmark
skills/server-application-optimization/scripts/run_placeholder_benchmark.sh demo-scenario

# MySQL 状态采集
skills/server-application-optimization/scripts/collect_mysql_status.sh --output-dir ./output/mysql

# 收益汇总
python3 skills/server-application-optimization/scripts/summarize_improvement.py \
  --baseline baseline.json --candidate tuned.json --round-name round-2

# 动态 skill 任务包生成与合并
skills/server-application-optimization/scripts/create_subagent_tasks.py --scenario demo --output-dir ./output/dynamic-skill-tasks
skills/server-application-optimization/scripts/merge_subagent_results.py \
  --results-dir ./output/dynamic-skill-results --output-candidate-pool ./output/candidate_pool.json
```

## Coding Conventions

- **Shell 脚本**：`#!/usr/bin/env bash` + `set -euo pipefail`，引用变量加引号，输出路径显式指定
- **Python 脚本**：仅使用标准库（除非有合理理由），4 空格缩进，保持小型化
- **Markdown**：简洁标题，相对链接，命令示例从仓库根目录执行
- **子 skill 命名**：小写 kebab-case（如 `cpu-affinity-optimization`）
- **输出路径**：所有生成输出放在 `output/` 目录下（已在 .gitignore 中忽略）
- **每个 reference 文件不超过 200 行**，超过时应考虑进一步拆分
- **Commit 风格**：短祈使句 + 前缀（`docs:`、`fix:`、`test:`、`feat:`）

## Git Rules

- **禁止 force push 到 main 分支**。main 分支只能通过 Merge Request 合并，不得使用 `git push --force` 或 `git push --force-with-lease` 推送到 main。
- 所有变更必须先提交到功能分支，再通过 AtomGit Merge Request 合并到 main。
- 合并时如需 squash，在 AtomGit MR 页面选择 squash merge 选项，不要本地 squash 后 force push。
- MR 描述应说明变更的 skill 行为、执行的验证命令、以及是否同步更新了轻量入口。

## Key Files Quick Reference

- `skills/server-application-optimization/SKILL.md` — 主 skill，AI Agent 首先加载
- `skills/server-application-optimization/references/workflow.md` — 完整工作流细节
- `skills/server-application-optimization/references/checklist.md` — 各阶段执行检查清单
- `skills/server-application-optimization/references/reading-guide.md` — 文档关系与单点定义表
- `docs/architecture-4plus1.md` — 架构设计
