# WAL/提交/计划缓存/后台进程调参

> 每项含机制（架构对齐）、适用条件、风险与回滚。所有 GUC 真实存在（17/18）；
> 改动走 ALTER SYSTEM 或 conf 副本 + diff 记录，A/B 单变量。

## 1. 计划缓存（读负载侧）

| 证据形态（先取证） | 处置 |
|--------------------|------|
| 火焰图 BuildCachedPlan/standard_planner 高（读多写少负载） | 参数化改写查询（绑定参数让 generic plan 有意义）；核对 `plan_cache_mode`（auto/default/force_generic_plan/force_custom_plan）是否被设成 force_custom_plan |
| pg_stat_statements Δplans/Δcalls 增量比率高（前提：整个窗口启用 `pg_stat_statements.track_planning`——该 GUC 默认 off，重规划再频繁 plans 也恒 0、不可判读；且 calls 有增量、按 (dbid, userid, queryid, toplevel) 快照差分） | 同上；敏感列（skew）场景不要强推 generic（劣化风险） |

- 机制：GetCachedPlan 命中仍可能走 custom plan 重优化（见
  姊妹 skill pgsql-architecture 的 sql-engine-and-execution.md（按域路由定位） §3）；
  读负载实测重建路径占比高时，generic plan 生效可省 ~9% CPU 口径（128G buffer
  读负载轮，只作方向参考）；
- 风险：skew 列（少数值占大头）下 generic plan 会选错计划——强推前对同一条预备
  语句分别 `SET plan_cache_mode = force_custom_plan` 与 `force_generic_plan`、
  用 `EXPLAIN EXECUTE <stmt>(<代表性实参>)` 生成对照计划比较代价差
  （PG 17/18 没有 EXPLAIN (CUSTOM_PLAN) 选项）；
- 回滚：`plan_cache_mode` 回 auto（`ALTER SYSTEM SET ... ; SELECT
  pg_reload_conf()`）。

## 2. 提交链（写负载侧）

| 参数 | 机制 | 适用 | 风险 |
|------|------|------|------|
| `synchronous_commit = off` | 提交不等本地 XLogFlush 刷盘即返回（省等待+自旋） | 吞吐优先、可容忍崩溃丢最近提交窗口（事务不撕裂） | 崩溃丢窗口内提交；复制间隙 |
| `synchronous_commit = local` | 仍等**本地刷盘**、仅省同步备库的确认等待 | 有同步备库且提交被复制确认拖慢时 | 无同步备库的实例上从 on 改 local 收益≈0（勿误判调参收益） |
| `commit_delay`（配 `commit_siblings`） | 组提交：主动延迟 N 微秒攒批量刷 | 高并发短事务（WALSync 等待+等待队列自旋显著） | 低并发时纯加延迟；参数不敏感场景收益噪声 |
| `wal_buffers` 加大 | 插入满页等待概率下降 | WAL buffer 频繁翻转（默认口径见架构参考 §3） | 内存占用；过大无边际收益 |
| `wal_compression` | **仅压缩写进 WAL 的全页镜像（FPI）**，普通 WAL 记录不压缩 | WAL 写带宽瓶颈**且** FPI 主导（同负载下 `pg_stat_wal` 的 `wal_fpi` 增量 / `wal_bytes` 增量核对：FPI 占比高才可期收益）且 CPU 有余量 | CPU 换带宽；写热点负载反向；带宽主要耗在普通记录时开了几乎无收益 |

- 证据门槛：等待事件 WALSync/WALWrite 占比 + 火焰图 WaitXLogInsertionsToFinish
  /XLogFlush + LWLockWaitListLock 同向；
- `synchronous_commit=off` 是**持久性换吞吐**：先与业务确认容忍窗口，不要当
  “免费加速”默认项。

## 3. 后台进程组（读写混合侧：淘汰刷脏链）

| 参数 | 方向 | 说明 |
|------|------|------|
| `bgwriter_delay` 调小 / `bgwriter_lru_maxpages` 调大 | 后台多刷 | 降低查询进程命中脏 victim 概率（淘汰链机制见架构 overview §4） |
| `bgwriter_lru_multiplier` 提高 | 按需求预测多刷 | 超前刷的负担换关键路径干净 victim |
| `checkpoint_completion_target = 0.9` | 摊平 checkpoint | 避免末期集中刷脏与业务撞车 |

- 证据门槛：rw 负载火焰图 GetVictimBuffer→FlushBuffer→mdwritev 链 + 等待事件
  BufferIO/DataFileWrite + pg_buffercache 脏页比 + pg_stat_bgwriter 消化滞后；
- 风险：后台刷脏本身占 IO 带宽——存储能力不足时调参只是搬位置，先量 IO 余量。

## 4. autovacuum（HOT/死元组侧）

- 证据：写负载 heap_page_prune 显著、表膨胀（pgstattuple 死元组比）、
  `pg_stat_user_tables` 的 n_dead_tup 堆积；
- 处置：`autovacuum_vacuum_cost_limit`/`autovacuum_vacuum_scale_factor`（按表
  `ALTER TABLE ... SET (autovacuum_...)`），写重表单独收紧 scale_factor；
- 风险：worker 数与 IO 配额全局限性——并发 vacuum 会与业务抢 IO。

## 5. 优化排序速查（证据对齐后）

```
读多写少: 计划缓存(§1) → 网络/连接池(域外) → 指令级(最后)
读写混合: 淘汰刷脏链(§3) → 池/分区(见 buffer 条目) → autovacuum(§4)
纯写:     提交链(§2) → WAL 带宽(§2) → 索引结构(域外评估)
HNSW 类:  锁路径(buffer/hnsw 条目) → ef_search → 指令级(最后)
```

每轮只走一格，A/B 全程同口径（≥3 轮、剔首轮、<5% 判噪声）。
