# HNSW / pgvector 检索路径优化

> 口径：pgvector HNSW、ARM 48 核（96T）、80GB 数据集、pg_prewarm 预热后全命中；
> pgvector 版本以目标环境
> `SELECT extversion FROM pg_extension WHERE extname='vector'` 为准（不写悬空
> 版本锚点，路径按 pgvector 源码树 `contrib/vector/` 记述）。

## 1. 检索路径的锁放大（为什么 HNSW 对锁敏感）

- 每访问一个图节点至少 **2 次 ReadBuffer + LockBuffer + UnlockReleaseBuffer**
  （一次邻居页取邻居列表、一次向量页算距离）；一次搜索（ef_search=40、M=16）
  遍历数百节点 = **数百到上千次 ReadBuffer**，每次完整走
  BufferAlloc 的分区锁 + BufTableLookup + PinBuffer CAS（机制见
  姊妹 skill pgsql-architecture 的 buffer-manager-and-locks.md（按域路由定位） §1）。
- 预热后全部走 found 路径（无 I/O）——热点=纯锁路径与元数据访问，这是
  “分区扩容/缩池”两项优化的场景根源（处置见
  `references/buffer-and-lock-optimizations.md`）。

## 2. 参数侧（真实可调项）

| 项 | 作用 | 权衡 |
|----|------|------|
| `hnsw.ef_search`（pgvector 索引级参数，SET hnsw.ef_search=…） | 候选队列深度 | 检索质量（召回）vs 遍历节点数（锁路径次数）：调小直接减 ReadBuffer 总量 |
| `M`（建索引参数） | 每节点邻居数 | 图连通度 vs 邻居页/向量页访问量；建好后不可改（需重建索引） |
| `pg_prewarm`（扩展函数） | 把堆/索引页读进缓冲池 | 冷启动长尾消除；池需容纳热集（见缩池适用条件） |

- ef_search 调小必须**同步验证召回**（同一查询集的 top-k 对账，归
  pgsql-test-verification 的正确性门禁）——性能数字不能掩盖检索质量回退。

## 3. CAS/锁竞争观测（先测量再优化）

```sql
-- 自研扩展（观测口径见 姊妹 skill pgsql-test-verification 的 observation-extensions.md（按域路由定位））
SET buffer_stats_enabled = on;
SELECT * FROM pg_buffer_stats.cas_hotspots ORDER BY retries DESC LIMIT 10;
```

- 编译期深挖：源码树加 `LWLOCK_STATS` 宏重编（诊断构建，性能有损，只用于
  源码级分析，不用于 A/B 吞吐轮）；关注 `BufferAlloc`/`BufTableLookup`/
  `PinBuffer`/`UnpinBuffer`/`LWLockAcquire`/`LWLockRelease`/
  `pg_atomic_compare_exchange_u32` 的重试分布；
- off-CPU：offcputime 看 BufTableLookup 一带的睡眠栈——与 cas_hotspots 互证。

## 4. 验证协议（HNSW 场景专用）

1. **正确性**：同查询集 top-k 对账（召回率口径写入结论），不同 ef_search 的
   轮次各自对账；
2. **性能**：QPS/p95 的 ≥3 轮均值（剔首轮），同口径；
3. **锁证据**：等待事件 BufferMapping/BufferPin 采样占比、cas_hotspots 重试
   计数——三项与吞吐同向才判有效。

## 5. 负结果与不做清单

- **手动 SVE/NEON 距离计算微优化**：在锁路径主导的场景优先级最低——先消
  锁竞争再看计算（与“机制优先”纪律一致）；距离计算热点确实显著时才评估，
  且与通用向量化方法（域外）衔接；
- **盲目加池**：数据全热时加池只剩元数据代价（见缩池条目的数学）；
- 任何“没效果”的结论先核对：参数是否真的生效（`SHOW hnsw.ef_search`）、
  池是否真的热（pg_buffercache）、A/B 口径是否一致。
