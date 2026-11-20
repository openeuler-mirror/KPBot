# 配置面机制与配置项详解

> **何时读我**：要读/改/下发任何 3FS 配置项时读本文 —— 前半部分是配置快照与热更新机制（`get-config`/`set-config`/ConfigVersion、哪些参数需重启）、改参标准流程；后半部分是逐项配置详解（`sync_when_write`、`traffic_control.write`、`num_bg_threads`、checksum、`force_use_tcp`、线程池基线、RDMA/ibsocket）。

节点角色与示例 IP 见 `SKILL.md`；本文命令中的 `${MOUNT_POINT}`、`${FIO_DIR}` 为占位符。

---

## 一、配置面快照与热更新机制

**任何 A/B 之前先快照配置面**，否则会把配置收益误记到别的改动头上（曾因此误判一次，详见 `lever-ranking.md` §2.5 ②）。

### 1.1 快照命令

```bash
ADMIN_CLI="/opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml"
$ADMIN_CLI "get-config --node-type STORAGE -o /tmp/storage_current.toml"
$ADMIN_CLI "get-config --node-type FUSE    -o /tmp/fuse_current.toml"
$ADMIN_CLI "get-config --node-type MGMTD   -o /tmp/mgmtd_current.toml"
grep -nE 'enableChecksum|chunk_checksum_type|sync_when_write|force_use_tcp|num_bg_threads|max_concurrent_requests' /tmp/*_current.toml
```

- `get-config --node-type <TYPE>` 读的是 **mgmtd 里存的该类型配置**，输出表格含 `NodeType` / `ConfigVersion` / `ConfigDesc` / `OutputFile`；`-o/--output-file` 必填。
- `get-config --node-id <id> -o <file>` 读的是 **该运行实例进程内的配置**（走 Core 服务），用于核对"节点上跑的和 mgmtd 里存的是否一致"。
- `get-config --list-versions` 一次列出所有类型的 ConfigVersion。
- `<TYPE>` 可选：`MGMTD` / `META` / `STORAGE` / `CLIENT` / `CLIENT_AGENT` / `FUSE`。

记录 **ConfigVersion**，A/B 两腿必须同版本。

> **纪律：节点配置一律以实际导出为准。** 各节点 `/opt/3fs/etc/*.toml` 是本地文件，可能与运行时不一致；直接看本地缓存文件可能并不准确。判断"当前值是什么"只用 `get-config` 的导出结果。

### 1.2 修改与下发

```bash
sed 's/sync_when_write = true/sync_when_write = false/' /tmp/storage_current.toml > /tmp/storage_new.toml
$ADMIN_CLI "set-config --type STORAGE --file /tmp/storage_new.toml --desc 'sync_when_write=false'"
# → 输出 Succeed + ConfigVersion 递增（如 15 → 16）
```

**改前先备份**：`cp /opt/3fs/etc/storage_main.toml /opt/3fs/etc/storage_main.toml.bak.$(date +%Y%m%d)`，同时保留 `get-config` 导出的那份（回滚用它最可靠，因为它就是改动前的运行时真相）。

**单项改动优先用 `scripts/set_config_param.sh`**（段落感知，防同名配置项误伤），见本文 §二。`sed` 全文替换只在确认该 key 全文唯一时使用 —— 例如 `enableChecksum` 同时存在于 `[storage_io.read]` 与 `[storage_io.write]`，`sync_when_write` 同时存在于 `kv_store` 与 `meta_store` 段。

### 1.3 热更新能力（关键差异）

源码里用 `CONFIG_HOT_UPDATED_ITEM(name, default, ...)` 宏声明的配置项才具备热更新能力（`src/common/utils/ConfigBase.h:116`，展开为 `CONFIG_ADD_ITEM(..., true, ...)`）。核对某个参数是否可热更新：

```bash
grep -rn "CONFIG_HOT_UPDATED_ITEM(<参数名>" <3FS 源码目录>/src/
```

**声明可热更新 ≠ 实测立即生效**，两者要分开记录：

| 组件 | 配置热更新 | 生效方式 |
|---|---|---|
| `storage_main` | **支持** | `set-config` 后部分项即时生效；`sync_when_write` 实测需 `systemctl restart storage_main` |
| `hf3fs_fuse_main` | **不支持** | 日志：`ApplicationBase.cc:125 WARNING] Update config ignored: this app declares that its config is not hotupdatable` → **必须 `systemctl restart hf3fs_fuse_main`** |
| `mgmtd_main` / `meta_main` | 部分支持 | 保守做法：restart |

示例：`sync_when_write` 在 `src/kv/KVStore.h:23` 声明为 `CONFIG_HOT_UPDATED_ITEM(sync_when_write, true)`，但实测改完仍需重启 storage 才见效 —— 所以**每次改参都要用复测确认生效**，不要假设。

### 1.4 重启面控制

重启顺序：**storage 全部节点 → 所有 FUSE 客户端**。

```bash
# 密码通过 read -rs 注入环境变量, 不落盘、不进 history
read -rs SSHPASS; export SSHPASS
for h in <storage1> <storage2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart storage_main"
done
for h in <fuse-client-1> <meta> <fuse-client-2>; do
  sshpass -e ssh -o StrictHostKeyChecking=no root@"$h" "systemctl restart hf3fs_fuse_main"
done
```

**重启面越小越好**：只重启受影响的组件。改客户端参数不必重启 storage；改 storage 参数不必重建 chain table。

**FUSE 挂载点残留**：客户端崩溃后重启会报 `fuse: failed to access mountpoint ${MOUNT_POINT}: Transport endpoint is not connected` —— **必须先 umount 再 restart**。

### 1.5 节点本地配置文件同步

改了参数之后，**各节点 `/opt/3fs/etc/` 下的同名 toml 也要改成一致内容**（例如两个 storage 节点的 `storage_main.toml`）。这个目录展示的配置项必须与运行实例一致，否则下次进程重启会用旧的本地文件启动，把 mgmtd 里的新配置"顶掉"，A/B 结论随之失效。

上传本地文件到 mgmtd 的写法（与 `set-config --file` 相同）：

```bash
$ADMIN_CLI "set-config --type STORAGE --file /opt/3fs/etc/storage_main.toml --desc 'sync from node local'"
$ADMIN_CLI "set-config --type META    --file /opt/3fs/etc/meta_main.toml"
$ADMIN_CLI "set-config --type FUSE    --file /opt/3fs/etc/hf3fs_fuse_main.toml"
```

---

## 二、改参标准流程（备份 → 改 → 下发 → 回读 → 重启面控制 → 复测）

```
0. 前置：确认瓶颈在软件配置面（用 skill 3fs-hotspot-code-optimization 做热点归因）
        并对照 lever-ranking.md 两张表，确认这个杠杆值得动、且没被证伪
1. 基线：scripts/fio_bench.sh 跑标准压测，记录带宽/IOPS/延迟 + ConfigVersion + 二进制 md5
2. 备份：get-config 导出当前配置并另存（set_config_param.sh 自动做，文件名含 ConfigVersion）
3. 改参：一次只动一个变量
4. 下发：set-config --type <TYPE> --file <new> --desc '<改了什么>'  → ConfigVersion 递增
5. 回读：再 get-config 一次，确认生效值 == 目标值（不看本地文件、不看命令回显）
6. 重启面控制：按 §1.3 表判断是否需要重启，只重启必要组件；重启后再次回读
7. 同步：各节点 /opt/3fs/etc/ 下同名 toml 改成一致（§1.5）
8. 复测：同口径重跑第 1 步的压测，对比带宽/CPU-秒/延迟分位数
9. 台账：记录到调参台账（模板见 SKILL.md）；无收益或劣化 → 用备份回滚并记入"已证伪"
```

`scripts/set_config_param.sh` 覆盖第 2~5 步：

```bash
# 用法: set_config_param.sh <NODE_TYPE> <toml_section> <key> <value>
./scripts/set_config_param.sh STORAGE server.targets.storage_target.kv_store sync_when_write false
./scripts/set_config_param.sh FUSE storage_io.write enableChecksum false
./scripts/set_config_param.sh FUSE storage.traffic_control.write max_concurrent_requests 256
./scripts/set_config_param.sh FUSE storage.traffic_control.write max_concurrent_requests_per_server 64
```

脚本内部动作（也是手工改参时应当照做的动作）：

1. `get-config --node-type <TYPE> --output-file <tmp>` 导出运行时配置，记录改前 ConfigVersion
2. **awk 状态机段内定位**：遇到段头行重置 `insec` 标志，只在目标段内匹配 key，取出当前值 —— 防止其它段的同名 key 被误伤
3. 幂等：当前值 == 目标值直接退出
4. **python3 段落感知替换**：同样按段头切换 `insec`，只替换段内该 key；`changed != 1` 立即报错且不写文件（0 处 = 段名/key 写错，2 处 = 该段是 `[[array.of.tables]]` 重复段，两种情况都不允许盲改）
5. 备份改前配置到 `${BACKUP_DIR}/<TYPE>_config_v<版本>_<时间>.toml`，并打印回滚命令
6. `set-config --type <TYPE> --file <tmp> --desc "set <section>.<key> to <value>"`
7. `sleep 1` 后再次 `get-config` 回读，校验 `FINAL == TARGET`，打印 ConfigVersion 变化

env：`ADMIN_CLI`（默认 `/opt/3fs/bin/admin_cli`）、`ADMIN_CLI_CONFIG`（默认 `/opt/3fs/etc/admin_cli.toml`）、`BACKUP_DIR`（默认 `/tmp`）。脚本不含任何密码/Token 默认值。

**前置条件**：在部署了 `admin_cli` 的节点执行，且 `mgmtd_main` 已启动、RDMA/IB 链路正常（mgmtd 未运行或 IB 口 Down 时 `admin_cli` 会直接异常退出）。

---

## 三、配置项详解

### 3.1 `sync_when_write`（收益最大的安全类开关）

| 项 | 值 |
|---|---|
| 文件 | `/opt/3fs/etc/storage_main.toml` |
| 段 | `[server.targets.storage_target.kv_store]` |
| 源码默认 | `true`（`configs/storage_main.toml:469-503`） |
| 调优值 | `false` |
| 实测 | ConfigVersion 15（true）**10068 MiB/s**（fuse-client-1=3341, meta=3333, fuse-client-2=3394）→ ConfigVersion 16（false）**12767 MiB/s（+26.8%）**（fuse-client-1=4077, meta=4245, fuse-client-2=4445） |

**动机证据**：火焰图上 RocksDB/WAL sync 链占 storage CPU **23.6%**：

```
rocksdb::DBImpl::Write → WriteToWAL → WritableFileWriter::Sync
  → fdatasync → xfs_file_fsync → xlog_cil_force_seq
```

**作用范围（重要）**：只影响 **commit 阶段**的 KV 写（`src/storage/chunk_engine/src/core/engine.rs:470`）。**update 阶段的 sync 是硬编码的，不受此开关控制**：

```rust
// src/storage/chunk_engine/src/meta/meta_store.rs:348-355
// persist_writing_chunk()，每次 update 都会走（engine.rs:457）
self.rocksdb.put(chunk_meta_key, &bytes[..], true)   // sync=true 是字面量
```

C++ 旧路径的对应实现是 `src/kv/RocksDBStore.cc:79-82,111-114`：`options.sync = config_.sync_when_write() || sync`。

**风险**：未 fdatasync 的 WAL 在掉电/崩溃时丢失。**仅适用于测试环境、有 UPS、或多副本足以容忍单节点丢 WAL 的场景。** 上线前必须与用户确认。

**收益上限受硬件制约**：若 NVMe FLUSH 本身很快（A 集群固件 1059，`direct=1 + fdatasync` 均值 **2.3 µs**、p99 40~42 µs），关掉 sync 的收益会显著小于 26.8%。先测裸盘 fdatasync 延迟再决定（见 `hw-and-firmware.md`）。

完整闭环案例（含回退步骤与风险评估）：`case-sync-when-write.md`。

### 3.2 `traffic_control.write`（客户端并发窗口）

文件 `/opt/3fs/etc/hf3fs_fuse_main.toml`，段 `[storage.traffic_control.write]`：

| `max_concurrent_requests` | `max_concurrent_requests_per_server` | 顺序写带宽 | 状态 |
|---|---|---|---|
| 32（默认） | 8 | 785 MiB/s | PASS |
| 64 | 16 | 1188 MiB/s | PASS |
| 128 | 32 | 1190 MiB/s | PASS |
| 128 | 64 | 1168 MiB/s | PASS |
| 256 | 32 | 1185 MiB/s | PASS |
| **256** | **64** | **1198 MiB/s** | **PASS（最佳，当前值）** |
| 512 | 64 | — | **FAIL：`Bad file descriptor`** |
| 512 | 128 | — | **FAIL：`Bad file descriptor`** |

**64 → 256 之间有平台期**（1188/1190/1168/1185/1198 全在噪声带内），从 (32,8) 到 (64,16) 才是真跳变。**512 触发资源限制，是硬上限。**

其它 TC 段（read/query/remove/truncate）保持 `32/8`；相关项 `max_batch_bytes='4MB'`、`max_batch_size=128`、`process_batches_in_parallel=true`、`random_shuffle_requests=true`。

> 两个参数要分别改（各一次 `set_config_param.sh`），改完一起复测；若要严格遵循"一次只动一个变量"，先改 `max_concurrent_requests` 复测，再改 `max_concurrent_requests_per_server` 复测。

### 3.3 `num_bg_threads`（客户端后台线程）

需在 **4 处**同时改（launcher 与 main 各一处 `[client.thread_pool]`，加 `[storage.net_client.thread_pool]`、`[storage.net_client_for_updates.thread_pool]`）：

| `num_bg_threads` | 顺序写（单客户端） | CliBG 线程数 |
|---|---|---|
| 2 | 461 MiB/s | 8 |
| 4 | 782 MiB/s | 14 |
| **6** | **815 MiB/s** | **20** |
| 8 | 819 MiB/s（峰值） | 26 |
| 16 | 816 MiB/s | 50 |
| 32 | 818 MiB/s | 98 |

**6 之后完全平台期**。8 是峰值但只比 6 高 0.5%（噪声级）却多 6 个线程 → **选 6（性价比点）**。

> 这是"同名 key 出现在多个段"的典型：`set_config_param.sh` 一次只改一个段，4 处要调用 4 次（段名分别是 `client.thread_pool`、`storage.net_client.thread_pool`、`storage.net_client_for_updates.thread_pool`，launcher 配置在 `hf3fs_fuse_main_launcher.toml`）。

### 3.4 checksum 相关配置

| 配置项 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `enableChecksum`（写） | `src/client/storage/StorageClient.h:214` `WriteOptions` | **true** | 门控 `StorageClientImpl.cc:1878` 的 `ChecksumInfo::create`；改 false 得 **7.8x** |
| `enableChecksum`（读） | `[storage_io.read]` | — | 读校验在 `StorageClientImpl.cc:1717-1721` |
| `chunk_checksum_type` | `[storage]`（fuse main toml） | `'CRC32C'` | **`'NONE'` 不可用，见下** |
| `verifyChecksum` | 读路径 | Release 默认关 | |

FUSE 侧运行时配置的段名是 `[storage_io.write]` / `[storage_io.read]`（`src/fuse/FuseConfig.h:68` 的 `CONFIG_OBJ(storage_io, ...)`），两段都有 `enableChecksum`，**改的时候必须指定段**。

**`chunk_checksum_type = 'NONE'` 会导致写全部失败**（源码核实）：

- Rust chunk_engine 设 `without_checksum=true` 后**仍在服务端重算 CRC32C**（`src/storage/store/ChunkEngine.cc:41`、`chunk_engine/src/core/engine.rs:300`）
- chunk meta 只有 `checksum: u32`，**没有类型字段**（`chunk_meta.rs:7`）
- C++ wrapper 写死 CRC32C（`ChunkEngine.h:21,65`）
- HEAD 会比对 forward 与 local checksum（`StorageOperator.cc:464` → `kChecksumMismatch`）
- 读路径 `ChunkReplica` 对 NONE 返回 `kChunkReadFailed`

**要跳过 checksum 只能改代码**，不是配置开关。

**性能异常好先怀疑正确性**：若某次改动后带宽跳涨数倍，先确认不是 checksum 被意外跳过。

### 3.5 `force_use_tcp` 与 NIC 选择

`force_use_tcp = false` 需在三处一致：`[client]`、`[storage.net_client]`、`[storage.net_client_for_updates]`。

**事故复盘（400 MB/s = 4% 线速）**：

| 项 | 值 |
|---|---|
| 症状 | 顺序写仅 ~400 MB/s |
| 一手证据 | meta 节点 RDMA 计数器错误率 **75.7%**（11196/14782）：`resp_cqe_error` 5412+5784、`resp_cqe_flush_error` 5280+5640 vs `rx_write_requests` 14782 |
| 二手证据 | 客户端 fuse-client-1 的 `mlx5_1` 处于 DOWN（`NO-CARRIER`），无法 ping 通 meta 的 RDMA 地址（100% 丢包）；服务端 RDMA 双归属（两个子网） |
| 根因 | fuse-client-1 的 `hf3fs_fuse_main.toml` 里 `[client] force_use_tcp = true` |
| 修复 | `sed -i 's/force_use_tcp = true/force_use_tcp = false/'`（3 处）→ `set-config --type FUSE` → ConfigVersion 4 → `systemctl restart hf3fs_fuse_main` |
| 验证 | 日志出现 `RandomFollowServerSelection choose [NodeId(100), meta, RDMA://<meta RDMA 地址>:8001]`；`rdma resource show qp` 出现新 RC QP（`lqpn 79 rqpn 210 type RC state RTS`） |
| 残留风险 | 只剩单条 RDMA 路径，"带宽减半且无冗余" |

**RDMA 计数器排查命令**：

```bash
rdma stat show      # rx_write_requests, resp_cqe_error, resp_cqe_flush_error, req_cqe_error, local_ack_timeout_err
rdma resource show qp
rdma link show
ibv_devinfo         # link_layer / active_mtu / fw_ver / vendor_part_id
```

**实测 fabric 是 InfiniBand 不是 RoCE**：`mlx5_0` `link_layer: InfiniBand`、`active_mtu: 4096 (5)`、`sm_lid 1`、fw `16.31.1014`、`vendor_part_id 4119`（ConnectX-5 MT27800）；netdev `ibp129s0f0 mtu 2044`。第二口 `mlx5_1` 是 `link_layer: Ethernet active_mtu 1024`，在客户端上 DOWN。

**注意 `Listener.cc checkNicType` 的设计**：即使 `network_type='RDMA'`，listener 仍绑 TCP 前缀网卡（`en*/eth*/bond*/xgbe*`），RDMA 握手走 TCP 再升级（`rdma_listen_ethernet=true`）。所以 `filter_list` 里出现以太网口名是正常的：storage 用 `['ibp129s0f0']`，mgmtd/meta 用 `['enp125s0f0']`。

`rdma_listen_ethernet` true→false→true 实测**无可测影响**。

`[ib_devices]` 相关项：`device_filter = ['mlx5_0']`、`prefer_ibdevice = true`、`skip_inactive_ports = true`、`skip_unusable_device = true`、`default_pkey_index = 0`、`default_roce_pkey_index = 0`、`default_traffic_class = 0`、`fork_safe = true`。**未做 GID index / MTU / mlx5 模块参数调优。**

### 3.6 线程池与队列（实测未调，供参考基线）

| 段 | 参数 | 值 |
|---|---|---|
| `[server.storage.write_worker]`（UpdateWorker） | `num_threads` / `bg_num_threads` / `queue_size` | 32 / 8 / 4096 |
| `[server.coroutines_pool_update]`（UpdatePool） | `coroutines_num` / `queue_size` / `enable_work_stealing` | 64 / 1024 / false |
| `[server.base.thread_pool]` | `num_io_threads` / `num_proc_threads` / `num_bg_threads` | 32 / 32 / 2 |
| `[server.aio_read_worker]` | `ioengine` / `enable_io_uring` / `num_threads` / `queue_size` / `max_events` / `min_complete` | libaio / false / 4 / 4096 / 512 / 128 |
| `[server.sync_worker]` | `num_threads` / `num_channels` / `batch_size` | 16 / 1024 / 16 |
| `[server.storage]` | `max_concurrent_rdma_reads` / `max_concurrent_rdma_writes` / `post_buffer_per_bytes` | 256 / 256 / 64KB |
| `[server.targets.storage_target]` | `force_persist` / `mutex_num` | true / 257 |
| `[server.targets.storage_target.meta_store]` | `allocate_size` / `punch_hole_batch_size` / `recycle_batch_size` | 256MB / 16 / 256 |
| FUSE | `iov_limit` / `rdma_buf_pool_size` / `max_background` / `io_jobq_size` / `batch_io_coros` | 8MB / 1024 / 32 / 1024 / 128 |
| `[common.monitor]` | `collect_period` / `num_collectors` | 1s / 1 |

**两个未执行的候选**：

- `meta_store.allocate_size` 256MB → 1~2GB，目标是削 `xfs_file_fallocate` 的 **15.4%** 热点
- 降低 `[common.monitor] collect_period`/`num_collectors`，parquet+monitor 占火焰图 **7.0%**

**`kv_store` 段的 `type = 'LevelDB'` 与实际不符**：火焰图显示的是 RocksDB 栈。原因是 Rust chunk_engine 的 meta store 无论配置如何都是 RocksDB（`/storage/dataN/3fs/engine/meta`），配置里的 `type` 只作用于旧 C++ KV 路径。**以 storage 节点实际 `/opt/3fs/etc/storage_main.toml` 为准，不要按配置字面值推断实现。**

### 3.7 RDMA/ibsocket 参数（未调，含一次早期故障的建议值）

`buf_size=16384`、`send_buf_cnt=32`、`max_rdma_wr=128`、`max_rdma_wr_per_post=32`、`max_sge=16`、`max_rd_atomic=16`、`retry_cnt=7`、`rnr_retry=0`、`timeout=14`、`min_rnr_timer=1`、`sl=0`、`event_ack_batch=128`、`buf_ack_batch=8`、`buf_signal_batch=8`、`drain_timeout='5s'`；`transport_pool.max_connections=1`；`processor.max_coroutines_num=256`、`max_processing_requests_num=4096`；`rdma_control.max_concurrent_transmission=64`（`enable_rdma_control=false`）。

早期集群建链失败（`IBSocket ... get failed WC {WRType::CHECK, error: transport retry counter exceeded, vendor_error: 129}`，连接在 accept 后 2ms 被关闭）曾建议上调 `retry_cnt`(7) / `rnr_retry`(0) / `timeout`(14)，**未实施**。

`num_event_loop` 与 `max_connections` 增大对 USRBIO 顺序写**无变化**。
