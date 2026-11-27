# 功能验证：构建、mtr、冒烟、对账、复制与备份

本文件是“正确性门禁”的操作手册。任何性能结论都必须建立在它们全部通过之上。

## 1. 构建验证

```
BIN="${MARIADB_HOME}/bin/mariadbd"
"$BIN" --version
md5sum "$BIN"
ldd "$BIN" | grep -Ei 'jemalloc|tcmalloc|ssl|crypto|aio' || true
readelf -p .comment "$BIN" 2>/dev/null | head -n 5 || true
```

检查项：

- 版本字符串与源码 `VERSION` 一致（10.11.16）。
- 分配器符合预期（系统 malloc / jemalloc / tcmalloc 只能一种）。
- TLS 库符合预期（OpenSSL/GnuTLS）。
- 二进制 md5 写入报告；两腿 A/B 必须可区分。

启动冒烟：

```
mariadb --socket="${SOCKET}" -e "
  SELECT VERSION();
  SELECT @@default_storage_engine, @@character_set_server, @@collation_server;
  SELECT @@innodb_buffer_pool_size, @@innodb_log_file_size, @@innodb_flush_method;
  SHOW ENGINES;"
```

## 2. mtr（mysql-test-run）

`mysql-test/mariadb-test-run.pl` 是官方回归框架，自带独立实例、数据与端口。

### 2.1 suite 选择

| 改动范围 | 建议 suite |
| --- | --- |
| 解析/优化器/执行器 | `main`、`optimizer_unfixed_bugs`（如相关） |
| InnoDB | `innodb`、`innodb_fts`、`innodb_zip`、`innodb_gis` |
| Aria | `maria` |
| 复制 | `rpl`、`multi_source`、`binlog` |
| Galera | `galera`、`galera_3nodes`、`galera_sr` |
| 权限/角色 | `roles`、`sys_vars` |
| 备份 | `mariabackup` |
| 插件/引擎 | `plugins`、`engines`、`storage_engine` |
| 性能模式 | `perfschema` |

### 2.2 运行

```
cd "${SRC_ROOT}/mysql-test"

# 全量（耗时，机器需空闲）
perl mariadb-test-run.pl --parallel="$(nproc)" --retry=0

# 单 suite
perl mariadb-test-run.pl --suite=innodb --parallel=8 --retry=0

# 单用例 + 详细输出
perl mariadb-test-run.pl --verbose innodb.innodb_mysql

# 复现失败
perl mariadb-test-run.pl --start-and-exit --suite=innodb
```

结果：

- 汇总在 `mysql-test/var/log/mysqltest.log`、失败用例在 `var/log/*.err`。
- `--force` 会继续执行后续用例，适合摸底；门禁判定不要用。
- `--big-test` 才跑大用例；CI 与本地可不同，报告里要写清楚。

### 2.3 何时必须跑

- 改了 InnoDB/锁/事务/日志 → `innodb` + `main`。
- 改了复制/日志事件 → `rpl` + `binlog`。
- 改了解析/优化器 → `main` 中相关用例 + 业务 SQL 路径比对。
- 改了构建参数（LSE/jemalloc）→ 至少 `main` 冒烟 + 目标场景压测。

## 3. SQL 冒烟

只读冒烟见 `scripts/smoke_test.sh`：它只查询配置与只读路径（含 `@@tx_isolation`、
`@@read_only`），**不写库、不验证事务提交/回滚语义**。手工补充：

```
-- 元数据
SHOW DATABASES;
SHOW TABLES FROM ${DB};
SHOW CREATE TABLE ${DB}.${TABLE}\G

-- 访问路径
EXPLAIN SELECT * FROM ${DB}.${TABLE} WHERE id = 1;
EXPLAIN SELECT * FROM ${DB}.${TABLE} WHERE id BETWEEN 1 AND 100 ORDER BY id LIMIT 10;

-- 聚合
SELECT COUNT(*) FROM ${DB}.${TABLE};
SELECT MIN(id), MAX(id) FROM ${DB}.${TABLE};

-- 字符集/排序
SELECT @@character_set_connection, @@collation_connection;
```

写入/DDL 与事务提交/回滚是**独立验收项**，必须在隔离库单独执行并核对结果，不能用只读
冒烟退出 0 代替。每轮新建唯一库：遇重名立即停止；完成提交与回滚验证后**只清理本轮确认
创建的库**，绝不复用或删除既有库。

```bash
umask 077
PWFILE="$(mktemp)"
printf '[client]\npassword=%s\n' "${DB_PASSWORD}" > "${PWFILE}"
trap 'rm -f "${PWFILE}"' EXIT
MC=(mariadb --defaults-extra-file="${PWFILE}" --socket="${SOCKET}" --batch --skip-column-names)

# 本轮唯一库名（时间戳 + PID）；重名即停，不用 IF NOT EXISTS 掩盖冲突。
VERIFY_DB="verify_tmp_$(date +%s)_$$"
if "${MC[@]}" -e "SHOW DATABASES LIKE '${VERIFY_DB}'" | grep -qx "${VERIFY_DB}"; then
  echo "本轮验证库已存在，停止（不触碰既有库）：${VERIFY_DB}" >&2
  exit 1
fi
"${MC[@]}" -e "CREATE DATABASE ${VERIFY_DB}" || exit 1
CREATED_THIS_RUN=1

# DML 与查询
"${MC[@]}" -e "CREATE TABLE ${VERIFY_DB}.t (id INT PRIMARY KEY, v VARCHAR(32)) ENGINE=InnoDB"
"${MC[@]}" -e "INSERT INTO ${VERIFY_DB}.t VALUES (1,'a'),(2,'b')"
"${MC[@]}" -e "SELECT * FROM ${VERIFY_DB}.t ORDER BY id"
"${MC[@]}" -e "UPDATE ${VERIFY_DB}.t SET v='c' WHERE id=2"
"${MC[@]}" -e "DELETE FROM ${VERIFY_DB}.t WHERE id=1"
"${MC[@]}" -e "SELECT * FROM ${VERIFY_DB}.t"

# 事务验收：提交后可见、回滚后不可见；两项都核对计数，不是只看退出码。
"${MC[@]}" -e "START TRANSACTION; INSERT INTO ${VERIFY_DB}.t VALUES (3,'x'); COMMIT;"
COMMIT_CNT="$("${MC[@]}" -e "SELECT COUNT(*) FROM ${VERIFY_DB}.t WHERE id=3")"
[ "${COMMIT_CNT}" = "1" ] || { echo "提交验证失败：COUNT=${COMMIT_CNT}" >&2; exit 1; }
"${MC[@]}" -e "START TRANSACTION; INSERT INTO ${VERIFY_DB}.t VALUES (4,'y'); ROLLBACK;"
ROLLBACK_CNT="$("${MC[@]}" -e "SELECT COUNT(*) FROM ${VERIFY_DB}.t WHERE id=4")"
[ "${ROLLBACK_CNT}" = "0" ] || { echo "回滚验证失败：COUNT=${ROLLBACK_CNT}" >&2; exit 1; }

# 只在确认本轮创建后才清理，且放在回滚验证之后，避免事务验证失去目标表。
if [ "${CREATED_THIS_RUN:-0}" = "1" ]; then
  "${MC[@]}" -e "DROP DATABASE ${VERIFY_DB}"
fi
```

注意：`CREATE DATABASE` 不带 `IF NOT EXISTS`；若检查与创建之间被并发占用，`CREATE` 会失败并
立即退出，不会误删不属于本轮的库。

## 4. 数据对账

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 行数 | `SELECT COUNT(*)` | 与装载日志/期望一致 |
| 主键范围 | `SELECT MIN(id),MAX(id)` | 与数据生成范围一致 |
| 空值比例 | `SUM(col IS NULL)/COUNT(*)` | 与 schema/数据生成规则一致 |
| 唯一性 | `COUNT(*)` vs `COUNT(DISTINCT id)` | 相等（主键/唯一键） |
| 索引 | `SHOW INDEX`、`EXPLAIN` | 索引存在且被使用 |
| 校验和 | `CHECKSUM TABLE`（MyISAM/Aria 适用） | 与基线一致 |

注意：InnoDB 的 `CHECKSUM TABLE` 成本高且语义有限，跨版本/跨引擎不要直接比较；
优先用计数 + 抽样 + 业务聚合。

## 5. 复制验证

```
SHOW MASTER STATUS;   -- 主库位点/GTID
SHOW SLAVE STATUS\G   -- 从库状态
```

检查：

- `Slave_IO_Running=Yes`、`Slave_SQL_Running=Yes`。
- `Seconds_Behind_Master` 在可接受范围；持续增长说明回放跟不上。
- MariaDB 口径：`Gtid_IO_Pos`（取回位点）与 `@@gtid_slave_pos`（已执行位点）是否追平；
  主库侧对比 `@@gtid_binlog_pos`。`Retrieved_Gtid_Set`/`Executed_Gtid_Set` 是 MySQL 列名，
  MariaDB 的 `SHOW SLAVE STATUS` 中没有这两列。
- `Last_Error` / `Last_IO_Error` / `Last_SQL_Error` 为空。

一致性抽检：

```
-- 主库与从库分别执行，比较结果
SELECT COUNT(*), COALESCE(SUM(id),0) FROM ${DB}.${TABLE};
SELECT MD5(GROUP_CONCAT(id ORDER BY id SEPARATOR ',')) FROM (
  SELECT id FROM ${DB}.${TABLE} ORDER BY id LIMIT 1000
) x;
```

大表可用分块校验（按主键区间）避免一次性扫描。

Galera：

```
SHOW STATUS LIKE 'wsrep_ready';            -- ON
SHOW STATUS LIKE 'wsrep_cluster_status';   -- Primary
SHOW STATUS LIKE 'wsrep_local_recv_queue'; -- 接近 0
SHOW STATUS LIKE 'wsrep_flow_control_paused'; -- 越低越好
```

## 6. 备份验证

```
mariadb-backup --prepare --target-dir="${BK}"
# 10.11 的 mariadb-backup 无 --verify 选项：以 prepare 成功 + 隔离实例 --copy-back
# 后的计数对账为准（见下方恢复演练）；不要用 --verify || true 吞掉失败
```

恢复演练（隔离实例）：

```
mariadb-backup --copy-back --target-dir="${BK}" --datadir="${RESTORE_DIR}"
chown -R mysql:mysql "${RESTORE_DIR}"
# 用独立端口/socket 启动隔离实例
```

校验：

- 关键表计数、抽样属性与源库一致。
- `mysql` 系统库用户/权限可用（能登录、能授权）。
- 从库恢复后能按位点/GTID 继续复制。

## 7. 错误签名速查

| 日志片段 | 含义 | 处置 |
| --- | --- | --- |
| `Deadlock found when trying to get lock` | 死锁，已回滚一个事务 | 看 `LATEST DETECTED DEADLOCK`，调整访问顺序/索引 |
| `Lock wait timeout exceeded` | 行锁等待超时 | 查长事务/未提交事务、隔离级别 |
| `Cannot add or update a child row` | 外键约束 | 数据顺序或约束定义 |
| `Duplicate entry` | 唯一键冲突 | 数据重复或并发插入逻辑 |
| `InnoDB: Error: log file ... is of different size` | redo 配置与现有文件不一致 | 按版本正确调整 redo（10.11 可在线 resize） |
| `Table ... is marked as crashed` | 非事务引擎崩溃 | `REPAIR TABLE` 或从备份恢复 |
| `WSREP: ... certification failure` | Galera 写集冲突 | 业务重试/降低冲突 |

## 8. 门禁规则

- 任一功能项失败 → 停止性能测试，先修复。
- 性能提升但功能回归 → 直接否决，不看数字。
- 结果不可复现 → 视为无效，不进入结论。
