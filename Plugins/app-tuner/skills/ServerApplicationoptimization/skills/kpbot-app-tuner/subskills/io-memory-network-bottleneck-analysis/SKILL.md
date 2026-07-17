---
name: io-memory-network-bottleneck-analysis
description: 多指标瓶颈预筛子 skill，基于 iostat、vmstat、sar、perf 等工具采集的 CPU、iowait、磁盘利用率、网络带宽、内存压力和内存带宽代理指标，输出架构图所需的瓶颈分类。已实现真实采集逻辑（配套 detect_bottleneck.sh），工具缺失时保守回退。
---

# IO / Memory / Network Bottleneck Analysis

本子 skill 是性能采集工具层的轻量多指标预筛阶段。主 skill 必须先完成该预筛，再决定是否进入 CPU 深挖或转向网络、磁盘、内存、硬件等路线。

## Recommended Inputs

- `scenario_description` — 场景描述
- `target_pid` — 目标进程 PID
- `baseline_metrics` — 基线测试结果（含 CPU 利用率、吞吐、延迟）
- `evidence_inputs` — 原始观测数据路径
- 观测时长建议（默认 10 秒）

## Analysis Rules

### 磁盘瓶颈判定

检查项：
1. `iostat -x 1 N`：设备利用率 `%util > 80%` 为磁盘瓶颈
2. `vmstat 1 N`：`wa > 20%` 为 IO 等待瓶颈
3. 应用同步 IO 模式：若应用为同步写入（如 `fsync` 敏感），磁盘瓶颈门槛更低

### 网络瓶颈判定

检查项：
1. `sar -n DEV 1 N`：网口带宽接近物理上限（如 1GbE > 110MB/s）
2. 网络错误计数：`ifconfig` 或 `ip -s link` 中的 drops/errors
3. TCP 重传率：`netstat -s | grep retransmit`，> 1% 需关注

### 内存带宽瓶颈判定

检查项：
1. `perf stat -e cache-misses,cache-references`：cache miss rate > 20% 需关注
2. NUMA 跨节点访问：`numastat -p <pid>` 中远程节点访问占比
3. 内存带宽：`mbw` 或 `stream` 基准测试（如可用）

### 判定优先级

1. 先判磁盘（iowait 和 %util 最直观）
2. 再判网络（带宽利用率和错误计数）
3. 最后判内存带宽（需要 perf 或专用工具）
4. 若 CPU 满足饱和条件，返回 `cpu_bottleneck`
5. 均不满足且证据完整时返回 `no_active_bottleneck`
6. 证据不足无法判断时返回 `unknown_bottleneck`

## Required Output

必须返回以下结论之一：

- `network_bottleneck`
- `disk_bottleneck`
- `memory_capacity_bottleneck`
- `memory_bandwidth_bottleneck`
- `cpu_bottleneck`
- `unknown_bottleneck`
- `no_active_bottleneck`

脚本内部允许先判定更细粒度的结果：

- `cpu`
- `disk_io`
- `disk_bandwidth`
- `network`
- `memory_capacity`
- `memory_bandwidth`
- `unknown`

再映射回当前框架高层结论。

输出格式为 JSON，至少包含：

- `bottleneck_type` — 高层结论（上述枚举之一）
- `legacy_bottleneck_type` — 兼容旧报告的结论字段，可为空
- `detailed_bottleneck_type` — 细粒度判定
- `evidence` — 可量化证据字段（见下方）
- `confidence` — 置信度（`high` / `medium` / `low`）
- `fallback_notes` — 降级说明

`evidence` 至少包含可量化字段：

- `network_total_sendq_bytes`
- `network_total_recvq_bytes`
- `network_rx_bytes_per_sec`
- `network_tx_packets_per_sec`
- `disk_util_pct`
- `iowait_pct`

## Suggested Script

```bash
scripts/detect_bottleneck.sh <pid> <duration>
```

## Caller Behavior

主 skill 应按以下方式解释返回值：

- 若为 `network_bottleneck`，告知用户优先进入网络优化链路
- 若为 `disk_bottleneck`，告知用户优先进入磁盘优化方向
- 若为 `memory_capacity_bottleneck`，告知用户优先进入应用配置、OS 和硬件容量路线
- 若为 `memory_bandwidth_bottleneck`，告知用户优先进入内存带宽优化方向
- 若为 `cpu_bottleneck`，进入火焰图、热点函数、线程和 topdown 深挖
- 若为 `unknown_bottleneck`，补充采集，不得盲目调优
- 若为 `no_active_bottleneck`，输出报告并停止全局优化

如果工具缺失：

- 必须显式提示工具不足
- 采用保守判断
- 不得伪造瓶颈结论

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `iostat` | 磁盘利用率 | 磁盘瓶颈判定降级 |
| `vmstat` | iowait 和系统概览 | IO 等待判定降级 |
| `sar` | 网络带宽统计 | 网络瓶颈判定降级 |
| `perf` | 缓存和内存带宽分析 | 内存带宽判定降级 |
| `ip` | 网络错误和队列统计 | 部分网络分析降级 |

缺失任一工具时，对应子系统的瓶颈判定置信度降低，但不应阻止其他子系统的判定。

## Candidate Action Contract

本 skill 通常只输出瓶颈分类，不直接输出可执行变更。若必须生成 `candidate_actions[]`，每项必须包含 `action_id`、`action_type`、`precondition`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`；否则只输出 `required_evidence` 和下一步路由，不伪造优化动作。
