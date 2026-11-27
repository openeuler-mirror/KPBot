# my.cnf 参数矩阵与原理

## 1. 先算预算

| 项 | 估算 |
| --- | --- |
| buffer pool | 内存 × 50%–70% |
| 连接/排序/临时 | 并发 × (sort/join/read buffers) + 临时表 |
| redo log buffer | 1–3G |
| binlog cache | 事务大小 × 并发（按需） |
| OS/其他 | 20%–30% |

容器：`--memory` ≥ buffer pool + 连接/排序 + 余量；否则 OOM/被 kill。

## 2. 连接/元数据

| 参数 | 默认/基线 | 何时增大 | 风险 |
| --- | --- | --- | --- |
| `max_connections` | 1200–48000（按规格） | 并发峰值 | 内存/线程暴涨 |
| `back_log` | 2048–4000 | 建连风暴 | 内存 |
| `thread_cache_size` | 接近峰值 | 短连接多 | 内存 |
| `table_open_cache` | 16000–500000 | 表多/并发高 | 内存 |
| `table_open_cache_instances` | 16–64 | 高并发降争用 | 过多分片 |
| `max_prepared_stmt_count` | 1280000–4194304（MySQL 8.0 上限 4194304） | PS 并发高 | 内存/清理 |
| `open_files_limit` | 102400+ | 表/连接多 | 受 ulimit 限制 |

## 3. Buffer Pool

- `innodb_buffer_pool_size`：8U32G→22G，2C8G→4G，8C16G→8G，32C256G→128G。
- `innodb_buffer_pool_instances`：大 pool 8–16；小 pool 4–8。实例过多会增加管理开销。
- `innodb_buffer_pool_chunk_size`：默认 128M；在线调整大小需按 chunk 对齐。
- `innodb_change_buffer_max_size`：写多读少可 25；读多可降。
- 命中率 = 1 - `Innodb_buffer_pool_reads/read_requests`；命中率高仍慢要看 latch/排序/日志。

## 4. redo / binlog / durability

| 参数 | 基线 | 说明 |
| --- | --- | --- |
| `innodb_log_file_size` | 2048M（8U32G） | 大→checkpoint 少、恢复慢 |
| `innodb_log_files_in_group` | 8 | 8.0.30 前；总 redo=size×count |
| `innodb_log_buffer_size` | 2G | 大事务/高并发 |
| `innodb_flush_log_at_trx_commit` | 1 | 1 最安全；2/0 有丢数据风险 |
| `sync_binlog` | 1 | 1 最安全；N/0 更快 |
| `binlog_format` | ROW | 体积大但一致 |
| `binlog_row_image` | FULL | MINIMAL 需评估 |
| `binlog_group_commit_sync_delay` | 0 | 微秒；增大换批次 |
| `binlog_group_commit_sync_no_delay_count` | 0 | 达阈值不再等 |

压测常用 1/1；性能优先改为 2/0 时必须在报告标注“非 durable”。
TLS 默认 `ssl=1`；`ssl=0` 仅在显式选择的隔离压测中可用——要求 TLS 的远端客户端会连接失败，
允许降级的客户端可能明文传输。

## 5. 刷脏/IO

| 参数 | 基线 | 说明 |
| --- | --- | --- |
| `innodb_io_capacity` | 30000 | 按盘实测；过高 IO 竞争 |
| `innodb_io_capacity_max` | 40000（1.33× baseline，本模板取值） | 通常 1.5–2× baseline；按盘实测可低于该倍数，过高 IO 竞争 |
| `innodb_max_dirty_pages_pct` | 90 | 高水位；太高尾延迟/恢复慢 |
| `innodb_max_dirty_pages_pct_lwm` | 0 | 预刷低水位 |
| `innodb_lru_scan_depth` | 9000 | 与 page cleaner/盘 |
| `innodb_page_cleaners` | 16 | 4–16 |
| `innodb_flush_neighbors` | 0 | SSD 通常 0 |
| `innodb_flush_method` | O_DIRECT | 避免 double cache |
| `innodb_doublewrite` | 1 | 原子写确认后可 0 |

## 6. 自旋/线程

| 参数 | Point/RO | RW/WO | 说明 |
| --- | --- | --- | --- |
| `innodb_spin_wait_delay` | 20 | 30 | `ut_delay` 热点时调 |
| `innodb_sync_spin_loops` | 100 | 110 | 与上者联合 |
| `innodb_spin_wait_pause_multiplier` | 5 | 5 | pause 倍数 |
| `innodb_read_io_threads` | 25–35 | 20 | 只读可增 |
| `innodb_write_io_threads` | 5 | 15 | 只读可降 |
| `innodb_purge_threads` | 1 | 4–16 | 长事务/写多 |
| `innodb_thread_concurrency` | 0 | 0 | 默认不限 |

## 7. AHI 与性能模式

- AHI：默认 ON；高 CPU 读多、命中率高、线程密度大可 OFF；低 CPU/随机读保持 ON；
中间区间纳入 A/B。在线 `SET GLOBAL` 可切，但会短时抖动。
- `performance_schema=OFF` 降开销；定位等待/线程时临时开启（需重启）。
- `transaction_isolation=READ-COMMITTED` 可降间隙锁/提升并发，但需业务兼容；
RR 下范围更新更易锁冲突。

## 8. 容器矩阵（来自 2C8G/8C16G/32C256G 基线）

| 参数 | 2C8G | 8C16G | 32C256G |
| --- | --- | --- | --- |
| `innodb_buffer_pool_size` | 4G | 8G | 128G |
| `innodb_buffer_pool_instances` | 4 | 8 | 8 |
| `innodb_log_file_size` | 1G | 2G | 4G |
| `innodb_page_cleaners` | 4 | 8 | 8 |
| `innodb_read_io_threads` | 1 | 4 | 16 |
| `innodb_write_io_threads` | 1 | 4 | 16 |
| `innodb_purge_threads` | 1 | 4 | 16 |
| `max_connections` | 1200 | 4000 | 48000 |
| `table_open_cache` | 1024 | 2048 | 2048 |
| `table_open_cache_instances` | 4 | 16 | 16 |
| `thread_pool_size`（Percona） | 2 | 8 | 32 |

**规格与场景的覆盖规则**：规格表是硬基线；场景线程/表缓存覆盖只作用于 `8u32g`/64t 基线
（`point|ro` = 25/5、500000/64；`rw|wo` = 20/15、16000/16），其它规格的 Point/RO
不覆盖线程/表缓存（`2c8g/ro` = 读/写 1/1、表缓存 1024/4），其它规格的 RW/WO 同样沿用
规格表；`rw|wo` 的 purge 至少 4 且不降低规格基线。
逐组合最终值（`render_mycnf.sh` 实际输出，point 同 ro、wo 同 rw）：

| 规格 | Point/RO 读/写 | RW/WO 读/写 | Point/RO purge | RW/WO purge | Point/RO 表缓存/实例 | RW/WO 表缓存/实例 |
| --- | --- | --- | --- | --- | --- | --- |
| 2C8G | 1/1 | 1/1 | 1 | 4 | 1024/4 | 1024/4 |
| 8C16G | 4/4 | 4/4 | 4 | 4 | 2048/16 | 2048/16 |
| 8U32G | 25/5 | 20/15 | 1 | 4 | 500000/64 | 16000/16 |
| 32C256G | 16/16 | 16/16 | 16 | 16 | 2048/16 | 2048/16 |

## 9. 在线/重启/重编译

| 参数 | 生效方式 |
| --- | --- |
| `max_connections`、`io_capacity`、`spin_wait_delay`、AHI | 动态 `SET GLOBAL` |
| `innodb_buffer_pool_size` | 动态但需 chunk 对齐，可能抖动 |
| `innodb_log_file_size`、`innodb_doublewrite` | 重启 |
| `innodb_page_size`、`lower_case_table_names` | 仅初始化可设：随 datadir 初始化确定；改值重启会因页大小与表空间不匹配导致实例无法启动，须新建实例并迁移数据 |
| 补丁/`-march`/PSI | 重编译 |

## 10. 验证

```
# 写盘/重启前 validate（MySQL 8.0.16+）：语法与取值非法时直接失败，不进入变更窗口
mysqld --defaults-file="${OUT_CNF}" --validate-config

SHOW VARIABLES WHERE Variable_name IN (
 'innodb_buffer_pool_size','innodb_buffer_pool_instances',
 'innodb_log_file_size','innodb_log_buffer_size',
 'innodb_flush_method','innodb_flush_log_at_trx_commit','sync_binlog',
 'innodb_io_capacity','innodb_page_cleaners','innodb_adaptive_hash_index',
 'table_open_cache','table_open_cache_instances','max_prepared_stmt_count',
 'innodb_read_io_threads','innodb_write_io_threads','innodb_purge_threads','ssl');
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';
SHOW GLOBAL STATUS LIKE 'Innodb_log_waits';
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
```

- `max_prepared_stmt_count` 必须 ≤ 4194304；`ssl` 默认 1（仅隔离压测为 0）。
- `render_mycnf.sh --write` 在同目录临时文件写完并校验后原子替换，保留 `.bak` 与原权限/属主；
  写盘失败或中断不会截断目标 my.cnf。
- 改后确认值生效；需重启项必须重启；`innodb_page_size` 只能新建实例。
- 单组变更 + 四场景 A/B；记录改前/改后与回滚。
