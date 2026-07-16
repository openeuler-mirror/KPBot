# 数据库专项分析参考 / Database Analysis Reference

## 目标

本参考文档用于在服务器应用优化流程中插入数据库或数据库驱动型工作负载的内部状态分析，避免在数据库内部状态已经解释问题时过早进入编译器、绑核或库替换方向。

## 通用分析框架

适用于以下场景：

- 当前服务本身就是数据库
- CPU 热点主要出现在数据库进程
- 业务瓶颈明显依赖数据库内部等待、锁、刷盘或缓存命中
- 用户提供了数据库状态信息，希望判断是否值得继续 CPU 深挖

推荐分析顺序：

1. 识别数据库类型和版本
2. 识别主要症状：高 CPU、低吞吐、高时延、锁等待、刷盘抖动
3. 判断瓶颈更像内部等待、缓存失效、刷盘压力还是纯 CPU 计算
4. 若数据库内部状态已能解释现象，优先输出数据库内部优化方向
5. 只有在数据库内部瓶颈被排除后，才继续 CPU 深挖

## MySQL / InnoDB 示例

建议重点采集以下内容：

- `SHOW ENGINE INNODB STATUS`
- `SHOW GLOBAL STATUS`
- `SHOW VARIABLES`

建议重点关注：

- Buffer Pool 命中率
- Row Lock Wait
- Adaptive Hash Index 效率
- Checkpoint / Flush 压力
- 后台线程与并发等待
- redo / undo 相关压力

### AHI 场景化判断

AHI 适合解决的问题：

- 随机读较多且 CPU 仍有余量的场景
- B+Tree 查找成本较高，希望通过哈希命中降低查找开销的场景

AHI 在 CPU-bound 读多场景下可能适得其反的原因：

- 哈希表维护会引入额外 CPU 开销
- 高线程密度下可能放大 latch 竞争
- 当 Buffer Pool 命中率已经很高时，AHI 的额外收益可能不足以覆盖维护成本

建议新增采集字段：

- `mysqld_cpu_pct`
- `threads_per_core`
- `buffer_pool_hit_rate`
- `workload_type`

建议判断规则：

- 若 `mysqld_cpu_pct > 90` 且 `threads_per_core > 4` 且 `buffer_pool_hit_rate > 95` 且 `workload_type` 属于 `read_only` 或 `read_heavy`
  - 推荐 `AHI=OFF`
- 若 `mysqld_cpu_pct < 70` 或 `buffer_pool_hit_rate < 90` 或 `threads_per_core <= 2`
  - 保持 `AHI=ON`
- 若处于中间区间
  - 输出 `need_more_evidence`
  - 建议纳入下一轮串行验证

## 输出建议

数据库专项分析输出至少包括：

- 数据库内部状态是否为主要因素
- 关键证据
- 建议优先优化的数据库方向
- 是否继续 CPU 深挖
- 当前判断
- 触发条件
- 推荐动作
- 风险说明
- 是否建议进入下一轮串行验证
