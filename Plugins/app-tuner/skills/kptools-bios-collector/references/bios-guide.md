# kptools-bios-collector 使用指南

## 1. 定位

BIOS/固件级只读采集。采集与分析分离（STANDARDS.md 设计原则）：本 SKILL 提供 BIOS 配置证据，分析+优化动作属 bios-optimization subskill。

## 2. 采集层级与来源标注

每个属性按三级来源输出，`source=` 标注区分可信度（信息来源规范：禁止把推断值伪装成 BIOS 实值）：

| source | 含义 |
|--------|------|
| `redfish` | BMC Redfish 实读 BIOS 属性（实值，high/medium 置信） |
| `os_inferred` | OS 层推断（sysfs/lscpu/numactl/dmidecode），为间接证据 |
| `unavailable` | 无 Redfish 且 OS 层不可推断 |

## 3. 模式与推断规则（与 app-tuner collect_bios_evidence.sh 一致）

| 模式 | Redfish 属性 | OS 推断 | 推断规则 |
|------|-------------|---------|---------|
| power_profile | WorkloadProfile/SystemProfile/PowerProfile | — | 仅 Redfish |
| smt | ProcHyperthreading/LogicalProc | `/sys/devices/system/cpu/smt/active` | 直接读 active 值 |
| cstate | ProcessorCstate 等 | cpupower idle-info + cpuidle states | 无 cpupower/非 root 时仅 sysfs |
| turbo | ProcTurbo 等 | lscpu + cpufreq | max_freq > min_freq → inferred_on |
| numa_interleaving | NodeInterleaving 等 | numactl -H | 节点数 ≤1 → on；>1 → off |
| ddr_speed | DDRSpeed 等 | dmidecode -t memory | Speed/Type/Size/Locator |
| hardware_prefetcher | HWPrefetcher 等 | — | 仅 Redfish |
| pcie_aspm | PcieAspmSupport 等 | lspci -vvv（LnkCtl/ASPM） | 需 root |

## 4. Redfish 属性归一化（redfish_attrs.sh）

三级匹配：
1. **精确**：属性名全等（confidence=high）
2. **关键词**：属性名含关键词；唯一候选（high）/多候选（ambiguous, medium，附候选列表）
3. **未匹配**：不出现在结果中 → 由 OS 推断模式补充

输出 JSON 结构：
```json
{"smt": {"value": "Enabled", "matched_property_name": "ProcHyperthreading",
          "match_method": "exact", "source": "redfish", "confidence": "high"}}
```

## 5. BIOS 术语与调优信号（供下游解读）

| 属性 | 建议值（性能场景） | 关联优化 |
|------|------------------|---------|
| power_profile | HighPerformance/Performance | bios-optimization |
| smt | 视应用（计算密集关闭、吞吐开启） | bios-optimization + cpu-affinity |
| cstate | Disable（低时延）/ Enable（省电） | bios-optimization |
| turbo | Enabled | bios-optimization |
| numa_interleaving | Disabled（NUMA 绑核场景） | cpu-affinity-optimization |
| ddr_speed | 标称频率（降频预示故障/节能） | 环境诊断 |
| pcie_aspm | Disabled（低时延网卡场景） | network-optimization |
| hardware_prefetcher | Enabled（默认，按负载） | bios-optimization |

## 6. 约束

- 只读：本 SKILL 永远不修改 BIOS（Redfish PATCH 属优化动作，在 bios-optimization 且需显式授权）
- 凭据：参数传传参对 ps 可见；建议 REDFISH_* 环境变量；任何情况下凭据不写入输出
- 鲲鹏整机：Redfish 属性名随 BIOS 供应商差异大，关键词匹配覆盖主流命名；未命中时输出原文 JSON 供人工判读
