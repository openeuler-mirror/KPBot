# 启停、日志、故障与升级回滚

本文件是部署后的运维手册：如何启停、看什么日志、常见故障如何定位、如何做版本切换与回滚。

> 服务控制变量与 `svc_start`/`svc_stop` 包装定义见本 skill `SKILL.md` §初始化与启动；
> 凭据文件 `${CLIENT_CNF}` 由 `data-load-and-backup.md` §0 建立（0600 + 单一 EXIT trap）。
> 单独执行本节前先加载这些定义，勿再裸用 `systemctl start/stop mariadb`。

## 1. 启停

### systemd（推荐；unit 由 SKILL.md §初始化与启动 创建）

```
SERVICE_CTL="${SERVICE_CTL:-systemctl}"
SERVICE_NAME="${SERVICE_NAME:-mariadb@${INSTANCE:-default}}"
svc_start                              # unit 不存在时回退 mysqld_safe
svc_stop                               # unit 不存在时回退 mariadb-admin shutdown
"${SERVICE_CTL}" restart "${SERVICE_NAME}"
"${SERVICE_CTL}" status "${SERVICE_NAME}"
journalctl -u "${SERVICE_NAME}" -n 200 --no-pager
```

### mysqld_safe（前台/调试）

```
"${MARIADB_HOME}/bin/mysqld_safe" \
  --defaults-file=/etc/my.cnf \
  --basedir="${MARIADB_HOME}" \
  --datadir="${DATADIR}" \
  --user=mysql &
```

### 优雅关闭

```
mariadb-admin --defaults-extra-file="${CLIENT_CNF}" --socket="${SOCKET}" -u root shutdown
# 或
svc_stop
```

要求：

- 关闭前确认没有长事务/大 DDL；必要时 `KILL` 并等待回滚。
- 不要 `kill -9`；会导致恢复时间变长、临时表残留。
- 多实例按依赖顺序停止（先停应用连接，再停从库，最后停主库/引导节点）。

## 2. 状态与日志

| 目标 | 命令 |
| --- | --- |
| 进程 | `ps -ef \| grep [m]ariadbd` |
| 线程 | `ps -L -p "$(pgrep -x mariadbd)" -o tid,comm,psr,pcpu` |
| 端口 | `ss -lntp \| grep mariadbd` |
| 连接 | `mariadb-admin --socket="${SOCKET}" status` |
| 错误日志 | `tail -n 200 "${LOG_DIR}/error.log"` |
| 慢日志 | `tail -n 100 "${LOG_DIR}"/*slow.log` |
| 复制 | `SHOW SLAVE STATUS\G` |
| Galera | `SHOW STATUS LIKE 'wsrep_%';` |

错误日志关键字：

```
[ERROR] InnoDB: Cannot
[ERROR] Aborting
[ERROR] unknown variable
Address already in use
Can't create/write to file
InnoDB: Operating system error number
WSREP: failed to
```

## 3. 常见故障

| 症状 | 可能原因 | 处置 |
| --- | --- | --- |
| 启动即退出，无输出 | 配置语法/未知变量/目录权限 | 前台 `mariadbd --defaults-file=... --console` 看报错 |
| `unknown variable 'xxx'` | 旧配置含已移除参数 | 对照 10.11 参数表删除/替换 |
| `Address already in use` | 端口/socket 冲突 | `ss -lntp` 找占用；多实例端口段重排 |
| `Can't connect through socket` | socket 路径不一致/权限 | 客户端显式 `--socket`；检查目录属主 |
| `InnoDB: Cannot allocate memory` | buffer pool 过大/大页不足/cgroup 限制 | 降 buffer pool、补大页、查 `memory.max` |
| 启动卡在 `InnoDB: Waiting for purge` | 上次异常关闭，恢复/回滚中 | 等待；看错误日志；不要强杀 |
| `Too many connections` | `max_connections` 不足/连接泄漏 | 开 `extra_port` 救援；查 `PROCESSLIST` 与池配置 |
| 查询突然变慢 | 统计信息/执行计划变化、锁等待、IO | `EXPLAIN`、`INNODB_TRX`、`iostat`、慢日志 |
| 磁盘满 | binlog/慢日志/临时表/undo 膨胀 | 清理 binlog（`PURGE BINARY LOGS`）、扩盘、查大文件 |
| 从库延迟增大 | 大事务/单线程回放/磁盘慢 | 拆事务、开并行复制、检查 IO |
| `WSREP: failed to reach primary view` | 网络/节点数不足/SST 失败 | 检查集群地址、防火墙、节点状态 |

## 4. 版本切换与升级

### 4.1 升级前

1. 阅读目标版本 release notes 与 incompatible changes。
2. 备份：`mariadb-backup` 全量 + 记录 binlog 位点/GTID。
3. 确认数据格式兼容：**不要用新版本数据目录回退到旧二进制**。
4. 在从库/测试实例先验证：启动、查询、复制、备份。

### 4.2 最小停机升级（主从/多实例）

```
从库：停 → 换二进制 → 启 → 验收 → 追平
主库：停写（或切主）→ 换二进制 → 启 → 验收 → 恢复写入
```

滚动替换时保持至少一个可用节点；Galera 需逐节点升级并等待 `wsrep_ready=ON`。
二进制替换或数据恢复失败时保持该节点停机，不要用旧二进制带半成品数据启动。

### 4.3 回滚

```
# 1) 停实例（unit 不存在时 svc_stop 回退，不会因 systemctl 失败直接中止回滚）
svc_stop || { echo "停止失败，中止回滚" >&2; exit 1; }

# 2) 恢复旧二进制（升级前已备份）
cp -a "${MARIADB_HOME}/bin.bak.<ts>/." "${MARIADB_HOME}/bin/" || exit 1

# 3) 恢复旧数据（若新版本已迁移数据格式）
#    保留当前 DATADIR，向新建空目录 copy-back；复制失败不得启动
#    升级前备份须已按 data-load-and-backup §4 解压（若压缩）+ prepare
DATADIR_BAK="${DATADIR}.bak.$(date +%Y%m%d_%H%M%S)"
[ ! -e "${DATADIR_BAK}" ] || { echo "原目录备份名已存在，中止" >&2; exit 1; }
mv "${DATADIR}" "${DATADIR_BAK}" || exit 1
mkdir -p "${DATADIR}" || exit 1
[ -z "$(ls -A "${DATADIR}")" ] || { echo "copy-back 目标目录非空，中止" >&2; exit 1; }
mariadb-backup --copy-back --target-dir="${BACKUP_ROOT}/pre-upgrade" \
  --datadir="${DATADIR}" || { echo "copy-back 失败，原数据仍在 ${DATADIR_BAK}，不要启动" >&2; exit 1; }
chown -R mysql:mysql "${DATADIR}" || exit 1

# 4) 启动并复验（仅在 copy-back 成功后）
svc_start || { echo "启动失败，请检查错误日志" >&2; exit 1; }
```

规则：

- 升级前必须备份并验证备份可恢复；只备份不验证等于没备份。
- copy-back 失败时保持停机并保留 `*.bak.<ts>` 原目录，不得用旧二进制带半成品数据启动；
  排查后再决定清空重试还是恢复原目录。
- 记录改前/改后版本、md5、停机窗口、验收结果。
- 回滚后重新跑功能冒烟与复制校验，确认没有数据丢失。

## 5. 配置变更纪律

- 在线可改参数：`SET GLOBAL` + 写入 my.cnf（两者都要，否则重启丢失）。
- 需重启参数：`innodb_page_size`、`innodb_data_file_path`、部分 redo/双写配置（以 10.11 为准）；
  `innodb_buffer_pool_instances` 在 10.11 已移除，勿列为可调项。
- 每次只改一个变量，记录改前/改后、验证方式、回滚值。
- 高风险（redo、双写、复制、字符集、页大小）先与用户确认并安排窗口。

## 6. 备份与恢复演练

- 每季度至少一次恢复演练：在隔离实例 `copy-back` 并用计数/校验和比对。
- 演练记录：备份时间、恢复耗时、校验结果、发现的问题。
- 备份保留策略与 binlog 保留要匹配恢复目标（RPO）；binlog 被提前清理会导致
时间点恢复失败。

## 7. 输出要求

运维/故障类回答必须包含：现象、证据（日志/命令输出）、根因、处置、验证、
以及是否会复发（配置/容量/流程层面的修复）。
