# 内置 Subskill 来源说明

## 目录目标

本文件记录根目录 `ref-skills/` 下各内置 skill 的来源、纳入时间与本地适配情况。

## 当前纳入列表

### 1. `library-replacement`

- 原始来源仓库：`git@gitee.com:KunpengSDK/skills.git`
- 原始来源路径：`library-replacement/`
- 当前纳入日期：`2026-04-16`
- 当前接入方式：复制最小可运行单元到仓库内
- 本地适配：有
  - 由 `performance-library-selection` 统一入口调度
  - 当前框架优先从 `ref-skills/library-replacement/` 查找
  - 仓库外历史路径仅作为 fallback

### 2. `network-io-performance`

- 原始来源仓库：`git@gitee.com:chen-kai888/opencode.git`
- 原始来源路径：`network-io-performance/`
- 当前纳入日期：`2026-04-16`
- 当前接入方式：复制最小可运行单元到仓库内
- 本地适配：有
  - 由 `network-optimization` 统一入口调度
  - 当前框架优先从 `ref-skills/network-io-performance/` 查找
  - 仓库外历史路径仅作为 fallback

### 3. `cpu-affinity-optimization`

- 原始来源仓库：`git@gitee.com:KunpengSDK/skills.git`
- 原始来源路径：`cpu-affinity-agent/`
- 辅助来源仓库：`https://gitee.com/wuqicong/multi-component-bind-core-agent.git`
- 辅助来源路径：`multi-component-bind-core-agent/`
- 当前纳入日期：`2026-04-17`
- 当前接入方式：提炼通用 CPU 亲和性能力后内置到仓库
- 本地适配：有
  - 统一使用 `cpu-affinity-optimization` 命名，不暴露 `bind-core` 作为主命名
  - 由 `skills/server-application-optimization/subskills/cpu-affinity-optimization/SKILL.md` 统一入口调度
  - 当前框架优先从 `ref-skills/cpu-affinity-optimization/` 查找
  - 仓库外历史路径不作为默认依赖，仅在框架内轻量规则回退时保留

### 4. `compiler-option-optimization`

- 原始来源仓库：`git@gitee.com:KunpengSDK/skills.git`
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
