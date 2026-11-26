# network-collector — 网络与中断采集指南

## 概述

采集网络层与中断层证据：带宽、丢包、重传、socket 队列、网卡配置、RSS/RPS/XPS、IRQ 亲和性与分布、NUMA 拓扑、softirq。数据供 network-optimization、cpu-affinity 等 subskill 消费。

## 模式与指标

| 模式 | 工具 | 指标 | 典型用途 |
|------|------|------|---------|
| `--iface` | ip + sysfs | UP 接口、carrier、NUMA、speed | 活跃接口发现 |
| `--bandwidth` | sar -n DEV | rx/tx kB/s、ifutil% | 带宽饱和判定 |
| `--drop` | /proc/net/dev 双采样 | rx/tx_drop delta、字节速率 | 丢包零容忍判定 |
| `--retrans` | netstat -s / nstat | 重传段、ListenDrops、BacklogDrop | 重传率计算 |
| `--sock` | ss -tinp | Recv-Q/Send-Q、retrans 段 | socket 堆积判定 |
| `--nic` | ethtool -l/-c/-g/-k/-S/-i | 队列数、coalesce、ring、offload、驱动、硬件计数器 | before 快照、调优回退基准 |
| `--rss` | sysfs | rps_cpus、xps_cpus、rps_flow_cnt | 多队列分发现状 |
| `--irq` | /proc/interrupts + sysfs | IRQ→CPU 绑定、effective_affinity | IRQ 对齐检查 |
| `--irqdelta` | /proc/interrupts 双采样 | per-IRQ delta、max/min ratio | 中断不均衡/RSS 塌缩 |
| `--numa` | sysfs | numa_node、local_cpulist、msi_irqs | IRQ-NUMA 对齐规则输入 |
| `--softirq` | /proc/softirqs | NET_RX/NET_TX per-CPU | 网络核饱和分析 |

## 阈值口径（源自 app-tuner 实践，供 subskill/agent 消费）

| 指标 | 阈值 | 判定 |
|------|------|------|
| socket 队列 | recvq 或 sendq > 131072 (128KB) | 网络瓶颈（medium 置信度） |
| 丢包增量 | delta > 0（零容忍） | 网络瓶颈 |
| errors/drops/collisions | > 0（netstat -i，零容忍） | 报告问题 |
| TCP 重传率 | > 1% | 需关注（重传率 = RetransSegs 增量 / OutSegs 增量） |
| 带宽 | 接近链路上限（1GbE > 110MB/s） | 带宽饱和 |
| 活跃 IRQ | delta > 1000/窗口 | 参与不均衡计算的 IRQ |
| 中断不均衡 | max/min delta ratio | > 3x → RSS 热点（playbook）；> 50 → RSS 塌缩 |
| 网络核饱和 | idle < 1% 且 soft+irq > 30% | 该 CPU 饱和 |
| IRQ-NUMA 对齐 | effective_affinity 必须落在网卡 numa_node 的 CPU 上 | 不对齐即问题 |

`--sock` 聚合输出中 `retrans_segments` 为各 socket `retrans:X/Y` 的 Y（`tcpi_total_retrans`）之和，即**自连接建立以来的累计重传段**，非窗口增量；需要窗口增量时用 `--retrans` 双采样或对比两次采集。

## 输出示例

--drop：
```
rx_bytes_per_sec=12400000 rx_drop_delta=0 tx_bytes_per_sec=5230000 tx_drop_delta=0
drop_status=none
```

--sock -p 1234：
```
socket_count=8 total_recvq=0 total_sendq=262144 retrans_segments=3
hint=socket_backlog_sampled
```

--irqdelta：
```
irq 281: delta=45231 (active)
irq 282: delta=120
active_irqs=2 max_delta=45231 min_delta=120 max_min_ratio=376.9
```

## 工具分工

| 采集需求 | 工具 |
|---------|------|
| 系统级资源基线（含网络吞吐） | kptools-nmon |
| 网络与中断深度（本 SKILL） | kptools-network-collector |
| 进程/线程级（绑核现状） | kptools-proc-collector |
| 微架构/topdown | kptools-devkit |

## 约束

- **只读**：ethtool -L/-C/-G/-K、写 rps_*/xps_*、改 smp_affinity_list 属修改类动作，由 network-optimization subskill 决策执行
- `--nic` 未指定 `-i` 时仅采集 UP 的物理网卡（有 PCI device 的接口）；veth/bridge 等虚拟接口用 `-i` 显式指定
- `--irqdelta` 输出 `matched_irqs=0` 表示 pattern 在 /proc/interrupts 中无匹配行（pattern 三级回溯后仍无）；`active_irqs=0` 表示有匹配但窗口内无增量
- `--sock` 的 `hint=no_sockets_for_pid` 表示目标 PID 无 TCP socket（纯计算/训练进程属正常，非采集故障）；`hint=socket_backlog_sampled` 表示已聚合 socket 队列
- 容器内 /proc/interrupts 可能不可读、sysfs 不完整——需宿主机视角
- netstat（net-tools）在新系统可能缺失，回退路径已在 collect.sh 内置并 stderr 标注
