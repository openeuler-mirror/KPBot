# 安装、初始化与基线配置

## 1. 用户与目录

```
useradd -r -s /sbin/nologin "${MYSQL_USER}" 2>/dev/null || true
mkdir -p "${DATA_ROOT}"/{data,log,tmp,run}
chown -R "${MYSQL_USER}":"${MYSQL_USER}" "${DATA_ROOT}"
chmod 750 "${DATA_ROOT}"
```

- datadir 必须是本地文件系统（推荐 XFS/ext4 + NVMe），不要放网络盘。
- tmpdir 与 datadir 同盘时，大排序/临时表会与数据竞争 IO；有条件分盘。
- socket 目录需可写；systemd 下可用 `/run/mysqld`。

## 2. 先配置、核验，再初始化

初始化会读取 `--defaults-file`，必须先生成并核验本实例的 my.cnf，不能先初始化后补配置。

```
# 1) 按 §4 模板渲染本实例 ${MYSQL_CONF}（basedir/datadir/socket/port/innodb_page_size）
# 2) 核验 --defaults-file 实际生效的路径、目标目录与页大小：
"${MYSQL_HOME}/bin/mysqld" --defaults-file="${MYSQL_CONF}" --verbose --help 2>/dev/null \
  | grep -Ei 'datadir|socket|innodb[-_]page[-_]size'
# 输出必须与 ${DATA_ROOT}/data、${SOCKET}、目标页大小一致；datadir 必须为空：
[ -z "$(ls -A "${DATA_ROOT}/data" 2>/dev/null)" ] || { echo "datadir 非空，拒绝初始化" >&2; exit 1; }

# 3) 再执行初始化
sudo -u "${MYSQL_USER}" "${MYSQL_HOME}/bin/mysqld" \
  --defaults-file="${MYSQL_CONF}" --initialize-insecure
```

- 新机器上若未先生成 my.cnf，`--initialize` 会按默认路径建库；路径上有旧配置还可能选错 datadir。
- `--initialize-insecure` 生成空 root 密码，首次登录后立即设置强密码。
- `--initialize` 生成随机临时密码，写在错误日志；适合更严格场景。
- `innodb_page_size` 仅初始化可设：初始化按 my.cnf 的值建库并写入表空间，之后不能修改；变更页大小必须新建实例并迁移数据。
- 初始化前确认 datadir 为空；非空会失败（保护数据）。

## 3. 账号与权限

先创建隔离压测库，只授予该库实际需要的最小权限，并把账号来源限定为已授权的压测客户端：

```
ALTER USER 'root'@'localhost' IDENTIFIED BY '<强密码>';
CREATE DATABASE IF NOT EXISTS bench_db DEFAULT CHARACTER SET utf8mb4;
CREATE USER 'bench'@'bench-client.example.net' IDENTIFIED BY '<强密码>';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON bench_db.* TO 'bench'@'bench-client.example.net';
FLUSH PRIVILEGES;
```

- `bench-client.example.net` 是文档示例主机名；实际部署替换为已授权的压测客户端主机名/IP（多台逐台授权），禁止 `'%'` 任意来源或不加约束的网段。
- 权限只给隔离库 `bench_db`，且只含 sysbench prepare/run/cleanup 实际需要的操作（读、写、建/删表、索引）；禁止 `GRANT ALL ON *.*`，也不要在生产复用该账号。
- 8.0 默认 `caching_sha2_password`；老客户端可显式使用
`mysql_native_password`（需评估安全）。
- 禁止把密码写入 shell 历史/脚本/报告；用 `--defaults-extra-file` 或交互输入。

## 4. 基线 my.cnf（8U32G，64t 量级）

```
[mysqld]
basedir=${MYSQL_HOME}
datadir=${DATA_ROOT}/data
socket=${SOCKET}
pid-file=${DATA_ROOT}/run/mysql.pid
log-error=${DATA_ROOT}/log/mysql.log
tmpdir=${DATA_ROOT}/tmp
port=${MYSQL_PORT}
user=${MYSQL_USER}
character_set_server=utf8mb4
default_time_zone=+8:00
skip_name_resolve=ON

max_connections=2000
back_log=4000
thread_cache_size=2000
max_prepared_stmt_count=1280000
performance_schema=OFF

innodb_file_per_table=ON
innodb_buffer_pool_size=22G
innodb_buffer_pool_instances=16
innodb_log_file_size=2048M
innodb_log_files_in_group=8
innodb_log_buffer_size=2G
innodb_open_files=10000
table_open_cache=30000
table_open_cache_instances=16

innodb_flush_method=O_DIRECT
innodb_flush_log_at_trx_commit=1
sync_binlog=1
innodb_use_native_aio=1
innodb_io_capacity=30000
innodb_io_capacity_max=40000
innodb_lru_scan_depth=9000
innodb_page_cleaners=16
innodb_flush_neighbors=0
innodb_spin_wait_delay=3
innodb_sync_spin_loops=200
innodb_spin_wait_pause_multiplier=5
innodb_adaptive_hash_index=OFF

innodb_write_io_threads=1
innodb_read_io_threads=1
innodb_purge_threads=1

log-bin=mysql-bin
ssl=0
sql_mode=STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO,STRICT_ALL_TABLES
```

说明：

- `innodb_page_size` 只能写在初始化用的 my.cnf 中（仅初始化可设）；已有 datadir 不可更改，变更须新建实例并迁移数据，不列为重启生效项。
- 这是特定 8U32G/64t 实测基线，不是通用默认值；其他规格按内存/核数/场景重算。
- 只读压测常用低 `write_io_threads`/`purge_threads`；读写/只写需提高（见 app-config skill）。
- 生产必须按 durability 目标设置 `flush_log_at_trx_commit`/`sync_binlog`，不能照搬压测。
- `ssl=0` 仅限隔离压测环境；生产启用 TLS。

## 5. systemd 托管（可选）

```
[Unit]
Description=MySQL Server
After=network.target

[Service]
User=${MYSQL_USER}
Group=${MYSQL_USER}
ExecStart=${MYSQL_HOME}/bin/mysqld --defaults-file=${MYSQL_CONF}
Restart=on-failure
LimitNOFILE=1000000
LimitNPROC=65535
PrivateTmp=false

[Install]
WantedBy=multi-user.target
```

- `LimitNOFILE` 必须 ≥ `open_files_limit`，否则启动告警并截断。
- systemd 下 `mysqld_safe` 不是必需；直接跑 `mysqld` 更易观测退出码。
- 容器/虚机内若用 `--privileged`，仍建议显式设置 nofile。

## 6. 字符集、时区与排序

- 8.0 默认 `utf8mb4` + `utf8mb4_0900_ai_ci`；压测基线常用 `utf8` 以贴近旧应用。
- 字符集/collation 影响索引长度、排序键生成与比较成本；变更需评估兼容性。
- `default_time_zone` 影响时间函数与复制；集群内保持一致。
- `lower_case_table_names` 只能在初始化时确定，之后不可改。

## 7. 初始化后验收

```
SELECT VERSION();
SHOW VARIABLES LIKE 'datadir';
SHOW VARIABLES LIKE 'innodb_page_size';
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'character_set_server';
SHOW DATABASES;
SELECT @@sql_mode;
```

并检查错误日志无 `[ERROR]`、无权限/大页/ NUMA 告警。

## 8. 重新初始化（危险）

仅在数据可丢或已备份时执行：

```
systemctl stop mysqld  # 或 kill 主进程
mv "${DATA_ROOT}/data" "${DATA_ROOT}/data.bak.$(date +%s)"
mkdir -p "${DATA_ROOT}/data" && chown "${MYSQL_USER}":"${MYSQL_USER}" "${DATA_ROOT}/data"
sudo -u "${MYSQL_USER}" "${MYSQL_HOME}/bin/mysqld" --defaults-file="${MYSQL_CONF}" --initialize-insecure
```

必须在报告中说明“数据已丢弃/已备份”，不得静默执行。
