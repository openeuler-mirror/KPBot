# 数据装载、对账与备份恢复

本文件覆盖 sysbench 与 TPC-C 的装载/重置、导入后的对账，以及 mariabackup 的备份、
恢复与校验。命令参数化，凭据不落 argv、不进进程环境。

## 0. 凭据契约（先执行；本节与 §1–§6 的代码块须在同一 shell 中按顺序执行）

```bash
# 单一 EXIT trap 覆盖全部临时凭据文件；后续章节不得再 mktemp 或重定义 EXIT trap 覆盖它。
umask 077
PWFILE="$(mktemp)"          # 0600 口令源
CLIENT_CNF="$(mktemp)"      # 0600 mariadb / mariadb-admin 的 --defaults-extra-file
SYSBENCH_CONF="$(mktemp)"   # 0600 sysbench 的 --config-file
trap 'rm -f "${PWFILE}" "${CLIENT_CNF}" "${SYSBENCH_CONF}"' EXIT
printf '%s\n' "${DB_PASSWORD}" > "${PWFILE}"
printf '[client]\nuser=%s\npassword=%s\n' "${DB_USER:-root}" "${DB_PASSWORD}" > "${CLIENT_CNF}"
```

结束时做存在性校验（清理生效才继续）：`test ! -e "${PWFILE}" && test ! -e "${CLIENT_CNF}" && test ! -e "${SYSBENCH_CONF}" && echo cleaned`。
口令只从 `${DB_PASSWORD}` 注入；`--defaults-extra-file` 必须是 mariadb CLI 的第一个选项。
禁止 `MYSQL_PWD`：环境变量可经 `/proc/<pid>/environ`（同 UID 进程/root）与 `ps e` 读取，
会被后台子进程继承，令暴露窗口持续整轮建索引。

## 1. sysbench 装载

```
# 依赖：sysbench（版本要与 lua 路径匹配）。sysbench 不读环境变量口令，口令统一经 §0 的
# --config-file 注入，不进 argv；此处只引用 §0 已创建的 ${SYSBENCH_CONF}/${PWFILE}，
# 不得重复 mktemp 或重定义 EXIT trap（否则会覆盖 §0 的清理承诺）。
sysbench --version

# 本轮专用库名（含实例名与轮次）：多实例下不指向默认实例的 bench
BENCH_DB="${BENCH_DB:-bench_${INSTANCE}_$(date +%Y%m%d_%H%M%S)}"

# sysbench --config-file：key=value、无 [section]；0600 临时文件，trap 清理，口令不进 argv
printf 'mysql-password=%s\n' "$(cat "${PWFILE}")" > "${SYSBENCH_CONF}"

# 准备 10 张表 × 1000 万行
sysbench --config-file="${SYSBENCH_CONF}" /usr/share/sysbench/oltp_common.lua \
  --mysql-host=${DB_HOST} --mysql-port=${PORT} \
  --mysql-user=bench \
  --mysql-db=${BENCH_DB} --tables=10 --table-size=10000000 \
  --db-driver=mysql prepare

# 预热（可选）
sysbench --config-file="${SYSBENCH_CONF}" /usr/share/sysbench/oltp_read_only.lua \
  --mysql-host=${DB_HOST} --mysql-port=${PORT} \
  --mysql-user=bench \
  --mysql-db=${BENCH_DB} --tables=10 --table-size=10000000 \
  --time=300 --threads=64 --db-driver=mysql run
```

注意：

- `prepare` 耗时与磁盘/redo 强相关；记录耗时、数据目录大小、redo 大小。
- 每次性能对比前重置到同一快照（重新 prepare 或恢复备份），否则数据分布变化会让
“后测更高”。
- 两例 sysbench 用 `--config-file="${SYSBENCH_CONF}"` 注入口令（不进 argv）；`PWFILE` /
  `SYSBENCH_CONF` / `CLIENT_CNF` 均由 §0 的 umask 077 + mktemp 生成，由**一条** EXIT trap
  统一清理；口令不写进脚本。禁止 `MYSQL_PWD=...`（sysbench 不读；CLI 会泄漏到进程环境）。
- 若该 sysbench 构建不支持 `--config-file`（`sysbench --help | grep config-file` 无输出）：
  改用专用低权限压测用户 + `--mysql-password`（会出现在 `ps` argv，须限定权限并在压测后
  轮换口令），或改用 unix_socket 认证；禁止把口令硬编码进脚本/仓库。mariadb CLI 仍用
  `--defaults-extra-file="${CLIENT_CNF}"`，不使用 `MYSQL_PWD`。
- `--mysql-host` 必须是被测库主机 `${DB_HOST}`，与 §2 的 `mariadb -h "${DB_HOST}"`
  一致；`${CLIENT_HOST}` 只是跑 sysbench 进程的压测机，压测机与数据库分离时不能用它，
  否则 prepare/预热会连到本地另一套库。`--mysql-db` 用本轮专用 `${BENCH_DB}`。

## 2. TPC-C（TPCCRunner）装载

TPC-C 的 9 张表与 5 类事务（New-Order/Payment/Delivery/Order-Status/Stock-Level）
是 OLTP 综合负载的常用口径。<LAB> 定制版流程：

```
# 1) 建库/建表（压力机执行，-h 指向被测库；口令走 0600 defaults-file，不进 argv/进程环境）
mariadb --defaults-extra-file="${CLIENT_CNF}" -h "${DB_HOST}" -P "${PORT}" -u "${LOAD_USER:-root}" -n \
  < sql/example/mysql/create_database.sql
mariadb --defaults-extra-file="${CLIENT_CNF}" -h "${DB_HOST}" -P "${PORT}" -u "${LOAD_USER:-root}" -n \
  < sql/example/mysql/create_table.sql

# 2) 导入（修改 loader.properties 的 url/user/password）
java -cp bin/TPCC_<LAB>_v2.jar:lib/mysql-connector-java-5.1.7-bin.jar \
  iomark.TPCCRunner.Loader conf/example/mysql/loader.properties

# 3) 建索引（大表建索引耗时长，后台执行并记录；后台任务同样不得用 env 注入口令）
nohup mariadb --defaults-extra-file="${CLIENT_CNF}" -h "${DB_HOST}" -P "${PORT}" -u "${LOAD_USER:-root}" -n \
  < sql/example/mysql/create_index.sql > index.log 2>&1 &

# 4) 运行（master 设定 warmup/run、事务占比、思考时间）
java -cp bin/TPCC_<LAB>_v2.jar:lib/mysql-connector-java-5.1.7-bin.jar \
  iomark.TPCCRunner.Master conf/example/mysql/master.properties
java -cp bin/TPCC_<LAB>_v2.jar:lib/mysql-connector-java-5.1.7-bin.jar \
  iomark.TPCCRunner.Slave conf/example/mysql/slave1.properties
```

> 远程装载不用 root：先创建仅授予本轮库所需权限的专用账号 `${LOAD_USER}`，建库/建索引
> 所需 DDL 权限按需单独授予并记录授权范围；`-u "${LOAD_USER:-root}"` 的 root 回退只用于
> 本机初始化场景，不作为远程操作的推荐做法。

口径纪律（来自测试方案，禁止修改）：

| 项 | 值 |
| --- | --- |
| newOrderPercent | 1 |
| paymentPercent | 1 |
| orderStatusPercent | 45 |
| deliveryPercent | 43 |
| stockLevelPercent | 10 |
| 思考时间（五类） | 0 |
| warmup / run | 10 min / 40 min（可按需缩短但两腿一致） |

> 上表是家族测试方案的偏斜口径（Order-Status/Delivery 为主，New-Order/Payment 各 1%），
> 不是 TPC-C 标准规格的 45/43/4/4/4；字段名与数值按测试方案原样保留，两腿必须一致，
> 不要把两种口径混比。

Slave 规划：

- 多个 slave 使用**不同 warehouse 区间**（`startWarehouseID`/`warehouseCount`），
降低同一数据行的热点冲突。
- `userCount ≤ poolSize`；`poolSize` 是连接池，`userCount` 是并发用户。
- 记录 slave 数（1/2/4）、poolSize/userCount（如 60/50、120/100、180/150）。

## 3. 导入后对账

```
-- 表数量/行数抽样（以 TPC-C 为例）
SELECT COUNT(*) FROM warehouse;
SELECT COUNT(*) FROM district;
SELECT COUNT(*) FROM customer;
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM new_orders;
SELECT COUNT(*) FROM order_line;
SELECT COUNT(*) FROM stock;
SELECT COUNT(*) FROM item;
SELECT COUNT(*) FROM history;

-- 抽样属性
SELECT * FROM warehouse ORDER BY w_id LIMIT 1;
SELECT * FROM customer ORDER BY c_w_id, c_d_id, c_id LIMIT 5;

-- 索引是否存在且被使用
SHOW INDEX FROM orders;
EXPLAIN SELECT * FROM orders WHERE o_w_id=1 AND o_d_id=1 AND o_id=1;
```

对账要求：

- 行数与装载工具日志/期望值一致；不一致先查失败样本，不要先看性能。
- 抽样检查数据类型、日期、字符串长度、NULL 比例。
- 索引数、主键、唯一约束与建表脚本一致；`EXPLAIN` 不应出现全表扫描（按主键查询）。

## 4. mariabackup 全量备份

```
# 备份凭据复用 §0 已创建并由单一 EXIT trap 清理的 ${CNF}（=${CLIENT_CNF}）；
# 本节不得再 mktemp 或重定义 EXIT trap，否则会覆盖 §0 的清理承诺。
CNF="${CLIENT_CNF}"

# 备份（需要备份用户具备 RELOAD/PROCESS/REPLICATION CLIENT 等权限）
BACKUP_DIR="${BACKUP_ROOT}/full-$(date +%Y%m%d_%H%M%S)"
mariadb-backup --backup \
  --defaults-extra-file="${CNF}" \
  --target-dir="${BACKUP_DIR}" \
  --parallel=4 || exit 1

# 新流程默认不压缩：--compress/--compress-threads/--decompress 基于 QuickLZ，
# 上游自 MariaDB 10.1.31/10.2.13 弃用（10.11 仍可用）。仅兼容既有压缩备份时：
#   mariadb-backup --backup --defaults-extra-file="${CNF}" \
#     --target-dir="${BACKUP_DIR}" --parallel=4 --compress --compress-threads=4 || exit 1
# 压缩备份须对同一个备份目录先 decompress 再 prepare（未压缩备份跳过 decompress）；
# 任一步失败立即停止，不得继续后续恢复。
mariadb-backup --prepare --target-dir="${BACKUP_DIR}" || exit 1

# 10.11 的 mariadb-backup 无 --verify 选项：以 prepare 成功 + 恢复后计数对账为准
# （见 §3 的 COUNT(*) 列表）；不要用 --verify || true 吞掉失败。
```

记录：

- 备份开始/结束时间、binlog 位点与 GTID（`SHOW MASTER STATUS`）。
- 数据目录大小、压缩后大小、prepare 耗时。
- 备份期间是否有 DDL；有则备份不可作为跨版本回滚点。

## 5. 恢复

```
# 停止实例（svc_stop/svc_start 定义见 SKILL.md §初始化与启动）：
# unit 不存在时回退 mariadb-admin shutdown，不再因 systemctl 失败直接 exit 1。
svc_stop || { echo "停止实例失败，中止恢复" >&2; exit 1; }

# 保留原数据目录（不要直接删）；copy-back 要求恢复到空目录，因此新建空目录
DATADIR_BAK="${DATADIR}.bak.$(date +%Y%m%d_%H%M%S)"
[ ! -e "${DATADIR_BAK}" ] || { echo "原目录备份名已存在，中止" >&2; exit 1; }
mv "${DATADIR}" "${DATADIR_BAK}" || exit 1
mkdir -p "${DATADIR}" || exit 1
[ -z "$(ls -A "${DATADIR}")" ] || { echo "copy-back 目标目录非空，中止" >&2; exit 1; }

# 使用 §4 已 decompress + prepare 的同一备份目录；失败时保留原目录且不要启动
mariadb-backup --copy-back --target-dir="${BACKUP_DIR}" \
  --datadir="${DATADIR}" || { echo "copy-back 失败，原数据仍在 ${DATADIR_BAK}，不要启动" >&2; exit 1; }

# 权限与启动：仅在 copy-back 成功后执行
chown -R mysql:mysql "${DATADIR}" || exit 1
svc_start || { echo "启动失败，请检查错误日志" >&2; exit 1; }
```

恢复后校验：

```
SELECT VERSION();
SHOW DATABASES;
SELECT COUNT(*) FROM <关键表>;
SHOW SLAVE STATUS\G     -- 如果是从库
```

注意：

- 压缩备份必须先对同一个备份目录 `--decompress` 再 `--prepare`（见 §4），任一步失败
  即停止；不要依赖 copy-back 自动解压。原数据目录保留为 `*.bak.<ts>`，copy-back 只写
  新建空目录，成功后才启动。
- 恢复的数据目录必须与二进制版本兼容；不要用新版本数据回退到旧二进制。
- 恢复后 binlog 位点可能与备份时不同；从库需重新指向正确位点/GTID。

## 6. 数据重置（压测前）

```
# 方式 A：恢复备份（推荐，快且一致）；golden 备份须按 §4 解压（若压缩）+ prepare
svc_stop || exit 1                     # 多实例由 SERVICE_NAME="mariadb@${INSTANCE}" 决定目标
DATADIR_BAK="${DATADIR}.bak.$(date +%Y%m%d_%H%M%S)"
[ ! -e "${DATADIR_BAK}" ] || { echo "原目录备份名已存在，中止" >&2; exit 1; }
mv "${DATADIR}" "${DATADIR_BAK}" || exit 1
mkdir -p "${DATADIR}" || exit 1
[ -z "$(ls -A "${DATADIR}")" ] || { echo "copy-back 目标目录非空，中止" >&2; exit 1; }
mariadb-backup --copy-back --target-dir="${BACKUP_ROOT}/golden" \
  --datadir="${DATADIR}" || { echo "copy-back 失败，原数据仍在 ${DATADIR_BAK}，不要启动" >&2; exit 1; }
chown -R mysql:mysql "${DATADIR}" || exit 1
svc_start || exit 1                   # 同上；unit 不存在时回退 mysqld_safe

# 方式 B：drop + 重新 prepare（显式连本轮实例，禁止连客户端默认实例）
RUN_ID="$(date +%Y%m%d_%H%M%S)"
BENCH_DB="bench_${INSTANCE}_${RUN_ID}"    # 本轮专用库，sysbench --mysql-db 用同值
DB=(mariadb --defaults-extra-file="${CLIENT_CNF}" --socket="${SOCKET}" --port="${PORT}" -u root)  # 口令走 §0 的 0600 defaults-file，不进 argv/进程环境
# 删除前核对实例身份：连到的 socket/port 必须等于本轮实例，不能是默认实例
ACTUAL="$("${DB[@]}" -N -B -e "SELECT @@socket, @@port;")"
[ "${ACTUAL}" = "$(printf '%s\t%s' "${SOCKET}" "${PORT}")" ] || { echo "实例身份不符，中止重置" >&2; exit 1; }
# 删除前核对库归属：目标只能是本轮专用库（实例名+轮次），不碰默认实例的 bench
case "${BENCH_DB}" in bench_"${INSTANCE}"_*) ;; *) echo "非本轮专用库，中止重置" >&2; exit 1 ;; esac
"${DB[@]}" -e "DROP DATABASE IF EXISTS \`${BENCH_DB}\`; CREATE DATABASE \`${BENCH_DB}\`;"
sysbench ... --mysql-host="${DB_HOST}" --mysql-db="${BENCH_DB}" prepare

# 清页缓存（会影响整机，需在维护窗口执行）
sync; echo 3 > /proc/sys/vm/drop_caches
```

纪律：

- 每轮压测前重置到同一状态；记录重置方式与耗时。
- 不要在重置后立刻开测：先 warmup，再进入采样窗口。
- 清缓存会同时影响 OS 与其他实例，测试机独占时再执行。

## 7. 大表在线变更

- 在线 DDL 会写 `#sql-*.ibd` 与 `innodb_online_alter_log_max_size` 限制的日志；
大表变更需评估磁盘、锁等待与复制延迟。
- 变更前备份、变更中监控 `INNODB_TRX`/MDL 等待、变更后 `ANALYZE TABLE`。
- 主从环境优先在从库验证，再切主或滚动执行。

## 8. 输出要求

装载/备份类任务必须给出：数据集与规模、命令与耗时、失败数、对账结果、
备份位点/GTID、恢复后校验、以及可复现的重置方式。
