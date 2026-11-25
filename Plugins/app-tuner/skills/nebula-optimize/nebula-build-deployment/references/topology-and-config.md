# 拓扑、绑核、端口与配置模板

## 1. 选型

| 形态 | 进程 | 编译 | 适用 | 代价 |
|------|------|------|------|------|
| standalone | 1 | `ENABLE_STANDALONE_VERSION=ON` | 功能验证、单进程性能、快速迭代 | 单队列 futex 争用更明显；不能多实例分摊 |
| 1P 分布式 | 1 metad + N graphd + M storaged | `OFF` | 单机验证多进程架构、突破单进程队列瓶颈 | 端口/CPU/内存成组规划；RPC 与缓存副本增加 |
| 多机分布式 | 三类进程分机 | `OFF` | 生产规模、水平扩展、容灾 | 网络/时钟/副本/故障域复杂 |

经验：

- 只做正确性与代码验证：standalone。
- 要看 graphd/storaged 扩展性：1P 分布式（如 8 graphd + 4 storaged）。
- 数据量超过单机资源或要容灾：多机。

## 2. NUMA 与绑核规划

### 2.1 先看拓扑

```bash
lscpu | grep -E '^CPU\(s\)|NUMA|Socket|Core|Thread'
numactl -H | grep -E '^node [0-9]+ cpus|^node [0-9]+ size'
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
for d in /sys/block/nvme*n1; do
  echo "$d $(cat $d/device/numa_node 2>/dev/null)"
done
```

规划原则：

1. 进程与其数据盘尽量同 NUMA；至少内存绑定到本地节点（`numactl -m`）。
2. graphd 与 storaged 分域，避免同核竞争；k6/客户端放独立 NUMA。
3. 每台机预留系统/中断/管理核，不要把所有核都分给业务。
4. 多实例时，storaged 与 graphd 的核区间不要重叠（除非验证过浮动部署更优）。
5. 内存绑定必须与核绑定一致；跨 NUMA 访存会显著抬高延迟。

### 2.2 两种布局

**固定绑核（可预测）**

```
NUMA 0: graphd-1 0-31, storaged-1 32-63
NUMA 1: graphd-2 96-127, storaged-2 128-159
NUMA 2: k6 192-287
```

- 每个进程独占区间，cache 局部性好，单场景延迟低。
- 混合负载下容易出现“某角色核闲置、另一角色排队”，利用率下降。

**全浮动（弹性）**

```
Nebula 进程共享 0-191，k6 独占 192-383
numactl -C 0-191 -m <node> 每个进程
```

- 调度器按瓶颈动态分配核，混合负载利用率高（实测 93–98%）。
- 线程迁移损失 cache 局部性；单场景点查不如固定绑核。
- 需要更高 VU 才能打满，延迟通常高于固定绑核。

选择：负载类型固定且明确（纯点查/纯遍历）→ 固定绑核；混合负载/不确定 → 先试浮动。
任何绑核方案都要用同一压测口径对比，不能只看 CPU 利用率。

### 2.3 进程数与线程数

| 进程 | 线程参数 | 经验 |
|------|---------|------|
| graphd | `num_worker_threads` | 实例越多，单实例 worker 越少；轻查询高 QPS 时 16–24 常优于 48 |
| graphd | `num_netio_threads` | 8 左右通常足够；序列化热点时可试 16 |
| storaged | `num_worker_threads` | 读密集可增；写密集先看 WAL/compaction |
| storaged | `num_io_threads` | 8–16 |
| storaged | `reader_handlers` | 与读并发/核数匹配；过多会争抢 |
| metad | `meta_num_*` | 正常查询不经过 metad；压测时可最小化 |

## 3. 端口规划

每个 storaged 占连续 3 个端口：

```
port-1 : StorageAdminService（metad 管理）
port   : GraphStorageService（graphd 读写）
port+1 : RaftexService（副本复制/选举）
```

示例（4 storaged）：

| 实例 | admin | storage | raft | HTTP |
|------|-------|---------|------|------|
| storaged-1 | 9778 | 9779 | 9780 | 19779 |
| storaged-2 | 9781 | 9782 | 9783 | 19782 |
| storaged-3 | 9784 | 9785 | 9786 | 19785 |
| storaged-4 | 9787 | 9788 | 9789 | 19788 |

graphd 客户端口递增即可（9670、9671、…），metad 9559。graphd 的 HTTP 端口是
`ws_http_port`：3.8.0 默认 19669；**19670 是 3.x 已弃用的 HTTP/2 端口，不要当默认**。
多实例用各自 `${GRAPH_HTTP_PORT_N}`（示例 19669/19671/19672/19673）。所有端口要在
防火墙/安全组放通（至少 graphd 客户端口与 HTTP、storaged 三连与 HTTP、metad 端口、Raft 端口）。

## 4. 配置模板

以下为 1P 分布式的最小可用模板；按 `${VAR}` 替换。完整生产配置还需按安全/日志/监控要求补充。

### 4.1 metad

```text
--daemonize=true
--pid_file=${PID_DIR}/nebula-metad.pid
--log_dir=${LOG_DIR}
--minloglevel=1   # 部署/排障期保留 WARNING 及以上；压测阶段可切 3（3 会丢弃 ERROR/INFO）
--v=0
--logbufsecs=30
--redirect_stdout=true
--stdout_log_file=metad-stdout.log
--stderr_log_file=metad-stderr.log
--stderrthreshold=3
--timestamp_in_logfile_name=false

--meta_server_addrs=127.0.0.1:${META_PORT}
--local_ip=127.0.0.1
--port=${META_PORT}
--ws_ip=0.0.0.0
--ws_http_port=19559

--data_path=${DATA_ROOT}/meta
--default_parts_num=100
--default_replica_factor=1
--heartbeat_interval_secs=10
--agent_heartbeat_interval_secs=60
--meta_num_io_threads=4
--meta_num_worker_threads=4
```

> `meta_num_*` 在部分版本需要源码修复才生效；启动后用 `ps -o nlwp -p <pid>` 核对线程数。
> 分布式下 metad 用 `--ws_http_port=19559` 表达自身 HTTP 端口；`--ws_meta_http_port` 只在 standalone 构建中定义（MetaDaemonInit.cpp 的 BUILD_STANDALONE 分支）。上游 nebula-graphd.conf.default 里那行 `--ws_meta_http_port` 属模板遗留——graphd 二进制没有此 flag，gflags 对 flagfile 未知行静默忽略，勿复制进分布式 conf。

### 4.2 graphd（每个实例只改 port/pid/log 名）

```text
--daemonize=true
--pid_file=${PID_DIR}/nebula-graphd${N}.pid
--local_config=true
--log_dir=${LOG_DIR}
--minloglevel=1   # 部署/排障期保留 WARNING 及以上；压测阶段可切 3（3 会丢弃 ERROR/INFO）
--v=0
--logbufsecs=30
--redirect_stdout=true
--stdout_log_file=graphd${N}-stdout.log
--stderr_log_file=graphd${N}-stderr.log
--stderrthreshold=3
--timestamp_in_logfile_name=false

--enable_optimizer=false
--accept_partial_success=false
--max_allowed_query_size=4194304

--meta_server_addrs=127.0.0.1:${META_PORT}
--local_ip=127.0.0.1
# listen_netdev=any 会绑定所有网卡：客户端口可被其他主机访问时，必须开启认证
# 并用防火墙/安全组限制来源；仅网络隔离的本机环境才允许本条保持 any。
--listen_netdev=any
--port=${GRAPH_PORT_N}
--reuse_port=false
--listen_backlog=1024
--client_idle_timeout_secs=28800
--session_idle_timeout_secs=28800
--num_accept_threads=1
--num_netio_threads=8
--num_max_connections=0
--num_worker_threads=20
--ws_ip=0.0.0.0
--ws_http_port=${GRAPH_HTTP_PORT_N}
--storage_client_timeout_ms=60000
--slow_query_threshold_us=200000

# 网络可达部署必须开启认证（部署后先为默认账号设置强密码）；免认证
# （--enable_authorize=false）仅限网络隔离的本机压测环境。
--enable_authorize=true
--auth_type=password
--system_memory_high_watermark_ratio=0.8
--enable_space_level_metrics=false
--enable_experimental_feature=false
--enable_data_balance=true
--enable_udf=false
--max_sessions_per_ip_per_user=0
--max_job_size=1
--min_batch_size=8192
--optimize_appendvertices=false
--path_batch_size=10000
```

### 4.3 storaged（每个实例只改 port/pid/log/data_path）

```text
--daemonize=true
--pid_file=${PID_DIR}/nebula-storaged${N}.pid
--local_config=true
--log_dir=${LOG_DIR}
--minloglevel=1   # 部署/排障期保留 WARNING 及以上；压测阶段可切 3（3 会丢弃 ERROR/INFO）
--v=0
--logbufsecs=30
--redirect_stdout=true
--stdout_log_file=storaged${N}-stdout.log
--stderr_log_file=storaged${N}-stderr.log
--stderrthreshold=3
--timestamp_in_logfile_name=false

--meta_server_addrs=127.0.0.1:${META_PORT}
--local_ip=127.0.0.1
--port=${STORAGE_PORT_N}
--ws_ip=0.0.0.0
--ws_http_port=${STORAGE_HTTP_PORT_N}
--heartbeat_interval_secs=10

--raft_heartbeat_interval_secs=30
--raft_rpc_timeout_ms=500
--wal_ttl=60

--data_path=${DATA_ROOT}/storage${N}
--minimum_reserved_bytes=268435456
--rocksdb_batch_size=4096
--rocksdb_block_cache=32768
--engine_type=rocksdb
--rocksdb_compression=lz4
--rocksdb_compression_per_level=
--enable_rocksdb_statistics=false
--rocksdb_stats_level=kExceptHistogramOrTimers
--enable_rocksdb_prefix_filtering=true
--enable_rocksdb_whole_key_filtering=false
--rocksdb_db_options={}
--rocksdb_column_family_options={"write_buffer_size":"67108864","max_write_buffer_number":"4","max_bytes_for_level_base":"268435456"}
--rocksdb_block_based_table_options={"block_size":"8192"}
--query_concurrently=true
--auto_remove_invalid_space=true

--num_io_threads=8
--num_worker_threads=8
--reader_handlers=8
```

### 4.4 standalone

standalone 用一个配置文件承载三服务参数，关键项：

| 参数 | 用途 |
|------|------|
| `--port` | graphd 客户端端口（默认 9669） |
| `--meta_port` | metad 端口（默认 9559） |
| `--storage_port` | storaged 端口（默认 9779） |
| `--ws_http_port` / `--ws_meta_http_port` / `--ws_storage_http_port` | graphd/metad/storaged 的 HTTP 端口（默认 19669/19559/19779）；standalone 单进程内三服务仍各自消费这三个 flag——`--ws_meta_http_port` 恰恰只在 BUILD_STANDALONE 分支定义并消费（MetaDaemonInit.cpp），上游 conf/nebula-standalone.conf.default 亦带 `--ws_meta_http_port=19559` |
| `--data_path` / `--meta_data_path` | storage/meta 数据目录 |

其余参数多数同名可用。例外：storaged 的线程数与端口在 **standalone 构建下**不同名——线程数为 `--storage_num_worker_threads`、端口为 `--storage_port`（StorageServer.cpp 的 BUILD_STANDALONE 分支）；standalone 下 `--num_worker_threads` 只被 graphd 与 ThreadManager 消费，调 storaged 线程数别调错对象。

## 5. 配置参数的取舍

| 目标 | 先动什么 | 不要先动什么 |
|------|---------|-------------|
| 轻查询 QPS | 增加 graphd 实例、降低单实例 worker | 盲目加 worker、加 netio |
| 多跳查询 | 增加 storaged/reader、减少 RPC 扇出 | 只加 graphd worker |
| 写入吞吐 | WAL/compaction/磁盘、批量写入 | 只加 worker |
| 延迟长尾 | compaction、快照、Raft、NUMA | 只调 block cache |
| 内存 | block cache、write buffer、水位、实例数 | 无脑开大页 |
| 元数据变更慢 | heartbeat、metad 负载、graphd 实例数 | 关闭 MetaClient 缓存 |

参数细节与实测影响见 `nebula-performance-optimization/references/config-tuning.md`。

## 6. 多机分布式补充

- 所有节点时间同步（chrony/NTP）；时钟漂移会导致心跳、Raft、证书/认证异常。
- `meta_server_addrs` 填全部 metad 地址；storage 的 `local_ip` 用可被其他节点访问的地址。
- 副本数 >1 时，确保故障域（机架/交换机）分散；同机多副本没有容灾意义。
- 跨机 RPC 对网络延迟敏感；深遍历/大结果场景优先靠近数据部署 graphd。
- 防火墙放通：graphd 客户端口、storaged 三连、metad 端口、Raft 端口、HTTP 监控端口。

## 7. 目录与磁盘

```
${DATA_ROOT}/
├── meta/                  # metad RocksDB
├── storage1/              # storaged-1（每 Space 一个子目录）
├── storage2/
├── storage3/
├── storage4/
└── logs/ pids/ backup/
```

- 每个 storaged 的 `data_path` 独立；多盘用逗号分隔。
- 数据盘与日志盘尽量分开；日志 I/O 会干扰 RocksDB。
- 备份目录单独放盘，避免与在线数据争 I/O。
- 确保 `minimum_reserved_bytes` 留足空间，磁盘满会导致写入失败/只读。
