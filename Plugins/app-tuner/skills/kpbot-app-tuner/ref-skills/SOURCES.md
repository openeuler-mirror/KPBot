# 内置 Subskill 来源说明

## 目录目标

本文件记录 skill 内置 `ref-skills/` 下各内置 skill 的来源、纳入时间与本地适配情况。

## 当前纳入列表

### 1. `network-io-performance`

- 原始来源仓库：`ref-skills/network-io-performance/`
- 原始来源路径：`network-io-performance/`
- 当前纳入日期：`2026-04-16`
- 当前接入方式：复制最小可运行单元到仓库内
- 本地适配：有
  - 由 `network-optimization` 统一入口调度
  - 当前框架优先从 `ref-skills/network-io-performance/` 查找
  - 仓库外历史路径仅作为 fallback

### 2. `cpu-affinity-optimization`

- 原始来源仓库：`ref-skills/cpu-affinity-optimization/`
- 原始来源路径：`cpu-affinity-agent/`
- 辅助来源仓库：`ref-skills/cpu-affinity-optimization/`
- 辅助来源路径：`multi-component-bind-core-agent/`
- 当前纳入日期：`2026-04-17`
- 当前接入方式：提炼通用 CPU 亲和性能力后内置到仓库
- 本地适配：有
  - 统一使用 `cpu-affinity-optimization` 命名，不暴露 `bind-core` 作为主命名
  - 由 `skills/kpbot-app-tuner/subskills/cpu-affinity-optimization/SKILL.md` 统一入口调度
  - 当前框架优先从 `ref-skills/cpu-affinity-optimization/` 查找
  - 仓库外历史路径不作为默认依赖，仅在框架内轻量规则回退时保留

### 3. `compiler-option-optimization`

- 原始来源仓库：`ref-skills/compiler-option-optimization/`
- 原始来源路径：`compiler-option-optimization/`（PR #11，commit `057b8fd`）
- 当前纳入日期：`2026-05-14`
- 当前接入方式：复制完整 skill 到仓库内（SKILL.md + scripts/perf_hotspot.sh）
- 本地适配：有
  - 由 `subskills/compiler-optimization` 统一入口调度
  - SKILL.md 适配为框架分析型 ref-skill 格式（去除直接实施指令，增加证据快照输入约定）
  - 脚本路径由 `~/.claude/skills/c-cpp-compiler-optimization/` 调整为 `ref-skills/compiler-option-optimization/`
  - 当前框架优先从 `ref-skills/compiler-option-optimization/` 查找

## 维护原则

- 仓库内 `ref-skills/` 是主框架默认使用的外部能力来源
- 若更新内置 subskill，需同步更新此文件中的来源与适配说明
- 若保留仓库外 fallback，必须在主 skill、依赖检查脚本和架构文档中显式说明

## 已合并移出

### `library-replacement`

- 原纳入日期：`2026-04-16`
- 移出日期：`2026-07-30`
- 移出原因：能力吸收合并到 `subskills/performance-library-selection/`（SKILL.md 内置 aarch64 全类别库检测工作流 + `references/optimization_kb.json` 知识库 + `scripts/` 检测脚本），不再作为独立 ref-skill 存在
- 残留清理：外部接入辅助资产（`check_external_library_replacement.sh`、`install_external_library_replacement.sh`、`external-library-replacement-integration.md`、根文档与设计文档中的引用）已同步删除或更新
