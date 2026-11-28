# OS 调优细节

## 1. 只读采集

```
uname -a
lscpu -p=CPU,NODE | grep -v '^#'
cat /proc/meminfo | grep -E 'MemTotal|Huge'
cat /sys/kernel/mm/transparent_hugepage/enabled
cat /proc/sys/vm/nr_hugepages
ulimit -a
getenforce 2>/dev/null
systemctl is-active irqbalance 2>/dev/null
ethtool -i "${ETH}"
ethtool -l "${ETH}"
ethtool -g "${ETH}"
ethtool -c "${ETH}"
cat /proc/interrupts
cat /proc/net/bonding/* 2>/dev/null
```

记录：架构、NUMA、内存/大页、THP、fd、SELinux、irqbalance、网卡队列/buffer/coalesce、
IRQ 分布、bond、文件系统、调度器。

## 2. IRQ 绑定完整流程

```
# 0) 先核实 BDF 与 IRQ 集合：空 BDF 不得用于匹配 /proc/interrupts
BDF="$(ethtool -i "${ETH}" | awk -F': ' '/^bus-info/{print $2}')"
[ -n "${BDF}" ] || exit 1
IRQS="$(cat /sys/class/net/"${ETH}"/device/msi_irqs/* 2>/dev/null)"
[ -n "${IRQS}" ] || exit 1

# 1) 队列数与 buffer/coalesce（改队列会重建 IRQ）
ethtool -L "${ETH}" combined "${QUEUES}"
ethtool -G "${ETH}" rx 2048 tx 2048
ethtool -C "${ETH}" adaptive-rx on
ethtool -C "${ETH}" adaptive-tx on
lspci -v -s "${BDF}" | grep -i numa

# 2) 重新枚举当前 IRQ，确认非空后才停自动平衡（不停止宿主防火墙）
IRQS="$(cat /sys/class/net/"${ETH}"/device/msi_irqs/* 2>/dev/null)"
[ -n "${IRQS}" ] || exit 1
systemctl stop irqbalance

# 3) 逐队列绑核并回读
echo "${CORE_i}" > /proc/irq/"${IRQ_i}"/smp_affinity_list
cat /proc/irq/"${IRQ_i}"/effective_affinity_list
```

校验：

- 改队列后 IRQ 号会变，必须从该网卡 sysfs 重读；空 BDF/空 IRQ 集合直接退出，
  不得用空 BDF 匹配 `/proc/interrupts` 的其它设备中断。
- 确认 IRQ 集合后才停 irqbalance；停服失败或某 IRQ 写失败要逐项记录并回读，返回非零。
- 中断增量：压测前后 `/proc/interrupts` 差值应落在目标核。
- `effective_affinity_list` 必须与写入一致；不一致可能被 irqbalance/驱动改回。
- 多队列可用 `ethtool -X ... equal N` 调整哈希分布（按网卡支持）。

## 3. THP/大页决策

| 形态 | THP | 标准大页 | 说明 |
| --- | --- | --- | --- |
| 裸机 MySQL | never（常见） | 0 或按需 | 避免 THP 抖动；`large_pages` 可选 |
| 虚机 host | 按需 | 给 guest 分配 | 1G 页优先；guest 内再确认 |
| 虚机 guest | never/按需 | 由 host 提供 | guest 内不要重复分配宿主页 |
| 容器 | 继承 host | 继承 host | 容器内改宿主参数属越权 |

命令：

```
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo 0 > /proc/sys/vm/nr_hugepages
# 或按 node 分配
echo 16 > /sys/devices/system/node/node1/hugepages/hugepages-1048576kB/nr_hugepages
```

- 64K 页内核：目录名可能为 `hugepages-524288kB`。
- 大页不足会导致虚机/MySQL 启动失败；先确认 `HugePages_Free`。
- `large_pages=1` 与 buffer pool 必须匹配；改 buffer pool 后复核。

## 4. ulimit 与 systemd

```
ulimit -n 102400            # 当前 shell
# /etc/security/limits.conf:
# * soft nofile 102400
# * hard nofile 102400
```

systemd：

```
[Service]
LimitNOFILE=1000000
LimitNPROC=65535
```

- `ulimit` 不影响已运行进程；mysqld 需重启或改 unit。
- `open_files_limit` 与 `table_open_cache`、连接数、表数相关。

## 5. SELinux/AppArmor

```
getenforce
sestatus
# /etc/selinux/config: SELINUX=disabled（需重启）
```

- 生产优先用策略放行 datadir/socket/log，而不是全局关闭。
- 关闭后需重启；记录原策略与回滚。

## 6. 内核启动参数

修改前：

- 备份 `/boot/efi/EFI/<os>/grub.cfg` 或 `/etc/default/grub`。
- 保留旧内核启动项；确认可回退。
- 逐项评估安全影响（`kpti`、`iommu.passthrough`、`selinux=0`）。

常见项：

| 参数 | 作用 | 风险 |
| --- | --- | --- |
| `iommu.passthrough=1` | 直通性能 | 安全/隔离 |
| `nohz=on` | 减少 tick | 调度行为变化 |
| `audit=0` | 关闭审计 | 合规 |
| `kpti=off` | 关闭页表隔离 | 安全漏洞 |
| `arm64.nopauth` | 关闭指针认证 | 安全 |
| `modprobe.blacklist=...` | 屏蔽驱动 | 设备不可用 |

## 7. sysctl 分组

按目标分组应用，不要一次全写；脚本用 `--category net/io/memory/scheduler` 选类
（`sysctl` 为四组合集），计划、备份和执行只覆盖所选类别。

1. 网络（`net`，脚本写入集合）：连接队列 `somaxconn`/`tcp_max_syn_backlog`/
   `netdev_max_backlog`；`tcp_sack`/`tcp_tw_reuse`/`tcp_adv_win_scale`；
   `tcp_timestamps=1` 仅 write 画像写入并按 OS/网络实测（部分场景建议 0）。
   收发缓冲 `rmem/wmem_*`、`tcp_rmem/tcp_wmem`、`optmem_max`、`tcp_max_tw_buckets`
   与已 obsolete 的 `tcp_low_latency` 均不在脚本写入集合内，需另行评估。
2. IO（`io`）：`fs.aio-max-nr=1048576`、`vm.dirty_background_ratio=5`、
   `vm.dirty_ratio=20` —— 异步 IO 上限与后台/前台刷脏起点，影响写路径抖动。
3. 内存（`memory`）：`vm.overcommit_memory=0`、`vm.swappiness=1`、
   `vm.min_free_kbytes=262144` —— 大 buffer pool 的过提交、换出倾向与回收水位。
4. 调度（`scheduler`）：`kernel.numa_balancing=0` —— 显式绑 NUMA 时避免自动迁移，
   是否需要开启按实测 A/B。

写入：

```
sysctl -w net.core.somaxconn=8192
sysctl -w vm.dirty_background_ratio=5
sysctl -w vm.swappiness=1
# 永久：/etc/sysctl.d/99-mysql.conf
sysctl --system
```

记录原值；不支持的键记为失败并回读目标值，部分成功返回非零，回滚用原值文件。

## 8. 磁盘/文件系统

```
df -Th
lsblk
cat /sys/block/<dev>/queue/scheduler
cat /sys/block/<dev>/queue/nr_requests
```

- HDD：`deadline`/`mq-deadline`、`nr_requests=2048`。
- NVMe：通常不改调度；确认 `none`/`mq-deadline` 默认。
- XFS/ext4 选型：只适用于**新建空盘**。执行任何 `mkfs.*` 前先用 `lsblk -f`/`blkid`/`findmnt`
  核对设备、文件系统与挂载点，确认未挂载且无数据；承载 MySQL 数据的分区严禁 `mkfs.*`
  （`-f` 会强制覆盖既有文件系统，误用即不可逆数据丢失）。同族 `mysql-vm-deployment`
  的虚机部署细节 §6.2 给出了空盘格式化的守卫模式。本 skill 只读采集数据盘信息，
  不给数据盘格式化命令。
- 原子写：`hioadm atomicwrite`（厂商工具）+ 文件系统/页大小要求。

## 9. bond

- 模式 0（round-robin）/4（802.3ad）常用；需交换机配置。
- bond 是逻辑网卡，无独立 IRQ；对每张 slave 单独绑核。
- 变更会短暂断网；远程操作需 BMC/串口。
- 校验：`cat /proc/net/bonding/bond0`、`ip add`（slave MAC 一致）、`sar -n DEV 1`。

## 10. 回滚清单

- before 备份按每次运行独占创建（`os-before.XXXXXX`），含各 sysctl 原值、THP/
  `nr_hugepages`、irqbalance 状态、队列数，以及按设备和队列保存的 `smp_affinity_list`。
- 顺序：队列数恢复 → 重新定位 IRQ（队列变更会重建 IRQ）→ 恢复原 `smp_affinity_list`
  → 恢复 irqbalance 原状态 → sysctl 恢复 → THP/大页恢复 → ulimit/SELinux 恢复
  （必要时重启）→ 内核参数/启动项恢复（重启）→ 文件系统/调度恢复。
- `bash scripts/optimize_os.sh --rollback "${BACKUP_FILE}"` 按上述顺序执行；容器内跳过
  THP/大页的宿主接口回写。
- buffer/coalesce/hash 恢复按采集记录单独处理。
- 复测网络/MySQL，确认无残留；部分恢复失败时返回非零并报告失败项。
