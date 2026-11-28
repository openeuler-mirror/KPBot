# 配置调优原理与场景矩阵

>
> 数值来自鲲鹏 920B/920G 实测（MySQL 8.0.25/8.0.28）。不同内存/核数/数据/压缩/版本会变化；
> 先按瓶颈选杠杆，再用同口径 A/B 验证。完整模板与渲染见
> `mysql-app-config-optimization`。

## 1. 调优顺序

```
① 规格/部署（核/内存/盘/绑核/IRQ）
② 连接/表缓存/PS
③ Buffer Pool
④ redo/binlog/durability
⑤ 刷脏/IO
⑥ 自旋/AHI/线程
⑦ 编译/补丁
```

部署/配置的杠杆通常最大；源码优化在瓶颈被正确归因后才有意义。

## 2. 连接与元数据

| 参数 | 原理 | 只读/Point | RW/WO |
| --- | --- | --- | --- |
| `max_connections` | 连接线程上限 | ≥并发，留余量 | 同 |
| `back_log` | accept 队列 | 高并发建连增大 | 同 |
| `thread_cache_size` | 空闲线程复用 | 接近峰值 | 同 |
| `table_open_cache` | 打开表缓存 | 30000–500000（表多/并发高） | 16000–30000 |
| `table_open_cache_instances` | 分片降争用 | 16–64 | 16 |
| `max_prepared_stmt_count` | PS 上限 | 并发×语句数（如 1280000/4194304） | 同 |
| `open_files_limit` | FD 上限 | ≥表数+连接 | 同 |

注意：`table_open_cache` 过大浪费内存；`max_prepared_stmt_count` 过大增加内存与清理成本。
MySQL 8.0 对该参数有硬上限 4194304（`sql/sys_vars.cc` 中 `num_prepared_stmt_limit`），
配置超过上限会使 mysqld 启动失败；取值不得超过 4194304。

## 3. Buffer Pool

| 参数 | 原理 | 建议 |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 数据页缓存 | 内存 50%–70%；8U32G 约 22G |
| `innodb_buffer_pool_instances` | 分片降低 mutex 争用 | 8–16（大 pool）；小 pool 不要过多 |
| `innodb_buffer_pool_chunk_size` | 分配粒度 | 影响在线调整；默认 128M |
| `innodb_old_blocks_pct/time` | LRU 冷热分区 | 默认通常够用；大扫描可调 |
| `innodb_change_buffer_max_size` | 二级索引延迟合并 | 写多读少可增；读多可降 |

判据：命中率 `1 - reads/read_requests`；未命中高先查工作集/索引，不要只加内存。

## 4. redo / binlog / durability

| 参数 | 原理 | 影响 |
| --- | --- | --- |
| `innodb_log_file_size` | redo 单文件大小 | 大→checkpoint 少、恢复慢；小→频繁 checkpoint |
| `innodb_log_files_in_group` | redo 文件数（8.0.30 前） | 总 redo ≈ size×count |
| `innodb_log_buffer_size` | redo 内存缓冲 | 大事务/高并发可增 |
| `innodb_flush_log_at_trx_commit` | 提交 redo 策略 | 1/2/0 权衡 durability |
| `sync_binlog` | binlog fsync 频率 | 1 最安全；N/0 更快但有丢日志风险 |
| `binlog_format` | 日志格式 | ROW 体积大但一致；STATEMENT 体积小但风险 |
| `binlog_row_image` | 行镜像 | FULL 最安全；MINIMAL 体积小但需评估 |
| `binlog_group_commit_sync_delay` | 组提交延迟 | 微秒级等待换更大批次 |
| `binlog_group_commit_sync_no_delay_count` | 批量阈值 | 达到即不再等 |

压测常见 `1/1`；如果为性能改为 `2/0`，必须在报告中声明“非 durable，崩溃可能丢数据”。

## 5. 刷脏与 IO

| 参数 | 原理 | 建议 |
| --- | --- | --- |
| `innodb_io_capacity` | 后台 IO 基线 | NVMe 可 20000–30000；按盘实测 |
| `innodb_io_capacity_max` | 峰值 IO | 通常 1.5–2× baseline |
| `innodb_max_dirty_pages_pct` | 脏页高水位 | 90（只读基线）；写多可调但注意尾延迟 |
| `innodb_max_dirty_pages_pct_lwm` | 预刷低水位 | 0 或 10；提前刷脏平滑 |
| `innodb_lru_scan_depth` | LRU 扫描深度 | 与 page cleaner/盘匹配 |
| `innodb_page_cleaners` | 刷脏线程 | 4–16；与 pool/盘 |
| `innodb_flush_neighbors` | 邻页刷 | SSD 通常 0 |
| `innodb_flush_method` | O_DIRECT/fsync | O_DIRECT 常用；小 IO 需验证 |
| `innodb_doublewrite` | 页原子性 | 仅所有表空间路径整页原子写逐一确认后可关；任一未确认则保持 ON |

## 6. 自旋与并发

| 参数 | 原理 | Point/RO | RW/WO |
| --- | --- | --- | --- |
| `innodb_spin_wait_delay` | 自旋间隔 | 20 | 30 |
| `innodb_sync_spin_loops` | 自旋次数 | 100 | 110 |
| `innodb_spin_wait_pause_multiplier` | pause 倍数 | 5 | 5 |
| `innodb_thread_concurrency` | 并发上限 | 0（不限） | 0 |
| `innodb_concurrency_tickets` | 票据 | 5000 | 5000 |
| `innodb_read_io_threads` | 读 IO 线程 | 25–35 | 20 |
| `innodb_write_io_threads` | 写 IO 线程 | 5 | 15 |
| `innodb_purge_threads` | purge | 1 | 4–16 |

`ut_delay` 热点：CPU 有富余可增自旋；CPU 饱和/持锁者被 IO 阻塞时，增大自旋可能更差。

## 7. AHI

- 默认 ON；高 CPU、读多、命中率高、线程密度大时可 OFF。
- 判据与上层 `database-workload-analysis` 一致：
  - OFF：`mysqld_cpu_pct>90` + `threads_per_core>4` + `hit_rate>95` + read_only/read_heavy；
  - ON：`cpu<70` 或 `hit_rate<90` 或 `threads_per_core<=2`；
  - 中间：need_more_evidence，纳入 A/B。
- 变更可在线（`SET GLOBAL`），但会有短时抖动；必须在稳态压测下比较。

## 8. 场景矩阵（8U32G，实测）

| 参数 | Point/RO | RW/WO |
| --- | --- | --- |
| `innodb_spin_wait_delay` | 20 | 30 |
| `innodb_sync_spin_loops` | 100 | 110 |
| `innodb_write_io_threads` | 5 | 15 |
| `innodb_read_io_threads` | 25 | 20 |
| `innodb_max_dirty_pages_pct` | 90 | 90 |
| `innodb_max_dirty_pages_pct_lwm` | 0 | 0 |
| `max_prepared_stmt_count` | 4194304 | 4194304 |
| `table_open_cache` | 500000 | 16000 |

64t/20t 模板的其余项（buffer pool、log、io_capacity、page cleaners、AHI）基本一致；
差异主要由数据集/工作集/并发决定，不要只按表数套值。

## 9. 参数变更检查

```
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'innodb_log_file_size';
SHOW VARIABLES LIKE 'innodb_flush_log_at_trx_commit';
SHOW VARIABLES LIKE 'sync_binlog';
SHOW VARIABLES LIKE 'innodb_adaptive_hash_index';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';
SHOW GLOBAL STATUS LIKE 'Innodb_log_waits';
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
```

- 动态参数用 `SET GLOBAL` 后确认 `SHOW VARIABLES`；需重启项必须重启并复验。
- 每次只改一组相关参数；记录改前/改后与回滚命令。
