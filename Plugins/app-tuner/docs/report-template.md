# 服务器应用优化报告模板

> 字段名标注格式：`<!-- schema: field_name -->`，对应 `references/report-schema.md` 中的定义。

## 1. 项目背景 / Background <!-- schema: scenario_name -->

- 项目名称 / Project Name:
- 应用名称 / Application Name:
- 优化场景 / Optimization Scenario:
- 优化目标 / Optimization Goal:
- 报告日期 / Report Date:
- 执行人 / Owner:

## 2. 环境信息备份 / Environment Baseline <!-- schema: environment_snapshot -->

### 2.1 硬件信息 / Hardware

- CPU:
- NUMA:
- Memory:
- Disk:
- NIC:

### 2.2 软件信息 / Software

- BIOS Version:
- OS:
- Kernel:
- Compiler:
- Runtime:
- Container/Virtualization:
- Environment Type:

### 2.3 初始关键配置 / Initial Key Configurations

- BIOS 配置摘要:
- OS/Kernel 配置摘要:
- 网络配置摘要:
- 应用部署配置摘要:
- THP 状态:
- HugePages 状态:
- NUMA 绑定策略:
- 构建系统 / Build System:
- 编译选项摘要 / Build Flags:
- Container CPUSET:
- Container Memory Limit:

### 2.4 前置依赖状态 / Dependency Status <!-- schema: dependency_status, missing_dependencies, degraded_capabilities, skill_source -->

- 依赖状态总览 / Dependency Summary:
- 能力来源 / Skill Source: `repo_local_subskill` / `unavailable`
- 缺失依赖 / Missing Dependencies:
- 已降级能力 / Degraded Capabilities:
- 当前置信度 / Confidence:

## 3. 测试场景与部署说明 / Scenario & Deployment <!-- schema: deployment_method, raw_evidence_paths -->

- 测试拓扑 / Test Topology:
- 部署方式 / Deployment Method:
- 工作负载模型 / Workload Model:
- 测试脚本 / Benchmark Script:
- 测试窗口 / Test Window:
- 约束条件 / Constraints:
- 是否允许重启 / Restart Allowed:
- 是否优先在线修改 / Online Change Preferred:

## 4. 基线数据 / Baseline

### 4.1 基线指标 / Baseline Metrics <!-- schema: baseline_metrics -->

| 指标 Metric | 基线值 Baseline | 单位 Unit | 说明 Notes |
| --- | --- | --- | --- |
| Throughput |  |  |  |
| Latency P50 |  |  |  |
| Latency P99 |  |  |  |
| CPU Utilization |  |  |  |
| Memory BW |  |  |  |
| Disk IOPS/BW |  |  |  |
| Network BW/PPS |  |  |  |

### 4.2 原始证据 / Raw Evidence <!-- schema: raw_evidence_paths -->

- 环境快照路径:
- 基线日志路径:
- 监控数据路径:
- perf/BPF 数据路径:
- 依赖检查结果路径:

## 5. 瓶颈定位过程 / Bottleneck Analysis

### 5.1 非 CPU 瓶颈排查

- 网络瓶颈结论:
- 磁盘瓶颈结论:
- 内存带宽瓶颈结论:
- 是否进入 CPU 深挖:

### 5.2 CPU 侧热点与特征

- 热点进程:
- 热点线程:
- 热点函数:
- topdown 特征:
- BPF 观察摘要:

### 5.3 数据库型工作负载的应用配置专项分析 / Database-oriented Application Config Analysis <!-- schema: database_findings -->

说明：

- 本节用于承接数据库型工作负载在 `application-config-optimization` 中完成的专项分析
- 若引用了 `database-workload-analysis`，其结论也应汇总到本节，而不是单独拆出主流程章节

- 工作负载类型 / Workload Type:
- 数据库引擎 / Database Engine:
- 数据库内部状态是否为主要因素:
- 数据库专项结论 / Database Findings:
- MySQL/InnoDB 关键观察:
  - Buffer Pool:
  - Row Lock Wait:
  - Adaptive Hash Index:
  - AHI 当前状态 / AHI Current State:
  - AHI 推荐动作 / AHI Recommendation:
  - AHI 决策依据 / AHI Decision Evidence:
  - 是否纳入下一轮串行验证 / AHI Next Validation Step:
  - Checkpoint / Flush:
  - SHOW ENGINE INNODB STATUS 摘要:
  - SHOW GLOBAL STATUS 摘要:
  - SHOW VARIABLES 摘要:

### 5.4 平台专项检查 / Platform-specific Checks <!-- schema: platform_notes -->

- NUMA 拓扑与跨节点访问结论:
- THP 检查结果:
- HugePages 适用性结论:
- ARM/aarch64 平台专项说明:
- Container 环境限制说明:

### 5.5 测试规范与结果可信度 / Test Methodology

- 是否重启 MySQL / Restart Before Test:
- 是否使用交替测试法 / Alternating Test Method:
- 是否检测到 TPS 衰减风险 / TPS Decay Warning:
- 最终性能值 / Final Performance Value:

### 5.6 优化前后 perf 热点对比 / Perf Hotspot Diff

| 排名 Rank | 优化前函数 Before | CPU% | 优化后函数 After | CPU% | 变化 Change |
| --- | --- | --- | --- | --- | --- |
| 1 |  |  |  |  |  |
| 2 |  |  |  |  |  |
| 3 |  |  |  |  |  |

### 5.7 ARM 架构编译选项优化建议 / ARM Architecture Flag Suggestions

- 当前架构选项 / Current Arch Flags:
- 热点驱动候选项 / Hotspot-driven Candidates:
- 推荐架构选项 / Recommended Arch Flags:
- 建议原因 / Arch Flag Reason:
- 是否仍有进一步编译优化空间 / Further Compiler Optimization Potential:

### 5.8 线程-CPU 均衡性摘要 / Thread-CPU Balance Summary

- CPU 均衡状态 / CPU Balance Status:
- 热点 CPU / Hot CPU List:
- 线程-CPU 偏斜说明 / Thread CPU Skew:
- 中断冲突说明 / IRQ CPU Conflict Notes:
- 重新平衡建议 / Rebalance Recommendation:
- CPU 亲和性分析来源 / CPU Affinity Skill Source:
- 线程角色分类摘要 / Thread Role Classification:
- NUMA 亲和结论 / NUMA Affinity Findings:
- 选中策略 / Selected Affinity Strategy:
- 策略脚本路径 / Binding Script Path:
- 回滚脚本路径 / Rollback Script Path:
- 验证计划 / Binding Validation Plan:

## 6. 优化动作清单 / Optimization Actions <!-- schema: optimization_actions, online_vs_restart_changes, stackability_notes -->

| 阶段/轮次 Phase/Round | 优化类型 Type | 动作描述 Action | 变更对象 Scope | 在线或重启 Change Mode | 叠加说明 Stackability | 风险 Risk | 是否回滚友好 Rollback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Application Config |  |  |  |  |  |  |
| 2 | Performance Library |  |  |  |  |  |  |
| 3 | CPU Affinity |  |  |  |  |  |  |
| 4 | OS |  |  |  |  |  |  |
| 5 | BIOS |  |  |  |  |  |  |
| 6 | Compiler |  |  |  |  |  |  |

## 7. 分阶段累计验证结果 / Cumulative Validation Summary <!-- schema: cumulative_validation_summary, effective_config_history, current_round_summary, selected_optimization_actions, rejected_optimization_actions, iteration_decision, iteration_decision_reason, next_round_focus, stop_reason -->

| 轮次 Round | 当前生效配置 Effective Config | 本轮新增动作 New Actions | 本轮主要证据 Evidence Summary | 当前主瓶颈 Current Bottleneck | 相对上一轮变化 Delta vs Prev | 相对初始基线累计变化 Cumulative vs Baseline | 是否继续 Continue | 继续/停止原因 Decision Reason | 下一轮重点 Next Round Focus |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Round 1 |  |  |  |  |  |  |  |  |  |
| Round 2 |  |  |  |  |  |  |  |  |  |
| Round 3 |  |  |  |  |  |  |  |  |  |

## 8. 优化动作耗时统计 / Optimization Timing <!-- schema: agent_timing_summary, per_skill_timing_summary, optimization_timing, optimization_timing_details -->

### 8.1 各优化分析项耗时明细 / Per-Optimization Timing Details

| 轮次 Round | 优化项 Optimization Item | 分析耗时 Analysis | 实施耗时 Implementation | 验证耗时 Validation | 总耗时 Total | 备注 Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Round 1 | OS |  |  |  |  |  |
| Round 1 | BIOS |  |  |  |  |  |
| Round 1 | Network |  |  |  |  |  |
| Round 1 | CPU Affinity |  |  |  |  |  |
| Round 1 | Compiler |  |  |  |  |  |
| Round 1 | Library |  |  |  |  |  |
| Round 1 | Application Config |  |  |  |  |  |

### 8.2 每轮汇总耗时 / Per-Round Timing Summary

| 轮次 Round | 总分析耗时 Total Analysis | 总实施耗时 Total Implementation | 总验证耗时 Total Validation | 轮次总耗时 Round Total | 备注 Notes |
| --- | --- | --- | --- | --- | --- |
| Round 1 |  |  |  |  |  |
| Round 2 |  |  |  |  |  |
| Round 3 |  |  |  |  |  |

### 8.3 各 Skill 汇总耗时 / Per-Skill Timing Summary

| Skill | 记录数 Records | 分析耗时 Analysis | 实施耗时 Implementation | 验证耗时 Validation | 总耗时 Total | 证据 Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Application Config |  |  |  |  |  |  |
| Performance Library |  |  |  |  |  |  |
| CPU Affinity |  |  |  |  |  |  |
| Network |  |  |  |  |  |  |
| Compiler |  |  |  |  |  |  |
| OS |  |  |  |  |  |  |
| BIOS |  |  |  |  |  |  |

## 9. 最终综合收益 / Overall Gain <!-- schema: final_bottleneck, stop_reason -->

- 最终吞吐提升 / Final Throughput Gain:
- 最终时延改善 / Final Latency Improvement:
- CPU 利用率变化 / CPU Utilization Change:
- 资源成本变化 / Resource Cost Change:
- 累计综合提升 / Overall Improvement %:
- 验证口径 / Validation Model: 串行叠加验证
- 是否存在不可线性叠加项 / Non-linear Stackability Notes:
- 最终停止原因 / Final Stop Reason:

## 10. 风险与回退建议 / Risks & Rollback <!-- schema: rollback_notes -->

- 高风险变更:
- 回退步骤:
- 持续观察项:
- 不建议立即上线项:

## 11. 失败尝试 / Failed Attempts

| 优化项 Optimization | 预期 Expected | 实际 Actual | 原因 Reason |
| --- | --- | --- | --- |
|  |  |  |  |

## 12. 待补充项 / Open Items <!-- schema: next_steps -->

- 待补采数据:
- 待确认假设:
- 待实施优化:
- 后续验证计划:

## 13. 结论 / Conclusion

- 当前主瓶颈:
- 最有效优化手段:
- 建议优先落地项:
- 下一阶段建议:
