# 观测扩展对账与判读

> 只读查询为主；pg_stat_statements_reset() 等清零操作属变更，A/B 期间按协议使用。

## 1. pg_stat_statements（谁最耗时）

```sql
-- Top SQL（总时间）
SELECT queryid, calls, round(total_exec_time::numeric, 0) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       rows, shared_blks_hit, shared_blks_read
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;

-- 计划缓存行为（两个前提，缺一则 plans 恒 0、比率不可用——标“不可判读”而非“未重建”）：
-- ① track_planning 必须在**压测连接内**生效——在观察用 psql 会话里 SET 只影响该
--    连接，sysbench 的连接仍按默认 off。整窗口启用按环境选一：
--    ALTER ROLE <压测用户> SET pg_stat_statements.track_planning = on;
--      （压测前设置；该角色此后新建连接自动启用——sysbench 每连接都生效）
--    或独占实例：ALTER SYSTEM SET pg_stat_statements.track_planning = on; + pg_reload_conf()
-- ② 压测前新开连接核验 SHOW pg_stat_statements.track_planning 为 on，再以同一
--    窗口前后快照的**增量**计算 Δplans/Δcalls（不是时点比率）。
SELECT dbid, userid, queryid, calls, plans, round(100.0*plans/calls, 1) AS plan_ratio
FROM pg_stat_statements WHERE calls > 100 ORDER BY plans DESC LIMIT 10;

-- 清零是变更操作，且无参形式清的是**整个实例**（其他库/角色的统计一并清除）。
-- 共享实例默认不清零：压测前后各取一次快照（表已在库内）做增量——连接键用
-- (dbid, userid, queryid, toplevel) 四元组匹配，不能只用 queryid：共享实例里
-- 不同用户/数据库可产生相同 queryid，仅按 queryid 连接会交叉匹配、重复计数，
-- 把其他负载的增量归到本次测试。快照先按授权库与角色限定（视图天然限当前库
-- 的 dbid，再按 userid = (SELECT oid FROM pg_roles WHERE rolname=current_user)
-- 过滤）；统计被中途重置的语句（calls 不增反减）丢弃并标注，不当作负增量。
-- 仅在获准的独占环境、且限定测试库与角色时才清零：
--   SELECT pg_stat_statements_reset(<userid>, <dbid>, 0);  -- userid 在前、dbid 在后
--   取值: SELECT oid FROM pg_roles WHERE rolname='<role>' / SELECT oid FROM pg_database WHERE datname='<db>'
```

- 判读口径：`plans/calls` 比率高与火焰图 `BuildCachedPlan` 占比互为印证；
  `shared_blks_read >> hit` 提示池未热/过小（对照 pg_buffercache）。
- 观测自身成本：pgss_store 在高吞吐负载实测占 postgres CPU ~1.8-5%
  （口径见架构参考）——**对比轮两侧开关必须一致**。

## 2. pg_buffercache（池内分布）

```sql
-- 使用分布（池占比、脏页、钉住）
SELECT count(*) AS total,
       count(*) FILTER (WHERE isdirty) AS dirty,
       round(100.0*count(*) FILTER (WHERE usagecount >= 3)
             / nullif(count(*),0), 1) AS hot_pct
FROM pg_buffercache;

-- 库/表维度 top（大表是否把彼此挤出去；PG 17/18 无 regdatabase 类型——
-- 保留 reldatabase 的 OID，或 LEFT JOIN pg_database 取库名；共享关系 OID=0 单列）
SELECT coalesce(d.datname, coalesce('(shared oid=0)','(db oid=' || b.reldatabase || ')')) AS db,
       b.relfilenode, count(*) AS buffers
FROM pg_buffercache b LEFT JOIN pg_database d ON d.oid = b.reldatabase
WHERE b.relfilenode IS NOT NULL
GROUP BY 1,2 ORDER BY buffers DESC LIMIT 10;
```

- 口径：快照瞬间的池分布（无内置历史）；与等待事件 `BufferMapping/BufferIO`
  采样互证淘汰链压力。

## 3. 自研 pg_buffer_stats（锁路径/竞争观测）

```sql
SET buffer_stats_enabled = on;          -- 自研 GUC，默认 off（开销 <1% 口径）

SELECT * FROM pg_buffer_stats.hotspots;          -- 缓冲区热点（按页/关系）
SELECT * FROM pg_buffer_stats.cas_hotspots;      -- CAS 竞争热点（重试计数）
SELECT * FROM pg_buffer_stats.analyze_distribution();
SELECT * FROM pg_buffer_stats.performance_summary();
SELECT pg_buffer_stats.reset_stats();            -- 变更操作：按 A/B 协议统一执行
```

- 定位：补齐内置工具看不到的 **CAS 重试与自旋等待**（PinBuffer 的
  refcount/usagecount 同字 CAS、LWLockAttemptLock 重试路径的统计，见
  姊妹 skill pgsql-architecture 的 buffer-manager-and-locks 文档 §5（按 DOMAIN.md 域路由定位，不用相对路径））；
- 判读：cas_hotspots 的重试计数突增与等待事件 `BufferMapping/BufferPin` 采样、
  topdown 原子串行化占比（0_ports_serialize）三方互证；
- 与编译期 `LWLOCK_STATS` 宏口径的关系：后者是源码诊断构建（重编+性能损耗），
  pg_buffer_stats 是可安装扩展（运行时 GUC 开关）——生产观测用后者，源码深挖
  用前者。

## 4. 等待事件快照（冒烟/对账共用）

```sql
-- 活跃会话等待分布（空转库采不到属正常，压测中采）
SELECT wait_event_type, wait_event, state, count(*)
FROM pg_stat_activity WHERE state = 'active'
GROUP BY 1,2,3 ORDER BY count(*) DESC;
```

- **可见性前提**：普通角色看不到其他角色会话的 `state`/`query`（`pg_stat_activity`
  按观察者角色脱敏）——观察者非超级用户/非 `pg_read_all_stats` 时，跨角色的锁等待
  采样会缺失：要么换监控角色重采，要么把结论限定为“可见会话”，权限不足时标注
  “不可判定”而非“无等待”。
- 与 `pg_stat_checkpointer`（PG 17 起检查点计数所在：`num_requested`/`num_timed`/
  `buffers_written` 同窗口增量）及 `pg_stat_bgwriter`（bgwriter 侧 `buffers_clean`）
  联合判读“查询进程替后台打工”链路（机制见
  姊妹 skill pgsql-architecture 的 overview-and-process-model 文档 §4（按域路由））。
