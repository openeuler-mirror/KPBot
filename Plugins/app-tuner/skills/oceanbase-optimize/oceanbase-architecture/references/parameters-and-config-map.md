# 参数地图与影响面

参数种子：`src/share/parameter/ob_parameter_seed.ipp`（4.3.5 约 2800+ 行）；
宏定义：`src/share/parameter/ob_parameter_macro.h`。

## 1. 定义与生效

```
DEF_INT(name, scope, def, range, desc, attr)
DEF_BOOL(name, scope, def, desc, attr)
DEF_STR(name, scope, def, desc, attr)
DEF_TIME(name, scope, def, range, desc, attr)
DEF_CAP(name, scope, def, range, desc, attr)
DEF_DBL(name, scope, def, range, desc, attr)

OB_CLUSTER_PARAMETER   // 集群级
OB_TENANT_PARAMETER    // 租户级
STATIC_EFFECTIVE / DYNAMIC_EFFECTIVE  // 是否需重启
```

查看与修改：

```
SHOW PARAMETERS LIKE '%memory%';
ALTER SYSTEM SET memory_limit = '250G';
ALTER SYSTEM SET cpu_count = 32;
-- 4.3.5：freeze_trigger_percentage 是租户级参数；sys 会话不指定 TENANT 只改 sys 自己
ALTER SYSTEM SET freeze_trigger_percentage = 30 TENANT = 'mysql_tenant';
-- 回读必须连到目标租户：sys 会话的 SHOW PARAMETERS 不能证明业务租户已更新
-- （在 mysql_tenant 会话中执行）
SHOW PARAMETERS LIKE 'freeze_trigger_percentage';
ALTER SYSTEM SET _sort_area_size = '64M' TENANT = 'mysql_tenant';
```

> 以下划线开头的隐藏参数与 `_` 前缀项只建议在测试环境验证；生产变更需评审与回滚方案。

## 2. 资源与规格

| 参数 | 默认 | 范围/说明 | 影响 |
| --- | --- | --- | --- |
| `cpu_count` | 0（自动） | observer 可用 CPU 数 | 决定 worker/PX/IO 线程规模；容器内需显式配 |
| `memory_limit` | 0M（自动） | observer 内存上限 | 过小导致 cache 洗出；过大挤压 OS |
| `memory_limit_percentage` | 80 | [10,95] | 与 memory_limit 二选一 |
| `system_memory` | 0M | 系统保留 | 影响租户可用内存 |
| `net_thread_count` | 0 | 0=max(6,CPU/8) | sql_nio 数；128U 实测需上调 |
| `tenant_task_queue_size` | 16384 | [1024,) | 每租户队列长度 |
| `workers_per_cpu_quota` | 10 | [2,20] | 4.x OMT 路径上不再驱动 worker 数（仅作 `times_of_workers` 构造参数传入后未使用）；调它无效，见 memory-and-thread-model.md §4 |
| `cpu_quota_concurrency` | 4 | [1,20]（租户） | 每 CPU 并发度 |
| `px_workers_per_cpu_quota` | 10 | — | 并行执行度 |
| `large_query_worker_percentage` | 30 | [0,100] | 大查询保留 worker |
| `large_query_threshold` | 5s | — | 大查询判定 |

## 3. 内存与冻结

| 参数 | 默认 | 说明 | 调参注意 |
| --- | --- | --- | --- |
| `memstore_limit_percentage` | 0（自动） | MemStore 占比 | 交易类写多场景实测会临时调到 50 |
| `freeze_trigger_percentage` | 20 | 租户级冻结阈值 | 交易创建实测临时调到 30；恢复要改回 |
| `writing_throttling_trigger_percentage` | 60 | 写限流 | 调低提前限流、护住 MemStore 但损性能；调高推迟限流，转储跟不上时更易耗尽 MemStore |
| `memory_reserved` | 500M | 紧急保留 | 不宜压缩 |
| `cache_wash_threshold` | 4GB | 缓存洗出阈值 | 过小频繁洗出 |
| `_sort_area_size` | 32M | 每算子排序内存 | 大排序易 dump；过大并发时爆内存 |
| `_hash_area_size` | 32M | 每算子哈希内存 | 同上 |
| `query_memory_limit_percentage` | 50 | 查询内存占比 | 影响并发查询数 |
| `_tx_share_memory_limit_percentage` | 0 | 事务共享内存 | 高并发事务 |
| `_tx_data_memory_limit_percentage` | 20 | TxData 内存 | 长事务/大事务 |

## 4. SQL、审计与计划缓存

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `enable_sql_audit` | true | SQL 审计；高 QPS 有开销，压测可评估关闭 |
| `enable_record_trace_log` | true | 记录 trace |
| `trace_log_slow_query_watermark` | 1s | 慢查询阈值 |
| `plan_cache_evict_interval` | 5s | 计划缓存淘汰间隔 |
| `enable_sql_operator_dump` | true | 算子允许落盘 |
| `ob_vector_memory_limit_percentage` | 0 | 向量索引内存（如使用） |

## 5. 事务、日志与副本

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `undo_retention` | 1800 | undo 保留秒数；影响历史版本与读放大 |
| `_replay_memory_limit` | 512M | 回放内存 |
| `_replay_pending_log_memory_limit` | 128M | 回放 pending 日志内存 |
| `weak_read_version_refresh_interval` | 100ms | 弱读版本刷新 |
| `schema_history_expire_time` | 7d | schema 历史保留 |

## 6. 存储与 IO

| 参数/配置 | 说明 | 影响 |
| --- | --- | --- |
| `data_dir` / `datafile_size` | 数据目录与数据文件大小 | 空间与 IO 分布 |
| `clog_path` / `slog_path` | 日志路径 | 与 data 分盘降低干扰 |
| `default_progressive_merge_num` | 渐进合并次数 | 大合并对前台影响 |
| IO Manager 内存上限 | 默认约 10GB（版本相关） | 异步 IO 请求池 |

## 7. 参数与瓶颈的对应关系

| 瓶颈证据 | 先看参数 | 不要先动 |
| --- | --- | --- |
| sql_nio 单线程 <数值>%、收包热点 | `net_thread_count` | `workers_per_cpu_quota` |
| worker 排队、CPU 未满 | 租户 `min_cpu`/`max_cpu`、`cpu_quota_concurrency`（走 min/max_worker_cnt 公式） | `workers_per_cpu_quota`（4.x 死参数）、`cpu_count`（已满时无效） |
| 写抖动、冻结频繁 | `freeze_trigger_percentage`、`memstore_limit_percentage` | `memory_limit` 无脑调大 |
| 写限流、尾延迟 | `writing_throttling_trigger_percentage` | 盲目关闭限流 |
| 排序/哈希 dump | `_sort_area_size`、`_hash_area_size`、`query_memory_limit_percentage` | 只加内存不看并发 |
| 计划反复编译 | `plan_cache_evict_interval`、schema 稳定性 | 关闭计划缓存 |
| 高 QPS 下 CPU 抬升 | `enable_sql_audit`、trace 级别 | 长期关闭审计 |
| 大规格跑不满 | 租户 `cpu_num`/`memory_size`、`net_thread_count` | 只调 `cpu_count` |

## 8. 变更纪律

1. 先记录现状：`SHOW PARAMETERS` 全量导出、租户 Unit、observer 启动参数、二进制 md5。
2. 一次只改一个变量；集群级与租户级分开验证。
3. 需要重启的（STATIC）与动态生效的分开安排窗口。
4. 记录改前→改后、验证 SQL、回滚 SQL；没有回滚语句不得变更。
5. 交易类业务模型常需要临时调冻结/内存参数，测试后必须按原值恢复并重启集群验证。
