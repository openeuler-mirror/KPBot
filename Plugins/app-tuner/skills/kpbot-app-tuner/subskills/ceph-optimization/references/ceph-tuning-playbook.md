# Ceph Tuning Playbook

Ceph 存储集群系统级与集群级调优的体检、变更与回退 SOP。事实来源：《鲲鹏BoostKit分布式存储使能套件 Ceph编译&部署&调优&测试指南》文档版本 07（2025-06-30）第 9.3.3 节（系统调优）、第 9.3.4 节（Ceph 调优），Ceph 块存储调优指南。本 playbook 所有变更命令均为候选动作素材，skill 体检阶段禁止直接执行（只读探测除外）。

## 1. 系统调优（指南 9.3.3）

> **归属边界**：本节为只读体检证据清单（os_tuning_findings[]），移交 os-optimization 出候选动作；变更命令与持久化（含 /etc/sysctl.d/99-os-optimization.conf）由 os-optimization 承接，建议值以其 os-playbook 为准。NUMA 设备亲和（1.2）同属证据移交范围；ceph.conf/PG/balancer/osd_numa_node（§2）才是本 skill 的候选动作范围。

### 1.1 OS 配置参数（表 9-14）

| 参数 | 默认值 | 建议值 | 配置方法 | 持久化 |
|---|---|---|---|---|
| vm.swappiness | 60 | 0（关闭 swap 使用） | `sudo sysctl vm.swappiness=0` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| MTU | 1500 Bytes | 9000 Bytes | `vim /etc/sysconfig/network-scripts/ifcfg-$(Interface)` 增加 `MTU="9000"`，然后 `service NetworkManager restart` | ifcfg 文件 |
| kernel.pid_max | 32768 | 4194303 | `echo 4194303 > /proc/sys/kernel/pid_max` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| fs.file-max | 系统自算（如 13291808） | 取 `cat /proc/meminfo \| grep MemTotal \| awk '{print $2}'` 的值 | `echo <file-max> > /proc/sys/fs/file-max` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| read_ahead | 128 KB | 8192 KB | `/sbin/blockdev --setra 8192 <dev>`（如 /dev/sdb） | udev rules / tuned / rc-local |
| I/O Scheduler | CFQ（发行版默认） | HDD=deadline；SSD/NVMe=none | `echo deadline > /sys/block/<dev>/queue/scheduler`；`echo none > /sys/class/block/<nvme_dev>/queue/scheduler` | udev rules / tuned |
| nr_requests | 128（SSD 默认 1023） | HDD=512；SSD/NVMe=1023 | `echo 512 > /sys/block/<dev>/queue/nr_requests`；`echo 1023 > /sys/block/<nvme_dev>/queue/nr_requests` | udev rules / tuned / rc-local |

- read_ahead / IO 调度器 / nr_requests 三项对**所有服务器上的所有数据盘**逐盘修改。
- 只读体检命令：`sysctl -n vm.swappiness`、`ip addr show`、`cat /proc/sys/kernel/pid_max`、`cat /proc/sys/fs/file-max`、`/sbin/blockdev --getra <dev>`、`cat /sys/block/<dev>/queue/scheduler`、`cat /sys/block/<dev>/queue/nr_requests`。

### 1.2 NUMA 亲和体检（只读证据）

- 目的：确认网络和存储资源在各 NUMA 节点间是否均衡分布。
- 体检命令（只读，仓库规范读路径）：`cat /sys/block/nvme*/device/numa_node`、`cat /sys/class/net/*/device/numa_node`。
- 结论处理：分布不均衡写入 `os_tuning_findings[]`（不生成动作）；绑核与 IRQ 亲和动作交 `cpu-affinity-optimization` / `network-optimization` 承接。
- 说明：设备 `numa_node` 由 PCIe 拓扑决定，sysfs 属性为只读，写入不可行也不会迁移设备；涉及插槽/拓扑调整属硬件层面，交 `hardware-upgrade-analysis`。

## 2. Ceph 调优（指南 9.3.4）

### 2.1 配置应用方式

- 所有 Ceph 配置通过修改 `/etc/ceph/ceph.conf` 实现，然后 `systemctl restart ceph.target` 重启守护进程生效。
- 只对当前节点生效；**必须修改所有 Ceph 节点的 ceph.conf 并重启**才对整个集群生效。
- 修改前备份：`cp /etc/ceph/ceph.conf{,.bak.<run_id>}`。
- cephadm/容器化部署：改用 `ceph config set <section> <key> <value>`（如 `ceph config set global osd_pool_default_size 3`），等价映射 kb 参数，无需手改 ceph.conf。
- 示例（副本数）：ceph.conf 添加 `osd_pool_default_size = 4` 后 `systemctl restart ceph.target`。

### 2.2 [global] 参数（表 9-15 前半）

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| osd_pool_default_min_size | 0 | 1 | min_size 是 PG 能接受 IO 的最小副本数 |
| cluster_network | 空 | 独立集群网段（如 192.168.4.0/24） | OSD 间副本复制/数据均衡走独立网段，缓解 public network 压力 |
| osd_pool_default_size | 3 | 3 | 副本数保持 3；变更属数据迁移风险动作 |
| mon_max_pg_per_osd | 250 | 3000 | 调大 PG 告警阈值 |
| mon_max_pool_pg_num | 65536 | 300000 | 调大 PG 告警阈值 |
| debug_*（全量 59 项） | 非 0 | 0/0 | 关闭 debug 减少日志打印开销；全量清单见 ceph-tuning-kb.json 的 ceph_conf.global.debug_*.list |
| throttler_perf_counter | True | False | 默认开启用于观察阈值是否达瓶颈，性能调到最佳后建议关闭（tracker 影响性能） |
| ms_dispatch_throttle_bytes | 104857600 | 2097152000 | 等待调度的最大消息数调大，提高消息处理效率 |
| ms_bind_before_connect | False | True | 消息队列绑定，保证多网口流量均衡 |

### 2.3 [client] 参数

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| rbd_cache | True | False | 关闭客户端 cache，关闭后 rbd cache 一直是 writethrough 模式 |

### 2.4 [osd] 参数（表 9-15 后半）

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| osd_max_write_size | 90 | 256 | OSD 一次可写入的最大值（MB） |
| osd_client_message_size_cap | 524288000 | 1073741824 | 客户端允许在内存中的最大数据（Bytes） |
| osd_map_cache_size | 50 | 1024 | 保留 OSD Map 的缓存（MB） |
| bluestore_rocksdb_options | 见下 | 见下 | rocksdb 配置参数 |
| mon_osd_full_ratio | 0.95 | 0.97 | OSD 视为已满的磁盘空间百分比 |
| mon_osd_nearfull_ratio | 0.85 | 0.95 | OSD 视为近似满的磁盘空间百分比 |
| osd_min_pg_log_entries | 3000 | 10 | PGLog 记录条数下阈值 |
| osd_max_pg_log_entries | 3000 | 10 | PGLog 记录条数上阈值 |
| bluestore_cache_meta_ratio | 0.4 | 0.8 | bluestore cache 中分配给 meta 的比例 |
| bluestore_cache_kv_ratio | 0.4 | 0.2 | bluestore cache 中分配给 kv 的比例 |

bluestore_rocksdb_options：
- 默认值：`compression=kNoCompression,max_write_buffer_number=4,min_write_buffer_number_to_merge=1,recycle_log_file_num=4,write_buffer_size=268435456,writable_file_max_buffer_size=0,compaction_readahead_size=2097152,max_background_compactions=2`
- 建议值：`compression=kNoCompression,max_write_buffer_number=64,min_write_buffer_number_to_merge=32,recycle_log_file_num=64,compaction_style=kCompactionStyleLevel,write_buffer_size=4MB,target_file_size_base=4MB,max_background_compactions=16,level0_file_num_compaction_trigger=64,level0_slowdown_writes_trigger=128,level0_stop_writes_trigger=256,max_bytes_for_level_base=6GB,compaction_threads=8,flusher_threads=4,compaction_readahead_size=2MB`

### 2.5 PG 分布调优（表 9-16）

- 公式：Total PGs = (Total_number_of_OSD × 100) / max_replication_count，结果向上取整到最近的 2 的整数次幂。
- 命令：创建时 `ceph osd pool create {pool-name} {pg-num} {pgp-num}`；已创建的 pool 用 `ceph osd pool set {pool_name} pg_num {pg-num}` 与 `ceph osd pool set {pool_name} pgp_num {pgp-num}` 修改；`ceph osd pool get {pool_name} pg_num/pgp_num` 查看。
- balancer：`ceph balancer mode upmap`（默认 none）+ `ceph balancer on`（默认关闭）；开启后每 60 秒自动调整少量 PG 分布；`ceph balancer eval` / `ceph pg dump` 随时查看分布，分布不再变化即达最佳。
- 注意：每个 OSD 承载的 PG 数量应相同或非常接近；除 PG 数量外，主 PG 也应尽可能均匀分布到各 OSD。

### 2.6 OSD 绑核（表 9-17）

- 通过 ceph.conf 添加 `osd_numa_node = <NUM>`（[osd.n] 节段）将 osd.n 绑定到指定空闲 NUMA 节点；空闲指处理网卡软中断的节点以外的节点。
- 查询网卡归属：`cat /sys/class/net/<网口名>/device/numa_node`；如网卡在 NUMA 2，则设 `osd_numa_node = 0` 或 `1`，避免 OSD 与网卡软中断使用相同 CPU core。
- 体检：`ceph daemon osd.N config show | grep osd_numa_node` 与网卡 NUMA 比对。

## 3. 变更与回退 SOP

1. **变更前**：备份 `/etc/ceph/ceph.conf`（`.bak.<run_id>`）；记录各 sysctl/sysfs/blockdev 原值；确认重启窗口授权。
2. **变更中**：逐节点应用（全部 Ceph 节点统一）；OS 层 sysctl 持久化由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf`，本 skill 不产生自有 sysctl 持久化文件；sysfs/blockdev 类变更由 os-optimization 逐盘执行并同步持久化（udev/tuned/rc-local）。
3. **变更后**：`systemctl restart ceph.target`；生效验证 `ceph daemon osd.0 config show | grep <param>`、`sysctl -n`、`blockdev --getra`；集群健康 `ceph -s` 回到 HEALTH_OK。
4. **回退**：还原 ceph.conf 备份并重启；sysctl/sysfs/blockdev 恢复原值并删除持久化文件；回退后复跑 `ceph -s` 与基线确认。

## 4. 验证

1. 配置生效：逐参数复查（见上）。
2. 集群健康：`ceph -s` 无新增 HEALTH_WARN；`ceph osd tree` 拓扑正常。
3. PG 收敛：`ceph balancer eval` 分值改善并趋于稳定；`ceph pg dump` 分布不再变化。
4. 业务级 A/B：rbd fio（如 4K 随机读写、1M 顺序）或业务压测，主流程轮次内 before/after 对比，收益回写 `measured`。
