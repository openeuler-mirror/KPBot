---
name: cpu-affinity-optimization
description: 面向通用服务的 CPU 亲和性专项 subskill，聚焦 CPU 拓扑、NUMA、线程/进程亲和性、线程迁移、线程-CPU 均衡性、中断冲突、CPU 亲和性策略生成，以及验证与回滚骨架。
---

# CPU Affinity Optimization

`ref-skills/cpu-affinity-optimization/` 是当前仓库内置的通用 CPU 亲和性能力源。

> **本文件是底层能力定义，不应被主流程或外部工具直接引用。** 上层调用应通过 `skills/server-application-optimization/subskills/cpu-affinity-optimization/SKILL.md`（统一入口适配层）进行，该适配层负责决定是否委派给本文件以及如何回退。

它不是单独给用户直接切换使用的第二入口，而是由主框架中的：

- `skills/server-application-optimization/subskills/cpu-affinity-optimization/SKILL.md`

统一适配和调用。

## 来源提炼

当前能力来自两个上游来源的归并提炼：

1. `KunpengSDK/skills` 中的 `cpu-affinity-agent`
   - 提供线程级诊断、NUMA/跨域分析、线程迁移、线程分布、SMT 冲突和 CPU 亲和性策略知识
2. `multi-component-bind-core-agent`
   - 提供工作目录初始化、诊断聚合、验证/回滚骨架、多步骤编排经验

本地提炼后的原则：

- 保留通用能力
- 去掉 VLLM、MySQL、Redis、Nginx 等业务专属硬编码
- 对外统一使用 `cpu-affinity-optimization` 命名
- 默认生成诊断结果、建议和脚本骨架，不默认自动执行 CPU 亲和性变更

## 能力分层

### 1. 诊断层

负责采集和分析：

- CPU 拓扑
- NUMA 拓扑
- 线程/进程当前亲和性
- 线程 CPU 分布
- 线程迁移
- IRQ 与业务线程冲突
- 热点 CPU 与线程-CPU 偏斜

### 2. 策略层

负责生成：

- `binding_strategy_candidates`
- `selected_binding_strategy`
- `rebalance_recommendation`
- `binding_validation_plan`

策略要综合以下证据：

- 工作负载类型
- 热点线程
- NUMA 拓扑
- 容器 cpuset 约束
- IRQ 分布
- 线程-CPU 均衡性

### 3. 落地层

提供轻量执行骨架：

- 工作目录初始化
- 诊断结果聚合
- 验证脚本
- 回滚脚本

默认行为是“生成并建议执行”，而不是主流程自动执行。

## 推荐脚本

- `scripts/init_workspace.sh`
- `scripts/analyze_system_topology.sh`
- `scripts/collect_thread_affinity.sh`
- `scripts/collect_thread_distribution.sh`
- `scripts/collect_thread_migration.sh`
- `scripts/collect_irq_affinity.sh`
- `scripts/aggregate_diagnosis.sh`
- `scripts/generate_affinity_strategy.sh`
- `scripts/verify_affinity.sh`
- `scripts/rollback.sh`

## 推荐输入

- `target_pid` / `target_pids`
- `application_name`
- `workload_type`
- `thread_info`
- `hot_threads`
- `environment_type`
- `cpuset_limit`
- `numa_topology`
- `interrupt_info`
- `current_round`
- `effective_config_snapshot`
- `benchmark_script_provided`
- `restart_allowed`

## 推荐输出

- `affinity_analysis_mode`
- `process_role_summary`
- `thread_role_classification`
- `numa_affinity_findings`
- `cpu_balance_status`
- `thread_cpu_skew`
- `hot_cpu_list`
- `irq_cpu_conflict_notes`
- `binding_strategy_candidates`
- `selected_binding_strategy`
- `binding_script_path`
- `rollback_script_path`
- `binding_validation_plan`
- `next_round_candidate`
- `skill_source`

## 迭代优化语义

在 `server-application-optimization` 的轮次编排里，本 subskill 不只输出建议，还要输出：

- 当前轮是否应优先推进 CPU 亲和性优化
- 当前轮是否已经完成 CPU 亲和性验证
- 是否应把当前 CPU 亲和性动作继续保留到下一轮
- 若当前证据不足，是否应暂缓到后续轮次
