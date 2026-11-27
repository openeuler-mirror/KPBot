# 拓扑、NUMA 与 OS 基线

本文给“装库之前机器应该长什么样”，以及“observer/obproxy/sysbench 应该放在哪”。
所有数值是实测基线与建议，不是硬性规格。

## 1. 规格与资源规划

| OB 规格 | observer `cpu_count` | `memory_limit` | 租户 CPU | 租户内存（交易创建/账务） | 备注 |
| --- | --- | --- | --- | --- | --- |
| 32U | 32 | 250G | 10（实测可跑满） | 150G / 20G | 本系列主测试规格 |
| 64U | 64 | 500G | 20 | 300G / 40G | 需同步放大租户 |
| 128U | 128 | 1000G | 120（或 128） | 600G / 80G | 实测出现 sql_nio 瓶颈，需加 `net_thread_count`（12→36） |

原则：

- `cpu_count` 是 observer 可用核数，不是租户核数；机器核多但租户小，跑不满是预期。
- 租户 `cpu_num`/`memory_size` 随规格翻倍；否则大规格下“机器有核、租户不用”。
- 大规格先确认 `net_thread_count` 与网卡队列/中断是否匹配，再谈 CPU 调优。
- 多实例（32U×2+64U×1）时，每实例独立 Unit、独立端口、独立目录，并做网络/中断隔离。

## 2. NUMA 与绑核

### 2.1 两种部署方式

| 方式 | 做法 | 优点 | 风险 |
| --- | --- | --- | --- |
| 裸机 `numactl` | 启动时 `numactl -C <cpus> -m <node> ./bin/observer` | 简单、直接 | 只绑 CPU 不绑内存会跨 P；启动后再 taskset 无效 |
| cgroup | 为 observer/obproxy 建 cgroup，限定 cpuset 与 memory node | 容器化生产一致；进程内部线程/内存在域内分配 | 配置错误时“看起来生效实际没生效”，需核验 |

### 2.2 绑核建议（实测）

| 规格 | observer CPU | observer 内存 | obproxy CPU | obproxy 内存 | sysbench CPU |
| --- | --- | --- | --- | --- | --- |
| 32U（三机） | 0-31 | numa0 | 32-95 | numa0 | 96-191（或按机器） |
| 64U | 0-63 | numa0 | 64-127 | numa0 | 其余 |
| 128U | 0-127 | numa0-1 | 128-191 | numa1 | 其余 |
| 单机三副本 | 每实例 32 核，分属不同 NUMA | 同 CPU | 单独 NUMA | 单独 NUMA | 单独 NUMA |

原则：

- **observer、obproxy、sysbench 尽量不跨片**；同 NUMA 优先，其次同 P。
- 网卡中断绑到与 observer/obproxy 同 NUMA 的核；多实例用 VF + 独立中断，避免实例互相拖累。
- 950 上 `2NPS → 1NPS` 实测读/写提升 12.8%/14.2%（920B 无明显变化），根因是减少跨 P。
- `observer` 绑核跨多个 L3（如 0-10,16-26,32-41）实测劣化；尽量连续、同 cluster。

### 2.3 跨 P 的机制

observer 启动时拉起上千线程，OS 把线程分布到全部 NUMA，内存就近分配；随后 `taskset`
只迁移线程 CPU，不迁移已分配内存 → 大量跨 P 访存。950 的 `numa_distance` 默认值使首次
选核更分散，跨 P 更严重。解决顺序：

1. 启动时绑定（`numactl` 或 cgroup）；
2. 无法重启时用 `migratepages <pid> <from> <to>` 迁移内存（临时手段）；
3. 调 `node_reclaim_distance`（内核参数，需验证；与 migratepages 收益重叠）。

## 3. 端口规划

| 用途 | 变量 | 本系列示例 | 检查 |
| --- | --- | --- | --- |
| observer MySQL | `${MYSQL_PORT}` | 2882 | 客户端/obproxy 指向一致 |
| observer RPC | `${RPC_PORT}` | 2881 | 节点间互通、防火墙放行 |
| obproxy | `${PROXY_PORT}` | 2828 | 压测机与业务入口 |
| SSH | 22 | 22 | 部署/采集 |
| 监控/导出（可选） | 自定义 | — | 不要与上面冲突 |

多实例时每组端口整体偏移，且不要与其它实例交叉。

## 4. 磁盘

```
yum install -y lvm2 xfsprogs
for d in /dev/nvme{1..12}n1; do dd if=/dev/zero of=$d bs=512 count=1; pvcreate $d; done
vgcreate ob_vg /dev/nvme{1..12}n1
lvcreate -n log_lv -L 2T ob_vg
lvcreate -n data_lv -l 100%FREE ob_vg
mkfs.xfs /dev/ob_vg/data_lv
mkfs.xfs /dev/ob_vg/log_lv
mkdir -p ${OB_BASE}/data ${OB_BASE}/log
mount /dev/ob_vg/data_lv ${OB_BASE}/data
mount /dev/ob_vg/log_lv ${OB_BASE}/log
# fstab: defaults,noatime,nodiratime,pquota
chown -R ${ADMIN_USER}:${ADMIN_USER} ${OB_BASE}/data ${OB_BASE}/log
```

原则：

- data 与 clog 分盘；clog 盘优先低延迟 NVMe。
- 单盘测试可用一块盘，但要知道 IO 争用会掩盖/放大问题。
- 文件系统 XFS；关闭 atime；预留足够空间（clog + 转储 + 备份）。
- 内存盘（tmpfs/ramdisk）只用于判断“IO 是否瓶颈”，不能作为容量结论。

## 5. OS 基线

### 5.1 sysctl（实测基线）

```
net.core.somaxconn=2048
net.core.netdev_max_backlog=500
net.core.rmem_default=65536
net.core.wmem_default=65536
net.core.rmem_max=8388608
net.core.wmem_max=8388608
net.ipv4.conf.default.rp_filter=1
net.ipv4.conf.default.accept_source_route=0
net.ipv4.tcp_syncookies=1
net.ipv4.tcp_rmem=4096 87380 16777216
net.ipv4.tcp_wmem=4096 65536 16777216
net.ipv4.tcp_max_syn_backlog=16384
net.ipv4.tcp_fin_timeout=15
net.ipv4.tcp_tw_reuse=1
net.ipv4.tcp_slow_start_after_idle=0
net.ipv4.ip_local_port_range=3500 65535
vm.swappiness=0
vm.min_free_kbytes=2097152
vm.max_map_count=655360
vm.overcommit_memory=0
vm.nr_hugepages=0
vm.zone_reclaim_mode=0
fs.aio-max-nr=1048576
fs.file-max=6573688
fs.pipe-user-pages-soft=0
kernel.numa_balancing=0
```

说明：

- `vm.max_map_count` 要足够大（observer 有大量 mmap）。
- `fs.aio-max-nr` 与异步 IO 相关。
- `kernel.numa_balancing`：压测/绑核场景常关；`numa_balance=on` 在部分场景有 5% 收益但也有
负收益（920B 只写 -2.6%），必须 A/B。
- `vm.zone_reclaim_mode=0` 避免本地节点回收造成抖动。

### 5.2 必须关闭/确认

```
# 透明大页
echo never > /sys/kernel/mm/transparent_hugepage/enabled
echo never > /sys/kernel/mm/transparent_hugepage/defrag
# swap
swapoff -a
# selinux
# /etc/selinux/config: SELINUX=disabled
# firewalld
systemctl stop firewalld && systemctl disable firewalld
# sshd/crond
systemctl enable --now sshd crond
```

> THP/swap 关闭在重启后可能失效；用 systemd unit/tuned 固化，preflight 每次检查。

### 5.3 ulimit

`/etc/security/limits.d/99-oceanbase_limits.conf`：

```
* soft nofile 655350
* hard nofile 655350
* soft stack 10240
* hard stack 10240
* soft nproc 655360
* hard nproc 655360
* soft core unlimited
* hard core unlimited
```

压测机（sysbench）常见还需要 `ulimit -SHn 1000000000`（按需，不要盲目套用）。

### 5.4 时间同步

三副本必须 chrony/ntpd 同步；时钟漂移会导致 Paxos/租约异常。

### 5.5 网络

- `nf_conntrack` 卸载（非客户标准模型，鲲鹏调优手段）：`/etc/modprobe.d/blacklist.conf`
中加入 `install nf_conntrack /bin/false` 与 `blacklist nf_conntrack*` 等，重启生效。
实测网络模块优化收益约 6.8%。
- 网卡中断绑核到 observer/obproxy 同 NUMA；多实例用 VF 隔离。
- 可选：sockmap/hisock（eBPF socket 直连）、SMC-D。二者收益有重叠，需成对 A/B；
实测某场景 sockmap 收益最大，且开 sockmap 后网卡中断绑核收益变小。
- 64K 页表：需重编内核，有实测收益但本基线未固化具体数值；影响周边组件，需评审并在目标环境 A/B 复测后再引用。
- grub 参数（实测约 1.8%）：`nohz=off cgroup_disable=files idle=poll kpti=off`，
修改 `/etc/default/grub` 后 `grub2-mkconfig`，重启生效。

## 6. cgroup 策略

三种常见模式（实测）：

| 模式 | 做法 | 950 | 920B | 适用 |
| --- | --- | --- | --- | --- |
| 绑核 | observer 固定 0-31，obproxy 32-95 | 最优 | 次优 | 裸机/固定拓扑 |
| 绑 NUMA | observer 限制在 NUMA0 的 CPU 范围，obproxy NUMA1 | 次优 | 最优 | 容器、生产推荐 |
| 绑 1P | observer 在 1P 内限 32 核自由调度 | 最差 | 最差 | 仅在需要弹性时 |

实测结论：950 绑核最优、920B 绑 NUMA 最优；cgroup 最优场景下 950 与 920B/AMD 9T34 的
具体差距本基线未固化数值，跨机型不横比，需在目标机型同口径复测后再引用。裸机建议启动时
`numactl`；容器建议 cgroup + CPU/内存亲和。

## 7. 部署前只读检查

```
OB_BASE=${OB_BASE} OB_SRC=${OB_SRC} \
  MYSQL_PORT=${MYSQL_PORT} RPC_PORT=${RPC_PORT} PROXY_PORT=${PROXY_PORT} \
  bash scripts/preflight_check.sh
```

检查项：架构、NUMA 节点与距离、内存、磁盘与挂载、内核参数、THP/swap/selinux、ulimit、
chrony、端口占用、依赖目录、残留 observer/obproxy 进程。有 FAIL 不得继续。
