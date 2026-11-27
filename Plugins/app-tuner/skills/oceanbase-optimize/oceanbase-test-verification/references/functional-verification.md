# 功能与数据正确性验证

## 1. 元数据视图

连接系统租户：

```
obclient -h "${OB_HOST}" -P "${OB_PORT}" -uroot@sys -Doceanbase -A
```

| 视图 | 用途 | 关键列 |
| --- | --- | --- |
| `DBA_OB_SERVERS` | 节点与端口 | `SVR_IP`、`SQL_PORT`、`SVR_PORT`、`STATUS`、`WITH_ROOTSERVER`、`BUILD_VERSION` |
| `DBA_OB_ZONES` | Zone | `ZONE`、`STATUS`、`IDC`、`REGION` |
| `DBA_OB_TENANTS` | 租户 | `TENANT_ID`、`TENANT_NAME`、`TENANT_TYPE`、`PRIMARY_ZONE`、`LOCALITY`、`COMPATIBILITY_MODE`、`STATUS` |
| `DBA_OB_UNITS` | Unit 与租户绑定 | `UNIT_ID`、`TENANT_ID`、`STATUS`、`UNIT_CONFIG_ID` |
| `DBA_OB_UNIT_CONFIGS` | Unit 规格 | `MAX_CPU`、`MIN_CPU`、`MEMORY_SIZE`、`MAX_IOPS` |
| `DBA_OB_RESOURCE_POOLS` | 资源池 | `NAME`、`UNIT_COUNT`、`ZONE_LIST`、`TENANT_ID` |
| `DBA_OB_LS` | Log Stream | `LS_ID`、`STATUS`、`PRIMARY_ZONE` |
| `CDB_OB_BACKUP_*` | 备份 | 任务/集/文件 |

通过标准：

- 节点全 `ACTIVE`；`BUILD_VERSION` 与二进制一致。
- Zone 数与 `zone_num` 一致。
- 业务租户 `STATUS=NORMAL`；`LOCALITY` 副本数与设计一致。
- Unit 的 CPU/内存与规划一致；`ZONE_LIST` 与 Zone 对应。
- `DBA_OB_LS` 的 LS 状态正常，主 Zone 与 `primary_zone` 一致。

## 2. 参数快照

```
SHOW PARAMETERS LIKE '%memory%';
SHOW PARAMETERS LIKE '%cpu%';
SHOW PARAMETERS LIKE '%thread%';
SHOW PARAMETERS LIKE '%freeze%';
SHOW PARAMETERS LIKE '%throttling%';
SHOW PARAMETERS LIKE '%port%';
```

用途：

- 确认 `cpu_count`/`memory_limit`/`net_thread_count` 与规格一致。
- 确认冻结/限流参数是否为测试临时值（交易类模型会临时改），测试后恢复。
- 作为 A/B 的“改前”证据。

## 3. SQL 冒烟

只读项：系统租户验元数据，业务租户验库表，**两者分开连接**——`root@sys` 看不到业务库
`obtest` 的表，`SPACE_DB` 只选库名、不切租户。

```
OB_HOST='<observer_or_proxy_ip>'   # 执行前替换为实际地址
OB_PORT=${MYSQL_PORT}              # 端口以目标集群 install.cfg 为准（官方默认 2881）
# 系统租户
obclient -h "${OB_HOST}" -P "${OB_PORT}" -uroot@sys -Doceanbase -A -e 'SELECT 1; SHOW DATABASES;'
# 业务租户（显式指定租户账号；表名按实际替换）
# 直连 observer：-u 只写 用户名@租户名，不能带 #集群名，否则连接报错
obclient -h "${OB_HOST}" -P "${OB_PORT}" -u'obtest@mysql_tenant' -Dobtest -A -e \
  'SHOW TABLES; SELECT COUNT(*) FROM sbtest1;'
# 经 obproxy：才使用 obproxy 格式 用户名@租户名#集群名
# obclient -h "${PROXY_IP}" -P "${PROXY_PORT}" -u'obtest@mysql_tenant#obcluster' -Dobtest -A -e \
#   'SHOW TABLES; SELECT COUNT(*) FROM sbtest1;'
```

抽样只用于确认可读，不要把业务表原始行贴进验收报告；确需样本证据时显式选列并脱敏。

写入项（隔离库；本轮唯一库名，撞名立即停止，只清理本轮确认创建成功的库）：

```
OB_HOST='<observer_or_proxy_ip>'   # 执行前替换为实际地址
OB_PORT=${MYSQL_PORT}
# 直连 observer 用 obtest@mysql_tenant；经 obproxy 才用 obtest@mysql_tenant#obcluster
BIZ_USER='obtest@mysql_tenant'
BIZ_PASSWORD='<由环境/交互注入，不硬编码进仓库>'
# 口令写入 0600 临时 defaults-file（见 smoke_test.sh 的 make_defaults），不进 argv
umask 077
BIZ_DEFAULTS="$(mktemp)"
trap 'rm -f "${BIZ_DEFAULTS}"' EXIT
printf '[client]\npassword=%s\n' "${BIZ_PASSWORD}" > "${BIZ_DEFAULTS}"
# --defaults-extra-file 必须放在其它客户端选项之前，否则客户端会忽略，口令仍无法认证
ob() { obclient --defaults-extra-file="${BIZ_DEFAULTS}" -h "${OB_HOST}" -P "${OB_PORT}" -u "${BIZ_USER}" "$@"; }

# 1) 生成本轮唯一库名：时间戳 + PID + 随机数
SMOKE_DB="smoke_$(date +%Y%m%d%H%M%S)_$$_${RANDOM}"

# 2) 先确认不存在；重名立即停止，绝不复用/删除已有库
EXISTS="$(ob -Doceanbase -N -B -e \
  "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='${SMOKE_DB}';")"
if [ -n "${EXISTS}" ]; then
  echo "FAIL 库名已存在，停止：${SMOKE_DB}"; exit 1
fi

# 3) 创建时不使用 IF NOT EXISTS；并发抢占失败也立即停止
if ! ob -Doceanbase -e "CREATE DATABASE ${SMOKE_DB};"; then
  echo "FAIL 建库失败，停止"; exit 1
fi
CREATED=1

# 4) 只在确认由本轮创建的库里做写回读
ob -D"${SMOKE_DB}" -e "
CREATE TABLE t(id INT PRIMARY KEY, v VARCHAR(64), n DECIMAL(10,2), ts DATETIME);
INSERT INTO t VALUES (1,'a',1.23,NOW()),(2,'b',2.34,NOW());
SELECT COUNT(*) FROM t;                 -- 期望 2
SELECT v,n FROM t WHERE id=1;           -- 期望 a,1.23
UPDATE t SET n=n+1 WHERE id=1;
SELECT n FROM t WHERE id=1;             -- 期望 2.23
DELETE FROM t WHERE id=2;
SELECT COUNT(*) FROM t;                 -- 期望 1
"

# 5) 只清理本轮确认创建成功的库
if [ "${CREATED:-0}" = "1" ]; then
  ob -Doceanbase -e "DROP DATABASE ${SMOKE_DB};"
fi
```

检查：类型（DECIMAL 精度、DATETIME 时区）、字符集/collation、主键冲突行为、事务回滚。

## 4. 事务与一致性

### 4.1 显式事务

```
BEGIN;
UPDATE acct SET balance=balance-100 WHERE id=1;
UPDATE acct SET balance=balance+100 WHERE id=2;
COMMIT;
-- 校验总额不变
SELECT SUM(balance) FROM acct;
```

### 4.2 回滚

```
BEGIN;
UPDATE acct SET balance=balance-100 WHERE id=1;
ROLLBACK;
-- 校验余额未变
```

### 4.3 隔离级别

- 确认业务期望的隔离级别（READ COMMITTED / SERIALIZABLE 等）与实际行为一致。
- 高并发下检查唯一键冲突（1062）、死锁/锁等待超时（1205）是否符合预期。
- 长事务会拉长版本链，影响读放大；压测中避免人为长事务。

## 5. 数据对账

| 项 | 方法 | 判定 |
| --- | --- | --- |
| 行数 | `SELECT COUNT(*)` | 与数据集/上轮一致（允许唯一键冲突导致的预期差异） |
| 聚合 | `SELECT SUM/AVG/MIN/MAX` | 与基线一致 |
| 抽样 | 随机/主键抽样 `LIMIT` | 属性类型与内容正确 |
| 索引 | 索引列等值/范围查询 | 结果与全表扫描一致（小表验证） |
| 主副本 | `DBA_OB_LS`/执行计划 | 主 Zone 符合 `primary_zone` |
| 副本 | `LOCALITY`/`DBA_OB_LS` | 副本数与设计一致 |

> OceanBase 的 `CHECKSUM TABLE` 支持情况随版本变化；不确定时用计数 + 聚合 + 抽样组合，
> 不要假设某个 MySQL 语句一定可用。

## 6. 主副本与路由

- 三副本时，确认业务租户 `primary_zone`（如 `z1;z2;z3`）；测试常固定为压测机同 Zone。
- 用 `EXPLAIN` 或 `GV$OB_SQL_AUDIT` 观察是否产生远程 DAS；跨机读会增加网络与延迟。
- 单节点必须 `locality=F`，否则建租户/副本会卡住。

## 7. 错误码

| 错误码 | 含义 | 是否正常 |
| --- | --- | --- |
| 1062 | 唯一键冲突 | 压测中常见，需按场景解释 |
| 6001 | 事务集合变化（OCC 写冲突，服务端重试） | 写冲突属预期，业务侧重试并排查热点行 |
| 6002 | 事务回滚（含空闲超时，`ob_trx_idle_timeout` 可调） | 查回滚/被杀原因、长事务 |
| 4012 | 查询超时（`ob_query_timeout` / `query_timeout` hint 可调） | 查慢 SQL、按需调超时，不按「OB 内部问题」处理 |
| 1205 | 锁等待超时 | 需查长事务/热点行 |
| 1020 / 4038 / 6004 | 只读/事务/副本相关 | 需查副本/租户/磁盘 |

压测脚本里的 `--mysql-ignore-errors` 会隐藏这些错误；报告中必须列出忽略了哪些码、
忽略比例是多少。

## 8. 验收清单

- `DBA_OB_SERVERS` 全 ACTIVE，版本正确。
- Zone/租户/Unit/资源池符合设计。
- `primary_zone`/`locality` 正确。
- 参数与规格一致，没有遗留临时值。
- 只读冒烟通过（计数/抽样）。
- 隔离库写回读/更新/删除/回滚通过。
- 事务总额守恒/回滚正确。
- 无异常错误码；忽略的错误码有记录。
- 备份/归档任务状态正常（如已配置）。

## 9. 验证矩阵

| 层级 | 手段 | 依赖 | 能回答什么 |
| --- | --- | --- | --- |
| 构建/部署 | 二进制 md5/版本、`DBA_OB_SERVERS` | 运行中的集群 | 装出来的东西是否可信 |
| 元数据 | `DBA_OB_ZONES/TENANTS/UNITS` | 系统租户 | 拓扑、资源、副本是否正确 |
| 功能 | SQL 冒烟、计数、抽样、写回读 | 业务库 | 读写与事务是否正确 |
| 数据 | 表计数、校验和、抽样属性 | 数据集/schema | 数据是否完整/一致 |
| 微基准 | 单表点查/写入小规模 | 集群 | 单操作是否变慢 |
| 系统压测 | sysbench 五场景 + 业务模型 | 压测机 + obproxy | 端到端吞吐/延迟/扩展性 |
| 判定 | A/B 同口径、多轮、噪声阈值 | 基线 + 候选 | 收益是否真实、是否保留 |
