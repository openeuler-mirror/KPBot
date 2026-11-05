---
name: kptools-bios-collector
description: >
  采集 BIOS/固件配置与状态：Power Profile、SMT、C-State、Turbo、
  NUMA Interleaving、DDR 内存频率、PCIe ASPM、Hardware Prefetcher、
  BMC Redfish BIOS 属性。当用户需要检查 BIOS 配置、确认高性能模式、
  SMT/C-State/Turbo 状态、内存频率、ASPM、Prefetcher、获取 BIOS
  优化证据、Redfish 采集时触发。
license: GPL-3.0
compatibility: Linux x86_64/aarch64；Redfish 需 BMC 可达；dmidecode/cpupower/lspci -vvv 需 root
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: bios_firmware
  version: "1.0.0"
---

# kptools-bios-collector — BIOS/固件配置采集

## 工作流程

1. 检查工具可用性：`bash scripts/discover.sh`
2. 执行采集：`bash scripts/collect.sh --<mode>`（可多模式组合）
3. Redfish 属性归一化（可选）：`bash scripts/redfish_attrs.sh -f <bios.json>`
4. 返回原始输出（分析+优化建议属 bios-optimization subskill，本 SKILL 只采集）

## 采集命令

```bash
# DMI 信息（BIOS/系统/主板，root）
bash scripts/collect.sh --dmi

# Redfish BIOS 属性（BMC 凭据：参数或 REDFISH_HOST/REDFISH_USER/REDFISH_PASS 环境变量）
bash scripts/collect.sh --redfish --bmc-host <host> -u <user> -p <pass>

# BIOS 属性归一化（三级匹配：精确 → 关键词 → 交由 OS 推断）
bash scripts/redfish_attrs.sh -f bios.json

# SMT / C-State / Turbo / NUMA Interleaving / DDR Speed / PCIe ASPM
bash scripts/collect.sh --smt
bash scripts/collect.sh --cstate
bash scripts/collect.sh --turbo
bash scripts/collect.sh --numa
bash scripts/collect.sh --ddr
bash scripts/collect.sh --aspm

# Hardware Prefetcher（仅 Redfish 可查）
bash scripts/collect.sh --prefetcher --bmc-host <host> -u <user> -p <pass>

# 全部模式顺序执行（无凭据/无 root 的模式记 SKIP 继续）
bash scripts/collect.sh --all
```

## 输出

- stdout: 各模式原始输出（`===` 分节；Redfish 含 /Bios 与 /Bios/Settings JSON 原文）
- stderr: SKIP/降级/错误原因（凭据缺失、BMC 不可达、401、工具缺失）
- 退出码: 0 成功（含 SKIP）；3 参数错误；1 单模式失败

## 参数速查

| 参数 | 说明 |
|------|------|
| `--dmi` | dmidecode bios/system/baseboard + sysfs DMI |
| `--redfish` | BMC Redfish GET 序列 |
| `--bmc-host <host\|URL>` | BMC 地址（支持 http:// https:// 前缀，默认 https://） |
| `-u <user>` / `-p <pass>` | BMC 凭据（也可用 REDFISH_USER/REDFISH_PASS 环境变量） |
| `--smt` | SMT（Redfish → sysfs 推断） |
| `--cstate` | C-State（cpupower idle-info → sysfs 降级） |
| `--turbo` | Turbo（Redfish → lscpu + cpufreq 推断） |
| `--numa` | NUMA Interleaving（numactl -H 推断） |
| `--ddr` | DDR Speed（dmidecode -t memory） |
| `--aspm` | PCIe ASPM（Redfish → lspci -vvv） |
| `--prefetcher` | Hardware Prefetcher（仅 Redfish） |
| `--all` | 全部模式 |

## 约束

- **只读**：仅 Redfish GET；禁止 PATCH/PUT（BIOS 修改属 bios-optimization）
- **凭据安全**：BMC 凭据不写入输出文件、不回显；命令行传参对 ps 可见，敏感环境用环境变量
- root：dmidecode/cpupower/lspci -vvv 需 root；非 root 时相关模式降级或报 unavailable
- 推断标注：OS 层推断值标注 `source=os_inferred`，与 Redfish 实值区分，禁止混同
- VM/容器：dmidecode 在 VM 返回虚拟化 DMI；Redfish 不适用于容器内

## 错误处理

- `BMC 不可达` → 检查网络/BMC 服务（分类报因：连接失败 vs 401）
- `凭据错误（HTTP 401）` → 核对 BMC 账号
- `dmidecode not available or non-root` → root 执行或使用 sysfs DMI 段
- `无 Redfish 凭据` → SKIP 继续其余模式（--all 语义）

## 参考

- [BIOS 采集指南](references/bios-guide.md)
