# OceanBase 配置优化

参数定义以 `src/share/parameter/ob_parameter_seed.ipp` 为准；以下“实测”来自
OB 4.2.5.5 + sysbench/业务模型，换版本/机型需重测。

## 1. 规格与租户（大规格跑不满的首查项）

| 参数 | 32U | 64U | 128U | 说明 |
| --- | --- | --- | --- | --- |
| `cpu_count` | 32 | 64 | 128 | observer 可用核 |
| `memory_limit` | 250G | 500G | 1000G | 按规格 |
| 租户 `cpu_num` | 10 | 20 | 120/128 | 32U 可跑满；128U 需加大 |
| 租户 `memory_size`（交易创建/账务） | 150G/20G | 300G/40G | 600G/80G | 随规格翻倍 |
| `net_thread_count` | 12 | 12 | 36 | 128U 整机未满、`sql_nio` 单线程 ≥90% 单核且请求队列增长（口径见 §1 判据） |

判据：

- observer 目标核未满、租户 CPU 配额已用满 ⇒ 加租户 CPU。
- 目标核已满但 TPS 不涨 ⇒ 看 sql_nio/ApplySrv/L3/跨 P。
- 128U 整机 CPU 未满且 `sql_nio` 单线程持续 ≥90% 单核（`pidstat -t -p <observer_pid> 1`，≥3 次采样）、请求队列（Recv-Q）持续增长（`ss -lntp`）⇒ 先加 `net_thread_count`。

## 2. 线程

| 参数 | 默认 | 调整建议 | 风险 |
| --- | --- | --- | --- |
| `workers_per_cpu_quota` | 10 | 一般不动；轻查询可降 | 过低排队 |
| `cpu_quota_concurrency` | 4 | 租户级，按并发调 | 过高上下文切换 |
| `net_thread_count` | 0（max(6,CPU/8)） | 大规格上调 | 过多调度 |
| `px_workers_per_cpu_quota` | 10 | 大查询多可调 | 小查询开销 |
| `large_query_worker_percentage` | 30 | 大查询多可调 | 挤占普通查询 |

> 加 worker 不能解决 ApplySrv/sql_nio/IO 瓶颈；先看线程 CPU 分布。

## 3. 内存与冻结

| 参数 | 默认 | 调整场景 | 实测/风险 |
| --- | --- | --- | --- |
| `_memstore_limit_percentage`（租户级，租户值优先） | 随租户 | 交易创建临时 50（业务租户；或 sys 下 `TENANT=<tenant>`） | 挤压该租户查询内存；事后恢复 |
| `memstore_limit_percentage`（**集群级**，仅 sys 可设） | 0（自动） | 仅在需要全局调整时设 50（sys 租户，影响所有租户） | 挤压全部租户查询内存；不要当租户临时杠杆；事后恢复 |
| `freeze_trigger_percentage`（租户级） | 20 | 交易创建临时 30（业务租户；或 sys 下 `TENANT=<tenant>`） | 单次冻结更大；事后恢复 |
| `writing_throttling_trigger_percentage` | 60 | 写洪峰前评估 | 调低提前限流 |
| `memory_limit` | 0M | 按规格 | 过大挤压 OS |
| `_sort_area_size` | 32M | 排序大/落盘时调 | 并发大时内存压力 |
| `_hash_area_size` | 32M | 哈希连接/聚合落盘时调 | 同上 |
| `query_memory_limit_percentage` | 50 | 并发查询多时评估 | 影响并发数 |
| `undo_retention` | 1800 | 长事务/闪回需求 | 越大读放大越明显 |

> 作用域依据 `ob_parameter_seed.ipp`：`memstore_limit_percentage` 是 OB_CLUSTER_PARAMETER
> （集群级，仅 sys 租户可设，影响所有租户），租户级对应 `_memstore_limit_percentage`
> （OB_TENANT_PARAMETER；两者都设置时租户值优先）；`freeze_trigger_percentage` 是
> OB_TENANT_PARAMETER。租户配置容器只放 TENANT 参数，普通租户执行
> `ALTER SYSTEM SET memstore_limit_percentage=...` 会报错，不能把集群级参数当租户临时杠杆。
> `ALTER RESOURCE UNIT` / `DBA_OB_UNIT_CONFIGS` 是 sys 侧集群对象。

交易类模型示例（恢复 SQL 必须由变更前的实际值生成，不能写死默认值；注释标明执行租户）：

```
-- 0) 变更前读取实际值并记为 OLD_*（原值可能不是默认值，例如 20 CPU/40G）
--    执行租户：freeze/租户级 memstore 在业务租户查；集群级 memstore 与 Unit 在 sys 查。
SHOW PARAMETERS LIKE 'freeze_trigger_percentage';                 -- 业务租户（或 sys 下加 TENANT=<tenant>）；记为 OLD_FREEZE
SHOW PARAMETERS LIKE '_memstore_limit_percentage';                -- 业务租户；记为 OLD_TENANT_MEMSTORE（可能未显式设置）
SHOW PARAMETERS LIKE 'memstore_limit_percentage';                 -- sys 租户（集群级）；记为 OLD_MEMSTORE
SELECT NAME, MIN_CPU, MAX_CPU, MEMORY_SIZE
  FROM oceanbase.DBA_OB_UNIT_CONFIGS WHERE NAME='<unit>';         -- sys 租户（集群对象）；记为 OLD_MIN_CPU/OLD_MAX_CPU/OLD_MEMORY_SIZE

-- 1) 变更（示例值，按需要填）
ALTER SYSTEM SET freeze_trigger_percentage=30;                    -- 业务租户（或 sys 下加 TENANT=<tenant>）
ALTER SYSTEM SET _memstore_limit_percentage=50;                   -- 业务租户（或 sys 下加 TENANT=<tenant>）；租户级临时杠杆
-- 仅当确实要全局调整时，在 sys 租户执行（影响所有租户，不是租户临时手段）：
-- ALTER SYSTEM SET memstore_limit_percentage=50;                 -- sys 租户，集群级
ALTER RESOURCE UNIT <unit> MIN_CPU=<new_cpu>,MAX_CPU=<new_cpu>,MEMORY_SIZE='<new_mem>';  -- sys 租户，集群对象

-- 2) 恢复：用第 0 步读到的原值，不要写死 10/20G/20/0（否则会把 20 CPU/40G 缩容）
ALTER SYSTEM SET freeze_trigger_percentage=<OLD_FREEZE>;          -- 业务租户（或 sys + TENANT=<tenant>）
ALTER SYSTEM SET _memstore_limit_percentage=<OLD_TENANT_MEMSTORE>; -- 业务租户；原值未显式设置时按版本确认如何回到租户默认值，不要写死
-- 若第 1 步动过集群级 memstore，则在 sys 恢复：
-- ALTER SYSTEM SET memstore_limit_percentage=<OLD_MEMSTORE>;      -- sys 租户，集群级
ALTER RESOURCE UNIT <unit> MIN_CPU=<OLD_MIN_CPU>,MAX_CPU=<OLD_MAX_CPU>,MEMORY_SIZE='<OLD_MEMORY_SIZE>';  -- sys 租户

-- 3) 回滚后核对：再查一次，逐项与第 0 步比对，不一致立即修正
SHOW PARAMETERS LIKE 'freeze_trigger_percentage';                 -- 业务租户（或 sys + TENANT=<tenant>）
SHOW PARAMETERS LIKE '_memstore_limit_percentage';                -- 业务租户
SHOW PARAMETERS LIKE 'memstore_limit_percentage';                 -- sys 租户（集群级）
SELECT NAME, MIN_CPU, MAX_CPU, MEMORY_SIZE
  FROM oceanbase.DBA_OB_UNIT_CONFIGS WHERE NAME='<unit>';         -- sys 租户（集群对象）
```

## 4. SQL/审计/计划缓存

| 参数 | 默认 | 说明 | 实测/风险 |
| --- | --- | --- | --- |
| `enable_sql_audit` | true | SQL 审计 | 高 QPS 有开销；关闭损失诊断/合规 |
| `trace_log_slow_query_watermark` | 1s | 慢查询阈值 | 过低日志多 |
| `plan_cache_evict_interval` | 5s | 计划缓存淘汰 | 过短频繁重编译 |
| `enable_sql_operator_dump` | true | 算子落盘 | 关闭可能导致内存爆 |
| `large_query_threshold` | 5s | 大查询判定 | 影响降级 |

## 5. obproxy

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `work_thread_num` | 128（实测模型） | 与连接数/并发匹配；过高上下文切换 |
| 压缩协议 | 按需 | 可降带宽，增 CPU |
| 连接池/超时 | 按业务 | 避免连接风暴 |

实测：obproxy 参数/编译优化收益有限（约 1.7%~4.1%），且客户可能不接受改测试模型。
优先解决 observer 侧。

## 6. sysbench / 压测参数

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `threads` | 800（32U） | 与规格/客户端匹配；过高客户端先瓶颈 |
| `table_size` | 50w/100w | 与工作集/缓存匹配 |
| `tables` | 1（标准）/30（扩展性） | 扩展性专项用 30 表 |
| `--db-ps-mode=disable` | 常用 | 与业务 prepared statement 行为不同 |
| `--rand-type=uniform` | 常用 | 影响冲突率与热点 |

> 改压测参数属于“运行点优化”，不是服务器配置收益；报告要区分。
## 7. 参数变更纪律

1. 先导出 `SHOW PARAMETERS` 全量，记录租户 Unit（CPU/内存的原值）。
2. 一次一个变量；集群级/租户级分开：租户临时杠杆用 `_memstore_limit_percentage` 与
   `freeze_trigger_percentage`（业务租户或 sys + `TENANT=`），`memstore_limit_percentage`
   与 Unit 规格只在 sys 租户操作、按集群级影响评估。
3. 动态 vs 需重启（STATIC）分开安排。
4. 回滚 SQL 用变更前的实际值生成，不写死默认值；没有回滚不改。
5. 交易类临时参数必须恢复并重启验证；回滚后重读参数与 Unit，逐项核对与原值一致。
6. 大规格先确认 `net_thread_count` 与租户 CPU，再谈其它参数。

## 8. 参数—现象对照

| 现象 | 优先参数 | 不要先动 |
| --- | --- | --- |
| 128U 跑不满、sql_nio 高 | `net_thread_count` | `workers_per_cpu_quota` |
| 租户 CPU 满、observer 未满 | 租户 `cpu_num`/Unit | `cpu_count` |
| 写抖动、冻结频繁（租户） | `freeze_trigger_percentage`、`_memstore_limit_percentage`（租户级）；集群级 `memstore_limit_percentage` 仅 sys 全局调整 | `memory_limit` |
| 写限流、尾延迟 | `writing_throttling_trigger_percentage` | 关闭限流 |
| 排序/聚合落盘 | `_sort_area_size`、`_hash_area_size` | 只加内存 |
| 高 QPS CPU 抬升 | `enable_sql_audit`、trace | 长期关审计 |
| 计划反复编译 | `plan_cache_evict_interval`、schema 稳定性 | 关闭计划缓存 |
