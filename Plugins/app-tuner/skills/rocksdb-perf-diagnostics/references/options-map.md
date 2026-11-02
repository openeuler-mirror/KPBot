# 参数地图(按放大类型归类)

默认值来自 RocksDB 6.26;其他版本请以该版本 `include/rocksdb/options.h`、`advanced_options.h`、`table.h` 为准。**先用症状表定位,再从对应组里挑一个**,不要整组照抄。

可动态性说明:多数 memtable / compaction 阈值可通过 `SetOptions` / `SetDBOptions` 在不重启时修改(C++/Java 均有);表级选项(block cache、filter、压缩、block size)通常需要重启或重建 SST。以 `SetOptions` 的返回状态为准。

## 一、Memtable / 写路径

| 参数 | 默认 | 作用 | 代价 / 风险 |
|---|---|---|---|
| `write_buffer_size` | 64MB | 单个 memtable 大小;越大 flush 越少、写放大越低 | 内存↑、崩溃恢复(WAL replay)变慢 |
| `max_write_buffer_number` | 2 | 允许的 memtable 总数,影响 immutable memtable stall | 内存↑;过大可能掩盖 flush 不足 |
| `min_write_buffer_number_to_merge` | 1 | flush 前至少合并几个 memtable | 过大会延迟 flush、增加内存 |
| `db_write_buffer_size` | 0(不限) | 跨 CF 共享的 memtable 总量上限 | 与 `write_buffer_manager` 二选一 |
| `max_write_buffer_size_to_maintain` | 0 | 保留历史 memtable 字节以加速快照读 | 内存↑ |
| `allow_concurrent_memtable_write` | true | 多线程并发写 memtable | 关闭会显著降写吞吐 |
| `enable_pipelined_write` | false | 流水线写,降低写延迟 | 语义/兼容性需确认 |
| `two_write_queues` | false | WAL 与 memtable 双队列 | 增加复杂度 |

## 二、Compaction(写放大的主战场)

| 参数 | 默认 | 作用 | 代价 / 风险 |
|---|---|---|---|
| `max_background_jobs` | 2 | 后台 flush + compaction 总线程 | 太高抢 CPU/IO、影响前端 |
| `max_background_compactions` | -1(由上面推导) | compaction 并发 | 老参数,优先用 `max_background_jobs` |
| `max_background_flushes` | -1 | flush 并发 | 同上 |
| `max_subcompactions` | 1 | 单次 compaction 拆成几个子任务并行 | 提高 CPU/IO 占用,可能加剧抖动 |
| `level0_file_num_compaction_trigger` | 4 | L0 达到几个文件触发 compaction | 太小 → compaction 频繁、写放大高;太大 → 读放大与 stall 风险 |
| `level0_slowdown_writes_trigger` | 20 | L0 达到几个文件开始限速 | 太小 → 过早限速;太大 → 停写风险 |
| `level0_stop_writes_trigger` | 36 | L0 达到几个文件完全停写 | 同上 |
| `soft_pending_compaction_bytes_limit` | 64GB | 待压缩字节软限,触发限速 | 治标,不解决写放大 |
| `hard_pending_compaction_bytes_limit` | 256GB | 待压缩字节硬限,触发停写 | 同上 |
| `max_bytes_for_level_base` | 256MB | L1 目标大小 | 调大 → 层更大、写放大可能降但 compaction 更重 |
| `max_bytes_for_level_multiplier` | 10 | 每层放大倍数 | 影响层数与写/读放大平衡 |
| `target_file_size_base` | 64MB | L1 目标文件大小 | 调大 → compaction 粒度大、峰值高 |
| `target_file_size_multiplier` | 1 | 每层文件大小倍数 | 同上 |
| `level_compaction_dynamic_level_bytes` | false | 动态层级大小 | 适合非均匀/增量数据,改变层布局 |
| `compaction_style` | `kCompactionStyleLevel` | level / universal / FIFO | universal 降写放大但空间/读放大权衡 |
| `compression` | Snappy(若支持) | 压缩算法 | 更高压缩 → CPU↑、写放大可能变 |

## 三、读路径

| 参数 | 默认 | 作用 | 代价 / 风险 |
|---|---|---|---|
| `block_cache`(`BlockBasedTableOptions`) | 未设(内部小缓存) | 数据块缓存,**读性能第一杠杆** | 内存;需按工作集设定 |
| `block_size` | 4KB | 数据块大小 | 大块 → 顺序读好、点查浪费;小块 → 索引/查找开销 |
| `filter_policy` + `bloom_bits_per_key` | 通常 10 bits | 布隆过滤器,减少无效读 | 内存↑;过高收益递减 |
| `whole_key_filtering` | true | 全键 bloom | 一般保持开启 |
| `optimize_filters_for_hits` | false | 优化"大多数查找命中"的场景 | 只在命中率极高时有意义 |
| `cache_index_and_filter_blocks` | 视配置 | index/filter 是否进 block cache | 开启可省内存但可能增加 miss |
| `max_open_files` | -1 | 打开文件数上限 | -1 = 全开;受限环境需调整 |
| `prefix_extractor` | 无 | 前缀 bloom / 前缀 seek 优化 | 只对前缀访问模式有效 |
| `pin_l0_filter_and_index_blocks_in_cache` | false | L0 filter/index 常驻 | 内存换延迟 |

读密集点的顺序通常是:`block_cache` → bloom/filter → 降层数/文件数(靠 compaction 组)→ 大 value 考虑 BlobDB。

## 四、IO / 同步 / 限速

| 参数 | 默认 | 作用 | 代价 / 风险 |
|---|---|---|---|
| `bytes_per_sync` | 0(关) | 每写多少字节做一次 sync,SST 写 | 影响写回平滑度 |
| `wal_bytes_per_sync` | 0(关) | 同上,针对 WAL | |
| `strict_bytes_per_sync` | false | 严格限制未 sync 字节 | 提高可预测性,可能降吞吐 |
| `rate_limiter` | 无 | 限制后台 compaction/flush 的 IO 速率 | 保护前端,但可能拖慢 compaction |
| `delayed_write_rate` | 0(自动) | stall 时的限速值 | 太小 → 写入过慢 |
| `use_direct_io_for_flush_and_compaction` | false | 绕过 page cache | 避免双缓存,但需对齐/环境支持 |

## 五、空间

| 参数 | 默认 | 作用 | 代价 / 风险 |
|---|---|---|---|
| `bottommost_compression` | 未设 | 最底层用更强压缩 | CPU↑、compaction 变慢 |
| `compaction_style`(FIFO/universal) | level | 影响空间回收与放大 | 换策略是大事,需完整回归 |
| `max_write_buffer_size_to_maintain` | 0 | 保留历史 memtable | 内存换快照读 |

## 动态修改示例

```cpp
// C++:动态改
db->SetDBOptions({{"max_background_jobs", "8"}});
db->SetOptions(cf, {{"write_buffer_size", "268435456"},
                    {"level0_slowdown_writes_trigger", "24"}});
```

```java
// Java:动态改
db.setDBOptions(new MutableDBOptions(new HashMap<String, String>() {{
  put("max_background_jobs", "8");
}}));
db.setOptions(new MutableColumnFamilyOptions(new HashMap<String, String>() {{
  put("write_buffer_size", "268435456");
}}));
```

## 常见组合陷阱

- **只加 `max_background_jobs` 不降写放大**:前台不 stall 了,但 compaction 总量没变,只是把问题推迟,并抢走更多 CPU/IO。
- **同时调大 `write_buffer_size` 和 `max_write_buffer_number`**:内存可能成倍上涨,先把 `db_write_buffer_size` / 预算算清。
- **调大 `level0_file_num_compaction_trigger` 换低写放大**:L0 变多 → 点查读放大和 stall 风险上升,必须同时看 `DB_GET` P99。
- **放任 `soft/hard_pending_compaction_bytes_limit` 调大**:只是延后停写,不解决根因。
- **压缩算法/等级一把梭**:CPU 成为新瓶颈,需用 `perf` 与 `COMPACTION_CPU_TIME` 验证。
