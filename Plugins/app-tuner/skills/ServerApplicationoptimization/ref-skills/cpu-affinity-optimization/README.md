# 通用 CPU 亲和性 Subskill

`ref-skills/cpu-affinity-optimization/` 是当前仓库内置的通用 CPU 亲和性专项能力。

它和根目录下的：

- `ref-skills/network-io-performance/`
- `ref-skills/library-replacement/`

一样，属于主框架默认优先发现和加载的仓库内置 subskill。

## 定位

这个 subskill 负责提供：

- CPU 拓扑与 NUMA 诊断
- 线程/进程亲和性分析
- 线程迁移与线程-CPU 分布均衡性分析
- IRQ 与业务线程冲突分析
- CPU 亲和性策略生成
- 验证与回滚骨架

它不直接替代总框架里的入口，而是由：

- `skills/server-application-optimization/subskills/cpu-affinity-optimization/SKILL.md`

统一适配和调度。

## 能力来源

本 subskill 由以下两类来源提炼而来：

1. `KunpengSDK/skills` 的 `cpu-affinity-agent`
   - 提供线程级诊断、NUMA/跨域分析、SMT 冲突、线程迁移和 CPU 亲和性策略知识
2. `multi-component-bind-core-agent`
   - 提供工作目录、诊断聚合、验证/回滚骨架和多步骤编排经验

## 组织约束

- 统一对外使用 `cpu-affinity-optimization` 命名
- 不把 `bind-core` 作为新的主命名暴露给总框架
- 不保留业务专属输入模型作为通用接口
- 默认生成证据、策略、验证计划和回滚计划，不默认自动执行系统级绑核变更

## 说明

如果当前仓库内置版本可用，总框架会优先使用它。

只有在仓库内置版本缺失时，`server-application-optimization` 才会回退到内部轻量规则路径。
