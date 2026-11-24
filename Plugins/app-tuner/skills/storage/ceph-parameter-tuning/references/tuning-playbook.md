# Ceph Tuning Playbook

> **独立使用说明**：本 skill（ceph-parameter-tuning）独立使用时，§1 OS 层参数属本 skill 调优范围（变更动作由本 skill 输出，sysctl 类持久化统一 `/etc/sysctl.d/99-os-optimization.conf`）；作为编排器 subskill 双轨使用时，§1 仅输出只读证据 os_tuning_findings 移交 os-optimization，变更与持久化由 os-optimization 承接（见 kpbot-app-tuner/subskills/ceph-optimization）。§1 归属边界与 §3 变更中步骤均按上述两种模式分叉书写。

Ceph 存储集群系统级与集群级调优的体检、变更与回退 SOP。事实来源：《鲲鹏 Ceph 官方指南》（2025-06-30，系统调优与 Ceph 调优部分），Ceph 块存储调优指南；参数基线对应 Ceph 14.2/15.2 世代（Nautilus/Octopus），v15.2+/v16.2.10+ 的默认值差异见 tuning-kb.json 各条目 version_note，体检一律以现场读取为准。本 playbook 所有变更命令均为候选动作素材，skill 体检阶段禁止直接执行（只读探测除外）。

## 1. 系统调优

> **归属边界（按模式分叉）**：独立使用时，本节 OS 层参数（含 1.2 NUMA 设备亲和体检）由本 skill 出候选动作并负责持久化（sysctl 类落 `/etc/sysctl.d/99-os-optimization.conf`，sysfs/blockdev 类附 udev rules/tuned/rc-local 持久化方案）；编排器双轨使用时，本节为只读体检证据清单（os_tuning_findings[]），移交 os-optimization 出候选动作，变更命令与持久化（含 /etc/sysctl.d/99-os-optimization.conf）由 os-optimization 承接，建议值以其 os-playbook 为准。ceph.conf/PG/balancer/osd_numa_node（§2）两种模式下均为本 skill 的候选动作范围。

### 1.1 OS 配置参数

| 参数 | 默认值 | 建议值 | 配置方法 | 持久化 |
|---|---|---|---|---|
| vm.swappiness | 60 | 0（关闭 swap 使用） | `sudo sysctl vm.swappiness=0` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| MTU | 1500 Bytes | 9000 Bytes | `vim /etc/sysconfig/network-scripts/ifcfg-$(Interface)` 增加 `MTU="9000"`，然后 `service NetworkManager restart` | ifcfg 文件 |
| kernel.pid_max | 32768 | 4194303 | `echo 4194303 > /proc/sys/kernel/pid_max` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| fs.file-max | 系统自算（如 13291808） | `<MemTotal_kB>`（现场按 resolve_cmd 求值，禁止原样写入 sysctl.d） | `echo <file-max> > /proc/sys/fs/file-max` | 由 os-optimization 统一落 `/etc/sysctl.d/99-os-optimization.conf` |
| read_ahead | 128 KB | 8192 KB | `/sbin/blockdev --setra 16384 <dev>`（扇区单位，16384×512B = 8192 KiB；勿传 8192） | udev rules / tuned / rc-local |
| I/O Scheduler | CFQ（发行版默认） | HDD=deadline；SSD/NVMe=none | `echo deadline > /sys/block/<dev>/queue/scheduler`；`echo none > /sys/class/block/<nvme_dev>/queue/scheduler` | udev rules / tuned |
| nr_requests | 128（SSD 默认 1023） | HDD=512；SSD/NVMe=1023 | `echo 512 > /sys/block/<dev>/queue/nr_requests`；`echo 1023 > /sys/block/<nvme_dev>/queue/nr_requests` | udev rules / tuned / rc-local |

- read_ahead / IO 调度器 / nr_requests 三项对**所有服务器上的所有数据盘**逐盘修改。
- 只读体检命令：`sysctl -n vm.swappiness`、`ip addr show`、`cat /proc/sys/kernel/pid_max`、`cat /proc/sys/fs/file-max`、`/sbin/blockdev --getra <dev>`（期望 16384 扇区）、`cat /sys/block/<dev>/queue/scheduler`、`cat /sys/block/<dev>/queue/nr_requests`。

### 1.2 NUMA 亲和体检（只读证据）

- 目的：确认网络和存储资源在各 NUMA 节点间是否均衡分布。
- 体检命令（只读，仓库规范读路径）：`cat /sys/block/nvme*/device/numa_node`、`cat /sys/class/net/*/device/numa_node`。
- 结论处理：分布不均衡写入 `os_tuning_findings[]`（不生成动作）；绑核与 IRQ 亲和动作交 `cpu-affinity-optimization` / `network-optimization` 承接。
- 说明：设备 `numa_node` 由 PCIe 拓扑决定，sysfs 属性为只读，写入不可行也不会迁移设备；涉及插槽/拓扑调整属硬件层面，交 `hardware-upgrade-analysis`。

## 2. Ceph 调优

### 2.1 配置应用方式

- 所有 Ceph 配置通过修改 `/etc/ceph/ceph.conf` 实现，然后 `systemctl restart ceph.target` 重启守护进程生效。
- 只对当前节点生效；**必须修改所有 Ceph 节点的 ceph.conf 并重启**才对整个集群生效。
- 修改前备份：`cp /etc/ceph/ceph.conf{,.bak.<run_id>}`。
- cephadm/容器化部署用 `ceph config set <who> <key> <value>` 写入 Monitor 配置库；变更前用 `ceph config get <who> <key>` 记录原有显式值（区分"已显式设置"与"未设置"），回退时已设置的恢复旧值、本轮新增的用 `ceph config rm <who> <key>` 移除，并以 `ceph config get` 与 `ceph daemon osd.N config show` 核验有效值。
- 示例（副本数）：ceph.conf 添加 `osd_pool_default_size = 4` 后 `systemctl restart ceph.target`。

### 2.2 [global] 参数

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| cluster_network | 空 | 独立集群网段（如 192.0.2.0/24，RFC 5737 文档示例网段，按现场规划替换） | OSD 间副本复制/数据均衡走独立网段，缓解 public network 压力 |
| osd_pool_default_size | 3 | 3 | 副本数保持 3；变更属数据迁移风险动作 |
| mon_max_pg_per_osd | 250 | 3000 | 调大 PG 告警阈值 |
| mon_max_pool_pg_num | 65536 | 300000 | 调大 PG 告警阈值 |
| debug_*（全量 59 项） | 非 0 | 0/0 | 关闭 debug 减少日志打印开销；全量清单见 references/tuning-kb.json 的 ceph_conf.global.debug_*.list |
| throttler_perf_counter | True | False | 默认开启用于观察阈值是否达瓶颈，性能调到最佳后建议关闭（tracker 影响性能） |
| ms_dispatch_throttle_bytes | 104857600 | 2097152000 | 等待调度的最大消息数调大，提高消息处理效率 |
| ms_bind_before_connect | False | True | 消息队列绑定，保证多网口流量均衡 |

### 2.3 [client] 参数

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| rbd_cache | True | False | 关闭客户端 cache，关闭后 rbd cache 一直是 writethrough 模式 |

### 2.4 [mon] 参数

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| mon_osd_full_ratio | 0.95 | 0.97 | monitor 节段参数（monitor 不读 [osd]，conf 持久化须写 [mon] 节段，cephadm 持久化用 ceph config set mon）；OSD 视为已满的磁盘空间百分比；已运行集群用 ceph osd set-full-ratio/set-nearfull-ratio 运行时变更（ceph osd dump 先记录原值，回退恢复原值），校验 nearfull < backfillfull < full 顺序 |
| mon_osd_nearfull_ratio | 0.85 | 0.95 | monitor 节段参数（monitor 不读 [osd]）；OSD 视为近似满的磁盘空间百分比；运行时通道与顺序校验同上 |

### 2.5 [osd] 参数

| 参数 | 默认值 | 建议值 | 说明 |
|---|---|---|---|
| osd_max_write_size | 90 | 256 | OSD 一次可写入的最大值（MB） |
| osd_client_message_size_cap | 524288000 | 1073741824 | 客户端允许在内存中的最大数据（Bytes） |
| osd_map_cache_size | 50 | 1024 | 保留 OSD Map 的缓存（MB） |
| bluestore_rocksdb_options | 见下 | 见下 | rocksdb 配置参数 |
| osd_min_pg_log_entries | 3000 | 10 | PGLog 记录条数下阈值 |
| osd_max_pg_log_entries | 3000 | 10 | PGLog 记录条数上阈值 |
| bluestore_cache_meta_ratio | 0.4 | 0.8 | bluestore cache 中分配给 meta 的比例（前置 bluestore_cache_autotune=false，见下方说明） |
| bluestore_cache_kv_ratio | 0.4 | 0.2 | bluestore cache 中分配给 kv 的比例（前置 bluestore_cache_autotune=false，见下方说明） |

bluestore_rocksdb_options：
- 默认值：`compression=kNoCompression,max_write_buffer_number=4,min_write_buffer_number_to_merge=1,recycle_log_file_num=4,write_buffer_size=268435456,writable_file_max_buffer_size=0,compaction_readahead_size=2097152,max_background_compactions=2`
- 建议值：`compression=kNoCompression,max_write_buffer_number=64,min_write_buffer_number_to_merge=32,recycle_log_file_num=64,compaction_style=kCompactionStyleLevel,write_buffer_size=4MB,target_file_size_base=4MB,max_background_compactions=16,level0_file_num_compaction_trigger=64,level0_slowdown_writes_trigger=128,level0_stop_writes_trigger=256,max_bytes_for_level_base=6GB,compaction_threads=8,flusher_threads=4,compaction_readahead_size=2MB`

**bluestore cache 比例前置（bluestore_cache_autotune）**：`bluestore_cache_autotune` 自 Octopus（v15.2）起上游默认 true，开启时 cache 分区按优先级动态分配，仅改 meta/kv 比例大概率不生效——调整 `bluestore_cache_meta_ratio`/`bluestore_cache_kv_ratio` 前必须先置 `bluestore_cache_autotune = false`（体检命令 `ceph config get osd bluestore_cache_autotune` 或 `ceph daemon osd.N config show | grep bluestore_cache_autotune`，为 true 时先按 §3 流程变更再调比例）；关闭 autotune 后 cache 不再随 osd_memory_target 自动伸缩，需自行管理 `bluestore_cache_size`，且该机制以 TCMalloc 为分配器前提。

### 2.6 PG 分布调优

- 公式：Total PGs = (Total_number_of_OSD × 100) / max_replication_count，结果向上取整到最近的 2 的整数次幂。
- 命令：创建时 `ceph osd pool create {pool-name} {pg-num} {pgp-num}`；已创建的 pool 用 `ceph osd pool set {pool_name} pg_num {pg-num}` 与 `ceph osd pool set {pool_name} pgp_num {pgp-num}` 修改；`ceph osd pool get {pool_name} pg_num/pgp_num` 查看。
- balancer：`ceph balancer mode upmap`（默认 none）+ `ceph balancer on`（默认关闭）；开启后每 60 秒自动调整少量 PG 分布；`ceph balancer eval` / `ceph pg dump` 随时查看分布，分布不再变化即达最佳。
- 注意：每个 OSD 承载的 PG 数量应相同或非常接近；除 PG 数量外，主 PG 也应尽可能均匀分布到各 OSD。

### 2.7 OSD 绑核

- 通过 ceph.conf 添加 `osd_numa_node = <NUM>`（[osd.n] 节段）将 osd.n 绑定到指定空闲 NUMA 节点；空闲指处理网卡软中断的节点以外的节点。
- 查询网卡归属：`cat /sys/class/net/<网口名>/device/numa_node`；如网卡在 NUMA 2，则设 `osd_numa_node = 0` 或 `1`，避免 OSD 与网卡软中断使用相同 CPU core。
- 体检：`ceph daemon osd.N config show | grep osd_numa_node` 与网卡 NUMA 比对。

## 3. 变更与回退 SOP

1. **变更前**：备份 `/etc/ceph/ceph.conf`（`.bak.<run_id>`）；记录各 sysctl/sysfs/blockdev 原值；对每个将写入 /etc/sysctl.d/99-os-optimization.conf 的键，先检查该键在全部 sysctl.d 文件中是否已有显式条目（grep -r "^\s*<key>\s*=" /etc/sysctl.d/ /etc/sysctl.conf），记录"已存在+原值"或"不存在"两种状态；确认重启窗口授权。
2. **变更中**：逐节点应用（全部 Ceph 节点统一）；OS 层 sysctl 持久化——独立使用时由本 skill 落 `/etc/sysctl.d/99-os-optimization.conf`，双轨使用时由 os-optimization 统一落盘、本 skill 仅移交 os_tuning_findings 证据；sysfs/blockdev 类变更按对应模式由本 skill 或 os-optimization 逐盘执行并同步持久化（udev/tuned/rc-local）；写入时只新增/修改本轮涉及的键，不得整文件重写覆盖他人条目。
3. **变更后**：`systemctl restart ceph.target`；生效验证 `ceph daemon osd.0 config show | grep <param>`、`sysctl -n`、`blockdev --getra`；集群健康 `ceph -s` 回到 HEALTH_OK；cephadm 路径的回退按配置库键级恢复（config rm / 恢复旧值），并核验有效值；仅还原本地 ceph.conf 不足以回退配置库变更。
4. **回退**：还原 ceph.conf 备份并重启；sysctl 类回退为键级恢复——本轮修改且原值已存在的键恢复原值；本轮新增的键删除对应行；禁止删除整个 99-os-optimization.conf（同文件可能承载其他调优项）；删除后 sysctl --system 并逐键 sysctl -n 复核；回退后复跑 `ceph -s` 与基线确认。

## 4. 验证

1. 配置生效：逐参数复查（见上）。
2. 集群健康：`ceph -s` 无新增 HEALTH_WARN；`ceph osd tree` 拓扑正常。
3. PG 收敛：`ceph balancer eval` 分值改善并趋于稳定；`ceph pg dump` 分布不再变化。
4. 业务级 A/B：rbd fio（如 4K 随机读写、1M 顺序）或业务压测，主流程轮次内 before/after 对比，收益回写 `measured`。
