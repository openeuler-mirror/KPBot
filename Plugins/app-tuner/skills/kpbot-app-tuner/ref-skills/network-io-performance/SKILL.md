---
name: network-io-performance
description: 检测和分析网络IO性能，包括TCP/UDP流量、网络报文收发、网络中断和丢包检测。当用户提到网络性能检查、网络IO分析、TCP/UDP流量监控、网络丢包检测、中断负载分析或查看网络接口统计信息时触发此技能。当用户提到网络瓶颈、网络吞吐量或网络接口诊断时也触发。
---

# 网络IO性能检测和分析

此技能通过分析网络接口、中断、丢包和流量分布来诊断网络性能问题。

## 何时使用此技能

在以下情况使用此技能：
- 用户想要检查网络性能或网络IO
- 用户提到TCP/UDP流量分析
- 用户需要检测网络丢包
- 用户想要分析网络中断负载
- 用户询问网络接口统计信息
- 用户报告网络瓶颈或吞吐量问题
- 用户想要检查网络队列平衡

## 概述

网络性能问题可能源于：
- **中断不均衡**：网络中断集中在少数核心上
- **丢包**：网络接口丢弃数据包
- **队列不均衡**：TX/RX队列分布不均匀
- **高中断负载**：单个中断消耗过多CPU

此技能提供全面分析，包括：
1. **环境分析**：识别活跃网络接口及其中断号
2. **中断负载分析**：检查中断分布并识别热点
3. **丢包检测**：检查接口上的丢包情况
4. **队列平衡分析**：验证TX/RX队列分布

## 所需工具

你需要：
- `bash` 工具用于执行命令
- `write` 工具用于创建报告
- `read` 工具用于检查系统文件

## 脚本总览

本技能提供以下脚本，按顺序或单独执行：

| 脚本 | 用途 |
|------|------|
| `scripts/01_network_interfaces.sh` | 发现活跃网络接口 |
| `scripts/02_irq_info.sh` | 收集活跃接口的中断号与 CPU 亲和性 |
| `scripts/03_interrupt_load.sh` | 分析中断负载分布，识别高负载中断 |
| `scripts/04_packet_loss.sh` | 检查各网络接口丢包与错误统计 |
| `scripts/network_io_check.sh` | 综合入口，依次调用上述 4 个脚本并汇总报告 |

## 分步工作流程

### 一键执行（推荐）

使用综合入口脚本一次性完成所有分析：

```bash
bash scripts/network_io_check.sh
```

### 分步执行

也可按需单独运行各步骤脚本。

#### 步骤 1：网络接口发现

识别所有处于 link up 状态并正在处理流量的网络接口。

```bash
bash scripts/01_network_interfaces.sh
```

输出包含活跃接口名称列表。

#### 步骤 2：中断信息收集

对每个活跃接口，收集中断号、NUMA 节点和 CPU 亲和性。

```bash
bash scripts/02_irq_info.sh
```

依赖步骤 1 的输出。

#### 步骤 3：中断负载分析

分析中断负载分布，识别负载 >10% 的中断。

```bash
bash scripts/03_interrupt_load.sh
```

依赖 `irqtop`，不可用时回退到 `/proc/interrupts` 手动分析。

#### 步骤 4：丢包检测

检查所有网络接口的丢包、错误和冲突统计。

```bash
bash scripts/04_packet_loss.sh
```

#### 步骤 5：队列平衡与流量速率分析

使用 `ethtool -S` 检查 TX/RX 队列分布，并通过 `/sys/class/net/*/statistics/` 计算实时流量速率。这部分逻辑由综合脚本 `network_io_check.sh` 在步骤 4 之后自动执行。

若需手动采集：

```bash
# 队列统计
ethtool -S <iface> 2>/dev/null | grep -E "rx-|tx-"

# 流量速率（1 秒采样）
iface=<iface>
rx1=$(cat /sys/class/net/$iface/statistics/rx_packets)
tx1=$(cat /sys/class/net/$iface/statistics/tx_packets)
sleep 1
rx2=$(cat /sys/class/net/$iface/statistics/rx_packets)
tx2=$(cat /sys/class/net/$iface/statistics/tx_packets)
echo "RX: $((rx2 - rx1)) pkt/s  TX: $((tx2 - tx1)) pkt/s"
```

### 报告生成

综合脚本自动输出 markdown 格式报告。报告结构：

- 执行摘要
- 活跃网络接口
- 中断分析（中断号、核心绑定、负载分布、不均衡检测）
- 丢包分析（丢包状态、错误率、丢包率）
- 队列平衡分析（TX/RX 队列分布、平衡评估）
- 流量速率分析（每秒报文数、Mbps 估算）
- 建议
- 持续监控命令

## 错误处理

优雅地处理这些常见错误：

1. **命令未找到**：注意哪些工具缺失（irqtop、ethtool、netstat）
2. **权限被拒绝**：注意哪些步骤需要root权限
3. **无活跃接口**：如果没有接口正在处理流量则报告
4. **设备文件不可访问**：使用备用方法优雅处理

## 验证

分析完成后，提供持续监控的命令：

```bash
# 实时监控中断负载
watch -n 1 'cat /proc/interrupts | grep -E "eth|ens|eno"'

# 监控网络接口统计
watch -n 1 'netstat -i'

# 监控流量速率
sar -n DEV 1 5

# 监控特定接口队列平衡
watch -n 1 'ethtool -S <interface>'
```

## 重要说明

- 某些命令（irqtop、ethtool）可能需要root权限
- 分析提供快照；生产环境建议持续监控
- 丢包可能是瞬态的；运行多次以获得准确评估
- 队列平衡取决于网卡硬件能力
- 单个核心上的高中断负载（>10%）可能表明需要中断重平衡

## 常见问题及解决方案

### 单个核心上的高中断负载
**症状**：单个中断消耗 >10% CPU
**解决方案**：使用 `irqbalance` 服务或手动将中断分散到多个核心

### 检测到丢包
**症状**：非零错误/丢包计数器
**解决方案**：检查：
- 接口过载（升级带宽）
- 驱动问题（更新驱动）
- 硬件问题（更换网卡）
- 缓冲区溢出（增大环形缓冲区大小）

### 队列不均衡
**症状**：TX/RX队列分布不均匀
**解决方案**：配置RSS/RPS/XPS设置以分散负载

### 高负载但低流量
**症状**：高CPU但低报文速率
**解决方案**：检查：
- 中断风暴
- 驱动bug
- 恶意流量（DDoS）
