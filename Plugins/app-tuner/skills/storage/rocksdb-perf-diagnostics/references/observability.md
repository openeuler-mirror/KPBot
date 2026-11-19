# RocksDB 观测面全景

RocksDB 的观测手段比多数存储引擎丰富,而且很多是**零改代码**的。下面按"侵入性 / 成本"从低到高排列,并给出 C++ 与 Java(JNI) 的能力差异。先用低层级拿到大部分结论,不够再往下。

## 阶梯总览

| # | 手段 | 侵入性 | 持续性 | 定位能力 |
|---|---|---|---|---|
| 1 | LOG(`LOG` / `LOG.old.*`) | 无 | 历史 | 写停顿、compaction/flush 事件、后台错误、周期性 stats dump |
| 2 | DB properties(`GetProperty` / `GetIntProperty` / `GetMapProperty`) | 无 | 瞬时 | LSM 形状、pending 工作量、cache 占用、是否 write-stopped |
| 3 | `ldb` / `sst_dump` | 无 | 离线/瞬时 | 零改代码读 property、看 SST 属性、manifest、WAL |
| 4 | Statistics(tickers + histograms) | 低(挂对象) | 累计 | 延迟直方图、stall 计数、IO 字节、cache/filter 命中 |
| 5 | perf_context / iostats_context | 中(改代码 + 开销) | 单线程瞬时 | 单次读写的分段耗时 |
| 6 | EventListener | 中(改代码) | 实时回调 | 精确 stall 原因、每次 compaction/flush/IO 详情、后台错误 |
| 7 | ThreadStatus(`GetThreadList`) | 低(调用) | 瞬时 | 后台线程此刻在 compaction/flush/等待 |
| 8 | ColumnFamilyMetaData / CompactionJobStats | 低(调用) | 瞬时/单次 | 层文件分布、单次 compaction 读写字节与耗时 |
| 9 | StatsHistory(`GetStatsHistory`) | 低(需开启持久化) | 历史序列 | 指标随时间变化,做趋势 |
| 10 | Trace / IO tracer(`StartTrace` / `StartIOTrace`) | 高 | 单次录制 | 真实请求回放、逐操作 IO 时间线 |
| 11 | 系统层(iostat / perf / blktrace / /proc) | 无 | 实时 | 磁盘饱和、CPU 热点、调度与锁 |

## C++ 与 Java 能力矩阵

| 能力 | C++ | Java(JNI) | 说明 |
|---|---|---|---|
| LOG 分析 | ✅ | ✅ | 语言无关,读文件即可 |
| `GetProperty` / `GetIntProperty` | ✅ | ✅ `getProperty` / `getLongProperty` / `getMapProperty` | 名称随版本变化 |
| Statistics(tickers/histograms) | ✅ | ✅ `Statistics` | Java 有 `TickerType` / `HistogramType` / `HistogramData` |
| `StatsLevel` | ✅ | ✅ `StatsLevel` | 降低开销 |
| 周期性统计收集 | 自己写 | ✅ `StatisticsCollector` | Java 现成 |
| perf_context / `PerfLevel` | ✅ | ❌ 未绑定 | Java 需靠 Statistics + EventListener 近似 |
| iostats_context | ✅ | ❌ | 同上 |
| EventListener | ✅ | ✅ `EventListener` / `AbstractEventListener` | Java 可拿到 compaction/stall 回调 |
| ThreadStatus | ✅ `GetThreadList` | ✅ `Env.getThreadList()` | |
| ColumnFamilyMetaData | ✅ | ✅ | |
| CompactionJobStats | ✅ | ✅ `CompactionJobStats` | |
| StatsHistory | ✅ | 视绑定 | |
| Trace / IO tracer | ✅ | 部分(`TraceOptions` / `TraceWriter`) | 依赖版本 |

结论:**Java 路线优先 LOG + properties + Statistics + EventListener**;需要"单次读慢在哪一段"这种细粒度证据时,只能回到 C++ 侧或加自定义计时。

## 各观测面要点

### 1. LOG
见 `log-analysis.md`。这是排查写停顿和 compaction 影响最直接的材料,且不需要改任何代码。

### 2. DB properties
见 `properties-tools.md`。关键点:很多项既有字符串版也有整数版;`GetProperty` 返回 `false` 通常意味着**当前版本没有这个 property**,先核对版本。

### 3. Statistics
见 `statistics-perf.md`。ticker 是累计计数,histogram 是分布;判断"延迟是否异常"用 histogram,判断"发生了什么"用 ticker。

### 4. perf_context / iostats_context
见 `statistics-perf.md`。**线程局部**且只统计当前线程,必须在目标线程里读取;开启 `kEnableTime` 有可观开销,采完要降回 `kDisable`。

### 5. EventListener
见 `properties-tools.md`。适合"要精确知道每次 stall / compaction 的细节",代价是回调里不能做重活。

### 6. ThreadStatus
`db->GetThreadList(&list)`(Java `env.getThreadList()`)返回每个后台线程的类型与状态(compaction / flush / 等待)。用来回答"compaction 是真在跑,还是被卡住/没被调度"。

### 7. ColumnFamilyMetaData
`db->GetColumnFamilyMetaData(&meta)`(Java `getColumnFamilyMetaData()`)给出每层、每个 SST 的大小与 key 范围。用于判断读放大与层形状是否健康。

### 8. CompactionJobStats
在 `OnCompactionCompleted` 里拿,或在 compaction 相关 property 里看;包含 `bytes_read`、`bytes_written`、`elapsed_micros`、`num_output_records` 等,可直接算单次 compaction 的写放大。

### 9. StatsHistory
`db->GetStatsHistory(start, end, &iter)` 取持久化的统计快照(需 `stats_persist_period_sec` 配合)。适合没有外部监控、想回看趋势的场景。

### 10. Trace / IO tracer
`DB::StartTrace` 录制真实请求,`trace_analyzer` 离线分析;`StartIOTrace` + `io_tracer_parser` 拿到逐 IO 时间线。用于"线上偶发慢、复现不了"的定位,成本高,最后再考虑。

### 11. 系统层

```bash
iostat -x 1            # 磁盘吞吐 / await / util,判断是否 IO 饱和
pidstat -d 1 -p <pid>  # 进程级读写
perf top -p <pid>      # CPU 热点(压缩、比较器、锁)
perf record -g -p <pid> # 火焰图
vmstat 1               # 内存 / 换页 / 上下文切换
cat /proc/<pid>/io     # 进程读写字节
cat /proc/<pid>/status # VmRSS / Threads
```

系统层的结论要和 RocksDB 内部指标对齐才可信:例如 `iostat` 显示磁盘 util 100% + `rocksdb.actual-delayed-write-rate` 被触发,才能说"IO 饱和引起背压"。

## 版本注意

- property 名、ticker/histogram 枚举、perf 字段在不同大版本间会增删改名。
- 读取前先确认版本;读到空值或 `false`,先怀疑"该版本没有",而不是"值为 0"。
- 拿不准时以该版本源码 `include/rocksdb/` 与 `monitoring/` 为准。
