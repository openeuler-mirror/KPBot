# 症状 → 证据 → 病因 → 方向

先找症状对应的**证据**,再落到**病因**,最后才谈参数。每一行都给了"验证方法",避免改完不知道有没有效。

## 一、写停顿(write stall)

### A. L0 文件数触发

| | |
|---|---|
| 证据 | LOG:`Stalling/Stopping writes because we have N level-0 files`;`rocksdb.num-files-at-level0` 高于阈值;`Stalls(count)` 里 `level0_numfiles` 高 |
| 病因 | compaction 速度 < 写入产生 L0 的速度;常见于写密集、单条 value 大、或 compaction 线程/带宽不足 |
| 首选方向 | `max_background_jobs`↑;`write_buffer_size`↑(减少 flush 次数);`level0_file_num_compaction_trigger`↓(更早触发);`max_bytes_for_level_base` / `target_file_size_base` 调整 |
| 验证 | 复跑后 `num-files-at-level0` 不再长期贴阈值、`level0_numfiles` stall 降、写 P99 改善,且磁盘写吞吐未打满 |

### B. immutable memtable 触发

| | |
|---|---|
| 证据 | LOG:`Stalling/Stopping writes ... immutable memtables (waiting for flush)`;`rocksdb.num-immutable-mem-table` 高;`rocksdb.mem-table-flush-pending=1` |
| 病因 | flush 跟不上写入;`max_write_buffer_number` 太小;写入突发;flush 被 compaction 抢线程 |
| 首选方向 | `max_write_buffer_number`↑;`max_background_jobs`↑(给 flush 留线程);`write_buffer_size`↑;`db_write_buffer_size` / `write_buffer_manager` 控总量 |
| 验证 | `num-immutable-mem-table` 回落、`memtable_compaction` stall 降、内存占用在预算内 |

### C. pending compaction bytes 触发

| | |
|---|---|
| 证据 | LOG:`... estimated pending compaction bytes ...`;`rocksdb.estimate-pending-compaction-bytes` 高;`Stalls(count)` 里 `pending_compaction_bytes` 高 |
| 病因 | 压缩积压(写放大高 / 压缩线程不足 / 压缩太慢) |
| 首选方向 | 先降写放大(见三);`max_background_jobs`↑、`max_subcompactions`↑;必要时调 `soft/hard_pending_compaction_bytes_limit`(只治标) |
| 验证 | pending bytes 长期下降到阈值下,stall 减少,且 compaction 吞吐可持续 |

## 二、读变慢

### A. 点查 P99 高

| | |
|---|---|
| 证据 | `DB_GET` histogram 的 P99 远高于 P50;`block_cache_hit_count` 低;`READ_AMP_TOTAL_READ_BYTES / READ_AMP_ESTIMATE_USEFUL_BYTES` 高;perf_context 的 `block_read_count` 高 |
| 病因 | 读放大(层内文件多 / L0 多)、cache 不够、bloom 未生效、SST index 仍为二分查找、memtable 未开 prefix bloom、value 过大 |
| 首选方向 | `block_cache`↑;`filter_policy`(bloom)与 `bloom_bits_per_key` 检查;`optimize_filters_for_hits`;`prefix_extractor` + `block_based_table_factory={index_type=kHashSearch;}`(需重写存量 SST);`memtable_prefix_bloom_size_ratio>0`(动态,新 memtable 生效);减少层数/文件数(见写方向);大 value 考虑 BlobDB。详见 `references/optimization-playbook.md` |
| 验证 | P99 下降、cache 命中上升、perf_context 的 `block_read_count` 下降;`sst_dump --show_properties` 的 `prefix_extractor_name` 不是 `nullptr`;`MEMTABLE_HIT/MISS` 改善 |

### B. 范围扫描 / 迭代慢

| | |
|---|---|
| 证据 | `DB_SEEK` / `SST_READ_MICROS` 高;`num.files` 多;prefix extractor 缺失导致反复 seek |
| 病因 | 层内文件多、seq 读放大、prefix bloom 未用 |
| 首选方向 | 设置 `prefix_extractor` + prefix bloom;整理层形状;必要时 `optimize_filters_for_hits` |
| 验证 | seek 次数/耗时下降,扫描吞吐上升 |

### C. block cache 命中低

| | |
|---|---|
| 证据 | `rocksdb.block.cache.hit/miss` 比例低;`rocksdb.block-cache-usage` 接近 capacity;`pinned-usage` 偏高 |
| 病因 | cache 容量不足;工作集大于 cache;pinned 占用导致可用量下降 |
| 首选方向 | 扩 `block_cache`;检查 `cache_index_and_filter_blocks`;减少大块/大 value 挤占 |
| 验证 | 命中率上升、`BLOCK_CACHE_*_MISS` 降、读 P99 改善 |

## 三、写放大高

| | |
|---|---|
| 证据 | `COMPACT_WRITE_BYTES / BYTES_WRITTEN` 高;LOG `Compaction Stats` 的 W-Amp 高;单次 `CompactionJobStats` 的 output/input 高;JOB 频率异常高 |
| 病因 | 层级 compaction 反复重写;`level0_file_num_compaction_trigger` 太小;`max_bytes_for_level_base`/`multiplier` 不合理;压缩太激进;频繁覆盖写 |
| 首选方向 | 调大 `max_bytes_for_level_base` / `max_bytes_for_level_multiplier`;`level_compaction_dynamic_level_bytes=true`;`target_file_size_base` 调大;减少不必要的压缩;评估 `compaction_style`(level vs universal) |
| 验证 | 写放大比下降、compaction 写吞吐下降、业务写 P99 改善,同时空间放大不失控 |

## 四、空间放大高

| | |
|---|---|
| 证据 | `rocksdb.live-sst-files-size` / `total-sst-files-size` 远大于 `estimate-live-data-size`;`sst_dump --show_properties` 看压缩比与老数据;delete 后空间不降 |
| 病因 | 旧版本/快照阻止回收;compaction 不彻底;大量 delete/tombstone;压缩率低 |
| 首选方向 | 检查 `num-live-versions` / `num-snapshots` / `oldest-snapshot-sequence`;manual compaction(需授权);调整压缩算法;FIFO/universal 策略评估 |
| 验证 | SST 总大小相对活数据下降;注意读/写指标是否被拖累 |

## 五、资源背压

### A. IO 饱和

| | |
|---|---|
| 证据 | `iostat -x` 的 util/await 高;`rocksdb.actual-delayed-write-rate` 非 0;histogram `HARD/SOFT_RATE_LIMIT_DELAY_COUNT` 高;iostats 的 `write_nanos` / `fsync_nanos` 高 |
| 病因 | 磁盘带宽/IOPS 打满;fsync 太频繁;压缩与业务抢 IO |
| 首选方向 | `rate_limiter` 限速避免雪崩;`bytes_per_sync` / `wal_bytes_per_sync`;换更高吞吐介质;错峰 compaction |
| 验证 | util 下降、rate limit 延迟消失、业务尾延迟改善 |

### B. CPU 饱和 / 锁竞争

| | |
|---|---|
| 证据 | `perf top` 热点在压缩/比较器;perf_context `mutex_wait_nanos` 高;`rocksdb.db.mutex.wait.micros` 高;线程数很多但吞吐不涨 |
| 病因 | 压缩 CPU 成本高;锁粒度过粗;线程过多导致上下文切换 |
| 首选方向 | 更快的压缩算法/降低压缩级别;`max_subcompactions` 调整;减少共享热 key 的竞争 |
| 验证 | CPU 利用率与吞吐同向变化;锁等待下降 |

## 六、恢复 / 启动慢

| | |
|---|---|
| 证据 | 启动耗时集中在 WAL replay 或读取 MANIFEST;`max_write_buffer_number` / WAL 体积大 |
| 病因 | WAL 大;`avoid_flush_during_recovery=false`;大量未 flush memtable |
| 首选方向 | 控制 WAL 大小;`avoid_flush_during_recovery`;合理 `write_buffer_size`(过大反而恢复慢) |
| 验证 | 重启耗时下降,且正常路径指标不退化 |

## 七、长快照 / 旧版本拖住 compaction

| | |
|---|---|
| 证据 | `rocksdb.num-snapshots` 高、`oldest-snapshot-sequence` 长期不前进、`num-live-versions` 高;空间不回收 |
| 病因 | 业务持有长生命周期快照 / 迭代器 |
| 首选方向 | 排查业务侧快照/迭代器泄漏;不是调参能解决的 |
| 验证 | 快照释放后 `oldest-snapshot-sequence` 前进、compaction 恢复回收 |

## 八、后台错误

| | |
|---|---|
| 证据 | `rocksdb.background-errors > 0`;LOG 中 background / IO / corruption 错误 |
| 病因 | 磁盘故障、文件损坏、Env 问题 |
| 首选方向 | **先修正确性**,再谈性能 |
| 验证 | 错误清零、`ldb checkconsistency` 通过 |
