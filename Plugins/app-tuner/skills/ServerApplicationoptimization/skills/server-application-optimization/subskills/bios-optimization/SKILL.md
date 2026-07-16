---
name: bios-optimization
description: 根据应用瓶颈、CPU 拓扑、功耗策略、SMT、C-State、NUMA 和内存通道证据输出 BIOS 优化建议，作为服务器应用优化 Agent 候选优化 skill 列表中的 BIOS 优化 skill 使用；仅在证据指向 BIOS 配置可能限制性能且用户允许对应变更时执行。
---

# BIOS Optimization

使用本 skill 处理架构图中的“BIOS 优化 skill”。

## Inputs

- `bottleneck_classification`
- `environment_backup_dir`
- `evidence_snapshot_dir`
- BIOS 或 BMC/Redfish 证据
- CPU、NUMA、内存拓扑
- `agent_action_mode`
- `system_reboot_allowed`

## Workflow

1. 确认当前瓶颈是否可能由 BIOS 配置解释。
2. 检查 Power Profile、SMT、C-State、Turbo、NUMA/Node Interleaving、内存速率、PCIe ASPM。
3. 若缺少 BMC/BIOS 证据，输出 `status=blocked` 或 `degraded`，列出最小补充证据。
4. 输出候选动作，但不得在分析阶段修改 BIOS。
5. 将需系统重启或硬件窗口的动作标记为 `change_mode=system_reboot`、`risk=high`。

## Evidence Collection

优先使用 BMC Redfish 只读接口采集 BIOS 配置；不可用时降级到 OS 侧 DMI/sysfs 证据和人工采集清单。

### Redfish Key Fields

| Redfish 属性关键词 | 含义 | BIOS 菜单方向 |
|---|---|---|
| `WorkloadProfile` / `SystemProfile` | 电源/性能策略 | Power Profile / System Profile |
| `ProcHyperthreading` / `SMT` | 超线程 | Processor SMT / Hyper-Threading |
| `ProcTurbo` / `TurboBoost` | Turbo | Processor Turbo Mode |
| `EnergyPerfBias` | 能耗性能偏向 | Energy Performance Bias |
| `PcieAspmSupport` | PCIe ASPM | PCIe ASPM |
| `NumaGroupSizeOpt` / `NodeInterleaving` | NUMA 暴露方式 | NUMA / Node Interleaving |
| `MemoryInterleaving` / `DDRSpeed` | 内存交错和速率 | Memory Configuration |

Redfish 属性名因厂商而异。无法匹配固定字段时，从原始 JSON 中按关键词归并，不得伪造 BIOS 状态。

### Fallback Evidence

- `dmidecode -t bios` 或 `/sys/class/dmi/id/bios_*`
- `lscpu`
- `numactl --hardware`
- `/sys/devices/system/cpu/smt/{active,control}`
- `cpupower idle-info` 或 `/sys/devices/system/cpu/cpuidle/`
- BMC/iBMC/BIOS Setup 页面截图或人工摘录

当只能从 OS 侧观察 BIOS 状态时，输出 `bios_readonly_mode=true`，对应候选动作默认 `analysis_only` 或 `system_reboot` 人工建议。

## Decision Matrix

| BIOS Setting | Database OLTP | Compute | RPC/Latency-sensitive | Batch/Throughput |
|---|---|---|---|---|
| Power Profile | Performance | Performance | Performance | Performance / Custom |
| SMT / Hyper-Threading | Off 或按实测 | On | Off 或按实测 | On |
| NUMA / Node Interleaving | Off，暴露 NUMA | Off，暴露 NUMA | Off，暴露 NUMA | Off，暴露 NUMA |
| C-State Limit | C1/C0，避免唤醒延迟 | C6 可接受 | C1/C0 | OS controlled |
| Hardware Prefetcher | On | On | On | On |
| Energy Performance Bias | Max Performance | Balanced Performance | Max Performance | Balanced Performance |
| Turbo Boost | On | On | On | On |
| DDR Speed | Max supported | Max supported | Max supported | Max supported |

## Risk And Validation

| BIOS Setting | 生效方式 | 风险 | 回退 |
|---|---|---|---|
| Power Profile | 可能即时或下次重启 | Low | 恢复原 profile |
| SMT | 冷重启 | Medium | 再次重启恢复 |
| NUMA / Node Interleaving | 冷重启 | Medium | 再次重启恢复 |
| C-State Limit | 可能即时或下次重启 | Low | 恢复原 C-State |
| DDR Speed | 冷重启 | High | 恢复原内存速率 |

验证项：

- 重启后确认目标服务恢复、目标实例身份不变。
- 用 `numactl --hardware` 对比 NUMA 拓扑。
- 用 `cat /sys/devices/system/cpu/smt/active` 确认 SMT。
- 用 `cpupower idle-info` 或 cpuidle sysfs 确认 C-State。
- 跑短 warmup，确认无明显回退后再进入正式压测。

## Platform Notes

- Kunpeng/TaiShan：关注 SMT、NUMA 距离、Power Policy=Performance、Memory Refresh/DDR 速率。
- Intel Xeon：关注 SpeedStep/Speed Shift、C-State、System Profile。
- AMD EPYC：关注 CPPC、NUMA/NPS、C-State 和内存通道配置。

## Outputs

- `bios_findings`
- `candidate_actions`
- `required_evidence`
- `rollback`
- `validation_plan`
- `risk_notes`

## Boundary

OS governor、THP、HugePages、sysctl、irqbalance、ulimit、I/O scheduler 由 `os-optimization` 负责。本 skill 只输出 BIOS/BMC/固件层候选动作。
