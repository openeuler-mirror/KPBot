# 三负载画像（ro / rw / wo）

> 口径行（两套测量系列，各自成基线、不可横比数字）：本页 1.1 表为
> **17.5 三负载轮**（PostgreSQL 17.5 自编译 LTO、ARM aarch64 240 核、
> sysbench 1.0.20、64 表 × 1000 万行 ~186GB、扩展协议+prepared statements、
> pg_stat_statements 加载；百分比 = 占 postgres 进程 CPU，idle 归一化：
> ro×1.166、rw×1.348、wo×1.565，inclusive 聚合）；§3 分层表另为
> **读负载 128G buffer 轮**（同环境、shared_buffers=128G，SVG inclusive 近似拆分）。

## 1.1 关键数字总表（17.5 三负载轮，inclusive）

| 阶段/模块 | ro | rw | wo |
|---|---|---|---|
| 网络接收 secure_read（含内核 TCP） | 9.7（epoll 6.4+recv 2.7） | 7.7 | 5.2 |
| 网络发送 secure_write/socket_flush | 7.8 | 6.7 | 4.5 |
| 计划缓存 GetCachedPlan | 4.7（含重建 3.2） | 4.2（含 2.5） | 1.3（纯复用） |
| 优化器 standard_planner | 2.9 | 2.2 | ≈0 |
| 执行总入口 PortalRun | 56.9 | 65.0 | 54.6 |
| ─ ExecScan（含 IndexNext） | 22.7 | 20.2 | 18.8 |
| ─ ExecSort（ORDER BY 范围查询） | 20.5 | 16.9 | 0 |
| ─ ExecAgg（SUM） | 6.5 | 5.9 | 0 |
| ─ ExecModifyTable | 0 | 11.2 | 47.3 |
| ─ printtup | 4.8 | ~4 | ~0 |
| PortalStart | 7.9 | 5.4 | 0.1 |
| Buffer 读 StartReadBuffer | 14.3 | 24.8 | 9.5 |
| └ clock-sweep 淘汰 GetVictimBuffer | （干净页，免刷） | ~15.7 | 5.2 |
| └ 刷脏 FlushBuffer→mdwritev | 0 | 11.7 | 4.4 |
| └ fsync 请求队列 ForwardSyncRequest | 0 | 8.3 | 0.5 |
| WAL 插入 XLogInsertRecord | 0 | 1.2 | 24.3 |
| WAL 提交刷盘 XLogFlush | 0 | 1.6 | 25.3（其中 WaitXLogInsertionsToFinish 自旋 22.3） |
| 提交事务 CommitTransactionCommand | 0.5 | 3.1 | 28.8 |
| 快照 GetSnapshotData | 3.8 | 5.9 | 3.0 |
| LWLockWaitListLock | ~0 | 7.4 | 32.3 |
| futex 睡/醒 + try_to_wake_up | 0.6 | 2.1 | 7.9 |
| heap_page_prune | ~0.5 | 0.7 | 6.5 |
| pgss 观测（store + End hook） | ~5 | ~3 | ~1.4 |
| topdown IPC | 0.731 | 0.568 | 0.233 |
| topdown Retiring | 9.1% | 7.1% | 2.9% |
| topdown 原子/屏障串行化 | 10.8% | 15.9% | 33.2% |
| topdown L3_Bound | 46.6% | 49.9% | 49.6% |
| idle | 14% | 26% | 36% |

（└/─ 为 inclusive 嵌套关系，子项按链路包含，不可相加。）

## 2. 逐负载要点（机制详见 contention-mechanisms）

- **ro**：ExecSort 20.5%（sysbench 的 `ORDER BY c` 范围查询）+ ExecAgg 6.5% 是
  第一组；网络 epoll 6.4%（等下一条命令的成本与命令频率成正比）；
  victim 都是干净页免刷脏（mdwritev=0）。
- **rw**：buffer 淘汰刷脏链是第一热点（victim 常为脏页：淘汰→同步刷→排 fsync
  队列）；排序/聚合仍显著（写只占 1/3 语句）。
- **wo**：WAL 相关 ≈50% + LWLockWaitListLock 32.3%（等待队列自旋锁是第一大
  单项）+ ExecModifyTable 47.3%（UPDATE>INSERT 含索引维护>DELETE）+
  heap_page_prune 6.5%；微架构坍塌（IPC 0.233/Retiring 2.9%/串行化 33.2%）。

## 3. 分层对照（读负载 128G buffer 轮，PG vs MySQL 口径）

| 层 | MySQL（既有结论） | PG SELECT | PG 写负载 |
| --- | --- | --- | --- |
| 网络接入 | 23% | ≈24%（RX 10 + TX 14） | ≈14%（TX 7.2 + RX 6.9） |
| SQL 引擎 | 45% | ≈33%（优化器 10.5 + 执行器 ~16 + 初始化 ~3.5 + 计划缓存 1.5） | ≈11%（解析优化 ≈0） |
| 存储引擎（含事务） | 32% | ≈35%（索引扫描路径 34.7） | ≈75%（锁自旋 20.6 + btree 12.2 + buffer 17.3 + WAL 5.3 + heap 4.9 + MVCC 3.7…） |
| 其他 | — | ≈7% | ≈7%（进程唤醒/切换 6.2） |

- 一句话：SELECT 下 PG 与 MySQL 三层分布接近（计划缓存生效）；写负载下 PG 重心
  全压“存储+事务”（~75%），MySQL 的锁等待在 PG 对应换成 LWLock 多核自旋；
- 该轮的可优化点：计划重建（GetCachedPlan 11.5% 中 91% 落 BuildCachedPlan，
  generic plan 生效可省 ~9%——处置见
  `../../pgsql-performance-optimization/references/wal-and-plan-optimizations.md`）。

## 4. 使用方式

- 先对口径行（环境/轮次/归一化），再对数字；新环境采集（本 skill 的
  profiling-playbook）后与本表对照建立“像哪一类”的假设；
- 差异显著≠直接抄结论：按 contention-mechanisms 的证据映射逐条核实。
