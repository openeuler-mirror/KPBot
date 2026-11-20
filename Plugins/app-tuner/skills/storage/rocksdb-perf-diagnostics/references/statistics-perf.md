# Statistics 与 perf_context

这两套东西回答的问题不同:

- **Statistics** 是**全局累计**的计数与直方图,回答"整体发生了什么、延迟分布如何"。
- **perf_context / iostats_context** 是**线程局部、按次**的耗时拆解,回答"这一次操作慢在哪一段"。

## 一、Statistics

### 开启(C++)

```cpp
#include "rocksdb/statistics.h"

rocksdb::Options options;
options.statistics = rocksdb::CreateDBStatistics();
// 降低开销:默认已是 kExceptDetailedTimers
options.statistics->set_stats_level(rocksdb::StatsLevel::kExceptDetailedTimers);
// 需要完整数据时:
// options.statistics->set_stats_level(rocksdb::StatsLevel::kAll);
```

### `StatsLevel` 取值与取舍

| 级别 | 含义 |
|---|---|
| `kDisableAll` / `kExceptTickers` | 全关 / 只关 ticker |
| `kExceptHistogramOrTimers` | 关 timer 且跳过 histogram |
| `kExceptTimers` | 跳过 timer 统计 |
| `kExceptDetailedTimers` (**默认**) | 除 mutex 内耗时与压缩耗时外都统计 |
| `kExceptTimeForMutex` | 除 mutex 内耗时外都统计 |
| `kAll` | 全开,含 mutex 计时;**可能影响高并发写吞吐** |

诊断尾延迟时通常设 `kAll` 或至少 `kExceptTimeForMutex`;只做趋势观察保持默认即可。

### 读取(C++ / Java)

```cpp
auto* stats = db->GetStatistics();
uint64_t hits = stats->getTickerCount(rocksdb::BLOCK_CACHE_HIT);
std::string h = stats->getHistogramString(rocksdb::Histograms::DB_GET);
auto data = stats->getHistogramData(rocksdb::Histograms::DB_WRITE); // P50/P99/max/count...
std::string all = stats->ToString();
```

```java
Statistics stats = db.getStatistics();          // 或 RocksDB.getStatistics()
long hits = stats.getTickerCount(TickerType.BLOCK_CACHE_HIT);
HistogramData d = stats.getHistogramData(HistogramType.DB_GET);
String h = stats.getHistogramString(HistogramType.DB_GET);
```

Java 还可以用 `StatisticsCollector` 周期性采集,省掉自己起线程。

### 关键 ticker(累计计数)

| ticker | 用途 |
|---|---|
| `BLOCK_CACHE_HIT` / `BLOCK_CACHE_MISS` | 整体 block cache 命中;还有 data/index/filter 细分 |
| `BLOOM_FILTER_USEFUL` | bloom 真正挡掉了多少次读 |
| `NUMBER_KEYS_WRITTEN` / `NUMBER_KEYS_READ` | 业务量基线 |
| `BYTES_WRITTEN` / `BYTES_READ` | 业务吞吐 |
| `COMPACT_READ_BYTES` / `COMPACT_WRITE_BYTES` | compaction 读写字节 → 算写放大 |
| `FLUSH_WRITE_BYTES` | flush 字节 |
| `STALL_MICROS` | 累计停顿微秒数 |
| `READ_AMP_ESTIMATE_USEFUL_BYTES` / `READ_AMP_TOTAL_READ_BYTES` | 读放大(后者/前者) |

判断写放大:`COMPACT_WRITE_BYTES / BYTES_WRITTEN`(粗估,注意口径)。

### 关键 histogram(分布)

| histogram | 用途 |
|---|---|
| `DB_GET` / `DB_WRITE` / `DB_SEEK` / `DB_MULTIGET` | 各操作的端到端延迟分布,看 P50/P99 |
| `COMPACTION_TIME` / `COMPACTION_CPU_TIME` | 单次 compaction 墙钟 / CPU 时间 |
| `FLUSH_TIME` | 单次 flush 时间 |
| `SST_READ_MICROS` | 读 SST 的耗时 |
| `READ_BLOCK_GET_MICROS` | 读 block 的耗时(区分 cache 命中/未命中) |
| `WRITE_RAW_BLOCK_MICROS` | 写 block |
| `WAL_FILE_SYNC_MICROS` | WAL fsync 耗时;持久化慢时看它 |
| `STALL_L0_NUM_FILES_COUNT` / `STALL_MEMTABLE_COMPACTION_COUNT` / `STALL_L0_SLOWDOWN_COUNT` | 按原因的停顿次数 |
| `WRITE_STALL` | 写停顿累计 |
| `BYTES_PER_READ` / `BYTES_PER_WRITE` | 单次读写字节分布,识别大 value |
| `NUM_FILES_IN_SINGLE_COMPACTION` | 单次 compaction 文件数,过高说明层内碎片化 |

直方图优先看 **count / P50 / P99 / max**:P99 远高于 P50 = 尾延迟问题;max 异常大 = 偶发 IO 或调度抖动。

### 周期性输出

- `DBOptions::stats_dump_period_sec`(默认 600):每 N 秒把 `rocksdb.stats` 写进 LOG(见 `log-analysis.md`)。
- `DBOptions::stats_persist_period_sec`(默认 600):把统计持久化,供 `DB::GetStatsHistory` 回看。

## 二、perf_context 与 iostats_context(仅 C++)

### 开启

```cpp
#include "rocksdb/perf_context.h"
#include "rocksdb/perf_level.h"

rocksdb::SetPerfLevel(rocksdb::PerfLevel::kEnableTime);

// ...在目标线程里执行被测操作...

auto* ctx = rocksdb::get_perf_context();
std::string s = ctx->ToString();   // 或逐字段读取
ctx->Reset();                      // 采完复位,避免累计污染
rocksdb::SetPerfLevel(rocksdb::PerfLevel::kDisable);  // 关掉,避免长期开销
```

### `PerfLevel` 与开销

| 级别 | 内容 | 开销 |
|---|---|---|
| `kDisable` | 关闭 | 无 |
| `kEnableCount` | 只计数 | 很低 |
| `kEnableTimeExceptForMutex` | 计数 + 时间(不含 mutex) | 中 |
| `kEnableTimeAndCPUTimeExceptForMutex` | 再加 CPU 时间 | 中高 |
| `kEnableTime` | 计数 + 全部时间 | 高 |

**只在你怀疑的那条路径上短暂开启**,采完立刻降回 `kDisable`。长期开 `kEnableTime` 会明显拖慢高并发。

### 关键字段

`PerfContext` 字段按"读/写/迭代/compaction"分组,常用:

- 读:`user_key_comparison_count`、`block_cache_hit_count` / `block_cache_miss_count`、`block_read_count` / `block_read_byte`、`get_snapshot_time`、`seek_time`、`read_index_block_nanos`、`read_filter_block_nanos`、`new_table_block_iter_nanos`、`block_seek_nanos`。
- 写:`write_wal_time`、`write_memtable_time`、`write_delay_time`、`write_scheduling_flushes_compactions_time`、`write_thread_wait_nanos`。
- 迭代:`iter_next_count`、`iter_seek_time`、`iter_next_time`。
- 锁/调度:`mutex_lock_nanos`、`mutex_wait_nanos`、`thread_wait_nanos`(名字随版本略有差异)。

用法:一次 get 里 `block_read_count` 很高且 `block_cache_hit_count` 很低 → 读放大 / cache 不足;`write_wal_time` 占比高 → 持久化瓶颈;`mutex_wait_nanos` 高 → 锁竞争。

### iostats_context

```cpp
#include "rocksdb/iostats_context.h"
// 同样受 PerfLevel 控制
auto* io = rocksdb::get_iostats_context();
```

字段:`bytes_written`、`bytes_read`、`open_nanos`、`allocate_nanos`、`write_nanos`、`read_nanos`、`range_sync_nanos`、`fsync_nanos`、`prepare_write_nanos`、`logger_nanos`、`cpu_write_nanos`、`cpu_read_nanos`。用来区分"慢在真实 IO"还是"慢在 CPU/压缩/锁"。

## 注意

- perf_context / iostats_context 是**线程局部**:必须在执行操作的线程读取,否则拿到的是空/别的线程的数据;它不是全局聚合,不能当吞吐指标。
- Java 没有 perf_context 绑定。Java 侧用 Statistics 的 histogram + `EventListener` 的 `OnFileReadFinish/OnFileWriteFinish/OnFileSyncFinish` 近似"分段耗时"。
- ticker / histogram 的名称与是否存在**随版本变化**;读到 0 或调用失败先核对版本。
