# 存储、转储合并、日志与事务

源码根 `${OB_SRC}`。

## 1. LSM-Tree 数据组织

```
Table → Partition → Tablet → LS（Log Stream）
                                 │
        ┌────────────────────────┴─────────────────────────┐
        │ MemTable（Active/Frozen）                        │
        │ mini SSTable（MemTable 冻结后转储）               │
        │ minor SSTable（多次 mini 合并）                   │
        │ major SSTable（基线合并，通常一次）               │
        └──────────────────────────────────────────────────┘
```

- 读取 = MemTable + 多个 SSTable 的 **多路归并**（`ObMultipleMerge` / `ObSimpleRowsMerger` /
loser tree），按 rowkey 与 MVCC 版本决定可见性。
- 写入 = 追加 MemTable + redo；不原地更新。更新/删除产生新版本或删除标记。
- 合并（compaction）在后台 DAG 中执行，与前台写入争抢 CPU/IO；写场景的周期性 QPS 下降
常与冻结/合并同步。

## 2. 冻结与转储

触发：

```
ObTenantFreezer::check_and_do_freeze()     src/storage/tx_storage/ob_tenant_freezer.h
  每 2s 检查：active_memstore_used > freeze_trigger_percentage × memstore_limit
```

相关参数（`src/share/parameter/ob_parameter_seed.ipp`）：

| 参数 | 默认 | 作用 | 调大的风险 |
| --- | --- | --- | --- |
| `memstore_limit_percentage` | 0（自动） | MemStore 占租户内存比例 | 挤压其他 Context，查询内存不足 |
| `freeze_trigger_percentage` | 20 | 触发冻结的 MemStore 使用率 | 单次冻结数据量大，转储更重、抖动更大 |
| `writing_throttling_trigger_percentage` | 60 | 写限流阈值 | 调高会**推迟**限流：持续写入若转储跟不上，更易耗尽 MemStore；调低才会提前限流、尾延迟上升（与参数地图口径一致） |
| `writing_throttling_maximum_duration` | — | 限流最长持续时间 | 写入长时间降速 |

流程：

```
Freezer → set_frozen(memtable) → 创建 mini merge DAG
  → DagScheduler → DagWorker：MemTable → mini SSTable
  → minor merge：多个 mini → minor
  → major merge：所有 minor → major（基线）
  → 释放 retired pages / 旧 SSTable
```

## 3. SSTable 读路径与热点

```
ObSSTable::get() / scan()
  ├─ 索引树：ObIndexTreePrefetcher（多级索引块，可多趟预取）
  ├─ Bloom Filter：先排除不可能存在的 macro block
  ├─ 微块缓存：user_block_cache / index_block_cache（KV Cache，2MB memblock）
  ├─ 微块解码：ObMicroBlockDecoder + 列解码器（RLE/Dict/IntegerDiff/StringDiff...）
  └─ 行组装：ObDatumRow / ObRowReader / fuse row cache
```

常见热点函数（实测 sysbench read_only，aarch64）：

| 热点 | 机制 |
| --- | --- |
| `ObSortOpImpl::Compare`、`ob_strnncollsp_utf8mb4` | 排序 + 字符集比较（查询语义） |
| `ObDASMergeIter::get_next_rows`、`ObMultipleScanMerge` | 多路归并 |
| `ObMicroBlockDecoder::decode_cells`、列解码器 | 微块解码 |
| `ObArenaAllocator::alloc`、`PageArena::alloc_new_page` | 临时内存分配 |
| `ObBucketLock::wrlock`、`ObLatch::wrlock`、`ObLatchWaitQueue` | KV Cache / 表锁争用 |
| `ObIndexTreeMultiPassPrefetcher::prefetch` | 索引树预取 |

> 这些是“机制”，不是“结论”。是否值得优化取决于占比与对照实验。

## 4. MVCC 与读一致性

- 每行有多个版本，读按快照 SCN 判断可见性；写生成新版本。
- 点查走 MemTable 的 hash/B-tree（`ObMtHash`、`ObQueryEngine`、`ObMvccEngine`），再向下层
SSTable 查找；读到未提交版本时需 `lock_for_read` 检查事务状态。
- 长事务/未提交事务会拉长版本链，增加读放大；`undo_retention` 影响历史版本保留。

## 5. 日志与复制（PALF）

```
写入 redo → PALF Leader 追加本地日志 → 复制到 Follower
        → 多数派持久化 → 提交点推进 → 应用到 MemTable（可见）
```

- `ObLogService`（`src/logservice/ob_log_service.h`）管理 PALF、apply、replay、archive。
- **ApplySrv**：推进事务提交回调与可见性；insert 高负载时可成为瓶颈。
- **ReplaySrv**：备副本回放；写场景备节点 CPU/网络明显升高属正常。
- clog 盘延迟直接影响提交尾延迟；clog 与 data 分盘可降低互相干扰。
- 日志归档/备份依赖 `archiveservice` 与外部存储（NFS/S3/OSS）。

## 6. 事务（2PC、TxTable、锁）

```
ObTransService::commit_tx()            src/storage/tx/ob_trans_service.h
  └─ ObPartTransCtx（每 LS 每事务）     src/storage/tx/ob_trans_part_ctx.h
       ├─ 协调者 PREPARE → 参与者
       ├─ 协调者 COMMIT → 参与者
       ├─ redo 回调槽（OB_TX_MAX_LOG_CBS）
       ├─ ELR（Early Lock Release）
       └─ Standby 协调
```

事务 API（`src/storage/tx/ob_tx_api.h`）：`acquire_tx()`、`start_tx()`、`commit_tx()`、
`submit_commit_tx()`、`rollback_tx()`、`abort_tx()`。

### 6.1 锁类型（`deps/oblib/src/lib/lock/`）

| 锁 | 文件 | 特点 | 常见热点 |
| --- | --- | --- | --- |
| `ObLatchMutex` | `ob_latch.h` | CAS 自旋 + futex 等待，最底层互斥 | 微块/对象分配 |
| `ObLatch` | `ob_latch.h` | 读写 latch，读者计数 + 写者排队 | KV Cache、表锁 |
| `ObLDLatch` | `ob_latch.h` | 带诊断的 latch | 租户生命周期 |
| `ObSpinLock` | `ob_spin_lock.h` | 轻量自旋 | 短临界区 |
| `DRWLock` | `ob_drw_lock.h` | epoch 写排序 | 分布式读多写少 |
| `TCRWLock` | `ob_tc_rwlock.h` | per-thread 读计数 | 租户元数据 |
| `ObBucketLock` | `ob_bucket_lock.h` | 分片锁（哈希到 latch） | KV Cache map、表锁 |
| `ObFutex` | `ob_futex.h` | 用户态等待 | latch 等待队列 |

RAII：`ObLockGuard`、`ObLatchMutexGuard`、`SpinRLockGuard/SpinWLockGuard`、
`ObBucketRLockGuard/ObBucketWLockGuard`（`ob_lock_guard.h`）。
### 6.2 表锁

`src/storage/tablelock/`：`ObTableLockService`（acquire/release）、`ObLockMemtable`、
死锁检测、MySQL `LOCK TABLES` 执行器。DDL 与显式表锁会与 DML 争用。

## 7. 存储/事务排查清单

| 现象 | 证据 | 判读 |
| --- | --- | --- |
| 写 QPS 周期性下降 | 冻结/合并日志、`DagWorker`/`LSFreeze` 线程组 CPU、IO 延迟 | 冻结或合并抢占资源；调阈值/错峰/换盘（`L0_G0` 是租户 worker，不用于判冻结/合并） |
| 写尾延迟高 | redo/clog 延迟、PALF 落盘、`io write delay` | clog 盘瓶颈或多数派网络 |
| insert CPU 高但 TPS 低 | ApplySrv <数值>%、serialize 热点 | 提交回调/序列化瓶颈，不是 worker |
| 点查慢 | `lock_for_read_inner_`、TxTable 检查、KV Cache miss | 未提交事务/长事务/缓存不足 |
| 读放大 | `block cache miss`、微块解码占比、SSTable 层数 | 合并滞后或查询范围过大 |
| 锁争用 | `ObLatchWaitQueue`、`memstore wait read lock time` | 表锁/缓存锁/热点行 |
