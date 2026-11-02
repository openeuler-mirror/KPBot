# Properties、工具与回调

这一层是"零改代码就能拿到 RocksDB 内部状态"的主力,也是 Java 侧最可用的深度信息。

## 一、DB properties

```cpp
std::string v;
db->GetProperty("rocksdb.stats", &v);              // 字符串
db->GetProperty(cf, "rocksdb.is-write-stopped", &v);
uint64_t n = 0;
db->GetIntProperty("rocksdb.num-files-at-level0", &n);   // 整数
std::map<std::string, std::string> m;
db->GetMapProperty("rocksdb.cfstats", &m);          // map:rocksdb.cfstats / levelstats / dbstats
```

`GetProperty` 返回 `false` 通常表示**该版本没有这个 property**,不是"值为空"。

### 常用 property(C++ 名 / Java `getProperty`/`getLongProperty` 同一字符串)

| property | 含义 | 用途 |
|---|---|---|
| `rocksdb.stats` | 与 LOG 周期 dump 同内容的完整报告 | 一次拿全量快照 |
| `rocksdb.cfstats` / `rocksdb.levelstats` / `rocksdb.dbstats` | 分 CF / 分层 / DB 级统计(map) | 层形状与 compaction 统计 |
| `rocksdb.is-write-stopped` | 当前是否停写 | 停写现场确认 |
| `rocksdb.actual-delayed-write-rate` | 当前实际限速 | 限速是否生效 |
| `rocksdb.num-files-at-level<N>` | 第 N 层文件数 | 判断 L0 堆积 / 读放大 |
| `rocksdb.estimate-pending-compaction-bytes` | 估算待压缩字节 | compaction 积压 |
| `rocksdb.compaction-pending` | 是否有 compaction 待做 | 0/1 |
| `rocksdb.mem-table-flush-pending` | 是否有 flush 待做 | 0/1 |
| `rocksdb.num-immutable-mem-table` | 未 flush 的 memtable 数 | immutable stall 前兆 |
| `rocksdb.num-running-compactions` | 正在跑的 compaction 数 | 后台是否在干活 |
| `rocksdb.num-running-flushes` | 正在跑的 flush 数 | 同上 |
| `rocksdb.cur-size-all-mem-tables` | 所有 memtable 占用 | 内存压力 |
| `rocksdb.estimate-live-data-size` | 活数据大小 | 空间放大基线 |
| `rocksdb.live-sst-files-size` / `rocksdb.total-sst-files-size` | SST 占用 | 空间放大 |
| `rocksdb.block-cache-usage` / `rocksdb.block-cache-pinned-usage` | 缓存占用 / 钉住 | cache 是否够 |
| `rocksdb.background-errors` | 后台错误数 | 先排正确性 |
| `rocksdb.num-live-versions` / `rocksdb.num-snapshots` | 版本 / 快照数 | 旧版本拖住 compaction |
| `rocksdb.oldest-snapshot-sequence` | 最老快照序列号 | 长快照阻止回收 |

**读放大快捷判断**:`rocksdb.num-files-at-level0` 高 + `estimate-pending-compaction-bytes` 高 + stall 计数高 → compaction 跟不上,不是单纯读慢。

## 二、`ldb` 只读工具

`ldb` 是 RocksDB 自带的离线/在线管理工具,可在不改代码的情况下读状态。

```bash
ldb --db=/path/to/db get_property rocksdb.stats
ldb --db=/path/to/db get_property rocksdb.levelstats
ldb --db=/path/to/db manifest_dump --verbose
ldb --db=/path/to/db list_column_families
ldb --db=/path/to/db list_live_files_metadata
ldb --db=/path/to/db dump_live_files
ldb --db=/path/to/db approxsize
ldb --db=/path/to/db scan --from=... --to=...
```

`ldb` 子命令(不同版本略有增删):`get`、`put`、`batchput`、`delete`、`deleterange`、`scan`、`approxsize`、`get_property`、`dump`、`dump_live_files`、`dump_wal`、`manifest_dump`、`list_column_families`、`list_live_files_metadata`、`list_file_range_deletes`、`file_checksum_dump`、`checkconsistency`、`backup`、`restore`、`checkpoint`、`compact`、`repair`、`reduce_levels`、`change_compaction_style`、`ingest_extern_sst`、`write_extern_sst`、`unsafe_remove_sst_file` 等。

> **破坏性**:`compact`、`repair`、`reduce_levels`、`unsafe_remove_sst_file`、`put`/`batchput`/`delete` 等会改数据或大规模重写。诊断阶段只用只读命令;要执行前必须用户授权 + 备份。

`scripts/collect_properties.sh` 已把常用只读 property 打包成一次采集。

## 三、`sst_dump`

```bash
sst_dump --file=/path/to/db_or_sst [--command=check|scan|raw|verify|identify|recompress] [--show_properties] [--output_hex]
```

- `--show_properties` 输出 SST 的 TableProperties:条目数、数据大小、index/filter 大小、压缩比、key 范围等。用于判断"空间放大来自哪类 SST""bloom/index 是否过大"。
- `--command=check` 做完整性检查。

## 四、EventListener(最精确的实时回调)

```cpp
class MyListener : public rocksdb::EventListener {
  void OnStallConditionsChanged(const rocksdb::WriteStallInfo& info) override {
    // info.cf_name; info.condition.cur/prev ∈ {kNormal,kDelayed,kStopped}
  }
  void OnCompactionCompleted(rocksdb::DB*, const rocksdb::CompactionJobInfo& ci) override {
    // ci.stats: bytes_read / bytes_written / elapsed_micros / num_output_records ...
  }
  void OnFlushCompleted(rocksdb::DB*, const rocksdb::FlushJobInfo& fi) override {}
  void OnBackgroundError(rocksdb::BackgroundErrorReason, rocksdb::Status* bg_error) override {}
  void OnFileReadFinish(const rocksdb::FileOperationInfo& info) override {}   // 逐 IO 耗时
  void OnFileWriteFinish(const rocksdb::FileOperationInfo& info) override {}
  void OnFileSyncFinish(const rocksdb::FileOperationInfo& info) override {}
};
options.listeners.push_back(std::make_shared<MyListener>());
```

价值:

- `OnStallConditionsChanged` 直接告诉你**哪种 stall、发生在哪个 CF**——比从 LOG 推断更准。
- `OnCompactionCompleted` 的 `ci.stats` 是**单次 compaction 的读写字节与耗时**,可逐次算写放大。
- `OnFileReadFinish/WriteFinish/SyncFinish` 的 `FileOperationInfo` 带 `duration`、`offset`、`length`,可定位**是哪类文件、哪个 offset 的 IO 慢**。
- 回调里不要做重活/阻塞,否则会拖慢后台线程;只记录聚合值。

Java 对应 `org.rocksdb.EventListener` / `AbstractEventListener`。

## 五、ThreadStatus(后台线程此刻在干什么)

```cpp
std::vector<rocksdb::ThreadStatus> list;
db->GetThreadList(&list);
for (auto& t : list) {
  // t.GetThreadType(): HIGH_PRIORITY / LOW_PRIORITY / BOTTOM_PRIORITY / USER ...
  // t.GetThreadState(): STATE_UNKNOWN / STATE_MUTEX_WAIT ...
  // t.GetThreadOperation(): OP_COMPACTION / OP_FLUSH / ...
}
```

Java:`rocksdb.getEnv().getThreadList()`。

用来区分"compaction 在忙"还是"compaction 根本没被调度/卡在等待":如果 stall 频繁但 `num-running-compactions` 长期为 0 或线程都在等待,方向是**后台并发不足或上游阻塞**,而不是加压缩参数。

## 六、ColumnFamilyMetaData(LSM 形状)

```cpp
rocksdb::ColumnFamilyMetaData meta;
db->GetColumnFamilyMetaData(&meta);
// meta.size, meta.file_count
// meta.levels[i]: level, size, files[]
//   files[j]: size, smallestkey, largestkey, num_entries
```

Java:`db.getColumnFamilyMetaData()`。

用于:判断每层文件是否过多(读放大)、是否有超大 SST(compaction 粒度)、层大小是否偏离 `max_bytes_for_level_base * multiplier^(L-1)` 的目标。

## 七、CompactionJobStats(单次 compaction)

字段:`elapsed_micros`、`num_input_records`、`num_output_records`、`total_input_bytes`、`total_output_bytes`、`bytes_read`、`bytes_written`、`num_input_files`、`num_output_files`、`is_manual` 等。写放大 ≈ `total_output_bytes / total_input_bytes`(单次口径)。

## 八、db_bench 观测 flag

```bash
./db_bench --benchmarks=fillrandom,readrandom,readwhilewriting \
  --statistics=1 --histogram=1 \
  --perf_level=5 \
  --stats_interval_seconds=10 --stats_per_interval=1 \
  --report_interval_seconds=10 --report_file=report.csv
```

- `--statistics=1`:启用 Statistics。
- `--histogram=1`:打印每次操作的直方图。
- `--perf_level=<n>`:`PerfLevel` 数值(见 `statistics-perf.md`)。
- `--stats_interval_seconds` / `--stats_per_interval`:周期打印统计。
- `--report_interval_seconds` + `--report_file`:周期写 CSV,便于和业务指标对齐。

## 九、Java 映射速查

| C++ | Java |
|---|---|
| `db->GetProperty` | `db.getProperty` |
| `db->GetIntProperty` | `db.getLongProperty` |
| `db->GetMapProperty` | `db.getMapProperty` |
| `db->GetStatistics` | `db.getStatistics` / `RocksDB.getStatistics` |
| `db->GetThreadList` | `db.getEnv().getThreadList()` |
| `db->GetColumnFamilyMetaData` | `db.getColumnFamilyMetaData()` |
| `EventListener` | `org.rocksdb.EventListener` / `AbstractEventListener` |
| `SetDBOptions` / `SetOptions` | `setDBOptions` / `setOptions` |
| **perf_context** | **不支持** |
