# SQL 引擎与执行链路（REL_17_5）

> 只读知识库。百分比均来自源资料实测口径：PostgreSQL 17.5 自编译、ARM aarch64
> 240 核、sysbench 1.0.20（oltp_read_only / read_write / write_only）、64 表 ×
> 1000 万行、扩展协议 + 服务端 prepared statements、pg_stat_statements 已加载；
> 百分比 = 占 postgres 进程 CPU（idle 归一化），inclusive 聚合。用于建立假设，
> 不做目标环境的结论。

## 1. 一条语句的完整路径

```
客户端（libpq/驱动）──TCP 小包（每事务 5~10 条语句同步往返）
   ▼
① 收命令  PostgresMain → pq_recvbuf → secure_read
   socket 非阻塞：recv→EAGAIN→WaitEventSetWait→epoll_pwait→重试 recv
② 解析    raw_parser → 生成解析树（语句文本首次缓存后跳过）
③ 计划    GetCachedPlan（命中也可能走 BuildCachedPlan→standard_planner 重建）
④ 执行    PortalStart → PortalRun
            ├─ ExecScan（含 IndexNext：btree 下降 + 回表 heapam_index_fetch_tuple）
            ├─ ExecSort / ExecAgg / ExecModifyTable（DML）
            └─ 结果序列化 printtup → socket_flush → secure_write
⑤ 收尾    PortalDrop / 事务提交（见 wal-mvcc-and-commit.md）
```

## 2. 网络路径（PG16+ 的 epoll 代价）

- 客户端 socket 被 `pg_set_noblock` 设为非阻塞（`src/backend/libpq/be-secure.c`，
  17.5 源码确认）：数据已到达时首次 `recv()` 即成功返回，**仅在需要等待时**才走
  `recv→EAGAIN→WaitEventSetWait→epoll_pwait→重试`——epoll 次数与“需等待的
  读取”次数成正比（按命令频率估算 epoll 次数是上界口径，不是每次必付）。
- 实测占比（占读路径 CPU）：ro 中 epoll 部分 6.4%（占网络接收 9.7% 的约七成）；
  与“等下一条命令”的频率成正比：ro 6.4% > rw 4.8% > wo 2.9%。
- 网络收发合计（**128G buffer 读负载轮**——另一测量系列，勿与上行 6.4% 的
  三负载轮混读）：ro ≈24%（RX 10 + TX 14，SELECT 回传结果行使 TX > RX）、
  wo ≈14%（TX 7.2 + RX 6.9，小消息对称）；**三负载轮**的同口径值为
  ro ≈17.5%（RX 9.7 + TX 7.8）、wo ≈9.7%（RX 5.2 + TX 4.5）。与 MySQL 同负载
  网络占比（~23%）接近——短查询小包往返是共性成本，不是 PG 特有缺陷。
- 归因提示：把 `epoll_pwait`/`tcp_recvmsg`/`tcp_sendmsg` 合并看“网络层”；只看
  顶层帧容易误归因到 PostgresMain。等待占比与客户端节奏强相关（同步往返越频繁、
  每次等下一条命令的窗口越长，epoll 占比越高）。

## 3. 计划缓存（GetCachedPlan 的真实行为）

- `GetCachedPlan`（`src/backend/utils/cache/plancache.c`）命中语句后仍要决定走
  generic plan 还是 custom plan：generic plan 复用旧计划树；custom plan 每次走
  `BuildCachedPlan→standard_planner` 重新优化（参数字面量代入）。
- 触发 custom plan 的常见原因：计划失效（依赖对象变化）、前若干次执行内的探索、
  `plan_cache_mode = force_custom_plan`、以及 generic/custom 代价比较认为字面量
  值影响太大（如 skew 列）。
- 实测口径（两套测量系列，只作各系列内结论、不可横比数字；均为 **CPU 采样占比**
  口径）：17.5 三负载轮（本文件头部口径行）ro 负载 GetCachedPlan 4.7%、
  BuildCachedPlan→standard_planner 2.9%；另一轮 128G buffer 读负载系列
  （pgsql_workload 口径：PG 17.5、240 核、128G shared_buffers、sysbench
  oltp_read_only）GetCachedPlan 11.5% → BuildCachedPlan 10.5% →
  standard_planner 9.3%。注意：CPU 采样占比**不能**推出调用频率结论——耗时较长的
  少数规划就能贡献大量样本；「多少比例的调用重建了计划」须用同窗口的规划次数/
  执行次数（Δplans/Δcalls，见 pg_stat_statements 采集口径，且须全窗口启用
  track_planning）单独支撑，两类证据不可互相冒充。写负载对照：planner ≈0、
  计划纯复用（同为采样口径）。
- 判别方法（不猜配置，先取证）：PG 17/18 **没有** `EXPLAIN (CUSTOM_PLAN)` 选项——
  对同一条预备语句分别 `SET plan_cache_mode = force_custom_plan` 与
  `force_generic_plan`，用 `EXPLAIN EXECUTE <stmt>(<代表性实参>)` 生成对照计划；
  规划频率仅在窗口内开启 `pg_stat_statements.track_planning` 后才可用
  `plans/calls` 估算（默认 off，`total_calls` 列不存在、勿引用）。处置
  （plan_cache_mode、参数化改写）归 `pgsql-performance-optimization`。

## 4. 执行器分层（inclusive 口径速查）

| 阶段 | ro | rw | wo | 备注 |
|------|----|----|----|------|
| PortalRun（总入口，含扫描/排序/聚合/DML） | 56.9 | 65.0 | 54.6 | inclusive 层层包含 |
| ─ ExecScan（含 IndexNext） | 22.7 | 20.2 | 18.8 | 点查+范围扫描 |
| ─ ExecSort | 20.5 | 16.9 | 0 | sysbench read_only 的 `ORDER BY c` 范围查询驱动 |
| ─ ExecAgg | 6.5 | 5.9 | 0 | `SUM(k)` |
| ─ ExecModifyTable | 0 | 11.2 | 47.3 | UPDATE>INSERT(含索引维护)>DELETE |
| PortalStart（开表/快照/ExecutorStart） | 7.9 | 5.4 | 0.1 | |
| printtup（结果序列化） | 4.8 | ~4 | ~0 | 写回包小 |
| GetSnapshotData | 3.8 | 5.9 | 3.0 | ProcArray 顺序扫，连接数敏感 |

- 表达式解释器 `ExecInterpExpr` 在此负载 <1%——“PG 表达式解释慢”在这类点查负载
  不成立，不要按直觉先动它。
- MVCC 可见性判断近乎免费（`XidInMVCCSnapshot` ~0.1%）：hint bits 让点查免日志
  提交检查。

## 5. 目录缓存与统计观测的自身开销

- `SearchCatCacheInternal` ~1.8%、`pgss_store`（pg_stat_statements 落统计）
  ~1.8%（ro）——观测本身有成本；对延迟敏感的对比轮次，A/B 两侧必须同开关，
  否则口径被观测工具污染。
