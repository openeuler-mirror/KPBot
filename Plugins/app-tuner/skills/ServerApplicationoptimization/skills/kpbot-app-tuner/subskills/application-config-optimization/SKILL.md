---
name: application-config-optimization
description: 综合瓶颈分析结果输出线程数、队列、批量、缓存、连接等应用层最佳性能配置建议，作为 kpbot-app-tuner 的子 skill 使用。
---

# Application Config Optimization

当需要将平台、CPU 和热点分析结论沉淀为应用层最佳配置时，使用本子 skill。

## 数据库型工作负载专项承接

本子 skill 统一承接数据库型工作负载的专项分析。当检测到 `workload_type == database` 时，本子 skill 负责决定是否需要深入数据库内部状态分析，并按需引用：

- `subskills/database-workload-analysis/SKILL.md`

`database-workload-analysis` 提供数据库通用分析框架、MySQL/InnoDB 示例和 AHI 判断逻辑，但不作为主流程的独立阶段存在。其分析结果由本子 skill 汇总后统一输出给主 skill。

## 大数据框架专项承接

本子 skill 统一承接大数据框架工作负载的专项分析。当检测到 `workload_type` 匹配 spark/flink 或检测到相关配置文件时，按需引用：

- `ref-skills/bigdata-framework-optimization/SKILL.md`

`bigdata-framework-optimization` 提供 Spark、Flink 等大数据框架的参数推荐表格和适用条件。其分析结果由本子 skill 汇总后统一输出给主 skill。

重点关注：

- 线程数
- 队列深度
- 批量大小
- 缓存大小
- 连接池参数
- 并发模型
- 测试规范与结果可比性
- 配置项之间的协同关系

## Recommended Inputs

- `workload_type` — 工作负载类型（database、compute、rpc 等）
- `baseline_metrics` — 基线测试结果（含 TPS、QPS、p99 延迟等）
- `target_pid` — 目标进程 PID
- `current_round` — 当前优化轮次
- `effective_config_snapshot` — 当前已生效配置快照
- `previous_round_summary` — 上一轮优化摘要
- `restart_allowed` — 是否允许重启服务
- `benchmark_sequence_mode` — 测试模式（alternating / sequential）

## Expected Outputs

- `database_findings` — 数据库专项分析结论（数据库型工作负载时）
- `tps_decay_warning` — TPS 衰减告警
- `synergy_candidate_configs` — 单独负收益但组合有效的候选项
- `recommended_test_method` — 推荐测试方法
- `current_round_summary` — 当前轮优化摘要
- `selected_optimization_actions` — 当前轮被选中的优化动作
- `rejected_optimization_actions` — 当前轮被拒绝或暂缓的动作
- `iteration_decision` — `continue` / `stop`
- `iteration_decision_reason` — 继续或停止原因

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| 目标服务启停命令 | 配置变更后重启验证 | 无法验证配置变更效果 |
| 压测工具（sysbench 等） | 基线和复测 | 无法量化配置收益 |
| `mysql` 客户端 | 数据库状态采集 | 数据库型工作负载分析降级 |

## Test Methodology

对应用配置优化，默认采用规范化测试方法，而不是简单顺序对比：

- 每项配置变更前优先重启 MySQL 或目标服务，至少在首轮绝对对比时必须这样做
- 推荐使用交替测试法，而不是 `A A A -> B B B` 的顺序测试法
- 如果连续 3 次测试结果下降超过 2%，应输出 TPS 衰减警告
- 报告只记录最终可比较的性能值，不要求保存时序性能数据

推荐交替测试模式：

```text
for config in A B A B A B; do
  restart_service_with_config $config
  run_benchmark
done
```

## Synergy Detection

当前版本要求识别“单独负收益但组合有效”的配置项。

处理规则：

1. 先单独测试每项配置，记录独立收益
2. 若独立收益为负，则标记为 `synergy_candidate`
3. 将所有正向优化与 `synergy_candidate` 组合测试
4. 若组合中该候选项有效，则保留
5. 若组合仍无效，则剔除

输出中必须明确标注：

- 哪些配置仅在组合中生效
- 是否建议单独应用
- 是否纳入下一轮串行累计验证
- 哪些配置应作为 `next_round_candidate_configs`

## Dynamic Ordering

应用配置优化顺序不是固定不变的。

- 若 `bottleneck_classification == cpu_bottleneck` 且 `workload_type == database`
  - 推荐顺序：应用配置 → 性能库 → 亲和性 → OS → BIOS → 编译
- 若 `bottleneck_classification == cpu_bottleneck` 且 `workload_type == compute`
  - 推荐顺序：编译 → 性能库 → 亲和性 → OS → BIOS → 应用配置

输出应给出建议值、适用负载、风险、复测方法、推荐测试方法，以及配置协同关系说明。

在迭代编排语义下，本子 skill 还应补充：

- 哪些配置是当前轮最值得验证的动作
- 哪些配置虽然当前不优先，但应纳入下一轮继续验证
- 哪些配置已被当前轮证据否决，应暂缓或淘汰

## Candidate Action Contract

每个 `candidate_actions[]` 或 `selected_optimization_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。需要重启服务的配置必须明确 `restart_required=true`，并在 rollback 中给出恢复原配置和重启验证步骤。
