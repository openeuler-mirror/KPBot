# MariaDB/InnoDB 配置调优

本文件给出参数的取值思路、影响链与风险。所有“建议值”都是起点，不是承诺；
必须用目标数据与负载做 A/B。

## 1. 内存

| 参数 | 源码默认（10.11） | 调优思路 | 风险 |
| --- | --- | --- | --- |
| `innodb_buffer_pool_size` | 128MB | 热数据工作集的 1.1–1.3 倍，通常占内存 50%–70% | 过大会挤压连接/临时表内存，触发 swap/OOM |
| `innodb_buffer_pool_instances` | 8（受限，**仅 10.5.1 及更早的功能版本**；10.5.2 起弃用并忽略，10.6.0 起移除） | 历史版本每实例 ≥1GB，按核数分片降低争用 | 10.11 写入 my.cnf 会因未知配置项无法启动 |
| `innodb_log_file_size` | 96MB | 先排除 log buffer 不足/刷出滞后，再按写入速率与可接受恢复时间定 1–8GB | 过大恢复慢；10.11 支持在线 resize |
| `innodb_log_buffer_size` | 16MB | 大事务/写密集可增大 | 过大会增加内存与刷盘粒度 |
| `innodb_change_buffering` | none（10.11 默认 `IBUF_USE_NONE`，已标记弃用） | 10.11 默认即关闭；仅在历史版本非唯一二级索引多时才讨论保留 | 开启与弃用状态矛盾，需按版本核对 |
| `innodb_adaptive_hash_index` | OFF（10.11 默认关闭，源码注释 "disabled by default"） | 10.11 默认关闭；等值热点多时评估「开启」并 A/B latch 争用 | 开启可能引入 AHI latch 竞争，收益需实测 |

内存预算：

```
buffer pool + log buffer + 连接内存(max_connections × 每连接) +
临时表/排序缓冲 + Aria pagecache + PFS + OS 预留 ≤ 物理内存
```

## 2. 刷盘与 IO

| 参数 | 源码默认 | 调优思路 | 风险 |
| --- | --- | --- | --- |
| `innodb_flush_method` | `O_DIRECT` | 保持 O_DIRECT，避免双重缓存 | 小 IO 可能变慢；文件系统需支持 |
| `innodb_io_capacity` | 200 | NVMe 2000–8000，SATA SSD 400–1000 | 过高挤占前台 IO |
| `innodb_io_capacity_max` | 自动 | 通常为 capacity 的 2 倍 | 峰值 IO 抖动 |
| `innodb_doublewrite` | ON（10.11 变量为数值：1=ON，0=OFF） | 保持 ON；仅在可接受风险且实测有收益时评估关闭 | 部分页写无法恢复 |
| `innodb_flush_log_at_trx_commit` | 1 | 生产保持 1；2 可提升吞吐但崩溃丢最近事务 | 数据丢失 |
| `sync_binlog` | 0 | 默认 0（OS 崩溃丢 1 秒）；持久性优先显式设 1 | 默认 0 时 OS 崩溃丢 binlog |
| `innodb_read_io_threads`/`write_io_threads` | 4 | NVMe 可增到 8–16 | 过多线程收益递减 |
| `innodb_page_cleaners` | 4（**仅 10.5.1 及更早的功能版本**；10.5.2 起弃用并忽略，10.6.0 起移除） | 历史版本大 buffer pool/写密集可增到 8 | 10.11 写入 my.cnf 会因未知配置项无法启动 |
| `innodb_use_native_aio` | ON | Linux 保持 ON | 某些文件系统/容器需关闭 |

判断顺序：

1. `Innodb_log_waits` 增长 → 它统计“日志缓冲不足、等待刷出”的次数：先查
   `innodb_log_buffer_size` 是否够大、日志刷出速率是否跟得上（大事务容易填满缓冲）；
   再看 checkpoint 压力，据此决定是否扩 `innodb_log_file_size`，而不是直接扩 redo，
   也不是加 buffer pool。
2. 脏页高、`pending writes` → 提高 `io_capacity` 或换更快盘；`page_cleaners` 仅
   10.5.1 及更早的功能版本可调（10.5.2 起弃用并忽略，10.6.0 起移除）。
3. `iostat` await/aqu-sz 高但 `%util` 不高 → 队列/调度/远端存储问题。
4. 写入 P99 抖动且与 checkpoint 周期一致 → redo 太小或刷脏能力不足。

## 3. 连接、线程与表缓存

| 参数 | 源码默认 | 调优思路 | 风险 |
| --- | --- | --- | --- |
| `max_connections` | 151 | 按客户端池总和 + 余量；不是越大越好 | 内存/线程开销 |
| `thread_cache_size` | 256（源码 `DEFAULT(256)`；仅当超过 `max_connections` 时才下调） | 128–512，减少建线程 | 很小 |
| `thread_handling` | one-thread-per-connection | 高并发短查询改 `pool-of-threads` | 长查询占满 worker |
| `thread_pool_size` | CPU 相关 | ≈物理核数（按绑核范围） | 过大增加切换 |
| `thread_pool_stall_limit` | 默认 | 慢查询多时增大，避免频繁补线程 | 过大导致排队 |
| `table_open_cache` | 2000 量级 | 表多/并发高时 4000–20000 | 文件句柄/内存 |
| `table_definition_cache` | 自动 | 表多时增大 | 内存 |
| `extra_port` | 关闭 | 生产开启，救援连接 | 需限制监听地址 |

判断：

- `Threads_created` 持续增长 → `thread_cache_size` 不足。
- `Table_open_cache_misses` 高、`Opened_tables` 增长快 → 增大 table cache。
- `Threads_running` 高但 QPS 不涨 → 锁/IO 等待，不是连接数问题。

## 4. 日志与复制

| 参数 | 源码默认 | 调优思路 | 风险 |
| --- | --- | --- | --- |
| `binlog_format` | `MIXED` | 一致性/CDC 优先 `ROW` | 体积增大 |
| `max_binlog_size` | 1G | 按备份/复制需求 | 过大影响清理粒度 |
| `binlog_commit_wait_count` | 0 | 32–128（按并发）合并 fsync | 单事务延迟增加 |
| `binlog_commit_wait_usec` | 100000（源码 `DEFAULT(100000)`；仅在 `binlog_commit_wait_count>0` 时生效） | 500–2000（微秒，需配合 `binlog_commit_wait_count>0`） | 同上 |
| `sync_binlog` | 0 | 源码默认 0；持久性优先显式设为 1 | 设为 1 降低写吞吐，默认 0 则 OS 崩溃丢 1 秒 |
| `slave_parallel_threads` | 0 | 从库回放瓶颈时开启 | 依赖主库组提交质量 |
| `gtid_strict_mode` | OFF | 需要严格 GTID 时开启 | 运维约束 |

写吞吐优化顺序：先确认持久性要求 → 再考虑组提交 → 最后才考虑降低 `sync_binlog`
或 `flush_log_at_trx_commit`（通常不建议）。

## 5. Aria 与临时表

| 参数 | 调优思路 | 风险 |
| --- | --- | --- |
| `aria_pagecache_buffer_size` | 内部临时表/系统表多时 256M–2G | 与 buffer pool 争内存 |
| `aria_log_file_size` | 大事务/大批量时增大 | 恢复时间 |
| `tmpdir` | 独立盘，避免与数据/redo 争 IO | 磁盘满导致查询失败 |

> `internal_tmp_disk_storage_engine` 是 MySQL 5.7/8.0 变量，MariaDB（含 10.11）**不存在**，
> 写入 my.cnf 会因未知变量启动失败。MariaDB 内部磁盘临时表固定使用 Aria；可调的是
> `tmp_disk_table_size` 与 `tmpdir`，不是引擎选择。

证据：`Created_tmp_disk_tables`、`Created_tmp_tables`、`Sort_merge_passes`。

## 6. 优化器

| 参数/开关 | 用途 | 注意 |
| --- | --- | --- |
| `optimizer_switch` | 开关特定优化策略 | 全局影响；逐项 A/B |
| `optimizer_search_depth` | 复杂 JOIN 搜索深度 | 过大编译时间长 |
| `eq_range_index_dive_limit` | 等值范围估算方式 | 影响执行计划 |
| `statistics`（引擎） | 统计信息 | `ANALYZE TABLE` 后计划可能变化 |

纪律：

- 执行计划突变优先查统计信息/数据分布，而不是关优化器。
- 改 `optimizer_switch` 要记录完整值，便于回滚。
- 升级版本后重新检查计划，不能假设与旧版本一致。

## 7. 监控开关

| 参数 | 建议 | 原因 |
| --- | --- | --- |
| `performance_schema` | 按需开启 | 默认 OFF；开启有开销 |
| `slow_query_log` | 生产开启，`long_query_time` 1–2s | 定位慢 SQL |
| `log_queries_not_using_indexes` | 短时开启 | 日志量大 |
| `innodb_monitor_enable` | 按需启模块 | 计数器开销 |
| `userstat`（插件） | 需要按用户/表统计时 | 额外开销 |

A/B 两腿的监控开关必须一致，否则数字不可比。

## 8. 配置变更流程

1. 记录当前值：`SHOW GLOBAL VARIABLES` 快照。
2. 判断是否动态：`SET GLOBAL` 成功 ≠ 重启后仍生效，必须同步写 my.cnf。
3. 只改一个变量，观察一个场景。
4. 正确性门禁 + 同口径 A/B。
5. 保留则更新配置基线；回滚则恢复原值并重启/热加载。
6. 记录：参数、原值、新值、依据、结果、回滚命令。

## 9. 常见错误配置

| 配置 | 问题 |
| --- | --- |
| buffer pool 占内存 90% | 连接/临时表/OOM 风险 |
| redo 仍为默认 96MB 却做写密集压测 | checkpoint/log waits，写抖动 |
| `innodb_flush_log_at_trx_commit=0` 用于“生产优化” | 崩溃丢数据 |
| `max_connections` 极大但线程模型不变 | 线程爆炸、上下文切换 |
| 只调 `sort_buffer_size` 解决磁盘临时表 | 治标不治本，可能内存爆 |
| 关闭 doublewrite 换取写性能 | 崩溃恢复风险 |
| `Innodb_log_waits` 高就只扩 redo | 未查 `innodb_log_buffer_size`/刷出速率，抖动可能不改善 |
| 10.11 my.cnf 写 `innodb_buffer_pool_instances`/`innodb_page_cleaners` | 未知配置项导致启动失败；这些项仅 10.5.1 及更早的功能版本支持（10.5.2 起弃用并忽略，10.6.0 起移除） |
| PFS 一腿开一腿关 | 性能对比无效 |
