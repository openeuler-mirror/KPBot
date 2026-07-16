# Network Optimization Playbook

## Scope

本文件承接 `network-optimization/SKILL.md` 的细节。进入网络瓶颈分析、远程压测网络诊断、队列数寻优或协议栈调参时读取。

## Evidence First

- `mpstat` 与 `pidstat` 必须和压测并发采集。`pidstat` 不包含硬中断，不能单独用它判断 CPU 是否压满。
- 远程压测先确认客户端不是瓶颈，再看服务端网络。记录 RTT、客户端 CPU/网卡、服务端活跃接口。
- 所有网络修改前保存 before：`ethtool -l/-c/-g/-k/-S`、`ip -s link`、`sysctl -a` 相关项、`/proc/interrupts`、RPS/XPS 文件、irqbalance 状态。

## Knowledge Base Anchors

| 技术 | 适用信号 | 案例/收益口径 | 验证指标 |
|---|---|---|---|
| RSS/RPS/XPS/IRQ 亲和 | softirq 高、IRQ 单核热点、业务核压不满 | AWS A1 memcached 网络密集调优案例，队列、RPS、irqbalance 和绑核协同，memcached 最高 3.9x | QPS、P99、IRQ delta、softirq 分布 |
| CQ/中断路径优化 | RDMA/高 PPS、中断处理成本高 | Alibaba Cloud Linux SMC-R 数据路径 CQ interrupt 优化，QPS 约 +40% | PPS、CQ 中断、CPU softirq |
| TCP buffer/backlog | retrans、listen overflow、backlog drop | 通用内核网络参数调优 | retrans、ListenDrops、P99 |
| Coalesce/Ring | 中断过密或 drops | 按网卡/驱动验证，收益依负载变化 | irq rate、drops、tail latency |

知识库来源：`L1-BIOS-OS/Network Parameter Tuning.md`。

## Queue Count And IRQ Isolation

队列数决定网络核和业务核的资源划分，应先于 RPS、TCP、coalesce 和 ring buffer 调整。

```text
total CPUs = network CPUs (IRQ + softirq) + application CPUs
```

判断：

- 队列过少：IRQ CPU idle 接近 0，softirq 高，网络层饱和。
- 队列过多：网络占用过多 CPU，业务核减少，吞吐下降。
- 最优：网络核刚好不饱和时的最少队列数。

流程：

1. 读取 `ethtool -l <iface>`、`local_cpulist`、`msi_irqs`、应用 `Cpus_allowed_list`。
2. 停止或约束 `irqbalance`，把 IRQ 绑定到 NIC local CPU 中不与应用重叠的 CPU。
3. 应用进程或 cpuset 排除 IRQ CPU。
4. 每轮只改 `ethtool -L <iface> combined <N>`，短压测 15-20s。
5. 记录 QPS、P99、IRQ CPU idle/soft/irq、`/proc/interrupts` delta。
6. 最终候选用 120s 或用户认可窗口验证。

初始候选：

```text
N = min(application_cpu_count / 4, nic_max_combined / 2)
```

决策矩阵：

| 观测 | 判断 | 动作 |
|---|---|---|
| IRQ CPU idle=0%, soft > 80% | 网络核不够 | 队列数增加 25%-50% |
| IRQ CPU idle=0-3%, soft 60%-80% | 临界饱和 | 微调 ±2 队列找峰值 |
| IRQ CPU idle>5%, 业务核 idle>5% | 网络核过多或负载不足 | 减少队列或先确认客户端瓶颈 |
| max/min IRQ delta > 3x | RSS 热点 | 尝试相邻队列数、检查 RSS hash/RPS |
| 加队列后 QPS 下降 | 网络核抢占业务核或热点未改善 | 回退上一有效值 |

## RPS/RFS/XPS

RPS 不是默认收益项，必须按 NUMA 和 CPU 争抢判断：

- NIC NUMA node 与应用 node 相同，且存在不与应用重叠的 local CPU：可进入小轮次验证。
- NIC 与应用跨 NUMA：优先 IRQ 隔离，不默认开 RPS；跨 NUMA RPS 常见负收益。
- RPS/XPS mask 必须同时给出十六进制和 CPU 列表，避免 mask 映射错误。
- 候选为负收益时立即回退，不得带入下一轮。

RFS 只有在 RPS 已验证收益后再叠加：

```bash
echo 32768 > /proc/sys/net/core/rps_sock_flow_entries
for f in /sys/class/net/<iface>/queues/rx-*/rps_flow_cnt; do echo 2048 > "$f"; done
```

## Protocol Stack Actions

按顺序独立验证：

1. 防火墙/netfilter：热点出现 `nft_do_chain`、`ipt_do_table` 或规则过多时，测试环境可停用，生产优先加白名单规则。
2. TCP backlog/buffer：关注 `ListenOverflows`、`ListenDrops`、`TCPBacklogDrop`、retrans、prune、collapse。
3. Coalesce：`ethtool -C` 在吞吐与尾延迟之间权衡；低延迟和高吞吐用不同候选。
4. Ring buffer：`ethtool -G` 仅在 drops/overruns 或队列溢出证据明确时调整。
5. Offload：GRO/GSO/TSO/checksum 按业务协议和延迟目标验证，不做无证据全关/全开。

## Container, VM And Permission Limits

- 容器内 `ethtool -L/-C/-G`、防火墙和 IRQ affinity 往往无效，通常需要宿主机执行。
- 虚拟机取决于虚拟网卡、队列暴露、PMU 和中断能力映射；不可见时标记降级。
- 非 root 用户只能做只读诊断或生成命令计划；写 sysctl、sysfs、IRQ affinity 需要 root/capability。

## Reject Conditions

候选出现以下任一情况应回退或停止：

- QPS/吞吐下降超过测试噪声阈值，默认 >2%。
- P99/P999 明显恶化且用户目标包含延迟。
- drops、retrans、ListenDrops、BacklogDrop 增长。
- IRQ 与应用 CPU 重叠或跨 NUMA 解释不清。
- 当前证据 run_id、目标实例身份或压测条件不一致。
