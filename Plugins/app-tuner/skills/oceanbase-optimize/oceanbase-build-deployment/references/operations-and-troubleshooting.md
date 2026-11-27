# 运维、备份恢复与故障排查

## 1. 日常启停

### 1.1 带绑核启动 observer（裸机）

```
# 先确认旧进程已退出
ps -ef | grep '[o]bserver'

# 启动时绑定（示例：32U，NUMA0）
cd ${DATA_DIR}/${CLUSTER_NAME}.z1.obs0
numactl -C ${OBS_CPUS} -m ${OBS_NUMA} ./bin/observer

# 后台运行 + 日志
nohup numactl -C ${OBS_CPUS} -m ${OBS_NUMA} ./bin/observer \
  > ${OB_BASE}/log/observer.out 2>&1 &
```

> **启动后用 `taskset` 再绑核只迁移 CPU，不迁移内存**，会留下跨 NUMA 访存。必须启动时绑。

### 1.2 重启 obproxy

```
ps -ef | grep '[o]bproxy'
kill <pid>            # 优先 SIGTERM
# 按 §deploy-and-acceptance 的标准命令，用 numactl 重新拉起
```

### 1.3 停止

```
# 优先优雅停止（按版本/运维平台）
# 必要时：
kill <observer_pid>   # SIGTERM
sleep ${STOP_TIMEOUT}
ps -ef | grep '[o]bserver'
```

`kill -9` 只作为最后手段；非正常退出会导致恢复/回放变慢，且可能触发副本切换。

## 2. 备份

OceanBase 备份分两部分：

1. **数据备份**：基线数据。
2. **日志归档**：clog 增量，用于恢复到任意时间点。

4.3.5 流程（语法以目标版本为准）：

```
-- 分别配置该租户的数据备份目的地与日志归档目的地（NFS/S3/OSS）
ALTER SYSTEM SET DATA_BACKUP_DEST='<data_backup_url>' TENANT = ${TENANT_NAME};
ALTER SYSTEM SET LOG_ARCHIVE_DEST='<archive_url>' TENANT = ${TENANT_NAME};

-- 开启归档
ALTER SYSTEM ARCHIVELOG TENANT = ${TENANT_NAME};

-- 继续前确认归档已开始（STATUS 为 STARTING/DOING 且有 piece，不能是 STOPPED）
SELECT TENANT_ID, DEST_ID, STATUS, START_SCN_DISPLAY
  FROM oceanbase.CDB_OB_ARCHIVELOG ORDER BY START_SCN_DISPLAY DESC;
-- 未开始/异常先停止排查，不要继续全备

-- 全量备份
ALTER SYSTEM BACKUP DATABASE;          -- 或 BACKUP TENANT = '<tenant>';

-- 查看进度
SELECT * FROM oceanbase.CDB_OB_BACKUP_JOBS ORDER BY JOB_ID DESC;
SELECT * FROM oceanbase.CDB_OB_BACKUP_TASKS ORDER BY TASK_ID DESC;
```

要求：

- 备份前确认目的端容量与连通；NFS 需挂载且权限正确。
- 4.3.5 每个租户要分别配置 `DATA_BACKUP_DEST` 与 `LOG_ARCHIVE_DEST`，并确认归档已开始；否则全备能起但没有归档，无法按时间点恢复。
- 数据备份与归档必须来自同一集群、同一时间段；只恢复数据不恢复归档会丢增量。
- 备份期间避免大 DDL/大合并；记录备份集 ID 与时间点。
- 备份完成后做一次**恢复演练**（隔离集群/租户）。

## 3. 恢复

4.3.5 物理恢复用 `ALTER SYSTEM RESTORE`；`CREATE TENANT ... RESTORE FROM` 无法发起恢复。

```
-- 1) 查看可用备份集与归档 piece
SELECT * FROM oceanbase.CDB_OB_BACKUP_SET_FILES;
SELECT * FROM oceanbase.CDB_OB_ARCHIVELOG_PIECE_FILES;

-- 2) 准备目标资源单元与资源池（恢复目标端要有资源）
CREATE RESOURCE UNIT u_restore MAX_CPU 10, MIN_CPU 10, MEMORY_SIZE '20G',
  MAX_IOPS 10000000, MIN_IOPS 10000000, LOG_DISK_SIZE '60G';
CREATE RESOURCE POOL p_restore UNIT 'u_restore', UNIT_NUM 1, ZONE_LIST ('z1');

-- 3) 发起物理恢复：数据备份与日志归档两个 URL 逗号分隔写在同一个字符串内；
--    UNTIL 用 TIME='<timestamp>'（或 SCN=<整数>），不是裸字符串；WITH 指定目标资源池/locality
ALTER SYSTEM RESTORE <new_tenant>
  FROM '<data_backup_url>,<archive_url>'
  UNTIL TIME='<timestamp>'
  WITH 'pool_list=p_restore&primary_zone=z1&locality=F@z1';

-- 4) 用恢复作业状态验收（STATUS=SUCCESS 且无 ERROR 才继续核对数据）
SELECT * FROM oceanbase.CDB_OB_RESTORE_PROGRESS ORDER BY JOB_ID DESC;
-- 失败时看结果/报错：
SELECT * FROM oceanbase.CDB_OB_RESTORE_HISTORY ORDER BY JOB_ID DESC;
```

规则：

- 优先恢复到**新租户/新集群**，校验后再切换；直接覆盖生产风险极高。
- 恢复需要目标端有足够 Unit 资源；`locality`/Zone 要匹配，并指向已创建的目标资源池。
- 恢复作业未 `SUCCESS` 不得对外提供服务；先查进度/历史视图的报错再重试。
- 恢复后核对：租户状态、表数量、关键表计数、抽样行、索引可用性。
- 跨版本恢复要确认兼容性；低版本通常不能打开高版本备份。

## 4. 升级与回滚

### 4.1 升级前

- 备份数据 + 归档；记录当前 observer/obproxy 版本、commit、md5、编译参数。
- 确认新版本数据格式兼容（升级路径、是否有 DDL/系统表变更）。
- 在隔离环境用同一份数据演练升级 + 验收。
- 规划停机窗口与回滚条件（例如关键查询错误率、延迟阈值）。

### 4.2 滚动升级（多副本）

```
停一个备副本 → 替换二进制 → 启动 → 等待 ACTIVE/同步
  → 下一个备副本 → 最后主副本（触发切换）
```

每一步重启后先确认节点状态与版本：

```
SELECT SVR_IP, SVR_PORT, STATUS, BUILD_VERSION FROM oceanbase.DBA_OB_SERVERS;
```

**仅看节点状态/版本不能证明日志流副本已追平**，据此停下一台会削弱多数派。必须再按日志流核对同步进度与副本健康：

```
-- 各日志流副本的角色与同步位点：同一 LS 各副本 END_SCN 应一致/持续收敛
SELECT TENANT_ID, LS_ID, SVR_IP, SVR_PORT, ROLE, END_SCN
  FROM oceanbase.GV$OB_LOG_STAT ORDER BY TENANT_ID, LS_ID;

-- 日志流状态：应均为 NORMAL，无异常/迁移中（DBA_OB_LS 无 TENANT_ID 列；
-- 需按租户过滤时用含 TENANT_ID 的 CDB_OB_LS）
SELECT LS_ID, STATUS FROM oceanbase.DBA_OB_LS ORDER BY LS_ID;
```

放行条件（全部满足才停下一台；未满足立即停止滚动升级，排查/回滚）：

- `DBA_OB_SERVERS` 全部 `ACTIVE`，刚重启节点的 `BUILD_VERSION` 已更新。
- 每个日志流副本数与 locality 一致（三副本=3），有且仅有一个 `LEADER`，无 `OFFLINE`/异常副本。
- 同一 `LS_ID` 各副本 `END_SCN` 一致或差值在秒级阈值内且不再增长（回放已追平）。
- 存活副本仍满足多数派（三副本至少 2 个）；否则不得再停任何节点。

### 4.3 回滚

- 保留旧二进制目录 `bin.bak.<ts>` 与旧配置。
- 旧二进制 + 旧数据：只有在数据格式未升级时可行；若已升级，通常需恢复备份。
- 回滚后重跑：节点状态、租户、读写、关键查询、性能基线。

## 5. 日志与现场

| 内容 | 位置/方式 |
| --- | --- |
| observer 运行日志 | `${DATA_DIR}/log/`、`${CLOG_DIR}` 同级的 log 目录、启动重定向文件 |
| obproxy 日志 | obproxy 安装目录 `log/` |
| 系统日志 | `dmesg`、`/var/log/messages` |
| 内核 OOM | `dmesg \| grep -i oom`、`/var/log/kern.log` |
| core | `ulimit -c unlimited` 后的 core 文件；`coredumpctl`（如启用） |
| 线程栈 | `pstack <pid>` / `gdb -p <pid> -batch -ex 'thread apply all bt'` |
| SQL 审计 | `GV$OB_SQL_AUDIT` / `CDB_OB_SQL_AUDIT` |
| 会话/线程 | `GV$OB_PROCESSLIST`、线程状态视图（见 hotspot skill） |

现场保留顺序：先只读采集（日志、栈、指标、配置、二进制 md5），再考虑重启/替换。

## 6. 错误码与常见故障

| 错误码/症状 | 含义 | 处置 |
| --- | --- | --- |
| 1062 | 唯一键冲突 | 压测常见；`--mysql-ignore-errors` 或检查数据分布 |
| 6002 / 6001 | 事务被回滚（-6002 `OB_TRANS_ROLLBACKED`）/ 事务集合变化写冲突（-6001 `OB_TRANSACTION_SET_VIOLATION`） | 6001 确认热点行写冲突后业务侧重试；6002 查事务被杀/回滚原因 |
| 4012 | 超时（内部） | 查 RPC、副本、磁盘 |
| 1205 | 锁等待超时 | 查长事务/热点行/表锁 |
| 1020 / 4038 / 6004 | 只读/事务/副本相关 | 查副本状态、租户状态、磁盘 |
| `No memory` / 分配失败 | 租户内存不足 | 查 Context hold、cache wash、Unit 内存 |
| `disk full` / 冻结失败 | data/clog 空间不足 | 扩容/清理归档/调 `datafile_size` |
| `paxos` 异常/副本不同步 | 网络/时钟/磁盘 | 查 RPC 端口、chrony、clog 盘 |
| observer 反复重启 | OOM/权限/数据损坏 | `dmesg`、日志、core、目录权限 |
| `Text file busy` | 覆盖运行中的二进制 | 停进程后再替换 |
| 租户只读 | Unit/副本/磁盘问题 | 查 `DBA_OB_TENANTS`、`DBA_OB_SERVERS`、磁盘 |

## 7. 变更纪律

| 变更 | 风险 | 回滚 |
| --- | --- | --- |
| 端口/目录/集群名 | 起不来、数据找不到 | 恢复原配置与目录，按顺序重启 |
| 租户 Unit/副本/locality | 建租户卡住、数据重分布 | 恢复原 Unit/副本定义 |
| 冻结/限流/内存参数 | 写抖动、OOM、限流 | 恢复原参数并重启/等待生效 |
| 二进制升级 | 数据格式、行为变化 | 旧二进制/备份恢复 |
| 备份目的端/归档 | 备份失败、无法恢复 | 恢复原目的端，重新全备 |

高风险变更（副本、数据格式、归档、升级）必须先与用户确认并备份；生产变更安排窗口。
