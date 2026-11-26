# 场景热点与实测差异

数据来自鲲鹏 950/920B、AMD 9T34，OceanBase 4.2.5.5，sysbench 1.0.20，32U/250G，
observer 绑 0-31、obproxy/sysbench 按场景绑核。**只用于建立假设**，换版本/机型必须重测。
原实测百分比已脱敏，表中的“高/低/接近饱和”等是定性判读结论；需要具体数字时须在当前环境重采，不要据此横比。

## 1. 只读（read_only）

### 1.1 微架构画像

| 指标 | 950（实测） | 920B（实测） | 含义 |
| --- | --- | --- | --- |
| Frontend Bound | 低 | 中 | 前端取指/icache；950 更好 |
| Backend Bound | 高 | 高 | 后端主导 |
| Memory Bound | 高 | 高 | 访存延迟受限 |
| L3 Bound | 高 | 中 | 950 的 L3 private/cluster 时延更明显 |
| L1D miss rate | 高 | 低 | 950 更高 |
| L2D miss rate | 未记录 | 未记录 | — |
| L1D MPKI | 56.70 | 27.19 | 950 访存更重（此行为实测原值） |

判读：只读是“缓存层级穿透 + 主存延迟”型瓶颈，不是指令吞吐。MySQL 的 B+ 树是前端 Bound
较高，OB 的 LSM-Tree 是后端 Bound 高，两者优化方向不同。

### 1.2 热点函数（火焰图，按占比排序）

| 函数 | 机制 |
| --- | --- |
| `ObSortOpImpl::Compare`、`ob_strnncollsp_utf8mb4`、`ObCharset::strcmpsp` | 排序 + 字符集比较（SQL 语义/collation） |
| `ObNullSafeDatumStrCmp`、`ObDatum` 比较 | 行比较 |
| `ObDASMergeIter::get_next_rows`、`ObMultipleScanMerge`、`ObSimpleRowsMerger` | 多路归并 |
| `ObMicroBlockDecoder::decode_cells`、`ObStringDiffDecoder`、`ObIntegerBaseDiffDecoder` | 微块解码 |
| `ObArenaAllocator::alloc`、`PageArena::alloc_new_page`、`ObTenantCtxAllocator::alloc` | 临时内存分配 |
| `ObBucketLock::wrlock`、`ObLatch::wrlock`、`ObLatchWaitQueue` | KV Cache/表锁争用 |
| `ObIndexTreeMultiPassPrefetcher::prefetch` | 索引树预取 |
| `ObFuseRowCache::get_row`、`ObKVCacheMap::get` | 行缓存 |

> sysbench read_only 含 `ORDER BY`/`DISTINCT` 时，排序/比较链可占 10%+；这是查询语义与
> collation 问题，不是 OS 参数问题。换 workload（去排序）可解释大部分差异。

## 2. 只写（write_only）

### 2.1 画像

- 920B 只写 CPU 明显低于饱和、x86（Zen4）更低；V7 盘 off-CPU 等 IO 明显，内存盘后 IO 等待大幅下降。
- 换内存盘：920B 三写场景平均 +3%，950 +6%；CPU 利用率随三写场景逐级上升并接近饱和，TPS 随 CPU 线性提升。
- 说明只写是“CPU + IO 混合”，不是纯 CPU；内存盘后 CPU 瓶颈更明显。

### 2.2 热点/等待

| 现象 | 机制 |
| --- | --- |
| `ObIOHandle::wait` / `pwrite` / `pread` | 等 redo/数据 IO |
| `ObMvccValueIterator::lock_for_read_inner_` | 事务/锁等待 |
| 冻结/合并（DagWorker/LSFreeze、`MINOR_EXE`） | 周期性 QPS 下降；`T*_L0_G0` 是 worker，不据此判冻结 |
| `ObLatchMutex` 热点（占比小） | 锁占比小，常规锁优化无明显收益 |

### 2.3 已尝试但无明显收益

- 原子操作前预取、`isb`/非 `isb` pause、调自旋次数与等待时间：均无明显效果。
- 说明锁不是主因；不要继续在锁微调上投入。

## 3. 点查（point_select）

- 主要路径：MemTable hash/B-tree（`ObMtHash`、`ObQueryEngine`、`ObMvccEngine`）→
KV Cache（`ObFuseRowCache`、`ObKVCacheMap`）→ latch。
- 热点：`ObMemtable::get`、`ObMvccValueIterator`、`ObBucketLock`、`ObFuseRowCacheKey::hash`、
`ObDatumRowkey::compare`。
- 判读：点查是轻查询，CPU 高但单次工作量小；优化点在缓存命中、锁争用、绑核/NUMA，
而不是“加 worker”。
- 扩展性专项常用 30 表 × 100w 行（与 test-verification 的规格表一致），调 threads 到 observer 打满，测极限 QPS。

## 4. 插入（insert）

- 950 上 `T<tenant>_ApplySrv` 线程 CPU 接近饱和，AMD 明显更低；加 worker 无效。
- ApplySrv 热点：`serialize` 相关（遍历 id 数组 → 取 `sys_var_idx` → 再索引另一数组 →
调成员函数），随机访问、嵌套索引，预取效果差。
- 另有 `ldaddal` 等高时延原子指令（跨 cluster 共享），`recv_request` 路径的 barrier+load
在 950 上平均时延高于 920B。
- 实测：ApplySrv 增加工作线程后 insert +12%；多队列叠加无劣化。

## 5. 更新（update）

- 读旧行 + 写新版本：`MvccValueIterator::lock_for_read_inner_`、`ObReadRow::iterate_row`、
`ObMemtable::set`。
- 实测 950 上 `gettimeofday` 是显著热点；用 LD_PRELOAD 替换为更快的实现后占比明显下降，
update +1.4%。属于低风险、可回滚的源码/运行时优化。
- 索引列更新与非索引列更新差异大；`update_non_index` 是常用基线。

## 6. 业务模型（交易/账务）

- 交易创建需临时调大租户内存与冻结阈值（详见 test-verification）。
- 账务写放大明显，redo/clog 与冻结合并影响大。
- 业务模型的 SQL 形态、事务大小、冲突率与 sysbench 不同，热点不可直接套用。

## 7. 瓶颈迁移

```
轻查询/点查        → sql_nio/队列/latch/缓存命中
只读（排序/范围）  → 排序/比较、微块解码、DAS 归并、L3/跨 P
只写               → IO 等待、redo、冻结合并、ApplySrv
插入               → ApplySrv/序列化、L3/跨 P、redo
更新               → MVCC 版本链、锁、gettimeofday、内存
大规格（128U）     → sql_nio 瓶颈（实测 net_thread_count 12→36 解除）
```

## 8. 平台差异（950 vs 920B vs AMD）

| 维度 | 950 | 920B | AMD 9T34 |
| --- | --- | --- | --- |
| 前端 Bound | 低 | 中 | 较低 |
| L3 Bound | 高 | 中 | 较低 |
| L3 模式 | cluster 内 private | 支持 partitioned/private | — |
| 线性度 32U/16U | 1.64 | 1.87 | 1.93 |
| 跨 P 敏感度 | 高（numa_distance 差异） | 中 | 低 |
| obproxy 去掉后 | insert 提升明显 | 只写 +58%、只读 +20% | 可能下降（-6%） |
| ApplySrv | 接近饱和 | 有瓶颈但较低 | 低 |

> 不要把 920B 的 L3/绑核结论直接套到 950；反之亦然。

## 9. 对照实验清单

| 假设 | 对照 |
| --- | --- |
| 跨 P 访存 | 1NPS / cgroup 绑 NUMA / migratepages / 启动时 numactl |
| obproxy 瓶颈 | 去掉 obproxy 直连（同绑核） |
| IO 瓶颈 | 内存盘 vs NVMe（同数据/绑核） |
| ApplySrv 瓶颈 | 只加 ApplySrv 线程（不改其他） |
| 排序占比 | 去 ORDER BY/DISTINCT 的 workload（仅诊断） |
| 锁瓶颈 | 降并发/换只读/看 latch 等待 |
| 客户端瓶颈 | 加 sysbench 进程/核，看服务端 CPU 是否仍不满 |
