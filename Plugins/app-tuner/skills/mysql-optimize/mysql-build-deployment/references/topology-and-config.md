# 规格、拓扑与配置模板

>
> 数值来自鲲鹏 920B/920G 实测（MySQL 8.0.25/8.0.28、openEuler 22.03/24.03）。不同机型、
> 内存、盘、并发会改变最优值；模板是起点，不是承诺。

## 1. 规格与资源分配

| 规格 | CPU | 内存 | buffer pool | 适用 |
| --- | --- | --- | --- | --- |
| 2C8G（容器） | 2 | 8 GiB | 4 GiB | 功能/小规格 POC |
| 8C16G（容器） | 8 | 16 GiB | 8 GiB | 小规格基线 |
| 8U32G | 8 | 32 GiB | 22 GiB | 容器/虚机 POC 常用 |
| 32C256G | 32 | 256 GiB | 128–137 GiB | 中规格容器 |
| 整机 1P | 74 worker + IRQ + log | 512–768 GiB | ≈70%（512 GiB 约 358 GiB，768 GiB 约 538 GiB） | 物理机性能验收 |

原则：

- buffer pool 给内存的 50%–70%，其余留给连接、排序、临时表、binlog cache、OS。
- 小规格优先保证“业务核 + 1 个 IRQ 核”；大规格再做 IRQ/log 独立核。
- 容器/虚机的“核数”是 cpuset/vCPU，host 仍需规划 IRQ 与 NUMA。

## 2. NUMA、IRQ 与绑核前置

```
lscpu -p=CPU,NODE | grep -v '^#'
cat /sys/class/net/${ETH}/device/numa_node 2>/dev/null
ethtool -i ${ETH} | grep bus-info
cat /proc/interrupts | grep '<bus-info>'
```

- 网卡所在 NUMA 决定业务核与 IRQ 核的候选集合；`numa_node=-1` 时按全机核池，
但不得默认回落 node 0。
- IRQ 与业务分离：只读/Point 至少 1 个 IRQ 核；读写/只写再预留 2 个 log 核。
- 绑核表（80C/160T 实测机型，920B；前两行是整机逻辑 CPU 编号，后两行是容器 cpuset
  映射到的宿主编号）：

| 总核 | worker 核 | 网卡 IRQ | log 线程 | mysqld 绑核 | 并发 |
| --- | --- | --- | --- | --- | --- |
| 80C-160T（只读） | 74 | 80–85 | — | 86–159 | 592 |
| 80C-160T（读写） | 74 | 82–85 | 80,81 | 86–159 | 按 1:6~1:7 |
| 2C-4T（只读，容器） | 4 | 80 | — | 80–83 | 24 |
| 2C-4T（读写，容器） | 4 | 80 | — | 80–83 | 不单独绑 log |

前两行的 80–159 是 80C/160T 整机编号（上限 159），不是 40C/80T；后两行的 80–83 是
2C-4T 容器 cpuset 映射到的宿主编号，不是容器内 0–3，也不能套到只有 4 个逻辑 CPU 的机器。
小规格首核兼 IRQ（与 80–83 重叠是有意的），并非误配。核少时不要把 IRQ/log 完全独立出去，
否则业务核不足；用 A/B 决定。

## 3. 端口与目录

| 项 | 建议 |
| --- | --- |
| TCP | `${MYSQL_PORT}=3306`；多实例递增 |
| mysqlx | 33060；不用则 `WITH_MYSQLX=OFF`/`mysqlx=0` |
| socket | `${DATA_ROOT}/mysql.sock`；多实例独立 |
| datadir | `${DATA_ROOT}/data`；多实例独立 |
| log-error | `${DATA_ROOT}/log/mysql.log` |
| tmpdir | `${DATA_ROOT}/tmp`；大排序可独立盘 |
| binlog | `log-bin=mysql-bin`；注意磁盘容量与 rotate |

多实例必须同时隔离 port、socket、datadir、pid、log、server-id。

## 4. 8U32G / 20t 与 64t 模板差异

| 参数 | 20t×100w | 64t×1000w | 说明 |
| --- | --- | --- | --- |
| `max_connections` | 2000 | 2000 | 按并发 |
| `max_prepared_stmt_count` | 1280000 | 1280000 | PS 并发×语句 |
| `table_open_cache` | 30000 | 30000 | 表多可增 |
| `innodb_buffer_pool_size` | 22G | 22G | 8U32G 基线 |
| `innodb_log_file_size` | 2048M | 2048M | 写多可增 |
| `innodb_log_files_in_group` | 8 | 8 | 8.0.30 前 |
| `innodb_io_capacity` | 30000 | 30000 | NVMe |
| `innodb_page_cleaners` | 16 | 16 | 与实例/盘 |
| 数据集 | 20 表 | 64 表 | sysbench prepare |

两套模板除 datadir/库名外基本一致；差异主要由数据量/工作集决定，不要只看表数。

## 5. 容器规格模板（2C8G / 8C16G / 32C256G）

关键差异（来自容器基线）：

| 参数 | 2C8G | 8C16G | 32C256G |
| --- | --- | --- | --- |
| `innodb_buffer_pool_size` | 4G | 8G | 128G |
| `innodb_buffer_pool_instances` | 4 | 8 | 8 |
| `innodb_log_file_size` | 1G | 2G | 4G |
| `innodb_page_cleaners` | 4 | 8 | 8 |
| `innodb_read_io_threads` | 1 | 4 | 16 |
| `innodb_write_io_threads` | 1 | 4 | 16 |
| `innodb_purge_threads` | 1 | 4 | 16 |
| `max_connections` | 1200 | 4000 | 48000 |
| `table_open_cache` | 1024 | 2048 | 2048 |
| `table_open_cache_instances` | 4 | 16 | 16 |
| `thread_pool_size`（Percona） | 2 | 8 | 32 |

容器模板还常包含：`transaction_isolation=READ-COMMITTED`、`binlog_format=ROW`、
`sync_binlog=1`、`innodb_flush_log_at_trx_commit=1`、`innodb_doublewrite=1`、
`skip_name_resolve=ON`、`lower_case_table_names=1`。按业务兼容性取舍。

## 6. 页大小、文件系统与大页

- OpenEuler 22.03 基线可用 64K 页内核；4K 页需重编内核（`Kernel Features -> Page Size`）。
- 64K 页 + 未部署虚机：通常关闭 THP、`nr_hugepages=0`。
- 部署虚机：按虚机内存增加标准大页，并按 NUMA 分配。
- 文件系统：XFS 或 ext4；NVMe 原子写场景需 ext4 `bigalloc -C 16384` 且确认 SSD 支持。
- HDD 才调 `scheduler=deadline`、`nr_requests=2048`；NVMe 通常不需要。

## 7. 配置落地检查

```
SHOW VARIABLES WHERE Variable_name IN (
  'innodb_buffer_pool_size','innodb_buffer_pool_instances',
  'innodb_log_file_size','innodb_log_buffer_size',
  'innodb_flush_method','innodb_flush_log_at_trx_commit','sync_binlog',
  'innodb_io_capacity','innodb_page_cleaners','innodb_adaptive_hash_index');
```

- 配置生效但值不同：检查是否有 `!include`、命令行覆盖、`--defaults-file` 路径错误。
- 需重启的参数（log file、部分 instances）必须重启后复核；`innodb_page_size` 是仅初始化可设参数，不在此列——已有 datadir 修改后重启会因页大小不匹配无法启动，变更须新建实例并迁移数据。
- 任何模板落地前先备份原配置，并记录改前/改后与回滚命令。
