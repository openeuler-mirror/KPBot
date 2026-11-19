# LOG 日志分析

LOG 是排查 RocksDB 性能问题**最便宜、信息最密**的材料:不需要改代码,历史事件都留着。先把 LOG 读明白,再决定要不要上升级观测。

## LOG 在哪、怎么轮转

- 默认位置:`<db_path>/LOG`;同时存在 `LOG.old.<timestamp>`(按 `max_log_file_size` 轮转)和 `LOG.env`(Env 相关)。
- 自定义位置:`DBOptions::db_log_dir` / `info_log`。
- 级别:`DBOptions::info_log_level`(默认 `INFO_LEVEL`;调试可临时调到 `DEBUG_LEVEL`,但量很大)。
- 每行格式大致为:`YYYY/MM/DD-HH:MM:SS.micro  pid thread LEVEL file:line] message`,消息里通常带 `[<cf_name>]` 和 `[JOB <id>]`。

排查时先按 **时间窗 + column family** 过滤,避免被无关 CF 的日志淹没。

## 必须认识的几类行

### 1) 写停顿(最重要)

```
[cf] Stalling writes because we have 32 level-0 files rate 1048576
[cf] Stalling writes because we have 4 immutable memtables (waiting for flush), max_write_buffer_number is set to 4 rate 1048576
[cf] Stalling writes because of estimated pending compaction bytes 68719476736 rate 1048576
[cf] Stopping writes because we have 40 level-0 files
[cf] Stopping writes because we have 5 immutable memtables (waiting for flush), max_write_buffer_number is set to 4
[cf] Stopping writes because of estimated pending compaction bytes 274877906944
```

含义与对应方向:

| 行 | 触发条件 | 首要方向 |
|---|---|---|
| `Stalling/Stopping ... level-0 files` | L0 文件数达到 `level0_slowdown_writes_trigger`(默认 20)/ `level0_stop_writes_trigger`(默认 36) | compaction 跟不上写入,见 `options-map.md` 的 compaction 组 |
| `Stalling/Stopping ... immutable memtables` | 未 flush 的 memtable 达到 `max_write_buffer_number`(默认 2) | flush 跟不上,常与 `max_background_jobs`、`write_buffer_size` 有关 |
| `Stalling/Stopping ... pending compaction bytes` | 估算待压缩字节超过 `soft` / `hard_pending_compaction_bytes_limit`(默认 64GB / 256GB) | 压缩积压,通常是写放大或 compaction 线程不足 |

`Stalling` 是**限速**(delayed write),`Stopping` 是**完全停写**。出现 `Stopping` 说明问题已经严重,优先级最高。

### 2) 周期性 stats dump(信息密度最高的一段)

当设置了 `options.statistics` 且 `stats_dump_period_sec > 0`(默认 600 秒)时,LOG 会周期性追加 `rocksdb.stats` 内容,典型包含:

```
** Compaction Stats [default] **
Level  Files  Size(MB)  Score  Read(GB)  Rn(GB)  Rnp1(GB)  Write(GB)  Wnew(GB)  Moved(GB)  W-Amp  ...
  0     32     2048.0   1.2     ...
  1      ...
...
Cumulative compaction: 120.5 GB write, 45.2 MB/s write, 240.1 GB read, ...
Interval compaction:   12.3 GB write, ...
Stalls(count): 12 level0_slowdown, 3 level0_slowdown_with_compaction, 5 level0_numfiles, ...
```

怎么读:

- **Score > 1** 表示该层超过目标大小、compaction 有压力;持续 > 1 且 L0 堆积,说明 compaction 追不上。
- **W-Amp** 是该层的写放大;**Cumulative/Interval compaction 的 write 字节**与业务写入量对比,可估整体写放大。
- **`Stalls(count)` 一行是按原因分类的累计 stall 次数**,是判断"到底卡在哪类停顿"最快的一眼。各字段:level0_slowdown、level0_slowdown_with_compaction、level0_numfiles、level0_numfiles_with_compaction、stop for pending_compaction_bytes、slowdown for pending_compaction_bytes、memtable_compaction、memtable_slowdown,最后是 interval 与 total。
- **Interval** 段落反映最近一个统计周期,适合和"最近变慢"对齐时间。

### 3) Compaction / flush 事件

```
[cf] [JOB 123] Compacting level1 -> level2, score 1.05
[cf] Compaction start summary: ...
[cf] [JOB 123] Compacted (L1) (L2) => 1234567890 bytes
[cf] [JOB 123] Compaction ... finished ...
```

- 用 `JOB <id>` 把 "start summary" 与 "Compacted ... => N bytes" 配对,可算**单次 compaction 的输出字节**;配合 start summary 里的输入文件,可估该次写放大。
- **compaction 过于频繁**(单位时间 JOB 数很高)且业务写不大,往往意味着写放大高或 `level0_file_num_compaction_trigger` 太小。
- flush 事件(`flush_started` / `flush_finished`,或在 LOG 中的 flush 行)能看出 memtable 是否频繁被打满;flush 频繁 + immutable memtable stall,指向写入突发或 `write_buffer_size` 偏小。

### 4) 后台错误

`rocksdb.background-errors` 非 0、或 LOG 里出现 background error / IO error / corruption 字样时,先处理正确性问题,再谈性能。

## 用 LOG 回答三个问题

1. **有没有写停顿?** 搜 `Stalling writes` / `Stopping writes`,统计次数与原因,看是 L0、memtable 还是 pending bytes。
2. **compaction 是否在影响读写?** 看 `Compaction Stats` 的 Score、Interval compaction 的读写吞吐、JOB 频率;若 compaction 读/写吞吐接近磁盘上限,而业务延迟同时升高,就是它在抢 IO。
3. **延迟异常是否与后台活动同相?** 把延迟尖刺的时间点与 compaction 密集段对齐;对齐得上,优先怀疑 compaction 抢资源;对不上,再查 cache/锁/前端。

## 注意

- LOG 里的数字是**累计/区间**混合,先分清哪段是 cumulative、哪段是 interval。
- 多 CF 时务必按 `[cf]` 过滤;一个 CF 的 stall 不代表整个 DB。
- 用 `scripts/parse_rocksdb_log.py` 可先自动汇总 stall 次数、原因、compaction 次数与输出字节,再人工看时间线。
