---
name: os-optimization
description: 根据应用瓶颈、CPU/内存/网络/磁盘证据输出操作系统层优化建议，覆盖 governor、THP、HugePages、numa_balancing、irqbalance、sysctl、ulimit、I/O scheduler 和容器边界，作为服务器应用优化 Agent 候选优化 skill 列表中的 OS 优化 skill 使用。
---

# OS Optimization

使用本 skill 处理架构图中的“OS 优化 skill”。

## Inputs

- `bottleneck_classification`
- `environment_backup_dir`
- `evidence_snapshot_dir`
- Kernel、sysctl、ulimit、THP、HugePages、governor、irqbalance 证据
- 容器/虚拟化边界
- `agent_action_mode`
- `restart_allowed`

## Workflow

1. 判断 OS 配置是否能解释当前瓶颈或次级瓶颈。
2. 检查 governor、THP、HugePages、numa_balancing、irqbalance、sysctl、ulimit、I/O scheduler。
3. 容器环境下必须区分容器内可变更项和宿主机项。
4. 分别输出在线动作、需服务重启动作、需系统重启动作。
5. `agent_action_mode != approved_execute` 时只输出 dry-run 命令和人工确认项。

## Evidence Collection

从 `evidence_snapshot_dir` 读取或要求补采：

- `sysctl -a` 中 `vm.*`、`kernel.numa_balancing`、`net.core.*`
- `/sys/kernel/mm/transparent_hugepage/{enabled,defrag}`
- `/proc/meminfo`、HugePages 相关字段
- `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`
- `systemctl status irqbalance` 或等价输出
- `/proc/<pid>/limits`、systemd unit 中的 `LimitNOFILE` / `LimitNPROC`
- `/sys/block/*/queue/scheduler`
- 容器 cpuset、cgroup memory、root 权限和宿主机可见性

缺失证据时输出 `required_evidence`，不得直接给出高置信在线变更。

## Decision Matrix

| OS Setting | Database OLTP | Compute | RPC | Batch |
|---|---|---|---|---|
| `vm.swappiness` | 1 | 10 | 1 | 10 |
| `vm.dirty_ratio` | 5 | 20 | 5 | 20 |
| `vm.dirty_background_ratio` | 3 | 10 | 3 | 10 |
| `kernel.numa_balancing` | 绑核后 0 | 一般场景 1 | 0 | 1 |
| THP | never 或 madvise | madvise | never | madvise |
| HugePages | buffer pool >16GB 可建议 | test first | no | test first |
| `net.core.somaxconn` | 65535 | 默认或按连接数 | 65535 | 默认 |
| `nofile` / `nproc` | 65535+ | 默认或按进程数 | 65535+ | 默认 |
| irqbalance | 手动 IRQ 亲和后 off | on | off | on |
| CPU governor | performance | performance | performance | performance |
| I/O scheduler NVMe | none | mq-deadline 或 none | none | mq-deadline |

## Common Actions

在线动作示例：

```bash
sysctl -w vm.swappiness=1
sysctl -w vm.dirty_ratio=5
sysctl -w vm.dirty_background_ratio=3
sysctl -w kernel.numa_balancing=0
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance > "$cpu" 2>/dev/null || true
done
systemctl stop irqbalance 2>/dev/null || service irqbalance stop 2>/dev/null || true
```

需服务重启动作：

- HugePages：修改 `vm.nr_hugepages` 后重启目标服务。
- ulimit：修改 systemd unit 或 limits 配置后 `systemctl daemon-reload` 并重启服务。

## THP And HugePages Rules

| 条件 | THP 推荐 | HugePages 推荐 |
|---|---|---|
| MySQL/PostgreSQL buffer pool > 16GB | never 或 madvise | yes |
| MySQL/PostgreSQL buffer pool < 8GB | never | no |
| Redis/Memcached 大内存实例 | always 或 madvise | yes |
| 纯计算/ML 推理 | madvise | test first |
| RPC/微服务小内存 | never | no |

## Change Classification

| change_mode | 示例 | 风险 |
|---|---|---|
| `online` | sysctl、governor、THP、I/O scheduler | Low |
| `restart_required` | HugePages、ulimit、部分 irqbalance 持久化 | Medium |
| `system_reboot` | OS 内核参数需重启才生效的场景 | High |
| `analysis_only` | 容器/权限不足或用户未批准 | Low |

当 `change_scope` 限制了动作时，将该动作保留在 findings 中，但不要进入可执行 `candidate_actions`。

## Interaction Notes

- `cpu-affinity-optimization` 已经手动管理 IRQ 时，通常需要关闭 irqbalance，避免覆盖手工 IRQ 亲和性。
- 绑核后建议关闭 `kernel.numa_balancing`，避免自动迁移破坏 CPU/NUMA 约束。
- 网络 sysctl 由 `network-optimization` 主责；本 skill 只在 OS 全局配置中记录依赖关系。
- 容器环境下不得承诺宿主机 OS 参数已经修改成功。

## Outputs

- `os_findings`
- `online_change_actions`
- `restart_required_actions`
- `system_reboot_actions`
- `candidate_actions`
- `rollback`
- `validation_plan`
- `container_boundary_notes`

## Boundary

BIOS Power Profile、SMT、C-State、NUMA/Node Interleaving、DDR Speed 等固件/BMC 配置由 `bios-optimization` 负责。
