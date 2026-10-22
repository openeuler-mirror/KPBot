---
name: kptools-os-collector
description: >
  采集 OS/内核级配置与状态：内核版本、GRUB 启动参数、sysctl 内核参数、
  THP/HugePages、ulimit、tuned/A-Tune profile、irqbalance、I/O scheduler、
  numa_balancing、虚拟化/容器检测、cgroup 资源边界、perf/PMU 能力。
  当用户需要检查 OS 配置、内核参数、大页、系统服务、容器资源边界、
  perf 可用性、环境诊断基线时触发。
license: GPL-3.0
compatibility: Linux（openEuler 22.03/24.03 验证基线）；services 模式依赖 systemd
metadata:
  skill_type: tool_assisted
  category: profiling
  domain: os_kernel
  version: "1.0.0"
---

# kptools-os-collector — OS/内核配置采集

## 工作流程

1. 检查只读能力：`bash scripts/discover.sh`
2. 执行采集：`bash scripts/collect.sh --<mode>`（可多模式组合）
3. 返回原始输出（分析+优化建议属 os-optimization subskill，本 SKILL 只采集）

## 采集命令

```bash
# 内核/OS 版本 + GRUB 启动参数
bash scripts/collect.sh --kernel

# sysctl 全量 + 重点参数白名单
bash scripts/collect.sh --sysctl

# THP / HugePages / numa_balancing / ulimit
bash scripts/collect.sh --thp
bash scripts/collect.sh --hugepages
bash scripts/collect.sh --numa-balancing
bash scripts/collect.sh --ulimit

# tuned / A-Tune / irqbalance 服务状态
bash scripts/collect.sh --services

# per-device I/O scheduler（nvme/sd/vd/xvd）
bash scripts/collect.sh --iosched

# 虚拟化/容器检测 + cgroup 资源边界
bash scripts/collect.sh --virt
bash scripts/collect.sh --cgroup

# perf/PMU 能力（perf_event_paranoid + 事件枚举）
bash scripts/collect.sh --perf

# 全部模式顺序执行
bash scripts/collect.sh --all
```

## 输出

- stdout: 各模式原始输出（`===` 分节）
- stderr: 权限/服务缺失诊断（SKIP、OPTIONAL_ABSENT）
- 退出码: 0 成功；3 参数错误

## 参数速查

| 参数 | 说明 | 依赖 |
|------|------|------|
| `--kernel` | uname/os-release/cmdline | 无 |
| `--sysctl` | sysctl -a + 白名单 | sysctl |
| `--thp` | THP enabled/defrag | 无 |
| `--hugepages` | /proc/meminfo Huge 段 | 无 |
| `--ulimit` | ulimit -a（shell 视角） | 无 |
| `--services` | tuned/A-Tune/irqbalance | systemd（可选） |
| `--iosched` | per-device scheduler | 无 |
| `--numa-balancing` | /proc/sys/kernel/numa_balancing | 无 |
| `--virt` | systemd-detect-virt 多级推断 | systemd（可选） |
| `--cgroup` | v2 优先 v1 回退 | 无 |
| `--perf` | perf_event_paranoid + perf list | perf（可选） |
| `--all` | 全部模式 | - |

## 约束

- **只读**：无任何写操作（sysctl -w 属 os-optimization）
- 进程级 limits（/proc/PID/limits）与进程 cgroup（/proc/PID/cgroup）归 kptools-proc-collector，本 SKILL 仅系统级
- 容器内采集为容器视角（cgroup/sysctl 为容器边界值）；宿主机视角需在宿主机执行
- 非 root：sysctl -a 部分参数不可读（标注后继续）；--services 无 systemd 时 OPTIONAL_ABSENT

## 错误处理

- `tuned-adm 不存在` → OPTIONAL_ABSENT（正常，非失败）
- `sysctl 部分参数无权限` → 标注后继续
- `未知模式/无模式参数` → 退出码 3 + --help 提示

## 参考

- [OS 采集指南](references/os-guide.md)
