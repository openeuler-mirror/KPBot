# 配置与部署调优

> 数值来自鲲鹏 950 / LDBC SF10/SF100 / Nebula 3.8.0 实测。不同数据/版本/部署会变化，
> 先按瓶颈选杠杆，再用同口径 A/B 验证。

## 1. 调优顺序

```
① 部署拓扑（实例数/绑核/分区）
② 线程/队列
③ 内存（block cache/write buffer/水位）
④ 写路径（WAL/compaction/磁盘）
⑤ 运行时开关（optimizer/MemoryTracker/日志）
⑥ 编译/第三方（LSE/LTO/jemalloc）
⑦ 源码（容器/拷贝/缓存）
```

部署/配置的杠杆通常最大；源码优化在瓶颈被正确归因后才有意义。

## 2. 部署拓扑

### 2.1 graphd 实例数

实测（Go1，1P，2 storaged）：

| 部署 | 单实例 worker | 总 QPS | graphd futex | 新瓶颈 |
|------|--------------|-------:|-------------:|--------|
| 2 graphd | 48 | 189,679 | 59.4% | futex |
| 4 graphd（worker 同时 48→16） | 16 | 273,249（+44%） | 20.9% | folly Future / 内存 / storaged |

规律：

- +44% 是**组合配置**的结果（实例 2→4 且单实例 worker 48→16 同时生效），不能单独归因到
  实例数；要归因实例数，需补一组 worker 口径固定（仍为 48）的对照实验。
- 轻查询高 QPS：增加 graphd 实例、降低单实例 worker，能显著降低队列争用。
- 继续增加 VU 只增加排队；QPS 平台化、p99 暴涨。
- graphd 增多后 storaged 承受更高并发 RPC，需要同步增加 storaged/读池。

### 2.2 storaged 数量与读池

- Go2/Go3/FSP 的瓶颈多在 storaged；增加 storaged 可分摊 RocksDB 扫描与 schema 拷贝。
- 深查询（Go3）对读池容量敏感；读池不足时高 VU 出现失败/长尾。
- 增加 storaged 也增加 graphd 的 RPC 扇出与内存、磁盘数量要求。
- 分区数不是越多越好：SF100 上 96 与 192 分区全场景 ±3%，192 分区导入耗时约 4 倍；
  默认优先 96。

### 2.3 绑核 vs 浮动

实测（SF100，8 graphd + 4 storaged）：

| 场景 | 全浮动 | 固定绑核（进程级） | 线程级绑核 |
|------|-------:|------------------:|-----------:|
| Go1 | 315K / 93.9% | 337K / 80.6% | 375K / 76.8% |
| Go2 | **18.0K** / 97.6% | 15.5K / 76.7% | 11.5K / 52.8% |
| Go3 | **216** / 97.6% | 154 / 68.6% | 105 / 46.8% |
| FSP | **12.2K** / 98.0% | 9.0K / 69.2% | 7.4K / 54.5% |
| Insert | 36.2K | 33.7K | 27.8K |

结论：

- 绑核收益 = cache 局部性；代价 = 失去跨场景弹性。
- 点查型固定负载可绑核；混合负载优先浮动。
- 线程级 1:1 绑死通常负收益（突发负载下核搁浅）。
- 任何布局都用同一压测口径验证；不要只看 CPU 利用率。

### 2.4 分区与副本

| 参数 | 建议 | 影响 |
|------|------|------|
| `partition_num` | SF10 24–96；SF100 96 | 太少限制并行；太多增加导入/元数据/扫描 |
| `replica_factor` | 单机验证 1；生产 ≥3 | 副本增加写放大、网络、磁盘 |
| `vid_type` | 优先 INT64；必须字符串时选合适长度 | 字符串 VID 增加哈希/比较/内存 |

## 3. 线程与队列

| 参数 | 建议 | 说明 |
|------|------|------|
| `num_worker_threads`（graphd） | 16–24（多实例）/ 48（少实例） | 轻查询高 QPS 时过多加剧单队列争用 |
| `num_netio_threads`（graphd） | 8–16 | 序列化/网络热点可试 16 |
| `num_worker_threads`（storaged） | 8–48 | 读密集可增；写密集先看 WAL/compaction |
| `num_io_threads`（storaged） | 8–16 | 网络收发 |
| `reader_handlers`（storaged） | 8–32 | 与读并发/核数匹配；过多争抢 |
| `query_concurrently` | true | 关闭实测 -0.8%（单跳无收益） |

原则：

- 线程数要跟“实例数 × 每实例核数”匹配；不要所有实例都按全机核数配置。
- 队列争用看 `try_dequeue/waitOnTask/add`；RPC 等待看 Future/CPU 低；RocksDB 看迭代/比较。
- 一次只调一个线程参数，观察热点是否迁移。

## 4. 内存与 RocksDB

| 参数 | 建议 | 实测/注意 |
|------|------|----------|
| `rocksdb_block_cache` | 8–32GB/进程 | 覆盖工作集后收益递减；过大 LRU 管理开销增加 |
| `disable_page_cache` | true（大 cache） | 避免 OS page cache 与 block cache 双重缓存；需 O_DIRECT |
| `write_buffer_size`（非 flagfile 参数） | 64–256MB | 3.8.0 从 `rocksdb_column_family_options` 的 JSON 读取，写在 storaged flagfile 顶层会报未知参数；示例：`--rocksdb_column_family_options={"write_buffer_size":"134217728","max_write_buffer_number":"4"}`（单位字节） |
| `max_write_buffer_number` | 4 | 与写入速率匹配 |
| `max_bytes_for_level_base` | 256MB–1GB | 影响 L1 容量/compaction |
| `rocksdb_block_based_table_options.block_size` | 8–16KB | 大 block 减少索引开销，但读放大 |
| `rocksdb_compression` | lz4（吞吐）/ zstd（空间） | 压缩 CPU 与空间权衡 |
| `enable_rocksdb_prefix_filtering` | true | 前缀扫描场景重要 |
| `enable_rocksdb_whole_key_filtering` | false（点查可开） | 与查询模式匹配 |
| `system_memory_high_watermark_ratio` | 0.8–0.9 | 与 cache/并发一起规划 |
| `memory_tracker_limit_ratio` | 0.8 | 同上 |

注意：

- RocksDB 参数分三层：DB 级（如 `max_background_jobs`）、ColumnFamily 级（`write_buffer_size`/
  `max_write_buffer_number`/`max_bytes_for_level_base`，经 `rocksdb_column_family_options` JSON）、
  BlockBasedTable 级（`block_size`，经 `rocksdb_block_based_table_options` JSON）；把 ColumnFamily
  参数直接写进 flagfile 顶层会报未知参数。
- Block Cache 是进程级共享，不是每 Space 一份。
- 压测前先预热，让工作集进入 cache；否则第一轮数字偏低。
- 盲目调大 write buffer 可能增加 flush/compaction 长尾（实测 Insert -25.4%）。
- 内存不足会 OOM/被 kill，先算清 cache + buffer + 连接 + 结果集上限。

## 5. 写路径：WAL 与 compaction

| 参数/动作 | 建议 | 影响 |
|-----------|------|------|
| `wal_ttl` | 只读压测 60；生产按恢复需求 | 减少 WAL 清理/保留开销 |
| `wal_sync` | 3.8.0 上游默认 false；实验基线显式取 true 并记录 | false 相对 true 实测 Insert -23.6%（compaction 增加）；A/B 两端须分别显式配置并记录实际生效值 |
| `max_background_jobs` | 2–4（避免与前台争 CPU） | 影响 compaction/flush 并行 |
| `max_subcompactions` | 1–2 | 过大抢占前台 |
| `rocksdb_batch_size` | 4096 | 与写入批量匹配 |
| 导入后 `SUBMIT JOB COMPACT` | 是 | 减少后台 flush/compaction 对压测干扰 |
| 批量写入 | batch 100+ | 降低解析/每批 RPC/写放大 |

写入瓶颈的判据：

- CPU 不高但 p99 高：先看磁盘 await/%util、WAL fsync、compaction。
- `WriteThread::AwaitState` 高：写线程等待，可能是 WAL/磁盘/compaction。
- `LZ4_compress`/`BlockBuilder` 高：compaction CPU 抢占。
- 只读场景不要保留大 WAL 配置；生产不要为压测改动 `wal_sync`（3.8.0 上游默认即为 false）后
  忘记恢复——A/B 报告须记录两端的实际生效配置。

## 6. 运行时开关

| 开关 | 实测 | 适用 | 风险 |
|------|------|------|------|
| `ENABLE_MEMORY_TRACKER=OFF`（CMake **构建选项**，非运行开关：须 `-DENABLE_MEMORY_TRACKER=OFF` 重新编译；改运行配置+重启同一二进制无法切换） | Go1 +2.5% | 压测/性能对比；A/B 两端须记录各自构建身份（commit/二进制 md5） | 内存问题难定位 |
| `--enable_optimizer=false` | Go1 +2.6% | 简单 GO/固定语句 | 复杂 MATCH 可能变慢 |
| `--minloglevel=3` | Go1 +0.6% | 压测 | 丢失诊断日志 |
| `--slow_query_threshold_us` 调大 | 减少慢查询日志 | 高 QPS | 观测能力下降 |
| 关闭 `enable_space_level_metrics` | 少量 | 压测 | 监控缺失 |

这些是“减少观测/管理开销”的杠杆，不是业务优化；生产要权衡可观测性。

## 7. NUMA、THP、调度

| 项 | 建议 | 说明 |
|----|------|------|
| `numactl -C/-m` | 进程与数据同 NUMA | 跨节点访存显著抬高延迟 |
| `numa_balancing` | 压测可设 0 | 减少自动迁移抖动；需固定绑核 |
| THP | 按团队基线（常 madvise/never） | 大页对随机访存未必有利 |
| `irqbalance` | 手工绑核时停用 | 避免中断漂移 |
| governor | performance | 避免频率波动 |
| `ulimit -n` | ≥65535 | 高并发 fd |
| swap | 关闭/限制 | 避免抖动 |

这些属于 OS 层，具体操作交给 `kpbot-app-tuner` 的 `os-optimization` /
`cpu-affinity-optimization`；本 skill 只给出对 Nebula 的取舍。

## 8. 按场景的推荐起点

| 场景 | 部署/配置起点 |
|------|--------------|
| Go1 点查 | 多 graphd（4–8）、单实例 worker 16–24、netio 8；按需 LSE |
| Go2 多跳 | 4 storaged、reader/worker 匹配核数、block cache 覆盖工作集 |
| Go3 重读 | 4+ storaged、足够读池、限制扇出/结果集、compaction 低干扰 |
| FSP | graphd 多实例、BFS 容器/拷贝优化、足够内存 |
| Insert | 批量写入、WAL/compaction/磁盘调优、解析优化；不要加 CPU worker |

这些只是起点；每套环境都要跑基线并验证。

## 9. 失败/负结果

| 尝试 | 结果 | 原因 |
|------|------|------|
| 减少 graphd worker 96→48（standalone） | -14.3% | 400 并发下 worker 不足，请求积压 |
| 关闭 `query_concurrently` | -0.8% | 单跳无收益，增加调度 |
| `wal_sync=false` | Insert -23.6% | compaction 开销增加 |
| write_buffer 256MB | Insert -25.4% | 大 buffer 增加 flush 延迟 |
| `-mtune=neoverse-n2` | Go1 -2.5% | 热点不在指令调度 |
| 固定绑核用于混合负载 | Go2/3/FSP 大幅下降 | 静态核不足，失去弹性 |
| 线程级 1:1 绑核 | 全场景负收益 | 突发负载核搁浅 |

## 10. 配置变更协议

1. 记录当前配置与二进制身份。
2. 备份配置文件。
3. 只改一个 key。
4. 判断是否热更新；需要重启时说明最小重启面。
5. 重启后回读配置，确认生效。
6. 功能冒烟 → 性能 A/B。
7. 无效/劣化 → 恢复备份、重启、复测到基线。
8. 记录变更、证据、回滚命令、结论。

不要把“候选动作”直接当成生产变更；高风险项先与用户确认。
