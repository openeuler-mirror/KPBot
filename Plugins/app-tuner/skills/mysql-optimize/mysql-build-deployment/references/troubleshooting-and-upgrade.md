# 故障排查、备份、升级与回滚

## 1. 启动类故障

| 症状 | 可能原因 | 处置 |
| --- | --- | --- |
| `cannot allocate memory for the buffer pool` | buffer pool 超过可用内存/cgroup 限制 | 降到内存 50%–70%；容器检查 `--memory` 与 cgroup |
| `Can't connect ... through socket` | socket 路径不一致；残留 `.sock`/`.sock.lock` | 核对 my.cnf；确认无进程占用后删除残留文件 |
| `component_reference_cache.so` 缺失 | `basedir` 错误；只复制了 mysqld 未带 `lib/plugin` | 修正 basedir 或完整安装 |
| 启动即退出/版本不匹配 | datadir 来自不兼容版本 | 恢复匹配版本备份；或重新初始化（确认可丢数据） |
| `Address already in use` | 端口被占；多实例 socket/datadir 冲突 | `ss -lntp` 定位；隔离端口/目录 |
| `Too many open files` | ulimit/systemd LimitNOFILE 过低 | 提高 nofile 与 `open_files_limit` |
| 权限拒绝 | datadir/log/socket 属主不是运行用户 | `chown -R ${MYSQL_USER}`，检查父目录权限 |
| 大页不足 | `large_pages`/`innodb_buffer_pool` 与 hugepages 不匹配 | 增加 hugepages 或关闭 large_pages |

通用步骤：

1. 看错误日志：`tail -n 200 ${DATA_ROOT}/log/mysql.log`。
2. 看进程/端口/socket：`ps -ef | grep [m]ysqld`、`ss -lntp`。
3. 复核配置：`mysqld --defaults-file=... --verbose --help | head`（只解析，不启动）。
4. 最小化启动：临时去掉可疑参数，确认是配置还是数据问题。
5. 不在未备份时执行 `--initialize` 或删除 datadir。

## 2. 连接与认证类

- `Access denied`：账号 host 匹配、密码插件（`caching_sha2_password` vs
`mysql_native_password`）、`skip_name_resolve` 与 DNS。
- `Too many connections`：提高 `max_connections`，检查连接泄漏/长事务。
- `Host is blocked`：`max_connect_errors` 与失败连接；确认后 `FLUSH HOSTS`。
- SSL 握手失败：证书/`ssl=0`/客户端 TLS 版本；生产不要长期关闭 TLS。

## 3. 压测类故障

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 客户端 `Too many open files` | 压测端 nofile 低 | `ulimit -n` 提高；systemd 服务同样设置 |
| `mysql_stmt_prepare() failed` 1461 | `max_prepared_stmt_count` 不足 | 提高参数；或降低并发/复用 PS |
| 服务端 CPU 打不满 | 客户端瓶颈/网络/并发不足 | 查客户端 CPU 与网卡；增加压测机；提高并发 |
| 中断核满载 | IRQ 与业务未分离或队列不均 | 调整绑核/队列；`/proc/interrupts` 核对 |
| 写场景越跑越慢 | 脏页/checkpoint/IO 未归零 | 每轮前等 IO 与 mysqld CPU 降下来；检查 io_capacity |
| 结果波动大 | 数据未重置/预热不足/其他负载 | 固定数据快照、预热、隔离环境 |

客户端/服务端判别顺序：

1. 客户端 `top`/`mpstat` 是否先打满；sysbench 进程 CPU 接近 100%（单核打满）说明客户端瓶颈。
2. 客户端网卡 `sar -n DEV 1`、`ethtool ${ETH}` 速率/丢包。
3. 服务端 IRQ 核是否满载；业务核是否打满。
4. 最后才调 MySQL 参数或加并发。

## 4. 备份

### 4.1 逻辑备份

```
mysqldump --defaults-extra-file="${CRED_FILE}" \
  --single-transaction --routines --triggers --events \
  --databases <db1> <db2> > "${BACKUP_ROOT}/dump-$(date +%Y%m%d%H%M).sql"
```

- `--single-transaction` 对 InnoDB 一致性快照；DDL 期间不保证。
- 大库恢复慢；适合中小库/跨版本迁移。

### 4.2 物理备份

- 使用 XtraBackup/企业备份工具（不在本 skill 内提供）：热备 + prepare + 恢复；prepare 后必须确认可恢复到停写点。
- 或停写/停服后拷贝 datadir + binlog；必须保证同一时点、文件完整。
- 记录：binlog 文件名/位置、GTID、版本、页大小、字符集、账号。
- 备份完成到停写点之间若仍有提交：保留并验证该段 binlog（或停写后重做备份）；未验证的快照不能作为升级/回滚恢复点。

### 4.3 备份校验

- 恢复到隔离实例并 `CHECK TABLE`/计数抽样。
- 记录备份大小、耗时、校验和；没有校验的备份不视为可用。

## 5. 升级

```
评估兼容性 → 停写/只读 → 一致性备份并确认最终恢复点 → 替换二进制 → 启动 → 功能/性能验收 → 开放流量
```

- 先停写再做一致性备份：备份完成到停写点之间的事务必须靠已验证的 binlog 覆盖，或停写后重新备份；否则恢复快照会丢失已确认写入，不得替换二进制。
- 同大版本小版本：通常原地替换二进制即可；仍建议备份。
- 跨大版本：按官方升级路径（如 5.7→8.0 的 precheck/upgrade），不要直接替换。
- 升级前确认：数据字典版本、字符集、认证插件、复制拓扑、备份工具兼容。
- 记录改前/改后版本、md5、停机窗口、验收结果。
## 6. 回滚

1. 停写/停服（`FLUSH TABLES WITH READ LOCK` 或正常 shutdown），记录当前 GTID/binlog 位点。
2. 确认恢复点：快照早于停写点时，用已验证的 binlog 前滚到停写点（或明确接受并获确认的数据丢失）；未完成不得替换二进制。
3. 恢复旧二进制与旧 my.cnf（`bin.bak.<ts>`、`my.cnf.bak.<ts>`）。
4. 恢复升级前数据快照（物理/逻辑），确保与旧版本兼容。
5. 启动，跑功能冒烟 + 关键性能基线。
6. 记录回滚原因、耗时、残留风险。

>
> 只有“旧二进制 + 旧数据快照”同时具备才可声称可回滚；仅回退二进制而保留新数据，
> 旧版本可能无法启动。

## 7. 变更记录模板

```
时间：
目标：
改前：版本/md5/配置关键项/数据状态
改后：版本/md5/配置关键项/数据状态
备份：路径 + 校验
验证：命令 + 结果
回滚：命令 + 预计耗时
风险与确认人：
```
