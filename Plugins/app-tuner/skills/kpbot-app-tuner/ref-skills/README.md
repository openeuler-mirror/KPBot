# 内置 Subskill 目录

`ref-skills/` 用于放置当前仓库内置的第三方或外部来源 skill，作为 `kpbot-app-tuner` 主框架的默认扩展能力来源。

当前已纳入：

- `network-io-performance`
- `cpu-affinity-optimization`

> `library-replacement` 的能力已吸收合并到 `subskills/performance-library-selection/`（SKILL.md + `references/optimization_kb.json` + `scripts/`），不再作为独立 ref-skill 存在。

使用约定：

- 主框架优先从仓库内 `ref-skills/` 查找这些能力
- 若仓库内目录缺失或依赖不满足，则回退到内部通用路径
- 实际使用来源会通过 `skill_source` 写入依赖检查结果和最终报告

集成入口：

- `performance-library-selection` 内置 aarch64 全类别库检测（不再走外部 ref-skill）
- `network-optimization` 统一接入 `ref-skills/network-io-performance`
- `cpu-affinity-optimization` 统一接入 `ref-skills/cpu-affinity-optimization`

详细来源与适配说明见：

- `ref-skills/SOURCES.md`
