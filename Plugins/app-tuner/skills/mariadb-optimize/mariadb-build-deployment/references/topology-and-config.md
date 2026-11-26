# 拓扑、NUMA/大页与配置模板

本文件覆盖部署形态、NUMA 与绑核、大页、端口/目录规划，以及一份可用的 my.cnf 模板。
示例值必须按目标硬件重算，不要直接抄。

## 1. 形态与资源规划

| 形态 | CPU | 内存 | 磁盘 | 网络 |
| --- | --- | --- | --- | --- |
| 单实例 | 整机或单 NUMA 的连续核 | buffer pool 50%–70% | 数据/redo/binlog 可分盘 | 业务口 + 管理口 |
| 多实例 N 个 | 每实例一段连续核，不跨 NUMA | 各自 buffer pool，总和不超物理内存 | 每实例独立 datadir | 每实例独立端口 |
| 主从 | 主从同规格或从库略低 | 同左 | 从库独立盘 | 复制链路独立带宽 |
| Galera | 同规格 | 同左 | 同左 | 低延迟、低丢包 |

原则：

- **不跨 NUMA**：单实例尽量落在一个 NUMA 内；跨 NUMA 会引入远端内存访问。
- **不超卖内存**：所有实例 buffer pool + 连接内存 + 临时表 + OS 预留 ≤ 物理内存。
- **IO 分盘**：redo 与数据分盘可降低写竞争；binlog 单独盘有利于组提交与备份。

## 2. NUMA 与绑核

```
# 查看拓扑
lscpu | grep -E 'NUMA|Socket|Core|Thread'
numactl --hardware

# 单实例绑核 + 本 NUMA 内存
numactl --cpunodebind=0 --membind=0 -- "${MARIADB_HOME}/bin/mariadbd" --defaults-file=/etc/my.cnf

# 或 taskset（只绑 CPU，不绑内存）
taskset -c 0-63 -- "${MARIADB_HOME}/bin/mariadbd" --defaults-file=/etc/my.cnf
```

建议：

- 鲲鹏 4P / 多 Die 机型：BIOS 设为“每个 Die 一个 NUMA Node”，避免 NUMA 距离异常。
- 数据库线程应落在同一 NUMA；网卡中断绑到同一 NUMA 的核，减少跨片访问。
- 虚拟化场景：虚拟机 `cpuset` 必须落在同一 NUMA，`nodeset` 绑定对应节点；
跨 NUMA 分配会导致性能大幅下降（案例实测结论）。
- 混合负载可考虑“浮动 + 多实例”而不是单一静态绑核，避免某类负载空闲时资源浪费。

## 3. 大页

```
# 预留 2MB 大页（按 NUMA 节点）
echo 8192 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
# 预留 1GB 大页
echo 64 > /sys/devices/system/node/node0/hugepages/hugepages-1048576kB/nr_hugepages

# 查看
grep -H . /proc/sys/vm/nr_hugepages
cat /sys/devices/system/node/node*/hugepages/hugepages-*/nr_hugepages
```

my.cnf：

```
[mysqld]
large_pages = ON
# 或（部分版本/发行版）用
# large-pages
```

注意：

- 大页数量 = 预期内存 / 页大小；预留前 `echo 3 > /proc/sys/vm/drop_caches` 提高成功率。
- 大页不足时启动失败或回退；不要同时依赖 THP（建议关闭 THP，使用显式大页）。
- 虚拟化场景由宿主机分配大页给 guest，guest 内再启用大页，两层都要核对。

## 4. 端口与目录（多实例）

```
# /etc/my.cnf.d/mdb1.cnf
[mysqld]
port                    = 3306
socket                  = /data/mariadb/mdb1/run/mysql.sock
pid-file                = /data/mariadb/mdb1/run/mysqld.pid
datadir                 = /data/mariadb/mdb1/data
log-error               = /data/mariadb/mdb1/log/error.log
tmpdir                  = /data/mariadb/mdb1/tmp
# 第二个实例：3307 / mdb2 / 独立目录
```

systemd 模板实例（unit 须先按 `SKILL.md` §初始化与启动 创建；启停走同一服务控制变量）：

```
INSTANCE="${INSTANCE:-mdb1}"
SERVICE_NAME="${SERVICE_NAME:-mariadb@${INSTANCE}}"
svc_start                                  # unit 不存在时回退 mysqld_safe
"${SERVICE_CTL:-systemctl}" status "${SERVICE_NAME}"
```

要求：

- `port`、`socket`、`pid-file`、`datadir`、`log-error`、`tmpdir` 六者每实例唯一。
- `extra_port` 用于连接打满时救援，生产建议开启并只监听管理网。
- Galera 端口（4567/4568/4444）与多实例端口段整体规划，避免冲突。

## 5. my.cnf 模板（OLTP 起点）

```
[mysqld]
# --- 身份与路径 ---
user                    = mysql
basedir                 = ${MARIADB_HOME}
datadir                 = ${DATADIR}
socket                  = ${SOCKET}
port                    = ${PORT}
pid-file                = ${PID_DIR}/mysqld.pid
log-error               = ${LOG_DIR}/error.log
tmpdir                  = ${TMPDIR}

# --- 连接 ---
max_connections         = 2000
thread_cache_size       = 256
table_open_cache        = 8000
table_definition_cache  = 4000
# thread_handling       = pool-of-threads
# thread_pool_size      = 64

# --- InnoDB 内存 ---
innodb_buffer_pool_size = ${BP_SIZE}
innodb_log_file_size    = 4G
innodb_log_buffer_size  = 64M
innodb_flush_method     = O_DIRECT
innodb_flush_log_at_trx_commit = 1
innodb_doublewrite      = ON
innodb_io_capacity      = 4000
innodb_io_capacity_max  = 8000
innodb_read_io_threads  = 8
innodb_write_io_threads = 8
innodb_purge_threads    = 4
innodb_use_native_aio   = ON
# innodb_numa_interleave = ON

# --- 日志与复制 ---
sync_binlog             = 1
binlog_format           = ROW
max_binlog_size         = 1G
log_bin                 = ${LOG_DIR}/binlog
server_id               = 1
# gtid_strict_mode      = ON
# binlog_commit_wait_count = 32
# binlog_commit_wait_usec  = 1000

# --- 优化器与监控 ---
optimizer_switch        = 'index_merge=on,index_merge_union=on,index_merge_sort_union=on,index_merge_intersection=on,engine_condition_pushdown=on'
slow_query_log          = ON
long_query_time         = 1
# performance_schema    = ON

# --- Aria ---
aria_pagecache_buffer_size = 512M
aria_log_file_size      = 256M

# --- 大页 ---
large_pages             = ON

[client]
socket                  = ${SOCKET}
```

说明：

- 10.11 已移除 `innodb_buffer_pool_instances` 与 `innodb_page_cleaners`，模板中不得出现，
  否则启动即报 unknown variable；buffer pool 内部自行分片、page cleaner 线程数由版本管理。
- `${BP_SIZE}`、${PORT} 等需替换为实际值；模板不是“最优”，是“可启动且方向正确”。
- `binlog_format=ROW` 更适合一致性复制与 CDC；语句复制体积小但语义风险高。
- `performance_schema` 默认关闭；需要细粒度等待分析时开启，并在 A/B 两腿保持一致。
- 写密集先评估 redo 容量与 `innodb_io_capacity`，不要只加大 buffer pool。

## 6. 主从与 Galera 要点

主从：

```
-- 主库
CREATE USER 'repl'@'%' IDENTIFIED BY '<从环境变量注入>';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
-- 记录 GTID/位点后备份
```

```
-- 从库
SET GLOBAL gtid_slave_pos = '<位点>';
CHANGE MASTER TO
  MASTER_HOST='<主库地址>', MASTER_PORT=${PORT},
  MASTER_USER='repl', MASTER_PASSWORD='<由管理员交互注入，勿写入文档/配置>',
  MASTER_USE_GTID=slave_pos;
START SLAVE;
```

> 复制口令不得写进文档样例或 argv：经交互/0600 配置文件注入，样例中的尖括号只是占位。

Galera：

- 三节点起步，`wsrep_cluster_address=gcomm://<node1>,<node2>,<node3>`。
- 首次引导使用 `wsrep_new_cluster`（或 `galera_new_cluster`），其余节点正常加入。
- SST 方法（`rsync`/`mariabackup`）影响加入耗时与锁；大库优先 `mariabackup`。
- 写集认证冲突会回滚事务，业务需处理 `DEADLOCK`/`WSREP` 错误。

## 7. 校验命令

```
ss -lntp | grep -E ':3306|:3307|mariadbd'
mariadb --socket="${SOCKET}" -e "SHOW VARIABLES LIKE 'innodb_buffer_pool_size';"
numastat -p "$(pgrep -x mariadbd | head -1)"
grep -E 'HugePages|AnonHugePages' /proc/meminfo
```

部署完成后把实际端口、目录、绑核、参数快照保存到部署记录，供热点与优化 skill 复用。
