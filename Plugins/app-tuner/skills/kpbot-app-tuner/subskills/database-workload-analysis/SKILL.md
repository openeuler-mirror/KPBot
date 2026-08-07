---
name: database-workload-analysis
description: 用于数据库或数据库驱动型工作负载的专项分析子 skill，当前提供数据库通用框架与 MySQL/InnoDB 示例，帮助判断数据库内部状态是否足以解释当前瓶颈。
---

# Database Workload Analysis

本子 skill 保留在 `subskills/` 目录中，供 `application-config-optimization` 按需引用。

它不单独改写主流程路由；对外数据库专项归属为：

- `application-config-optimization`

当当前场景属于数据库本体，或热点主要落在数据库进程、存储引擎或数据库访问路径上时，由 `application-config-optimization` 视需要引用本子 skill。

## Recommended Inputs

- `database_engine` — 数据库类型（mysql、postgresql 等）
- `mysqld_cpu_pct` — mysqld 进程 CPU 占用百分比
- `threads_per_core` — 每核线程密度
- `buffer_pool_hit_rate` — Buffer Pool 命中率
- `ahi_current_state` — 当前 AHI 状态（ON / OFF）
- `workload_type` — 工作负载类型（read_only、read_heavy、write_heavy 等）
- `SHOW ENGINE INNODB STATUS` 输出（MySQL 场景）
- `SHOW GLOBAL STATUS` 输出（MySQL 场景）
- `SHOW VARIABLES` 输出（MySQL 场景）

## 适用场景

- MySQL、PostgreSQL 等数据库服务本体
- CPU 热点主要位于数据库进程
- 用户已提供数据库状态输出
- 当前问题更像锁等待、缓存失效、刷盘压力或内部并发问题

## 首轮实现范围

当前版本提供：

- 数据库通用分析框架
- MySQL/InnoDB 示例

## MySQL / InnoDB 重点关注

- Buffer Pool 命中率
- Row Lock Wait
- Adaptive Hash Index
- Checkpoint / Flush 压力
- 后台线程与并发等待
- `SHOW ENGINE INNODB STATUS`
- `SHOW GLOBAL STATUS`
- `SHOW VARIABLES`

## AHI Decision Logic

当前版本要求对 MySQL/InnoDB 的 Adaptive Hash Index 做场景化判断，而不是只采集状态。

### Recommend `AHI=OFF`

若同时满足以下条件，则推荐 `AHI=OFF`：

- `mysqld_cpu_pct > 90`
- `threads_per_core > 4`
- `buffer_pool_hit_rate > 95`
- `workload_type in ('read_only', 'read_heavy')`

推荐理由：

- 在 CPU-bound 的读多场景下，AHI 维护开销（latch 竞争 + 哈希表更新）可能大于 B+Tree 命中加速带来的收益

### Keep `AHI=ON`

若满足以下任一条件，则建议保持 `AHI=ON`：

- `mysqld_cpu_pct < 70`
- `buffer_pool_hit_rate < 90`
- `threads_per_core <= 2`

推荐理由：

- 当 CPU 仍有余量，或者缓存命中率和线程密度未到高压区间时，AHI 在随机读场景下可能仍然有正收益

### Need More Evidence

若指标落在中间区间，则输出：

- `ahi_recommendation=need_more_evidence`

并建议：

- 通过小步试验把 `AHI=OFF` 纳入下一轮串行累计验证
- 记录收益、风险和是否继续保留该变更

## 输出要求

输出应至少包括：

- 数据库内部状态是否为主要因素
- 关键证据
- 推荐优先动作
- 是否继续 CPU 深挖
- 需要补充采集的项
- `ahi_recommendation`
- `ahi_reason`
- `ahi_decision_evidence`
- `ahi_validation_next_step`
- `next_round_candidate`
- `validated_in_current_round`
- `keep_for_next_round`

当前版本要求数据库专项分析不仅输出“建议是什么”，还要说明：

- 哪些动作应作为当前轮优先动作
- 哪些动作应纳入下一轮继续验证
- 哪些动作在当前轮验证后应继续保留

输出结果默认回流到 `application-config-optimization`，由其统一纳入当前轮候选动作和主流程结论。

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `mysql` 客户端 | MySQL 状态采集 | MySQL 专项分析不可用 |
| 目标数据库客户端 | 对应数据库状态采集 | 对应数据库专项分析不可用 |
| `perf` | 数据库进程热点分析 | 热点函数分析降级 |
| `pidstat` | 数据库进程 CPU 构成分析 | CPU 构成分析降级 |

缺失时不伪造数据库内部状态，显式说明降级范围。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。数据库参数、AHI、buffer/cache、连接池或后台线程相关动作默认回流到 `application-config-optimization`，rollback 必须包含恢复原参数和数据库健康检查。
