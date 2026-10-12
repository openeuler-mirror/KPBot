---
name: kptools-network-collector
description: >
  采集网络与中断性能数据：网络带宽、丢包、TCP 重传、socket 队列（Recv-Q/Send-Q）、
  网卡队列数/中断聚合/ring buffer/offload、RSS/RPS/XPS 多队列配置、IRQ 亲和性、
  中断分布与不均衡度、网卡 NUMA 拓扑、softirq 分布。当用户需要排查网络丢包、
  重传率、socket 堆积、网卡多队列配置、中断绑核是否合理、网卡在哪个 NUMA、
  网络吞吐采集时触发。
license: GPL-3.0
compatibility: Linux
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: network
  version: "1.0.0"
---

# kptools-network-collector — 网络与中断数据采集

## 工作流程

1. 检查工具可用性：`bash scripts/discover.sh`
2. 工具缺失时：`bash scripts/deploy.sh`（root 自动通过包管理器安装并复验；非 root 打印安装命令，需用户确认后安装），安装成功后重试采集一次；仍失败则向用户说明缺失影响并给出安装命令，不要静默跳过
3. 执行采集：`bash scripts/collect.sh --<mode> [-i <IFACE>] [-d <sec>]`
4. 返回标准化输出（原始数据，由 agent/subskill 按 guide 阈值解读）

## 采集命令

```bash
# 接口发现（UP 接口 + carrier + NUMA）
bash scripts/collect.sh --iface

# 带宽（sar -n DEV，时长 -d 秒，默认 5）
bash scripts/collect.sh --bandwidth -d 5

# 丢包/错误（/proc/net/dev 双采样 delta + netstat -i）
bash scripts/collect.sh --drop -d 5

# TCP 重传（netstat -s，缺失回退 nstat）
bash scripts/collect.sh --retrans

# socket 队列（ss -tinp；-p 过滤并聚合目标进程）
bash scripts/collect.sh --sock -p <PID>

# 网卡参数（ethtool -l/-c/-g/-k/-S/-i；-i 指定接口；默认仅采集有 PCI 设备的
# UP 物理网卡——veth/bridge 等虚拟接口及容器内无 PCI 设备的接口自动跳过，
# 容器宿主机上数百 veth × 6 次 ethtool 不可控；被跳过时 stderr 会列出接口名）
bash scripts/collect.sh --nic -i <IFACE>

# RSS/RPS/XPS 配置 / IRQ 亲和性 / 中断 delta / 网卡 NUMA / softirq
bash scripts/collect.sh --rss -i <IFACE>
bash scripts/collect.sh --irq -i <IFACE>
bash scripts/collect.sh --irqdelta -i <IFACE> -d 5
bash scripts/collect.sh --numa
bash scripts/collect.sh --softirq

# 全部模式顺序执行
bash scripts/collect.sh --all
```

## 输出

- stdout: 原始采集数据（工具原生格式；--drop/--irqdelta 输出 delta 汇总行）
- stderr: 诊断信息（数据来源标注、依赖缺失、错误）

## 参数速查

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-i <IFACE>` | 目标接口（不指定则全部 UP 接口） | 全部 |
| `-p <PID>` | --sock 按进程过滤 | - |
| `-d <sec>` | 计时模式时长（--bandwidth/--drop/--irqdelta） | 5 |
| `--all` | 全部模式顺序执行 | - |
| `--help` | 显示帮助 | - |

## 不要做的事

- **纯只读采集**：禁止 ethtool -L/-C/-G/-K 修改队列/coalesce/ring/offload，禁止写 rps_cpus/xps_cpus，禁止改 IRQ 绑定——调整属 network-optimization subskill
- 不要解析/格式转换工具输出 — 原样返回

## 约束

- netstat 缺失时 --retrans 回退 nstat、--drop 回退 ip -s link（stderr 标注）
- --irqdelta 的窗口聚合依赖 python3，缺失时该模式报错
- /proc/interrupts 不可读（容器内常见）时 --irq/--irqdelta 报错
- irqdelta 的活跃判定与不均衡 ratio 阈值见 references/network-guide.md

## 错误处理

- `interface xxx does not exist` → `ip link show` 确认接口名
- `sar not found (install: sysstat)` → 安装 sysstat
- `/proc/interrupts not readable` → 容器内需宿主机视角采集
- ethtool 某项 failed (unsupported) → 该接口不支持对应配置，属正常，看其余段

## 参考

- [网络采集指南](references/network-guide.md)
