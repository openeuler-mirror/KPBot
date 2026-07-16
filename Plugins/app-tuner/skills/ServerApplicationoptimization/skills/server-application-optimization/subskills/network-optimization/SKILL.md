---
name: network-optimization
description: 在识别到网络侧瓶颈或次级网络瓶颈时，分析网卡、IRQ、队列、RSS/RPS/XPS、协议栈、丢包和远程压测网络影响，输出可验证、可回退的网络优化候选动作，作为 server-application-optimization 的子 skill 使用。
---

# Network Optimization

当当前证据显示网络可能限制吞吐、延迟或 CPU 利用率时使用本子 skill。分析阶段只读；真实修改必须回到主流程的串行执行验证阶段，并满足 `approved_execute`、目标实例身份、证据新鲜度和回退计划门禁。

## 何时触发

满足任一条件即进入本 skill：

- `perf`/火焰图中 TCP、UDP、socket、epoll、softirq、NAPI、SKB、netfilter、网卡驱动相关符号聚合占比高。
- `mpstat` 显示 `%soft`/`%irq` 高、单个 IRQ CPU 饱和、业务 CPU 压不满但网络 CPU 已满。
- `sar -n DEV,TCP,ETCP`、`ethtool -S`、`netstat -s` 显示 drops、errors、retrans、backlog overflow 或 queue imbalance。
- 远程压测时 RTT、客户端能力、服务端网卡 NUMA/IRQ/RPS 与应用绑核之间存在可疑关系。
- 主流程或 `io-memory-network-bottleneck-analysis` 把瓶颈分类为 network 或 network-secondary。

## 必读 Reference

按场景加载，避免一次性把所有网络细节放入上下文：

- 队列数、IRQ 隔离、RPS/XPS、TCP、coalesce、ring buffer、容器限制和案例：`references/network-playbook.md`
- 外部 network-io-performance 集成规则：`../../references/external-network-io-integration.md`
- 公共依赖和权限降级：`../../references/prerequisites.md`

## 输入证据

优先使用本轮 `current_run_id` 下的当前证据，不能用历史报告替代现场判断：

- `evidence_snapshot_dir/performance_signal_summary.json`
- `mpstat` 与 `pidstat` 并发日志，尤其是业务核、IRQ 核和全核视角。
- `/proc/interrupts` before/after、`/proc/softirqs`、IRQ smp_affinity。
- `ip -s link`、`ethtool -l/-c/-g/-k/-S <iface>`、`sar -n DEV,TCP,ETCP`。
- `/sys/class/net/<iface>/device/numa_node`、`local_cpulist`、`msi_irqs`。
- 服务端与客户端 RTT、压测客户端 CPU/网络是否已排除瓶颈。
- perf/PMU 诊断状态；若 `perf_pmu_status` 非 `passed`，必须说明网络栈热点分析降级。

## 分析流程

1. **确认网络是否真是瓶颈**
   - 区分客户端瓶颈、服务端网络瓶颈、应用配置瓶颈和 CPU/NUMA 瓶颈。
   - 远程压测必须同时看 RTT、客户端负载、服务端 `mpstat` 和 `pidstat`；禁止只用 `pidstat` 判断 CPU 是否压满。
2. **定位活跃接口与拓扑**
   - 找到承载压测流量的 `iface`，记录网卡 NUMA node、local CPU、队列数、IRQ 列表、应用 cpuset。
   - 判断 IRQ/RPS/XPS 候选 CPU 是否与应用核重叠、跨 NUMA 或受容器 cpuset 限制。
3. **按优先级生成候选动作**
   - 队列数和 IRQ 隔离优先，因为它决定网络核与业务核划分。
   - 然后评估防火墙/netfilter、RPS/XPS/RFS、TCP backlog/socket buffer、coalesce、ring buffer/offload。
   - 每轮只改变一个变量，保留 before/after 证据。
4. **输出验证计划**
   - 短诊断 15-20s 判断方向，最终候选至少 120s 或用户认可的正式窗口验证。
   - 以 QPS/吞吐、P99/P999、retrans/drops、IRQ max/min delta、softirq 分布、业务 CPU 利用率联合判断。

## 外部 Skill 路由

优先尝试仓库内外部网络 IO skill：

- 默认路径：`ref-skills/network-io-performance`
- 入口：`ref-skills/network-io-performance/SKILL.md`
- 脚本：`ref-skills/network-io-performance/scripts/network_io_check.sh`

只有当路径存在、脚本存在、环境允许只读网络检测时才调用。缺失或不可执行时不要中断主流程，记录 `fallback_reason`，回到本 skill 内部通用网络分析。

## 候选动作要求

每个 `candidate_actions[]` 必须包含：

- `action_id`
- `action_type`
- `precondition`
- `commands_dry_run`
- `commands_execute`
- `expected_gain`
- `risk`
- `validation`
- `rollback`
- `stop_or_reject_condition`
- `evidence_sources`

危险动作包括但不限于：`ethtool -L/-C/-G/-K`、写 `/proc/sys`、写 `/sys/class/net/**/rps_*`、修改 IRQ affinity、停止 `irqbalance`、调整防火墙。危险动作不得在分析阶段执行。

## 输出字段

至少输出：

- `network_analysis_mode`
- `external_network_skill_used`
- `fallback_reason`
- `active_iface`
- `network_numa_topology`
- `irq_isolation_status`
- `queue_count_findings`
- `rps_xps_findings`
- `tcp_stack_findings`
- `packet_loss_findings`
- `candidate_actions`
- `blocked_reasons`
- `confidence`

若证据不足，输出 `status=degraded|blocked`，并列出最小补采命令；不要把缺失证据解释成“网络无瓶颈”。
