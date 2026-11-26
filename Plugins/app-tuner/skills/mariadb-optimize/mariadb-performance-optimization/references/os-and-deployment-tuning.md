# OS、虚拟化与部署层调优

本文件只给**与 MariaDB 相关**的 OS/虚拟化取值与验证方式；通用 OS/BIOS/网络调优仍走
`kpbot-app-tuner` 的 `os-optimization`（其自带确认门控）。所有变更都属于系统级操作，
需在维护窗口执行并可回滚。

## 0. 执行门控（先读；适用于全文每一条主机级命令）

本文件是**操作手册**，不是自动执行清单。任何全局/主机级变更都必须先按
`kpbot-app-tuner/references/user-interaction-gates.md` §3 确认 `agent_action_mode=approved_execute`
与 `change_scope`，并向用户逐项展示：①完整命令；②影响面（整机/服务/实例、是否波及其他业务
与租户）；③回滚命令；**等待用户显式确认后才执行**。未获确认时只输出手册，不执行。

| 命令 / 动作 | 影响面 | 门控 |
| --- | --- | --- |
| `cpupower frequency-set`、`sysctl -w`、`echo ... > /sys/...` | 整机 | 逐项确认 + 记录原值；生产限维护窗口 |
| grub 参数 + `grub2-mkconfig` | 整机 | 仅生成手册，**需用户手动重启，Agent 不得自动重启** |
| 全局关闭 THP（`transparent_hugepage/...`） | 整机所有进程/实例 | 逐项确认，说明对同机其他业务的影响 |
| `sync; echo 3 > /proc/sys/vm/drop_caches` | 整机 page cache | **仅专用压测机执行，生产禁止**；会冲击同机其他业务/租户 |
| systemd drop-in + `systemctl restart mariadb` | 服务级，现有连接中断 | 维护窗口 + 逐项确认；回滚见 §8 |
| `systemctl stop irqbalance` | 整机中断分布 | 逐项确认 + 记录原状态，限压测机；恢复用 `systemctl start irqbalance` |
| `echo ... > /sys/block/<dev>/queue/...` | 整机块设备 IO | 逐项确认 + 记录原值 |

## 1. CPU 频率与空闲

```
# 当前 governor
cpupower frequency-info 2>/dev/null | grep -E 'governor|driver' || \
  cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# 设为 performance（示例，需 root）
cpupower frequency-set -g performance
```

建议：

- 压测/生产用 `performance`，避免降频造成 P99 抖动。
- 记录改前/改后频率与温度；虚拟机内可能不可控（由宿主决定）。

grub 参数（**测试环境**可选，通用性差）：

```
nohz=on nosoftlockup cpuidle.off=1 skew_tick=1 processor.max_cstate=0 \
nmi_watchdog=0 audit=0 idle=poll
```

说明：`idle=poll` 让空闲 CPU 忙等，降低唤醒延迟但显著增加功耗；只适合独占压测机。
修改前备份 `/etc/default/grub`；`grub2-mkconfig` 后**需用户手动重启，Agent 不得自动重启**
（本文件只生成操作手册，不执行重启），重启后由用户执行 `cat /proc/cmdline` 复核。回滚：
恢复 `/etc/default/grub` 备份、重建配置后同样由用户手动重启。

## 2. 透明大页与大页

```
# 关闭 THP（临时）：整机全局生效，影响同机所有进程/实例；按 §0 逐项确认后执行
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag

# 查看
cat /sys/kernel/mm/transparent_hugepage/enabled
grep -E 'HugePages|AnonHugePages' /proc/meminfo
```

显式大页：

```
# 2MB 大页（按 NUMA 节点）
echo 16384 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
# 1GB 大页
echo 64 > /sys/devices/system/node/node0/hugepages/hugepages-1048576kB/nr_hugepages
```

my.cnf：`large_pages = ON`。

纪律：

- 预留大页前 `sync; echo 3 > /proc/sys/vm/drop_caches` 提高成功率：**仅专用压测机执行，
  生产禁止**；它清空整机 page cache，会冲击同机其他业务与租户（见 §0）。
- 大页不足会导致启动失败；改前记录 `nr_hugepages` 与可用内存。
- 同时开 THP 与显式大页会互相干扰；二选一（推荐显式大页）。
- 虚拟化：宿主给 guest 分配大页 + guest 内启用，两层都要核对。

## 3. 内存回写与换页

```
sysctl vm.dirty_ratio vm.dirty_background_ratio vm.swappiness
# 示例（测试/专用库）
sysctl -w vm.dirty_ratio=5
sysctl -w vm.dirty_background_ratio=2
sysctl -w vm.swappiness=1
```

说明：

- `dirty_ratio` 过高会让内核在阈值处同步回写，造成写尖刺；数据库写密集可适当降低，
但过低会让内核过早回写，增加 IO。
- `swappiness=1` 减少换页，但不能替代内存规划；内存不足仍会 OOM。
- 与 `innodb_flush_method=O_DIRECT` 配合时，page cache 压力主要来自 binlog/临时文件。

## 4. IO 调度、队列与挂载

```
# 查看/设置调度器
cat /sys/block/<dev>/queue/scheduler
echo mq-deadline > /sys/block/<dev>/queue/scheduler   # 示例
echo none > /sys/block/<dev>/queue/scheduler          # NVMe 常用

# 队列深度
echo 1024 > /sys/block/<dev>/queue/nr_requests
```

挂载：

```
XFS，noatime（可选 nodiratime），数据/redo/binlog 分盘
```

纪律：

- NVMe 常用 `none`/`mq-deadline`；SAS/SATA SSD 视型号，需 A/B。
- 挂载参数与 `innodb_flush_method` 要一起看；`O_DIRECT` 下避免重复缓存。
- 文件系统变更需停机/迁移数据，属于高风险操作。

## 5. 绑核与 NUMA

```
lscpu | grep -E 'NUMA|Socket|Core'
numactl --hardware

# 错误做法：只约束 systemctl 客户端。mariadbd 由服务管理器启动，不会继承，
# A/B 会把未绑核的一轮当成“绑核成功”：
# numactl --cpunodebind=0 --membind=0 -- systemctl start mariadb
# taskset -c 0-63 -- systemctl start mariadb

# 正确做法：CPU/内存策略写进服务单元 drop-in（不要直接改发行版自带单元）
# /etc/systemd/system/mariadb.service.d/10-cpu-numa.conf：
#   [Service]
#   CPUAffinity=0-63
#   NUMAPolicy=bind
#   NUMAMask=0
# 服务级变更：重启会中断现有连接，按 §0 在维护窗口逐项确认后执行；回滚见 §8。
systemctl daemon-reload
systemctl restart mariadb

# 旧版 systemd 无 NUMAPolicy/NUMAMask 时，在同一 drop-in 里包装 ExecStart。
# ExecStart= 是列表型选项，drop-in 取值与主单元【合并】而非覆盖：必须先用一行空的
# ExecStart= 清空列表，否则出现两条 ExecStart=，非 oneshot 单元加载失败（报
# "more than one ExecStart="），mariadb 在重启这一步起不来。落地前先
# systemctl cat mariadb 看清原 ExecStart，其余指令保留；落地后用
# systemd-analyze verify mariadb.service 与 systemctl show -p ExecStart mariadb 校验。
# /etc/systemd/system/mariadb.service.d/10-cpu-numa.conf 内容示例：
#   [Service]
#   ExecStart=
#   ExecStart=/usr/bin/numactl --cpunodebind=0 --membind=0 /usr/sbin/mariadbd $MYSQLD_OPTS
```

原则：

- 数据库线程与网卡中断同 NUMA；避免远端内存访问。
- 多实例：每个实例独立连续核段、不跨 NUMA，并在各自服务单元分别设置策略；
  不要用 `pgrep -x mariadbd | head -1` 代表目标实例。
- `innodb_numa_interleave=ON` 适合多 NUMA 且希望内存交错；单 NUMA 无收益。
- 绑定会降低弹性：混合负载可考虑“浮动 + 多实例”，并用数据验证。

验证（必须核对启动后**服务单元的 MainPID**，不是 systemctl 客户端/压测进程）：

```
UNIT=mariadb
MAINPID="$(systemctl show -p MainPID --value "$UNIT")"
[ "$MAINPID" -gt 0 ] || { echo "未取得 MainPID" >&2; exit 1; }
grep -E 'Cpus_allowed_list|Mems_allowed_list' "/proc/$MAINPID/status"
taskset -cp "$MAINPID"
numastat -p "$MAINPID"
pidstat -p "$MAINPID" -t 1 10
grep -E 'eth|mlx' /proc/interrupts | head
```

多实例无法从服务单元确认目标 PID 时，跳过 `numastat`/分配器的运行期检查，
或改用指定 socket 连接确认 PID（见 `scripts/verify_build_opts.sh`）；不要把某个实例的
数字当成另一个实例的结果。

## 6. 网卡与中断

```
# 队列数
ethtool -l <iface>
# 合并参数（降低中断频率，可能增加延迟）
ethtool -C <iface> rx-usecs 64 tx-usecs 64
# 关闭 irqbalance 后手工绑核：整机中断分布变更，按 §0 逐项确认 + 记录原状态，
# 限专用压测机；恢复用 systemctl start irqbalance（见 §8）。
systemctl stop irqbalance
```

建议：

- 中断绑到数据库同 NUMA 的核；队列数按吞吐与核数平衡。
- 不要盲目开 TSO/GRO/LRO；以压测结果为准。
- 万兆/25G/100G 场景先确认软中断分布（`mpstat -P ALL` 的 `%soft`）。

> 网络队列/RSS/RPS/中断亲和属于 `network-optimization` 范围；本节只给与数据库联调的要点。

## 7. 虚拟化场景（KVM）

| 项 | 建议 | 原因 |
| --- | --- | --- |
| CPU 绑核 | `cpuset` 落同一 NUMA，不跨片 | 跨片性能大幅下降（案例结论） |
| 内存 | `nodeset` 绑定对应 NUMA | 避免远端分配 |
| 大页 | 宿主按节点预留，guest 启用 | 降低 TLB miss |
| 磁盘 | 直通/NVMe，避免 qcow2 叠加 | 降低 IO 路径开销 |
| 网卡 | 直通或 vhost-user，中断同 NUMA | 降低软中断与拷贝 |
| iothreads | 独立 iothread 绑核 | 避免 IO 与 vCPU 争用 |

验收：guest 内 `numastat`/`lscpu`、`fio`/`iostat`、数据库压测三项都要看；只测数据库
可能掩盖 IO/网络瓶颈。

## 8. 变更与回滚

| 变更 | 回滚 |
| --- | --- |
| governor | 恢复原 governor（记录原值） |
| THP | 恢复 `enabled/defrag` 原值 |
| 大页 | 将 `nr_hugepages` 调回原值，关闭 `large_pages` |
| sysctl | `sysctl -p` 恢复备份文件 |
| 调度器/队列 | 写回原值 |
| 挂载 | 需停机回滚，提前演练 |
| grub | 恢复 `/etc/default/grub` 备份并重建；重启须用户手动执行，Agent 不得自动重启 |
| 绑核 | 删除/还原 `mariadb.service.d` drop-in，`systemctl daemon-reload && systemctl restart mariadb`，再用 MainPID 复核；若 drop-in 内含空赋值重置 + 包装的 `ExecStart`，删除该 drop-in 即恢复主单元原 `ExecStart`（重置只在 drop-in 内生效） |
| irqbalance | `systemctl start irqbalance` 恢复整机中断分布，确认服务已起来后复核软中断分布 |

每次只改一项，记录原值与新值；没有基线数字的“优化”不保留。
