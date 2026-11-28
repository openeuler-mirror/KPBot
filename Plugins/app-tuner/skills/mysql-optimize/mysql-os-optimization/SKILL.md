---
name: mysql-os-optimization
description: >
  MySQL 场景的 OS/固件相邻优化子技能（鲲鹏/ARM 与 x86 Linux）：关闭 irqbalance、网卡
  buffer/coalesce/队列/hash、从目标网卡 sysfs 枚举并核实 IRQ 后写 smp_affinity_list
  （任何情况下都不停止宿主防火墙）、sysctl 四组基线（net 网络、io 的 vm.dirty*/fs.aio-max-nr、
  memory 的 overcommit/swappiness/min_free、scheduler 的 numa_balancing）、THP 与标准大页
  （裸机 vs 虚机；容器内拒绝）、ulimit nofile、SELinux、调度 steal、内核启动参数、
  nf_conntrack、HDD 调度/NVMe、XFS/ext4、bond 模式；与 BIOS 相邻的
  SMMU/电源/预取/BTB 只给建议并交回 bios-optimization。脚本默认 dry-run，执行必须
  `--execute --approved-change-id`，按 `--category` 一次只改一类并具备独占备份/回滚。
  触发词：MySQL OS 调优、中断绑核、irqbalance、smp_affinity、THP、大页、ulimit、
  SELinux、sysctl、网卡队列、RSS、bond、内核参数、文件系统。
license: MulanPSL-2.0
compatibility: openEuler 22.03/24.03、Kylin/CentOS 系；aarch64/x86_64；root；部分项需重启/BIOS
metadata:
  skill_type: tool_assisted
  category: tuning
  domain: relational_database
  version: "1.0.0"
---

# MySQL 场景 OS 优化

## 简介

MySQL 的 OS 层优化目标是：**业务核尽量用于业务，IRQ/后台线程可控，内存页与文件系统
行为稳定，fd/内核参数不成为隐性上限**。本 skill 只做 MySQL 场景画像与可执行清单；
通用 OS 原理仍以 `kpbot-app-tuner` 的 `os-optimization` 为准，BIOS 项交回
`bios-optimization`。

>
> 所有变更默认 **dry-run**。真正执行需要 `--execute --approved-change-id CHG-2026-001`，
> 并先备份原值；高风险项（内核启动参数、SMMU、队列重建）需维护窗口。

## 何时触发

- 中断集中在少数核、IRQ 与业务抢核、软中断高、网络吞吐上不去。
- THP/大页策略与部署形态不匹配（裸机/虚机/容器），内存回收或延迟抖动。
- `Too many open files`、连接数上不去、SELinux 拦截。
- 需要一套 MySQL 实测过的 sysctl/网卡/文件系统基线。
- 需要判断某个 OS 项该不该改、改了如何回滚。

**不适用**：my.cnf 参数 → `mysql-app-config-optimization`；源码/编译 →
`mysql-source-optimization`；BIOS 具体操作 → `bios-optimization`；通用网络调优 →
`network-optimization`。

## 变量约定

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `${ETH}` | 业务网卡 | `eth0` |
| `${BDF}` | 网卡 PCI 地址 | `0000:xx:00.1` |
| `${NUMA_NODE}` | 网卡 NUMA | 1 |
| `${IRQ_LIST}` | 网卡中断号列表 | 由 `/sys/class/net/${ETH}/device` 的 `msi_irqs/irq` 枚举（`/proc/interrupts` 仅用于采集与验证中断增量） |
| `${IRQ_CORES}` | IRQ 绑定核 | `80-85` 或 `64` |
| `${WORKER_CORES}` | 业务核 | `86-159` |
| `${HUGEPAGE_GB}` | 需要的大页总量 | 按虚机/实例 |
| `${CATEGORY}` | 本次变更类别（net/io/memory/scheduler/sysctl/thp/hugepages/irq/all） | `net,irq` |
| `${BACKUP_FILE}` | 每次运行独占创建的原值备份 | `os-before.XXXXXX` |
| `${CHANGE_ID}` | 变更单号 | `CHG-2026-001` |

## 工作流

```
只读采集 → 判定部署形态/瓶颈 → 生成 dry-run 计划 → 用户确认 → 备份原值
  → --execute --approved-change-id → 复测 → 保留/回滚
```

原则：一次只改一类（IRQ / 内存页 / fd / sysctl / 文件系统），每类单独复测。
脚本用 `--category` 显式选类（net/io/memory/scheduler/sysctl/thp/hugepages/irq/all），
计划、备份与执行都只覆盖所选类别；不指定时按所给参数自动推导。

## 1. 部署形态判定

```
systemd-detect-virt                # none/kvm/docker/...
cat /proc/1/cgroup | head
ls /sys/class/iommu/ 2>/dev/null   # SMMU 是否开启（有内容=开启）
```

| 形态 | THP/大页 | IRQ |
| --- | --- | --- |
| 裸机 | 通常 THP never、标准大页按需 | 宿主直接绑核 |
| 虚机 | 标准大页给 guest；guest 内 THP 按需 | 宿主 IRQ 与虚机同 NUMA；guest 网络 IRQ 绑 0 号核 |
| 容器 | 继承宿主；容器内不要改宿主 THP | 宿主 IRQ 绑到容器 cpuset 外/保留核 |

## 2. IRQ 与网卡

```
# 0) 先核实 BDF 与网卡自身的 IRQ 集合（空值立即退出；不用空 BDF 匹配 /proc/interrupts）
BDF="$(ethtool -i "${ETH}" | awk -F': ' '/^bus-info/{print $2}')"
[ -n "${BDF}" ] || exit 1
ls /sys/class/net/"${ETH}"/device/msi_irqs          # 多队列 IRQ 号
cat /sys/class/net/"${ETH}"/device/irq              # legacy 单 IRQ（部分驱动）
IRQS="$(cat /sys/class/net/"${ETH}"/device/msi_irqs/* 2>/dev/null)"
[ -n "${IRQS}" ] || exit 1

# 1) 队列/buffer/coalesce（改队列可能重建 IRQ）
ethtool -G "${ETH}" rx 2048 tx 2048
ethtool -C "${ETH}" adaptive-rx on
ethtool -C "${ETH}" adaptive-tx on
ethtool -L "${ETH}" combined "${QUEUES}"
lspci -v -s "${BDF}" | grep -i 'NUMA node'

# 2) 队列变更后重新枚举当前 IRQ，集合非空才停 irqbalance
IRQS="$(cat /sys/class/net/"${ETH}"/device/msi_irqs/* 2>/dev/null)"
[ -n "${IRQS}" ] || exit 1
systemctl stop irqbalance
# 不停止宿主防火墙：IRQ 绑核不需要改防火墙规则

# 3) 逐队列写亲和性并回读
echo "${CORE}" > "/proc/irq/${IRQ}/smp_affinity_list"
cat "/proc/irq/${IRQ}/effective_affinity_list"
```

要点：

- 必须从目标网卡的 sysfs 枚举 IRQ；BDF 为空或 IRQ 集合为空立刻退出，禁止用空 BDF 匹配
  `/proc/interrupts` 的其它设备中断，也禁止在确认前停 irqbalance。
- 改队列数会重建 IRQ，必须重读该网卡的 `msi_irqs`，不能沿用旧 IRQ 号。
- 多队列逐队列绑不同核；所有队列同一掩码会让中断集中。
- 每项写入都回读目标值；队列被驱动拒绝或 IRQ 写入失败时逐项记录并返回非零。
- IRQ 核与业务核分离；核少时（2C/4C）可共享，但需 A/B。
- 部分驱动改队列需 down/up，可能瞬断；远程操作走 BMC/串口。
- bond 逻辑网卡本身无中断，需对每张 slave 单独绑核；MySQL 常用 mode 0/4，需交换机配合。

## 3. THP 与标准大页

裸机（无虚机）通常：

```
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo 0 > /proc/sys/vm/nr_hugepages
```

虚机/需要大页时：

```
# 1G 页（示例）；按 NUMA 节点定向分配，页数按该节点内存/页大小计算
# per-node 写入为叠加语义（计入其它节点已有页数），不要再写入同值的全局
# /proc/sys/vm/nr_hugepages=N，否则总量约为 2N；脚本 --hugepages 走全局总量口径。
echo "${HUGEPAGE_COUNT}" > /sys/devices/system/node/node${NUMA_NODE}/hugepages/hugepages-1048576kB/nr_hugepages
grep Huge /proc/meminfo
```

- 64K 页内核（openEuler 22.03 基线）下大页目录可能是 `hugepages-524288kB`。
- MySQL `large_pages=1` 需要足够大页且 buffer pool 匹配；否则启动失败。
- 容器继承宿主大页；容器内改 `nr_hugepages` 可能影响宿主，禁止。脚本在首次写入前
  检查部署形态（cgroup/`/.dockerenv`/`systemd-detect-virt`），容器内拒绝
  `--thp`/`--hugepages`，不只看 UID。

## 4. fd、SELinux、调度

```
ulimit -n 102400
# 永久：/etc/security/limits.conf
#   * soft nofile 102400
#   * hard nofile 102400
# systemd：LimitNOFILE=1000000（与 my.cnf open_files_limit 匹配）

getenforce
# 关闭 SELinux（需确认策略）：/etc/selinux/config -> SELINUX=disabled
```

- `ulimit` 对已运行进程无效；systemd 需改 unit 并重启。
- SELinux 关闭需重启；生产优先用策略而非直接关闭。
- 调度 steal task（`/sys/kernel/debug/sched/features`）可优化抢占，但属实验项，
需 A/B 与回滚。

## 5. 内核启动参数（高风险）

示例（按平台/版本调整，不要照搬）：

```
modprobe.blacklist=hibmc_drm iommu.passthrough=1 selinux=0 apparmor=0 \
nohz=on audit=0 kpti=off arm64.nopauth
```

- `iommu.passthrough=1` 常用于直通；与 SMMU 策略相关，需确认安全影响。
- `kpti=off` 有安全影响；生产慎用。
- 修改 `/boot/efi/EFI/openEuler/grub.cfg` 后需重启；必须保留旧内核启动项。
- nf_conntrack 优化可能需补丁内核；不要在生产随意替换内核。

## 6. sysctl 基线（参考）

四组用 `--category net/io/memory/scheduler` 分别选择（`sysctl` 为四组合集别名）；
下表为参考基线，需按 OS/版本/负载实测调整，脚本逐项写入并回读目标值。

网络组（`net`）：

| 键 | 值 | 目的 |
| --- | --- | --- |
| `net.ipv4.tcp_max_syn_backlog` | 16384 | SYN 队列 |
| `net.core.somaxconn` | 8192 | accept 队列 |
| `net.ipv4.tcp_timestamps` | 1 | 时间戳（按文档；部分场景建议 0，仅 write 画像写入） |
| `net.ipv4.tcp_sack` | 1 | 选择性确认 |
| `net.ipv4.tcp_tw_reuse` | 1 | 复用 TIME_WAIT |
| `net.ipv4.tcp_adv_win_scale` | 1 | 窗口/缓冲分配 |
| `net.core.netdev_max_backlog` | 250000 | 输入队列 |

表中键即脚本网络组写入集合：前 6 键在 read/write 画像均写入，`tcp_timestamps`
仅 write 画像写入并纳入备份/回读/回滚；收发缓冲、TIME_WAIT 上限等其它参考项不在
脚本写入集合内，需另行评估。`tcp_low_latency` 在目标内核为 obsolete 项，已移除。

IO 组（`io`）：影响刷脏节奏与异步 IO 上限。

| 键 | 值 | 目的 |
| --- | --- | --- |
| `fs.aio-max-nr` | 1048576 | 异步 IO 上下文上限 |
| `vm.dirty_background_ratio` | 5 | 后台回写起点，抑制集中刷脏抖动 |
| `vm.dirty_ratio` | 20 | 前台回写阈值，避免写路径长尾 |

内存组（`memory`）：

| 键 | 值 | 目的 |
| --- | --- | --- |
| `vm.overcommit_memory` | 0 | 启发式 overcommit，按实例内存实测 |
| `vm.swappiness` | 1 | 尽量不换出匿名页（大 buffer pool） |
| `vm.min_free_kbytes` | 262144 | 内存保留水位，按物理内存调整 |

调度组（`scheduler`）：

| 键 | 值 | 目的 |
| --- | --- | --- |
| `kernel.numa_balancing` | 0 | 显式绑 NUMA 时关闭自动迁移，按实测 A/B |

注意：不同文档对 `tcp_timestamps` 取值不同（0/1），必须按目标 OS/网络实测；
不要一次性写全部 sysctl，分组 A/B 并记录原值。不支持的键会被逐项记为失败并返回非零，
不静默跳过。永久化写 `/etc/sysctl.d/99-mysql.conf` 并 `sysctl --system`，先 `sysctl -w` 试点。

## 7. 磁盘与文件系统

- MySQL 数据盘优先 NVMe；HDD 才调 `scheduler=deadline`、`nr_requests=2048`。
- 文件系统 XFS 或 ext4；原子写场景按厂商要求（如 ext4 bigalloc 16K）。
- 挂载选项按数据安全与性能选择（`noatime` 可减少元数据写，需评估）。
- 不要在网络盘/RAID 写缓存无电池保护的盘上跑 durable 写。

## 8. 安全与回滚

- 默认 dry-run；`--execute` 必须同时给 `--approved-change-id`。`--category` 决定
  计划/备份/执行覆盖的类别，避免只想改 THP 却写了网络 sysctl。
- 备份按每次运行独占创建 `${BACKUP_DIR}/os-before.XXXXXX`（mktemp）；同一 `--outdir`
  重跑不会截断首次备份。原值未完整保存前不做任何系统变更。
- before 备份含各 sysctl 原值、THP/`nr_hugepages`、irqbalance 状态、队列数，以及按设备/
  队列保存的 `smp_affinity_list`；执行后写 after 文件。
- 逐项回读目标值：部分成功返回非零，并列出已生效项与待恢复项；失败项不静默吞掉。
- 回滚顺序：恢复队列数 → 重新定位 IRQ → 恢复原 `smp_affinity_list` → 恢复 irqbalance
  状态 → 恢复 sysctl → THP/大页 → ulimit/SELinux → 内核参数（需重启）→ 文件系统/调度。
  可执行 `bash scripts/optimize_os.sh --rollback ${BACKUP_FILE}`，按此顺序自动恢复。
- 不删除未知 ntuple/IRQ 规则；只回滚本次记录项。
- 生产变更需窗口、备份、回滚演练；报告脱敏。

## 参考资料与脚本

| 文件 | 何时读 |
| --- | --- |
| `references/os-tuning.md` | 各项原理、命令、取值、部署形态差异、故障与回滚 |
| `scripts/optimize_os.sh` | dry-run 计划；`--category` 选类，`--execute --approved-change-id` 执行并独占备份，`--rollback` 回滚 |
